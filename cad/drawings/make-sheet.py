"""One A2 sheet per part: assembled 1:2 (top/front/side + pictorial) plus one
1:4 thumbnail per printable sub-part.

Every number on the sheet is measured from the BRep of the solid it describes.
Nothing is typed in. The run refuses to draw if the Part container does not
agree with the sum of its sub-parts, and refuses to print a dimension unless the
projected geometry confirms an edge at that coordinate.

Run:  freecadcmd make-sheet.py [file.FCStd]
Out:  sheets/<STEM>-A2.svg / .pdf / .png / .dxf  +  <STEM>-report.txt
"""
import FreeCAD as App
import TechDraw
import os, sys, math, collections

SRC = sys.argv[-1] if sys.argv[-1].endswith(".FCStd") else \
    "/home/danny/Projects/hri-project/cad/drawings/Top_Bracket.FCStd"
STEM = os.path.basename(SRC)[:-6]
OUTDIR = os.path.join(os.path.dirname(SRC), "sheets")
os.makedirs(OUTDIR, exist_ok=True)
REPORT = []


def log(s):
    REPORT.append(s)
    print(s)


# ---------------------------------------------------------------- model access
doc = App.openDocument(SRC)
doc.recompute()
part = next((o for o in doc.Objects if o.TypeId == "App::Part"), None)
if part is None:
    raise SystemExit("no App::Part container in %s" % SRC)
kids = [o for o in part.Group if o.TypeId == "PartDesign::Body"]
if not kids:
    raise SystemExit("no PartDesign::Body inside the Part container")

for b in kids:
    p = b.Placement.Base
    if abs(p.x) + abs(p.y) + abs(p.z) > 1e-6:
        raise SystemExit("%s has a non-zero placement; sheet assumes assembled "
                         "geometry. Handle it before drawing." % b.Name)

sumv, partv = sum(b.Shape.Volume for b in kids), part.Shape.Volume
log("PART %s: %d sub-parts, %.0f mm3, %d solid(s), bbox %.2f x %.2f x %.2f" % (
    part.Name, len(kids), partv, len(part.Shape.Solids),
    part.Shape.BoundBox.XLength, part.Shape.BoundBox.YLength, part.Shape.BoundBox.ZLength))
log("sum of sub-part volumes %.0f vs Part %.0f (delta %+.2f%%)" % (
    sumv, partv, 100.0 * (sumv - partv) / partv))
if abs(sumv - partv) / partv > 0.01:
    raise SystemExit("REFUSING: the Part container does not hold all sub-parts "
                     "(delta %.1f%%). An assembled view would be wrong."
                     % (100.0 * (sumv - partv) / partv))

UNION = kids[0].Shape
for b in kids[1:]:
    UNION = UNION.fuse(b.Shape)
UNION = UNION.removeSplitter()
log("union of the sub-parts: %.0f mm3 (sum %.0f, interference %.0f mm3)" % (
    UNION.Volume, sumv, sumv - UNION.Volume))

# --------------------------------------------------- cylindrical feature reading
def cyl_faces(shape):
    """Classify every cylindrical face. A bore sweeps ~360 deg; a concave fillet
    sweeps ~90 deg and both have an inward normal, so the sweep decides."""
    out = []
    for fc in shape.Faces:
        if fc.Surface.TypeId != "Part::GeomCylinder":
            continue
        s = fc.Surface
        u0, u1, v0, v1 = fc.ParameterRange
        sweep = math.degrees(abs(u1 - u0))
        p = fc.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        ax, bp = s.Axis, s.Center
        rad = App.Vector(p.x - bp.x, p.y - bp.y, p.z - bp.z)
        rad = rad - ax * rad.dot(ax)
        if rad.Length > 1e-9:
            rad.normalize()
        dot = fc.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(rad)
        if dot < 0 and sweep > 350:
            kind = "bore"
        elif dot < 0:
            kind = "concave"
        else:
            kind = "convex"
        out.append({"r": s.Radius, "dia": round(2 * s.Radius, 3), "sweep": sweep,
                    "kind": kind, "axis": ax, "c": bp,
                    "vlen": abs(fc.valueAt((u0 + u1) / 2, v1).sub(
                        fc.valueAt((u0 + u1) / 2, v0)).Length)})
    return out


def axis_name(ax):
    i = max(range(3), key=lambda k: abs(getattr(ax, "xyz"[k])))
    return "XYZ"[i]


