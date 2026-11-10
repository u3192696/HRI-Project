#!/usr/bin/env python3
"""viewer.py — serve the OAK-D Lite camera as an MJPEG web stream.

WHY MJPEG
  The OAK-D encodes JPEG **on the device** (VideoEncoder, MJPEG profile), so the
  Jetson CPU spends nothing on encoding — it just forwards bytes. Browsers
  display `multipart/x-mixed-replace` natively in a plain <img> tag, so there is
  no player, plugin, or JavaScript needed on the client. This is the cheapest
  possible path from camera to eyeball, which matters because the Jetson's CPU
  is also running the robot face and will later run an LLM.

ENDPOINTS
  /                  HTML page showing the live image
  /stream            the MJPEG stream itself (open this in any viewer)
  /snapshot.jpg      a single JPEG, for scripts and for `curl -O`
  /healthz           plain-text status: USB speed, fps, size, uptime

THE CRITICAL CONFIGURATION DETAIL
  `VideoEncoder` accepts **only NV12 or GRAY8** input. Feed it BGR and every
  frame is dropped with:

      [warning] Arrived frame type (2) is not either NV12 or YUV400p (8-bit Gray)

  ...repeating forever, with zero output and no hard error — the pipeline looks
  alive and simply never produces a frame. So the camera output MUST be
  requested as NV12:

      cam.requestOutput((w, h), type=dai.ImgFrame.Type.NV12)

USAGE
  .venv/bin/python viewer.py                      # serve on 0.0.0.0:8080
  .venv/bin/python viewer.py --port 9000 --fps 20 --quality 80
  .venv/bin/python viewer.py --self-test          # verify pipeline, no server

  Then browse to  http://<jetson-ip>:8080/

EXCLUSIVE ACCESS
  DepthAI holds the camera exclusively. Do not run probe.py, capture.py or a
  second viewer while this is running. The robot face does not use the camera,
  so it does not contend.

SECURITY
  This serves an unauthenticated camera image to the whole LAN. That is fine on
  a home/lab network and is exactly what was asked for; do not expose the port
  to the internet without adding authentication.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import depthai as dai

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
BOUNDARY = "oakframe"


class FrameStore:
    """Holds the newest JPEG plus simple stats, safely across threads.

    The capture loop writes, HTTP threads read. A lock is enough: frames are
    read whole and never mutated, so there is no torn-read problem to solve.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._t_start = time.monotonic()
        self._usb = "?"
        self._size = (0, 0)

    def put(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._seq += 1

    def get(self) -> tuple[bytes | None, int]:
        with self._lock:
            return self._jpeg, self._seq

    def set_meta(self, usb: str, size: tuple[int, int]) -> None:
        with self._lock:
            self._usb = usb
            self._size = size

    def status(self, fps: float) -> str:
        with self._lock:
            return (
                f"size={self._size[0]}x{self._size[1]}\n"
                f"usb={self._usb}\n"
                f"fps={fps:.1f}\n"
                f"frames={self._seq}\n"
                f"uptime={time.monotonic() - self._t_start:.0f}s\n"
            )


STORE = FrameStore()
STATE = {"usb": "?", "fps": 0.0, "stop": False}


def build_pipeline(size: tuple[int, int], fps: float, quality: int):
    """Create the device + camera -> NV12 -> MJPEG pipeline."""
    pipeline = dai.Pipeline(dai.Device())
    cam = pipeline.create(dai.node.Camera).build()
    # NV12 is REQUIRED by VideoEncoder - see the module docstring.
    #
    # fps must be set HERE, not only on the encoder. The encoder's frameRate is
    # an upper bound; the camera output rate is what actually throttles. With it
    # set only on the encoder the stream ran at the full sensor rate - measured
    # 30 fps when 15 was requested, doubling the bandwidth for no benefit.
    out = cam.requestOutput(size, type=dai.ImgFrame.Type.NV12, fps=fps)

    encoder = pipeline.create(dai.node.VideoEncoder)
    encoder.build(
        out,
        frameRate=fps,
        profile=dai.VideoEncoderProperties.Profile.MJPEG,
        quality=quality,
    )
    return pipeline, encoder


def capture_loop(pipeline, encoder, stop: threading.Event) -> None:
    """Pull JPEGs off the device and publish the newest one."""
    queue = encoder.out.createOutputQueue(maxSize=8, blocking=False)
    pipeline.start()

    device = pipeline.getDefaultDevice()
    STATE["usb"] = device.getUsbSpeed().name
    STORE.set_meta(STATE["usb"], tuple(STATE["size"]))
    print(f"streaming {STATE['size'][0]}x{STATE['size'][1]} "
          f"over USB {STATE['usb']}", flush=True)

    count = 0
    window = time.monotonic()

    while not stop.is_set():
        packet = queue.tryGet()
        if packet is None:
            time.sleep(0.001)
            continue

        jpeg = bytes(packet.getData())
        # Cheap sanity check: a truncated frame would stream forever as a
        # grey box and be blamed on the browser.
        if len(jpeg) > 4 and jpeg[:2] == JPEG_SOI and jpeg[-2:] == JPEG_EOI:
            STORE.put(jpeg)
            count += 1

        now = time.monotonic()
        if now - window >= 2.0:
            STATE["fps"] = count / (now - window)
            count, window = 0, now


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - base class API
        # The HTML page polls /healthz every 2s and /stream runs continuously,
        # so logging them writes tens of thousands of lines a day and buries
        # anything real. Only log the rare, interesting requests.
        if self.path.split("?")[0] not in ("/stream", "/healthz"):
            sys.stderr.write("  %s %s\n" % (self.address_string(), format % args))

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?")[0]

        if path == "/healthz":
            self._send(200, "text/plain; charset=utf-8",
                       STORE.status(STATE["fps"]).encode())
            return

        if path == "/snapshot.jpg":
            jpeg, _ = STORE.get()
            if jpeg is None:
                self._send(503, "text/plain", b"no frame yet\n")
            else:
                self._send(200, "image/jpeg", jpeg)
            return

        if path == "/stream":
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = -1
            try:
                while not STATE["stop"]:
                    jpeg, seq = STORE.get()
                    if jpeg is None or seq == last:
                        time.sleep(0.005)
                        continue
                    last = seq
                    self.wfile.write(b"--" + BOUNDARY.encode() + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # client closed the tab; normal
            return

        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
            return

        self._send(404, "text/plain", b"not found\n"
                   b"try /, /stream, /snapshot.jpg, /healthz\n")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAK-D Lite — robot camera</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #16161e; color: #c0caf5;
         font: 14px/1.5 ui-monospace, Menlo, Consolas, monospace;
         display: flex; flex-direction: column; align-items: center;
         padding: 16px; gap: 12px; }
  h1 { font-size: 15px; font-weight: 600; margin: 0; color: #7aa2f7; }
  img { max-width: 100%; height: auto; border: 1px solid #2f3549;
        border-radius: 6px; background: #000; }
  .bar { display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
         justify-content: center; }
  a { color: #7dcfff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  #status { color: #565f89; font-size: 12px; white-space: pre; text-align: center; }
  .pill { background: #1f2335; border: 1px solid #2f3549; border-radius: 999px;
          padding: 2px 10px; font-size: 12px; color: #9ece6a; }
</style>
</head>
<body>
  <h1>OAK-D Lite — live camera</h1>
  <img src="/stream" alt="live camera stream">
  <div class="bar">
    <span class="pill" id="usb">&nbsp;</span>
    <a href="/snapshot.jpg">single snapshot</a>
    <a href="/healthz">health</a>
  </div>
  <div id="status">connecting…</div>
<script>
  // Poll /healthz rather than parsing the stream: the stream is a raw
  // multipart response and reading it in JS would defeat the zero-cost design.
  async function tick() {
    try {
      const t = await (await fetch('/healthz', {cache: 'no-store'})).text();
      const o = Object.fromEntries(t.trim().split('\\n').map(l => l.split('=')));
      document.getElementById('status').textContent =
        o.size + '  ' + o.fps + ' fps  ' + o.frames + ' frames  up ' + o.uptime;
      document.getElementById('usb').textContent = 'USB ' + o.usb;
    } catch (e) {
      document.getElementById('status').textContent = 'no response from /healthz';
    }
  }
  tick();
  setInterval(tick, 2000);
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", default="1280x720",
                    help="WIDTHxHEIGHT (default 1280x720)")
    ap.add_argument("--fps", type=float, default=15.0,
                    help="encoder frame rate (default 15 - this is a monitoring "
                         "view, not a recording)")
    ap.add_argument("--quality", type=int, default=75,
                    help="JPEG quality 0-100 (default 75)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to bind (default 0.0.0.0 = whole LAN)")
    ap.add_argument("--self-test", action="store_true",
                    help="open the pipeline, verify JPEGs arrive, then exit")
    args = ap.parse_args()

    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except Exception:
        print(f"bad --size {args.size!r}; expected WIDTHxHEIGHT", file=sys.stderr)
        return 2

    STATE["size"] = (w, h)
    pipeline, encoder = build_pipeline((w, h), args.fps, args.quality)

    if args.self_test:
        # Verify the pipeline produces real JPEGs, then exit. Used to check the
        # NV12 requirement holds before wiring up a server and a browser.
        queue = encoder.out.createOutputQueue(maxSize=8, blocking=False)
        pipeline.start()
        print(f"USB: {pipeline.getDefaultDevice().getUsbSpeed().name}")
        good = bad = 0
        deadline = time.monotonic() + 15
        while good < 10 and time.monotonic() < deadline:
            packet = queue.tryGet()
            if packet is None:
                time.sleep(0.01)
                continue
            jpeg = bytes(packet.getData())
            if jpeg[:2] == JPEG_SOI and jpeg[-2:] == JPEG_EOI:
                good += 1
            else:
                bad += 1
        pipeline.stop()
        print(f"self-test: {good} valid JPEGs, {bad} malformed")
        return 0 if good >= 10 else 1

    stop = threading.Event()

    def on_signal(signum, _frame):
        print(f"\nsignal {signum}, shutting down", flush=True)
        stop.set()
        STATE["stop"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    worker = threading.Thread(target=capture_loop,
                              args=(pipeline, encoder, stop), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.timeout = 1.0
    print(f"serving on http://{args.bind}:{args.port}/  "
          f"({w}x{h} @ {args.fps:g} fps, q{args.quality})", flush=True)

    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        stop.set()
        STATE["stop"] = True
        worker.join(timeout=5)
        try:
            pipeline.stop()
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass
        print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
