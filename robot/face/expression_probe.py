#!/usr/bin/env python3
"""Measure what the face ACTUALLY draws, instead of trusting the parameters.

WHY THIS EXISTS
  The expression parameters (brow angle, eye openness, mouth curve, mouth open)
  are INPUTS. Whether a mouth reads as a smile is an OUTPUT, and the mapping is
  not obvious: the mouth switches between two completely different renderings
  depending on whether `mouth_open` crosses `w * 0.035` (a filled lens shape
  above, a thin stroked line below), and the apparent depth of a bow is a
  product of both `curve` and `mouth_open`.

  So "make happy happier" cannot be done by editing numbers and hoping. Render
  the mouth, measure the ink, and compare. This is also how to catch the
  reverse mistake: two expressions that look similar on screen when their
  numbers suggest they should not.

  Tuning by measurement is how the pitch constant in gaze.py was fixed too.

USAGE
  # metrics for every expression, at the real kiosk geometry
  python expression_probe.py --report

  # sweep candidates: curve/opening/width-multiplier triples
  python expression_probe.py --sweep 0.55/0.10/1.0 1.0/0.18/1.2

  # render full faces for eyeballing, and contact-sheet them
  python expression_probe.py --png /tmp/x --sweep 0.85/0.16/1.15

METRICS
  width_px    ink width of the mouth
  height_px   ink height at the mouth's centre column (0 for a closed line)
  smile_px    how far the centre sits BELOW the corners. Positive = smile.
              This is the number that decides whether a mouth reads as happy.
  smile_pct   smile_px as a percentage of width_px, so candidates at different
              widths can be compared fairly.
  open        True if it took the filled-lens path, False for the stroked line
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

FACE_PY = os.environ.get("FACE_PY", "/home/jetson/robot/face/face.py")
KIOSK_W, KIOSK_H = 1920, 1080          # the real kiosk geometry
INK = 100                              # alpha above this counts as ink


def load_face():
    """Import face.py by path, without needing it on sys.path."""
    spec = importlib.util.spec_from_file_location("face", FACE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_mouth(mod, curve, opening, w, scale=1.0, pad=0.45):
    """Draw ONLY the mouth onto a transparent surface and return the ink map.

    Isolating the mouth is deliberate: measuring it inside a full face means
    separating mouth ink from eye, brow and outline ink by colour, which is
    fragile. `_draw_mouth` is a standalone primitive, so call it directly.
    """
    import cairo
    W = int(w * (1.0 + 2 * pad))
    H = int(w * 0.9)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    # Start fully transparent, then draw the mouth normally.
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    cx, cy = W / 2.0, H / 2.0
    mod._draw_mouth(cr, cx, cy, w, curve, opening, scale)

    buf = surf.get_data()
    stride = surf.get_stride()
    return W, H, buf, stride, cx, cy


def measure(mod, curve, opening, w, scale=1.0):
    """Return the ink metrics for one mouth configuration.

    ARGB32 is premultiplied and little-endian, so each pixel is B,G,R,A in
    memory order. Only the alpha byte matters here.
    """
    W, H, buf, stride, cx, cy = render_mouth(mod, curve, opening, w, scale)

    cols = {}
    for y in range(H):
        row = y * stride
        for x in range(W):
            if buf[row + x * 4 + 3] > INK:
                lo, hi = cols.get(x, (y, y))
                cols[x] = (min(lo, y), max(hi, y))
    if not cols:
        return None

    xlo, xhi = min(cols), max(cols)
    # Corner height: sample a couple of pixels in from each end, where the
    # shape actually has ink but is still essentially the corner.
    def centre_at(x):
        lo, hi = cols[x]
        return (lo + hi) / 2.0

    inset = 2
    corner_lo1, corner_hi1 = cols[min(xlo + inset, xhi)]
    corner_lo2, corner_hi2 = cols[max(xhi - inset, xlo)]
    corner_y = (centre_at(min(xlo + inset, xhi)) + centre_at(max(xhi - inset, xlo))) / 2.0
    corner_bottom = (corner_hi1 + corner_hi2) / 2.0
    mid_x = (xlo + xhi) // 2
    mid_y = centre_at(mid_x)
    lo, hi = cols[mid_x]

    return {
        "width_px": xhi - xlo,
        "height_px": hi - lo,
        "smile_px": round(mid_y - corner_y, 2),
        "smile_pct": round((mid_y - corner_y) / max(xhi - xlo, 1) * 100.0, 2),
        # How much LOWER the mouth's bottom edge sits at the centre than at the
        # corners. This is the visually salient "smile" for an OPEN mouth: the
        # centroid metric above tracks the ink's middle of mass, which for a
        # lens shape sits only slightly below the corners even when the lower
        # lip sags a long way. Measure both, or an open smile reads as shallow.
        "drop_px": round(hi - corner_bottom, 2),
        "drop_pct": round((hi - corner_bottom) / max(xhi - xlo, 1) * 100.0, 2),
        "open": (opening * w) > (w * 0.035),
    }


def kiosk_mouth_width(mod, mult=1.0):
    """The mouth width the real renderer uses, for the kiosk geometry."""
    s = min(KIOSK_W, KIOSK_H)
    R = s * 0.30
    return R * 0.72 * mult


def kiosk_parts(mod):
    """The R and base mouth width the real renderer uses, for the kiosk."""
    s = min(KIOSK_W, KIOSK_H)
    R = s * 0.30
    return R, R * 0.72


def preview_mouth(mod, curve, opening, w, path, scale=1.0):
    """Write a small PNG of the mouth ALONE, on its own transparent surface.

    Isolated so the only ink in the image is mouth - a full-face preview makes
    it hard to see the mouth shape, and stacking candidates in one PNG is the
    easiest way to compare them by eye.
    """
    import cairo
    W, H, _buf, _stride, _cx, _cy = render_mouth(mod, curve, opening, w, scale)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    mod._draw_mouth(cr, W / 2.0, H / 2.0, w, curve, opening, scale)
    surf.write_to_png(path)
    return path


def band_sheet(mod, rows, out):
    """Stack rows of mouth PNGs into one image, with a labelled blank row order.

    `rows` is a list of lists of (curve, opening) pairs; each row becomes one
    horizontal band. Rows are separated by a transparent gap so different
    candidates do not visually merge.
    """
    import cairo
    R0, W0 = kiosk_parts(mod)
    pad = 0.5
    cell_w = int(W0 * (1 + 2 * pad))
    cell_h = int(W0 * 0.9)
    gap = 24
    ncols = max(len(r) for r in rows)
    surf_w = ncols * cell_w
    surf_h = len(rows) * cell_h + (len(rows) - 1) * gap
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, surf_w, surf_h)
    cr = cairo.Context(surf)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0.05, 0.05, 0.08, 1.0)      # dark backdrop to judge ink
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    for r, row in enumerate(rows):
        for c, spec in enumerate(row):
            if spec is None:
                continue
            curve, opening, mult = spec
            w = R0 * 0.72 * mult
            cr.save()
            cr.translate(c * cell_w + cell_w / 2.0,
                         r * (cell_h + gap) + cell_h / 2.0)
            mod._draw_mouth(cr, 0.0, 0.0, w, curve, opening)
            cr.restore()
    surf.write_to_png(out)
    return out, surf_w, surf_h


def report(mod):
    print(f"{'expression':<11} {'curve':>6} {'open':>6} {'mult':>5} "
          f"{'w_px':>6} {'h_px':>6} {'smile%':>7} {'drop%':>6}  path")
    print("-" * 74)
    for name, p in mod.EXPRESSION_PARAMS.items():
        mult = p.get("mouth_w", 1.0)
        m = measure(mod, p["mouth_curve"], p["mouth_open"],
                    kiosk_mouth_width(mod, mult))
        if not m:
            print(f"{name:<11} {'-':>6} {'-':>6} {'-':>5}  (no ink)")
            continue
        print(f"{name:<11} {p['mouth_curve']:>6.2f} {p['mouth_open']:>6.2f} "
              f"{mult:>5.2f} {m['width_px']:>6} {m['height_px']:>6} "
              f"{m['smile_pct']:>7.2f} {m['drop_pct']:>6.2f}  "
              f"{'lens' if m['open'] else 'line'}")


def sweep(mod, specs, png_dir=None):
    print(f"{'candidate':<22} {'w_px':>6} {'h_px':>6} {'smile_px':>9} "
          f"{'smile%':>7}  path")
    print("-" * 70)
    rendered = []
    for spec in specs:
        parts = spec.split("/")
        if len(parts) != 3:
            print(f"  bad spec {spec!r} - want curve/opening/mult, e.g. 0.85/0.16/1.15")
            continue
        curve, opening, mult = (float(v) for v in parts)
        w = kiosk_mouth_width(mod, mult)
        m = measure(mod, curve, opening, w)
        if not m:
            print(f"  {spec:<20} (no ink)")
            continue
        print(f"  {spec:<20} {m['width_px']:>6} {m['height_px']:>6} "
              f"{m['smile_px']:>9} {m['smile_pct']:>7.2f}  "
              f"{'lens' if m['open'] else 'line'}")

        if png_dir:
            # Render a real face with these params so the result can be looked
            # at, not just tabulated. Patch the module dict so this exercises
            # exactly the code path the kiosk uses.
            orig = dict(mod.EXPRESSION_PARAMS["happy"])
            mod.EXPRESSION_PARAMS["happy"] = dict(
                brow_l=0.10, brow_r=0.10, eye_open=0.78,
                mouth_curve=curve, mouth_open=opening, mouth_w=mult)
            try:
                out = os.path.join(png_dir, f"happy_{curve}_{opening}_{mult}.png")
                mod.snapshot(out, "happy", 560, 315)
                rendered.append(out)
            finally:
                mod.EXPRESSION_PARAMS["happy"] = orig
    return rendered


def contact_sheet(mod, paths, out):
    """Stitch PNGs side by side with ffmpeg if available, else report the list."""
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0:
        args = ["ffmpeg", "-y"]
        for p in paths:
            args += ["-i", p]
        args += ["-filter_complex", f"hstack=inputs={len(paths)}", out]
        if subprocess.run(args, capture_output=True).returncode == 0:
            print(f"  contact sheet: {out}")
            return out
    print("  (no ffmpeg - individual PNGs listed below)")
    for p in paths:
        print(f"    {p}")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true",
                    help="measure every expression at the real kiosk geometry")
    ap.add_argument("--sweep", nargs="+", metavar="CURVE/OPEN/MULT",
                    help="measure candidate mouth settings")
    ap.add_argument("--png", metavar="DIR",
                    help="also render full-face PNGs for each candidate")
    ap.add_argument("--sheet", metavar="OUT", help="contact-sheet the PNGs (needs ffmpeg)")
    ap.add_argument("--band", metavar="OUT",
                    help="render a comparison sheet of mouth shapes to one PNG, "
                         "for eyeballing. Rows are separated by blank lines in "
                         "the --rows spec.")
    ap.add_argument("--rows", metavar="SPEC",
                    help="semicolon-separated rows of comma-separated "
                         "curve/open/mult; e.g. '0.55/0.10/1.0,0.85/0.16/1.1;"
                         "0.55/0.22/1.1'")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    mod = load_face()
    if not (args.report or args.sweep or args.band):
        ap.print_help()
        return 1

    if args.report:
        print("=== current expressions (kiosk geometry 1920x1080) ===\n")
        report(mod)

    if args.sweep:
        print(f"\n=== sweep ({len(args.sweep)} candidates) ===\n")
        if args.png:
            os.makedirs(args.png, exist_ok=True)
        paths = sweep(mod, args.sweep, args.png)
        if args.sheet and paths:
            print()
            contact_sheet(mod, paths, args.sheet)

    if args.band:
        if not args.rows:
            print("\n--band needs --rows, e.g. '0.55/0.10/1.0,0.85/0.16/1.1;0.55/0.22/1.1'")
            return 1
        rows = []
        for row_spec in args.rows.split(";"):
            row_spec = row_spec.strip()
            if not row_spec:
                continue
            row = []
            for cell in row_spec.split(","):
                cell = cell.strip()
                if not cell or cell == "-":
                    row.append(None)
                    continue
                curve, opening, mult = (float(v) for v in cell.split("/"))
                row.append((curve, opening, mult))
            if row:
                rows.append(row)
        out, w, h = band_sheet(mod, rows, args.band)
        print(f"\n=== mouth comparison sheet ===\n  {out}  ({w}x{h})")
        print(f"  rows: {len(rows)}")
        for i, row in enumerate(rows):
            desc = ", ".join(
                "blank" if c is None else f"curve={c[0]} open={c[1]} mult={c[2]}"
                for c in row)
            print(f"    row {i}: {desc}")
        print(f"\n  NOTE this is the mouth DRAWN AT KIOSK SCALE - each cell is")
        print(f"  {int(kiosk_parts(mod)[1] * 1.5)}px wide, matching the real 1920x1080 render.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