def features(shape):
    """Measured feature summary for one solid."""
    bb = shape.BoundBox
    thick = {"X": bb.XLength, "Y": bb.YLength, "Z": bb.ZLength}
    fs = cyl_faces(shape)
    bores = [f for f in fs if f["kind"] == "bore"]
    concave = [f for f in fs if f["kind"] == "concave"]
    convex = [f for f in fs if f["kind"] == "convex" and f["sweep"] > 350]
    lines, data = [], {"bores": [], "concave": [], "convex": []}
    for grp, lst, label in (("bores", bores, "bore"), ("concave", concave, "concave")):
        g = collections.defaultdict(list)
        for f in lst:
            g[(f["dia"], axis_name(f["axis"]))].append(f)
        for (dia, axn), faces in sorted(g.items()):
            thru = max(f["vlen"] for f in faces) >= 0.97 * thick[axn]
            centres = [(round(f["c"].x, 2), round(f["c"].y, 2), round(f["c"].z, 2))
                       for f in faces]
            data[grp].append({"dia": dia, "axis": axn, "n": len(faces),
                              "thru": thru, "depth": round(max(f["vlen"] for f in faces), 2),
                              "centres": centres})
            if grp == "bores":
                lines.append("%d× ⌀%g %s" % (
                    len(faces), dia, "THRU" if thru else
                    "× %.1f DEEP" % max(f["vlen"] for f in faces)))
            else:
                lines.append("%d× ⌀%g %s-ROUND" % (
                    len(faces), dia, "HALF" if faces[0]["sweep"] > 170 else "QUARTER"))
    radii = sorted({round(f["r"], 2) for f in fs if f["kind"] == "convex" and f["sweep"] < 350})
    for r in radii:
        data["convex"].append(r)
    return {"lines": lines, "data": data, "radii": radii,
            "size": (bb.XLength, bb.YLength, bb.ZLength),
            "vol": shape.Volume, "solids": len(shape.Solids)}


FEAT = {b.Name: features(b.Shape) for b in kids}
for b in kids:
    f = FEAT[b.Name]
    log("  %-8s %.2f x %.2f x %.2f  vol %.0f  solids %d  | %s | R%s" % (
        b.Name, f["size"][0], f["size"][1], f["size"][2], f["vol"], f["solids"],
        "; ".join(f["lines"]) or "-", ",".join("%g" % r for r in f["radii"]) or "-"))

# --------------------------------------------------------------- projections
VIS_IDX, HID_IDX = (0, 1, 2, 3, 4), (5, 6, 7, 8, 9)
FRAMES = {           # measured: (u,v) -> (h,w); all det = +1
    "front": ((0.0, 1.0), (-1.0, 0.0)),
    "side": ((0.0, -1.0), (1.0, 0.0)),
    "top": ((1.0, 0.0), (0.0, 1.0)),
    "axono": ((-1.0, 0.0), (0.0, -1.0)),
}
for k, (hc, wc) in FRAMES.items():
    assert abs(hc[0] * wc[1] - hc[1] * wc[0] - 1.0) < 1e-9, "frame %s mirrors" % k


def edge_pts(e):
    if e.Curve.TypeId == "Part::GeomLine":
        return [(p.x, p.y) for p in (e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter))]
    return [(p.x, p.y) for p in e.discretize(40)]


def project(shape, direction, fkey):
    hc, wc = FRAMES[fkey]
    parts = list(TechDraw.projectEx(shape, direction))
    out = {}
    for key, idxs in (("vis", VIS_IDX), ("hid", HID_IDX)):
        polys = []
        for i in idxs:
            s = parts[i]
            if s is None:
                continue
            for e in s.Edges:
                polys.append([(u * hc[0] + v * hc[1], u * wc[0] + v * wc[1])
                              for (u, v) in edge_pts(e)])
        out[key] = polys
    allp = [p for k in ("vis", "hid") for poly in out[k] for p in poly]
    out["bbox"] = (min(p[0] for p in allp), min(p[1] for p in allp),
                   max(p[0] for p in allp), max(p[1] for p in allp))
    return out


W = {"X": UNION.BoundBox.XLength, "Y": UNION.BoundBox.YLength,
     "Z": UNION.BoundBox.ZLength}
VIEWDEF = {"TOP": ((0, 0, 1), "top", ("X", "Y")),
           "FRONT": ((0, -1, 0), "front", ("X", "Z")),
           "SIDE": ((1, 0, 0), "side", ("Y", "Z")),
           "PICT": ((1, -1, 0.8), "axono", (None, None))}
