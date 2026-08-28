from __future__ import annotations

import hashlib
import json
from pathlib import Path

from delivery_preparation import approval_digest, prepare_x_twitter_release


RUN_ID = "market_20260823_1730"


def _write_fixture(root: Path, text: str = "市场摘要") -> dict[str, Path]:
    output_root = root / "outputs" / "runs" / RUN_ID
    content_path = output_root / "market_content" / "market_content.json"
    content_path.parent.mkdir(parents=True)
    content = {"summary": text, "date": "2026-08-23"}
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    content_hash = hashlib.sha256(content_path.read_bytes()).hexdigest()
    manifest_path = output_root / "logs" / "run_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({
        "run_id": RUN_ID,
        "run_mode": "production",
        "mode": "text",
        "output_root": str(output_root),
        "content_hash": content_hash,
        "artifact_hashes": {"market_content.json": content_hash},
        "qa_status": "pass",
        "canary_technical_ready": True,
        "canary_stability_pass": True,
        "production_ready": True,
    }), encoding="utf-8")
    (root / "config").mkdir(parents=True)
    (root / "config" / "delivery_policy.json").write_text(json.dumps({
        "enabled": False,
        "global_delivery_kill_switch": True,
        "external_delivery_enabled": False,
        "target_whitelist": [],
    }), encoding="utf-8")
    (root / "config" / "release_policy.json").write_text(json.dumps({"external_delivery_enabled": False}), encoding="utf-8")
    return {"manifest": manifest_path, "content": content_path}


def test_prepare_is_read_only_and_fail_closed(tmp_path: Path, monkeypatch) -> None:
    paths = _write_fixture(tmp_path)

    class Adapter:
        def health(self):
            return {"status": "ready", "adapter": "x_twitter", "media_upload": False}

    monkeypatch.setattr("delivery_preparation.build_delivery_adapter", lambda policy: Adapter())
    before_policy = paths["manifest"].read_bytes()
    report = prepare_x_twitter_release(tmp_path, RUN_ID, "@example")

    assert report["status"] == "BLOCKED"
    assert report["external_request_made"] is False
    assert report["config_mutated"] is False
    assert report["delivered"] is False
    assert "KILL_SWITCH_ACTIVE" in report["blockers"]
    assert paths["manifest"].read_bytes() == before_policy
    assert Path(report["report_path"]).is_file()


def test_prepare_rejects_long_x_text_before_send(tmp_path: Path, monkeypatch) -> None:
    _write_fixture(tmp_path, "x" * 281)

    class Adapter:
        def health(self):
            return {"status": "ready", "adapter": "x_twitter", "media_upload": False}

    monkeypatch.setattr("delivery_preparation.build_delivery_adapter", lambda policy: Adapter())
    report = prepare_x_twitter_release(tmp_path, RUN_ID, "@example")
    assert report["text_ready"] is False
    assert "X_TEXT_OVER_280_CHARACTERS" in report["blockers"]


def test_approval_digest_is_stable_and_secret_free() -> None:
    first = approval_digest(run_id=RUN_ID, target="@example", content_hash="c" * 64, artifact_hash="a" * 64)
    second = approval_digest(run_id=RUN_ID, target="@example", content_hash="c" * 64, artifact_hash="a" * 64)
    assert first == second
    assert len(first) == 64
    assert "secret" not in first
