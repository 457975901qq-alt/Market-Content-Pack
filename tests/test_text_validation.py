from pathlib import Path

from text_validation import validate_text_artifacts


def test_shared_text_validation_reports_pass(tmp_path: Path) -> None:
    content = tmp_path / "market_content.json"
    copy = tmp_path / "douyin.md"
    content.write_text('{"edition":"morning_close_review"}', encoding="utf-8")
    copy.write_text("文字版文案", encoding="utf-8")

    report = validate_text_artifacts(content, copy, expected_edition="morning_close_review")

    assert report["status"] == "pass"
    assert report["critical_errors"] == []


def test_shared_text_validation_blocks_missing_artifact(tmp_path: Path) -> None:
    content = tmp_path / "market_content.json"
    content.write_text('{"edition":"morning_close_review"}', encoding="utf-8")

    report = validate_text_artifacts(content, tmp_path / "douyin.md", expected_edition="morning_close_review")

    assert report["status"] == "fail"
    assert "platform_copy_file" in report["critical_errors"]