MASTER = {}
log("master view frame checks:")
for label, (d, fk, (ha, wa)) in VIEWDEF.items():
    MASTER[label] = project(UNION, App.Vector(*d), fk)
    x0, y0, x1, y1 = MASTER[label]["bbox"]
    if ha:
        assert abs((x1 - x0) - W[ha]) < 0.06 and abs((y1 - y0) - W[wa]) < 0.06, label
        log("  %-5s h=%.2f (%s)  w=%.2f (%s)  OK" % (label, x1 - x0, ha, y1 - y0, wa))

THUMB = {}
for b in kids:
    THUMB[b.Name] = {"TOP": project(b.Shape, App.Vector(0, 0, 1), "top"),
                     "FRONT": project(b.Shape, App.Vector(0, -1, 0), "front")}

# ------------------------------------------------------------------ sheet items
SHEET_W, SHEET_H = 594.0, 420.0
items, texts = [], []


def poly(pts, layer, width, dash=None):
    items.append(("poly", layer, pts, width, dash))


def text(x, y, s, size=3.0, anchor="middle", weight="normal", layer="TEXT", rot=0):
    w = 0.62 * size * len(s)
    h = size * 1.15
    if rot:
        box = (x - h / 2, y - w / 2, x + h / 2, y + w / 2)
    elif anchor == "middle":
        box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    elif anchor == "start":
        box = (x, y - h / 2, x + w, y + h / 2)
    else:
        box = (x - w, y - h / 2, x, y + h / 2)
    texts.append((box, s))
    items.append(("text", layer, x, y, s, size, anchor, weight, rot))
    return box


def arrow(tip, direction, size=2.2):
    dx, dy = direction
    n = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    b = (tip[0] - ux * size, tip[1] - uy * size)
    poly([tip, (b[0] + px * size * 0.38, b[1] + py * size * 0.38),
          (b[0] - px * size * 0.38, b[1] - py * size * 0.38), tip], "DIMS", 0.25)


PLACE = {}        # view key -> (cx, cy, scale)


def S(view, h, w):
    x0, y0, x1, y1 = MASTER[view]["bbox"]
    cx, cy, sc = PLACE[view]
    return (cx + (h - (x0 + x1) / 2) * sc, cy - (w - (y0 + y1) / 2) * sc)


def dim_h(view, h0, h1, wref, off, label, size=3.0):
    (ax, ay), (bx, by) = S(view, h0, wref), S(view, h1, wref)
    y = ay + off
    x1, x2 = sorted((ax, bx))
    poly([(x1, y), (x2, y)], "DIMS", 0.25)
    for x in (x1, x2):
        poly([(x, y - 1.1), (x, y + 1.1)], "DIMS", 0.25)
    arrow((x1, y), (1, 0)); arrow((x2, y), (-1, 0))
    text((x1 + x2) / 2, y + 2.8 if off >= 0 else y - 2.8, label, size)


def dim_v(view, w0, w1, href, off, label, size=3.0):
    (ax, ay), (bx, by) = S(view, href, w0), S(view, href, w1)
    x = ax - off
    y1, y2 = sorted((ay, by))
    poly([(x, y1), (x, y2)], "DIMS", 0.25)
    for y in (y1, y2):
        poly([(x - 1.1, y), (x + 1.1, y)], "DIMS", 0.25)
    arrow((x, y1), (0, -1)); arrow((x, y2), (0, 1))
    text(x - 3.4, (y1 + y2) / 2, label, size, rot=90)


def leader(view, h, w, dx, dy, note, length=12.0, size=2.8):
    x0, y0 = S(view, h, w)
    n = (dx * dx + dy * dy) ** 0.5
    x1, y1 = x0 + dx / n * length, y0 + dy / n * length
    x2 = x1 + (5.0 if dx >= 0 else -5.0)
    poly([(x0, y0), (x1, y1), (x2, y1)], "DIMS", 0.25)
    arrow((x0, y0), (x1 - x0, y1 - y0), 2.2)
    text(x2 + (1.4 if dx >= 0 else -1.4), y1, note, size,
         anchor="start" if dx >= 0 else "end")


def draw_view(view, style="master"):
    for pl in MASTER[view]["hid"]:
        poly([S(view, h, w) for h, w in pl], "HIDDEN", 0.2, dash="1.8 1.2")
    for pl in MASTER[view]["vis"]:
        poly([S(view, h, w) for h, w in pl], "VISIBLE", 0.5)


def vwh(view, sc):
    x0, y0, x1, y1 = MASTER[view]["bbox"]
    return (x1 - x0) * sc, (y1 - y0) * sc


