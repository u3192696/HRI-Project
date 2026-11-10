#!/usr/bin/env python3
"""BENCH ONLY: publish a constant "looking" verdict into /run/face/gaze-status.json.

WHY THIS EXISTS
    The gaze bar and its completion burst are the two things that can only be judged
    on the projector, and filling the bar needs 50 s of a person being judged as
    looking at the camera. That makes an eyeball check depend on standing in exactly
    the right place, which is the wrong dependency to have when what is being checked
    is the DRAWING, not the detector.

    So this writes the verdict the detector would have written, and the rest of the
    chain - the conductor's accumulation, session.json, the kiosk's drawing - runs
    unchanged. What it verifies is therefore the whole path EXCEPT the gaze estimate,
    which is verified separately by looking at the camera for real.

    gaze.service MUST be stopped first: it owns this file, and two writers would be
    a race, not a test. One writer per file.

USAGE (on the board, with gaze.service stopped)
    python3 bench-looking.py /run/face/gaze-status.json 120
"""
import json
import os
import sys
import time


def main(argv):
    path = argv[1] if len(argv) > 1 else "/run/face/gaze-status.json"
    secs = float(argv[2]) if len(argv) > 2 else 120.0
    # 0.2 s: the conductor treats a sample older than GAZE_STALE (0.75 s) as a stalled
    # perception loop, so the file has to stay fresher than that or nothing fills.
    end = time.time() + secs
    n = 0
    while time.time() < end:
        now = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"t": now, "has_face": True, "subject": True, "looking": True,
                       "faces": 1, "subject_rate": 1.0, "hit_rate": 1.0,
                       "dry_run": False, "bench": True}, fh)
        os.replace(tmp, path)      # atomic: the conductor must never read a half file
        n += 1
        time.sleep(0.2)
    print("bench-looking: wrote %d samples in %.1fs" % (n, secs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
