from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QualityStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    warning = "warning"


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_name: str = Field(min_length=1)
    result: QualityStatus
    severity: str = "error"
    artifact: str | None = None
    expected_value: Any = None
    actual_value: Any = None
    remediation_step: str | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^market_\d{8}_\d{4}$")
    status: QualityStatus
    critical_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checks: list[ValidationCheck] = Field(default_factory=list)
    validated_at: datetime
    validator_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def pass_has_no_critical_errors(self) -> "ValidationReport":
        if self.status == QualityStatus.passed and self.critical_errors:
            raise ValueError("status=pass requires critical_errors to be empty")
        return self
