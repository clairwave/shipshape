"""The portability seam: pipeline stages emit JobSpecs, executors run them somewhere.

A JobSpec is pure data (JSON-serializable). The same spec runs on a local ComfyUI,
a rented H200 pod, or a serverless worker — the executor is the only thing that changes.
Every result carries the spec that produced it, so any asset is reproducible.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

JobKind = Literal["still", "video", "train", "tts", "lipsync"]


class JobSpec(BaseModel):
    kind: JobKind
    # For comfy jobs: workflow filename + node input overrides.
    # For train/tts/lipsync: backend-specific params.
    workflow: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Input artifacts by name -> local path or s3:// URI (e.g. the still to animate).
    artifacts_in: dict[str, str] = Field(default_factory=dict)
    seed: int | None = None

    def fingerprint(self) -> str:
        payload = self.model_dump_json(exclude_none=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class JobResult(BaseModel):
    spec: JobSpec
    # Output artifacts by name -> local path (executor downloads remote outputs).
    artifacts: dict[str, str]
    duration_s: float
    executor: str
    extra: dict[str, Any] = Field(default_factory=dict)

    def write_sidecar(self, artifact_path: str) -> None:
        """Record provenance next to a generated asset."""
        with open(artifact_path + ".json", "w", encoding="utf-8") as f:
            f.write(json.dumps(self.model_dump(), indent=2, default=str))


class Executor(Protocol):
    name: str

    async def run(self, spec: JobSpec) -> JobResult: ...
