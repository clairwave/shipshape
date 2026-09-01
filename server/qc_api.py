"""qc-api: fleet model QC service on cw1.

Owns the task queue (sqlite) and the MMSI model store metadata; serves GLBs
for interim QC. Both QC surfaces (dal manager, platform votes) write here;
only the fleet worker mutates by_mmsi/ content.

Run: uvicorn qc_api:app --host 0.0.0.0 --port 8877
Auth: X-QC-Token header on mutating/ops endpoints (token file next to app).
"""
import json
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path("/data/disks/media/clairwave-models/ships")
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
          x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE status=? "
                         "ORDER BY id LIMIT ?", (status, limit)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/tasks/{task_id}/claim")
def claim(task_id: int, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
    with db() as c:
        cur = c.execute("UPDATE tasks SET status='running', updated=? "
                        "WHERE id=? AND status='pending'",
                        (time.time(), task_id))
    if cur.rowcount == 0:
        raise HTTPException(409, "not claimable")
    return {"claimed": task_id}


@app.post("/api/tasks/{task_id}/complete")
def complete(task_id: int, body: dict, x_qc_token: str | None = Header(None)):
    auth(x_qc_token)
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


@app.get("/models/{mmsi}/{fname}")
def model_file(mmsi: str, fname: str):
    if fname not in ("model.glb", "photo.png", "meta.json"):
        raise HTTPException(404)
    p = meta_path(mmsi).parent / fname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)
