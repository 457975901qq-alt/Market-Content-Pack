import json
import os
import unittest
from unittest.mock import patch

import model_providers
from edition_profiles import resolve_edition_context
from market_content_openai import INVESTMENT_DISCLAIMER, generate_with_provider, parse_json_response, rule_template_response
from model_providers import ProviderError, health_check, _json_request


class ModelProviderTests(unittest.TestCase):
    def test_rule_template_is_schema_shaped_and_has_no_market_facts(self):
        context = resolve_edition_context("morning_close_review")
        payload = rule_template_response(context)
        self.assertEqual(payload["edition"], "morning_close_review")
        self.assertEqual(payload["major_indexes"], [])
        self.assertEqual(payload["important_stocks"], [])
        self.assertIn("暂无", payload["summary"])
        self.assertEqual(payload["ai_investment_view"]["stance"], "数据不足")
        self.assertEqual(payload["ai_investment_view"]["disclaimer"], INVESTMENT_DISCLAIMER)

    def test_rule_template_can_carry_validated_quotes(self):
        context = resolve_edition_context("evening_premarket_watch")
        payload = rule_template_response(context, market_data={
            "quotes": [
                {"symbol": "SPX", "display_name": "标普500", "asset_type": "index", "change_pct": -1.2},
                {"symbol": "NDX", "display_name": "纳斯达克100", "asset_type": "index", "change_pct": -1.5},
                {"symbol": "DJI", "display_name": "道琼斯", "asset_type": "index", "change_pct": -0.8},
                {"symbol": "NVDA", "display_name": "NVDA", "asset_type": "stock", "change_pct": -2.1},
            ]
        })
        self.assertEqual({item["ticker"] for item in payload["major_indexes"]}, {"SPX", "NDX", "DJI"})
        self.assertEqual(payload["important_stocks"][0]["ticker"], "NVDA")

    def test_rule_template_builds_non_personalized_view_from_validated_indexes(self):
        context = resolve_edition_context("morning_close_review")
        payload = rule_template_response(context, market_data={
            "quotes": [
                {"symbol": "SPX", "asset_type": "index", "change_pct": 0.7, "source_url": "https://example.test/spx", "source_id": "spx", "freshness": {"stale": False}, "cross_check": {"conflict": False}},
                {"symbol": "NDX", "asset_type": "index", "change_pct": 0.6, "source_url": "https://example.test/ndx", "source_id": "ndx", "freshness": {"stale": False}, "cross_check": {"conflict": False}},
                {"symbol": "DJI", "asset_type": "index", "change_pct": 0.5, "source_url": "https://example.test/dji", "source_id": "dji", "freshness": {"stale": False}, "cross_check": {"conflict": False}},
            ]
        })
        view = payload["ai_investment_view"]
        self.assertEqual(view["stance"], "偏积极观察")
        self.assertEqual(view["action"], "等待验证")
        self.assertEqual(view["disclaimer"], INVESTMENT_DISCLAIMER)
        self.assertTrue(all("source_id=" in item for item in view["evidence"]))

    def test_health_check_does_not_expose_credentials(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-value", "GEMINI_MODEL": "test-model"}, clear=False):
            status = health_check("gemini")
        self.assertTrue(status["configured"])
        self.assertEqual(status["status"], "healthy")
        self.assertNotIn("secret-value", json.dumps(status))

    def test_gemini_health_check_is_unhealthy_without_credentials(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            status = health_check("gemini")
        self.assertFalse(status["configured"])
        self.assertEqual(status["status"], "unhealthy")

    def test_provider_http_errors_are_normalized_without_secret_url(self):
        with self.assertRaises(ProviderError) as raised:
            with patch("model_providers.urllib.request.urlopen", side_effect=OSError("offline")):
                _json_request("https://example.test?key=secret", {}, {}, 1)
        self.assertEqual(raised.exception.error_type, "provider_unavailable")
        self.assertNotIn("secret", str(raised.exception))

    def test_auto_provider_reaches_rule_template_after_unavailable_backends(self):
        context = resolve_edition_context("evening_premarket_watch")
        with patch("market_content_openai.call_ollama", side_effect=ProviderError("provider_unavailable", "offline")), patch(
            "market_content_openai.call_gemini", side_effect=ProviderError("provider_unavailable", "offline")
        ):
            raw = generate_with_provider("fixture", context, "auto")
        payload = parse_json_response(raw)
        self.assertEqual(payload["important_stocks"], [])
        self.assertIn("暂无", payload["summary"])

    def test_fault_injection_is_disabled_outside_canary(self):
        with patch.dict(os.environ, {"SELF_HEALING_CANARY_MODE": "false", "SELF_HEALING_FAULT": "ollama_unavailable"}, clear=False):
            self.assertEqual(model_providers._CANARY_FAULTS_INJECTED, set())


if __name__ == "__main__":
    unittest.main()
