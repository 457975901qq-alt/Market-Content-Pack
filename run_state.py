from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.runtime_models import RuntimeState


RUN_ID_RE = re.compile(r"^market_\d{8}_\d{4}$")
STEPS = ["health_check", "collect_github", "collect_sources", "collect_market_quotes", "generate_content", "final_validation", "build_review_package", "reviewer_agent", "reviewer_gate", "offline_evaluation", "archive"]
STATUSES = {"pending", "running", "success", "failed", "skipped"}
LOGICAL_STEP_TO_EXECUTOR = {
    "inspect_environment": "health_check",
    "build_execution_plan": "health_check",
    "select_tools": "health_check",
    "execute_plan": "health_check",
    "collect_market_data": "collect_market_quotes",
    "collect_news": "collect_sources",
    "extract_web_content": "collect_sources",
    "validate_market_data": "collect_market_quotes",
    "validate_content_consistency": "generate_content",
    "final_quality_gate": "final_validation",
    "archive": "archive",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def create(run_id: str, edition: str, root: Path, output_root: Path) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must match market_YYYYMMDD_HHMM")
    timestamp = now()
    state = {
        "run_id": run_id,
        "edition": edition,
        "date": run_id.split("_")[1],
        "current_step": None,
        "completed_steps": [],
        "failed_step": None,
        "retry_count": 0,
        "started_at": timestamp,
        "updated_at": timestamp,
        "delivered": False,
        "output_root": str(output_root),
        "steps": {step: {"step": step, "status": "pending", "started_at": None, "completed_at": None, "error": None, "artifacts": []} for step in STEPS},
        "logical_steps": {step: {"step": step, "status": "pending", "started_at": None, "completed_at": None, "error": None, "artifacts": []} for step in LOGICAL_STEP_TO_EXECUTOR},
    }
    save(state, root)
    return state


def path(run_id: str, root: Path) -> Path:
    # Canary state is kept directly under state/canary so it never shares the
    # production or Shadow namespace. Older roots retain their original path.
    return root / f"{run_id}.json" if root.name == "canary" else root / "state" / f"{run_id}.json"


def load(run_id: str, root: Path) -> dict[str, Any]:
    target = path(run_id, root)
    if not target.exists():
        raise FileNotFoundError(target)
    state = json.loads(target.read_text(encoding="utf-8"))
    # Migrate states created before the unified source layer was introduced.
    missing_steps = sorted(set(STEPS) - set(state.get("steps", {})))
    if missing_steps:
        state["steps"] = {name: state.get("steps", {}).get(name, _pending(name)) for name in STEPS}
        state["_migrated_pending_steps"] = missing_steps
    validated = RuntimeState.model_validate(state)
    return validated.model_dump(mode="json")


def _pending(step: str) -> dict[str, Any]:
    return {"step": step, "status": "pending", "started_at": None, "completed_at": None, "error": None, "artifacts": []}


def save(state: dict[str, Any], root: Path) -> None:
    validated = RuntimeState.model_validate(state)
    serialized = validated.model_dump(mode="json")
    state.clear()
    state.update(serialized)
    state["updated_at"] = now()
    atomic_write_json(path(state["run_id"], root), state)
    try:
        from runtime_index import index_for_state_root

        index_for_state_root(root).upsert_run(state, path(state["run_id"], root))
    except Exception:
        # SQLite is an index/audit layer; JSON state remains authoritative.
        pass


def mark(state: dict[str, Any], step: str, status: str, root: Path, error: dict[str, Any] | None = None, artifacts: list[Path] | None = None) -> None:
    if step not in state["steps"] or status not in STATUSES:
        raise ValueError(f"invalid_step_or_status:{step}:{status}")
    item = state["steps"][step]
    timestamp = now()
    item["status"] = status
    item["error"] = error
    item["artifacts"] = [artifact_record(p, state["run_id"]) for p in (artifacts or []) if p.exists() and p.is_file()]
    if status == "running":
        item["started_at"] = timestamp
    if status in {"success", "failed", "skipped"}:
        item["completed_at"] = timestamp
    state["current_step"] = step
    if status == "success" and step not in state["completed_steps"]:
        state["completed_steps"].append(step)
    if status == "success" and state.get("failed_step") == step:
        state["failed_step"] = None
    if status == "failed":
        state["failed_step"] = step
    logical_steps = state.setdefault("logical_steps", {})
    for logical, executor_step in LOGICAL_STEP_TO_EXECUTOR.items():
        if executor_step == step:
            logical_steps[logical] = {
                "step": logical,
                "status": status,
                "started_at": item.get("started_at"),
                "completed_at": item.get("completed_at"),
                "error": error,
                "artifacts": item.get("artifacts", []),
            }
    save(state, root)
    try:
        from runtime_index import index_for_state_root

        index_for_state_root(root).record_step(state["run_id"], step, status, error, len(item["artifacts"]))
    except Exception:
        pass


def artifact_record(path: Path, run_id: str) -> dict[str, Any]:
    return {"path": str(path.resolve()), "file_name": path.name, "sha256": sha256(path), "size": path.stat().st_size, "run_id": run_id}


def artifact_valid(record: dict[str, Any], run_id: str) -> bool:
    target = Path(str(record.get("path", "")))
    return bool(target.exists() and target.is_file() and target.stat().st_size > 0 and record.get("run_id") == run_id and sha256(target) == record.get("sha256"))


def first_resume_step(state: dict[str, Any]) -> str | None:
    for step in STEPS:
        item = state["steps"][step]
        if item["status"] in {"failed", "running"}:
            return step
        if item["status"] == "success" and (not item.get("artifacts") or not all(artifact_valid(record, state["run_id"]) for record in item.get("artifacts", []))):
            return step
    for step in state.get("_migrated_pending_steps", []):
        if state["steps"].get(step, {}).get("status") == "pending":
            return step
    return None


def reset_from(state: dict[str, Any], step: str, root: Path) -> None:
    if step not in STEPS:
        raise ValueError(f"unknown_step:{step}")
    start = STEPS.index(step)
    for name in STEPS[start:]:
        state["steps"][name] = {"step": name, "status": "pending", "started_at": None, "completed_at": None, "error": None, "artifacts": []}
        if name in state["completed_steps"]:
            state["completed_steps"].remove(name)
    state["failed_step"] = None
    state["current_step"] = step
    for logical, executor_step in LOGICAL_STEP_TO_EXECUTOR.items():
        if executor_step in STEPS[start:]:
            state.setdefault("logical_steps", {})[logical] = _pending(logical)
    save(state, root)
