"""Bounded agent loop for the market-content workflow.

The loop is deliberately a controller, not an unrestricted code agent.  It
can observe state, choose the next allowlisted step, and record a re-plan, but
it cannot invent tools, bypass gates, or execute delivery.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field


class AgentAction(str, Enum):
    execute = "execute"
    replan = "replan"
    skip = "skip"
    wait = "wait"
    stop = "stop"


class AgentLoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: str = "controlled_agent"
    max_iterations: int = Field(default=30, ge=1)
    max_tool_calls: int = Field(default=30, ge=1)
    max_runtime_seconds: int = Field(default=1200, ge=1)
    max_replans: int = Field(default=8, ge=0)
    max_stagnant_iterations: int = Field(default=2, ge=0)
    allow_dynamic_step_order: bool = True
    allow_skip_optional_steps: bool = True
    mandatory_gates: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)
    human_approval_required_for: list[str] = Field(default_factory=list)
    stop_on_unknown_failure: bool = True


class AgentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    iteration: int = Field(ge=0)
    current_step: str | None = None
    candidate_steps: list[str] = Field(default_factory=list)
    step_statuses: dict[str, str] = Field(default_factory=dict)
    last_step: str | None = None
    last_status: str | None = None
    last_error: dict[str, Any] | None = None
    tool_calls: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    iteration: int = Field(ge=0)
    action: AgentAction
    selected_step: str | None = None
    selected_tool: str | None = None
    reason: str
    mandatory: bool = False
    requires_human_approval: bool = False
    blocked: bool = False
    created_at: datetime


_ALIASES = {
    "collect_market_data": "collect_market_quotes",
    "validate_market_data": "collect_market_quotes",
    "validate_content_consistency": "generate_content",
    "final_quality_gate": "final_validation",
}

_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "health_check": (),
    "collect_github": ("health_check",),
    "collect_sources": ("health_check",),
    "collect_market_quotes": ("health_check", "collect_sources"),
    "generate_content": ("collect_sources", "collect_market_quotes"),
    "final_validation": ("generate_content",),
    "build_review_package": ("final_validation",),
    "reviewer_agent": ("build_review_package",),
    "reviewer_gate": ("reviewer_agent", "final_validation"),
    "offline_evaluation": ("reviewer_gate",),
    "archive": ("offline_evaluation",),
}


def load_agent_policy(path: Path) -> AgentLoopConfig:
    """Load policy without allowing a malformed policy to weaken safety."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AgentLoopConfig.model_validate(payload)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"agent_policy_unreadable:{type(exc).__name__}") from exc


