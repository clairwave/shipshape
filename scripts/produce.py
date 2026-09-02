"""Production runner: preprocessed ship photo(s) -> textured scene-ready GLB.

2.1 shape (approved) + 2.0 delight/paint texture chain + gltfpack finishing.
Resumable: existing final GLBs are skipped, so crashes/restarts just rerun.

Usage:
    python scripts/produce.py testdata/tanker1.png [more.png ...]
    python scripts/produce.py --all            # every png in testdata/
    python scripts/produce.py img.png -o out/production
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from postprocess import postprocess  # noqa: E402
from shipshape.config import COMFYUI_URL  # noqa: E402
from shipshape.executor.base import JobSpec  # noqa: E402
from shipshape.executor.local import ComfyHTTPExecutor  # noqa: E402

WORKFLOW = "ship_production.json"


async def produce_one(executor, img: Path, outdir: Path) -> dict:
    final = outdir / f"{img.stem}.glb"
    if final.exists():
        return {"ship": img.stem, "skipped": True}
    t0 = time.time()
    spec = JobSpec(kind="mesh", workflow=WORKFLOW, seed=42,
                   artifacts_in={"image": str(img)},
                   inputs={"ShipImage.image": "@image"})
    result = await executor.run(spec)
    raw = next((p for p in result.artifacts.values()
                if str(p).endswith(".glb")), None)
    if raw is None:
        cands = [p for p in Path(os.environ.get("COMFY_OUTPUT_DIR", ROOT / "ComfyUI3D" / "output")).rglob("*.glb")
                 if p.stat().st_mtime >= t0]
        if not cands:
            raise RuntimeError("no glb produced")
        raw = max(cands, key=lambda p: p.stat().st_mtime)
    raw_path = outdir / f"{img.stem}_raw.glb"
    Path(raw).replace(raw_path)
    stats = postprocess(raw_path, final)
    return {"ship": img.stem, "gen_s": round(time.time() - t0, 1), **stats}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-o", "--outdir", type=Path,
                    default=ROOT / "out" / "production")
    ap.add_argument("--comfy", default=COMFYUI_URL)
    args = ap.parse_args()

    images = list(args.images)
    if args.all:
        images += sorted((ROOT / "testdata").glob("*.png"))
    if not images:
        sys.exit("no input images")

    args.outdir.mkdir(parents=True, exist_ok=True)
    executor = ComfyHTTPExecutor(args.comfy, out_dir=str(ROOT / "out"))
    manifest = args.outdir / "manifest.jsonl"
    for img in images:
        try:
            row = await produce_one(executor, img, args.outdir)
        except Exception as e:
            print(f"[fail] {img.stem}: {e}", flush=True)
            continue
        print(json.dumps(row), flush=True)
        if not row.get("skipped"):
            with manifest.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    print("=== PRODUCTION RUN DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
