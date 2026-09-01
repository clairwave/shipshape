"""Headless ComfyUI client: queue an API-format workflow, wait via websocket, fetch outputs.

Works against any ComfyUI reachable over HTTP — local instance or a pod with the
port exposed. Workflows are the API-format JSON exported from the ComfyUI UI
("Export (API)") and stored in workflows/.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from ship3d.config import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / "workflows"


def load_workflow(name: str) -> dict[str, Any]:
    return json.loads((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def apply_overrides(workflow: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Override node inputs by 'node_id.input_name' (e.g. '6.text', '31.seed').

    Node titles also work: 'PositivePrompt.text' matches the node whose _meta.title
    is 'PositivePrompt' — title your key nodes in the UI so workflows stay editable
    without breaking the orchestrator.
    """
    wf = json.loads(json.dumps(workflow))  # deep copy
    titles = {
        node.get("_meta", {}).get("title"): node_id
        for node_id, node in wf.items()
        if node.get("_meta", {}).get("title")
    }
    for key, value in overrides.items():
        node_ref, _, input_name = key.rpartition(".")
        node_id = titles.get(node_ref, node_ref)
        if node_id not in wf:
            raise KeyError(f"workflow has no node '{node_ref}' (from override '{key}')")
        wf[node_id]["inputs"][input_name] = value
    return wf


class ComfyClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = str(uuid.uuid4())

    async def upload_image(self, path: str | Path, server_name: str | None = None) -> str:
        """Upload an input file; returns server-side filename.

        server_name MUST be unique per job for concurrent pipelines: uploads
        land in one shared input dir and prompts read the file at EXECUTION
        time, so identical basenames from concurrent jobs overwrite each other
        and cross-contaminate whichever prompt runs later."""
        path = Path(path)
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{self.base_url}/upload/image",
                files={"image": (server_name or path.name, path.read_bytes())},
                data={"overwrite": "true"},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["name"]

    async def submit(self, workflow: dict[str, Any]) -> str:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": self.client_id},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["prompt_id"]

    async def _in_history(self, prompt_id: str) -> bool:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{self.base_url}/history/{prompt_id}", timeout=30)
            resp.raise_for_status()
            return prompt_id in resp.json()

    async def _in_queue(self, prompt_id: str) -> bool:
        """Is the prompt still queued or executing? Used to tell 'slow' apart
        from 'lost to a server restart'."""
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{self.base_url}/queue", timeout=30)
            resp.raise_for_status()
            q = resp.json()
            return any(item[1] == prompt_id
                       for item in q.get("queue_running", []) + q.get("queue_pending", []))

    async def _wait_on_ws(self, ws, prompt_id: str, timeout_s: float) -> None:
        """Wait for completion on an already-open websocket, with /history polling as
        a fallback — a fast (cached) job can finish before its events are observed."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    if await self._in_history(prompt_id):
                        return
                except httpx.HTTPError:
                    pass  # server busy in a blocking model load — keep waiting
                if time.monotonic() > deadline:
                    raise TimeoutError(f"prompt {prompt_id} not finished after {timeout_s}s")
                continue
            except websockets.exceptions.ConnectionClosed:
                # a long synchronous node can block the server loop so hard the
                # websocket dies — degrade to pure history polling
                gone = 0
                while time.monotonic() <= deadline:
                    try:
                        if await self._in_history(prompt_id):
                            return
                        # A restarted server loses both queue and history. If the
                        # prompt is in neither, it will never complete: fail now
                        # so the caller can resume, instead of polling for hours.
                        # Two consecutive misses guard against a transient blip.
                        if not await self._in_queue(prompt_id):
                            gone += 1
                            if gone >= 2:
                                raise RuntimeError(
                                    f"prompt {prompt_id} vanished (server restart?)")
                        else:
                            gone = 0
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(10)
                raise TimeoutError(f"prompt {prompt_id} not finished after {timeout_s}s")
            if isinstance(raw, bytes):  # preview frames
                continue
            msg = json.loads(raw)
            data = msg.get("data", {})
            if data.get("prompt_id") != prompt_id:
                continue
            if msg.get("type") == "executing" and data.get("node") is None:
                return
            if msg.get("type") == "execution_error":
                raise RuntimeError(
                    f"workflow error in node {data.get('node_type')}: "
                    f"{data.get('exception_message')}"
                )

    async def fetch_outputs(self, prompt_id: str, out_dir: str | Path) -> list[Path]:
        """Download all image/video/audio outputs recorded in history for this prompt."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{self.base_url}/history/{prompt_id}", timeout=60)
            resp.raise_for_status()
            history = resp.json()[prompt_id]
            for node_output in history["outputs"].values():
                for kind in ("images", "gifs", "videos", "audio"):
                    for item in node_output.get(kind, []):
                        params = {
                            "filename": item["filename"],
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                        }
                        data = await http.get(
                            f"{self.base_url}/view", params=params, timeout=300
                        )
                        data.raise_for_status()
                        dest = out_dir / item["filename"]
                        dest.write_bytes(data.content)
                        saved.append(dest)
        return saved

    async def run(
        self, workflow: dict[str, Any], out_dir: str | Path, timeout_s: float = 28800
    ) -> list[Path]:
        # 8 h ceiling: long InfiniteTalk dubs queue behind interleaved jobs and
        # can wait hours before their own ~2 h render even starts.
        # (Old 3 h ceiling: 14B/1080p/121f production takes run ~2 h on an H100
        # (341 s/step measured) — the old 1 h default orphaned live renders
        # Websocket must be open BEFORE submitting: a cached prompt can complete in
        # milliseconds and its completion event would otherwise be missed forever.
        ws_url = self.base_url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}"
        # transient connect failures (busy server) shouldn't kill hour-long
        # batch scripts — retry the handshake before giving up
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                async with websockets.connect(ws_url, max_size=None,
                                              ping_timeout=None, open_timeout=60) as ws:
                    prompt_id = await self.submit(workflow)
                    await self._wait_on_ws(ws, prompt_id, timeout_s)
                return await self.fetch_outputs(prompt_id, out_dir)
            except (OSError, asyncio.TimeoutError) as e:
                last_exc = e
                await asyncio.sleep(15 * (attempt + 1))
        raise last_exc