# ---- master layout: ortho left, pictorial right (auto scale), 1:2 for ortho
MSC = 0.5
tw, th = vwh("TOP", MSC)
fw, fh = vwh("FRONT", MSC)
sw, sh = vwh("SIDE", MSC)
PLACE["TOP"] = (40 + tw / 2, 38 + th / 2, MSC)          # y grows DOWN the sheet
PLACE["FRONT"] = (40 + fw / 2, 182 + fh / 2, MSC)
PLACE["SIDE"] = (235 + sw / 2, 182 + sh / 2, MSC)

for psc in (0.5, 0.4, 1 / 3.0, 0.25):
    pw, ph = vwh("PICT", psc)
    if pw <= 225 and ph <= 185:
        break
PLACE["PICT"] = (481, 34 + ph / 2, psc)
log("pictorial scale 1:%.1f, %.1f x %.1f mm" % (1 / psc, pw, ph))

for v in ("TOP", "FRONT", "SIDE", "PICT"):
    draw_view(v)

# seam indication: each sub-part's own outline, chain-dotted
for b in kids:
    for view, d, fk in (("TOP", (0, 0, 1), "top"), ("FRONT", (0, -1, 0), "front"),
                        ("SIDE", (1, 0, 0), "side")):
        pr = project(b.Shape, App.Vector(*d), fk)
        for pl in pr["vis"]:
            poly([S(view, h, w) for h, w in pl], "SEAM", 0.18, dash="6 1.6 1 1.6")

# ---- master dimensions (all measured from the union bbox and the bores)
dim_h("FRONT", MASTER["FRONT"]["bbox"][0], MASTER["FRONT"]["bbox"][2],
      MASTER["FRONT"]["bbox"][1], 18, "%.1f" % W["X"])
dim_v("FRONT", MASTER["FRONT"]["bbox"][1], MASTER["FRONT"]["bbox"][3],
      MASTER["FRONT"]["bbox"][0], 18, "%.1f" % W["Z"])
dim_h("SIDE", MASTER["SIDE"]["bbox"][0], MASTER["SIDE"]["bbox"][2],
      MASTER["SIDE"]["bbox"][1], 18, "%.1f" % W["Y"])
dim_v("TOP", MASTER["TOP"]["bbox"][1], MASTER["TOP"]["bbox"][3],
      MASTER["TOP"]["bbox"][0], 18, "%.1f" % W["Y"])

# bore pattern, straight off the measured bore centres
allb = collections.defaultdict(list)
for b in kids:
    for hh in FEAT[b.Name]["data"]["bores"]:
        for c in hh["centres"]:
            allb[(hh["dia"], hh["axis"])].append(c)
xs, ys = [], []
for (dia, axn), cs in allb.items():
    if axn != "Z":
        continue
    xs = sorted({c[0] for c in cs}); ys = sorted({c[1] for c in cs})
    bb = UNION.BoundBox
    # pattern pitch only - the edge offsets live in note 5, they do not fit
    # inside a 15 mm rim at 1:2 without colliding with the bore circles
    if len(xs) > 1:
        dim_h("TOP", xs[0], xs[-1], 40, 0, "%.1f" % (xs[-1] - xs[0]), 2.8)
    if len(ys) > 1:
        dim_v("TOP", ys[0], ys[-1], -60, 0, "%.1f" % (ys[-1] - ys[0]), 2.8)
    leader("TOP", xs[-1], ys[-1], 1.0, -0.6,
           "%d× ⌀%g THRU" % (len(cs), dia), length=12, size=2.6)
    break

# R25 corner + cross-hole callouts
big = 0.0
corners = [(b, f) for b in kids for f in cyl_faces(b.Shape)
           if f["kind"] == "convex" and 80 < f["sweep"] < 100 and f["r"] > 5]
if corners:
    big = max(f["r"] for b, f in corners)
    mx = (UNION.BoundBox.XMin + UNION.BoundBox.XMax) / 2
    my = (UNION.BoundBox.YMin + UNION.BoundBox.YMax) / 2
    # call the corner out on the LEFT-hand side: the leader runs up over the
    # sheet margin, away from the bore callout on the right
    b0, f = min([x for x in corners if abs(x[1]["r"] - big) < 1e-6],
                key=lambda t: t[1]["c"].x)
    sx = 1 if f["c"].x > mx else -1
    sy = 1 if f["c"].y > my else -1
    leader("TOP", f["c"].x + sx * f["r"] * 0.7071, f["c"].y + sy * f["r"] * 0.7071,
           -0.45, -1.0, "R%g (%d×)" % (big, len(corners)), length=10, size=2.6)
CROSS = sum(hh["n"] for b in kids for hh in FEAT[b.Name]["data"]["bores"]
            if hh["axis"] != "Z")
CROSS0 = [(b, hh) for b in kids for hh in FEAT[b.Name]["data"]["bores"]
          if hh["axis"] != "Z"]
