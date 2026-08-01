from pathlib import Path

from PIL import Image, ImageDraw

from split_market_pack import cell_box, split_sheet


def make_sheet(path: Path) -> None:
    cell_w, cell_h = 240, 320
    sheet = Image.new("RGB", (cell_w * 2, cell_h * 4), "white")
    draw = ImageDraw.Draw(sheet)
    for page in range(8):
        row, col = divmod(page, 2)
        shade = 25 + page * 25
        draw.rectangle(
            (col * cell_w, row * cell_h, (col + 1) * cell_w - 1, (row + 1) * cell_h - 1),
            fill=(shade, shade, shade),
        )
    sheet.save(path)


def test_cell_box_uses_row_major_order():
    assert cell_box(
        image_width=400,
        image_height=800,
        col=1,
        row=2,
        cols=2,
        rows=4,
        outer_margin=0,
        gutter_x=0,
        gutter_y=0,
    ) == (200, 400, 400, 600)


def test_split_sheet_exports_eight_independent_pages(tmp_path: Path):
    source = tmp_path / "combined.png"
    output = tmp_path / "pages"
    make_sheet(source)

    exported = split_sheet(source, output, page_size=(1080, 1440))

    assert len(exported) == 8
    assert (output / "manifest.json").exists()
    assert [item.page for item in exported] == list(range(1, 9))

    for item in exported:
        page_path = output / item.filename
        assert page_path.exists()
        with Image.open(page_path) as page:
            assert page.size == (1080, 1440)


def test_remove_source_happens_after_successful_export(tmp_path: Path):
    source = tmp_path / "combined.png"
    output = tmp_path / "pages"
    make_sheet(source)

    split_sheet(source, output, remove_source=True)

    assert not source.exists()
    assert len(list(output.glob("*.png"))) == 8
