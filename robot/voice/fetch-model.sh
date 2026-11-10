#!/usr/bin/env bash
# Fetch the Vosk speech model keyword.py needs.
#
# WHY THIS IS A SCRIPT AND NOT A COMMITTED DIRECTORY
#   The model is 68 MB unpacked and never changes. Committed, it would bloat
#   every clone and every offline history bundle. Not committed, it still has to
#   exist on the device, because THE LAB HAS NO LAN and nothing can be fetched
#   there. So: fetch it here, once, while you have network. If the board is
#   reimaged, run this again from home before you leave.
#
# The URL and the checksum are pinned together, so a truncated download or a
# changed file is caught here rather than blamed on the recogniser later.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MODELS="$HERE/models"
NAME=vosk-model-small-en-us-0.15
URL="https://alphacephei.com/vosk/models/${NAME}.zip"
SHA256=30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498

say() { printf '%s\n' "$*"; }

mkdir -p "$MODELS"
cd "$MODELS"

if [ -d "$NAME" ]; then
  say "already unpacked: $MODELS/$NAME"
  say "nothing to do."
  exit 0
fi

if [ ! -f "$NAME.zip" ]; then
  say "== downloading $URL =="
  python3 - "$URL" "$NAME.zip" <<'PY'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
print("  done")
PY
fi

say
say "== verifying sha256 =="
got=$(sha256sum "$NAME.zip" | cut -d' ' -f1)
if [ "$got" != "$SHA256" ]; then
  say "FATAL: checksum mismatch" >&2
  say "  expected $SHA256" >&2
  say "  got      $got" >&2
  say "  the file is truncated or has changed; deleting it." >&2
  rm -f "$NAME.zip"
  exit 1
fi
say "  ok $SHA256"

say
say "== unpacking =="
python3 -c "import zipfile; zipfile.ZipFile('$NAME.zip').extractall('.')"
du -sh "$NAME"
say
say "model ready: $MODELS/$NAME"
