#!/usr/bin/env python3
"""Measure torn reads: open("w")+dump vs write-beside-and-rename.

The claim being tested is that the conductor's occasional "no usable gaze status
file" is a reader catching the status file mid-write, and that renaming a complete
file removes it. Two processes, one writing at roughly gaze.py's rate, one reading
as fast as it can, and the reader counts JSON that does not parse.

Run under the project python; needs nothing but the stdlib.
"""
import json
import multiprocessing as mp
import os
import tempfile
import time

# The real file is ~700 bytes with this shape, indent=1, rewritten ~10x a second.
PAYLOAD = {
    "t": 1793805053.2399557, "has_face": True, "subject": True, "looking": False,
    "faces": 2, "face": {"box_w": 0.0951, "box_h": 0.1651, "area": 0.0157,
                         "cx": 0.3034, "cy": 0.5736, "conf": 0.748},
    "gaze": {"x": -1.0, "y": 0.2709}, "gaze_mag": 1.0, "expression": "angry",
    "expression_enabled": True, "yaw_raw": -2.0118, "pitch_raw": 0.1087,
    "debug": {"landmarks": True, "n_kp": 5, "eye_dist": 0.0091,
              "yaw_raw": -2.0118, "pitch_raw": 0.1087, "nose_ratio": 0.5047},
    "fps_avg": 6.74, "hit_rate": 0.842, "subject_rate": 0.15, "dry_run": False,
}


def writer(path, plain, stop):
    while not stop.value:
        if plain:
            with open(path, "w") as fh:
                json.dump(PAYLOAD, fh, indent=1)
        else:
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(PAYLOAD, fh, indent=1)
            os.replace(tmp, path)


def reader(path, stop, counts):
    ok = torn = 0
    while not stop.value:
        try:
            with open(path) as fh:
                json.load(fh)
            ok += 1
        except FileNotFoundError:
            pass
        except ValueError:
            torn += 1
    counts["ok"], counts["torn"] = ok, torn


def run(plain, seconds=3.0):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "gaze-status.json")
    with open(path, "w") as fh:
        json.dump(PAYLOAD, fh, indent=1)
    stop = mp.Value("b", False)
    counts = mp.Manager().dict()
    w = mp.Process(target=writer, args=(path, plain, stop))
    r = mp.Process(target=reader, args=(path, stop, counts))
    w.start(); r.start()
    time.sleep(seconds)
    stop.value = True
    w.join(); r.join()
    ok, torn = counts.get("ok", 0), counts.get("torn", 0)
    total = ok + torn
    rate = (torn / total * 100) if total else 0.0
    return ok, torn, rate


if __name__ == "__main__":
    for plain, label in ((True, 'open("w") + json.dump   (what gaze.py did)'),
                         (False, "write beside it, then os.replace (what it does now)")):
        ok, torn, rate = run(plain)
        print("  %-42s %7d reads, %5d torn (%.3f%%)" % (label, ok + torn, torn, rate))
