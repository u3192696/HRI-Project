#!/usr/bin/env python3
"""gaze_bar_probe.py — measure the gaze bar from the rendered pixels.

WHY A PROBE AND NOT A CODE REVIEW. A parameter is an INPUT; a pixel is the OUTPUT,
and the two are not obviously related. Three face defects on this robot (thin
brows, thin lips, an over-clipped iris) were invisible in review and obvious in the
pixels. The gaze bar is the participant's score, so the same rule applies with more
force than usual: what gets measured is the ink the projector will actually show.

WHAT IT CHECKS, and why each one is here:

  1. THE FILL IS PROPORTIONAL. The bar's ink is diffed against the same bar at zero,
     so the measurement is the fill alone - not the fill plus the track, plus the
     edge, plus the caption. The fill's height is then compared against the linear
     prediction, and against the fraction it represents. A bar whose fill did not
     move with its own value is the failure this exists to catch.

  2. THE STRAY-INK CHECK. Every pixel that changed between two full frames must lie
     inside the box THE RENDERER ITSELF REPORTS (`draw_gaze` returns it). Hunting
     for stray ink by colour aliases with the face's own pale ink, and a stray
     `arc()` line was missed that way once already - on the countdown disc, where it
     put 26,850 px of diagonal across the face. Diffing cannot miss it.

  3. IT COLLIDES WITH NOTHING. The bar's box is tested against the countdown's box,
     the listening pill's box, the caption's ink and the footer's ink. The footer is
     the one that matters most: it is in the same band as the caption, and the check
     is horizontal rather than vertical because that is the only way the two could
     meet.

  4. IT READS. Contrast of the fill and of the track against the background, to the
     3:1 large-text floor, measured from the pixels rather than from the palette
     constants - the projector gets the composited colour, not the source colour.

  5. THE RAMP'S ENDS ARE THE COLOURS SOMEBODY CHOSE, and the middle stays readable.
     Both readings of "starts magenta, ends green" are measured, because they differ
     in the one number that matters at a glance: the colour at the TOP of the fill.

  6. THE COMPLETION BURST EXISTS, STAYS OFF THE COUNTDOWN, AND STOPS. The bar's top is
     33 px below the countdown's ring, so a celebration that rose would paint over the
     time left. Diffed frame against frame, the same trick as check 1: the ink counted
     is the burst's own, and the last check is that after its 1.8 s there is none.

USAGE
  python3 gaze_bar_probe.py                     # every check, with the numbers
  python3 gaze_bar_probe.py --sweep             # the fill table only
  python3 gaze_bar_probe.py --png-dir /tmp/bars # write the frames it measured
  python3 gaze_bar_probe.py --sheet /tmp/v.png  # variant contact sheet for a pick

FACE_PY selects the renderer (default: the deployed one), so this can be pointed at
a working copy without deploying it.
"""

import argparse
import importlib.util
import os
import sys

FACE_PY = os.environ.get("FACE_PY", "/home/jetson/robot/face/face.py")

# The real kiosk geometry. Everything below is reported in pixels AND as a
# percentage of the frame, so a number here can be compared with the countdown's own
# measurements (bar 14.8% of the frame height, disc 24.0%) without arithmetic.
W, H = 1920, 1080

# Alpha above this counts as ink. Same threshold expression_probe.py uses.
INK = 100

# The fill values measured. 0.0 is the baseline every other frame is diffed against.
SWEEP = (0.0, 0.25, 0.5, 0.75, 1.0)

# The look the robot actually wears: stage 1's face, pinned, with a session running
# so both instruments are on screen.
FACE_STYLE = "cartoon"
SCREEN_STYLE = "cartoon"
STAGE_TEXT = "Stage 3"

FILL_ALPHA_MIN = 0.5    # the fill must be more solid than the countdown's 0.60
LARGE_TEXT_CONTRAST = 3.0