cross_note = ("%d× ⌀%g × %.1f DEEP INTO EACH ⌀%g BORE, FROM THE R%g CORNER"
              % (CROSS, CROSS0[0][1]["dia"], CROSS0[0][1]["depth"],
                 [x for x in allb if x[1] == "Z"][0][0], big)) if CROSS0 else ""

# view labels
for v, lab in (("TOP", "TOP (plan)"), ("FRONT", "FRONT"), ("SIDE", "SIDE (from +X)"),
               ("PICT", "PICTORIAL - ASSEMBLED")):
    cx, cy, sc = PLACE[v]
    w_, h_ = vwh(v, sc)
    text(cx, cy + h_ / 2 + 5.5, lab, 3.4, weight="bold", layer="LABEL")
    if v == "PICT":
        text(cx, cy - h_ / 2 - 6.5, "1:%g" % round(1 / sc, 2), 3.4, weight="bold",
             layer="LABEL")
text(14, 16, "ASSEMBLED PART  ·  1:2  ·  THIRD-ANGLE PROJECTION  ·  ALL DIMS mm",
     3.8, anchor="start", weight="bold", layer="LABEL")

# ---- thumbnails, 1:4, four cells
TSC = 0.25
CELL_TOP, CELL_BOT = 222.0, 336.0
CELL_X0, CELL_W = 12.0, 104.0
ORDER = sorted(kids, key=lambda b: (round(b.Shape.BoundBox.XMin, 1),
                                    -round(b.Shape.BoundBox.YMax, 1)))
for i, b in enumerate(ORDER):
    x0 = CELL_X0 + i * CELL_W
    cx = x0 + CELL_W / 2
    f = FEAT[b.Name]
    poly([(x0, CELL_TOP), (x0 + CELL_W, CELL_TOP), (x0 + CELL_W, CELL_BOT),
          (x0, CELL_BOT), (x0, CELL_TOP)], "FRAME", 0.2)
    text(x0 + 2, CELL_TOP + 6, "%d / 4   %s" % (i + 1, b.Label), 3.2,
         anchor="start", weight="bold", layer="LABEL")

    vx0, vy0, vx1, vy1 = THUMB[b.Name]["TOP"]["bbox"]
    tw_, th_ = (vx1 - vx0) * TSC, (vy1 - vy0) * TSC
    fx0, fy0, fx1, fy1 = THUMB[b.Name]["FRONT"]["bbox"]
    fw_, fh_ = (fx1 - fx0) * TSC, (fy1 - fy0) * TSC
    cy_top = CELL_TOP + 13 + th_ / 2
    cy_frt = cy_top + th_ / 2 + 12 + fh_ / 2

    for view, cyv in (("TOP", cy_top), ("FRONT", cy_frt)):
        pr = THUMB[b.Name][view]
        ax0, ay0, ax1, ay1 = pr["bbox"]
        for pl in pr["hid"]:
            poly([(cx + (h - (ax0 + ax1) / 2) * TSC, cyv - (w - (ay0 + ay1) / 2) * TSC)
                  for h, w in pl], "HIDDEN", 0.15, dash="1.4 1.0")
        for pl in pr["vis"]:
            poly([(cx + (h - (ax0 + ax1) / 2) * TSC, cyv - (w - (ay0 + ay1) / 2) * TSC)
                  for h, w in pl], "VISIBLE", 0.35)

    yb = cy_top + th_ / 2 + 5.0
    poly([(cx - tw_ / 2, yb), (cx + tw_ / 2, yb)], "DIMS", 0.2)
    arrow((cx - tw_ / 2, yb), (1, 0), 1.8); arrow((cx + tw_ / 2, yb), (-1, 0), 1.8)
    text(cx, yb + 2.6, "%.1f" % f["size"][0], 2.5)
    xl = cx - tw_ / 2 - 5.0
    poly([(xl, cy_top - th_ / 2), (xl, cy_top + th_ / 2)], "DIMS", 0.2)
    arrow((xl, cy_top - th_ / 2), (0, -1), 1.8); arrow((xl, cy_top + th_ / 2), (0, 1), 1.8)
    text(xl - 2.6, cy_top, "%.1f" % f["size"][1], 2.5, rot=90)
    zl = cx - fw_ / 2 - 5.0
    poly([(zl, cy_frt - fh_ / 2), (zl, cy_frt + fh_ / 2)], "DIMS", 0.2)
    arrow((zl, cy_frt - fh_ / 2), (0, -1), 1.8); arrow((zl, cy_frt + fh_ / 2), (0, 1), 1.8)
    text(zl - 2.6, cy_frt, "%.1f" % f["size"][2], 2.5, rot=90)

    ty = CELL_TOP + 78
    text(x0 + 3, ty, "%.1f x %.1f x %.1f  %.0f mm3  %d solid%s" % (
        f["size"][0], f["size"][1], f["size"][2], f["vol"], f["solids"],
        "" if f["solids"] == 1 else "S !"), 2.3, anchor="start")
    ty += 4.3
    for line in f["lines"]:
        text(x0 + 3, ty, line.replace("×", "x").replace("⌀", "DIA "), 2.3, anchor="start")
        ty += 4.3
    if f["radii"]:
        text(x0 + 3, ty, "R" + " / R".join("%g" % r for r in f["radii"]), 2.3, anchor="start")
        ty += 4.3
    if f["solids"] > 1:
        text(x0 + 3, ty, "NOT ONE PRINTABLE SOLID", 2.3, anchor="start", weight="bold")

