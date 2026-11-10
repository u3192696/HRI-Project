#!/usr/bin/env python3
"""Measure binary STLs: bounding box, volume (divergence theorem), triangle count.

    uv run --no-project python stl_report.py a.stl b.stl ...
"""
import struct, sys, pathlib


def read_stl(path):
    data = pathlib.Path(path).read_bytes()
    if data[:5] == b"solid" and b"facet" in data[:200]:
        return None                      # ASCII STL -- not expected here
    n = struct.unpack("<I", data[80:84])[0]
    tris = []
    off = 84
    for _ in range(n):
        vals = struct.unpack("<12fH", data[off:off + 50])
        tris.append(vals[3:12])
        off += 50
    return tris


def measure(path):
    tris = read_stl(path)
    if not tris:
        return None
    lo = [min(t[i] for t in tris) for i in (0, 1, 2)]
    hi = [max(t[i] for t in tris) for i in (0, 1, 2)]
    vol = 0.0
    for t in tris:
        ax, ay, az, bx, by, bz, cx, cy, cz = t
        vol += (ax * (by * cz - bz * cy)
                - ay * (bx * cz - bz * cx)
                + az * (bx * cy - by * cx)) / 6.0
    return dict(n=len(tris), lo=lo, hi=hi, vol=abs(vol))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        m = measure(p)
        name = pathlib.Path(p).name
        if m is None:
            print(f"{name:22} EMPTY (no triangles)")
            continue
        dx = m["hi"][0] - m["lo"][0]
        dy = m["hi"][1] - m["lo"][1]
        dz = m["hi"][2] - m["lo"][2]
        print(f"{name:22} {m['n']:6d} tris  "
              f"size {dx:6.1f} x {dy:6.1f} x {dz:6.1f}  "
              f"bbox [{m['lo'][0]:7.1f},{m['lo'][1]:7.1f},{m['lo'][2]:6.1f}] .. "
              f"[{m['hi'][0]:7.1f},{m['hi'][1]:7.1f},{m['hi'][2]:6.1f}]  "
              f"vol {m['vol']/1000.0:8.2f} cm3  ({m['vol']*1.27/1000.0:6.0f} g PETG)")
