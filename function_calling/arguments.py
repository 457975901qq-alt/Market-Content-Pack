from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Edition(str, Enum):
    morning_close_review = "morning_close_review"
    evening_premarket_watch = "evening_premarket_watch"


class SafeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^market_\d{8}_\d{4}$")
    edition: Edition
    timeout_seconds: float = Field(default=120, gt=0, le=1200)


class CollectMarketDataArgs(SafeArgs):
    symbols: list[str] = Field(min_length=1)


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


def within_allowed_root(path_value: str, roots: tuple[Path, ...]) -> bool:
    path = Path(path_value).expanduser().resolve()
    return any(path == root or root in path.parents for root in roots)
