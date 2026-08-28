from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .action import AgentAction
from .observation import ToolObservation


class AgentState(BaseModel):
    """Serializable Agent state shared by Planner, Controller and checkpoints."""

    model_config = ConfigDict(extra="allow")

    goal: str = Field(min_length=1)
    run_id: str | None = None
    edition: str | None = None
    timezone_name: str = "Asia/Tokyo"
    cutoff_at: datetime | None = None
    status: str = "running"
    plan: list[AgentAction] = Field(default_factory=list)
    completed_actions: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    available_evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    tool_history: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    review_feedback: list[dict[str, Any]] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    retry_budget: int = Field(default=3, ge=0)
    step_count: int = Field(default=0, ge=0)
    max_steps: int = Field(default=50, ge=1)
    current_action: AgentAction | None = None
    final_result: dict[str, Any] | None = None
    failure_reason: str | None = None
    failure: dict[str, Any] | None = None
    failure_history: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_version: str = "agent-v1"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state_hash: str | None = None

    def checkpoint_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["state_hash"] = self.compute_hash(payload)
        return payload

    def compute_hash(self, payload: dict[str, Any] | None = None) -> str:
        value = dict(payload or self.model_dump(mode="json"))
        value.pop("state_hash", None)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def refresh_hash(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
        payload = self.model_dump(mode="json")
        self.state_hash = self.compute_hash(payload)

    def apply_observation(self, observation: ToolObservation | dict[str, Any]) -> None:
        """Merge the latest tool evidence without retaining resolved defects."""
        value = observation.to_dict() if isinstance(observation, ToolObservation) else dict(observation)
        self.observations.append(value)
        result = value.get("data") if isinstance(value.get("data"), dict) else value.get("result")
        if not isinstance(result, dict):
            result = value

        failure = result.get("failure")
        if isinstance(failure, dict):
            self.failure = dict(failure)
            self.failure_history.append(dict(failure))
        elif value.get("success") is True:
            self.failure = None

        for key in ("evidence", "available_evidence"):
            items = result.get(key)
            if isinstance(items, list):
                existing = {repr(item) for item in self.available_evidence}
                self.available_evidence.extend(item for item in items if repr(item) not in existing)

        for key, target in (("missing_information", "missing_information"), ("conflicts", "conflicts")):
            if key in result and isinstance(result[key], list):
                setattr(self, target, list(result[key]))

        if "review_feedback" in result and isinstance(result["review_feedback"], list) and (
            value.get("tool_name") == "review_content" or result["review_feedback"]
        ):
            self.review_feedback = list(result["review_feedback"])
            decisions = {str(item.get("decision")) for item in self.review_feedback if isinstance(item, dict)}
            self.review_recheck_required = bool(decisions & {"reject", "needs_revision"})
            if "approve" in decisions:
                self.review_recheck_required = False

        for key in ("market_data_complete", "schema_valid", "grounding_valid", "review_approved", "report_generated", "required_sections"):
            if key in result:
                self.final_result = {**(self.final_result or {}), key: result[key]}
