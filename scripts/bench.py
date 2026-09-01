"""Candidate shootout: every model x every testdata photo -> bench report.

Each candidate maps to a ComfyUI API workflow in workflows/ (added as its
wrapper gets installed). Identical preprocessing and postprocessing so the
comparison isolates the generator.

Usage: python scripts/bench.py [--models hunyuan2 sf3d] [--comfy URL]
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from postprocess import postprocess, tri_count  # noqa: E402
from ship3d.config import COMFYUI_URL  # noqa: E402
from ship3d.executor.base import JobSpec  # noqa: E402
from ship3d.executor.local import ComfyHTTPExecutor  # noqa: E402

# model key -> (workflow json, node-title input overrides)
CANDIDATES = {
    "hunyuan2": "hunyuan3d_2.json",
    "hunyuan21": "hunyuan3d_2_1.json",
    "trellis": "trellis.json",
}


async def run_model(executor, model: str, workflow: str, img: Path,
                    outdir: Path) -> dict:
    t0 = time.time()
    spec = JobSpec(kind="mesh", workflow=workflow, seed=42,
                   artifacts_in={"image": str(img)},
                   inputs={"ShipImage.image": "@image"})
    result = await executor.run(spec)
    gen_s = time.time() - t0
    raw = next((p for p in result.artifacts.values()
                if str(p).endswith((".glb", ".gltf"))), None)
    if raw is None:
        # mesh exports don't always surface in /history outputs — take the
        # newest glb the server wrote since this job started
        comfy_out = ROOT / "ComfyUI3D" / "output"
        candidates = [p for p in comfy_out.rglob("*.glb")
                      if p.stat().st_mtime >= t0]
        if not candidates:
            raise RuntimeError("no glb produced")
        raw = max(candidates, key=lambda p: p.stat().st_mtime)
    raw_path = outdir / f"{img.stem}_raw.glb"
    Path(raw).replace(raw_path)
    stats = postprocess(raw_path, outdir / f"{img.stem}.glb")
    return {"model": model, "ship": img.stem, "gen_s": round(gen_s, 1),
            **stats}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=list(CANDIDATES),
                    choices=list(CANDIDATES))
    ap.add_argument("--comfy", default=COMFYUI_URL)
    args = ap.parse_args()

    images = sorted((ROOT / "testdata").glob("*.png")) + \
        sorted((ROOT / "testdata").glob("*.jpg"))
    if not images:
        sys.exit("no test images in testdata/ — drop AIS vessel photos there")

    executor = ComfyHTTPExecutor(args.comfy, out_dir=str(ROOT / "out"))
    rows = []
    for model in args.models:
        wf = ROOT / "workflows" / CANDIDATES[model]
        if not wf.exists():
            print(f"[skip] {model}: workflows/{CANDIDATES[model]} not yet "
                  f"authored", flush=True)
            continue
        outdir = ROOT / "out" / "bench" / model
        outdir.mkdir(parents=True, exist_ok=True)
        for img in images:
            if (outdir / f"{img.stem}.glb").exists():
                print(f"[skip] {model}/{img.stem}: already done", flush=True)
                continue
            try:
                row = await run_model(executor, model, CANDIDATES[model],
                                      img, outdir)
                rows.append(row)
                print(json.dumps(row), flush=True)
            except Exception as e:
                print(f"[fail] {model}/{img.stem}: {e}", flush=True)

    report = ROOT / "out" / "bench" / "results.jsonl"
    with report.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"=== BENCH DONE: {len(rows)} rows appended -> {report} ===")


asyncio.run(main())
