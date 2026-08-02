"""Allowlisted, deterministic repair-plan generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_STEPS = {
    "collect_market_quotes",
    "collect_news",
    "extract_web_content",
    "validate_market_data",
    "generate_content",
    "validate_content_consistency",
    "final_quality_gate",
    "reviewer_gate",
}

STEP_TO_FUNCTION = {
    "collect_market_quotes": "collect_market_data",
    "collect_news": "collect_news",
    "extract_web_content": "extract_web_content",
    "validate_market_data": "validate_market_data",
    "generate_content": "generate_content",
    "validate_content_consistency": "validate_content_consistency",
    "final_quality_gate": "final_quality_gate",
    "reviewer_gate": "final_quality_gate",
}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _next_index(root: Path) -> int:
    values = []
    for path in root.glob("repair_plan_*.json"):
        try:
            values.append(int(path.stem.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    return max(values, default=0) + 1


class RepairPlanner:
    def __init__(self, output_root: Path, max_attempts: int = 2) -> None:
        self.output_root = output_root
        self.max_attempts = max_attempts

    def build(
        self,
        *,
        run_id: str,
        trigger_error: str,
        gap: dict[str, Any],
        current_state: dict[str, Any] | None = None,
        selected_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        reset_steps = [step for step in gap.get("downstream_steps_to_reset", []) if step in ALLOWED_STEPS]
        repair_step = gap.get("repair_step")
        if repair_step in ALLOWED_STEPS and repair_step not in reset_steps:
            reset_steps.insert(0, repair_step)
        if not gap.get("repairable") or not reset_steps:
            raise ValueError("repair_plan_not_allowed_or_empty")

        state_steps = (current_state or {}).get("steps", {})
        preserved = [
            step for step, item in state_steps.items()
            if step not in reset_steps and isinstance(item, dict) and item.get("status") == "success"
        ]
        safe_tools = []
        for tool in selected_tools or []:
            name = str(tool)
            if name in STEP_TO_FUNCTION.values() and name not in safe_tools:
                safe_tools.append(name)
        if not safe_tools:
            safe_tools = [STEP_TO_FUNCTION[step] for step in reset_steps if step in STEP_TO_FUNCTION]

        root = self.output_root / run_id
        index = _next_index(root)
        plan = {
            "repair_id": f"repair_{run_id}_{index}",
            "run_id": run_id,
            "trigger_error": trigger_error,
            "affected_artifacts": list(gap.get("affected_artifacts", [])),
            "missing_fields": list(gap.get("missing_fields", [])),
            "selected_tools": safe_tools,
            "repair_scope": list(gap.get("repair_scope", [])),
            "preserved_steps": preserved,
            "reset_steps": reset_steps,
            "max_attempts": self.max_attempts,
            "status": "planned",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(root / f"repair_plan_{index}.json", plan)
        return plan


__all__ = ["ALLOWED_STEPS", "RepairPlanner"]
