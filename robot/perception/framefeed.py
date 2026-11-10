#!/usr/bin/env python3
"""framefeed.py — serve the frames gaze.py is already reading, to a browser.

WHY THIS IS PART OF THE GAZE PROCESS AND NOT ITS OWN VIEWER. The OAK-D is
exclusive: one DepthAI process at a time. During a run that process is
`gaze.service`, so a viewer that opens the camera for itself cannot co-exist with
the robot doing its job — that is exactly why `oak-viewer.service` carries
`Conflicts=gaze.service` and stops the eyes when you start it. That makes it a
pre-flight tool ("can the camera see me?") and useless DURING a session, which is
when an operator most wants to see what the robot is looking at.

The frames a monitor wants are the ones gaze is already holding. So gaze
annotates them with what it detected and publishes the JPEGs here, and this serves
them. Nothing but gaze ever touches the camera, and the robot's own behaviour is
untouched: with nobody watching, gaze does not encode a single frame.

    gaze.py --stream 8080          # publishes into this server (see gaze.py --help)
    python3 framefeed.py --port 8080 --selftest   # serve synthetic frames, no camera

Then browse to  http://<jetson-ip>:8080/  — the page shows the live stream plus the
same numbers the robot is acting on, polled from the gaze status file.

Serving is stdlib only. The JPEGs are made by the caller (gaze.py, with cv2), so
this module has no image code and no depthai import, and can be tested with no
camera and no venv.

THE PORT IS THE SAME AS camera/viewer.py's DEFAULT (8080) ON PURPOSE: the two are
mutually exclusive by construction, so there is one URL to remember — if it is
showing the raw feed, gaze is stopped; if it is showing boxes and a verdict, the
robot is running.

Do not expose this port beyond the LAN. It serves pictures of whoever is in front
of the robot to anyone who can reach it, with no authentication: that is fine on a
home/lab network and is exactly what was asked for, and it is not fine anywhere
else.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A client that has fetched a frame in the last this many seconds counts as
# watching. gaze asks `wanted()` before encoding anything, so an unwatched robot
# pays nothing for a feature nobody is using.
IDLE_SECS = 5.0
BOUNDARY = "gazeframe"

# A 1x1 dark JPEG, so --selftest works on a machine with no cv2 and no camera.
# Only ever used by --selftest; every real frame comes from gaze.
PLACEHOLDER_JPEG = base64.b64decode(
    b"/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    b"Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    b"AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def _jpeg_ok(blob: bytes) -> bool:
    """A JPEG starts with SOI and ends with EOI. Cheap, and it catches the one
    failure that otherwise looks like success: a stream that carries no image."""
    return bool(blob) and blob[:2] == b"\xff\xd8" and blob[-2:] == b"\xff\xd9"


class FrameStore:
    """The newest frame, protected by a lock, with a sequence number so a reader
    can tell a new frame from the one it just sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._meta: dict = {}
        self.published = 0
        self.last_client_t = 0.0

    def put(self, jpeg: bytes, meta: dict | None = None) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._seq += 1
            self.published += 1
            if meta:
                self._meta = meta

    def get(self) -> tuple[bytes | None, int]:
        with self._lock:
            return self._jpeg, self._seq

    def set_meta(self, **meta) -> None:
        with self._lock:
            self._meta.update(meta)

    def meta(self) -> dict:
        with self._lock:
            return dict(self._meta)

    def watching(self) -> bool:
        """Is anyone reading? gaze uses this to skip the encode entirely."""
        with self._lock:
            return (time.monotonic() - self.last_client_t) < IDLE_SECS

    def saw_client(self) -> None:
        with self._lock:
            self.last_client_t = time.monotonic()

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self.last_client_t if self.last_client_t else -1.0


