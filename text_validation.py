"""Shared deterministic validation for the text-only market pack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def validate_text_artifacts(
    content_path: Path,
    *,
    expected_edition: str | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    critical_errors: list[str] = []

    def check(name: str, passed: bool, expected: Any, actual: Any, artifact: Path) -> None:
        result = "pass" if passed else "fail"
        checks.append({"check_name": name, "result": result, "expected": expected, "actual": actual, "artifact": str(artifact)})
        if not passed:
            critical_errors.append(name)

    content_exists = content_path.exists() and content_path.is_file() and content_path.stat().st_size > 0
    check("content_file", content_exists, "non-empty JSON file", content_exists, content_path)

    content: dict[str, Any] = {}
    if content_exists:
        try:
            raw = json.loads(content_path.read_text(encoding="utf-8"))
            content = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            content = {}
    check("content_json", bool(content), "JSON object", bool(content), content_path)
    if expected_edition is not None:
        check("edition", content.get("edition") == expected_edition, expected_edition, content.get("edition"), content_path)

    status = "pass" if not critical_errors else "fail"
    return {
        "status": status,
        "mode": "text",
        "critical_errors": critical_errors,
        "checks": checks,
    }


__all__ = ["validate_text_artifacts"]
