from __future__ import annotations

import json
from pathlib import Path

from production_preflight import evaluate_preflight


def test_text_only_runtime_does_not_require_image_adapter(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "runtime_policy.json").write_text(json.dumps({"allow_image_generation": False}), encoding="utf-8")
    (tmp_path / "config" / "evaluation_policy.json").write_text(json.dumps({"allow_delivery": False, "allow_production_update": True}), encoding="utf-8")
    (tmp_path / "config" / "tool_routing_policy.json").write_text(json.dumps({"tools": {}}), encoding="utf-8")
    (tmp_path / "config" / "delivery_policy.json").write_text(json.dumps({"enabled": False, "adapter": "smtp_email"}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "production_canary_report.json").write_text(json.dumps({"production_ready": False}), encoding="utf-8")

    report = evaluate_preflight(tmp_path)

    assert report["checks"]["image_pipeline_available"]["status"] == "pass"
    assert "image pipeline is not required" in report["checks"]["image_pipeline_available"]["detail"]