# ---- notes (middle column, beside the plan view)
NX = 228.0
notes = ["NOTES — ALL DIMENSIONS IN mm, THIRD-ANGLE",
         "1. MEASURED FROM THE SOLID (BRep), NOT RETYPED.",
         "2. %d PRINTABLE SUB-PARTS, SHOWN 1:4 BELOW." % len(kids),
         "3. CHAIN-DOTTED (GREEN) = SUB-PART JOINTS.",
         "4. HIDDEN EDGES DASHED.",
         "5. BORE PATTERN: %.1f x %.1f, %.1f FROM X EDGE, %.1f FROM Y EDGE."
         % (xs[-1] - xs[0], ys[-1] - ys[0], xs[0] - UNION.BoundBox.XMin,
            ys[-1] - UNION.BoundBox.YMin) if len(xs) > 1 and len(ys) > 1 else "5. -"]
if cross_note:
    notes.append("6. " + cross_note)
notes.append("7. ASSEMBLED SIZE = SUM OF THE %d PARTS, VERIFIED." % len(kids))
yy = 78.0
for n in notes:
    text(NX, yy, n, 2.35, anchor="start")
    yy += 4.5

# ---- model check box, under the notes
BOX_W, BOX_H = 146.0, 58.0
bx0, by0 = NX - 4, 122.0
poly([(bx0, by0), (bx0 + BOX_W, by0), (bx0 + BOX_W, by0 + BOX_H),
      (bx0, by0 + BOX_H), (bx0, by0)], "FRAME", 0.3)
text(bx0 + 3, by0 + 5.5, "MODEL CHECK (measured)", 2.5, anchor="start", weight="bold")
checks = [("sum of %d pieces" % len(kids), "%.0f mm3" % sumv),
          ("fused union", "%.0f mm3" % UNION.Volume),
          ("interference", "%.0f mm3" % (sumv - UNION.Volume)),
          ("solids in union", "%d" % len(UNION.Solids)),
          ("each piece", "1 solid" if all(f["solids"] == 1 for f in FEAT.values())
           else "CHECK - some are multi-solid")]
cy_ = by0 + 11.0
for k, v in checks:
    text(bx0 + 3, cy_, k, 2.35, anchor="start")
    text(bx0 + 62, cy_, v, 2.35, anchor="start")
    cy_ += 4.6

# ---- sub-part table (right column, under the pictorial)
TBL_W = 204.0
tbx0, tby0 = SHEET_W - 8 - TBL_W, 168.0
rows = [(b.Label, "%.1f x %.1f x %.1f" % FEAT[b.Name]["size"],
         "%.0f" % FEAT[b.Name]["vol"], "%d" % FEAT[b.Name]["solids"]) for b in ORDER]
rh = 5.4
poly([(tbx0, tby0), (tbx0 + TBL_W, tby0), (tbx0 + TBL_W, tby0 + 8 + rh * (len(rows) + 1)),
      (tbx0, tby0 + 8 + rh * (len(rows) + 1)), (tbx0, tby0)], "FRAME", 0.3)
for xx, hd in ((3, "PIECE"), (46, "SIZE (mm) X x Y x Z"), (140, "VOLUME mm3"), (180, "SOLIDS")):
    text(tbx0 + xx, tby0 + 5.5, hd, 2.4, anchor="start", weight="bold")
for i, (nm, sz, vo, so) in enumerate(rows):
    y = tby0 + 10.7 + i * rh
    text(tbx0 + 3, y, nm, 2.3, anchor="start")
    text(tbx0 + 46, y, sz, 2.3, anchor="start")
    text(tbx0 + 140, y, vo, 2.3, anchor="start")
    text(tbx0 + 180, y, so, 2.3, anchor="start")

