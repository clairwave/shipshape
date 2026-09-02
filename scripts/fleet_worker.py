"""Fleet worker: consumes qc-api tasks on cw0, produces GLBs, ships to cw1.

Loop: claim pending task -> resolve photo (task param URL, reuploaded
photo_new.png, or existing photo.png) -> preprocess -> ship_production
workflow -> gltfpack -> push by_mmsi/<mmsi>/ to cw1 (history-rotating) ->
report completion. Idempotent; safe under the ComfyUI restart loop.

Run: python scripts/fleet_worker.py [--once] [--api http://cw1:8877]
Token: secrets/qc_token
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from postprocess import postprocess  # noqa: E402
from preprocess import preprocess  # noqa: E402
from produce import produce_one  # noqa: E402
from shipshape.config import COMFYUI_URL  # noqa: E402
from shipshape.executor.local import ComfyHTTPExecutor  # noqa: E402

TOKEN = os.environ.get("QC_TOKEN") or \
    (ROOT / "secrets" / "qc_token").read_text().strip()
REMOTE = os.environ.get("SHIPSHAPE_SSH_REMOTE", "jon@cw1")
STORE = os.environ.get(
    "SHIPSHAPE_STORE", "/data/disks/media/clairwave-models/ships") + "/by_mmsi"
PIPELINE_TAG = "ship_production.json"


def ssh(cmd: str):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE, cmd],
                          capture_output=True, text=True)


def api(method, path, base, **kw):
    kw.setdefault("headers", {})["X-QC-Token"] = TOKEN
    r = requests.request(method, base + path, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def resolve_photo(task, base, workdir: Path, http=False) -> Path | None:
    """Priority: params.photo_url > reuploaded photo_new.png > photo.png."""
    mmsi = task["mmsi"]
    params = json.loads(task.get("params") or "{}")
    raw = workdir / f"{mmsi}_src.png"
    url = params.get("photo_url")
    if url:
        raw.write_bytes(requests.get(url, timeout=120).content)
        return raw
    if http:
        r = requests.get(f"{base}/models/{mmsi}/photo.png",
                         headers={"X-QC-Token": TOKEN}, timeout=60)
        if r.status_code == 200:
            raw.write_bytes(r.content)
            return raw
        return None
    for fname in ("photo_new.png", "photo.png"):
        r = subprocess.run(["scp", "-o", "BatchMode=yes",
                            f"{REMOTE}:{STORE}/{mmsi}/{fname}", str(raw)],
                           capture_output=True)
        if r.returncode == 0:
            return raw
    return None


async def handle(task, base, executor, http_push=False) -> str:
    mmsi = task["mmsi"]
    action = task["action"]
    if action in ("needs_photo", "use_archetype"):
        return f"{action}: no-op for worker (archetype assignment is serve-time)"
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        src = resolve_photo(task, base, workdir, http=http_push)
        if src is None:
            raise RuntimeError("no photo available; needs_photo")
        pre = preprocess(src, workdir)
        pre = pre.rename(workdir / f"{mmsi}.png")
        outdir = workdir / "out"
        outdir.mkdir()
        row = await produce_one(executor, pre, outdir)
        meta = {
            "mmsi": mmsi, "kind": "unique", "status": "auto",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pipeline": PIPELINE_TAG, "task_id": task["id"], **row,
        }
        (outdir / "meta.json").write_text(json.dumps(meta, indent=1))
        if http_push:
            # community mode: no ssh access — upload result over the api
            with open(outdir / f"{mmsi}.glb", "rb") as fm, \
                    open(pre, "rb") as fp, \
                    open(outdir / "meta.json", "rb") as fj:
                r = requests.post(
                    f"{base}/api/tasks/{task['id']}/result",
                    headers={"X-QC-Token": TOKEN},
                    files={"model": fm, "photo": fp, "meta": fj}, timeout=300)
            r.raise_for_status()
            return json.dumps(row)
        dest = f"{STORE}/{mmsi}"
        ssh(f"mkdir -p {dest}/history && "
            f"[ -f {dest}/model.glb ] && "
            f"mv {dest}/model.glb {dest}/history/$(date +%s).glb || true")
        for local, name in [(outdir / f"{mmsi}.glb", "model.glb"),
                            (pre, "photo.png"),
                            (outdir / "meta.json", "meta.json")]:
            subprocess.run(["scp", "-o", "BatchMode=yes", str(local),
                            f"{REMOTE}:{dest}/{name}"], check=True,
                           capture_output=True)
        ssh(f"chmod -R a+rX {dest} && rm -f {dest}/photo_new.png")
        return json.dumps(row)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://cw1:8877")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--idle", type=float, default=30)
    ap.add_argument("--http-push", action="store_true",
                    help="upload results over the api instead of ssh/scp "
                         "(community workers)")
    args = ap.parse_args()
    executor = ComfyHTTPExecutor(COMFYUI_URL, out_dir=str(ROOT / "out"))
    while True:
        pending = api("GET", "/api/tasks?status=pending&limit=10", args.api)
        pending = [t for t in pending
                   if t["action"] in ("generate", "regen")]
        if not pending:
            if args.once:
                break
            time.sleep(args.idle)
            continue
        task = pending[0]
        try:
            api("POST", f"/api/tasks/{task['id']}/claim", args.api)
        except requests.HTTPError:
            continue  # raced another worker
        print(f"[task {task['id']}] {task['action']} mmsi={task['mmsi']}",
              flush=True)
        try:
            note = await handle(task, args.api, executor,
                                http_push=args.http_push)
            api("POST", f"/api/tasks/{task['id']}/complete", args.api,
                json={"ok": True, "note": note})
            print(f"[task {task['id']}] done", flush=True)
        except Exception as e:
            api("POST", f"/api/tasks/{task['id']}/complete", args.api,
                json={"ok": False, "note": str(e)})
            print(f"[task {task['id']}] FAILED: {e}", flush=True)
        if args.once:
            break


asyncio.run(main())
