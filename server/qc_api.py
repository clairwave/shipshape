"""qc-api: fleet model QC service on cw1.

Owns the task queue (sqlite) and the MMSI model store metadata; serves GLBs
for interim QC. Both QC surfaces (dal manager, platform votes) write here;
only the fleet worker mutates by_mmsi/ content.

Run: uvicorn qc_api:app --host 0.0.0.0 --port 8877
Auth: X-QC-Token header on mutating/ops endpoints (token file next to app).
"""
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(os.environ.get("SHIPSHAPE_STORE",
                           "/data/disks/media/clairwave-models/ships"))
BY_MMSI = ROOT / "by_mmsi"
QC_DIR = ROOT / "qc"
DB = QC_DIR / "queue.db"
TOKEN = (Path(__file__).parent / "token").read_text().strip()

app = FastAPI(title="clairwave ship-model qc-api")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    BY_MMSI.mkdir(parents=True, exist_ok=True)
    QC_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY, mmsi TEXT NOT NULL,
            action TEXT NOT NULL, status TEXT DEFAULT 'pending',
            params TEXT DEFAULT '{}', note TEXT DEFAULT '',
            created REAL, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS votes(
            mmsi TEXT, vote INTEGER, session TEXT, ts REAL,
            UNIQUE(mmsi, session))""")


init()


def auth(token: str | None):
    if not token or token.strip() != TOKEN:
        raise HTTPException(401, "bad token")


def worker_tokens() -> set:
    f = Path(__file__).parent / "worker_tokens"
    return {t.strip() for t in f.read_text().splitlines()
            if t.strip()} if f.exists() else set()


def auth_worker(token: str | None):
    """Ops token OR a per-contributor worker token (claim/complete/result
    only — never QC actions or enqueue)."""
    if not token or (token.strip() != TOKEN
                     and token.strip() not in worker_tokens()):
        raise HTTPException(401, "bad token")


def meta_path(mmsi: str) -> Path:
    if not mmsi.isdigit() or len(mmsi) > 9:
        raise HTTPException(400, "bad mmsi")
    return BY_MMSI / mmsi / "meta.json"


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "qc_viewer.html")


@app.get("/api/health")
def health():
    return {"ok": True, "models": sum(1 for _ in BY_MMSI.glob("*/meta.json"))}


@app.post("/api/enqueue")
def enqueue(body: dict, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    mmsi = str(body["mmsi"])
    with db() as c:
        cur = c.execute(
            "INSERT INTO tasks(mmsi, action, params, note, created, updated) "
            "VALUES(?,?,?,?,?,?)",
            (mmsi, body.get("action", "generate"),
             json.dumps(body.get("params", {})), body.get("note", ""),
             time.time(), time.time()))
    return {"task_id": cur.lastrowid}


@app.get("/api/tasks")
def tasks(status: str = "pending", limit: int = 10,
          action: str | None = None,
          x_qc_token: str | None = Header(None)):
    auth_worker(x_qc_token)
    with db() as c:
        if action:
            rows = c.execute("SELECT * FROM tasks WHERE status=? AND action=? "
                             "ORDER BY id LIMIT ?",
                             (status, action, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM tasks WHERE status=? "
                             "ORDER BY id LIMIT ?", (status, limit)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/tasks/{task_id}/claim")
def claim(task_id: int, x_qc_token: str | None = Header(None)):
    auth_worker(x_qc_token)
    with db() as c:
        cur = c.execute("UPDATE tasks SET status='running', updated=? "
                        "WHERE id=? AND status='pending'",
                        (time.time(), task_id))
    if cur.rowcount == 0:
        raise HTTPException(409, "not claimable")
    return {"claimed": task_id}


@app.post("/api/tasks/{task_id}/complete")
def complete(task_id: int, body: dict, x_qc_token: str | None = Header(None)):
    auth_worker(x_qc_token)
    with db() as c:
        c.execute("UPDATE tasks SET status=?, note=?, updated=? WHERE id=?",
                  ("done" if body.get("ok") else "failed",
                   body.get("note", ""), time.time(), task_id))
    return {"ok": True}


@app.get("/api/models")
def models(status: str | None = None, limit: int = 100):
    out = []
    for mp in sorted(BY_MMSI.glob("*/meta.json")):
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        if status and m.get("status") != status:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


@app.post("/api/action")
def action(body: dict, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    mmsi, act = str(body["mmsi"]), body["action"]
    if act not in ("approve", "regen", "needs_photo", "use_archetype"):
        raise HTTPException(400, "bad action")
    mp = meta_path(mmsi)
    if act == "approve":
        m = json.loads(mp.read_text())
        m["status"] = "approved"
        mp.write_text(json.dumps(m, indent=1))
        return {"ok": True}
    if mp.exists():
        m = json.loads(mp.read_text())
        m["status"] = {"regen": "regen_queued", "needs_photo": "needs_photo",
                       "use_archetype": "auto"}[act]
        mp.write_text(json.dumps(m, indent=1))
    if act != "regen":
        return {"ok": True}  # metadata-only actions: no worker task needed
    return enqueue({"mmsi": mmsi, "action": act,
                    "params": body.get("params", {}),
                    "note": body.get("note", "")}, x_qc_token)


@app.post("/api/photo/{mmsi}")
async def photo(mmsi: str, file: UploadFile,
                x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    d = meta_path(mmsi).parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "photo_new.png").write_bytes(await file.read())
    return enqueue({"mmsi": mmsi, "action": "regen",
                    "note": "photo reupload"}, x_qc_token)


@app.post("/api/vote")
def vote(body: dict):
    mmsi, v = str(body["mmsi"]), int(body["vote"])
    if v not in (-1, 1):
        raise HTTPException(400, "vote must be +-1")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO votes(mmsi, vote, session, ts) "
                  "VALUES(?,?,?,?)",
                  (mmsi, v, str(body.get("session", "anon")), time.time()))
        tally = c.execute("SELECT COALESCE(SUM(vote),0) s FROM votes "
                          "WHERE mmsi=?", (mmsi,)).fetchone()["s"]
    mp = meta_path(mmsi)
    if mp.exists():
        m = json.loads(mp.read_text())
        m.setdefault("votes", {})["net"] = tally
        # community can flag but never auto-regen an approved model
        if tally <= -5 and m.get("status") == "auto":
            m["status"] = "regen_queued"
            with db() as c:
                c.execute("INSERT INTO tasks(mmsi, action, params, note, "
                          "created, updated) VALUES(?,?,?,?,?,?)",
                          (mmsi, "regen", '{"seed_bump": true}',
                           "community threshold", time.time(), time.time()))
        elif tally <= -5:
            m["status"] = "community_flagged"
        mp.write_text(json.dumps(m, indent=1))
    return {"net": tally}


@app.get("/api/archetypes")
def archetypes():
    root = ROOT / "archetypes"
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if d.is_dir():
                out.append({"folder": d.name,
                            "variants": sorted(p.name for p in d.glob("*.glb"))})
    return out


@app.get("/archetypes/{folder}/{fname}")
def archetype_file(folder: str, fname: str):
    if "/" in folder or ".." in folder or not fname.endswith(".glb") \
            or "/" in fname or ".." in fname:
        raise HTTPException(404)
    p = ROOT / "archetypes" / folder / fname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ── staged-photo review: photos held here before any GPU time is spent ──

@app.get("/api/staged")
def staged(limit: int = 200, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    out = []
    for sp in BY_MMSI.glob("*/stage.json"):
        try:
            s = json.loads(sp.read_text())
        except Exception:
            continue
        if s.get("review", "pending") != "pending":
            continue
        if (sp.parent / "model.glb").exists() \
                or not (sp.parent / "photo.png").exists():
            continue
        st = s.get("statics", {})
        out.append({"mmsi": s["mmsi"], "name": st.get("name"),
                    "type": st.get("type"), "length": st.get("length"),
                    "score": s.get("score"), "days_seen": s.get("days_seen"),
                    "photo_page": s.get("photo_page")})
        if len(out) >= limit:
            break
    return out


@app.post("/api/staged/decision")
def staged_decision(body: dict, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    approved = rejected = 0
    for d in body.get("decisions", []):
        mmsi = str(d["mmsi"])
        sp = meta_path(mmsi).parent / "stage.json"
        if not sp.exists():
            continue
        s = json.loads(sp.read_text())
        if d.get("approve"):
            s["review"] = "approved"
            sp.write_text(json.dumps(s, indent=1))
            with db() as c:
                c.execute("INSERT INTO tasks(mmsi, action, params, note, "
                          "created, updated) VALUES(?,?,?,?,?,?)",
                          (mmsi, "generate", "{}", "photo approved",
                           time.time(), time.time()))
            approved += 1
        else:
            s["review"] = "rejected"
            sp.write_text(json.dumps(s, indent=1))
            (sp.parent / "photo.png").unlink(missing_ok=True)
            with (QC_DIR / "stage_misses.jsonl").open("a") as f:
                f.write(json.dumps({"mmsi": mmsi, "ts": time.time(),
                                    "reason": "ops rejected photo"}) + "\n")
            rejected += 1
    return {"approved": approved, "rejected": rejected}


@app.post("/api/align")
def align(body: dict, x_qc_token: str | None = Header(None)):
    """Ops orientation fix: rotate model about +Y by yaw_deg (convention:
    bow = +Z, up = +Y). Composes with any existing alignment."""
    auth(x_qc_token)
    from glb_align import apply_yaw
    if body.get("archetype"):
        folder, fname = str(body["archetype"]).split("/", 1)
        if ".." in folder or ".." in fname or "/" in fname:
            raise HTTPException(400, "bad archetype")
        glb = ROOT / "archetypes" / folder / fname
        if not glb.exists():
            raise HTTPException(404, "no archetype")
        total = apply_yaw(glb, float(body.get("yaw_deg", 0)))
        return {"ok": True, "align_yaw": total, "archetype": body["archetype"]}
    mmsi = str(body["mmsi"])
    glb = meta_path(mmsi).parent / "model.glb"
    if not glb.exists():
        raise HTTPException(404, "no model")
    total = apply_yaw(glb, float(body.get("yaw_deg", 0)))
    mp = meta_path(mmsi)
    if mp.exists():
        m = json.loads(mp.read_text())
        m["align_yaw"] = total
        m["aligned"] = True
        mp.write_text(json.dumps(m, indent=1))
    return {"ok": True, "align_yaw": total}


@app.post("/api/staged/photo/{mmsi}")
async def staged_photo(mmsi: str, file: UploadFile,
                       x_qc_token: str | None = Header(None)):
    """Ops replaces a staged photo directly (e.g. after rejecting the Commons
    hit). A human-chosen photo is implicitly approved -> queue generation."""
    auth(x_qc_token)
    data = await file.read()
    if len(data) > 25_000_000:
        raise HTTPException(413, "photo too large")
    d = meta_path(mmsi).parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "photo.png").write_bytes(data)
    sp = d / "stage.json"
    s = json.loads(sp.read_text()) if sp.exists() else {"mmsi": mmsi}
    s.update({"review": "approved", "photo_source": "ops-upload",
              "photo_page": None, "staged": time.time()})
    sp.write_text(json.dumps(s, indent=1))
    with db() as c:
        c.execute("INSERT INTO tasks(mmsi, action, params, note, created, "
                  "updated) VALUES(?,?,?,?,?,?)",
                  (mmsi, "generate", "{}", "ops photo upload",
                   time.time(), time.time()))
    return {"ok": True, "queued": True}


# ── public fleet portal (read-only + votes): /fleet/* — no token needed;
# nginx exposes only this prefix publicly ──

_statics_cache: dict = {}


def statics():
    if not _statics_cache:
        snap = QC_DIR / "statics_snapshot.jsonl"
        if snap.exists():
            for line in snap.read_text().splitlines():
                try:
                    v = json.loads(line)
                    _statics_cache[str(v["mmsi"])] = {
                        "mmsi": str(v["mmsi"]), "name": v.get("name") or "",
                        "type": v.get("type"), "length": v.get("length"),
                        "beam": v.get("beam")}
                except Exception:
                    pass
    return _statics_cache


@app.get("/fleet")
@app.get("/fleet/")
def fleet_page():
    return FileResponse(Path(__file__).parent / "fleet.html")


@app.get("/fleet/api/search")
def fleet_search(q: str, limit: int = 25):
    q = q.strip()
    if len(q) < 3:
        return []
    ql = q.lower()
    out = []
    for v in statics().values():
        if (q.isdigit() and v["mmsi"].startswith(q)) \
                or (not q.isdigit() and ql in v["name"].lower()):
            has = (BY_MMSI / v["mmsi"] / "model.glb").exists()
            out.append({**v, "has_model": has})
            if len(out) >= limit * 3:
                break
    out.sort(key=lambda r: not r["has_model"])
    return out[:limit]


@app.post("/fleet/api/interest")
def fleet_interest(body: dict, request: Request):
    """Public demand signal: a viewed vessel with no model queues a staging
    task (photo lookup + QC gate). Deduped; per-IP daily cap."""
    mmsi = str(body.get("mmsi", ""))
    mp = meta_path(mmsi)  # validates mmsi format
    if (mp.parent / "model.glb").exists():
        return {"queued": False, "reason": "model exists"}
    ip = request.headers.get("x-real-ip") or (request.client.host
                                              if request.client else "?")
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS public_req(ip TEXT, ts REAL)")
        n = c.execute("SELECT count(*) n FROM public_req WHERE ip=? AND ts>?",
                      (ip, time.time() - 86400)).fetchone()["n"]
        if n >= 20:
            raise HTTPException(429, "daily limit reached")
        dup = c.execute(
            "SELECT count(*) n FROM tasks WHERE mmsi=? AND status IN "
            "('pending','running')", (mmsi,)).fetchone()["n"]
        if dup:
            return {"queued": False, "reason": "already queued"}
        done_stage = c.execute(
            "SELECT count(*) n FROM tasks WHERE mmsi=? AND action='stage' "
            "AND updated>?", (mmsi, time.time() - 30 * 86400)).fetchone()["n"]
        if done_stage:
            return {"queued": False, "reason": "recently attempted"}
        c.execute("INSERT INTO public_req VALUES(?,?)", (ip, time.time()))
        c.execute("INSERT INTO tasks(mmsi, action, params, note, created, "
                  "updated) VALUES(?,?,?,?,?,?)",
                  (mmsi, "stage", "{}", f"public interest ({ip})",
                   time.time(), time.time()))
    return {"queued": True}


@app.post("/fleet/api/interest_batch")
def fleet_interest_batch(body: dict, request: Request):
    """Platform demand signal: the terrain scene reports every vessel in view
    that resolved to an archetype/none. Deduped like /interest; higher cap
    because one bbox change can carry hundreds of vessels."""
    mmsis = [str(m) for m in body.get("mmsis", [])][:300]
    ip = request.headers.get("x-real-ip") or (request.client.host
                                              if request.client else "?")
    queued = 0
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS public_req(ip TEXT, ts REAL)")
        n = c.execute("SELECT count(*) n FROM public_req WHERE ip=? AND ts>?",
                      (ip, time.time() - 86400)).fetchone()["n"]
        budget = max(0, 2000 - n)
        for mmsi in mmsis:
            if queued >= budget or not mmsi.isdigit() or len(mmsi) > 9:
                continue
            if (BY_MMSI / mmsi / "model.glb").exists():
                continue
            if c.execute("SELECT 1 FROM tasks WHERE mmsi=? AND (status IN "
                         "('pending','running') OR (action='stage' AND updated>?))",
                         (mmsi, time.time() - 30 * 86400)).fetchone():
                continue
            if (BY_MMSI / mmsi / "stage.json").exists():
                continue  # already staged (in review) or decided
            c.execute("INSERT INTO public_req VALUES(?,?)", (ip, time.time()))
            c.execute("INSERT INTO tasks(mmsi, action, params, note, created, "
                      "updated) VALUES(?,?,?,?,?,?)",
                      (mmsi, "stage", "{}", f"platform view ({ip})",
                       time.time(), time.time()))
            queued += 1
    return {"queued": queued, "considered": len(mmsis)}


@app.post("/api/tasks/{task_id}/result")
async def task_result(task_id: int, model: UploadFile, photo: UploadFile,
                      meta: UploadFile, x_qc_token: str | None = Header(None)):
    """HTTP result upload for workers without ssh access (community GPUs)."""
    auth_worker(x_qc_token)
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=? AND status='running'",
                        (task_id,)).fetchone()
    if row is None:
        raise HTTPException(409, "task not running")
    mmsi = row["mmsi"]
    glb = await model.read()
    if not (1000 < len(glb) < 5_000_000) or glb[:4] != b"glTF":
        raise HTTPException(400, "invalid glb")
    m = json.loads((await meta.read()).decode())
    m.update({"mmsi": mmsi, "kind": "unique", "status": "auto",
              "task_id": task_id, "uploaded_via": "http"})
    d = meta_path(mmsi).parent
    (d / "history").mkdir(parents=True, exist_ok=True)
    if (d / "model.glb").exists():
        (d / "model.glb").rename(d / "history" / f"{int(time.time())}.glb")
    (d / "model.glb").write_bytes(glb)
    (d / "photo.png").write_bytes(await photo.read())
    (d / "meta.json").write_text(json.dumps(m, indent=1))
    with db() as c:
        c.execute("UPDATE tasks SET status='done', note='http result', "
                  "updated=? WHERE id=?", (time.time(), task_id))
    return {"ok": True}


@app.post("/fleet/api/photo/{mmsi}")
async def fleet_photo(mmsi: str, file: UploadFile, request: Request):
    """Community photo contribution for an UNGENERATED vessel. Goes to the
    ops review queue (staged photos) — never straight to generation."""
    d = meta_path(mmsi).parent
    if (d / "model.glb").exists():
        raise HTTPException(409, "model exists — vote on it instead")
    ip = request.headers.get("x-real-ip") or (request.client.host
                                              if request.client else "?")
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS public_req(ip TEXT, ts REAL)")
        n = c.execute("SELECT count(*) n FROM public_req WHERE ip=? AND ts>?",
                      (ip, time.time() - 86400)).fetchone()["n"]
        if n >= 5:
            raise HTTPException(429, "daily contribution limit reached")
        c.execute("INSERT INTO public_req VALUES(?,?)", (ip, time.time()))
    data = await file.read()
    if len(data) > 15_000_000:
        raise HTTPException(413, "photo too large (15MB max)")
    d.mkdir(parents=True, exist_ok=True)
    (d / "photo.png").write_bytes(data)
    sp = d / "stage.json"
    s = json.loads(sp.read_text()) if sp.exists() else {"mmsi": mmsi}
    s.update({"review": "pending", "photo_source": "community-upload",
              "photo_page": None, "uploader_ip": ip, "staged": time.time()})
    sp.write_text(json.dumps(s, indent=1))
    return {"ok": True, "review": "pending"}


_taxonomy: dict = {}


def taxonomy():
    if not _taxonomy:
        f = Path(__file__).parent / "taxonomy.json"
        if f.exists():
            _taxonomy.update(json.loads(f.read_text()))
    return _taxonomy


def archetype_for(ship_type, loa, beam):
    """AIS type code + dims -> archetype folder (assets/archetypes/taxonomy)."""
    tx = taxonomy()
    code = int(ship_type or 0)
    for rule in tx.get("code_map", []):
        if not any(lo <= code <= hi for lo, hi in rule["codes"]):
            continue
        if "folder" in rule:
            return rule["folder"]
        for r in rule.get("rules", []):
            d = r.get("dims", {})
            if "loa_min" in d and not (loa and loa >= d["loa_min"]):
                continue
            if "beam_loa_ratio_max" in d and not (
                    loa and beam and beam / loa <= d["beam_loa_ratio_max"]):
                continue
            return r["folder"]
    return "other_generic"


@app.get("/fleet/api/resolve/{mmsi}")
def fleet_resolve(mmsi: str):
    """One call per vessel for the platform: unique model if it exists,
    else the class archetype variant (deterministic by mmsi), with the
    alignment yaw and AIS dims for scaling."""
    mp = meta_path(mmsi)
    st = statics().get(mmsi, {})
    base = {"mmsi": mmsi, "name": st.get("name"), "type": st.get("type"),
            "length": st.get("length"), "beam": st.get("beam")}
    if (mp.parent / "model.glb").exists():
        m = json.loads(mp.read_text()) if mp.exists() else {}
        return {**base, "kind": "unique",
                "url": f"/fleet/models/{mmsi}/model.glb",
                "yaw": m.get("align_yaw", 0), "aligned": m.get("aligned", False),
                "status": m.get("status")}
    folder = archetype_for(st.get("type"), st.get("length"), st.get("beam"))
    variants = sorted(p.name for p in (ROOT / "archetypes" / folder).glob("*.glb"))         if (ROOT / "archetypes" / folder).exists() else []
    if not variants:
        return {**base, "kind": "none", "archetype_class": folder}
    v = variants[int(mmsi) % len(variants)]
    return {**base, "kind": "archetype", "archetype_class": folder,
            "variant": v, "url": f"/fleet/archetypes/{folder}/{v}", "yaw": 0}


@app.get("/fleet/archetypes/{folder}/{fname}")
def fleet_archetype_file(folder: str, fname: str):
    return archetype_file(folder, fname)


@app.post("/fleet/api/vote")
def fleet_vote(body: dict):
    return vote(body)


@app.get("/fleet/models/{mmsi}/{fname}")
def fleet_model_file(mmsi: str, fname: str):
    return model_file(mmsi, fname)


@app.get("/models/{mmsi}/{fname}")
def model_file(mmsi: str, fname: str):
    if fname not in ("model.glb", "photo.png", "meta.json"):
        raise HTTPException(404)
    p = meta_path(mmsi).parent / fname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)
