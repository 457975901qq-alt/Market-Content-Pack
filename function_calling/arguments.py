from __future__ import annotations

from enum import Enum
from pathlib import Path

from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class Edition(str, Enum):
    morning_close_review = "morning_close_review"
    evening_premarket_watch = "evening_premarket_watch"


class SafeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^market_\d{8}_\d{4}(?:_[a-z0-9]{4,8})?$")
    edition: Edition
    timeout_seconds: float = Field(default=120, gt=0, le=1200)


class CollectMarketDataArgs(SafeArgs):
    symbols: list[str] = Field(min_length=1)
    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def timezone_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware ISO 8601")
        return value.astimezone(timezone.utc) if value is not None else None


class CollectNewsArgs(SafeArgs):
    sources: list[str] = Field(min_length=1)


class ExtractWebContentArgs(SafeArgs):
    urls: list[str] = Field(min_length=1)

    @field_validator("urls")
    @classmethod
    def valid_urls(cls, value: list[str]) -> list[str]:
        if any(not item.startswith(("http://", "https://")) for item in value):
            raise ValueError("urls must be http(s) URLs")
        return value


class GenerateContentArgs(SafeArgs):
    input_path: str
    provider: str = Field(min_length=1)
    raw_response_path: str | None = None


class ValidateMarketDataArgs(SafeArgs):
    market_data_path: str


class ValidateContentArgs(SafeArgs):
    content_path: str
    source_path: str


class FinalQualityGateArgs(SafeArgs):
    validation_paths: list[str] = Field(min_length=1)


class CrosscheckMarketQuoteArgs(SafeArgs):
    symbol: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9.^=-]+$",
        validation_alias=AliasChoices("symbol", "ticker"),
    )
    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def timezone_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware ISO 8601")
        return value.astimezone(timezone.utc) if value is not None else None

    @property
    def ticker(self) -> str:
        return self.symbol


class GetMarketQuoteArgs(SafeArgs):
    symbol: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9.^=-]+$",
        validation_alias=AliasChoices("symbol", "ticker"),
    )
    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def timezone_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware ISO 8601")
        return value.astimezone(timezone.utc) if value is not None else None

    @property
    def ticker(self) -> str:
        return self.symbol


class GenerateMarketSectionArgs(GenerateContentArgs):
    section: str = Field(min_length=1)


class SchemaCheckArgs(ValidateContentArgs):
    pass


class GroundingCheckArgs(ValidateContentArgs):
    pass


class TemporalCheckArgs(ValidateContentArgs):
    pass


class AnalyzeGapArgs(SafeArgs):
    validation_errors: list[dict[str, object]] = Field(default_factory=list)
    current_state: dict[str, object] = Field(default_factory=dict)
    artifact_manifest: dict[str, object] = Field(default_factory=dict)


class SearchSourcesArgs(SafeArgs):
    sources: list[str] = Field(min_length=1)


class FetchSourceArgs(SafeArgs):
    url: str = Field(min_length=8)


class ReviewContentArgs(SafeArgs):
    content_path: str
    section: str | None = None


class ReviewerGateArgs(SafeArgs):
    content_path: str
    section: str | None = None


class RepairSectionArgs(SafeArgs):
    section: str = Field(min_length=1)
    reason: str | None = None


class RegenerateSectionArgs(RepairSectionArgs):
    pass


class ReportArgs(SafeArgs):
    content_path: str


class SaveReportArgs(SafeArgs):
    report_path: str


def within_allowed_root(path_value: str, roots: tuple[Path, ...]) -> bool:
    path = Path(path_value).expanduser().resolve()
    return any(path == root or root in path.parents for root in roots)
