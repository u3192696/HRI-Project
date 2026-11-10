// =============================================================================
//  Orin rig  —  reflector mount      (back ring + front lip ring, 3 mm acrylic)
//  All dimensions mm.  World frame:   +X  forward (toward the screen)
//                                     +Y  left
//                                     +Z  up,  origin = projector lens
//  Local 45 deg frame:  translate(CM) rotate([0,45,0])
//      +X  out of the acrylic's face (forward and down)
//      +Y  across the mirror
//      +Z  up-slope along the mirror
//
//  Render:  openscad -D 'part="back"'  -o back.stl  reflector-mount.scad
//           openscad -D 'part="front"' -o front.stl reflector-mount.scad
// =============================================================================

part = "assembly";              // back | front | mirror | props | assembly | beams

// ---- optics: do not change without re-deriving the rig ----------------------
MIR       = 250;        // mirrored acrylic, square
MIR_T     = 3;          // acrylic thickness
LENS_MIR  = 600;        // lens -> where the beam axis meets the mirror plane
UP_SLOPE  = 11.9;       // mirror centre sits this far up-slope of that hit point
                        //   -> face footprint 203 x 160 sits central (24 mm margins)

// ---- pillars: Danny's pick, 6.3 mm solid aluminium (1/4") --------------------
PIL_BORE  = 6.8;        // clearance bore
PIL_X     = 100;        // plan position, world X   (> 88 + r + wall: clears the acrylic)
PIL_Y     = 106;        // plan position, world Y

// ---- screen-mount bars: 6.3 mm rod, horizontal, under the mirror -------------
BAR_BORE  = 6.8;
BAR_Y     = 120;
BAR_Z     = 505;        // below the acrylic's lowest point (520) and outside the beam cone

// ---- rings ------------------------------------------------------------------
OUT_Y     = 135;        // ring half-width (world Y)
BACK_T    = 8;          // back ring thickness along the mirror normal
LIP_T     = 6;          // front lip ring thickness
GAP       = 0.15;       // clearance each side of the acrylic -> 3.30 groove
PAD_D     = 9;          // hard-stop pad at each clamp screw (sets the groove)
BACK_Z0   = -160;  BACK_Z1 = 150;     // back ring ends (local Z)
LIP_Z0    = -172;  LIP_Z1  = 136;     // front lip ring ends
OPEN_BY   = 100;  OPEN_BZ = 119;      // back opening: 35 mm side band, 6 mm end band
OPEN_FY   =  95;  OPEN_FZ = 115;      // front opening: 30 mm side lip, 10 mm end lip

// ---- features ---------------------------------------------------------------
FENCE_T   = 5.5;  FENCE_H = 8.5;      // hard stop under the acrylic's lower edge
M4        = 4.5;  M4NUT = 6.2;        // set screw per pillar (grips it, no thread needed)
M3        = 3.4;  M3NUT = 6.2;        // 4 screws clamping the two rings together
M3_Y      = 130;  M3_Z  = 70;
NUT_DEEP  = 3;

C45 = 0.70710678;
CM  = [UP_SLOPE*C45, 0, LENS_MIR + UP_SLOPE*C45];   // optical plane through here
// 2nd-surface acrylic: the coating sits ON the optical plane, the sheet in front of it
BX0  = 0;                           // acrylic's back (coated) face = the optical plane
BX1  = -(GAP + BACK_T);            // back ring's outer face
MIDB = -GAP - BACK_T/2;            // back ring mid-plane
FX0  = MIR_T + GAP;                // front ring's inner face
MIDF = FX0 + LIP_T/2;

// local Z at which a vertical line at world X = px crosses a plane at local X = midx
function lz(px, midx) = 1.4142135*(px - CM[0]) - midx;

$fn = 64;

module local() { translate(CM) rotate([0,45,0]) children(); }

// ------------------------------------------------------------------ the acrylic
module mirror_plate() {
    local() translate([BX0, -MIR/2, -MIR/2]) cube([MIR_T, MIR, MIR]);
}

// ------------------------------------------------------------------ set screw
// one per pillar bore: hex pocket in the back face, M4 hole through to the bore
module set_screw(hole_len, face_x) {
    translate([face_x-0.2, 0, 0]) rotate([0,90,0]) cylinder(h=NUT_DEEP, d=M4NUT, $fn=6);
    translate([face_x-0.2, 0, 0]) rotate([0,90,0]) cylinder(h=hole_len, d=M4);
}

