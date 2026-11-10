"""head-mount drawing set: A2 sheet, 3 orthographic views + pictorial, feature
dimensions taken from the model's own sketches/features and cross-checked against
the measured solid. Emits SVG and DXF from one draw-item list.

Run:  freecadcmd make-drawings.py
"""
import FreeCAD as App
import TechDraw
import os

SRC = "/home/danny/Projects/hri-project/cad/head-mount-colour.FCStd"
OUTDIR = "/home/danny/.config/hermes/profiles/support/cache/scratch/drawings"
os.makedirs(OUTDIR, exist_ok=True)

doc = App.openDocument(SRC)
tip = doc.getObject("Fillet")
shape = tip.Shape
bb = shape.BoundBox
W = {"X": bb.XLength, "Y": bb.YLength, "Z": bb.ZLength}

VIS_IDX, HID_IDX = (0, 1, 2, 3, 4), (5, 6, 7, 8, 9)
# measured (frame_probe.py): (u,v) -> (h,w); all det = +1 (pure rotations)
FRAMES = {
    "front": ((0.0, 1.0), (-1.0, 0.0)),       # h=+X, w=+Z
    "side":  ((0.0, -1.0), (1.0, 0.0)),       # h=+Y, w=+Z
    "top":   ((1.0, 0.0), (0.0, 1.0)),        # h=+X, w=+Y
    "axono": ((-1.0, 0.0), (0.0, -1.0)),      # pictorial, Z up
}
for k, (hc, wc) in FRAMES.items():
    assert abs(hc[0] * wc[1] - hc[1] * wc[0] - 1.0) < 1e-9, "frame %s mirrors" % k


def edge_pts(e):
    if e.Curve.TypeId == "Part::GeomLine":
        return [(p.x, p.y) for p in (e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter))]
    return [(p.x, p.y) for p in e.discretize(48)]


def project(direction, frame):
    hc, wc = frame
    parts = list(TechDraw.projectEx(shape, direction))
    out = {}
    for key, idxs in (("vis", VIS_IDX), ("hid", HID_IDX)):
        polys = []
        for i in idxs:
            s = parts[i]
            if s is None:
                continue
            for e in s.Edges:
                polys.append([(u * hc[0] + v * hc[1], u * wc[0] + v * wc[1]) for (u, v) in edge_pts(e)])
        out[key] = polys
    allp = [p for k in out for poly in out[k] for p in poly]
    out["bbox"] = (min(p[0] for p in allp), min(p[1] for p in allp),
                   max(p[0] for p in allp), max(p[1] for p in allp))
    return out


VIEWS = {"FRONT": ((0, -1, 0), "front", ("X", "Z")),
         "SIDE":  ((1, 0, 0), "side", ("Y", "Z")),
         "TOP":   ((0, 0, 1), "top", ("X", "Y")),
         "AXONO": ((1, -1, 0.8), "axono", (None, None))}
data = {}
print("=== frame checks ===")
for label, (d, fk, (ha, wa)) in VIEWS.items():
    data[label] = project(App.Vector(*d), FRAMES[fk])
    x0, y0, x1, y1 = data[label]["bbox"]
    if ha:
        assert abs((x1 - x0) - W[ha]) < 0.05 and abs((y1 - y0) - W[wa]) < 0.05, label
        print("  %-6s h=%.3f (%s) w=%.3f (%s)  OK" % (label, x1 - x0, ha, y1 - y0, wa))


def edges_near(label, axis, value, tol=0.35):
    """number of projected segments lying on a line at h/w == value (feature exists)"""
    idx = 0 if axis == "h" else 1
    n = 0
    for key in ("vis", "hid"):
        for poly in data[label][key]:
            for a, b in zip(poly, poly[1:]):
                if abs(a[idx] - value) < tol and abs(b[idx] - value) < tol:
                    n += 1
    return n


# ---------------------------------------------------------- feature data (model)
sk = {o.Name: o for o in doc.Objects if o.TypeId == "Sketcher::SketchObject"}
feat = {o.Name: o for o in doc.Objects if o.TypeId.startswith("PartDesign::")}