class Feed:
    """The running server. `publish()` is the caller's whole API."""

    def __init__(self, port: int, status_path: str | None = None,
                 bind: str = "0.0.0.0") -> None:
        self.store = FrameStore()
        self.port = port
        self.status_path = status_path
        self.started_t = time.monotonic()
        self.served = 0
        self.clients = 0
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer((bind, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="framefeed", daemon=True)

    def start(self) -> "Feed":
        self.thread.start()
        return self

    def publish(self, jpeg: bytes, **meta) -> None:
        self.store.put(jpeg, meta or None)

    def wanted(self) -> bool:
        return self.store.watching()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    def status_text(self) -> str:
        jpeg, seq = self.store.get()
        m = self.store.meta()
        age = self.store.age()
        return "\n".join([
            f"port {self.port}",
            f"uptime {time.monotonic() - self.started_t:.0f}s",
            f"frames published {self.store.published} (last seq {seq}, "
            f"{len(jpeg) if jpeg else 0} bytes)",
            f"streams served {self.served}",
            f"last client {age:.1f}s ago" if age >= 0 else "last client never",
            f"watching {'yes' if self.store.watching() else 'no'}",
            *(f"{k} {v}" for k, v in sorted(m.items())),
        ]) + "\n"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>robot eye - gaze feed</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #0d0b13; color: #d1d6e3;
         font: 14px/1.5 system-ui, sans-serif; display: flex; flex-wrap: wrap; }
  #left { flex: 1 1 640px; padding: 16px; }
  #right { flex: 0 0 320px; padding: 16px; border-left: 1px solid #26232f; }
  img { width: 100%; height: auto; background: #000; border-radius: 8px;
        border: 1px solid #26232f; }
  h1 { font-size: 15px; font-weight: 600; margin: 0 0 4px; }
  p.note { margin: 0 0 16px; color: #8b90a0; font-size: 12px; }
  table { border-collapse: collapse; width: 100%; }
  td { padding: 4px 0; border-bottom: 1px solid #1c1a24; }
  td.k { color: #8b90a0; }
  td.v { text-align: right; font-variant-numeric: tabular-nums; }
  .subject { color: #6ad08e; }
  .notsubject { color: #d8a13c; }
  .nobody { color: #8b90a0; }
  #verdict { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
  code { color: #7fd1e8; }
</style>
</head>
<body>
<div id="left">
  <h1>Robot eye &mdash; what gaze is reading right now</h1>
  <p class="note">These are the frames the running gaze process is already
  analysing. Nothing here opens the camera, so watching does not disturb a
  session.</p>
  <img src="/stream" alt="live camera with detections drawn">
  <p class="note">Boxes: <span class="subject">green</span> = someone in front of
  the robot, <span class="notsubject">amber</span> = a face the subject gate
  rejected, grey = raw detector hit. Circles are the five landmarks the head pose
  is derived from.</p>
</div>
<div id="right">
  <div id="verdict" class="nobody">waiting for the status file...</div>
  <table id="table"></table>
  <p class="note">/healthz for the feed itself, /frame.jpg for a single frame,
  /status.json for the raw status.</p>
</div>
<script>
const ROWS = ["has_face","subject","faces","gaze","expression","fps_avg",
              "hit_rate","subject_rate","dry_run"];
function fmt(v) {
  if (v === null) return "-";
  if (typeof v === "object") return JSON.stringify(v);
  if (typeof v === "number") return (Math.round(v * 100) / 100).toString();
  return String(v);
}
async function poll() {
  try {
    const s = await (await fetch("/status.json", {cache: "no-store"})).json();
    const v = document.getElementById("verdict");
    if (s.subject) { v.textContent = "SUBJECT - someone is in front"; v.className = "subject"; }
    else if (s.has_face) { v.textContent = "face seen, gate rejected it"; v.className = "notsubject"; }
    else { v.textContent = "nobody in front"; v.className = "nobody"; }
    document.getElementById("table").innerHTML = ROWS
      .filter(k => k in s)
      .map(k => `<tr><td class="k">${k}</td><td class="v">${fmt(s[k])}</td></tr>`)
      .join("");
  } catch (e) {
    document.getElementById("verdict").textContent = "no status file yet";
  }
  setTimeout(poll, 1000);
}
poll();
</script>
</body>
</html>
"""


def _make_handler(feed: Feed):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gazefeed/1.0"

        def log_message(self, fmt, *args):  # noqa: A002 - base class API
            # The page polls /status.json every second and /stream runs
            # continuously; logging those writes tens of thousands of lines a day
            # and buries anything worth reading.
            if self.path.split("?")[0] not in ("/stream", "/status.json", "/healthz"):
                sys.stderr.write("framefeed: %s\n" % (fmt % args))

        def _send(self, code: int, ctype: str, body: bytes,
                  extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - base class API
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
                return
            if path == "/healthz":
                self._send(200, "text/plain; charset=utf-8",
                           feed.status_text().encode())
                return
            if path == "/status.json":
                feed.store.saw_client()
                body = b"{}"
                if feed.status_path:
                    try:
                        body = pathlib.Path(feed.status_path).read_bytes()
                    except OSError:
                        body = b'{"error": "no status file yet"}'
                self._send(200, "application/json", body)
                return
            if path == "/frame.jpg":
                feed.store.saw_client()
                jpeg, _ = feed.store.get()
                if not jpeg:
                    self._send(503, "text/plain", b"no frame yet\n")
                else:
                    self._send(200, "image/jpeg", jpeg)
                return
            if path == "/stream":
                self._stream()
                return
            self._send(404, "text/plain", b"try /, /stream, /frame.jpg, "
                                           b"/status.json, /healthz\n")

        def _stream(self) -> None:
            feed.store.saw_client()
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            feed.served += 1
            last_seq = -1
            try:
                while True:
                    jpeg, seq = feed.store.get()
                    if jpeg is not None and seq != last_seq:
                        last_seq = seq
                        feed.store.saw_client()
                        self.wfile.write(b"--" + BOUNDARY.encode() + b"\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(b"Content-Length: %d\r\n\r\n"
                                         % len(jpeg))
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                    # A little faster than any producer we expect, so a new frame
                    # goes out immediately without spinning.
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                # A closed tab is normal, not an error.
                return
            except OSError:
                return

    return Handler


def start(port: int, status_path: str | None = None,
          bind: str = "0.0.0.0") -> Feed:
    """Start the feed. Raises OSError if the port is taken - the caller decides
    whether that is fatal, because a monitoring feature must never take the eyes
    down."""
    return Feed(port, status_path, bind).start()


def selftest_frames(feed: Feed, count: int = 40) -> None:
    """Publish synthetic frames so the server can be exercised with no camera.

    Uses cv2 when it is importable (the robot's venv has it) to make a real,
    moving, non-trivial JPEG; falls back to a placeholder so this runs anywhere.
    """
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        for i in range(count):
            feed.publish(PLACEHOLDER_JPEG, source="placeholder", frame=i)
            time.sleep(0.1)
        return
    for i in range(count):
        img = np.zeros((360, 640, 3), dtype="uint8")
        img[:, :] = (30, 20, 15)
        x = 40 + (i * 6) % 480
        cv2.circle(img, (x, 180), 60, (90, 200, 140), 2)
        cv2.putText(img, "selftest frame %d" % i, (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (210, 214, 227), 2)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            feed.publish(buf.tobytes(), source="selftest", frame=i,
                         size="640x360")
        time.sleep(0.1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Serve the frames gaze.py publishes, to a browser.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to listen on (default %(default)s). Keep it on "
                         "the LAN: this serves pictures of whoever is in front of "
                         "the robot, with no authentication")
    ap.add_argument("--status", default="/run/face/gaze-status.json",
                    help="the gaze status file to serve at /status.json")
    ap.add_argument("--selftest", action="store_true",
                    help="serve synthetic frames instead of waiting for gaze, so "
                         "the page can be checked with no camera and no session")
    args = ap.parse_args()

    try:
        feed = start(args.port, args.status, args.bind)
    except OSError as exc:
        print(f"framefeed: cannot listen on {args.bind}:{args.port}: {exc}")
        return 1

    # Say where to browse. The address is whatever the LAN can reach, so print the
    # hostname and any non-loopback IPv4 rather than claiming one.
    host = socket.gethostname()
    addrs = {host}
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    print(f"framefeed: serving on {args.bind}:{args.port}"
          + ("  (synthetic frames - no camera)" if args.selftest else "")
          + "\n  browse to " + "  or  ".join(
              f"http://{a}:{args.port}/" for a in sorted(addrs))
          + f"\n  status: {args.status}", flush=True)

    if args.selftest:
        threading.Thread(target=selftest_frames, args=(feed,),
                         name="selftest", daemon=True).start()

    stopping = threading.Event()

    def on_signal(signum, _frame):
        print(f"\nframefeed: signal {signum}, stopping", flush=True)
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        while not stopping.is_set():
            stopping.wait(1.0)
    finally:
        feed.stop()
    print(feed.status_text(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
