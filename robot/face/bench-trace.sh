#!/usr/bin/env bash
# bench-trace.sh — sample the conductor's session file at 2 Hz during a bench run.
#
# READ-ONLY. Run it ON the board, not through an SSH pipe: a pipe block-buffers, so
# an operator watching a live session sees the trace lag by kilobytes, which is
# exactly when it is least useful. Writes its own file so it can be tailed live:
#
#   bash bench-trace.sh /tmp/bench-trace.txt 2400 &     # 20 minutes at 2 Hz
#   tail -f /tmp/bench-trace.txt
#
# WHY SAMPLE AT ALL: "the bar filled" and "the bar filled BECAUSE the person was
# looking" are different claims, and only the second one is the feature. This records
# `looking` and `progress` on the SAME timestamp line, so the coupling can be shown
# rather than asserted - the fill must freeze while `looking` is false, and must never
# decrease. Pair it with the conductor's own log, which carries the verdict:
#
#   tail /tmp/bench-conductor.log     # "stage 1 session FINISHED - ... gaze bar FAIL"
#   tail /tmp/session-log.txt         # the durable record
#
# NOTE on stopping it: do NOT `pkill -f bench-trace.sh` over SSH. The pattern matches
# the SSH command line running the pkill, so it kills the session instead. Use the pid
# file (or the pgid) that the launcher writes.
set -u

OUT="${1:-/tmp/bench-trace.txt}"
LOOPS="${2:-2400}"          # 20 minutes at 2 Hz

: > "$OUT"
for _ in $(seq 1 "$LOOPS"); do
    printf '%s ' "$(date +%H:%M:%S.%2N)" >> "$OUT"
    python3 -u -c '
import json, sys

try:
    with open("/run/face/session.json") as fh:
        d = json.load(fh)
except Exception as exc:                    # absent, half-written, or garbage
    print("unreadable: %s" % exc)
    sys.exit()

if not isinstance(d, dict) or d.get("phase") != "running":
    print(d.get("phase") if isinstance(d, dict) else "not-a-dict")
    sys.exit()

g = d.get("gaze") or {}
print("stage=%s step=%-8s secs=%-5s progress=%-7s looking=%-5s looked=%-6s verdict=%s"
      % (d.get("stage"), d.get("step"), d.get("seconds"), g.get("progress"),
         g.get("looking"), g.get("looked"), g.get("verdict")))
' >> "$OUT" 2>&1
    sleep 0.5
done
