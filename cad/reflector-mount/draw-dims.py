#!/usr/bin/env python3
"""Draw the reflector mount as a dimensioned 3-sheet A3 PDF.

Geometry comes from reflector-mount.py (imported, never retyped), so this drawing
cannot drift from the model.  Every label is placed on the page explicitly and the
finished sheets are then checked mechanically for overlapping text.

    cd ~/Projects/hri-project && uv run --with matplotlib python cad/reflector-mount/draw-dims.py

Sheet 1  1:5  assembly section on the mirror's centre line — the optics dimensions
Sheet 2  1:2.5 the two printed rings in true shape + hole table + notes
Sheet 3  2:1  details: pillar bore and set screw, clamp stack, fence + print notes
"""
import importlib.util
import math
import pathlib
import struct

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Arc, Ellipse, Polygon

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mount", HERE / "reflector-mount.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)          # parameters only: the __main__ guard blocks exports

C45 = math.sqrt(0.5)
MM = 2.834645669                     # mm -> pt for font sizes
PAGE = (420.0, 297.0)                # A3 landscape in mm
DPI = 110


def world(a, b):
    """local (through-thickness a, along-slope b) -> world (X, Z)"""
    return (M.CMX + C45 * (a + b), M.CMZ + C45 * (b - a))


def local(a, b):
    return (a, b)


def rect(a0, a1, b0, b1):
    return [world(a0, b0), world(a1, b0), world(a1, b1), world(a0, b1)]


def lrect(a0, a1, b0, b1):
    """the same rectangle kept in the LOCAL frame, for the axis-aligned details"""
    return [(a0, b0), (a1, b0), (a1, b1), (a0, b1)]


def lz(px, mid):
    """local Z where a world-vertical line at X = px crosses a plane at local X = mid"""
    return 1.4142135 * (px - M.CMX) - mid


# the beam's footprint on the face, top and bottom, and the lit content at the screen
FOOT = [(-63.2, 536.8), (80.0, 680.0)]
LIT = [(250.0, 500.0), (250.0, 700.0)]


