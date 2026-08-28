from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewDecision(str, Enum):
    approve = "approve"
    reject = "reject"
    needs_review = "needs_review"


class ReviewerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_name: str = Field(min_length=1)
    result: str
    evidence: list[Any] = Field(default_factory=list)
    artifact: str | None = None
    expected: Any = None
    actual: Any = None
    remediation_step: str | None = None


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^market_\d{8}_\d{4}(?:_[a-z0-9]{4,8})?$")
    content_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    reviewer: ReviewerInfo
    decision: ReviewDecision
    confidence: float = Field(ge=0, le=1)
    critical_findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checks: list[ReviewCheck] = Field(default_factory=list)
    reviewed_at: datetime
    independence_warning: bool = False

    @model_validator(mode="after")
    def approved_has_no_critical_findings(self) -> "ReviewResult":
        if self.decision == ReviewDecision.approve and self.critical_findings:
            raise ValueError("decision=approve requires critical_findings to be empty")
        return self
