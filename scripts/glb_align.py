"""GLB orientation: standardized ship alignment (bow = +Z, up = +Y).

Rotation is applied by injecting/updating a single root wrapper node in the
glTF JSON chunk — geometry, textures, and compression stay untouched, so it
is safe on gltfpack output. Cumulative yaw is tracked in the wrapper's
extras, so repeated adjustments compose instead of nesting.
"""
import json
import math
import struct
from pathlib import Path

ALIGN_NODE = "shipshape_align"


def _read(path):
    data = Path(path).read_bytes()
    if data[:4] != b"glTF":
        raise ValueError("not a glb")
    jlen = struct.unpack_from("<I", data, 12)[0]
    if data[16:20] != b"JSON":
        raise ValueError("unexpected first chunk")
    doc = json.loads(data[20:20 + jlen])
    return doc, data[20 + jlen:]  # rest = BIN chunk(s) verbatim


def _write(path, doc, rest):
    j = json.dumps(doc, separators=(",", ":")).encode()
    j += b" " * (-len(j) % 4)
    total = 12 + 8 + len(j) + len(rest)
    Path(path).write_bytes(
        b"glTF" + struct.pack("<II", 2, total)
        + struct.pack("<I", len(j)) + b"JSON" + j + rest)


def _set_yaw(doc, total_deg):
    half = math.radians(total_deg) / 2
    quat = [0.0, round(math.sin(half), 6), 0.0, round(math.cos(half), 6)]
    scene = doc["scenes"][doc.get("scene", 0)]
    nodes = doc.setdefault("nodes", [])
    roots = scene["nodes"]
    if len(roots) == 1 and nodes[roots[0]].get("name") == ALIGN_NODE:
        w = nodes[roots[0]]
        w["rotation"] = quat
        w["extras"] = {"yaw_deg": total_deg % 360}
    else:
        nodes.append({"name": ALIGN_NODE, "rotation": quat,
                      "children": roots,
                      "extras": {"yaw_deg": total_deg % 360}})
        scene["nodes"] = [len(nodes) - 1]


def current_yaw(doc) -> float:
    scene = doc["scenes"][doc.get("scene", 0)]
    roots = scene["nodes"]
    nodes = doc.get("nodes", [])
    if len(roots) == 1 and nodes[roots[0]].get("name") == ALIGN_NODE:
        return float(nodes[roots[0]].get("extras", {}).get("yaw_deg", 0))
    return 0.0


def apply_yaw(path, delta_deg) -> float:
    """Add delta_deg (about +Y) to the model's alignment. Returns new total."""
    doc, rest = _read(path)
    total = (current_yaw(doc) + delta_deg) % 360
    _set_yaw(doc, total)
    _write(path, doc, rest)
    return total


def _extents(doc):
    mn, mx = [1e18] * 3, [-1e18] * 3
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            pos = prim.get("attributes", {}).get("POSITION")
            if pos is None:
                continue
            acc = doc["accessors"][pos]
            if "min" not in acc or "max" not in acc:
                continue
            for i in range(3):
                mn[i] = min(mn[i], acc["min"][i])
                mx[i] = max(mx[i], acc["max"][i])
    return [mx[i] - mn[i] for i in range(3)]


def auto_align(path) -> float:
    """Put the longest horizontal axis on Z (hull line). Bow-vs-stern (180)
    stays an ops decision. Idempotent. Returns yaw applied (0 or 90)."""
    doc, rest = _read(path)
    if current_yaw(doc) != 0.0:
        return 0.0  # already aligned (auto or ops) — never fight ops
    ex = _extents(doc)
    if ex[0] > ex[2] * 1.15:  # clearly X-long -> rotate onto Z
        _set_yaw(doc, 90.0)
        _write(path, doc, rest)
        return 90.0
    return 0.0


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(p, "yaw->", auto_align(p))