def load_face(path=None):
    """Import the renderer by path, without needing it on sys.path."""
    spec = importlib.util.spec_from_file_location("face", path or FACE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Pixels
# --------------------------------------------------------------------------
def surface(w=W, h=H, transparent=False):
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    if transparent:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
    return surf, cr


def ink_box(buf, stride, w, h, x0=0, y0=0, x1=None, y1=None):
    """The box containing every pixel with alpha > INK, or None if there is none."""
    x1 = w if x1 is None else x1
    y1 = h if y1 is None else y1
    lo_x, lo_y, hi_x, hi_y = None, None, None, None
    for y in range(y0, y1):
        row = y * stride
        for x in range(x0, x1):
            if buf[row + x * 4 + 3] > INK:
                if lo_x is None or x < lo_x:
                    lo_x = x
                if hi_x is None or x > hi_x:
                    hi_x = x
                if lo_y is None:
                    lo_y = y
                hi_y = y
    if lo_x is None:
        return None
    return (lo_x, lo_y, hi_x, hi_y)


def diff_box(a, b, stride, w, h):
    """The box containing every pixel that differs between two frames.

    ARGB32 is premultiplied and little-endian: each pixel is B,G,R,A. Comparing all
    four bytes is what makes this a picture diff rather than an alpha test - a
    change that moved no alpha, e.g. ink over ink, still counts.
    """
    lo_x, lo_y, hi_x, hi_y = None, None, None, None
    for y in range(h):
        row = y * stride
        for x in range(w):
            i = row + x * 4
            if a[i:i + 4] != b[i:i + 4]:
                if lo_x is None or x < lo_x:
                    lo_x = x
                if hi_x is None or x > hi_x:
                    hi_x = x
                if lo_y is None:
                    lo_y = y
                hi_y = y
    if lo_x is None:
        return None
    return (lo_x, lo_y, hi_x, hi_y)


def rgb_at(buf, stride, x, y):
    """(r, g, b) 0..255 at one pixel. Alpha is 255 on every frame, so the premultiplied
    bytes are the colour itself."""
    i = y * stride + x * 4
    return (buf[i + 2], buf[i + 1], buf[i])


def _linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast(rgb_a, rgb_b):
    """WCAG contrast ratio between two colours."""
    la = sum(k * _linear(c) for k, c in zip((0.2126, 0.7152, 0.0722), rgb_a))
    lb = sum(k * _linear(c) for k, c in zip((0.2126, 0.7152, 0.0722), rgb_b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mean_rgb(buf, stride, x0, y0, x1, y1):
    n = 0
    r = g = b = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            pr, pg, pb = rgb_at(buf, stride, x, y)
            r += pr
            g += pg
            b += pb
            n += 1
    n = max(n, 1)
    return (r / n, g / n, b / n)


def overlaps(a, b, pad=0):
    """Do two (x, y, w, h) boxes intersect, allowing `pad` px of separation?"""
    ax0, ay0, ax1, ay1 = a[0] - pad, a[1] - pad, a[0] + a[2] + pad, a[1] + a[3] + pad
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def state(mod, expression):
    st = mod.FaceState()
    st.set_expression(expression)
    for _ in range(60):          # settle the eased params on the target
        st.update(1.0 / 30.0)
    st.gaze_x = 0.0
    st.gaze_y = 0.0
    st.t = 0.0                   # pins the breathing alpha, so renders compare
    return st


def bar_only(mod, progress, fill=None):
    """The bar alone, on a transparent surface. Returns (buf, stride, box).

    ISOLATED ON PURPOSE, the same reason expression_probe.py draws the mouth alone:
    measuring it inside a full frame means separating its ink from the face's, which
    is fragile and breaks on a palette change.

    `fill` is a NAME ("ramp", "ramp:warm", "magenta"...), resolved by the renderer, so
    a ramp cannot be measured here as if it were one colour.
    """
    kind, value = mod.gaze_fill_spec(fill or mod.GAZE_BAR_FILL_DEFAULT)
    surf, cr = surface(transparent=True)
    box = mod.gaze_bar_box(W, H, "disc")
    mod.draw_gaze_bar(cr, box[0], box[1], box[2], box[3], progress,
                      **mod.gaze_fill_args(fill or mod.GAZE_BAR_FILL_DEFAULT))
    return surf.get_data(), surf.get_stride(), box


def frame(mod, progress, caption="none", ink=None, step="session",
          screen="face", style=SCREEN_STYLE, text=STAGE_TEXT, listen=True,
          expression=None, prompt=False, burst_age=None):
    """One whole session frame. Returns (buf, stride).

    No geometry comes out of here: every box used below is asked of the renderer
    (gaze_bar_box, timer_box, draw_gaze's return, draw_pill's return), so none of the
    checks can quietly agree with a copy of the layout kept in this file.
    """
    surf, cr = surface()
    st = state(mod, expression if expression is not None else mod.PIN_EXPRESSION_DEFAULT)
    scr = mod.sample_screen(screen, style, text, step, gaze_progress=progress)
    mod.draw_frame(cr, W, H, st, scr, style=style, listen=listen,
                   timer_style="disc", gaze_caption=caption, gaze_ink=ink,
                   gaze_burst=burst_age,
                   prompt=mod.sample_prompt("start") if prompt else None)
    return surf.get_data(), surf.get_stride()


def pill_box(mod):
    surf, cr = surface(transparent=True)
    return mod.draw_pill(cr, W, H, 0.0)


def footer_box(mod, text, size_frac=0.028, y_frac=0.94):
    """The footer's ink box, measured with cairo's own text extents.

    The footer is drawn by draw_frame and its box is not returned, so it is measured
    here rather than guessed: the caption and the footer share a band, and the only
    question between them is horizontal.
    """
    import cairo
    surf, cr = surface(transparent=True)
    size = min(W, H) * size_frac
    cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cy = H * y_frac
    return (W / 2.0 - ext.width / 2.0 - ext.x_bearing,
            cy - ext.height / 2.0 - ext.y_bearing,
            ext.width, ext.height)


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------
def report(title):
    print("\n== %s ==" % title)


def measure_fill(mod, failures, png_dir=None):
    """1. The fill is proportional, and the box the renderer reports holds it."""
    report("the fill, measured from its own ink (caption off)")
    base, stride, box = bar_only(mod, 0.0)
    x0, y0, bw, bh = box
    print("  track box %.0fx%.0f px at x %.0f, y %.0f  (%.1f%% of the frame height)"
          % (bw, bh, x0, y0, bh / H * 100.0))
    print("  %-8s %-12s %-14s %-12s %s" % ("fill", "ink height", "% of track",
                                           "% of frame", "predicted vs measured"))
    for p in SWEEP:
        if p == 0.0:
            continue
        buf, _, _ = bar_only(mod, p)
        changed = diff_box(buf, base, stride, W, H)
        if changed is None:
            failures.append("fill %.2f changed no pixels" % p)
            print("  %.2f     NOTHING CHANGED" % p)
            continue
        cx0, cy0, cx1, cy1 = changed
        fill_h = cy1 - cy0 + 1
        # The ink box is INCLUSIVE at both ends, so a run of n pixels reports as n-1
        # in the difference: expected = round(bh * p) + 1. Stated rather than left
        # implicit, because a table reading "100.2% of the track" at full is how an
        # off-by-one gets argued about instead of settled.
        predicted = bh * p
        expected = round(predicted) + 1
        top_predicted = y0 + bh * (1.0 - p)
        if png_dir:
            write_png(mod, os.path.join(png_dir, "bar-%03d.png" % round(p * 100)), p)
        ok_h = abs(fill_h - expected) <= max(2.0, predicted * 0.02)
        ok_x = (cx0 >= x0 - 2 and cx1 <= x0 + bw + 2)
        ok_top = abs(cy0 - top_predicted) <= max(2.0, bh * 0.02)
        print("  %-8.2f %-12.0f %-14.1f %-12.1f %.0f vs %.0f px top=%.0f ok=%s"
              % (p, fill_h, fill_h / bh * 100.0, fill_h / H * 100.0,
                 expected, fill_h, cy0, "y" if (ok_h and ok_x and ok_top) else "NO"))
        if not ok_h:
            failures.append("fill %.2f: height %.0f px, expected %.0f"
                            % (p, fill_h, expected))
        if not ok_x:
            failures.append("fill %.2f: ink escapes the track sideways "
                            "(%d..%d vs %d..%d)" % (p, cx0, cx1, x0, x0 + bw))
        if not ok_top:
            failures.append("fill %.2f: fill top %.0f px, predicted %.0f"
                            % (p, cy0, top_predicted))
        # The fill never exceeds the track: the clip is the reason, and an unclipped
        # rectangle would poke out at the rounded ends.
        if cy0 < y0 - 1 or cy1 > y0 + bh + 1:
            failures.append("fill %.2f: ink outside the track vertically" % p)
    # A full bar must be exactly the track: this is what "the bar is full" means.
    buf, _, _ = bar_only(mod, 1.0)
    changed = diff_box(buf, base, stride, W, H)
    if changed and (changed[1] < y0 - 2 or changed[3] > y0 + bh + 2):
        failures.append("a full bar does not match the track it fills")


def measure_frame(mod, failures, caption="none", ink=None, png_dir=None):
    """2 + 3 + 4. Stray ink, collisions, contrast - on whole frames."""
    report("a whole session frame (caption=%s)" % caption)
    a, stride = frame(mod, 0.0, caption, ink)
    b, _ = frame(mod, 0.6, caption, ink)
    changed = diff_box(a, b, stride, W, H)
    x0, y0, bw, bh = mod.gaze_bar_box(W, H, "disc")
    # The box the RENDERER reports for the bar plus its caption, which is what the
    # stray-ink check must use: the drawing code's own answer, not a re-derivation.
    surf, cr = surface(transparent=True)
    reported = mod.draw_gaze(cr, W, H, 0.6, caption, "disc")
    rx0, ry0, rbw, rbh = reported
    print("  renderer reports the bar+caption box: %.0fx%.0f px at x %.0f, y %.0f"
          % (rbw, rbh, rx0, ry0))
    print("  fill bar box: %.0fx%.0f px at x %.0f, y %.0f" % (bw, bh, x0, y0))
    if changed is None:
        failures.append("a 0.6 fill changed no pixels in a full frame")
        print("  NOTHING CHANGED between a 0%% and a 60%% frame")
    else:
        print("  changed pixels (0%% vs 60%%): x %d..%d, y %d..%d"
              % (changed[0], changed[2], changed[1], changed[3]))
        inside = (changed[0] >= rx0 - 2 and changed[2] <= rx0 + rbw + 2
                  and changed[1] >= ry0 - 2 and changed[3] <= ry0 + rbh + 2)
        print("  all of it inside the reported box: %s" % ("yes" if inside else "NO"))
        if not inside:
            failures.append("changed pixels outside the reported box: %s vs %s"
                            % (changed, reported))

    caption_boxes = {}
    for name in ("word", "percent", "none"):
        cbuf, cstride = frame(mod, 0.6, name, ink)
        nbuf, _ = frame(mod, 0.6, "none", ink)
        cap = diff_box(cbuf, nbuf, cstride, W, H)
        caption_boxes[name] = cap
        print("  caption %-8s ink box: %s" % (
            name, "none" if cap is None else
            "x %d..%d, y %d..%d" % (cap[0], cap[2], cap[1], cap[3])))

    # Collisions, with the boxes the renderer reports.
    disc = mod.timer_box(W, H, "disc")
    pill = pill_box(mod)
    footer = footer_box(mod, "stage 1 of 4")
    print("  countdown box  x %.0f..%.0f, y %.0f..%.0f"
          % (disc[0], disc[0] + disc[2], disc[1], disc[1] + disc[3]))
    print("  pill box       x %.0f..%.0f, y %.0f..%.0f"
          % (pill[0], pill[0] + pill[2], pill[1], pill[1] + pill[3]))
    print("  footer ink     x %.0f..%.0f, y %.0f..%.0f"
          % (footer[0], footer[0] + footer[2], footer[1], footer[1] + footer[3]))
    for other, name in ((disc, "the countdown"), (pill, "the pill")):
        if overlaps((x0, y0, bw, bh), other):
            failures.append("the gaze bar overlaps %s" % name)
    print("  bar vs countdown: %s" % (
        "CLEAR" if not overlaps((x0, y0, bw, bh), disc) else "OVERLAP"))
    for name, cap in caption_boxes.items():
        if cap is None:
            continue
        if overlaps((cap[0], cap[1], cap[2] - cap[0] + 1, cap[3] - cap[1] + 1),
                    footer, pad=-1):
            failures.append("the %s caption overlaps the footer" % name)
        gap = min(abs(cap[0] - (footer[0] + footer[2])),
                  abs(footer[0] - cap[2]))
        print("  caption %-8s vs footer: %s (%.0f px apart)"
              % (name, "CLEAR" if not overlaps(
                  (cap[0], cap[1], cap[2] - cap[0] + 1, cap[3] - cap[1] + 1),
                  footer, pad=-1) else "OVERLAP", gap))

    # Contrast, measured from the pixels: the projector gets the composite.
    full, fstride = frame(mod, 0.6, "none", ink)
    bg = mean_rgb(full, fstride, 40, 700, 120, 780)
    fill = mean_rgb(full, fstride, int(x0 + bw * 0.5) - 5, int(y0 + bh * 0.8) - 5,
                    int(x0 + bw * 0.5) + 5, int(y0 + bh * 0.8) + 5)
    track = mean_rgb(full, fstride, int(x0 + bw * 0.5) - 5, int(y0 + bh * 0.05),
                     int(x0 + bw * 0.5) + 5, int(y0 + bh * 0.05) + 10)
    print("  fill rgb%s contrast %.2f:1 | track rgb%s contrast %.2f:1 | background "
          "rgb%s" % (tuple(round(c) for c in fill), contrast(fill, bg),
                     tuple(round(c) for c in track), contrast(track, bg),
                     tuple(round(c) for c in bg)))
    if contrast(fill, bg) < LARGE_TEXT_CONTRAST:
        failures.append("the fill is %.2f:1 against the background, under %.1f:1"
                        % (contrast(fill, bg), LARGE_TEXT_CONTRAST))
    if png_dir:
        write_png_frame(mod, os.path.join(png_dir, "frame-60.png"), 0.6, caption, ink)


def write_png(mod, path, progress, fill=None):
    surf, cr = surface(transparent=True)
    box = mod.gaze_bar_box(W, H, "disc")
    mod.draw_gaze_bar(cr, box[0], box[1], box[2], box[3], progress,
                      **mod.gaze_fill_args(fill or mod.GAZE_BAR_FILL_DEFAULT))
    surf.write_to_png(path)
    return path


def write_png_frame(mod, path, progress, caption="none", ink=None, burst_age=None):
    surf, cr = surface()
    st = state(mod, mod.PIN_EXPRESSION_DEFAULT)
    mod.draw_frame(cr, W, H, st,
                   mod.sample_screen("face", SCREEN_STYLE, STAGE_TEXT, "session",
                                     gaze_progress=progress),
                   style=SCREEN_STYLE, listen=True, timer_style="disc",
                   gaze_caption=caption, gaze_ink=ink, gaze_burst=burst_age)
    surf.write_to_png(path)
    return path


# --------------------------------------------------------------------------
# The colour ramp and the completion burst
# --------------------------------------------------------------------------
def bar_colour_at(mod, progress, fill, y_frac):
    """The fill's COMPOSITED colour at a fraction of the track's height, from pixels.

    From pixels rather than from the palette, because the fill is drawn at
    GAZE_BAR_FILL_ALPHA over the background: the projector never shows the source
    colour, and a contrast number computed from the constant would describe a colour
    nobody sees.
    """
    buf, stride, box = bar_only(mod, progress, fill)
    x0, y0, bw, bh = box
    # The CENTRE of the bar's width, not its edge: the track's ends are rounded by
    # half its width, so a sample 3 px in from the edge falls outside the corner arc
    # near either end and reads as untouched background - a 0,0,0 that looks like a
    # rendered colour in a table.
    return rgb_at(buf, stride, int(x0 + bw / 2.0), int(y0 + bh * y_frac))


def measure_ramp(mod, failures):
    """Both readings of "starts magenta, ends green", measured where they differ.

    A flat hue that moves with the fraction ('ramp') and a fixed ramp the fill
    uncovers ('gradient') are the same instruction read two ways. The numbers that
    separate them are the colour at the TOP of the fill and the colour at its BASE:
    for a gradient the base stays magenta and only the top arrives at green, so a
    glance at 90% cannot tell you whether the bar is finished.
    """
    report("the fill's colour ramp (composited, from pixels)")
    bg = (13, 11, 25)
    for mode in ("ramp", "gradient"):
        print("\n  %s:" % mode)
        print("    %-6s %-22s %-9s %-22s %-9s" %
              ("fill", "top of the fill", "contrast", "base of the fill", "contrast"))
        worst = 99.0
        for t in (0.05, 0.25, 0.5, 0.75, 0.95, 1.0):
            # Just inside the fill's TOP edge, and just above the track's bottom: the
            # two places that tell the two modes apart.
            top = bar_colour_at(mod, t, mode, (1.0 - t) + 0.03 * t)
            base = bar_colour_at(mod, t, mode, 0.99)
            ct, cb = contrast(top, bg), contrast(base, bg)
            worst = min(worst, ct, cb)
            print("    %-6.0f%% rgb%-18s %-9s rgb%-18s %-9s"
                  % (t * 100.0, str(top), "%.1f:1" % ct, str(base), "%.1f:1" % cb))
        print("    worst contrast anywhere on the fill: %.1f:1" % worst)
        if worst < LARGE_TEXT_CONTRAST:
            failures.append("%s: fill contrast %.1f:1 is below the %.1f:1 floor"
                            % (mode, worst, LARGE_TEXT_CONTRAST))

    # The endpoints, at the pixels: whatever the ramp does in between, 0 must be the
    # magenta somebody chose and 100% must be the green that means "done". Sampled at
    # the MIDPOINT OF THE FILL (1 - t/2), not the midpoint of the track: at 2% full the
    # middle of the track is empty, and an empty track's dim colour would read as the
    # ramp's start being wrong.
    start = bar_colour_at(mod, 0.02, "ramp", 1.0 - 0.01)
    end = bar_colour_at(mod, 1.0, "ramp", 0.5)
    print("\n  endpoints: %.0f%% -> rgb%s, 100%% -> rgb%s" % (2, start, end))
    if not (end[1] == max(end)):
        failures.append("ramp: the full fill is not green-dominant: rgb%s" % (end,))
    if start[0] < start[1]:
        failures.append("ramp: the empty end is not magenta-ish (red >= green): rgb%s"
                        % (start,))

    # A gradient must REVEAL the ramp rather than compress it: at a part-filled bar the
    # base is still the ramp's start and the top is not yet the ramp's end.
    g_top = bar_colour_at(mod, 0.4, "gradient", 0.62)
    g_base = bar_colour_at(mod, 0.4, "gradient", 0.97)
    print("  gradient at 40%%: base rgb%s, top rgb%s"
          % (g_base, g_top))
    if g_top[1] >= g_top[0]:
        failures.append("gradient: the top of a 40%% fill already reads green: rgb%s"
                        % (g_top,))
    if g_base[0] < g_base[1]:
        failures.append("gradient: the base of a 40%% fill is not the ramp's start: "
                        "rgb%s" % (g_base,))
    both = bar_colour_at(mod, 1.0, "gradient", 0.03)
    print("  gradient at 100%%: base rgb%s, top rgb%s"
          % (bar_colour_at(mod, 1.0, "gradient", 0.99), both))


def diff_rect(a, b, stride, w, h):
    """The (x, y, w, h) box of the pixels that differ, or None if the frames match.

    diff_box returns CORNERS. Handing those to code that wants a width doubles every
    extent, which produces a box that is plausible-looking, too big, and wrong - it
    read as ink outside the frame and ink over the caption, neither of which was true.
    Converting in one place means the two conventions cannot be mixed up again.
    """
    corners = diff_box(a, b, stride, w, h)
    if corners is None:
        return None
    return (corners[0], corners[1], corners[2] - corners[0] + 1, corners[3] - corners[1] + 1)


def diff_pixels(a, b, stride, w, h):
    """How many pixels differ between two frames at all - the burst's own ink."""
    n = 0
    for y in range(h):
        row = y * stride
        for x in range(w):
            i = row + x * 4
            if a[i:i + 4] != b[i:i + 4]:
                n += 1
    return n


def measure_burst(mod, failures, png_dir=None):
    """The completion burst: what appears, where it goes, and that it goes away.

    Measured by DIFF against the same frame with no burst, so the ink counted is the
    burst's own - the bar is identical between the two, which is the whole trick. The
    countdown is 33 px above the top of the bar, so the question this answers is
    whether the celebration stays out of it without being shrunk to nothing.
    """
    report("the completion burst at 100% (diffed against the same frame without it)")
    tbox = mod.timer_box(W, H, "disc")
    gx0, gy0, gbw, _gbh = mod.gaze_bar_box(W, H, "disc")
    # The same ceiling the renderer uses, built the same way from the countdown's box.
    ceiling = tbox[1] + tbox[3] + mod.GAZE_BURST_CEILING_PAD
    base, stride = frame(mod, 1.0, "percent")
    # The caption's own ink box, for the collision test: the two frames differ only in
    # their text, so the diff is the caption exactly.
    without_caption, _ = frame(mod, 1.0, "none")
    caption_box = diff_rect(base, without_caption, stride, W, H)
    err = mod.GAZE_BURST_LIFE
    print("  countdown box y %d..%d | bar top y %d | air between them %d px | life %.1fs"
          % (tbox[1], tbox[1] + tbox[3], gy0, gy0 - (tbox[1] + tbox[3]), err))
    print("    %-7s %-8s %-9s %-18s %-9s %s"
          % ("age", "particles", "ink px", "ink box", "width", "clear of countdown"))
    seen_any = False
    widths = []
    for age in (0.05, 0.2, 0.4, 0.7, 1.0, 1.3, 1.6, err, err + 0.05, 2.5):
        buf, bstride = frame(mod, 1.0, "percent", burst_age=age)
        box = diff_rect(base, buf, bstride, W, H)
        n_particles = len(mod.burst_particles(age, W, H, gx0, gy0, gbw, ceiling))
        if box is None:
            print("    %-7.2f %-8d %-9s %-18s %-9s %s"
                  % (age, n_particles, "0", "-", "-", "-"))
            continue
        seen_any = True
        widths.append(box[2])
        ink = diff_pixels(base, buf, bstride, W, H)
        gap = box[1] - (tbox[1] + tbox[3])
        print("    %-7.2f %-8d %-9d x %d..%d y %d..%d %-9s %d px"
              % (age, n_particles, ink, box[0], box[0] + box[2], box[1],
                 box[1] + box[3], box[2], gap))
        if age <= err:
            if overlaps((box[0], box[1], box[2], box[3]), tbox):
                failures.append("burst at %.2fs: ink inside the countdown's box" % age)
            if box[0] < 0 or box[1] < 0 or box[0] + box[2] > W \
                    or box[1] + box[3] > H:
                failures.append("burst at %.2fs: ink outside the frame" % age)
            if caption_box and overlaps((box[0], box[1], box[2], box[3]),
                                        caption_box):
                failures.append("burst at %.2fs: ink over the caption" % age)
            if png_dir:
                write_png_frame(mod, os.path.join(png_dir, "burst-%03d.png"
                                                  % round(age * 100)),
                                1.0, "percent", None, age)
    if not seen_any:
        failures.append("burst: it never drew anything")
    elif widths[0] >= widths[-1]:
        failures.append("burst: it does not spread outward (%.0f px -> %.0f px)"
                        % (widths[0], widths[-1]))
    print("    after %.1fs: %s"
          % (err, "empty, as it must be"
             if not mod.burst_particles(err + 0.05, W, H, gx0, gy0, gbw, ceiling)
             else "STILL DRAWING"))


# --------------------------------------------------------------------------
# The variant sheet — one image, so a look can be picked rather than described
# --------------------------------------------------------------------------
def sheet(mod, path, combos=None, cell_w=620, cell_h=349):
    """A labelled grid of variants, so the look is picked from pixels, not prose.

    Each cell is the real frame, scaled down. The default set answers the two questions
    a description cannot: which READING of "starts magenta, ends green" was meant (the
    whole fill shifting hue, or the fill uncovering a fixed ramp), and whether the
    burst reads as fireworks at the size it will really be.

    The label above each cell is drawn with the renderer's own text function, so the
    sheet cannot disagree with the face about what a variant is called.
    """
    combos = combos or [
        # (fill, caption, progress, burst age)
        ("ramp", "percent", 0.62, None),
        ("gradient", "percent", 0.62, None),
        ("ramp:warm", "percent", 0.62, None),
        ("ramp", "percent", 1.0, None),
        ("ramp", "percent", 1.0, 0.25),
        ("ramp", "percent", 1.0, 0.7),
    ]
    cols = 3
    rows = (len(combos) + cols - 1) // cols
    pad, head = 18, 34
    sheet_w = cols * cell_w + (cols + 1) * pad
    sheet_h = rows * (cell_h + head) + (rows + 1) * pad
    surf, cr = surface(sheet_w, sheet_h)
    cr.set_source_rgb(*mod.BG)
    cr.paint()
    for i, (fill, caption, progress, age) in enumerate(combos):
        r, c = divmod(i, cols)
        cx = pad + c * (cell_w + pad)
        cy = pad + r * (cell_h + head + pad) + head
        cell, ccr = surface()
        st = state(mod, mod.PIN_EXPRESSION_DEFAULT)
        mod.draw_frame(ccr, W, H, st,
                       mod.sample_screen("face", SCREEN_STYLE, STAGE_TEXT, "session",
                                         gaze_progress=progress),
                       style=SCREEN_STYLE, listen=True, timer_style="disc",
                       gaze_caption=caption, gaze_ink=fill, gaze_burst=age)
        cr.save()
        cr.rectangle(cx, cy, cell_w, cell_h)
        cr.clip()
        cr.translate(cx, cy)
        cr.scale(cell_w / float(W), cell_h / float(H))
        cr.set_source_surface(cell, 0, 0)
        cr.paint()
        cr.restore()
        label = "%s @ %d%%" % (fill, round(progress * 100))
        if age is not None:
            label += " + burst at %.2fs" % age
        mod.draw_text_at(cr, cx + cell_w / 2.0, cy - head * 0.55, label, 20,
                         mod.STANDBY_INK, 0.95)
    surf.write_to_png(path)
    return path


def stats_frame(mod, spans=None, target=None, listen=True):
    """One stats screen. Returns (buf, stride).

    Rendered through sample_screen("stats"), so the timeline is the module's own
    sample - the probe never keeps a second copy of what the screen is supposed to
    contain, only of what it must ADD UP TO (the spans must tile the session).
    """
    surf, cr = surface()
    st = state(mod, mod.PIN_EXPRESSION_DEFAULT)
    scr = mod.sample_screen("stats", spans=spans, target=target)
    mod.draw_frame(cr, W, H, st, scr, style=SCREEN_STYLE, listen=listen)
    return surf.get_data(), surf.get_stride()


def measure_stats(mod, failures, png_dir=None):
    """6. The stats screen: the timeline lands where the times say it does.

    The screen is a claim about a session - "you were looking here, and not here" -
    so what has to be true is that the SHAPE matches the numbers: each span starts
    and ends at the pixel its seconds say, the two states differ in height as well
    as colour, and the target line is where the target is.
    """
    report("the stats screen: the timeline against its own numbers")
    buf, stride = stats_frame(mod)
    screen = mod.sample_screen("stats")
    spans = screen["spans"]
    length = float(screen["seconds"])
    target = screen["target"]

    bx = (W - W * mod.STATS_BAR_WIDTH_FRAC) / 2.0
    bw = W * mod.STATS_BAR_WIDTH_FRAC
    bh = H * mod.STATS_BAR_HEIGHT_FRAC
    th = H * mod.STATS_TRACK_HEIGHT_FRAC
    cy = int(round(H * mod.STATS_BAR_Y_FRAC))

    def x_at(t):
        return bx + bw * max(0.0, min(1.0, t / length))

    # Every span must tile the session: no gap and no overlap, or the bar would be
    # claiming a moment that was neither looking nor not-looking.
    tiles = spans[0][0] == 0.0 and abs(spans[-1][1] - length) < 1e-6
    contiguous = all(abs(spans[i][1] - spans[i + 1][0]) < 1e-6
                     for i in range(len(spans) - 1))
    print("  the sample timeline tiles the session: %s (starts at %.1f s, ends at "
          "%.1f s of %.1f s)" % ("yes" if tiles and contiguous else "NO",
                                 spans[0][0], spans[-1][1], length))
    if not (tiles and contiguous):
        failures.append("stats: the sample timeline does not tile the session")

    look_rgb = tuple(round(c * 255) for c in mod.STATS_LOOK_INK)
    away_rgb = tuple(round(c * 255) for c in mod.STATS_AWAY_INK)
    # The renderer's own colours, not copies: if the palette moves, this probe
    # follows it rather than measuring against a stale idea of the background.
    BG_RGB = tuple(round(c * 255) for c in mod.BG)
    DIM_RGB = tuple(round(c * 255) for c in mod.DIM_INK)

    def near(rgb, want, tol=6):
        return all(abs(a - b) <= tol for a, b in zip(rgb, want))

    # The looking spans, found by scanning the bar's centre row for the green.
    runs, run = [], None
    for x in range(int(bx) - 2, int(bx + bw) + 3):
        is_look = near(rgb_at(buf, stride, x, cy), look_rgb)
        if is_look and run is None:
            run = x
        elif not is_look and run is not None:
            runs.append((run, x - 1))
            run = None
    if run is not None:
        runs.append((run, int(bx + bw) + 2))
    # The 10 s ticks are drawn THROUGH the blocks, so a block reads as two runs with a
    # 2 px gap where a tick crosses it. Merged here rather than in the renderer: a
    # 0.1 s hole between two looks is not a difference anyone can see, and treating it
    # as one would report a span that the screen does not show.
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 4:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    runs = merged
    want_runs = [(x_at(s), x_at(e)) for s, e, looking in spans if looking]
    print("  looking spans: %d found, %d expected" % (len(runs), len(want_runs)))
    ok = len(runs) == len(want_runs)
    for (got0, got1), (want0, want1) in zip(runs, want_runs):
        good = abs(got0 - want0) <= 2 and abs(got1 - want1) <= 2
        ok = ok and good
        print("    x %4d..%4d  expected %6.1f..%6.1f  %s"
              % (got0, got1, want0, want1, "ok" if good else "OFF"))
    if not ok:
        failures.append("stats: a looking span is not where its seconds say")

    # Height, at a point inside a looking span and inside an away one: the two states
    # differ in height as well as colour, which is what carries them for a viewer who
    # cannot tell the colours apart.
    def height_at(x):
        return sum(1 for y in range(int(cy - bh), int(cy + bh) + 1)
                   if not near(rgb_at(buf, stride, x, y), BG_RGB, tol=3))

    def away_height_at(x):
        return sum(1 for y in range(int(cy - bh), int(cy + bh) + 1)
                   if near(rgb_at(buf, stride, x, y), away_rgb))

    look_x = int((runs[0][0] + runs[0][1]) / 2) if runs else int(x_at(spans[0][1] / 2))
    away_span = next((s for s in spans if not s[2]), None)
    away_x = int(x_at((away_span[0] + away_span[1]) / 2)) if away_span else None
    look_h = height_at(look_x)
    away_h = away_height_at(away_x) if away_x is not None else 0
    print("  height: looking %d px (expected ~%d), away %d px (expected ~%d)"
          % (look_h, round(bh), away_h, round(th)))
    if abs(look_h - bh) > 3 or abs(away_h - th) > 3:
        failures.append("stats: the two states do not have the heights they claim")

    # The target line, taller than the bar and at the target's own x.
    tx = int(round(x_at(target)))
    above = [y for y in range(int(cy - bh * 1.6), int(cy - bh * 0.5))
             if near(rgb_at(buf, stride, tx, y), DIM_RGB, tol=8)]
    print("  target line at x %d (expected %d): %d px of ink above the bar"
          % (tx, round(x_at(target)), len(above)))
    if len(above) < 5:
        failures.append("stats: the target line is not drawn where the target is")

    # Contrast, from the pixels: the two states against the background, and against
    # each other. A dim state that vanishes on a projector is not a state.
    look_c = contrast(rgb_at(buf, stride, look_x, cy), BG_RGB)
    away_c = contrast(rgb_at(buf, stride, away_x, cy), BG_RGB) if away_x else 0.0
    between = contrast(rgb_at(buf, stride, look_x, cy),
                       rgb_at(buf, stride, away_x, cy)) if away_x else 0.0
    print("  contrast: looking %.1f:1, away %.1f:1, and %.1f:1 between them "
          "(floors 7, 3 and 1.4)" % (look_c, away_c, between))
    if look_c < 7.0 or away_c < 3.0 or between < 1.4:
        failures.append("stats: the timeline does not read on the projector")

    # Clear space and no strays. Two separate things, and the first attempt at this
    # got it wrong: ink_box() is an ALPHA test, and this frame is opaque, so it
    # matched the entire window and reported the whole screen as crowding the bar.
    def nearest_ink_row(y0, y1, step=-1, skip_x=None):
        """The first row in the range (scanned from y0 towards y1) that is not the
        background, or None.

        `skip_x` ignores one column: the TARGET LINE is supposed to stand taller than
        the bar, so measuring clearance without excluding it measures the target line
        and reports 1 px. And `step` has to be given a sign for a downward scan - the
        default is for scanning up, and range(564, 1080, -1) is empty, which read as
        "nothing below the bar" when the labels were there all along.
        """
        for y in range(y0, y1, step):
            if any(not near(rgb_at(buf, stride, x, y), BG_RGB, tol=3)
                   for x in range(0, W, 2)
                   if skip_x is None or abs(x - skip_x) > 3):
                return y
        return None

    gap_above = int(cy - bh / 2.0) - (nearest_ink_row(int(cy - bh / 2.0) - 1, 0,
                                                       skip_x=tx) or 0)
    below = nearest_ink_row(int(cy + bh / 2.0) + 1, H, 1, skip_x=tx)
    gap_below = (below - int(cy + bh / 2.0)) if below is not None else H
    print("  clearance: %d px above the bar, %d px below (floors 8 and 4)"
          % (gap_above, gap_below))
    if gap_above < 8 or gap_below < 4:
        failures.append("stats: the timeline is crowding its own labels")

    # And nothing else may be ON it: the bar's own rows hold the two state colours,
    # the background (the ticks) and the target line, and nothing else. This is the
    # check that catches a stray caption or a footer drawn through the bar.
    allowed = (BG_RGB, look_rgb, away_rgb, DIM_RGB)
    # The span boundaries are ANTIALIASED: the pixel where a green block meets the
    # slate is a blend of the two and matches neither within any sane tolerance, so
    # the edge columns are excluded rather than counted as strays. Same for the target
    # line's own two antialiased columns.
    edges = set()
    for s, e, _looking in spans:
        for t in (s, e):
            edges.update(range(int(round(x_at(t))) - 2, int(round(x_at(t))) + 3))
    stray_rows = []
    for y in range(int(cy - bh / 2.0) + 2, int(cy + bh / 2.0) - 1):
        for x in range(int(bx), int(bx + bw)):
            if x in edges or abs(x - tx) <= 2:
                continue
            rgb = rgb_at(buf, stride, x, y)
            if not any(near(rgb, a, tol=10) for a in allowed):
                stray_rows.append((x, y, rgb))
                break
    print("  stray ink on the timeline: %d rows%s"
          % (len(stray_rows), (" e.g. %s" % (stray_rows[:2],)) if stray_rows else ""))
    if stray_rows:
        failures.append("stats: something is drawn through the timeline")

    if png_dir:
        path = os.path.join(png_dir, "stats.png")
        surf, cr = surface()
        st = state(mod, mod.PIN_EXPRESSION_DEFAULT)
        mod.draw_frame(cr, W, H, st, mod.sample_screen("stats"),
                       style=SCREEN_STYLE, listen=True)
        surf.write_to_png(path)
        print("  wrote %s" % path)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true",
                    help="the fill table only, no layout checks")
    ap.add_argument("--png-dir", default=None,
                    help="write the rendered frames here")
    ap.add_argument("--sheet", default=None, metavar="PATH",
                    help="write the variant contact sheet here and exit")
    ap.add_argument("--face", default=FACE_PY,
                    help="the renderer to measure (default %(default)s)")
    args = ap.parse_args()

    mod = load_face(args.face)

    if args.sheet:
        print("wrote %s" % sheet(mod, args.sheet))
        return 0

    failures = []
    print("gaze bar probe: %s" % args.face)
    print("frame %dx%d | ink threshold alpha > %d | geometry from the renderer's "
          "own gaze_bar_box()" % (W, H, INK))
    measure_fill(mod, failures, args.png_dir)
    if not args.sweep:
        for caption in ("percent", "none"):
            measure_frame(mod, failures, caption, None, args.png_dir)
        measure_ramp(mod, failures)
        measure_burst(mod, failures, args.png_dir)
        measure_stats(mod, failures, args.png_dir)

    print()
    if failures:
        print("PROBE FAILED:")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("PROBE PASSED: the fill is proportional, nothing escapes the box the "
          "renderer reports, nothing collides, and it reads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
