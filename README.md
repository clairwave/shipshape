# ship3d

Picture → lightweight 3D ship model (GLB) pipeline for the Clairwave
platform. Fork of the clair avatar infra: reuses its ComfyUI client,
executor, and workflow-JSON pattern.

Input: AIS vessel photos (the ones aissidepanel.vue already fetches).
Output: ≤10k-tri, ≤~400KB textured GLBs for the three.js terrain scene
(up to ~400 displayed at once).

## Layout

- `src/ship3d/` — comfy client + executor (ported from clair, proven)
- `workflows/` — ComfyUI API-format workflow JSONs per candidate model
- `scripts/bench.py` — candidate shootout harness (see docs/model_candidates.md)
- `scripts/postprocess.py` — mesh simplify + quantize + KTX2 → final GLB
- `testdata/` — real AIS photos for the bench (not in git)
- `out/` — generated meshes, turntables, bench reports (not in git)

## Status

- [x] Workdir + infra port
- [ ] Model shootout: Hunyuan3D-2.1 / 2mini vs TRELLIS vs SF3D (docs/model_candidates.md)
- [ ] Post-processing chain (gltfpack) + size/accuracy report
- [ ] Model choice locked
- [ ] Batch runner (backend/pod) — after choice
- [ ] Clairwave integration — explicitly out of scope until pipeline is proven
