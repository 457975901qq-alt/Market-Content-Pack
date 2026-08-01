from pathlib import Path
import json

from PIL import Image

from validate_market_image_pack import EXPECTED_NAMES, EXPECTED_SIZE


def write_pack(root: Path) -> None:
    pack_dir = root / "outputs" / "market_image_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "date": "2026-08-01",
        "timezone": "Asia/Tokyo",
        "market_session": "close_review",
        "width": EXPECTED_SIZE[0],
        "height": EXPECTED_SIZE[1],
        "page_count": 8,
        "pages": [f"outputs/market_image_pack/{name}" for name in EXPECTED_NAMES],
    }
    (pack_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name in EXPECTED_NAMES:
        image = Image.new("RGB", EXPECTED_SIZE, "white")
        image.save(pack_dir / name, format="PNG")


def test_expected_names_are_eight_pages():
    assert len(EXPECTED_NAMES) == 8
    assert EXPECTED_NAMES[0] == "01_cover.png"
    assert EXPECTED_NAMES[-1] == "08_flows_summary.png"


def test_expected_size_is_3_by_4_social_canvas():
    assert EXPECTED_SIZE == (1080, 1440)
