# Fleet QC architecture — two surfaces, one state store

## Storage (cw1 TB disk, MMSI-keyed, time-machine adjacent)

```
/data/clairwave/models/ships/
  by_mmsi/<mmsi>/
    model.glb            # current serving model (unique or archetype link)
    meta.json            # provenance + QC state (schema below)
    photo.png            # preprocessed source photo (if unique)
    history/<ts>.glb     # superseded versions (time-machine style)
  archetypes/<folder>/<variant>.glb   # shared, instanced client-side
  qc/queue.db            # sqlite: task queue + votes (single writer: worker)
```

meta.json:
```json
{
  "mmsi": "257123456",
  "kind": "unique | archetype",
  "archetype": "cargo_bulker/02",        // when kind=archetype
  "status": "auto | approved | regen_queued | needs_photo | community_flagged",
  "source_photo_sha": "...", "generated": "2026-09-01T...",
  "pipeline": "ship_production.json@838f098",
  "gen_s": 244.1, "final_bytes": 141388, "final_tris": 10000,
  "votes": {"up": 12, "down": 3}
}
```

Serving: nginx static at https://www.clairwave.com/dal/models/ships/... —
same pattern as the bucket; GLBs are immutable-cacheable (bust via
?v=<generated-ts> from meta).

## The pipeline loop (producer)

1. Time-machine DB emits MMSIs (new sightings or photo-acquired events).
2. Photo fetch (aissidepanel lookup function, backend-side).
3. **Input QC gate** (automated): rembg cutout → cheap checks (single blob,
   fill ratio, aspect) → VLM classifier ("open-water single vessel, no port
   structures?") → pass = unique-model path, fail = archetype path +
   `needs_photo` status so a better photo can heal it later.
4. produce.py chain → GLB + meta.json → by_mmsi/. Fully idempotent/resumable;
   runs local (cw0), backend batch, or rented pods — same repo.

## QC surface 1 — interim ops manager (dal-served, us)

Static viewer/manager at https://www.clairwave.com/dal/qc/ (three.js, same
bones as ship3d/viewer) + a small FastAPI service on cw1 (`qc-api`,
k8s or systemd, sqlite-backed):

- `GET  /qc/api/models?status=auto&limit=...` — review queue with meta
- `POST /qc/api/action` — `{mmsi, action: approve|regen|needs_photo|
  use_archetype, note?}` — writes a task row; the producer consumes
  `regen` tasks (optionally with param overrides: seed, octree, steps)
- `POST /qc/api/photo` — multipart reupload for a vessel (replaces source,
  queues regen). Auth: shared token header for interim (we are the only
  users); real auth arrives with platform integration.

## QC surface 2 — community signal (clairwave platform, end-game)

- Platform UI: thumbs up/down on the rendered ship in the terrain scene.
- `POST /qc/api/vote {mmsi, vote: +1|-1, session}` (rate-limited,
  deduped per session) → votes tally in meta/queue.db.
- Policy: `down - up >= N` (start N=5) AND status != approved →
  auto-queue regen with next seed; two failed community cycles →
  `needs_photo` + archetype fallback swap. `approved` (human QC) models
  only get flagged (`community_flagged`), never auto-regenerated.

## Convergence rule

Both surfaces write tasks/votes to the same queue.db; only the producer
mutates by_mmsi/. One writer = no coordination problems, and the whole QC
state is a single file to back up alongside the time machine.

## Build order (proposed)

1. `scripts/fleet_worker.py` — queue consumer + by_mmsi writer (wraps
   produce.py), local first
2. qc-api (FastAPI + sqlite, ~150 lines) + nginx route on cw1
3. dal QC viewer (adapt ship3d/viewer: queue list, approve/regen buttons,
   photo upload)
4. Archetype generation once folders are populated (variants × ~17 classes)
5. Platform vote endpoint + scene UI — with clairwave integration proper
```
