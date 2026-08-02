from __future__ import annotations

from pathlib import Path

from runtime_index import StateIndex


def test_sqlite_index_records_state_and_step_events(tmp_path: Path) -> None:
    index = StateIndex(tmp_path / "state.sqlite3")
    state = {"run_id": "market_20260720_1500", "edition": "evening_premarket_watch", "current_step": "generate_content", "delivered": False, "updated_at": "now"}
    index.upsert_run(state, tmp_path / "state.json")
    index.record_step(state["run_id"], "generate_content", "success", None, 2)
    assert index.events(state["run_id"])[0]["status"] == "success"
