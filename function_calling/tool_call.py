from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FunctionStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    rejected = "rejected"
    timeout = "timeout"


class FunctionError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_type: str = Field(min_length=1)
    error_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    remediation_step: str | None = None
    traceback: str | None = None


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="planner", min_length=1)
    step: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FunctionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    status: FunctionStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: FunctionError | None = None
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
