from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentAction(BaseModel):
    """A structured, allowlisted action proposed by the Agent Planner."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    expected_result: str = ""
    priority: int = Field(default=100, ge=0)
    depends_on: list[str] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
