#!/usr/bin/env bash
# Render an A2 sheet SVG to PNG + PDF at a verified aspect ratio.
#
# The trap this exists for: rsvg-convert's -p is --dpi-y, NOT the page height.
# `rsvg-convert -f pdf -w 594 -p 420` sets y-DPI to 420 and leaves x at 96, so
# the page comes out 594x420 px while the content is scaled anisotropically -
# the drawing lands as a squashed band across the middle of the sheet.
#
#   ./render-sheet.sh Top_Bracket-A2.svg
set -euo pipefail

SVG="${1:?usage: render-sheet.sh file.svg}"
STEM="${SVG%.svg}"
A2_W_PX=2245      # 594 mm at 96 css dpi; rsvg writes PDF at 72 units/inch,
                  # so this lands as 1684 x 1191 pt = a true A2 page
WANT_RATIO=1.41429

nix shell nixpkgs#librsvg nixpkgs#poppler-utils -c bash -c "
set -e
rsvg-convert -f pdf -w $A2_W_PX '$SVG' -o '$STEM.pdf'
rsvg-convert -w 2600 -b white '$SVG' -o '$STEM.png'
pdfinfo '$STEM.pdf' | grep -Ei 'page size'
"

read -r PW PH < <(nix shell nixpkgs#poppler-utils -c pdfinfo "${STEM}.pdf" \
  | awk '/Page size:/ {print $3, $5}')
RATIO=$(awk -v w="$PW" -v h="$PH" 'BEGIN{printf "%.5f", w/h}')
OK=$(awk -v r="$RATIO" -v t="$WANT_RATIO" 'BEGIN{d=r-t; if(d<0)d=-d; print (d<0.01)?"yes":"no"}')
echo "page ${PW} x ${PH} pt  ratio ${RATIO}  (A2 wants ${WANT_RATIO})  -> $OK"
if [ "$OK" != "yes" ]; then
  echo "REFUSING: page aspect is wrong - the drawing would render squashed." >&2
  exit 1
fi
ls -la "${STEM}.pdf" "${STEM}.png"