// ------------------------------------------------------------------ back ring
module back_ring() {
    difference() {
        union() {
            local() translate([BX1, -OUT_Y, BACK_Z0])
                cube([BACK_T, 2*OUT_Y, BACK_Z1-BACK_Z0]);
            // fence: catches the acrylic's lower edge if it ever creeps down-slope
            local() translate([BX0 - 2, -OUT_Y, -134])
                cube([MIR_T + GAP + FENCE_T + 2, 2*OUT_Y, FENCE_H]);
            // hard-stop pad at each clamp screw: without it, tightening the M3s
            // would close the 3.3 groove and leave the acrylic loose.  Sunk 1 mm
            // into the ring so the fuse is not face-on-face.
            for (sy=[-1,1], sz=[-1,1])
                local() translate([-GAP - 1, sy*M3_Y, sz*M3_Z]) rotate([0,90,0])
                    cylinder(h = MIR_T + 2*GAP + 1, d = PAD_D);
        }
        // beam window: the whole middle is open (this is the cage, not a box)
        local() translate([BX1-1, -OPEN_BY, -OPEN_BZ])
            cube([BACK_T+2, 2*OPEN_BY, 2*OPEN_BZ]);
        // vertical pillar bores + a set screw each
        for (sx=[-1,1], sy=[-1,1]) {
            translate([sx*PIL_X, sy*PIL_Y, -300]) cylinder(h=1400, d=PIL_BORE);
            local() translate([MIDB, sy*PIL_Y, lz(sx*PIL_X, MIDB)])
                set_screw(BACK_T/2 + 3, BX1);
        }
        // screen-mount bar bores, straight through the lower end band
        for (sy=[-1,1]) translate([-260, sy*BAR_Y, BAR_Z])
            rotate([0,90,0]) cylinder(h=700, d=BAR_BORE);
        // 4 screws that clamp the acrylic between the two rings
        for (sy=[-1,1], sz=[-1,1]) {
            local() translate([MIDB, sy*M3_Y, sz*M3_Z]) rotate([0,90,0])
                cylinder(h=BACK_T+6, d=M3, center=true);
            local() translate([BX1-0.2, sy*M3_Y, sz*M3_Z]) rotate([0,90,0])
                cylinder(h=NUT_DEEP, d=M3NUT, $fn=6);
        }
    }
}

// ------------------------------------------------------------------ front lip ring
module front_ring() {
    difference() {
        local() translate([FX0, -OUT_Y, LIP_Z0])
            cube([LIP_T, 2*OUT_Y, LIP_Z1-LIP_Z0]);
        local() translate([FX0-1, -OPEN_FY, -OPEN_FZ])
            cube([LIP_T+2, 2*OPEN_FY, 2*OPEN_FZ]);
        // pillar and bar clearance
        for (sx=[-1,1], sy=[-1,1])
            translate([sx*PIL_X, sy*PIL_Y, -300]) cylinder(h=1400, d=PIL_BORE+0.4);
        for (sy=[-1,1]) translate([-260, sy*BAR_Y, BAR_Z])
            rotate([0,90,0]) cylinder(h=700, d=BAR_BORE+0.4);
        // screw clearance
        for (sy=[-1,1], sz=[-1,1])
            local() translate([MIDF, sy*M3_Y, sz*M3_Z]) rotate([0,90,0])
                cylinder(h=LIP_T+8, d=M3, center=true);
    }
}

// ------------------------------------------------------------------ props (context only)
module props() {
    // 4 pillars
    for (sx=[-1,1], sy=[-1,1])
        translate([sx*PIL_X, sy*PIL_Y, -60]) cylinder(h=830, d=6.3);
    // 2 screen-mount bars
    for (sy=[-1,1]) translate([-150, sy*BAR_Y, BAR_Z])
        rotate([0,90,0]) cylinder(h=430, d=6.3);
    // screen frame, 260 square, 230 window, bottom rail reaching the bars
    difference() {
        translate([250, -130, 470]) cube([15, 260, 260]);
        translate([248, -115, 485]) cube([20, 230, 230]);
    }
    // projector body, sitting on the floor plate
    translate([-70, -75, -160]) cube([140, 150, 100]);
    translate([-150, -150, -170]) cube([300, 300, 10]);
}

// ------------------------------------------------------------------ beams (clearance proof)
// The face's footprint on the mirror, and the lit content at the screen: the two
// sections bound the incoming and reflected light exactly (all rays share the lens,
// and the reflected ones all point back at the lens's image behind the mirror).
FP  = [[80,80,680], [80,-80,680], [-63.2,-63.2,536.8], [-63.2,63.2,536.8]];
IMG = [[250,100,700], [250,-100,700], [250,-100,500], [250,100,500]];
module dots(pts) { for (p = pts) translate(p) sphere(0.01, $fn=6); }
module beam_in()  { hull() { cylinder(h=0.02, d=0.02, $fn=6); dots(FP); } }
module beam_out() { hull() { dots(FP); dots(IMG); } }

// ------------------------------------------------------------------ interference probes
TARGET = "back";        // back | front | mirror | props
BEAM   = "in";          // in | out
module body(t) {
    if      (t == "back")   back_ring();
    else if (t == "front")  front_ring();
    else if (t == "mirror") mirror_plate();
    else                    props();
}
module beamm(b) { if (b == "in") beam_in(); else beam_out(); }

// ------------------------------------------------------------------ output
if      (part == "probe")    intersection() { body(TARGET); beamm(BEAM); }
else if (part == "back")     back_ring();
else if (part == "front")    front_ring();
else if (part == "mirror")   mirror_plate();
else if (part == "props")    props();
else if (part == "beams")    { beam_in(); beam_out(); }
else { back_ring(); front_ring(); mirror_plate(); props(); }
