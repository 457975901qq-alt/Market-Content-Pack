from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class StepState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1)
    status: StepStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeState(BaseModel):
    # Extra fields keep old state files readable while the core fields are typed.
    model_config = ConfigDict(extra="allow")

    run_id: str = Field(pattern=r"^market_\d{8}_\d{4}(?:_[a-z0-9]{4,8})?$")
    edition: str = Field(min_length=1)
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    failed_step: str | None = None
    retry_count: int = Field(default=0, ge=0)
    delivered: bool = False
    started_at: datetime
    updated_at: datetime
    output_root: str = Field(min_length=1)
    steps: dict[str, StepState]