class Sheet:
    """One A3 page.  Data units are millimetres ON THE PAGE, so text sizes are
    absolute and each view carries its own scale."""

    def __init__(self, title, scale, scalenum, subtitle=""):
        self.title, self.sub, self.scale, self.S = title, subtitle, scale, scalenum
        self.fig = plt.figure(figsize=(PAGE[0] / 25.4, PAGE[1] / 25.4))
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, PAGE[0])
        self.ax.set_ylim(0, PAGE[1])
        self.ax.set_aspect("equal")
        self.ax.axis("off")
        self.ax.add_patch(Polygon([(8, 8), (412, 8), (412, 289), (8, 289)], closed=True,
                                  fc="none", ec="#444", lw=0.8))
        self.ox = self.oy = 0.0
        self.mapfn = world

    # ------------------------------------------------------------ placement
    #  Convention: the coordinates you pass to poly/line/dim/leader are in the
    #  frame of the current view — world (X,Z), local (through-thickness,
    #  along-slope), or face (Y, Z_local).  Those frames differ by more than a
    #  translation, so the mapping is done by the caller with world() and the
    #  view only sets the page origin and scale.
    def view(self, ox, oy):
        self.ox, self.oy = ox, oy
        return self

    def at(self, src, dst):
        """put the view point src at page point dst.  src is expressed in the
        frame the view's coordinates are already in — pass world(...) to locate
        a local point in the world frame."""
        self.ox, self.oy = dst[0] - src[0] * self.S, dst[1] - src[1] * self.S
        return self

    def p(self, x, y):
        return (self.ox + x * self.S, self.oy + y * self.S)

    def poly(self, pts, **kw):
        self.ax.add_patch(Polygon([self.p(x, y) for x, y in pts], closed=True, **kw))

    def line(self, pts, **kw):
        v = np.array([self.p(x, y) for x, y in pts])
        self.ax.plot(v[:, 0], v[:, 1], **kw)

    def text(self, x, y, s, h=2.2, col="#000", ha="center", va="center", rot=0, w="normal"):
        self.ax.text(*self.p(x, y), s, fontsize=h * MM, color=col, ha=ha, va=va,
                     rotation=rot, rotation_mode="anchor", weight=w)

    def ptext(self, x, y, s, h=2.2, **kw):
        self.ax.text(x, y, s, fontsize=h * MM, **kw)

    # ------------------------------------------------------------------ dims
    def dim(self, p1, p2, label, off=8, h=2.0, frame=None):
        """dimension between two view points, offset perpendicular (page mm)"""
        a, b = self.p(*p1), self.p(*p2)
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L * off, dx / L * off
        a2, b2 = (a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny)
        self.ax.annotate("", xy=a2, xytext=b2, arrowprops=dict(
            arrowstyle="<|-|>", color="#b00", lw=0.7, mutation_scale=6))
        self.ax.plot([a[0], a2[0]], [a[1], a2[1]], color="#b00", lw=0.5)
        self.ax.plot([b[0], b2[0]], [b[1], b2[1]], color="#b00", lw=0.5)
        ang = math.degrees(math.atan2(dy, dx))
        if abs(ang) > 90:
            ang += 180
        pad = 0.8
        self.ax.text((a2[0] + b2[0]) / 2 + (-ny / L) * pad, (a2[1] + b2[1]) / 2 + (nx / L) * pad,
                     label, fontsize=h * MM, ha="center", va="center", rotation=ang,
                     rotation_mode="anchor", color="#b00")

    def leader(self, src, label, page_xy, h=2.1, col="#333"):
        """arrow at view point src, label text placed at page point page_xy.
        Drawn as a separate text + a bare arrow so the text box can be measured."""
        self.ax.annotate("", xy=self.p(*src), xytext=page_xy,
                         arrowprops=dict(arrowstyle="-", color="#888", lw=0.6,
                                         shrinkA=1, shrinkB=2))
        self.ax.text(page_xy[0], page_xy[1], label, fontsize=h * MM, color=col,
                     ha="left", va="center", linespacing=1.35)

    # ------------------------------------------------------------- furniture
    def heading(self, sheet, sub):
        self.ptext(20, 280, "SHEET %d — %s" % (sheet, self.title), h=4.8, weight="bold",
                   ha="left", va="center")
        self.ptext(20, 273.5, sub, h=2.4, ha="left", va="center", color="#333")

    def titleblock(self, sheet):
        x0, y0, w, hh = 250, 14, 162, 40
        self.ax.add_patch(Polygon([(x0, y0), (x0 + w, y0), (x0 + w, y0 + hh), (x0, y0 + hh)],
                                  closed=True, fc="white", ec="#444", lw=0.8))
        rows = [("ORIN RIG — REFLECTOR MOUNT", 3.2, "bold"),
                (self.title, 2.7, "normal"),
                (self.sub, 2.3, "normal"),
                ("SCALE %s    UNITS mm    A3    SHEET %d of 3" % (self.scale, sheet), 2.3, "normal"),
                ("PETG, 0.2 mm layers — prototype for a single demonstration", 2.1, "normal")]
        for i, (s, h, w8) in enumerate(rows):
            self.ptext(x0 + 3, y0 + hh - 6.4 - i * 6.3, s, h=h, ha="left", va="center", weight=w8)

    def column(self, x, y0, lines, h=2.1, col="#333"):
        y = y0
        for s in lines:
            bold = s.isupper() and len(s) < 40
            self.ptext(x, y, s, h=h, ha="left", va="center",
                       color="#000" if bold else col, weight="bold" if bold else "normal")
            y -= h * 2.0
        return y

    # ------------------------------------------------------------ self-check
    def check(self, sheet):
        self.fig.canvas.draw()
        r = self.fig.canvas.get_renderer()
        s = 25.4 / self.fig.dpi                # mm per pixel
        boxes, outside = [], []
        for t in self.ax.texts:
            if not t.get_text().strip():
                continue                       # bare arrows carry no text
            bb = t.get_window_extent(renderer=r)
            box = (bb.x0 * s, bb.y0 * s, bb.x1 * s, bb.y1 * s)
            boxes.append((t.get_text()[:34].replace("\n", " "), box))
            if box[0] < 9 or box[2] > 411 or box[1] < 9 or box[3] > 288:
                outside.append((t.get_text()[:34].replace("\n", " "), box))
        bad = []
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i][1], boxes[j][1]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    bad.append((boxes[i][0], boxes[j][0]))
        for name, box in outside:
            bad.append((name, "OFF PAGE"))
        return bad, boxes

    def save(self, pdf, png=None):
        if png:
            self.fig.savefig(png, dpi=DPI, facecolor="white")
        pdf.savefig(self.fig)
        plt.close(self.fig)


