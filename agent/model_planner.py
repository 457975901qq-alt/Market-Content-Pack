"""Safe model-assisted planning for Agent V1.

The model may suggest the next registered business function, but it never
gets a callable, shell capability, delivery control, or permission to bypass
the deterministic planner gates. Invalid or unavailable model output falls
back to ``RuleBasedAgentPlanner``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from .action import AgentAction
from .planner import AgentPlanner, RuleBasedAgentPlanner
from .state import AgentState


BLOCKED_TOOLS = {"deliver", "canary_deliver", "shell", "exec_shell"}
MANDATORY_ORDER = {
    "validate_content_consistency": {"generate_content"},
    "final_quality_gate": {"validate_content_consistency"},
    "review_content": {"final_quality_gate"},
    "reviewer_gate": {"review_content"},
    "build_html_report": {"reviewer_gate"},
    "build_markdown_report": {"reviewer_gate"},
    "save_report": {"build_html_report", "build_markdown_report"},
}


class PlannerModelError(RuntimeError):
    """Raised internally when model planning cannot produce a safe action."""


def _default_model_call(provider: str, prompt: str) -> str:
    if provider == "ollama":
        from model_providers import call_ollama

        return call_ollama(prompt, model=os.environ.get("AGENT_PLANNER_MODEL") or os.environ.get("OLLAMA_MODEL"))
    if provider == "gemini":
        from model_providers import call_gemini

        return call_gemini(prompt, model=os.environ.get("AGENT_PLANNER_MODEL") or os.environ.get("GEMINI_MODEL"))
    raise PlannerModelError(f"unsupported_planner_provider:{provider}")


class ModelAssistedAgentPlanner(AgentPlanner):
    """Hybrid planner: model suggestion first, deterministic fallback always.

    ``call_model`` is injectable for tests. Production calls use the existing
    provider adapters, so planner traces and credential redaction remain in
    the existing observability path.
    """

    def __init__(
        self,
        *,
        provider: str,
        allowed_tools: Iterable[str],
        fallback: RuleBasedAgentPlanner | None = None,
        call_model: Callable[[str], str] | None = None,
    ) -> None:
        self.provider = provider.strip().lower()
        self.allowed_tools = {str(item) for item in allowed_tools}
        self.fallback = fallback or RuleBasedAgentPlanner(provider=self.provider)
        self.call_model = call_model or (lambda prompt: _default_model_call(self.provider, prompt))
        self.last_error: str | None = None

    def create_initial_plan(self, state: AgentState) -> list[AgentAction]:
        return self.fallback.create_initial_plan(state)

    def next_action(self, state: AgentState) -> AgentAction | None:
        fallback_action = self.fallback.next_action(state)
        if self.provider not in {"ollama", "gemini"}:
            return fallback_action
        try:
            proposed = self._propose(state)
            if self._safe_action(proposed, state):
                self.last_error = None
                return proposed
            self.last_error = "planner_action_rejected"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}:{str(exc)[:200]}"
        return fallback_action

    def replan(self, state: AgentState) -> list[AgentAction]:
        action = self.next_action(state)
        return [action] if action is not None else []

    def _propose(self, state: AgentState) -> AgentAction:
        prompt = self._prompt(state)
        raw = self.call_model(prompt)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise PlannerModelError("planner_invalid_json") from exc
        if not isinstance(payload, dict):
            raise PlannerModelError("planner_payload_not_object")
        actions = payload.get("actions")
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
            raise PlannerModelError("planner_requires_one_action")
        candidate = dict(actions[0])
        tool_name = str(candidate.get("tool_name") or "").strip()
        if not tool_name:
            raise PlannerModelError("planner_tool_name_missing")
        candidate.setdefault("action_id", f"model_{tool_name}")
        candidate.setdefault("reason", "model-selected next registered action")
        candidate.setdefault("expected_result", "validated tool observation")
        candidate.setdefault("priority", 50)
        candidate.setdefault("arguments", {})
        return AgentAction.model_validate(candidate)

    def _safe_action(self, action: AgentAction, state: AgentState) -> bool:
        tool_name = action.tool_name
        if tool_name not in self.allowed_tools or tool_name in BLOCKED_TOOLS:
            return False
        completed = {
            str(item.get("tool_name") or item.get("action_id"))
            for item in state.completed_actions
            if isinstance(item, dict)
        }
        required = MANDATORY_ORDER.get(tool_name, set())
        if required and not required.issubset(completed):
            return False
        # Do not let a model repeatedly spend calls on a successful action.
        # Reviewer re-checks are the only intentional repeat in the core flow.
        if tool_name in completed and tool_name not in {"review_content", "validate_market_data"}:
            return False
        return True

    def _prompt(self, state: AgentState) -> str:
        completed = [
            str(item.get("tool_name") or item.get("action_id"))
            for item in state.completed_actions
            if isinstance(item, dict)
        ]
        recent = []
        for item in state.tool_history[-5:]:
            if not isinstance(item, dict):
                continue
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
            recent.append({
                "tool_name": action.get("tool_name"),
                "success": observation.get("success"),
                "error_type": observation.get("error_type"),
            })
        context = {
            "goal": state.goal,
            "registered_tools": sorted(self.allowed_tools),
            "edition": state.edition,
            "cutoff_at": state.cutoff_at.isoformat() if isinstance(state.cutoff_at, datetime) else None,
            "completed_tools": completed,
            "missing_information": [str(item)[:200] for item in state.missing_information[:10]],
            "conflicts": state.conflicts[:5],
            "review_feedback_present": bool(state.review_feedback),
            "recent_tool_results": recent,
        }
        return (
            "You are a constrained planner for Daily Market Agent V1. "
            "Choose exactly one tool from registered_tools only; never invent a tool name. "
            "Never choose delivery, shell, "
            "code/config changes, or a provider callable. Respect mandatory gate order. "
            "Return strict JSON only in the form "
            '{"actions":[{"tool_name":"...","arguments":{},"reason":"...",'
            '"expected_result":"...","priority":50}]}\n'
            "Planning context:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)
        )


__all__ = ["ModelAssistedAgentPlanner", "PlannerModelError"]
