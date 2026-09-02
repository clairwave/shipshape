"""Generate archetype variant GLBs from assets/archetypes/<folder>/ photos.

Each photo -> preprocess -> production chain -> out/archetypes/<folder>/
<stem>.glb, then sync the lot to cw1's fleet store. Resumable.

Run: python scripts/gen_archetypes.py
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from preprocess import preprocess  # noqa: E402
from produce import produce_one  # noqa: E402
from ship3d.config import COMFYUI_URL  # noqa: E402
from ship3d.executor.local import ComfyHTTPExecutor  # noqa: E402

SRC = ROOT / "assets" / "archetypes"
OUT = ROOT / "out" / "archetypes"
REMOTE = os.environ.get("SHIP3D_SSH_REMOTE", "jon@cw1")
REMOTE_DIR = os.environ.get(
    "SHIP3D_STORE", "/data/disks/media/clairwave-models/ships") + "/archetypes"


async def main():
    executor = ComfyHTTPExecutor(COMFYUI_URL, out_dir=str(ROOT / "out"))
    photos = [p for d in sorted(SRC.iterdir()) if d.is_dir()
              for p in sorted(d.glob("*"))
              if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    print(f"=== {len(photos)} archetype variants queued ===", flush=True)
    done = 0
    for photo in photos:
        folder = photo.parent.name
        outdir = OUT / folder
        outdir.mkdir(parents=True, exist_ok=True)
        pre_dir = outdir / "pre"
        pre_dir.mkdir(exist_ok=True)
        try:
            if not (outdir / f"{photo.stem}.glb").exists():
                pre = (pre_dir / f"{photo.stem}.png")
                if not pre.exists():
                    pre = preprocess(photo, pre_dir)
                row = await produce_one(executor, pre, outdir)
                print(json.dumps({"folder": folder, **row}), flush=True)
            done += 1
        except Exception as e:
            print(f"[fail] {folder}/{photo.stem}: {e}", flush=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE,
                    f"mkdir -p {REMOTE_DIR}"], capture_output=True)
    for d in sorted(OUT.iterdir()):
        if not d.is_dir():
            continue
        glbs = [str(p) for p in d.glob("*.glb") if not p.stem.endswith("_raw")]
        if not glbs:
            continue
        subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE,
                        f"mkdir -p {REMOTE_DIR}/{d.name}"],
                       capture_output=True)
        subprocess.run(["scp", "-o", "BatchMode=yes", *glbs,
                        f"{REMOTE}:{REMOTE_DIR}/{d.name}/"],
                       check=True, capture_output=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE,
                    f"chmod -R a+rX {REMOTE_DIR}"], capture_output=True)
    print(f"=== ARCHETYPES DONE: {done}/{len(photos)} synced to cw1 ===",
          flush=True)


asyncio.run(main())
