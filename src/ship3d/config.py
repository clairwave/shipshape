"""ship3d config: repo root + comfy endpoint."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8189")