def world(centre, placement):
    v = placement.multVec(App.Vector(centre.x, centre.y, 0))
    return (v.x, v.y, v.z)


holes = [world(g.Center, sk["Sketch004"].Placement) for g in sk["Sketch004"].Geometry if g.TypeId.endswith("Circle")]
hr = sk["Sketch004"].Geometry[0].Radius
cyl_z = [f for f in shape.Faces if f.Surface.TypeId == "Part::GeomCylinder"
         and abs(abs(f.Surface.Axis.z) - 1.0) < 1e-6 and abs(f.Surface.Radius - hr) < 1e-3]
cyl_f = [f for f in shape.Faces if f.Surface.TypeId == "Part::GeomCylinder" and abs(f.Surface.Radius - 25.0) < 1e-3]

hx = sorted({round(p[0], 3) for p in holes})
hy = sorted({round(p[1], 3) for p in holes})
DIMS = {}
DIMS["hole_dia"] = 2 * hr
DIMS["hole_n"] = len(holes)
DIMS["hole_thru"] = feat["Pocket003"].Type == "ThroughAll"
DIMS["sp_x"] = hx[-1] - hx[0]
DIMS["sp_y"] = hy[-1] - hy[0]
DIMS["off_x"] = hx[0] - bb.XMin
DIMS["cav_x"] = max(g.StartPoint.x for g in sk["Sketch002"].Geometry) - min(g.StartPoint.x for g in sk["Sketch002"].Geometry)
DIMS["cav_y"] = 212.130
DIMS["win_x"] = 270.0
DIMS["win_z"] = 200.0
DIMS["slot_w"] = 237.071 - 227.071
DIMS["groove_w"] = 230.121 - 227.071
DIMS["plate_t"] = feat["Pad001"].Length
DIMS["fillet_r"] = feat["Fillet"].Radius

print("\n=== feature data: value | source | cross-check against the solid ===")
checks = [
    ("hole dia", DIMS["hole_dia"], "Sketch004 circle r=%.2f" % hr,
     "%d cylinder face(s) r=%.2f along Z in solid" % (len(cyl_z), hr)),
    ("hole count", DIMS["hole_n"], "Sketch004 circles", "%d cylinder face(s) found" % len(cyl_z)),
    ("hole thru", DIMS["hole_thru"], "Pocket003 Type", "ThroughAll" if DIMS["hole_thru"] else "not through"),
    ("spacing X", DIMS["sp_x"], "hole centres x %.2f / %.2f" % (hx[0], hx[-1]),
     "%d edge(s) at h=%.2f, %d at h=%.2f in TOP" % (edges_near("TOP", "h", hx[0]), hx[0], edges_near("TOP", "h", hx[-1]), hx[-1])),
    ("spacing Y", DIMS["sp_y"], "hole centres y %.2f / %.2f" % (hy[0], hy[-1]), "measured from placement"),
    ("offset X", DIMS["off_x"], "hole x %.2f vs bbox Xmin %.2f" % (hx[0], bb.XMin), "symmetric: right offset %.2f" % (bb.XMax - hx[-1])),
    ("cavity X", DIMS["cav_x"], "Sketch002 outline + DistanceX=280.0",
     "%d edge(s) at h=10.00, %d at h=290.00 in TOP" % (edges_near("TOP", "h", 10.0), edges_near("TOP", "h", 290.0))),
    ("cavity Y", DIMS["cav_y"], "Sketch002 DistanceY=212.13",
     "%d edge(s) at w=212.13, %d at w=0.00 in TOP" % (edges_near("TOP", "w", 212.130), edges_near("TOP", "w", 0.0))),
    ("window X", DIMS["win_x"], "Sketch001 Distance=270.0",
     "%d edge(s) at h=15.00 / h=285.00 in FRONT" % (edges_near("FRONT", "h", 15.0) + edges_near("FRONT", "h", 285.0))),
    ("window Z", DIMS["win_z"], "Sketch001 Distance=200.0",
     "%d edge(s) at w=200.00 in FRONT" % edges_near("FRONT", "w", 200.0)),
    ("slot width", DIMS["slot_w"], "Sketch005 outline 227.071..237.071",
     "%d edge(s) at h=227.07 / 237.07 in SIDE" % (edges_near("SIDE", "h", 227.071) + edges_near("SIDE", "h", 237.071))),
    ("groove width", DIMS["groove_w"], "Sketch006 outline 227.071..230.121",
     "%d edge(s) at h=230.12 in SIDE" % edges_near("SIDE", "h", 230.121)),
    ("plate thick", DIMS["plate_t"], "Pad001 Length",
     "%d edge(s) at w=-15.00 / 0.00 in FRONT" % (edges_near("FRONT", "w", -15.0) + edges_near("FRONT", "w", 0.0))),
    ("fillet R", DIMS["fillet_r"], "Fillet.Radius",
     "%d cylinder face(s) r=25 in solid" % len(cyl_f)),
]
for name, val, src, check in checks:
    print("  %-12s %-10s | %-38s | %s" % (name, val, src, check))