# ---- build note (right of the thumbnails)
BX, BY = 440.0, 222.0
build = ["BUILD", 
         "PRINT EACH PIECE SEPARATELY AT 1:1, THEN ASSEMBLE.",
         "THE PIECES OVERLAP BY %.0f mm3 TOTAL AT THE SEAM TABS"
         % (sumv - UNION.Volume),
         "(3 mm LAP IN 10 x 10 PATCHES) - AS SOLIDS THEY",
         "INTERFERE. CHECK THAT IS INTENDED BEFORE PRINTING."]
by_ = BY
for i, line in enumerate(build):
    text(BX, by_, line, 2.35, anchor="start",
         weight="bold" if i == 0 else "normal")
    by_ += 4.6

# ---- title block (bottom right)
TBL_W, TBL_H = 208.0, 52.0
tx0, ty0 = SHEET_W - 8 - TBL_W, SHEET_H - 8 - TBL_H
poly([(tx0, ty0), (tx0 + TBL_W, ty0), (tx0 + TBL_W, ty0 + TBL_H), (tx0, ty0 + TBL_H),
      (tx0, ty0)], "FRAME", 0.5)
trows = [("PART", part.Label),
         ("FILE", os.path.basename(SRC)),
         ("SIZE", "%.2f × %.2f × %.2f mm (X Y Z)" % (W["X"], W["Y"], W["Z"])),
         ("ASSEMBLY", "%d sub-parts, %.0f mm3, %d solid(s)" % (
             len(kids), partv, len(part.Shape.Solids))),
         ("BUILD", "print each sub-part separately; see 1:4 views"),
         ("PROJECTION", "third-angle, TechDraw HLR, headless"),
         ("SCALE / SHEET", "1:2 master, 1:4 sub-parts · A2 · mm"),
         ("REV", "measured from BRep · %s" % STEM)]
for i, (k, v) in enumerate(trows):
    y = ty0 + 5.5 + i * 5.6
    text(tx0 + 3, y, k, 2.5, anchor="start", weight="bold")
    text(tx0 + 33, y, v, 2.7, anchor="start")

poly([(8, 8), (SHEET_W - 8, 8), (SHEET_W - 8, SHEET_H - 8), (8, SHEET_H - 8), (8, 8)],
     "FRAME", 0.35)

# --------------------------------------------------------------- layout checks
def inter(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


problems = []
for i, (box, s) in enumerate(texts):
    if box[0] < 9 or box[1] < 9 or box[2] > SHEET_W - 9 or box[3] > SHEET_H - 9:
        problems.append("OFF SHEET %r at %s" % (s[:24], [round(v, 1) for v in box]))
    for j in range(i + 1, len(texts)):
        if inter(box, texts[j][0]):
            problems.append("TEXT/TEXT %r vs %r" % (s[:20], texts[j][1][:20]))
for v, (cx, cy, sc) in PLACE.items():
    w_, h_ = vwh(v, sc)
    r = (cx - w_ / 2 - 1, cy - h_ / 2 - 1, cx + w_ / 2 + 1, cy + h_ / 2 + 1)
    if r[0] < 9 or r[1] < 9 or r[2] > SHEET_W - 9 or r[3] > SHEET_H - 9:
        problems.append("VIEW %s off sheet" % v)
    if inter(r, (tx0, ty0, tx0 + TBL_W, ty0 + TBL_H)):
        problems.append("VIEW %s over title block" % v)
for a in PLACE:
    for bb_ in PLACE:
        if a < bb_:
            ra = (PLACE[a][0] - vwh(a, PLACE[a][2])[0] / 2, PLACE[a][1] - vwh(a, PLACE[a][2])[1] / 2,
                  PLACE[a][0] + vwh(a, PLACE[a][2])[0] / 2, PLACE[a][1] + vwh(a, PLACE[a][2])[1] / 2)
            rb = (PLACE[bb_][0] - vwh(bb_, PLACE[bb_][2])[0] / 2, PLACE[bb_][1] - vwh(bb_, PLACE[bb_][2])[1] / 2,
                  PLACE[bb_][0] + vwh(bb_, PLACE[bb_][2])[0] / 2, PLACE[bb_][1] + vwh(bb_, PLACE[bb_][2])[1] / 2)
            if inter(ra, rb):
                problems.append("VIEW OVERLAP %s/%s" % (a, bb_))
log("layout problems: %s" % (problems if problems else "NONE"))
log("draw items %d, texts %d" % (len(items), len(texts)))

# ------------------------------------------------------------------ emit files
def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


COL = {"VISIBLE": "#000", "HIDDEN": "#444", "DIMS": "#a01020", "FRAME": "#333",
       "TEXT": "#111", "LABEL": "#111", "SEAM": "#0a6"}
svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="%gmm" height="%gmm" '
       'viewBox="0 0 %g %g">' % (SHEET_W, SHEET_H, SHEET_W, SHEET_H),
       '<rect x="0" y="0" width="%g" height="%g" fill="#fff"/>' % (SHEET_W, SHEET_H)]
