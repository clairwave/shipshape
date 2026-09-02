# ship3d

**A pipeline that turns a single vessel photo into a lightweight, textured 3D
model — and scales it to a fleet database keyed by MMSI.**

Built for [Clairwave](https://www.clairwave.com)'s live AIS terrain scene,
where up to ~400 ships render at once in three.js. Every model is a
scene-ready GLB: **≤10k triangles, ~150KB, KTX2-compressed texture,
meshopt-compressed geometry** — generated once, served forever.

```
photo ─▶ rembg cutout ─▶ Hunyuan3D-2.1 shape (3.3B) ─▶ multiview paint
      ─▶ texture bake ─▶ gltfpack (simplify + quantize + KTX2) ─▶ model.glb
```

Measured on a single RTX 5060 Ti 16GB: **~4 min per textured ship**.

## Components

| Path | What it is |
|---|---|
| `workflows/ship_production.json` | The production ComfyUI graph (shape + texture) |
| `scripts/produce.py` | photo(s) → GLB, resumable |
| `scripts/postprocess.py` | uniform gltfpack finishing (tri budget, KTX2) |
| `scripts/preprocess.py` | rembg cutout + square pad |
| `scripts/bench.py` | model-candidate shootout harness |
| `scripts/fleet_worker.py` | queue consumer: claims tasks, generates, pushes to the store (N workers safe) |
| `scripts/gen_archetypes.py` | class-archetype variant generation |
| `server/qc_api.py` | FastAPI + sqlite: task queue, MMSI model store, QC actions, community votes |
| `server/qc_viewer.html` | browser QC manager (three.js orbit review, approve/regen/reupload) |
| `server/stage_worker.py` | CPU staging: priority list → Wikimedia Commons photo → QC gate → enqueue |
| `assets/archetypes/taxonomy.json` | AIS ship-type code → archetype class mapping (with dims rules) |
| `docs/` | model selection rationale, fleet taxonomy, QC architecture |

## The fleet architecture

1. **Prioritize** — rank MMSIs by sighting persistence in your AIS history
   (a vessel seen daily deserves a unique model; a one-off doesn't).
2. **Stage (CPU)** — `stage_worker` resolves IMO/name per MMSI, fetches a
   photo from Wikimedia Commons (IMO-category first — polite rate limiting,
   attribution recorded), background-removes, and gates quality: single
   dominant blob, no port clutter, sufficient resolution.
3. **Generate (GPU)** — `fleet_worker`s consume the queue anywhere: a local
   GPU trickles 24/7; rented multi-GPU nodes burn backlogs (~$0.01–0.05 per
   ship). Claim semantics make concurrent workers safe.
4. **Serve** — `by_mmsi/<mmsi>/model.glb` + `meta.json` (provenance, QC
   status, votes, attribution) + `history/` versioning. Static, cacheable,
   ~150KB each.
5. **QC, two surfaces** — an ops manager (approve / regen / needs-photo /
   photo reupload) and community thumbs-up/down with a threshold that
   auto-queues regeneration but never overrides a human approval.
6. **Fallback** — vessels without a usable photo get a class archetype:
   17 classes derived from the AIS ship-type field (+ dims heuristics),
   several variants each, assigned by `hash(mmsi) % n` and scaled to the
   vessel's reported dimensions. See `docs/fleet_taxonomy.md`.

## Quickstart (single ship)

Requirements: a CUDA GPU with ≥12GB VRAM, Python 3.12, a
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) install with
[ComfyUI-Hunyuan3DWrapper](https://github.com/kijai/ComfyUI-Hunyuan3DWrapper),
and a **native** (BasisU-enabled) [gltfpack](https://github.com/zeux/meshoptimizer/releases)
— the npm build cannot write KTX2.

Weights (into `ComfyUI/models/diffusion_models/`):
- `hunyuan3d-dit-v2-1.fp16.ckpt` — from `tencent/Hunyuan3D-2.1`
  (`hunyuan3d-dit-v2-1/model.fp16.ckpt`; the wrapper's 2.1 loader needs the
  original nested-dict ckpt, not flat safetensors repackages)
- delight + paint models auto-download on first texture run

```bash
python scripts/preprocess.py my_ship_photo.jpg -o testdata/
python scripts/produce.py testdata/my_ship_photo.png
# → out/production/my_ship_photo.glb  (~150KB, textured, scene-ready)
```

Configuration is environment-driven — see `.env.example`.

## Format choices, briefly

GLB (binary glTF) is the container; inside it we use **meshopt**
(`EXT_meshopt_compression` — near-instant decode, which matters at hundreds
of ships in view; Draco would save ~10KB of geometry but decode slower) and
**KTX2/BasisU** textures (GPU-native, small VRAM footprint). Geometry is
simplified to a 10k-triangle budget; textures capped at 512px. The generator
does not control file size — this postprocess does.

## Licensing notes

- **This code:** MIT.
- **Generated models from Commons photos** are arguably derivatives of their
  source photographs (typically CC-BY / CC-BY-SA). Attribution is captured
  per model (`stage.json` / `meta.json`); distribute model sets under
  CC-BY-SA with that attribution intact.
- **Hunyuan3D weights** ship under Tencent's community license, which has
  territory restrictions — read it before running the generator in your
  jurisdiction. The pipeline is model-agnostic at the workflow layer
  (`scripts/bench.py` exists to compare candidates).

## Provenance

Extracted from Clairwave's production pipeline (underwater acoustics
platform; the ships swim above the sound-speed profiles). Issues and PRs
welcome — especially additional archetype photos, QC-gate improvements, and
alternative generator workflows.
