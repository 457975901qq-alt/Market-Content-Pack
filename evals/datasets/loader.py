from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_dataset(path: Path) -> list[dict[str, Any]]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    cases: list[dict[str, Any]] = []
    for item in files:
        if item.name == "dataset.json" or item.name.endswith(".case.json"):
            data = json.loads(item.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("cases"), list):
                cases.extend(data["cases"])
            elif isinstance(data, dict):
                cases.append(data)
    return cases


def dataset_version(path: Path, cases: list[dict[str, Any]]) -> str:
    data = path / "dataset.json" if path.is_dir() else path
    if data.exists():
        return json.loads(data.read_text(encoding="utf-8")).get("dataset_version", "market_content_v1")
    return "market_content_v1"
