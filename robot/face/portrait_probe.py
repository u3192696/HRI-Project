#!/usr/bin/env python3
"""Measure the rung-4 portrait's landmarks from its own pixels.

WHY A MEASUREMENT RATHER THAN NUMBERS TYPED BY HAND
---------------------------------------------------
Rung 4 cannot be redrawn from floats: it is an IMAGE PUPPET, so the mouth and the
eyes have to be found where they actually are in the still. Six numbers guessed by
eye put the warp in the wrong place, and the failure looks like bad drawing rather
than like a bad measurement - the same class of bug as the rung 2 nose that painted
itself in the frame's top-left corner because nobody measured where it landed.

The FIRST attempt at this file did it by global threshold ("the blue thing", "the
warm thing") and got the boxes wrong: the face box ran the height of the frame
because the hair highlights matched skin, and the mouth landed on the neck because
a shadow under the jaw is redder than the lips. A threshold that has to hold for the
whole frame is the wrong instrument. So each feature is measured inside a NARROW
WINDOW around a seeded position, which is the only thing the seeds are for - they
bound the search, they do not supply the answer. Every number written to the JSON is
measured inside its box.

    python3 portrait_probe.py                    # measure, write the JSON
    python3 portrait_probe.py --mark /tmp/lm.png # + a debug frame to eyeball
    python3 portrait_probe.py --show             # print, write nothing

A DIFFERENT PORTRAIT needs different seeds: pass --eye x,y --eye x,y --mouth x,y
(the seeds are the only prior knowledge in here). Everything else follows from the
pixels, including the aperture, which is DERIVED from the measured iris rather than
found separately - the iris radius and the lid opening are the same anatomy, and
deriving one from the other means a blink cannot drift out of step with the eye it
closes.
"""

import argparse
import json
import os
import sys

import cairo

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET = os.path.join(HERE, "assets", "portrait-r4.png")
LANDMARKS = os.path.join(HERE, "assets", "portrait-r4.json")

# The seeds, in ASSET pixel coordinates. These are the SHIPPED asset's, so running
# this script with no arguments reproduces the shipped landmarks file exactly; for a
# different still, pass --eye/--mouth read off a gridded crop.
EYE_SEEDS = ((597.0, 348.0), (748.0, 348.0))
MOUTH_SEED = (672.0, 505.0)


def is_warm_skin(c):
    """Skin: warm, and not in deep shadow.

    Stated as "what is NOT eye" rather than "what is eye", because the sclera loses
    its neutrality towards the corners - it is shadowed there, and a brightness gate
    rejected it, measuring each eye's opening about a quarter too narrow. That left
    the closed lid as a small ellipse with white sclera showing at both corners.
    """
    # NO BRIGHTNESS FLOOR, deliberately. Adding one (luma > 0.40) reads better as a
    # definition of skin and broke rung 4: on Elliot it let the walk run up through
    # the warm lash shadow into the brow, taking the measured opening from 13 px to
    # 19.8 px above the iris and re-breaking a blink that had just been fixed. The
    # threshold that ships is the one the committed landmarks were measured with; a
    # second face is a reason to check, not a reason to retune the first.
    r, g, b = c
    return (r - b) > 0.20 and r > 0.35


def is_eye_pixel(c):
    """Is this pixel part of the eye opening rather than the face around it?

    Everything inside an opening is unlike skin: the sclera is bright and neutral,
    the iris is blue, the pupil and lash line are dark. Skin is warm, so "not warm
    skin" is the test. Slightly generous on purpose - over-covering the opening with
    a lid is invisible, while under-covering leaves a white sliver poking out.
    """
    return not is_warm_skin(c)


def walk_to_skin(rows, w, h, cx, cy, dx, dy, limit, gap=3, also=None):
    """How far the eye opening extends from (cx, cy) along (dx, dy).

    Walks outward and returns the last distance at which an eye pixel was seen
    before a run of `gap` non-eye pixels. A RUN rather than a single pixel: the
    limbal edge between iris and sclera is itself a pixel or two of neither, and at
    gap=2 the walk stopped dead at the iris rim on one eye and measured its opening
    30% narrower than the other's. Three still leaves the brow safely out of reach -
    the skin between the lash line and the brow is a good eight pixels wide here.

    `also` is a set of coordinates already known to be eye - the grown iris blob.
    Without it a BROWN iris fails the "not warm skin" test (amber is warm and not
    dark enough to be excluded), so the walk stopped on the pupil it started in and
    reported an opening the size of a pinhead.
    """
    run, last = 0, 0
    for i in range(1, int(limit)):
        x, y = int(round(cx + dx * i)), int(round(cy + dy * i))
        if not (0 <= x < w and 0 <= y < h):
            break
        if (also is not None and (x, y) in also) or is_eye_pixel(rows[y][x]):
            run, last = 0, i
        else:
            run += 1
            if run >= gap:
                break
    return last


