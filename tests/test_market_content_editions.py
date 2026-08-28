import datetime as dt
import unittest

from edition_profiles import resolve_edition_context
from market_content_openai import DAILY_SECTION_DEFINITIONS, INVESTMENT_DISCLAIMER, MarketContentError, _normalize_run_metadata, build_prompt, validate_market_content


TOKYO = dt.timezone(dt.timedelta(hours=9))


def valid_payload(context):
    return {
        "date": context.scheduled_cutoff.date().isoformat(),
        "timezone": "Asia/Tokyo",
        "edition": context.edition,
        "prompt_version": context.prompt_version,
        "data_cutoff": context.scheduled_cutoff.isoformat(),
        "scheduled_local_time": context.scheduled_local_time,
        "source_window_start": context.source_window_start.isoformat(),
        "source_window_end": context.source_window_end.isoformat(),
        "market_session": context.market_session,
        "edition_fields": {field: f"已验证的{field}" for field in context.version_fields},
        "summary": "仅用于本地版本路由测试。",
        "key_points": ["测试数据不进入生产发送。"],
        "major_indexes": [],
        "important_stocks": [],
        "macro_events": [],
        "earnings": [],
        "risk_factors": [],
        "analysis_text": {"title": "测试标题", "subtitle": "测试副标题", "sections": [{"heading": "测试", "content": "测试内容"}]},
    }


class MarketContentEditionTests(unittest.TestCase):
    def setUp(self):
        self.morning_started = dt.datetime(2026, 7, 19, 8, 0, tzinfo=TOKYO)
        self.evening_started = dt.datetime(2026, 7, 19, 20, 0, tzinfo=TOKYO)

    def test_morning_payload_uses_close_review_contract(self):
        context = resolve_edition_context("morning_close_review", self.morning_started)
        payload = valid_payload(context)
        validate_market_content(payload, context=context)
        self.assertEqual([item["section_id"] for item in payload["daily_sections"]], [item[0] for item in DAILY_SECTION_DEFINITIONS])
        self.assertEqual(payload["market_session"], "close_review")
        self.assertEqual(set(payload["edition_fields"]), set(context.version_fields))

    def test_evening_payload_uses_premarket_contract(self):
        context = resolve_edition_context("evening_premarket_watch", self.evening_started)
        payload = valid_payload(context)
        validate_market_content(payload, context=context)
        self.assertEqual(payload["market_session"], "premarket_watch")
        self.assertEqual(set(payload["edition_fields"]), set(context.version_fields))

    def test_prompts_are_distinct_and_include_cutoff_context(self):
        morning = resolve_edition_context("morning_close_review", self.morning_started)
        evening = resolve_edition_context("evening_premarket_watch", self.evening_started)
        morning_prompt = build_prompt("fixture", morning)
        evening_prompt = build_prompt("fixture", evening)
        self.assertNotEqual(morning_prompt, evening_prompt)
        self.assertIn("上一交易时段收盘复盘", morning_prompt)
        self.assertIn("美股盘前催化剂", evening_prompt)
        self.assertIn(morning.scheduled_cutoff.isoformat(), morning_prompt)
        self.assertIn(evening.scheduled_cutoff.isoformat(), evening_prompt)

    def test_cross_edition_fields_are_rejected(self):
        context = resolve_edition_context("morning_close_review", self.morning_started)
        payload = valid_payload(context)
        payload["edition_fields"]["premarket_catalysts"] = "错误版本字段"
        with self.assertRaises(MarketContentError) as raised:
            validate_market_content(payload, context=context)
        self.assertEqual(raised.exception.error_type, "edition_fields_mismatch")

    def test_stale_cutoff_is_rejected(self):
        context = resolve_edition_context("evening_premarket_watch", self.evening_started)
        payload = valid_payload(context)
        payload["data_cutoff"] = "2026-07-19T06:30:00+09:00"
        with self.assertRaises(MarketContentError) as raised:
            validate_market_content(payload, context=context)
        self.assertEqual(raised.exception.error_type, "edition_metadata_mismatch")

    def test_missing_provider_date_is_filled_from_edition_cutoff(self):
        context = resolve_edition_context("evening_premarket_watch", self.evening_started)
        payload = {"date": "", "timezone": ""}
        _normalize_run_metadata(payload, context)
        self.assertEqual(payload["date"], context.scheduled_cutoff.date().isoformat())
        self.assertEqual(payload["timezone"], "Asia/Tokyo")

    def test_provider_edition_typo_is_normalized_from_runtime_context(self):
        context = resolve_edition_context("evening_premarket_watch", self.evening_started)
        payload = {"edition": "evening_premarkarket_watch"}
        _normalize_run_metadata(payload, context)
        self.assertEqual(payload["edition"], "evening_premarket_watch")

    def test_legacy_payload_gets_explicit_conservative_investment_view(self):
        context = resolve_edition_context("morning_close_review", self.morning_started)
        payload = valid_payload(context)
        validate_market_content(payload, context=context)
        self.assertEqual(payload["ai_investment_view"]["action"], "数据不足")
        self.assertEqual(payload["ai_investment_view"]["disclaimer"], INVESTMENT_DISCLAIMER)

    def test_direct_trading_instruction_is_rejected(self):
        context = resolve_edition_context("morning_close_review", self.morning_started)
        payload = valid_payload(context)
        payload["ai_investment_view"] = {
            "market_environment": {"regime": "risk_on", "summary": "测试环境", "confidence": 0.6, "signals": ["source-1"]},
            "stance": "偏积极观察",
            "action": "观察",
            "thesis": "观察趋势",
            "evidence": ["source-1"],
            "risks": ["波动"],
            "invalidation_conditions": ["来源冲突"],
            "suggestions": ["等待验证"],
            "disclaimer": INVESTMENT_DISCLAIMER,
        }
        payload["ai_investment_view"]["thesis"] = "建议买入"
        with self.assertRaises(MarketContentError) as raised:
            validate_market_content(payload, context=context)
        self.assertEqual(raised.exception.error_type, "investment_view_unsafe")

    def test_investment_view_requires_fixed_disclaimer(self):
        context = resolve_edition_context("evening_premarket_watch", self.evening_started)
        payload = valid_payload(context)
        payload["ai_investment_view"] = {
            "market_environment": {"regime": "mixed", "summary": "测试环境", "confidence": 0.6, "signals": ["source-1"]},
            "stance": "中性观察",
            "action": "等待验证",
            "thesis": "等待更多证据",
            "evidence": ["source-1"],
            "risks": ["波动"],
            "invalidation_conditions": ["数据失效"],
            "suggestions": ["等待验证"],
            "disclaimer": "这是建议",
        }
        with self.assertRaises(MarketContentError) as raised:
            validate_market_content(payload, context=context)
        self.assertEqual(raised.exception.failure_position, "ai_investment_view.disclaimer")

    def test_daily_sections_reject_unknown_section(self):
        context = resolve_edition_context("morning_close_review", self.morning_started)
        payload = valid_payload(context)
        payload["daily_sections"] = [{"section_id": "unknown", "title": "未知", "status": "available", "content": "不应通过", "evidence": []}]
        with self.assertRaises(MarketContentError) as raised:
            validate_market_content(payload, context=context)
        self.assertEqual(raised.exception.error_type, "daily_sections_invalid")


if __name__ == "__main__":
    unittest.main()
