"""Controlled execution planning for the existing resumable pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_state import atomic_write_json
from tool_router import ToolRouter


PLAN_VERSION = "controlled-planner-v1"
PLAN_STEPS = (
    "health_check",
    "collect_github",
    "collect_news",
    "extract_web_content",
    "collect_market_data",
    "validate_market_data",
    "generate_content",
    "validate_content_consistency",
    "final_quality_gate",
    "build_review_package",
    "reviewer_agent",
    "reviewer_gate",
    "offline_evaluation",
    "archive",
)
MANDATORY_GATES = ("validate_market_data", "validate_content_consistency", "final_quality_gate")
EXECUTOR_STEP_ALIASES = {
    "collect_news": "collect_sources",
    "extract_web_content": "collect_sources",
    "collect_market_data": "collect_market_quotes",
    "validate_market_data": "collect_market_quotes",
    "validate_content_consistency": "generate_content",
    "final_quality_gate": "final_validation",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPlanner:
    """Build plans from a fixed step set; no arbitrary command is emitted."""

    def __init__(self, router: ToolRouter) -> None:
        self.router = router

    def build(
        self,
        *,
        run_id: str,
        edition: str,
        state: dict[str, Any],
        preferred_provider: str | None = None,
        prior_plan: dict[str, Any] | None = None,
        text_only: bool = True,
    ) -> dict[str, Any]:
        old_steps = {item.get("step"): item for item in (prior_plan or {}).get("steps", []) if isinstance(item, dict)}
        provider_decision = self.router.choose(
            "content_model",
            preferred=preferred_provider,
            previous_tool=(prior_plan or {}).get("selected_provider"),
        )
        market_primary = self.router.choose("market_primary", previous_tool=(prior_plan or {}).get("market_primary"))
        market_secondary = self.router.choose("market_secondary", previous_tool=(prior_plan or {}).get("market_secondary"))
        news = self.router.choose("news_discovery", previous_tool=(prior_plan or {}).get("news_discovery"))
        extraction = self.router.choose("web_extraction", previous_tool=(prior_plan or {}).get("web_extraction"))
        selected_provider = provider_decision["selected_tool"]
        selected_steps: list[dict[str, Any]] = []
        state_steps = state.get("steps") or {}
        for step in PLAN_STEPS:
            previous = old_steps.get(step, {})
            executor_alias = EXECUTOR_STEP_ALIASES.get(step, step)
            status = (state_steps.get(executor_alias) or {}).get("status", "pending")
            if step == "final_quality_gate":
                selected_tool = "final_quality_gate"
                logical = "final_quality_gate"
                executor_step = "final_validation"
                reason = "mandatory quality gate; cannot be skipped"
            elif step == "collect_news":
                selected_tool = news["selected_tool"]
                logical = step
                executor_step = "collect_sources"
                reason = news["selected_reason"]
            elif step == "extract_web_content":
                selected_tool = extraction["selected_tool"]
                logical = step
                executor_step = "collect_sources"
                reason = extraction["selected_reason"]
            elif step == "collect_market_data":
                selected_tool = market_primary["selected_tool"]
                logical = step
                executor_step = "collect_market_quotes"
                reason = market_primary["selected_reason"]
            elif step == "validate_market_data":
                selected_tool = "deterministic_validator"
                logical = step
                executor_step = "collect_market_quotes"
                reason = "market artifact validation is mandatory"
            elif step == "generate_content":
                selected_tool = selected_provider
                logical = step
                executor_step = step
                reason = provider_decision["selected_reason"]
            elif step == "validate_content_consistency":
                selected_tool = "deterministic_validator"
                logical = step
                executor_step = "generate_content"
                reason = "content/data consistency validation is mandatory"
            elif step == "build_review_package":
                selected_tool = "deterministic_review_package"
                logical = step
                executor_step = step
                reason = "fixed local artifact builder"
            elif step == "reviewer_agent":
                selected_tool = "reviewer_agent"
                logical = step
                executor_step = step
                reason = "independent reviewer stage"
            elif step == "reviewer_gate":
                selected_tool = "reviewer_gate"
                logical = step
                executor_step = step
                reason = "review approval required before delivery"
            elif step == "archive":
                selected_tool = "local_archive"
                logical = step
                executor_step = step
                reason = "write local text artifacts only"
            else:
                selected_tool = previous.get("selected_tool") or ("github" if step == "collect_github" else "deterministic_executor")
                logical = step
                executor_step = step
                reason = previous.get("reason") or "fixed allowlisted pipeline step"
            selected_steps.append({
                "step": logical,
                "executor_step": executor_step,
                "selected_tool": selected_tool,
                "fallback_chain": previous.get("fallback_chain") or [],
                "reason": reason,
                "status": "success" if status == "success" else "pending",
                "mandatory": step in {
                    "collect_market_data",
                    "validate_market_data",
                    "generate_content",
                    "validate_content_consistency",
                    "final_quality_gate",
                },
            })

        constraints = {
            "max_steps": int(self.router.policy.get("max_steps", 15)),
            "max_tool_calls": int(self.router.policy.get("max_tool_calls", 30)),
            "max_runtime_seconds": int(self.router.policy.get("max_runtime_seconds", 1200)),
            "allow_delivery": False,
        }
        return {
            "run_id": run_id,
            "edition": edition,
            "goal": "generate_market_content_pack",
            "steps": selected_steps,
            "mandatory_gates": list(MANDATORY_GATES),
            "selected_provider": selected_provider,
            "market_primary": market_primary["selected_tool"],
            "market_secondary": market_secondary["selected_tool"],
            "news_discovery": news["selected_tool"],
            "news_fallback_chain": news.get("fallback_chain", []),
            "web_extraction": extraction["selected_tool"],
            "web_fallback_chain": extraction.get("fallback_chain", []),
            "text_only": text_only,
            "constraints": constraints,
            "created_at": _now(),
            "planner_version": PLAN_VERSION,
            "resumed_from_plan": bool(prior_plan),
            "replanned_steps": [item["step"] for item in selected_steps if item["status"] == "pending"],
            "status": "planned",
        }

    @staticmethod
    def write(path: Path, plan: dict[str, Any]) -> None:
        atomic_write_json(path, plan)


def executor_step_for(step: str) -> str:
    """Translate a logical plan step to the existing state-machine step."""
    return EXECUTOR_STEP_ALIASES.get(step, step)


__all__ = ["EXECUTOR_STEP_ALIASES", "ExecutionPlanner", "MANDATORY_GATES", "PLAN_STEPS", "PLAN_VERSION", "executor_step_for"]
