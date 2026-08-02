from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorType(str, Enum):
    transient_error = "transient_error"
    data_validation_error = "data_validation_error"
    dependency_error = "dependency_error"
    configuration_error = "configuration_error"
    code_error = "code_error"


class RecoveryError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_type: ErrorType
    error_code: str = Field(min_length=1)
    step: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=0, ge=0)
    selected_fallback: str | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def retry_count_within_limit(self) -> "RecoveryError":
        if self.retry_count > self.max_retries:
            raise ValueError("retry_count cannot exceed max_retries")
        return self
