from __future__ import annotations

from delivery_report import render_delivery_report, render_delivery_report_html


def _content(edition: str = "morning_close_review") -> dict:
    return {
        "date": "2026-08-05",
        "edition": edition,
        "scheduled_local_time": "06:30" if edition == "morning_close_review" else "17:30",
        "summary": "三大指数全面反弹，科技股领涨，但短线波动仍需验证。",
        "major_indexes": [
            {"name": "标普500", "ticker": "SPX", "change_percent": "+1.79%", "reason": "反弹"},
            {"name": "纳斯达克100", "ticker": "NDX", "change_percent": "+3.32%", "reason": "科技股领涨"},
            {"name": "道琼斯", "ticker": "DJI", "change_percent": "+1.71%", "reason": "同步反弹"},
        ],
        "analysis_text": {"sections": [{"heading": "上涨动能", "content": "科技股带动指数反弹。"}]},
        "risk_factors": ["季节性疲软可能限制反弹持续性。"],
        "ai_investment_view": {"stance": "偏积极观察", "action": "等待验证", "thesis": "观察反弹持续性。", "disclaimer": "仅作信息整理与情景分析，不构成个性化投资建议。"},
    }


def _manifest(qa_status: str = "pass", mode: str = "text") -> dict:
    return {"run_id": "market_20260805_1731", "edition": "morning_close_review", "qa_status": qa_status, "mode": mode, "external_publish": "removed", "delivered": False, "source_status": {"source_count": 49}, "output_root": "/tmp/run", "data_cutoff": "2026-08-05T06:30:00+09:00"}


def _status(**kwargs) -> dict:
    return {"files": {"content": "/tmp/run/market_content/market_content.json", "manifest": "/tmp/run/logs/run_manifest.json"}, "output_root": "/tmp/run", **kwargs}


def test_rich_report_uses_compact_status_cards_and_folded_technical_section() -> None:
    report = render_delivery_report(_content(), _manifest(), _status(publish_status="已关闭"))
    assert "每日市场早盘报告" in report
    assert "2026-08-05 · 早盘" in report
    assert "QA 已通过" in report and "49 个来源" in report and "未发布" in report
    assert "指数表现" in report
    assert "border-radius:10px" in report
    assert "运行与技术信息 ▾" in report
    assert "<details" in report
    assert "| 指数 |" not in report
    assert report.index("市场驱动") < report.index("AI 投资观察")
    assert report.index("market_content.json") > report.index("运行与技术信息")


def test_media_generation_state_is_not_rendered() -> None:
    report = render_delivery_report(_content(), _manifest(mode="text"), _status(publish_status="已关闭"))
    assert "图片生成" not in report
    assert "外部发布功能" in report and "已关闭" in report


def test_qa_failure_renders_error_report_only() -> None:
    report = render_delivery_report(_content(), _manifest("fail"), _status(failed_step="generate_content", error_reason="schema 校验失败", log_path="/tmp/run/logs/errors.log"))
    assert "生成失败" in report
    assert "失败阶段" in report and "generate_content" in report
    assert "schema 校验失败" in report
    assert "指数表现" not in report
    assert "市场驱动" not in report


def test_missing_indexes_are_explicitly_marked_without_derived_status() -> None:
    content = _content()
    content["major_indexes"] = []
    report = render_delivery_report(content, _manifest(), _status())
    assert report.count("数据未提供") >= 3


def test_missing_index_status_is_not_invented_from_change_percent() -> None:
    report = render_delivery_report(_content(), _manifest(), _status())
    assert ">上涨<" not in report
    assert ">领涨<" not in report
    assert "状态未提供" in report
    assert "数据未提供" not in report


def test_missing_ai_investment_view_is_explicitly_marked() -> None:
    content = _content()
    content.pop("ai_investment_view")
    report = render_delivery_report(content, _manifest(), _status())
    assert "AI 投资观察" in report
    assert "数据未提供" in report


def test_morning_and_evening_titles_switch_dynamically() -> None:
    assert "每日市场早盘报告" in render_delivery_report(_content("morning_close_review"), _manifest(), _status())
    evening = _manifest()
    evening["edition"] = "evening_premarket_watch"
    assert "每日市场晚间报告" in render_delivery_report(_content("evening_premarket_watch"), evening, _status())
    assert "2026-08-05 · 晚间" in render_delivery_report(_content("evening_premarket_watch"), evening, _status())


