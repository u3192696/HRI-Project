#!/usr/bin/env python3
"""Orin rig — reflector mount, as real solids for FreeCAD.

Port of cad/reflector-mount/reflector-mount.scad, same parameters, so the two
must agree.  Exports STEP (FreeCAD-native) plus STL for slicing, and prints
volumes so they can be checked against the OpenSCAD meshes.

    cd ~/Projects/hri-project && uv run python cad/reflector-mount/reflector-mount.py
"""
import math
from build123d import (Align, Box, Compound, Cylinder, Pos, RegularPolygon, Rot,
                        export_step, export_stl, extrude)

# ---- optics ----------------------------------------------------------------
MIR, MIR_T = 250.0, 3.0
LENS_MIR, UP_SLOPE = 600.0, 11.9

# ---- pillars: 6.3 mm solid aluminium (1/4") ---------------------------------
PIL_D, PIL_X, PIL_Y = 6.8, 100.0, 106.0

# ---- screen-mount bars: 6.3 mm rod, under the mirror ------------------------
BAR_D, BAR_Y, BAR_Z = 6.8, 120.0, 505.0

# ---- rings -----------------------------------------------------------------
OUT_Y, BACK_T, LIP_T, GAP = 135.0, 8.0, 6.0, 0.15
PAD_D = 9.0                           # hard-stop pad at each clamp screw
BACK_Z0, BACK_Z1 = -160.0, 150.0
LIP_Z0, LIP_Z1 = -172.0, 136.0
OPEN_BY, OPEN_BZ = 100.0, 119.0
OPEN_FY, OPEN_FZ = 95.0, 115.0

# ---- features ---------------------------------------------------------------
FENCE_T, FENCE_H = 5.5, 8.5
M4, M4NUT, M3, M3NUT, NUT_DEEP = 4.5, 6.2, 3.4, 6.2, 3.0
M3_Y, M3_Z = 130.0, 70.0

C45 = math.sqrt(0.5)
CMX, CMZ = UP_SLOPE * C45, LENS_MIR + UP_SLOPE * C45
BX0 = 0.0                             # the coated optical plane (2nd surface)
BX1 = -(GAP + BACK_T)                 # back ring's outer face
MIDB = -GAP - BACK_T / 2              # back ring mid-plane
FX0 = MIR_T + GAP                     # lip ring's inner face
MIDF = FX0 + LIP_T / 2

LW = Pos(CMX, 0, CMZ) * Rot(Y=45)     # local -> world (45 deg mirror frame)
MIN = (Align.MIN, Align.MIN, Align.MIN)
CEN = (Align.CENTER, Align.CENTER, Align.CENTER)
CENMIN = (Align.CENTER, Align.CENTER, Align.MIN)


def lz(px, midx):
    """local Z where a world-vertical line at X=px crosses a plane at local X=midx"""
    return 1.4142135 * (px - CMX) - midx


def hex_prism(across_corners, depth):
    """hexagonal pocket prism along +Z, flat side... circumradius = across_corners/2"""
    return extrude(RegularPolygon(across_corners / 2, 6), amount=depth)


