"""Executor for a ComfyUI instance reachable over HTTP.

Covers both `local_comfy` (your 5060 box) and `runpod_pod` (rented H200 with
ComfyUI's port exposed) — same protocol, different URL.
"""
from __future__ import annotations

import time
from pathlib import Path

from shipshape.comfy.client import ComfyClient, apply_overrides, load_workflow
from shipshape.executor.base import JobResult, JobSpec


class ComfyHTTPExecutor:
    def __init__(self, base_url: str, out_dir: str | Path = "out", name: str = "local_comfy"):
        self.name = name
        self.client = ComfyClient(base_url)
        self.out_dir = Path(out_dir)

    async def run(self, spec: JobSpec) -> JobResult:
        if spec.workflow is None:
            raise ValueError(f"{spec.kind} job requires a workflow on this executor")
        start = time.monotonic()

        overrides = dict(spec.inputs)
        # Upload any local input artifacts and rewrite the corresponding overrides:
        # artifacts_in {"image": "out/still.png"} + inputs {"LoadImage.image": "@image"}
        fp = spec.fingerprint()
        for name, path in spec.artifacts_in.items():
            # unique per-job server name — concurrent jobs sharing basenames
            # overwrote each other's inputs (prompts read files at execution
            # time, not submission time)
            unique = f"{fp}_{name}{Path(path).suffix}"
            server_name = await self.client.upload_image(path, unique)
            for key, value in overrides.items():
                if value == f"@{name}":
                    overrides[key] = server_name
        if spec.seed is not None and "Sampler.seed" not in overrides:
            overrides["Sampler.seed"] = spec.seed

        workflow = apply_overrides(load_workflow(spec.workflow), overrides)
        # job scratch lives under out/jobs/ — these accumulate in the hundreds
        # and used to bury the real deliverables at the top of out/
        job_dir = self.out_dir / "jobs" / f"{spec.kind}_{fp}"
        outputs = await self.client.run(workflow, job_dir)

        return JobResult(
            spec=spec,
            artifacts={p.stem: str(p) for p in outputs},
            duration_s=time.monotonic() - start,
            executor=self.name,
        )
