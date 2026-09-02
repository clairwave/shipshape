"""Raw generated mesh -> scene-ready GLB, identical for every candidate.

gltfpack (meshoptimizer) does the heavy lifting: simplify toward the tri
budget, quantize attributes, KTX2/BasisU-compress textures at 512px.

Usage: python scripts/postprocess.py raw.glb final.glb [--ratio 0.05]
Requires gltfpack on PATH (npm i -g gltfpack) with KTX2 support (--tc).
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# native build (BasisU/KTX2 support) preferred; npm build lacks -tc
_NATIVE = Path(__file__).resolve().parents[1] / "tools" / "gltfpack.exe"
GLTFPACK = os.environ.get("GLTFPACK") or (
    str(_NATIVE) if _NATIVE.exists() else
    shutil.which("gltfpack") or shutil.which("gltfpack.cmd") or "gltfpack")


def tri_count(glb: Path) -> int:
    """Sum triangle counts from the glTF JSON chunk (indexed primitives)."""
    data = glb.read_bytes()
    if data[:4] != b"glTF":
        return -1
    length = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + length])
    accessors = doc.get("accessors", [])
    tris = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if "indices" in prim:
                tris += accessors[prim["indices"]]["count"] // 3
            else:
                pos = prim.get("attributes", {}).get("POSITION")
                if pos is not None:
                    tris += accessors[pos]["count"] // 3
    return tris


def postprocess(raw: Path, final: Path, ratio: float = 0.25) -> dict:
    final.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [GLTFPACK, "-i", str(raw), "-o", str(final),
         "-si", str(ratio),          # simplify to ~ratio of source tris
         "-tc",                       # KTX2/BasisU textures
         "-tl", "512",                # texture size cap
         "-cc",                       # meshopt compression
         ], check=True, capture_output=True, text=True)
    return {
        "raw_bytes": raw.stat().st_size,
        "final_bytes": final.stat().st_size,
        "raw_tris": tri_count(raw),
        "final_tris": tri_count(final),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", type=Path)
    ap.add_argument("final", type=Path)
    ap.add_argument("--ratio", type=float, default=0.25)
    args = ap.parse_args()
    stats = postprocess(args.raw, args.final, args.ratio)
    print(json.dumps(stats, indent=2))
    if stats["final_tris"] > 10_000:
        print(f"warning: over tri budget ({stats['final_tris']})",
              file=sys.stderr)