def test_delivered_state_is_rendered_as_execution_result() -> None:
    report_false = render_delivery_report(_content(), _manifest(), _status(delivered=False))
    report_true = render_delivery_report(_content(), _manifest(), _status(delivered=True))
    assert "未发布" in report_false
    assert "交付结果" in report_false and "未执行" in report_false
    assert "交付结果" in report_true and "已交付" in report_true


def test_light_and_dark_themes_are_explicit() -> None:
    light = render_delivery_report(_content(), _manifest(), _status(), theme="light")
    dark = render_delivery_report(_content(), _manifest(), _status(), theme="dark")
    assert 'data-theme="light"' in light
    assert 'data-theme="dark"' in dark
    assert "#f8fafc" in light
    assert "#111827" in dark


def test_plain_text_fallback_remains_available() -> None:
    report = render_delivery_report(_content(), _manifest(), _status(), rich_text=False)
    assert "每日市场早盘报告" in report
    assert "指数表现" in report
    assert "<section" not in report
    assert "运行与技术信息 ▾" in report


def test_full_paths_are_only_in_technical_section() -> None:
    report = render_delivery_report(_content(), _manifest(), _status())
    assert report.index("/tmp/run/market_content/market_content.json") > report.index("运行与技术信息")


def test_html_report_is_a_standalone_document_with_real_ui_components() -> None:
    report = render_delivery_report_html(_content(), _manifest(), _status())
    assert report.startswith("<!doctype html>")
    assert "<style>" in report
    assert "--report-good" in report
    assert "@media (max-width: 720px)" in report
    assert report.count('class="panel index-card"') == 3
    assert 'class="status-badge status-badge--good"' in report
    assert "<details class=\"technical\">" in report
    assert "<summary>运行与技术信息</summary>" in report
    assert "[QA 已通过]" not in report


def test_html_report_supports_explicit_light_and_dark_themes() -> None:
    light = render_delivery_report_html(_content(), _manifest(), _status(), theme="light")
    dark = render_delivery_report_html(_content(), _manifest(), _status(), theme="dark")
    assert '<body data-theme="light">' in light
    assert '<body data-theme="dark">' in dark
    assert "--report-bg: #f8fafc" in light
    assert "--report-bg: #111827" in dark


def test_html_missing_index_status_is_distinguished_from_missing_index_data() -> None:
    report = render_delivery_report_html(_content(), _manifest(), _status())
    assert "状态未提供" in report
    assert "数据未提供" not in report


def test_html_qa_failure_is_not_rendered_as_normal_market_report() -> None:
    report = render_delivery_report_html(_content(), _manifest("fail"), _status(failed_step="generate_content", error_reason="schema 校验失败"))
    assert "生成失败" in report
    assert "schema 校验失败" in report
    assert "指数表现" not in report
    assert 'class="status-badge status-badge--bad"' in report


def test_indexes_use_fixed_ndx_spx_dji_order() -> None:
    content = _content()
    content["major_indexes"] = [content["major_indexes"][0], content["major_indexes"][2], content["major_indexes"][1]]
    report = render_delivery_report_html(content, _manifest(), _status())
    assert report.index("纳斯达克100") < report.index("标普500") < report.index("道琼斯")


def test_missing_numeric_value_is_distinct_from_missing_status() -> None:
    content = _content()
    content["major_indexes"][0]["change_percent"] = None
    report = render_delivery_report_html(content, _manifest(), _status())
    assert "标普500" in report
    assert "数据未提供" in report
    assert "状态未提供" in report


def test_direction_and_numeric_sign_conflict_renders_error_page() -> None:
    content = _content()
    content["major_indexes"][0]["direction"] = "down"
    report = render_delivery_report_html(content, _manifest(), _status())
    assert "生成失败" in report
    assert "指数数值与涨跌方向冲突" in report
    assert "major_indexes[SPX].direction" in report
    assert "+1.79%" in report
    assert "指数表现" not in report