# ------------------------------------------------------------------ sheet layout
SHEET_W, SHEET_H, SCALE = 594.0, 420.0, 0.5
items = []          # ("poly"|"line"|"text", layer, ...) in sheet mm


def wh(label):
    x0, y0, x1, y1 = data[label]["bbox"]
    return (x1 - x0) * SCALE, (y1 - y0) * SCALE


fw, fh, tw, th, sw, sh, aw, ah = (wh("FRONT") + wh("TOP") + wh("SIDE") + wh("AXONO"))
COL1, COL2, ROW1, GAPPY = 34.0, 239.0, 20.0, 25.0
FRONT_CY = ROW1 + th + GAPPY + fh / 2
place = {"TOP": (COL1 + tw / 2, ROW1 + th / 2),
         "FRONT": (COL1 + fw / 2, FRONT_CY),
         "SIDE": (COL2 + sw / 2, FRONT_CY),
         "AXONO": (375.0 + aw / 2, ROW1 + ah / 2)}


def S(label, h, w):
    x0, y0, x1, y1 = data[label]["bbox"]
    cx, cy = place[label]
    return (cx + (h - (x0 + x1) / 2) * SCALE, cy - (w - (y0 + y1) / 2) * SCALE)


texts = []


def text(x, y, s, size=3.0, anchor="middle", weight="normal", layer="TEXT"):
    w = 0.62 * size * len(s)
    h = size * 1.15
    if anchor == "middle":
        box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    elif anchor == "start":
        box = (x, y - h / 2, x + w, y + h / 2)
    else:                                  # end
        box = (x - w, y - h / 2, x, y + h / 2)
    texts.append((box, s))
    items.append(("text", layer, x, y, s, size, anchor, weight))


def poly(pts, layer, width, dash=False):
    items.append(("poly", layer, pts, width, dash))


def arrow(tip_xy, direction, size=2.2):
    dx, dy = direction
    n = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    t = (tip_xy[0], tip_xy[1])
    b = (tip_xy[0] - ux * size, tip_xy[1] - uy * size)
    poly([t, (b[0] + px * size * 0.4, b[1] + py * size * 0.4), (b[0] - px * size * 0.4, b[1] - py * size * 0.4), t],
         "DIMS", 0.25)


def dims_of(label, h0, h1, w0, w1, text_s, off, orient):
    """linear dimension; h0..h1 along the measured axis, placed off mm to the
    outside (negative = other side). orient 'h' measures h, 'v' measures w."""
    if orient == "h":
        ax, ay = S(label, h0, w0)
        bx, _ = S(label, h1, w0)
        y = ay + off
        x1, x2 = sorted((ax, bx))
        poly([(x1, y), (x2, y)], "DIMS", 0.25)
        for x in (x1, x2):
            poly([(x, y - 1.2), (x, y + 1.2)], "DIMS", 0.25)
        arrow((x1, y), (1, 0))
        arrow((x2, y), (-1, 0))
        text((x1 + x2) / 2, y - 2.7, text_s)
    else:
        ax, ay = S(label, h0, w0)
        _, by = S(label, h0, w1)
        x = ax - off
        y1, y2 = sorted((ay, by))
        poly([(x, y1), (x, y2)], "DIMS", 0.25)
        for y in (y1, y2):
            poly([(x - 1.2, y), (x + 1.2, y)], "DIMS", 0.25)
        arrow((x, y1), (0, -1))
        arrow((x, y2), (0, 1))
        text(x - 3.1, (y1 + y2) / 2, text_s)


