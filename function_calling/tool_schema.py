from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolFunctionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
    supported_steps: list[str] = Field(min_length=1)
    enabled: bool = True
    timeout_seconds: float = Field(gt=0, le=1200)
    requires_approval: bool = False
