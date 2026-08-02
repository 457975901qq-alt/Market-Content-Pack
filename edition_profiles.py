from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent
PROFILE_PATH = PROJECT_ROOT / "config" / "edition_profiles.json"


def supported_editions() -> tuple[str, ...]:
    return tuple(_load_profiles().get("editions", {}).keys())


@dataclass(frozen=True)
class EditionContext:
    edition: str
    timezone_name: str
    label: str
    session_name: str
    market_session: str
    scheduled_local_time: str
    scheduled_cutoff: datetime
    source_window_start: datetime
    source_window_end: datetime
    data_window_policy: str
    focus: str
    prompt_version: str
    prompt_file: str
    prompt_text: str
    prompt_hash: str
    version_fields: tuple[str, ...]

    def as_json(self) -> dict:
        return {
            "edition": self.edition,
            "label": self.label,
            "session_name": self.session_name,
            "market_session": self.market_session,
            "scheduled_local_time": self.scheduled_local_time,
            "data_cutoff": self.scheduled_cutoff.isoformat(),
            "source_window_start": self.source_window_start.isoformat(),
            "source_window_end": self.source_window_end.isoformat(),
            "data_window_policy": self.data_window_policy,
            "focus": self.focus,
            "prompt_version": self.prompt_version,
            "prompt_file": self.prompt_file,
            "prompt_hash": self.prompt_hash,
            "version_fields": list(self.version_fields),
        }


def _load_profiles() -> dict:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return payload


def is_schedule_slot(edition: str, now: datetime | None = None, tolerance_minutes: int = 15) -> bool:
    """Return whether ``now`` is inside the explicit edition schedule window."""
    payload = _load_profiles()
    profile = payload.get("editions", {}).get(edition)
    if profile is None:
        raise ValueError(f"unsupported_edition:{edition}")
    tz = ZoneInfo(payload["timezone"])
    local_now = (now or datetime.now(tz)).astimezone(tz)
    hour, minute = (int(part) for part in profile["scheduled_local_time"].split(":", 1))
    scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return abs((local_now - scheduled).total_seconds()) <= tolerance_minutes * 60


def resolve_edition_context(edition: str, started_at: datetime | None = None) -> EditionContext:
    payload = _load_profiles()
    timezone_name = payload["timezone"]
    profile = payload.get("editions", {}).get(edition)
    if profile is None:
        raise ValueError(f"unsupported_edition:{edition}")
    tz = ZoneInfo(timezone_name)
    local_started = (started_at or datetime.now(tz)).astimezone(tz)
    hour, minute = (int(part) for part in profile["scheduled_local_time"].split(":", 1))
    cutoff = local_started.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_started < cutoff:
        cutoff -= timedelta(days=1)
    if edition == "morning_close_review":
        window_start = cutoff - timedelta(hours=13)
    else:
        window_start = cutoff - timedelta(hours=11)
    prompt_path = PROJECT_ROOT / profile["prompt_file"]
    prompt_text = prompt_path.read_text(encoding="utf-8")
    return EditionContext(
        edition=edition,
        timezone_name=timezone_name,
        label=profile["label"],
        session_name=profile["session_name"],
        market_session=profile["market_session"],
        scheduled_local_time=profile["scheduled_local_time"],
        scheduled_cutoff=cutoff,
        source_window_start=window_start,
        source_window_end=cutoff,
        data_window_policy=profile["data_window_policy"],
        focus=profile["focus"],
        prompt_version=profile["prompt_version"],
        prompt_file=profile["prompt_file"],
        prompt_text=prompt_text,
        prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        version_fields=tuple(profile["version_fields"]),
    )