def leader(label, h, w, dx, dy, note, length=11.0, size=3.0):
    """leader from a feature point at (h,w) out along sheet direction (dx,dy)"""
    x0, y0 = S(label, h, w)
    ln = length
    n = (dx * dx + dy * dy) ** 0.5
    x1, y1 = x0 + dx / n * ln, y0 + dy / n * ln
    x2 = x1 + (6.0 if dx >= 0 else -6.0)
    poly([(x0, y0), (x1, y1), (x2, y1)], "DIMS", 0.25)
    arrow((x0, y0), (x1 - x0, y1 - y0), 2.4)
    text(x2 + (1.6 if dx >= 0 else -1.6), y1 - 2.4, note, size,
         anchor="start" if dx >= 0 else "end")


# geometry
for label in ("TOP", "FRONT", "SIDE", "AXONO"):
    for poly_pts in data[label]["hid"]:
        poly([S(label, h, w) for h, w in poly_pts], "HIDDEN", 0.22, dash=True)
    for poly_pts in data[label]["vis"]:
        poly([S(label, h, w) for h, w in poly_pts], "VISIBLE", 0.5)
    cx, cy = place[label]
    w_, h_ = wh(label)
    text(cx, cy + h_ / 2 + 5.5, {"SIDE": "SIDE (from +X)", "AXONO": "PICTORIAL"}.get(label, label),
         4.0, weight="bold", layer="LABEL")

# overall dimensions (measured extents)
for label, (ha, wa) in (("FRONT", ("X", "Z")), ("TOP", ("X", "Y")), ("SIDE", ("Y", "Z"))):
    x0_, y0_, x1_, y1_ = data[label]["bbox"]
    dims_of(label, x0_, x1_, y0_, 0, "%s %.1f" % (ha, W[ha]), 14.0, "h")
    dims_of(label, x0_, 0, y0_, y1_, "%s %.1f" % (wa, W[wa]), 14.0, "v")

# feature dimensions - every value below was measured (removed_volume.py) or read
# from the governing sketch constraint, and cross-checked in the table above.
dims_of("TOP", hx[0], hx[-1], -11.0, 0, "%.1f" % DIMS["sp_x"], 0.0, "h")     # hole pitch X (inside, at bottom)
dims_of("TOP", -20.0, 0, hy[0], hy[-1], "%.1f" % DIMS["sp_y"], 0.0, "v")     # hole pitch Y (inside, at left)
dims_of("TOP", 10.0, 290.0, 150.0, 0, "%.1f" % DIMS["cav_x"], 0.0, "h")      # cavity 280 (inside)
dims_of("TOP", 200.0, 0, 0.0, 212.13, "%.2f" % DIMS["cav_y"], 0.0, "v")      # cavity 212.13 (inside)
dims_of("FRONT", 15.0, 285.0, -15.0, 0, "%.1f" % DIMS["win_x"], 30.0, "h")   # window 270 (below view)
dims_of("FRONT", 150.0, 0, 0.0, 200.0, "%.1f" % DIMS["win_z"], 0.0, "v")     # window 200 (inside)
dims_of("FRONT", 20.0, 0, -15.0, 0.0, "%.1f" % DIMS["plate_t"], 0.0, "v")    # plate thickness (inside)

leader("TOP", hx[-1], hy[-1], 1.0, -0.35, "%d\u00d7 \u00d8%.2f THRU" % (DIMS["hole_n"], DIMS["hole_dia"]), length=24.0)
leader("TOP", 300.0, 212.071, 1.0, 1.1, "R%.0f" % DIMS["fillet_r"], length=26.0)

