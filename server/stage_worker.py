"""stage_worker: CPU staging for the fleet pipeline (runs on cw1).

Walks the sighting-priority MMSI list, resolves statics (IMO/name) from the
redis snapshot, fetches a vessel photo from Wikimedia Commons (same
IMO-category waterfall as clairwave's sidepanel), background-removes and
QC-gates it, and on pass drops the ready photo into by_mmsi/<mmsi>/photo.png
+ enqueues a generate task. Misses are negative-cached in qc/stage_misses.jsonl.

Run: .venv/bin/python stage_worker.py --limit 200 [--min-days 7] [--mmsi ...]
"""
import argparse
import io
import json
import os
import time
from pathlib import Path

import requests
from PIL import Image
from rembg import remove

# a handful of Commons originals are 100+ megapixel — decoding one can eat
# gigabytes and starve the host. Hard-cap decode size; oversized -> miss.
Image.MAX_IMAGE_PIXELS = 40_000_000

ROOT = Path(os.environ.get("SHIPSHAPE_STORE",
                           "/data/disks/media/clairwave-models/ships"))
BY_MMSI = ROOT / "by_mmsi"
QC_DIR = ROOT / "qc"
MISSES = QC_DIR / "stage_misses.jsonl"
TOKEN = (Path(__file__).parent / "token").read_text().strip()
API = "http://127.0.0.1:8877"
COMMONS = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent":
      "ClairwaveVesselPhotos/1.0 (https://www.clairwave.com; ops@clairwave.com)"}
RATE_S = 1.1
_last = [0.0]


def commons(params):
    wait = RATE_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    r = requests.get(COMMONS, params={**params, "format": "json"},
                     headers=UA, timeout=10)
    r.raise_for_status()
    return r.json()


def imageinfo(title):
    j = commons({"action": "query", "titles": title, "prop": "imageinfo",
                 "iiprop": "url|extmetadata", "iiurlwidth": 1600})
    for page in (j.get("query", {}).get("pages") or {}).values():
        for ii in page.get("imageinfo", []):
            return {"url": ii.get("thumburl") or ii.get("url"),
                    "page": ii.get("descriptionurl"),
                    "meta": {k: (v or {}).get("value", "")
                             for k, v in (ii.get("extmetadata") or {}).items()
                             if k in ("Artist", "LicenseShortName")}}
    return None


def by_imo(imo):
    j = commons({"action": "query", "list": "categorymembers",
                 "cmtitle": f"Category:IMO {imo}", "cmtype": "file",
                 "cmlimit": 3})
    files = [m["title"] for m in j.get("query", {}).get("categorymembers", [])]
    if not files:
        j = commons({"action": "query", "list": "categorymembers",
                     "cmtitle": f"Category:IMO {imo}", "cmtype": "subcat",
                     "cmlimit": 1})
        subs = j.get("query", {}).get("categorymembers", [])
        if subs:
            j = commons({"action": "query", "list": "categorymembers",
                         "cmtitle": subs[0]["title"], "cmtype": "file",
                         "cmlimit": 3})
            files = [m["title"] for m in
                     j.get("query", {}).get("categorymembers", [])]
    for t in files:
        info = imageinfo(t)
        if info:
            info["source"] = "commons-imo"
            return info
    return None


def by_name(name):
    name = (name or "").strip()
    if len(name) < 4:
        return None
    j = commons({"action": "query", "list": "search", "srnamespace": 6,
                 "srsearch": f'intitle:"{name}" ship', "srlimit": 3})
    for h in j.get("query", {}).get("search", []):
        info = imageinfo(h.get("title", ""))
        if info:
            info["source"] = "commons-search"
            return info
    return None


def qc_gate(cutout: Image.Image):
    """Cheap checks on the rembg RGBA cutout. Returns (ok, reason, score)."""
    import numpy as np
    a = np.asarray(cutout.split()[-1], dtype=np.uint8)
    fg = a > 32
    total = fg.size
    cover = fg.sum() / total
    if cover < 0.05:
        return False, f"tiny foreground {cover:.2f}", 0
    if cover > 0.80:
        return False, f"no separation {cover:.2f}", 0
    ys, xs = fg.nonzero()
    w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    if w < 300:
        return False, f"low res bbox {w}px", 0
    # port-clutter proxy: foreground should be one dominant blob
    from scipy import ndimage
    lbl, n = ndimage.label(fg)
    if n > 1:
        sizes = ndimage.sum(fg, lbl, range(1, n + 1))
        if sizes.max() / sizes.sum() < 0.85:
            return False, f"fragmented fg ({n} blobs)", 0
    score = round(min(1.0, cover * 2 + min(w, 1600) / 3200), 2)
    return True, "ok", score


