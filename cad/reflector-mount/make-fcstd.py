"""Import the reflector-mount STEPs into a FreeCAD document (run with freecadcmd).

    freecadcmd make-fcstd.py

Writes reflector-mount.FCStd next to this file, with the four objects named and
coloured, and prints each object's volume so the import is proven to be a solid.
"""
import os
import FreeCAD
import Part

HERE = os.path.dirname(os.path.abspath(__file__))
PARTS = [("back ring", "back.step", (0.80, 0.82, 0.85)),
         ("lip ring", "front.step", (0.90, 0.90, 0.92)),
         ("acrylic 3mm", "acrylic.step", (0.15, 0.45, 0.80)),
         ("props", "props.step", (0.35, 0.65, 0.35))]

doc = FreeCAD.newDocument("reflector-mount")
for label, fn, rgb in PARTS:
    shape = Part.Shape()
    shape.read(os.path.join(HERE, fn))
    obj = doc.addObject("Part::Feature", label.replace(" ", "_"))
    obj.Label = label
    obj.Shape = shape
    if obj.ViewObject:                       # not available in console mode
        obj.ViewObject.ShapeColor = rgb
    print(f"{label:12} {fn:14} solids={len(shape.Solids):2d} faces={len(shape.Faces):4d} "
          f"valid={shape.isValid()} volume={shape.Volume / 1000:8.2f} cm3")

doc.recompute()
out = os.path.join(HERE, "reflector-mount.FCStd")
doc.saveAs(out)
print("saved", out, os.path.getsize(out), "bytes")
