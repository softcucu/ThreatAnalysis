"""Runtime configuration loading helpers."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.model_router import RuntimeConfig


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Load agent runtime configuration from a JSON file."""

    with Path(path).open("r", encoding="utf-8") as fh:
        return RuntimeConfig.from_dict(json.load(fh))
