from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_network_retries: int = Field(default=3, ge=0)
    max_provider_retries: int = Field(default=2, ge=0)
    max_json_retries: int = Field(default=2, ge=0)
    allow_provider_switch: bool = True
    allow_content_regeneration: bool = True
    allow_section_repair: bool = True
    allow_code_modification: bool = False
    allow_external_publish: bool = False

    @classmethod
    def from_config(cls, path: Path) -> "RecoveryPolicy":
        payload = json.loads(path.read_text(encoding="utf-8"))
        backoff = payload.get("network_backoff_seconds", [2, 5, 10])
        return cls(
            max_network_retries=len(backoff) if isinstance(backoff, list) else 3,
            max_provider_retries=int(payload.get("ollama_restart_max_attempts", 1)) + 1,
            max_json_retries=int(payload.get("gemini_max_retries", 2)),
            allow_provider_switch=True,
            allow_content_regeneration=True,
            allow_section_repair=True,
            allow_code_modification=False,
            allow_external_publish=False,
        )

    def allows(self, operation: str) -> bool:
        return bool(getattr(self, f"allow_{operation}", False))