HATCH = dict(fc="white", ec="#333", lw=0.9, hatch="////")
THIN = dict(fc="none", ec="#9a9a9a", lw=0.6, ls=(0, (2, 2)))
BEAM = dict(fc="#f7edff", ec="#a0f", lw=0.9, ls=(0, (4, 3)))


# ================================================================== sheet 1
def sheet1():
    s = Sheet("ASSEMBLY SECTION", "1:5", 0.2,
              "cut on the mirror's centre line — world X forward, Z up")
    s.view(112, 83)
    s.main = (112, 83)          # the main section's origin, for the tripwire
    s.poly([(0, 0)] + FOOT[::-1], **BEAM)
    s.poly(FOOT + LIT[::-1], **BEAM)
    for a0, a1, b0, b1 in ((M.BX1, -M.GAP, M.BACK_Z0, -M.OPEN_BZ),
                           (M.BX1, -M.GAP, M.OPEN_BZ, M.BACK_Z1),
                           (M.FX0, M.FX0 + M.LIP_T, M.LIP_Z0, -M.OPEN_FZ),
                           (M.FX0, M.FX0 + M.LIP_T, M.OPEN_FZ, M.LIP_Z1),
                           (-2, M.FENCE_T + M.MIR_T + M.GAP + 2, -134, -134 + M.FENCE_H)):
        s.poly(rect(a0, a1, b0, b1), **HATCH)
    s.poly(rect(0, M.MIR_T, -M.MIR / 2, M.MIR / 2), fc="#bfe0ff", ec="#036", lw=1.5)
    for sx in (-1, 1):
        s.poly([(sx * M.PIL_X - 3.15, -60), (sx * M.PIL_X + 3.15, -60),
                (sx * M.PIL_X + 3.15, 790), (sx * M.PIL_X - 3.15, 790)], **THIN)
    s.poly([(-150, M.BAR_Z - 3.15), (285, M.BAR_Z - 3.15),
            (285, M.BAR_Z + 3.15), (-150, M.BAR_Z + 3.15)], **THIN)
    s.poly([(250, 470), (265, 470), (265, 730), (250, 730)], fc="#fafafa", ec="#999", lw=0.8)
    s.poly([(-70, -170), (70, -170), (70, -60), (-70, -60)], fc="#fdfaf0", ec="#999", lw=0.8)
    s.poly([(-150, -180), (150, -180), (150, -170), (-150, -170)], fc="#fdfaf0", ec="#999", lw=0.8)
    s.line([(0, -80), (0, M.LENS_MIR + 90)], color="#a0f", lw=0.7, ls=(0, (5, 3)))
    s.line([world(0, -M.MIR / 2), world(0, M.MIR / 2)], color="#036", lw=2.0)
    cx, cy = s.p(0, 300)
    s.ax.add_patch(Arc((cx, cy), 34, 34, theta1=0, theta2=45, color="#b00", lw=0.7))
    s.ptext(cx + 10, cy + 6, "45.0°", h=2.2, color="#b00", ha="left", va="center")
    # dimensions
    s.dim((-150, -110), (-150, M.LENS_MIR), "600   lens → mirror plane", off=-9, h=2.3)
    s.dim((0, -140), (250, -140), "250   mirror plane → screen", off=9, h=2.3)
    s.dim(world(0, -M.MIR / 2), world(0, M.MIR / 2), "250 acrylic", off=-24, h=2.3)
    # leaders, label positions all explicit (page mm) and spaced 20 mm apart
    s.leader(world(-M.GAP - M.BACK_T / 2, 132), "back ring, 8 thick", (150, 258))
    s.leader(world(M.FX0 + M.LIP_T / 2, 122), "lip ring, 6 thick\nperimeter only", (150, 240))
    s.leader(world(-4, -70), "back 8 · acrylic 3 · lip 6\ngroove 3.30 — see sheet 3", (150, 220))
    s.leader((0, 700), "lens axis; mirror centre 12 mm up-slope,\n"
                       "so the 203 × 160 footprint sits central", (150, 200))
    s.leader(world(M.FX0 + M.LIP_T / 2, M.OPEN_FZ), "lip ring's top edge clears\n"
                                                  "the top ray by ≈6 mm", (150, 180))
    s.leader((250, 600), "screen 230 × 230, centre 250 forward,\nsame height as the hit point",
             (150, 160))
    s.leader(world(3, -130), "fence 8.5 high, 5.5 proud,\nbites 2 mm into the ring", (150, 140))
    s.leader(world(-M.GAP - M.BACK_T / 2, -M.OPEN_BZ), "cage opening 200 × 238 —\n"
                                                      "the light needs 160 × 203", (150, 120))
    s.leader((0, -110), "projector lens = origin", (20, 60))
    # pattern inset, bottom right of the left half
    s.at((0, 0), (196, 100))
    s.poly([(-M.OUT_Y, 495), (M.OUT_Y, 495), (M.OUT_Y, 720), (-M.OUT_Y, 720)],
           fc="none", ec="#666", lw=0.8)
    s.poly([(-M.MIR / 2, 518), (M.MIR / 2, 518), (M.MIR / 2, 697), (-M.MIR / 2, 697)],
           fc="#eef6ff", ec="#036", lw=0.9)
    for sx in (-1, 1):
        for sy in (-1, 1):
            s.poly([(sx * M.PIL_X - 3.15, -60), (sx * M.PIL_X + 3.15, -60),
                    (sx * M.PIL_X + 3.15, 780), (sx * M.PIL_X - 3.15, 780)], **THIN)
    for sy in (-1, 1):
        s.ax.add_patch(Ellipse(s.p(sy * M.BAR_Y, M.BAR_Z), 6.3 * s.S, 6.3 * s.S,
                               fc="none", ec="#2a6b2a", lw=0.9))
    s.dim((-M.PIL_X, 380), (M.PIL_X, 380), "200", off=-8, h=2.0)
    s.ptext(233, 106, "pillar pitch 212", h=2.1, ha="center", color="#333")
    s.ptext(233, 100, "up the beam", h=2.1, ha="center", color="#333")
    s.ptext(233, 94, "pillar + bar pattern", h=2.1, ha="center", color="#333")
    s.heading(1, "rings hatched · acrylic blue · rods, screen, base grey (behind the cut) · light violet")
    s.titleblock(1)
    s.column(250, 250, [
        "NOTES",
        "1.  Groove 3.30 nominal for 3.00 acrylic.  The pads on the screw axes",
        "     set that width, so tightening the M3s cannot close the groove onto",
        "     nothing and leave the sheet loose.",
        "2.  Second-surface mirror: the coating lies ON the optical plane and the",
        "     sheet sits in front of it, toward the lens.",
        "3.  Pillars Ø6.3 on a 200 × 212 plan pattern; screen-mount bars Ø6.3 at",
        "     Z 505, Y ±120, through the lower end band.",
        "4.  Envelope 225 × 270 × 225 mm.  Both rings exceed the SOVOL bed —",
        "     print on the university machine, or split each ring.",
        "5.  Light clearance verified by intersecting the parts with the beam",
        "     envelopes: both rings miss both the incoming and reflected light.",
    ])
    return s