def load(path):
    """(width, height, rows of (r, g, b) floats)."""
    surf = cairo.ImageSurface.create_from_png(path)
    w, h = surf.get_width(), surf.get_height()
    buf = bytes(surf.get_data())
    stride = surf.get_stride()
    rows = []
    for y in range(h):
        base = y * stride
        rows.append([(buf[base + x * 4 + 2] / 255.0,
                      buf[base + x * 4 + 1] / 255.0,
                      buf[base + x * 4] / 255.0) for x in range(w)])
    return w, h, rows


def centroid(points):
    n = float(len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def is_iris(c):
    r, g, b = c
    return b - r > 0.15 and b > 0.35 and b > g + 0.10


def grow_iris(rows, w, h, cx, cy, r_max=42, tol=0.15):
    """Iris pixels grown from the seed by COLOUR, not by hue.

    `is_iris` above tests for blue and is exact for the rung-4 still. It is useless
    on a brown eye: it found zero pixels on Milo, whose iris is amber, so the probe
    refused the seed. An iris does not have to be blue - what it has to be is a
    fairly uniform disc that is nothing like the sclera around it - so this grows the
    region from the seed and accepts anything within `tol` of the seed colour. It
    works on blue, brown and green alike, and it stops at the limbus because the
    sclera is a different colour entirely.
    """
    seed = rows[int(cy)][int(cx)]
    seen = set()
    stack = [(int(cx), int(cy))]
    out = []
    while stack:
        x, y = stack.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        if abs(x - cx) > r_max or abs(y - cy) > r_max * 0.85:
            continue
        if not (0 <= x < w and 0 <= y < h):
            continue
        c = rows[y][x]
        if max(abs(c[0] - seed[0]), abs(c[1] - seed[1]),
               abs(c[2] - seed[2])) > tol:
            continue
        out.append((x, y))
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return out


def is_lip(c):
    """Redder than the skin around it, and not the shadow under the jaw.

    Skin here is about (0.98, 0.70, 0.57): r-g = 0.28, g-b = 0.13. Lips are
    (0.84, 0.42, 0.40): r-g = 0.42 and g-b near zero. The discriminator is redness
    PLUS the green-blue gap closing, not redness alone - which is what put the first
    version's mouth on the neck, where a jaw shadow reads r-g 0.35 with g-b 0.19.
    """
    r, g, b = c
    return r - g > 0.28 and (g - b) < 0.10 and r > 0.45


def measure_eye(rows, w, h, seed):
    sx, sy = seed
    win = [(x, y) for y in range(max(0, int(sy - 30)), min(h, int(sy + 31)))
           for x in range(max(0, int(sx - 45)), min(w, int(sx + 46)))
           if is_iris(rows[y][x])]
    if len(win) < 40:
        # Not a blue eye. Grow it from the seed by colour instead; a brown or green
        # iris is just as measurable, and refusing it made every non-blue face
        # unprobeable.
        win = grow_iris(rows, w, h, sx, sy)
    if len(win) < 40:
        raise SystemExit("portrait_probe: no iris near %r (%d px) - wrong seed?"
                         % (seed, len(win)))
    cx, cy = centroid(win)
    x0, y0, x1, y1 = bbox(win)
    r = (x1 - x0) / 2.0
    # The PUPIL is the darkest thing inside the iris; it is reported as a diagnostic
    # only. The CENTRE the renderer uses is the iris centroid, because that is the
    # disc whose movement IS the gaze - and a pupil search is easily captured by the
    # lash line, which measured 5 px above the true centre on this portrait.
    pr = r * 0.55
    pup = [(x, y) for y in range(int(cy - pr), int(cy + pr) + 1)
           for x in range(int(cx - pr), int(cx + pr) + 1)
           if max(rows[y][x]) < 0.28]
    px, py = centroid(pup) if len(pup) >= 6 else (cx, cy)

    # THE APERTURE IS MEASURED, NOT DERIVED. It used to be r * 2.0 wide and
    # r * 1.34 tall - an iris radius times a constant that happened to fit the
    # original tight crop. On a face further from the camera that under-measured the
    # opening by about 40%, so a blink covered only the middle of the eye and left
    # sclera showing round the lid. Walk the opening instead, so the number describes
    # THIS image.
    lim = max(12.0, r * 4.0)
    blob = set(win)
    left = walk_to_skin(rows, w, h, cx, cy, -1.0, 0.0, lim, also=blob)
    right = walk_to_skin(rows, w, h, cx, cy, 1.0, 0.0, lim, also=blob)
    up = walk_to_skin(rows, w, h, cx, cy, 0.0, -1.0, lim, also=blob)
    down = walk_to_skin(rows, w, h, cx, cy, 0.0, 1.0, lim, also=blob)
    # CLAMPED TO ANATOMY. The walk is the measurement; these are the bounds that
    # keep a bad walk from becoming a bad blink. A human eye opening is roughly 1.1
    # to 2.6 iris radii across and 0.7 to 1.8 above and below the centre, so a walk
    # that overruns into the brow or a shadow is pulled back to the outside of that
    # range rather than trusted.
    ap_rx = max(r * 1.15, min(r * 2.6, (left + right) / 2.0))
    ap_ry = max(r * 0.85, min(r * 1.8, (up + down) / 2.0))
    up = min(up, r * 1.8)
    down = min(down, r * 1.8)
    return dict(cx=round(cx, 1), cy=round(cy, 1), iris_r=round(r, 1),
                ap_rx=round(ap_rx, 1), ap_ry=round(ap_ry, 1),
                ap_up=float(up), ap_down=float(down),
                pupil_x=round(px, 1), pupil_y=round(py, 1), px=len(win))


def measure_mouth(rows, w, h, seed):
    sx, sy = seed
    x0, x1 = max(0, int(sx - 130)), min(w, int(sx + 130))
    # +/-22 ROWS, not +/-45. The seed is supplied ON the lips, so the probe's job is
    # to refine it, not to hunt for a mouth anywhere in the lower face. At +/-45 the
    # search reached the nose on Milo, whose shaded nose tip is redder than his lips,
    # and the puppet would have parted his nostrils.
    y0, y1 = max(0, int(sy - 22)), min(h, int(sy + 22))
    # The MOUTH LINE: the row carrying the most lip-coloured pixels. That is where
    # the two lips meet, and it is the seam the puppet parts on.
    counted = {}
    for y in range(y0, y1):
        n = sum(1 for x in range(x0, x1) if is_lip(rows[y][x]))
        if n:
            counted[y] = n
    if not counted:
        raise SystemExit("portrait_probe: no lips near %r - wrong seed?" % (seed,))
    line_y = max(counted, key=counted.get)

    def lip(x, y):
        if not (0 <= x < w and 0 <= y < h):
            return False
        return is_lip(rows[y][x])

    def run(step, limit):
        """How far a lip-coloured run continues from the seed, allowing 1-px gaps.

        Walking outward until the colour stops is what makes the corners land where
        they visually are; taking the min/max of every lip pixel in a band instead
        reported the nose shadow and the crease under the lower lip, because a
        SHADOW is desaturated and so passes a redness test that the skin beside it
        fails. A gap tolerance of one pixel stops a stray speck from extending the
        box without letting a real corner cut the mouth short.
        """
        d, gap = 0, 0
        for i in range(1, limit + 1):
            if lip(*(step(i))):
                d, gap = i, 0
            else:
                gap += 1
                if gap > 2:
                    break
        return d

    left = run(lambda i: (int(sx) - i, line_y), 120)
    right = run(lambda i: (int(sx) + i, line_y), 120)
    cx = (int(sx) - left + int(sx) + right) / 2.0
    half_w = (left + right) / 2.0
    up = run(lambda i: (int(cx), line_y - i), 40)
    down = run(lambda i: (int(cx), line_y + i), 40)
    return dict(cx=round(cx, 1), cy=round(float(line_y), 1),
                half_w=round(half_w, 1), up=round(max(2.0, up), 1),
                down=round(max(2.0, down), 1),
                px=left + right + up + down)


def measure_skin(rows, w, h, eyes, mouth):
    """The lid colour, sampled from the cheek between the eyes and the mouth.

    Not a palette lookup: the lid has to be the colour of the skin AROUND that eye,
    or closing it draws a patch of someone else's face.
    """
    ex = (eyes[0]["cx"] + eyes[1]["cx"]) / 2.0
    ey = (eyes[0]["cy"] + eyes[1]["cy"]) / 2.0
    cy = int(ey + (mouth["cy"] - ey) * 0.55)
    cx = int(ex)
    samples = [rows[y][x] for y in range(cy - 6, cy + 7)
               for x in range(cx - 6, cx + 7)]
    samples.sort(key=lambda c: sum(c))
    return [round(c, 3) for c in samples[len(samples) // 2]]


def background(rows, w, h):
    corners = []
    for cx, cy in ((0, 0), (w - 16, 0), (0, h - 16), (w - 16, h - 16)):
        for y in range(cy, cy + 16):
            for x in range(cx, cx + 16):
                corners.append(rows[y][x])
    corners.sort(key=lambda c: sum(c))
    return [round(c, 3) for c in corners[len(corners) // 2]]


def measure(path, eye_seeds, mouth_seed):
    w, h, rows = load(path)
    eyes = [measure_eye(rows, w, h, s) for s in eye_seeds]
    eyes.sort(key=lambda e: e["pupil_x"])
    mouth = measure_mouth(rows, w, h, mouth_seed)
    return dict(
        source=os.path.relpath(path, HERE), width=w, height=h,
        background=background(rows, w, h),
        skin=measure_skin(rows, w, h, eyes, mouth),
        aperture_measured=True,
        eyes=eyes, mouth=mouth,
    )


def _ellipse(cr, cx, cy, rx, ry):
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, ry)
    cr.new_sub_path()
    cr.arc(0, 0, 1, 0, 6.2832)
    cr.restore()


def mark(path, lm, out):
    """A debug frame: where the measurement SAYS the features are.

    A number table is not evidence that the puppet will land in the right place; this
    is the frame that shows it, and the same render-then-look loop the rest of the
    face is built with.
    """
    surf = cairo.ImageSurface.create_from_png(path)
    cr = cairo.Context(surf)
    w, h = lm["width"], lm["height"]
    cr.set_line_width(1.6)
    for e in lm["eyes"]:
        cr.set_source_rgb(1.0, 0.9, 0.2)
        cr.new_sub_path()
        cr.arc(e["cx"], e["cy"], 3, 0, 6.2832)
        cr.stroke()
        cr.new_sub_path()
        cr.arc(e["cx"], e["cy"], e["iris_r"], 0, 6.2832)
        cr.stroke()
        cr.set_source_rgb(0.2, 1.0, 0.5)
        _ellipse(cr, e["cx"], e["cy"], e["ap_rx"], e["ap_ry"])
        cr.stroke()
    m = lm["mouth"]
    cr.set_source_rgb(1.0, 0.35, 0.9)
    cr.rectangle(m["cx"] - m["half_w"], m["cy"] - m["up"],
                 m["half_w"] * 2.0, m["up"] + m["down"])
    cr.stroke()
    cr.move_to(m["cx"] - m["half_w"] - 10, m["cy"])
    cr.line_to(m["cx"] + m["half_w"] + 10, m["cy"])
    cr.stroke()
    cr.select_font_face("monospace")
    cr.set_font_size(max(12, h * 0.028))
    cr.set_source_rgb(1, 1, 1)
    cr.move_to(8, h - 10)
    cr.show_text("eyes %s  aperture %.0fx%.0f  mouth c %d,%d %.0fx%.0f+%.0f"
                 % ("/".join("%d,%d" % (e["pupil_x"], e["pupil_y"])
                             for e in lm["eyes"]),
                    lm["eyes"][0]["ap_rx"] * 2, lm["eyes"][0]["ap_ry"] * 2,
                    m["cx"], m["cy"], m["half_w"] * 2, m["up"], m["down"]))
    surf.write_to_png(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--asset", default=ASSET)
    ap.add_argument("--eye", action="append", metavar="X,Y",
                    help="seed for an iris, repeatable (default: the shipped asset's)")
    ap.add_argument("--mouth", metavar="X,Y", help="seed for the mouth")
    ap.add_argument("--mark", metavar="PNG", default=None,
                    help="write a debug frame marking what was measured")
    ap.add_argument("--show", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    eyes = (tuple(tuple(float(v) for v in s.split(",")) for s in args.eye)
            if args.eye else EYE_SEEDS)
    mouth = (tuple(float(v) for v in args.mouth.split(",")) if args.mouth
             else MOUTH_SEED)
    lm = measure(args.asset, eyes, mouth)
    if args.mark:
        mark(args.asset, lm, args.mark)
        print("marked %s" % args.mark)
    if args.show:
        print(json.dumps(lm, indent=2))
        return 0
    # THE LANDMARKS BELONG TO THE ASSET THEY WERE MEASURED FROM. This used to write
    # LANDMARKS - the shipped rung-4 file - whatever --asset said, so probing a
    # second face silently overwrote Elliot's landmarks with Milo's and the next
    # render blunk in the wrong place. Derive the path from the asset instead.
    out = os.path.splitext(args.asset)[0] + ".json"
    with open(out, "w") as fh:
        json.dump(lm, fh, indent=2)
        fh.write("\n")
    print("wrote %s" % out)
    print(json.dumps(lm, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