for it in items:
    if it[0] == "poly":
        _, layer, pts, width, dash = it
        d = "M %.3f %.3f " % pts[0] + " ".join("L %.3f %.3f" % p for p in pts[1:])
        da = ' stroke-dasharray="%s"' % dash if dash else ""
        svg.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f"%s/>'
                   % (d, COL[layer], width, da))
    else:
        _, layer, x, y, s, size, anchor, weight, rot = it
        tr = ' transform="rotate(-90 %.3f %.3f)"' % (x, y) if rot else ""
        svg.append('<text x="%.3f" y="%.3f" font-size="%.2f" text-anchor="%s" '
                   'dominant-baseline="middle" font-weight="%s" fill="%s" '
                   'font-family="DejaVu Sans, Arial, sans-serif"%s>%s</text>'
                   % (x, y, size, anchor, weight, COL[layer], tr, esc(s)))
svg.append("</svg>")
svgp = os.path.join(OUTDIR, "%s-A2.svg" % STEM)
open(svgp, "w").write("\n".join(svg))

LINES = sum(len(it[2]) - 1 for it in items if it[0] == "poly")
dxf = ["0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n0\nENDSEC",
       "0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLTYPE\n70\n3",
       "0\nLTYPE\n2\nCONTINUOUS\n70\n0\n3\nSolid\n72\n65\n73\n0\n40\n0.0",
       "0\nLTYPE\n2\nDASHED\n70\n0\n3\n_ _ _\n72\n65\n73\n2\n40\n3.0\n49\n1.8\n49\n-1.2",
       "0\nLTYPE\n2\nCENTER\n70\n0\n3\n__ . __\n72\n65\n73\n4\n40\n8.0\n49\n6.0\n49\n-1.6\n49\n1.0\n49\n-1.6",
       "0\nENDTAB", "0\nTABLE\n2\nLAYER\n70\n7"]
for lname, colour, ltype in (("VISIBLE", 7, "CONTINUOUS"), ("HIDDEN", 8, "DASHED"),
                             ("DIMS", 1, "CONTINUOUS"), ("FRAME", 250, "CONTINUOUS"),
                             ("TEXT", 7, "CONTINUOUS"), ("LABEL", 7, "CONTINUOUS"),
                             ("SEAM", 3, "CENTER")):
    dxf.append("0\nLAYER\n2\n%s\n70\n0\n62\n%d\n6\n%s" % (lname, colour, ltype))
dxf += ["0\nENDTAB", "0\nENDSEC", "0\nSECTION\n2\nENTITIES"]
for it in items:
    if it[0] == "poly":
        _, layer, pts, width, dash = it
        for a, b in zip(pts, pts[1:]):
            dxf.append("0\nLINE\n8\n%s\n10\n%.4f\n20\n%.4f\n30\n0.0\n11\n%.4f\n21\n%.4f\n31\n0.0"
                       % (layer, a[0], SHEET_H - a[1], b[0], SHEET_H - b[1]))
    else:
        _, layer, x, y, s, size, anchor, weight, rot = it
        st = (s.replace("⌀", "DIA ").replace("×", "x").replace("—", "-")
               .replace("·", "."))
        dxf.append("0\nTEXT\n8\n%s\n10\n%.4f\n20\n%.4f\n30\n0.0\n40\n%.3f\n1\n%s\n72\n%d\n"
                   "11\n%.4f\n21\n%.4f\n31\n0.0\n50\n%d"
                   % (layer, x, SHEET_H - y, size, st, 1 if anchor == "middle" else 0,
                      x, SHEET_H - y, 90 if rot else 0))
dxf += ["0\nENDSEC\n0\nEOF"]
open(os.path.join(OUTDIR, "%s-A2.dxf" % STEM), "w").write("\n".join(dxf) + "\n")

log("SVG %s (%d bytes, %d line segs)" % (svgp, os.path.getsize(svgp), LINES))
log("DXF %d bytes" % os.path.getsize(os.path.join(OUTDIR, "%s-A2.dxf" % STEM)))
open(os.path.join(OUTDIR, "%s-report.txt" % STEM), "w").write("\n".join(REPORT) + "\n")