# ================================================================== sheet 2
def sheet2():
    s = Sheet("PART TRUE VIEWS", "1:3", 1 / 3.0,
              "looking along the mirror's normal; up-slope is up the page")

    def ring(centre, kind):
        s.at((0, 0), centre)
        if kind == "back":
            z0, z1, ob, of, mid, T = M.BACK_Z0, M.BACK_Z1, M.OPEN_BY, M.OPEN_BZ, M.MIDB, M.BACK_T
        else:
            z0, z1, ob, of, mid, T = M.LIP_Z0, M.LIP_Z1, M.OPEN_FY, M.OPEN_FZ, M.MIDF, M.LIP_T
        s.poly([(-M.OUT_Y, z0), (M.OUT_Y, z0), (M.OUT_Y, z1), (-M.OUT_Y, z1)],
               fc="#f2f2f2", ec="#333", lw=1.2)
        s.poly([(-ob, -of), (ob, -of), (ob, of), (-ob, of)], fc="white", ec="#333", lw=1.1)
        if kind == "back":
            s.poly([(-M.OUT_Y, -134), (M.OUT_Y, -134), (M.OUT_Y, -134 + M.FENCE_H),
                    (-M.OUT_Y, -134 + M.FENCE_H)], fc="none", ec="#666", lw=0.7, ls=(0, (3, 2)))
        for sx in (-1, 1):
            for sy in (-1, 1):
                cy, cz = sy * M.PIL_Y, lz(sx * M.PIL_X, mid)
                s.ax.add_patch(Ellipse(s.p(cy, cz), M.PIL_D * s.S, M.PIL_D / C45 * s.S,
                                       fc="white", ec="#2a6b2a", lw=1.0))
        for sy in (-1, 1):
            s.ax.add_patch(Ellipse(s.p(sy * M.BAR_Y, lz(-95, mid)), M.BAR_D * s.S,
                                   M.BAR_D / C45 * s.S, fc="white", ec="#2a6b2a", lw=1.0))
        for sy in (-1, 1):
            for sz in (-1, 1):
                c = s.p(sy * M.M3_Y, sz * M.M3_Z)
                s.ax.add_patch(Ellipse(c, M.M3 * s.S, M.M3 * s.S, fc="white", ec="#c00", lw=0.9))
                if kind == "back":
                    s.ax.add_patch(Ellipse(c, M.M3NUT * s.S, M.M3NUT * s.S, fc="none",
                                           ec="#c00", lw=0.6, ls=(0, (2, 2))))
                    s.ax.add_patch(Ellipse(c, M.PAD_D * s.S, M.PAD_D * s.S, fc="none",
                                           ec="#080", lw=0.6, ls=(0, (1, 2))))
        return s

    # back ring, centred at (72, 162) in a 90 x 103 box
    ring((72, 162), "back")
    s.at((0, 0), (72, 162))
    s.ptext(20, 232, "BACK RING", h=3.0, ha="left", weight="bold")
    s.dim((-M.OUT_Y, M.BACK_Z1), (M.OUT_Y, M.BACK_Z1), "270", off=9, h=2.2)
    s.dim((-M.OUT_Y, M.BACK_Z0), (-M.OUT_Y, M.BACK_Z1), "310", off=-9, h=2.2)
    s.dim((-M.OPEN_BY, -M.OPEN_BZ), (M.OPEN_BY, -M.OPEN_BZ), "200 opening", off=9, h=2.1)
    s.dim((0, -M.OPEN_BZ), (0, M.OPEN_BZ), "238", off=30, h=2.1)
    s.dim((-M.PIL_Y, M.BACK_Z1 - 60), (M.PIL_Y, M.BACK_Z1 - 60), "212 pillar pitch",
          off=-11, h=2.1)
    s.leader((M.PIL_Y, lz(M.PIL_X, M.MIDB)),
             "Ø6.8 pillar bore, world-vertical → 6.8 × 9.6 ellipse\n"
             "on the face; set screw enters the back face", (20, 96))
    # lip ring
    ring((182, 162), "front")
    s.at((0, 0), (182, 162))
    s.ptext(137, 232, "FRONT LIP RING", h=3.0, ha="left", weight="bold")
    s.dim((-M.OUT_Y, M.LIP_Z1), (M.OUT_Y, M.LIP_Z1), "270", off=9, h=2.2)
    s.dim((M.OUT_Y, M.LIP_Z0), (M.OUT_Y, M.LIP_Z1), "308", off=9, h=2.2)
    s.leader((0, M.OPEN_FZ), "190 × 230 opening: the lip touches the sheet only\n"
                             "in the 15 mm margin outside the light", (137, 96))
    s.leader((0, -M.OPEN_FZ), "M3 clearance at Y ±130, Z ±70;\npads Ø9 on the back ring", (137, 80))
    s.heading(2, "the two printed rings in true shape at 1:3, dimensioned for the print")
    s.titleblock(2)
    s.column(250, 250, [
        "HOLE TABLE                          BACK / LIP",
        "4 × Ø6.8   pillar bores, world-vertical (45° to the face)      6.8 / 6.8",
        "2 × Ø6.8   screen-mount bars along X, at Z 505                  6.8 / 6.8",
        "4 × Ø3.4   M3 clamp screws at Y ±130, Z ±70                     3.4 / 3.4",
        "4 × Ø6.2   hex pockets (AF 5.5), 3 deep, back face            nut / none",
        "4 × Ø4.5   M4 set screws + hex pocket, back face               4.5 / none",
        "4 × Ø9     hard-stop pads, 3.30 long, on the screw axes       none / none",
        "",
        "The rings share one 270-wide outline; the lip ring is the shorter of",
        "the two and its opening is 10 mm wider in Y and 8 mm shorter in Z, so",
        "that it overlaps the sheet by 15 mm all round without shadowing the",
        "lit 203 × 160 footprint.",
        "Ellipses read 1.414 × the rod diameter: the rods cross the face at 45°.",
    ])
    return s


