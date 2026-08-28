from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from run_state import atomic_write_json

from .action import AgentAction
from .finish_policy import FinishPolicy
from .planner import AgentPlanner
from .state import AgentState


class AgentCheckpointStore:
    """JSON checkpoint plus optional SQLite audit, without replacing run_state."""

    def __init__(self, path: Path, *, sqlite_index: Any | None = None) -> None:
        self.path = path
        self.sqlite_index = sqlite_index

    def save(self, state: AgentState) -> None:
        state.refresh_hash()
        payload = state.checkpoint_payload()
        atomic_write_json(self.path, payload)
        if self.sqlite_index is not None:
            self.sqlite_index.audit(state.run_id or "unknown", "agent_checkpoint", {
                "step": state.step_count,
                "action": state.current_action.model_dump(mode="json") if state.current_action else None,
                "state_hash": payload["state_hash"],
            })

    def load(self) -> AgentState:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        state = AgentState.model_validate(payload)
        expected = state.compute_hash(payload)
        if payload.get("state_hash") and payload["state_hash"] != expected:
            raise ValueError("agent_checkpoint_hash_mismatch")
        return state


class DailyMarketAgent:
    """Generic Agent V1 controller; all actions go through ToolExecutor."""

    def __init__(self, planner: AgentPlanner, executor: Any, finish_policy: FinishPolicy, checkpoint_store: AgentCheckpointStore | None = None, recovery_policy: Any | None = None) -> None:
        self.planner = planner
        self.executor = executor
        self.finish_policy = finish_policy
        self.checkpoint_store = checkpoint_store
        self.recovery_policy = recovery_policy

    def run(self, goal: str, initial_state: AgentState | None = None) -> AgentState:
        state = initial_state or AgentState(goal=goal)
        if not state.plan:
            state.plan = self.planner.create_initial_plan(state)
        while True:
            if state.step_count >= state.max_steps:
                return self._block(state, "max_steps_exceeded")
            finish = self.finish_policy.evaluate(state)
            if finish.finished:
                state.status = "finished"
                state.final_result = finish.result
                self._checkpoint(state)
                return state
            action = self.planner.next_action(state)
            if action is None:
                if isinstance(state.failure, dict) and state.failure.get("failure_category"):
                    return self._block(state, f"non_retryable_failure:{state.failure['failure_category']}")
                return self._block(state, "no_valid_action")
            if action.tool_name in {"deliver", "canary_deliver", "shell", "exec_shell"}:
                return self._block(state, f"blocked_tool:{action.tool_name}")
            state.current_action = action
            state.step_count += 1
            state.decisions.append({"step": state.step_count, "action": action.model_dump(mode="json")})
            observation = self.executor.execute(action)
            state.tool_history.append({"action": action.model_dump(mode="json"), "observation": observation})
            # Failures can carry missing fields, conflicts or provider hints;
            # feed those observations to the Planner before deciding whether
            # to retry or re-plan.
            state.apply_observation(observation)
            if observation.get("success"):
                state.completed_actions.append(action.model_dump(mode="json"))
            else:
                self._handle_failure(state, action, observation)
                if state.status == "blocked":
                    self._checkpoint(state)
                    return state
                state.plan = self.planner.replan(state)
            state.current_action = None
            self._checkpoint(state)

    @staticmethod
    def _update_state_from_observation(state: AgentState, observation: dict[str, Any]) -> None:
        result = observation.get("result") if isinstance(observation.get("result"), dict) else observation
        if not isinstance(result, dict):
            return
        state.available_evidence.extend(result.get("evidence", []) if isinstance(result.get("evidence"), list) else [])
        state.missing_information = [str(item) for item in result.get("missing_information", [])] if isinstance(result.get("missing_information"), list) else state.missing_information
        state.conflicts = list(result.get("conflicts", [])) if isinstance(result.get("conflicts"), list) else state.conflicts
        if isinstance(result.get("review_feedback"), list) and (
            observation.get("tool_name") == "review_content" or result.get("review_feedback")
        ):
            # Reviewer output is a current snapshot, not an append-only list;
            # otherwise an old reject would keep re-triggering repair after a
            # later approve.
            state.review_feedback = list(result["review_feedback"])
            decisions = {str(item.get("decision")) for item in state.review_feedback if isinstance(item, dict)}
            state.review_recheck_required = bool(decisions & {"reject", "needs_revision"})
            if "approve" in decisions:
                state.review_recheck_required = False
        for key in ("market_data_complete", "schema_valid", "grounding_valid", "review_approved", "report_generated", "required_sections"):
            if key in result:
                state.final_result = {**(state.final_result or {}), key: result[key]}

    def _handle_failure(self, state: AgentState, action: AgentAction, observation: dict[str, Any]) -> None:
        state.retry_count += 1
        if state.retry_count > state.retry_budget:
            state.status = "blocked"
            state.failure_reason = "retry_budget_exceeded"

    def _block(self, state: AgentState, reason: str) -> AgentState:
        state.status = "blocked"
        state.failure_reason = reason
        self._checkpoint(state)
        return state

    def _checkpoint(self, state: AgentState) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(state)
