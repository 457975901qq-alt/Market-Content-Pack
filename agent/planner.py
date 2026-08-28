from __future__ import annotations

from typing import Any

from .action import AgentAction
from .state import AgentState


class AgentPlanner:
    """Planner contract used by the Controller."""

    def create_initial_plan(self, state: AgentState) -> list[AgentAction]:
        raise NotImplementedError

    def next_action(self, state: AgentState) -> AgentAction | None:
        raise NotImplementedError

    def replan(self, state: AgentState) -> list[AgentAction]:
        raise NotImplementedError


class RuleBasedAgentPlanner(AgentPlanner):
    """Deterministic safe planner used when a model planner is unavailable.

    The initial plan is intentionally incomplete. Missing data, conflicts,
    reviewer feedback and provider failures add actions at runtime.
    """

    def __init__(self, *, provider: str = "ollama") -> None:
        self.provider = provider

    def create_initial_plan(self, state: AgentState) -> list[AgentAction]:
        return [
            self._action("collect_news", "collect source evidence", "normalized source evidence"),
            self._action("collect_market_data", "collect validated market data", "market data artifact"),
            self._action("generate_content", "generate the market content pack", "structured content pack"),
            self._action("final_quality_gate", "run the mandatory final quality gate", "quality gate pass"),
        ]

    def next_action(self, state: AgentState) -> AgentAction | None:
        completed = {str(item.get("tool_name") or item.get("action_id")) for item in state.completed_actions if isinstance(item, dict)}
        failed = {str(item.get("tool_name")) for item in state.tool_history if isinstance(item, dict) and not (item.get("observation") or {}).get("success")}
        failure = state.failure if isinstance(state.failure, dict) else {}
        failure_category = str(failure.get("failure_category") or failure.get("category") or "")
        if failure_category in {"market_data_future", "market_data_stale", "market_data_not_validated"}:
            # A stale/future artifact cannot be repaired by repeating the same
            # snapshot. The collector must return a valid as-of observation;
            # without one, fail closed instead of relaxing temporal rules.
            return None
        if failure_category == "market_data_conflict" and "crosscheck_market_quote" not in completed:
            ticker = next(iter(failure.get("details", {}).get("symbols", [])), self._default_ticker(state))
            return self._action("crosscheck_market_quote", "cross-check the conflicting market quote", "resolved quote conflict", priority=10, arguments=self._market_arguments(state, {"symbol": ticker}))
        if state.conflicts:
            return self._action("crosscheck_market_quote", "a quote conflict remains unresolved", "resolved quote conflict", priority=10, arguments=self._market_arguments(state))
        if state.missing_information:
            missing = state.missing_information[0]
            if any(str((item.get("action") or {}).get("tool_name")) == "collect_market_data" and not (item.get("observation") or {}).get("success") for item in state.tool_history if isinstance(item, dict)):
                return self._action("crosscheck_market_quote", f"primary market data failed; crosscheck missing field: {missing}", "resolved market-data gap", priority=8, arguments=self._market_arguments(state))
            if missing.startswith("market"):
                failed_market = any(
                    str((item.get("action") or {}).get("tool_name")) == "collect_market_data"
                    and not (item.get("observation") or {}).get("success")
                    for item in state.tool_history if isinstance(item, dict)
                )
                if failed_market or "collect_market_data" in completed:
                    ticker = missing.split(":", 1)[-1]
                    return self._action(
                        "crosscheck_market_quote",
                        f"crosscheck unresolved market field: {missing}",
                        "resolved market-data gap",
                        priority=8,
                        arguments=self._market_arguments(state, {"symbol": ticker}),
                    )
                return self._action("collect_market_data", f"repair missing market field: {missing}", "complete market data", priority=10, arguments=self._market_arguments(state))
            return self._action("collect_news", f"search evidence for missing information: {missing}", "source evidence", priority=20)
        if any("provider" in f"{item.get('error_type', '')} {item.get('error', '')}".lower() for item in state.observations if isinstance(item, dict)) and "generate_content" not in completed:
            fallback = "gemini" if self.provider == "ollama" else "rule_template"
            return self._action("generate_content", f"provider failure; use controlled fallback {fallback}", "content generated with a recorded fallback", priority=15, arguments={"provider": fallback})
        if any("json" in f"{item.get('error_type', '')} {item.get('error', '')}".lower() for item in state.observations if isinstance(item, dict)) and "generate_content" not in completed:
            return self._action("generate_content", "structured output parse failed; use deterministic fallback", "schema-valid content", priority=15, arguments={"provider": "rule_template"})
        if state.review_feedback:
            recommended = self._recommended_action(state)
            if recommended and recommended.tool_name not in completed:
                return recommended
            if bool(getattr(state, "review_recheck_required", False)) and "review_content" in completed:
                return self._action("review_content", "re-review after the Reviewer-requested repair", "review approval", priority=5)
            if "review_content" not in completed and recommended:
                return recommended
        if "collect_news" not in completed:
            return self._action("collect_news", "source evidence is not complete", "source evidence")
        if "collect_market_data" not in completed:
            return self._action("collect_market_data", "market data is not complete", "market data", arguments=self._market_arguments(state))
        if bool(getattr(state, "require_market_validation", False)) and "validate_market_data" not in completed:
            return self._action("validate_market_data", "market data requires an explicit validation result", "validated market data")
        if "generate_content" not in completed:
            return self._action("generate_content", "content artifact is not complete", "content pack")
        if "validate_content_consistency" not in completed:
            return self._action("validate_content_consistency", "validate content against evidence", "schema and consistency result")
        if "final_quality_gate" not in completed:
            return self._action("final_quality_gate", "mandatory quality gate has not passed", "quality gate pass", priority=5)
        if "review_content" not in completed:
            return self._action("review_content", "independent review is required", "review decision", priority=5)
        if bool(getattr(state, "require_reviewer_gate", False)) and "reviewer_gate" not in completed:
            return self._action("reviewer_gate", "review approval must be verified before report persistence", "reviewer gate pass", priority=5)
        if "build_html_report" not in completed:
            return self._action("build_html_report", "build the visual delivery report", "HTML report")
        if "build_markdown_report" not in completed:
            return self._action("build_markdown_report", "build the text fallback report", "Markdown report")
        if "save_report" not in completed:
            return self._action("save_report", "persist the validated report", "saved report")
        return None

    @staticmethod
    def _default_ticker(state: AgentState) -> str:
        for item in reversed(state.tool_history):
            action = item.get("action") if isinstance(item, dict) else None
            if isinstance(action, dict):
                ticker = action.get("arguments", {}).get("ticker") or action.get("arguments", {}).get("symbol")
                if ticker:
                    return str(ticker)
        return "VOO"

    @staticmethod
    def _market_arguments(state: AgentState, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(arguments or {})
        if state.cutoff_at is not None:
            payload["as_of"] = state.cutoff_at.isoformat()
        return payload

    def replan(self, state: AgentState) -> list[AgentAction]:
        action = self.next_action(state)
        return [action] if action is not None else []

    def _recommended_action(self, state: AgentState) -> AgentAction | None:
        for feedback in reversed(state.review_feedback):
            for item in feedback.get("recommended_actions", []) if isinstance(feedback, dict) else []:
                if not isinstance(item, dict) or not item.get("tool"):
                    continue
                return self._action(str(item["tool"]), "follow independent Reviewer remediation", "review issue resolved", arguments=item.get("arguments") or {}, priority=10)
        return None

    @staticmethod
    def _action(tool_name: str, reason: str, expected: str, *, priority: int = 100, arguments: dict[str, Any] | None = None) -> AgentAction:
        return AgentAction(
            action_id=f"agent_{tool_name}",
            tool_name=tool_name,
            arguments=arguments or {},
            reason=reason,
            expected_result=expected,
            priority=priority,
        )