# ================================================================== sheet 3
def sheet3():
    s = Sheet("DETAILS", "2:1", 2.0, "pillar bore and set screw · clamp stack")

    # ---- A: the pillar bore and its set screw, world section (ring at 45°)
    zb = lz(M.PIL_X, M.MIDB)
    band = rect(M.BX1, -M.GAP, zb - 26, zb + 26)
    zlo = min(q[1] for q in band)
    zhi = max(q[1] for q in band)
    s.at(world(M.MIDB, zb), (110, 206))
    s.poly(band, **HATCH)
    for dx in (-3.4, 3.4):
        s.line([(M.PIL_X + dx, zlo), (M.PIL_X + dx, zhi)], color="#2a6b2a", lw=1.2)
    s.line([world(M.BX1 - 1.5, zb), world(M.BX1 + M.BACK_T / 2 + 1.5, zb)], color="#c00", lw=1.0)
    for sgn in (-1, 1):
        s.line([world(M.BX1 - 1.2, zb + sgn * 3.1), world(-M.GAP, zb + sgn * 3.1)],
               color="#c00", lw=0.8, ls=(0, (2, 2)))
    s.dim((M.PIL_X - 3.4, zhi + 12), (M.PIL_X + 3.4, zhi + 12), "Ø6.8 through", off=8, h=2.1)
    s.dim(world(M.BX1, zb - 36), world(-M.GAP, zb - 36), "8", off=-9, h=2.1)
    s.dim((M.PIL_X - 3.1, zlo - 14), (M.PIL_X + 3.1, zlo - 14), "Ø6.2 hex × 3", off=-8, h=2.1)
    s.ptext(20, 262, "A — PILLAR BORE AND SET SCREW    (section on the pillar's axis)", h=2.7,
            ha="left", weight="bold")
    s.leader(world(-M.GAP - 1, zb + 10), "M4 × 12 set screw in a Ø4.5 hole;\n"
                                         "pocket Ø6.2 hex (AF 5.5) × 3 deep", (152, 212))

    # ---- B: the clamp stack, axis-aligned in the local frame
    s.ptext(20, 166, "B — CLAMP STACK    (section along the mirror's normal)", h=2.7,
            ha="left", weight="bold")
    for cx, tag in ((75, "on the screw axis, Y ±130"), (175, "through the sheet, |Y| < 125")):
        s.at((0, 0), (cx, 100))
        s.poly(lrect(M.BX1, -M.GAP, -20, 20), **HATCH)
        s.poly(lrect(M.FX0, M.FX0 + M.LIP_T, -20, 20), **HATCH)
        s.dim((M.BX1, -26), (-M.GAP, -26), "8", off=8, h=2.0)
        s.dim((M.FX0, -26), (M.FX0 + M.LIP_T, -26), "6", off=8, h=2.0)
        s.ptext(cx, 148, tag, h=2.2, ha="center")
    s.at((0, 0), (75, 100))
    s.poly(lrect(-M.GAP, M.MIR_T + M.GAP, -4.5, 4.5), fc="#eaf7ea", ec="#080", lw=0.9)
    s.ptext(75, 30, "pad Ø9 × 3.30 — sets the groove", h=2.0, ha="center", color="#080")
    s.ptext(75, 24, "and takes the screw's load", h=2.0, ha="center", color="#080")
    s.at((0, 0), (175, 100))
    s.poly(lrect(0, M.MIR_T, -20, 20), fc="#bfe0ff", ec="#036", lw=1.2)
    s.dim((0, -26), (M.MIR_T, -26), "3.00 acrylic", off=8, h=2.0)
    s.ptext(175, 30, ".15 each side → 3.30 groove", h=2.0, ha="center", color="#036")
    s.heading(3, "details at 2:1, and the notes that go with the print")
    s.titleblock(3)
    s.column(250, 250, [
        "ASSEMBLY NOTES",
        "8 × M3 × 16 + nut        clamp the rings; the pad sets the 3.30 groove",
        "4 × M4 × 12 set screw    grip the pillars once the 45° is set",
        "1 × mirrored acrylic 250 × 250 × 3, coating on the back face",
        "4 × Ø6.3 × 800 aluminium pillars (1/4\")",
        "2 × Ø6.3 × 400 bars — galvanised steel, not aluminium: over 370 mm",
        "     of cantilever aluminium sags 7.6 mm, steel 2.7 mm.  For the",
        "     10 mm aluminium tube instead, open the bar bores to Ø10.4.",
        "Fence under the sheet's lower edge: 8.5 high, 5.5 proud, biting 2 mm",
        "into the back ring, clearing the acrylic by 0.5.",
        "Print each ring flat, in the mirror's plane, so the bed holds it.  The",
        "pillar bores then run 45° to the layers: fine at Ø6.8, but expect to",
        "ream them.",
        "Light clearance verified: both rings miss both beam envelopes.",
    ])
    return s


