# Image → 3D model candidates for AIS vessel GLBs

Goal: one AIS vessel photo (single oblique view, water at the hull line,
background clutter) → lightweight textured GLB for the Clairwave three.js
terrain scene. Up to ~400 on screen at once.

## Hard constraints

- Local bringup on cw0 (RTX 5060 Ti 16GB); batch later on backend/pods.
- Output budget per ship (after post-processing, not raw model output):
  - **≤ 10k triangles** (400 ships × 10k = 4M tris — fine for three.js)
  - **≤ ~400KB GLB** (meshopt-quantized + KTX2 512px texture);
    400 unique ships ≈ 160MB worst case, cached + lazy-loaded per viewport
- Underside accuracy irrelevant — ships sit on water in the scene.
- Silhouette + superstructure recognizability is the accuracy metric that
  matters at scene camera distances, not close-up texture fidelity.

## Shortlist

| Model | Params | VRAM | Time/img | Raw output | Verdict |
|---|---|---|---|---|---|
| **Hunyuan3D-2.1** (shape+paint) | 1.1B + 1.3B | 10–16GB | ~60–120s | GLB, textured | Primary candidate. Strong on elongated hulls, kijai ComfyUI wrapper runs on Windows, paint stage optional |
| **Hunyuan3D-2mini** | 0.6B | 5–8GB | ~30s | GLB | Batch-cost candidate if quality holds |
| **TRELLIS** (image-large) | 1.2B | 12–16GB | ~30–60s | GLB/3DGS/NeRF | Primary candidate. Top geometry quality; native install is linux-bound (kaolin/spconv/nvdiffrast) — use ComfyUI-Trellis nodes or run on pods |
| **Hunyuan3D-2.1** shape | 3.3B | ~10-14GB | TBD | GLB | Largest open shape DiT; bench vs 2.0 for quality-per-second (2.5's 10B is API-only, no weights) |
| ~~SF3D~~ | ~1B | — | — | — | Dropped per user (HF-gated, and 2.0's quality already approved) |
| TripoSR | 0.4B | 6GB | ~1s | obj, vertex color | Older LRM, blobs on elongated objects — bench only as floor |
| TripoSG | 1.5B | 12GB+ | slow | mesh | Quality ceiling but heavier; only if TRELLIS/Hunyuan disappoint |
| LGM / CRM (gaussian) | — | — | — | splats | Rejected: not GLB/three.js-mesh friendly |

## Why diffusion-native (TRELLIS/Hunyuan) over one-shot LRM (TripoSR/SF3D)

Ships are the adversarial case for LRMs: long thin hulls, masts, cranes,
antennas. Latent-diffusion 3D models keep thin structures and straight hull
lines much better. SF3D stays in the bench as the cheap baseline — if it is
"good enough at 200m camera distance," its 30x speed advantage wins batch.

## Pipeline shape (model-agnostic)

1. **Preprocess** — rembg/BiRefNet background + water removal, square pad.
   AIS photos have waterline occlusion; the model hallucinates the hull
   bottom, which is hidden in-scene. Bench with and without water masked.
2. **Generate** — candidate model → raw mesh+texture (100k–500k tris typical).
3. **Post** — gltfpack (meshoptimizer): simplify to ≤10k tris, quantize,
   KTX2/BasisU texture at 512px → final GLB. This stage, not the generator,
   controls file size; run identical post for every candidate.
4. **Validate** — headless three.js/pyrender turntable (8 views) + tri/byte
   counts into a per-ship report row.

## Bench plan

- 8–10 real AIS photos across classes: tanker, container, bulker, tug,
  fishing, ferry, sailing yacht, offshore.
- Run every shortlist model on identical preprocessed inputs.
- Identical post-processing; score: silhouette IoU against the source photo
  at matched azimuth, artifact notes, tris/bytes, wall-clock, VRAM peak.
- Output: docs/bench_results.md + out/bench/<model>/<ship>.glb + turntables.

## Fallback strategy for scale (post-bench, for integration later)

Most AIS targets have no photo. Plan: ~10 generic class archetypes
(instanced, shared GLBs) for photoless vessels; unique GLBs only for
photo-bearing ships, generated once and cached on cw1.
