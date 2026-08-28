from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolObservation(BaseModel):
    """Small, serializable observation contract shared by tools and the Agent."""

    model_config = ConfigDict(extra="allow")

    success: bool
    tool_name: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    review_feedback: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