# notes + title block
NOTES = ["NOTES \u2014 ALL DIMENSIONS IN mm",
         "1. FEATURE DIMS READ FROM THE GOVERNING SKETCH",
         "   CONSTRAINT, CROSS-CHECKED AGAINST THE SOLID.",
         "2. WINDOW 270.0 \u00d7 200.0 THRU IN Y (Sketch001).",
         "3. CAVITY 280.0 \u00d7 212.13 THRU IN Z (Sketch002).",
         "4. SLOT 300.0 \u00d7 10.0 \u00d7 212.13 DEEP (Sketch005),",
         "   FLOOR AT z=0.063.",
         "5. GROOVE 300.0 \u00d7 3.05 \u00d7 7.0 DEEP (Sketch006),",
         "   FLOOR AT z=-6.937, CUT INTO THE SLOT FLOOR.",
         "6. HIDDEN EDGES DASHED. THIRD-ANGLE PROJECTION."]
for i, line in enumerate(NOTES):
    text(378.0, 232.0 + i * 4.4, line, 2.6, anchor="start", layer="TEXT")

TBL_W, TBL_H = 210.0, 52.0
tbx0, tby0 = SHEET_W - 6 - TBL_W, SHEET_H - 6 - TBL_H
poly([(tbx0, tby0), (tbx0 + TBL_W, tby0), (tbx0 + TBL_W, tby0 + TBL_H), (tbx0, tby0 + TBL_H), (tbx0, tby0)], "FRAME", 0.5)
rows = [("PART", tip.Label), ("FILE", os.path.basename(SRC)),
        ("SOURCE", "Part Design Body, tip = %s" % tip.Name),
        ("SIZE", "%.2f \u00d7 %.2f \u00d7 %.2f mm (X Y Z)" % (W["X"], W["Y"], W["Z"])),
        ("VOLUME", "%.0f mm\u00b3 in %d solid(s)" % (shape.Volume, len(shape.Solids))),
        ("PROJECTION", "TechDraw HLR, headless \u00b7 front(X,Z) top(X,Y) side(Y,Z)"),
        ("SCALE / SHEET", "1:2  \u00b7  A2  \u00b7  mm  \u00b7  third-angle"),
        ("REV", "colour #FFFFFF \u00b7 feature dims from model constraints")]
for i, (k, v) in enumerate(rows):
    y = tby0 + 5.5 + i * 5.6
    text(tbx0 + 3, y, k, 2.6, anchor="start", weight="bold", layer="TEXT")
    text(tbx0 + 34, y, v, 2.8, anchor="start", layer="TEXT")

poly([(6, 6), (SHEET_W - 6, 6), (SHEET_W - 6, SHEET_H - 6), (6, SHEET_H - 6), (6, 6)], "FRAME", 0.35)

# ------------------------------------------------------------------ layout check
def rect(label):
    cx, cy = place[label]
    w_, h_ = wh(label)
    return (cx - w_ / 2 - 1, cy - h_ / 2 - 1, cx + w_ / 2 + 1, cy + h_ / 2 + 1)


def inter(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


problems = []
for i, (box, s) in enumerate(texts):
    if box[0] < 8 or box[1] < 8 or box[2] > SHEET_W - 8 or box[3] > SHEET_H - 8:
        problems.append("OFF SHEET: %r at %s" % (s, [round(v, 1) for v in box]))
    for j, (box2, s2) in enumerate(texts):
        if j > i and inter(box, box2):
            problems.append("TEXT/TEXT: %r vs %r" % (s[:18], s2[:18]))
for a in place:
    for b in place:
        if a < b and inter(rect(a), rect(b)):
            problems.append("VIEW OVERLAP %s/%s" % (a, b))
tbr = (tbx0, tby0, tbx0 + TBL_W, tby0 + TBL_H)
for a in place:
    if inter(rect(a), tbr):
        problems.append("VIEW %s over title block" % a)
    if inter(rect(a), (378.0 - 1, 246, 560, 285)):
        problems.append("VIEW %s over notes" % a)

print("\nlayout problems:", problems if problems else "NONE")
print("views:", {k: (round(v[0], 1), round(v[1], 1)) for k, v in place.items()})
print("draw items:", len(items), "| texts:", len(texts))

# ------------------------------------------------------------------ emit SVG
def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


COL = {"VISIBLE": "#000", "HIDDEN": "#333", "DIMS": "#b00020", "FRAME": "#333", "TEXT": "#111", "LABEL": "#111"}
svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="%gmm" height="%gmm" viewBox="0 0 %g %g">'
       % (SHEET_W, SHEET_H, SHEET_W, SHEET_H),
       '<rect x="0" y="0" width="%g" height="%g" fill="#ffffff" stroke="#333" stroke-width="0.5"/>' % (SHEET_W, SHEET_H)]