class ControlledAgentLoop:
    """Observe state and select the next safe step under explicit budgets."""

    def __init__(self, config: AgentLoopConfig, *, audit_path: Path | None = None, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        self.audit_path = audit_path
        self.started = time.monotonic()
        self.iteration = 0
        self.replans = 0
        self.tool_calls = 0
        self.stagnant_iterations = 0
        self._last_signature: tuple[str, ...] | None = None

    @property
    def mandatory_steps(self) -> set[str]:
        return {_ALIASES.get(step, step) for step in self.config.mandatory_gates}

    def observe(
        self,
        state: dict[str, Any],
        candidates: Iterable[str],
        *,
        last_step: str | None = None,
        last_status: str | None = None,
        last_error: dict[str, Any] | None = None,
    ) -> AgentObservation:
        statuses = {name: str((state.get("steps") or {}).get(name, {}).get("status", "pending")) for name in candidates}
        return AgentObservation(
            run_id=self.run_id,
            iteration=self.iteration,
            current_step=state.get("current_step"),
            candidate_steps=list(candidates),
            step_statuses=statuses,
            last_step=last_step,
            last_status=last_status,
            last_error=last_error,
            tool_calls=self.tool_calls,
            replans=self.replans,
        )

    def select_next_step(
        self,
        state: dict[str, Any],
        candidates: Iterable[str],
        *,
        last_step: str | None = None,
        last_status: str | None = None,
        last_error: dict[str, Any] | None = None,
    ) -> AgentDecision:
        self.iteration += 1
        candidate_list = list(dict.fromkeys(candidates))
        observation = self.observe(state, candidate_list, last_step=last_step, last_status=last_status, last_error=last_error)
        statuses = observation.step_statuses
        if not self.config.enabled:
            return self._record(AgentDecision(run_id=self.run_id, iteration=self.iteration, action=AgentAction.stop, reason="agent_loop_disabled", created_at=_now()))
        if self.iteration > self.config.max_iterations:
            return self._record(self._stop("max_iterations_exceeded"))
        if self.tool_calls >= self.config.max_tool_calls:
            return self._record(self._stop("max_tool_calls_exceeded"))
        if time.monotonic() - self.started > self.config.max_runtime_seconds:
            return self._record(self._stop("max_runtime_seconds_exceeded"))

        pending = [step for step in candidate_list if statuses.get(step) not in {"success", "skipped"}]
        if not pending:
            return self._record(AgentDecision(run_id=self.run_id, iteration=self.iteration, action=AgentAction.stop, reason="all_steps_complete", created_at=_now()))

        signature = tuple(pending)
        if signature == self._last_signature:
            self.stagnant_iterations += 1
        else:
            self.stagnant_iterations = 0
        self._last_signature = signature
        if self.stagnant_iterations > self.config.max_stagnant_iterations:
            return self._record(self._stop("stagnant_plan"))

        ready = [step for step in pending if self._dependencies_ready(step, statuses)]
        if not ready:
            return self._record(self._stop("unsatisfied_step_dependencies"))
        selected = ready[0]
        mandatory = selected in self.mandatory_steps
        action = AgentAction.replan if last_status in {"failed", "timeout"} and last_step else AgentAction.execute
        reason = "mandatory_gate" if mandatory else "dependency_ready"
        if action is AgentAction.replan:
            self.replans += 1
            if self.replans > self.config.max_replans:
                return self._record(self._stop("max_replans_exceeded"))
            reason = f"replan_after_{last_status}:{last_step}"
        decision = AgentDecision(
            run_id=self.run_id,
            iteration=self.iteration,
            action=action,
            selected_step=selected,
            selected_tool=self._selected_tool(state, selected),
            reason=reason,
            mandatory=mandatory,
            requires_human_approval=selected in self.config.human_approval_required_for,
            created_at=_now(),
        )
        return self._record(decision)

    def register_tool_calls(self, count: int = 1) -> bool:
        if count < 0:
            raise ValueError("tool_call_count_must_be_nonnegative")
        self.tool_calls += count
        # FunctionExecutor remains the execution-time enforcement point.  The
        # loop observes the overage and stops before selecting another step;
        # returning a boolean keeps the current function chain fail-closed
        # without turning a budget rejection into an uncaught exception.
        return self.tool_calls <= self.config.max_tool_calls

    def tool_allowed(self, tool_name: str) -> bool:
        """Return whether a proposed tool is permitted by the agent policy."""
        return bool(tool_name.strip()) and tool_name not in set(self.config.blocked_tools)

    def assert_tool_allowed(self, tool_name: str) -> None:
        if not self.tool_allowed(tool_name):
            raise PermissionError(f"agent_tool_blocked:{tool_name or 'empty'}")

    def _dependencies_ready(self, step: str, statuses: dict[str, str]) -> bool:
        return all(statuses.get(dep) in {"success", "skipped"} for dep in _DEPENDENCIES.get(step, ()))

    @staticmethod
    def _selected_tool(state: dict[str, Any], step: str) -> str | None:
        plan = state.get("execution_plan") or state.get("plan") or {}
        for item in plan.get("steps", []):
            if isinstance(item, dict) and item.get("step") in {step, *[key for key, value in _ALIASES.items() if value == step]}:
                return str(item.get("selected_tool") or "") or None
        return None

    def _stop(self, reason: str) -> AgentDecision:
        return AgentDecision(run_id=self.run_id, iteration=self.iteration, action=AgentAction.stop, reason=reason, blocked=True, created_at=_now())

    def _record(self, decision: AgentDecision) -> AgentDecision:
        if self.audit_path is not None:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return decision


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AgentAction",
    "AgentDecision",
    "AgentLoopConfig",
    "AgentObservation",
    "ControlledAgentLoop",
    "load_agent_policy",
]