def stl_size(path):
    with open(path, "rb") as f:
        f.read(80)
        n = struct.unpack("<I", f.read(4))[0]
        v = np.array([struct.unpack("<12f", f.read(50)[:48]) for _ in range(n)])
    pts = v[:, 3:].reshape(-1, 3)
    return pts.min(axis=0), pts.max(axis=0)


def drawn_world_bbox(sheet):
    """map the hatched ring polygons of sheet 1 back to world mm, using the
    origin the main view was placed with (the sheet re-uses it for the inset)"""
    ox, oy = sheet.main
    xs, zs = [], []
    for p in sheet.ax.patches:
        if not getattr(p, "get_hatch", None) or not p.get_hatch():
            continue
        for x, y in p.get_xy():
            xs.append((x - ox) / sheet.S)
            zs.append((y - oy) / sheet.S)
    return (min(xs), max(xs)), (min(zs), max(zs))


if __name__ == "__main__":
    pages = [sheet1(), sheet2(), sheet3()]
    for i, pg in enumerate(pages, 1):          # check while the canvas is still Agg
        bad, _ = pg.check(i)
        print(f"sheet {i}: {pg.title}: " +
              ("no overlapping labels" if not bad else f"{len(bad)} OVERLAPS"))
        for a, b in bad:
            print(f"    '{a}'  x  '{b}'")
    with PdfPages(HERE / "reflector-mount-dims.pdf") as pdf:
        for i, pg in enumerate(pages, 1):
            pg.save(pdf, png=HERE / f"sheet{i}.png")
    for name in ("back", "front"):
        f = HERE / f"{name}.stl"
        if f.exists():
            lo, hi = stl_size(f)
            print(f"tripwire {name}.stl: width {hi[1]-lo[1]:7.2f} (drawn 270.00)  "
                  f"X {hi[0]-lo[0]:7.2f}  Z {hi[2]-lo[2]:7.2f}")
    lo, hi = stl_size(HERE / "back.stl")
    (dx0, dx1), (dz0, dz1) = drawn_world_bbox(pages[0])
    print(f"tripwire sheet 1 drawn section: X {dx0:7.1f}..{dx1:7.1f}  (mesh {lo[0]:7.1f}..{hi[0]:7.1f})"
          f"   Z {dz0:7.1f}..{dz1:7.1f}  (mesh {lo[2]:7.1f}..{hi[2]:7.1f})")
    n_ell = sum(1 for p in pages[1].ax.patches if type(p).__name__ == "Ellipse")
    print(f"tripwire sheet 2: {n_ell} ellipses drawn of 28 expected")

    # every edge of the hatched ring section must lie at 45 deg in world space,
    # and the pillar bore must come out vertical — the two things a frame bug
    # would break while still looking plausible
    def angles(pts):
        pts = [tuple(float(v) for v in q) for q in pts]
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        return sorted({round(math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 180, 1)
                       for p, q in zip(pts, pts[1:] + pts[:1])})

    seen = set()
    for p in pages[0].ax.patches:
        if getattr(p, "get_hatch", None) and p.get_hatch():
            seen |= set(angles(list(p.get_xy())))
    print(f"tripwire sheet 1: hatched section edge angles {sorted(seen)} (expect 45 and 135)")
    vert = [(round(x0, 2), round(x1, 2)) for ln in pages[2].ax.lines
            for x0, x1 in [(ln.get_xdata()[0], ln.get_xdata()[-1])] if x0 == x1]
    print(f"tripwire sheet 3: {len(vert)} vertical lines drawn, e.g. x = {vert[:2]}")
    print("wrote reflector-mount-dims.pdf and sheet1-3.png")