def test_up_down_neutral_use_arrow_text_and_semantic_classes() -> None:
    content = _content()
    content["major_indexes"] = [
        {"name": "纳斯达克100", "ticker": "NDX", "change_percent": "+3.32%", "direction": "up", "status": "领涨"},
        {"name": "标普500", "ticker": "SPX", "change_percent": "-1.79%", "direction": "down", "status": "走弱"},
        {"name": "道琼斯", "ticker": "DJI", "change_percent": "0.00%", "direction": "neutral", "status": "持平"},
    ]
    report = render_delivery_report_html(content, _manifest(), _status())
    assert "index-change--up" in report and "index-change--down" in report and "index-change--neutral" in report
    assert "↑ 上涨" in report and "↓ 下跌" in report and "— 中性" in report


def test_data_cutoff_is_read_from_artifact_or_explicitly_missing() -> None:
    report = render_delivery_report_html(_content(), _manifest(), _status())
    assert "数据截止：2026-08-05 06:30 JST" in report
    manifest = _manifest()
    manifest.pop("data_cutoff")
    content = _content()
    content.pop("data_cutoff", None)
    report_missing = render_delivery_report_html(content, manifest, _status())
    assert "截止时间未提供" in report_missing


def test_html_escapes_content_manifest_and_error_text() -> None:
    content = _content()
    content["summary"] = "<script>alert('x')</script> & unsafe"
    content["major_indexes"][0]["name"] = '<img src=x onerror="bad">'
    report = render_delivery_report_html(content, _manifest(), _status())
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&lt;img" in report


def test_html_uses_relative_clickable_file_links() -> None:
    report = render_delivery_report_html(_content(), _manifest(), _status())
    assert 'href="../market_content/market_content.json"' in report
    assert 'href="../logs/run_manifest.json"' in report
    assert 'href="/tmp/run/' not in report


def test_runtime_switches_are_independent_from_delivery_result() -> None:
    status = _status(external_publish_enabled=True, delivered=False)
    report = render_delivery_report_html(_content(), _manifest(mode="text"), status)
    assert "图片生成" not in report
    assert "外部发布功能" in report and "已开启" in report
    assert "交付结果" in report and "未执行" in report


def test_long_text_keeps_document_structure_intact() -> None:
    content = _content()
    content["summary"] = "长文本" * 1000
    content["ai_investment_view"]["thesis"] = "观点" * 1000
    report = render_delivery_report_html(content, _manifest(), _status())
    assert report.startswith("<!doctype html>")
    assert report.endswith("</html>\n")
    assert report.count("<details") == 1


def test_fifteen_market_modules_are_independent_and_navigable() -> None:
    content = _content()
    content["daily_sections"] = [
        {
            "section_id": section_id,
            "title": title,
            "status": "available",
            "content": f"{title}的独立内容。",
            "evidence": [f"source-{number}"],
        }
        for number, (section_id, title) in enumerate(
            (
                ("top_catalysts", "今日Top 3市场催化剂"),
                ("ai_semiconductors", "AI与半导体"),
                ("mega_tech", "大科技"),
                ("us_macro", "美国宏观"),
                ("global_central_banks", "全球央行"),
                ("geopolitics_policy", "地缘政治与政策"),
                ("index_rebalances", "指数调整"),
                ("etf_flows", "ETF调仓与资金流"),
                ("opex_derivatives", "OPEX与衍生品"),
                ("treasuries_liquidity", "美债与流动性"),
                ("oil_commodities", "原油与大宗商品"),
                ("ipo_financing", "IPO与融资"),
                ("breaking_news", "突发新闻"),
                ("github_ai_projects", "GitHub热门AI项目"),
                ("asset_impact", "对重点资产的影响"),
            ),
            start=1,
        )
    ]
    report = render_delivery_report_html(content, _manifest(), _status())
    assert report.count('class="panel daily-section-card') == 15
    assert report.count("class=\"module-index__item\"") == 15
    assert report.count("<details") == 1
    for number in range(1, 16):
        assert f'id="daily-module-{number:02d}"' in report


def test_missing_daily_modules_render_as_separate_non_fact_cards() -> None:
    report = render_delivery_report_html(_content(), _manifest(), _status())
    assert report.count('class="panel daily-section-card') == 15
    assert report.count("数据暂缺") >= 15
    assert "每日市场栏目（15项）" not in report
