from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import AgentState


@dataclass
class FinishResult:
    finished: bool
    result: dict[str, Any] | None = None
    missing_conditions: list[str] | None = None


class FinishPolicy:
    """Fail-closed completion policy based on evidence, not step count."""

    def __init__(self, required_sections: int = 15) -> None:
        self.required_sections = required_sections

    def evaluate(self, state: AgentState) -> FinishResult:
        missing: list[str] = []
        if self._required_sections(state) < self.required_sections:
            missing.append("required_sections")
        if not self._market_data_complete(state):
            missing.append("market_data_complete")
        if not self._schema_valid(state):
            missing.append("schema_valid")
        if not self._grounding_valid(state):
            missing.append("grounding_valid")
        if not self._review_approved(state):
            missing.append("review_approved")
        if self._high_severity_issue_count(state) > 0:
            missing.append("high_severity_issues")
        if not self._report_generated(state):
            missing.append("report_generated")
        if missing:
            return FinishResult(finished=False, missing_conditions=missing)
        return FinishResult(finished=True, result={"status": "completed", "delivered": False})

    def _signals(self, state: AgentState) -> dict[str, Any]:
        signals: dict[str, Any] = {}
        if isinstance(state.final_result, dict):
            signals.update(state.final_result)
        for item in reversed(state.observations):
            if isinstance(item, dict):
                data = item.get("result") if isinstance(item.get("result"), dict) else item
                if isinstance(data, dict):
                    for key in ("required_sections", "market_data_complete", "schema_valid", "grounding_valid", "review_approved", "report_generated"):
                        if key in data and key not in signals:
                            signals[key] = data[key]
        return signals

    def _required_sections(self, state: AgentState) -> int:
        signals = self._signals(state)
        if isinstance(signals.get("sections"), list):
            return len(signals["sections"])
        try:
            return int(signals.get("required_sections", 0))
        except (TypeError, ValueError):
            return 0

    def _market_data_complete(self, state: AgentState) -> bool:
        return bool(self._signals(state).get("market_data_complete", False))

    def _schema_valid(self, state: AgentState) -> bool:
        return bool(self._signals(state).get("schema_valid", False))

    def _grounding_valid(self, state: AgentState) -> bool:
        return bool(self._signals(state).get("grounding_valid", False))

    def _review_approved(self, state: AgentState) -> bool:
        if bool(self._signals(state).get("review_approved", False)):
            return True
        return any(item.get("decision") == "approve" for item in state.review_feedback if isinstance(item, dict))

    def _high_severity_issue_count(self, state: AgentState) -> int:
        issues: list[dict[str, Any]] = []
        for item in state.review_feedback:
            if isinstance(item, dict):
                issues.extend(item.get("issues", []))
        issues.extend(item for item in state.conflicts if isinstance(item, dict))
        return sum(1 for item in issues if str(item.get("severity", "")).lower() in {"high", "critical"})

    def _report_generated(self, state: AgentState) -> bool:
        return bool(self._signals(state).get("report_generated", False))