def preprocess(img: Image.Image, size=1024) -> Image.Image:
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    side = int(max(img.size) * 1.1)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def load_misses():
    seen = {}
    if MISSES.exists():
        for line in MISSES.read_text().splitlines():
            try:
                r = json.loads(line)
                seen[r["mmsi"]] = r["ts"]
            except Exception:
                pass
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-days", type=int, default=7)
    ap.add_argument("--mmsi", nargs="*", help="explicit MMSIs (skip csv)")
    ap.add_argument("--retry-days", type=float, default=30)
    ap.add_argument("--queue", action="store_true",
                    help="daemon: service 'stage' tasks from the qc-api queue")
    args = ap.parse_args()
    if args.queue:
        return queue_loop()

    statics = {}
    for line in (QC_DIR / "statics_snapshot.jsonl").read_text().splitlines():
        try:
            v = json.loads(line)
            statics[str(v["mmsi"])] = v
        except Exception:
            pass

    if args.mmsi:
        order = [(m, 999) for m in args.mmsi]
    else:
        order = []
        for line in (QC_DIR / "priority_mmsi.csv").read_text().splitlines()[1:]:
            m, days, _ = line.split(",")
            if int(days) < args.min_days:
                break
            order.append((m, int(days)))

    misses = load_misses()
    staged = failed = 0
    for mmsi, days in order:
        if staged >= args.limit:
            break
        d = BY_MMSI / mmsi
        if (d / "model.glb").exists() or (d / "photo.png").exists():
            continue
        if time.time() - misses.get(mmsi, 0) < args.retry_days * 86400:
            continue
        ok, note = stage_vessel(mmsi, statics, days=days, enqueue=True)
        if ok:
            staged += 1
        else:
            failed += 1
    print(f"=== STAGING DONE: {staged} staged, {failed} misses ===",
          flush=True)


def stage_vessel(mmsi, statics, days=0, enqueue=True):
    """Commons lookup + QC gate for one vessel. Returns (ok, note)."""
    d = BY_MMSI / mmsi
    v = statics.get(mmsi, {})
    imo, name = v.get("imo"), v.get("name")
    try:
        info = None
        if imo and int(imo) > 0:
            info = by_imo(int(imo))
        if info is None and name:
            info = by_name(name)
        if info is None:
            raise LookupError("no commons photo")
        raw = requests.get(info["url"], headers=UA, timeout=30).content
        if len(raw) > 25_000_000:
            raise LookupError(f"photo too large ({len(raw)>>20}MB)")
        src = Image.open(io.BytesIO(raw))
        src.thumbnail((2048, 2048))  # bound rembg memory before inference
        cutout = remove(src).convert("RGBA")
        ok, reason, score = qc_gate(cutout)
        if not ok:
            raise LookupError(reason)
        d.mkdir(parents=True, exist_ok=True)
        preprocess(cutout).save(d / "photo.png")
        (d / "stage.json").write_text(json.dumps({
            "mmsi": mmsi, "days_seen": days, "score": score,
            "photo_source": info["source"], "photo_page": info["page"],
            "attribution": info["meta"], "statics": v,
            "staged": time.time()}, indent=1))
        if enqueue:
            requests.post(f"{API}/api/enqueue", json={
                "mmsi": mmsi, "action": "generate",
                "note": f"staged score={score}"},
                headers={"X-QC-Token": TOKEN}, timeout=10).raise_for_status()
        print(f"[staged] {mmsi} {name or ''} score={score}", flush=True)
        return True, f"score={score}"
    except Exception as e:
        reason = str(e) or "error"
        with MISSES.open("a") as f:
            f.write(json.dumps({"mmsi": mmsi, "ts": time.time(),
                                "reason": reason}) + "\n")
        print(f"[miss] {mmsi} {name or ''}: {reason}", flush=True)
        return False, reason


def queue_loop(idle=60):
    """Daemon: service 'stage' tasks (public interest) from the queue."""
    statics_map = {}
    for line in (QC_DIR / "statics_snapshot.jsonl").read_text().splitlines():
        try:
            v = json.loads(line)
            statics_map[str(v["mmsi"])] = v
        except Exception:
            pass
    hdrs = {"X-QC-Token": TOKEN}
    print(f"stage queue daemon up ({len(statics_map)} statics)", flush=True)
    while True:
        try:
            rows = requests.get(
                f"{API}/api/tasks?status=pending&limit=25&action=stage",
                headers=hdrs, timeout=30).json()
            stage_tasks = [t for t in rows if t["action"] == "stage"]
            if not stage_tasks:
                time.sleep(idle)
                continue
            for t in stage_tasks:
                r = requests.post(f"{API}/api/tasks/{t['id']}/claim",
                                  headers=hdrs, timeout=30)
                if r.status_code != 200:
                    continue
                ok, note = stage_vessel(t["mmsi"], statics_map, enqueue=True)
                requests.post(f"{API}/api/tasks/{t['id']}/complete",
                              headers=hdrs, timeout=30,
                              json={"ok": ok, "note": note})
        except Exception as e:
            print(f"[queue-loop error] {e}", flush=True)
            time.sleep(idle)


if __name__ == "__main__":
    main()