def back_ring():
    p = LW * Pos(BX1, -OUT_Y, BACK_Z0) * Box(BACK_T, 2 * OUT_Y, BACK_Z1 - BACK_Z0, align=MIN)
    p += LW * Pos(BX0 - 2, -OUT_Y, -134) * Box(MIR_T + GAP + FENCE_T + 2, 2 * OUT_Y, FENCE_H, align=MIN)
    p -= LW * Pos(BX1 - 1, -OPEN_BY, -OPEN_BZ) * Box(BACK_T + 2, 2 * OPEN_BY, 2 * OPEN_BZ, align=MIN)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p -= Pos(sx * PIL_X, sy * PIL_Y, 200) * Cylinder(PIL_D / 2, 1600, align=CEN)
            anchor = LW * Pos(MIDB, sy * PIL_Y, lz(sx * PIL_X, MIDB))
            p -= anchor * Pos(BX1 - 0.2, 0, 0) * Rot(Y=90) * Cylinder(M4 / 2, BACK_T / 2 + 3, align=CENMIN)
            p -= anchor * Pos(BX1 - 0.2, 0, 0) * Rot(Y=90) * hex_prism(M4NUT, NUT_DEEP)
    for sy in (-1, 1):
        p -= Pos(-260, sy * BAR_Y, BAR_Z) * Rot(Y=90) * Cylinder(BAR_D / 2, 700, align=CENMIN)
    for sy in (-1, 1):
        for sz in (-1, 1):
            pad = (LW * Pos(-GAP - 1, sy * M3_Y, sz * M3_Z) * Rot(Y=90)
                   * Cylinder(PAD_D / 2, MIR_T + 2 * GAP + 1, align=CENMIN))
            p += pad
    for sy in (-1, 1):
        for sz in (-1, 1):
            p -= LW * Pos(MIDB, sy * M3_Y, sz * M3_Z) * Rot(Y=90) * Cylinder(M3 / 2, BACK_T + 6, align=CEN)
            p -= LW * Pos(BX1 - 0.2, sy * M3_Y, sz * M3_Z) * Rot(Y=90) * hex_prism(M3NUT, NUT_DEEP)
    return p


def front_ring():
    p = LW * Pos(FX0, -OUT_Y, LIP_Z0) * Box(LIP_T, 2 * OUT_Y, LIP_Z1 - LIP_Z0, align=MIN)
    p -= LW * Pos(FX0 - 1, -OPEN_FY, -OPEN_FZ) * Box(LIP_T + 2, 2 * OPEN_FY, 2 * OPEN_FZ, align=MIN)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p -= Pos(sx * PIL_X, sy * PIL_Y, 200) * Cylinder((PIL_D + 0.4) / 2, 1600, align=CEN)
    for sy in (-1, 1):
        p -= Pos(-260, sy * BAR_Y, BAR_Z) * Rot(Y=90) * Cylinder((BAR_D + 0.4) / 2, 700, align=CENMIN)
    for sy in (-1, 1):
        for sz in (-1, 1):
            p -= LW * Pos(MIDF, sy * M3_Y, sz * M3_Z) * Rot(Y=90) * Cylinder(M3 / 2, LIP_T + 8, align=CEN)
    return p


def acrylic():
    return LW * Pos(0, -MIR / 2, -MIR / 2) * Box(MIR_T, MIR, MIR, align=MIN)


def props():
    out = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            out.append(Pos(sx * PIL_X, sy * PIL_Y, -60) * Cylinder(6.3 / 2, 830, align=CENMIN))
    for sy in (-1, 1):
        out.append(Pos(-150, sy * BAR_Y, BAR_Z) * Rot(Y=90) * Cylinder(6.3 / 2, 430, align=CENMIN))
    frame = (Pos(250, -130, 470) * Box(15, 260, 260, align=MIN)
             - Pos(248, -115, 485) * Box(20, 230, 230, align=MIN))
    out.append(frame)
    out.append(Pos(-70, -75, -160) * Box(140, 150, 100, align=MIN))
    out.append(Pos(-150, -150, -170) * Box(300, 300, 10, align=MIN))
    return out


if __name__ == "__main__":
    back, front, glass = back_ring(), front_ring(), acrylic()
    context = props()

    for name, part, ref in [("back ring", back, 307.65), ("lip ring", front, 234.47),
                            ("acrylic", glass, 187.50)]:
        v = part.volume / 1000.0
        print(f"{name:10} {v:8.2f} cm3   (OpenSCAD mesh {ref:7.2f} cm3, "
              f"diff {100*(v-ref)/ref:+5.2f} %)   bbox {part.bounding_box().size}")

    for name, part in [("back", back), ("front", front), ("acrylic", glass)]:
        export_step(part, f"{name}.step")
        export_stl(part, f"{name}.stl")
    export_step(Compound(children=context), "props.step")
    export_step(Compound(children=[back, front, glass]), "reflector-mount.step")
    export_step(Compound(children=[back, front, glass] + context), "reflector-mount-assembly.step")
    print("wrote back/front/acrylic/props .step (+ .stl) and reflector-mount[-assembly].step")