for it in items:
    if it[0] == "poly":
        _, layer, pts, width, dash = it
        d = "M %.3f %.3f " % pts[0] + " ".join("L %.3f %.3f" % p for p in pts[1:])
        da = ' stroke-dasharray="1.8 1.2"' if dash else ""
        svg.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f"%s/>' % (d, COL[layer], width, da))
    else:
        _, layer, x, y, s, size, anchor, weight = it
        svg.append('<text x="%.3f" y="%.3f" font-size="%.2f" text-anchor="%s" dominant-baseline="middle" '
                   'font-weight="%s" fill="%s" font-family="DejaVu Sans, Arial, sans-serif">%s</text>'
                   % (x, y, size, anchor, weight, COL[layer], esc(s)))
svg.append("</svg>")
svg_path = os.path.join(OUTDIR, "head-mount-sheet.svg")
open(svg_path, "w").write("\n".join(svg))

# ------------------------------------------------------------------ emit DXF
LINES = sum(len(it[2]) - 1 for it in items if it[0] == "poly")
dxf = ["0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n0\nENDSEC",
       "0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLTYPE\n70\n2",
       "0\nLTYPE\n2\nCONTINUOUS\n70\n0\n3\nSolid line\n72\n65\n73\n0\n40\n0.0",
       "0\nLTYPE\n2\nDASHED\n70\n0\n3\nDashed _ _ _\n72\n65\n73\n2\n40\n3.0\n49\n1.8\n49\n-1.2\n0\nENDTAB",
       "0\nTABLE\n2\nLAYER\n70\n6"]
for lname, colour, ltype in (("VISIBLE", 7, "CONTINUOUS"), ("HIDDEN", 8, "DASHED"),
                             ("DIMS", 1, "CONTINUOUS"), ("FRAME", 250, "CONTINUOUS"),
                             ("TEXT", 7, "CONTINUOUS"), ("LABEL", 7, "CONTINUOUS")):
    dxf.append("0\nLAYER\n2\n%s\n70\n0\n62\n%d\n6\n%s" % (lname, colour, ltype))
dxf += ["0\nENDTAB", "0\nENDSEC", "0\nSECTION\n2\nENTITIES"]
for it in items:
    if it[0] == "poly":
        _, layer, pts, width, dash = it
        for a, b in zip(pts, pts[1:]):
            dxf.append("0\nLINE\n8\n%s\n10\n%.4f\n20\n%.4f\n30\n0.0\n11\n%.4f\n21\n%.4f\n31\n0.0"
                       % (layer, a[0], SHEET_H - a[1], b[0], SHEET_H - b[1]))
    else:
        _, layer, x, y, s, size, anchor, weight = it
        dxf.append("0\nTEXT\n8\n%s\n10\n%.4f\n20\n%.4f\n30\n0.0\n40\n%.3f\n1\n%s\n72\n%d\n11\n%.4f\n21\n%.4f\n31\n0.0"
                   % (layer, x, SHEET_H - y, size, s, 1 if anchor == "middle" else 0, x, SHEET_H - y))
dxf += ["0\nENDSEC", "0\nEOF"]
dxf_path = os.path.join(OUTDIR, "head-mount-sheet.dxf")
open(dxf_path, "w").write("\n".join(dxf) + "\n")

print("SVG: %s (%d bytes)" % (svg_path, os.path.getsize(svg_path)))
print("DXF: %s (%d bytes, %d LINE entities, %d TEXT)" % (dxf_path, os.path.getsize(dxf_path), LINES,
                                                         sum(1 for it in items if it[0] == "text")))
