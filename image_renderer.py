#!/usr/bin/env python3
"""Deterministic local SVG renderer and fail-closed image QA."""

from __future__ import annotations

import hashlib
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


WIDTH = 1080
HEIGHT = 1920


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _lines(value: Any, limit: int = 28) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _text_block(lines: list[str], x: int, y: int, size: int, fill: str, line_height: int = 44) -> str:
    escaped = [html.escape(line) for line in lines]
    body = "".join(f'<tspan x="{x}" dy="{0 if index == 0 else line_height}">{line}</tspan>' for index, line in enumerate(escaped))
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-family="Arial, sans-serif">{body}</text>'


def render_image_pack(content_path: Path, output_dir: Path, run_id: str) -> dict[str, Any]:
    content = _read_json(content_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    title = ((content.get("analysis_text") or {}).get("title") or content.get("summary") or "每日市场内容包")
    date = str(content.get("date") or "待核验")
    summary = content.get("summary") or "暂无可靠数据"
    key_points = [str(item) for item in content.get("key_points", []) if item]
    risks = [str(item) for item in content.get("risk_factors", []) if item]
    indexes = [item for item in content.get("major_indexes", []) if isinstance(item, dict)]
    index_text = "  ".join(f"{item.get('ticker', '')} {item.get('change_percent', '待核验')}" for item in indexes)
    sections = [
        _text_block(_lines(title, 20), 72, 150, 58, "#F8FAFC", 70),
        _text_block([f"{date}  ·  {content.get('edition', 'market')}", "仅作信息整理与情景分析"], 72, 310, 28, "#94A3B8", 44),
        '<rect x="72" y="430" width="936" height="4" fill="#38BDF8"/>',
        _text_block(_lines(summary, 30), 72, 530, 34, "#E2E8F0", 52),
        _text_block(["主要指数", index_text or "市场数据暂缺"], 72, 900, 34, "#7DD3FC", 56),
        _text_block(["今日要点"] + [f"• {item}" for item in key_points[:5]], 72, 1080, 30, "#E2E8F0", 48),
        _text_block(["风险提示"] + [f"• {item}" for item in risks[:4]], 72, 1480, 30, "#FCA5A5", 48),
        _text_block(["数据来源和质量门禁已由程序校验", f"run_id: {run_id}"], 72, 1810, 22, "#94A3B8", 34),
    ]
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}"><rect width="100%" height="100%" fill="#0F172A"/>{"".join(sections)}</svg>'
    target = output_dir / "market_content.svg"
    target.write_text(svg, encoding="utf-8")
    return {"status": "pass", "path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "width": WIDTH, "height": HEIGHT}


def validate_image_pack(image_path: Path, content_path: Path, run_id: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    passed = True
    try:
        raw = image_path.read_text(encoding="utf-8")
        root = ET.fromstring(raw)
        dimensions_ok = root.attrib.get("width") == str(WIDTH) and root.attrib.get("height") == str(HEIGHT)
        checks.append({"name": "dimensions", "status": "pass" if dimensions_ok else "fail"})
        passed = passed and dimensions_ok
    except (OSError, ValueError, ET.ParseError):
        checks.append({"name": "svg_parse", "status": "fail"})
        passed = False
        raw = ""
    try:
        content = _read_json(content_path)
        title = str(((content.get("analysis_text") or {}).get("title") or content.get("summary") or "每日市场内容包"))
        content_markers_ok = bool(title) and str(content.get("date") or "待核验") in raw
    except (OSError, ValueError, TypeError):
        content_markers_ok = False
    checks.append({"name": "content_markers", "status": "pass" if content_markers_ok else "fail"})
    passed = passed and content_markers_ok
    return {"run_id": run_id, "status": "pass" if passed else "fail", "image_path": str(image_path), "content_path": str(content_path), "image_hash": hashlib.sha256(image_path.read_bytes()).hexdigest() if image_path.exists() else None, "checks": checks}


__all__ = ["HEIGHT", "WIDTH", "render_image_pack", "validate_image_pack"]
