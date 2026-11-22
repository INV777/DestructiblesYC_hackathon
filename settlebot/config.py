"""Load config.yaml and .env. No third-party dotenv dependency."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into os.environ without overriding existing values."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("limits", {})
    cfg["limits"].setdefault("max_cost_inr", 50.0)
    cfg["limits"].setdefault("max_time_seconds", 600)
    return cfg


def env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
