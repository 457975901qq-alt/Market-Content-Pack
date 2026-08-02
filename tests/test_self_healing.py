import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from self_healing.agent import RepairAdapters, RepairController, RepairStatus, classify_failure, repair_json_response
from error_classifier import classify_error
from repair_selector import select_repair_plan
from build_daily_market_pack import main as build_main
from self_healing.canary import compare_runs, run_fixture_suite
from healthcheck import _task_metrics, record_task_event


class SelfHealingTests(unittest.TestCase):
    def controller(self, adapters):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return RepairController("market_20260719_1800", Path(temp.name) / "state" / "canary", adapters, sleep=lambda _: None)

    def test_classifies_supported_failures_and_unknown_requires_human(self):
        self.assertEqual(classify_failure("market_20260719_1800", "f1", "generate_content", "Ollama unavailable").failure_category, "ollama_unavailable")
        self.assertEqual(classify_failure("market_20260719_1800", "f2", "collect_news", "HTTP 503").failure_category, "temporary_network_failure")
        unknown = classify_failure("market_20260719_1800", "f3", "x", "unexpected state")
        self.assertEqual(unknown.failure_category, "unknown_failure")
        self.assertTrue(unknown.requires_human_approval)

    def test_quality_gate_path_is_not_misclassified_as_market_data(self):
        failure = classify_failure(
            "market_20260719_1800",
            "f8",
            "final_quality_gate",
            "quality_gate_missing:/workspace/market_20260719_1800/qa_report.json",
        )
        self.assertEqual(failure.failure_category, "unknown_failure")
        self.assertTrue(failure.requires_human_approval)

    def test_ollama_restarts_once_and_resumes(self):
        health_results = iter([{"status": "unhealthy"}, {"status": "healthy"}])
        restarts = []
        controller = self.controller(RepairAdapters(
            health_check_ollama=lambda: next(health_results),
            restart_ollama_once=lambda: restarts.append(True) or {"status": "started"},
        ))
        result = controller.repair("f1", "generate_content", "Ollama unavailable")
        self.assertEqual(result["result"]["status"], RepairStatus.repair_succeeded.value)
        self.assertEqual(len(restarts), 1)
        self.assertTrue(result["result"]["original_failure_resolved"])

    def test_ollama_waits_for_service_after_restart(self):
        health_results = iter([{"status": "unhealthy"}, {"status": "unhealthy"}, {"status": "healthy"}])
        waits = []
        controller = self.controller(RepairAdapters(
            health_check_ollama=lambda: next(health_results),
            restart_ollama_once=lambda: {"status": "started"},
        ))
        result = RepairController.repair(controller, "wait", "generate_content", "Ollama unavailable")
        self.assertEqual(result["result"]["status"], RepairStatus.repair_succeeded.value)
        self.assertTrue(result["result"]["original_failure_resolved"])

    def test_truncated_json_only_closes_structural_delimiters(self):
        self.assertEqual(repair_json_response('{"status":"ok"'), {"status": "ok"})
        self.assertIsNone(repair_json_response('{"status":"'))

    def test_ollama_falls_back_to_gemini(self):
        controller = self.controller(RepairAdapters(
            health_check_ollama=lambda: {"status": "unhealthy"},
            restart_ollama_once=lambda: {"status": "failed"},
            select_gemini_fallback=lambda: {"status": "selected"},
        ))
        result = controller.repair("f1", "generate_content", "Ollama unavailable")
        self.assertEqual(result["result"]["selected_fallback"], "gemini")
        self.assertTrue(result["result"]["repair_action_succeeded"])

    def test_network_failure_uses_bounded_backoff_and_does_not_retry_forever(self):
        calls = []
        waits = []
        controller = RepairController(
            "market_20260719_1801",
            Path(tempfile.mkdtemp()) / "state" / "canary",
            RepairAdapters(retry_collector=lambda step: calls.append(step) or {"status": "success" if len(calls) == 2 else "failed"}),
            sleep=waits.append,
        )
        result = controller.repair("f2", "collect_news", "collector timeout")
        self.assertEqual(result["result"]["retry_count"], 2)
        self.assertEqual(waits, [2, 5])

    def test_model_provider_switch_is_a_successful_repair_with_retry_arguments(self):
        controller = self.controller(RepairAdapters())
        retry_arguments = {
            "run_id": "market_20260719_1800",
            "edition": "morning_close_review",
            "input_path": "/tmp/input.json",
            "provider": "rule_template",
        }
        result = controller.repair(
            "provider_503",
            "generate_content",
            "HTTP 503 from content provider",
            {
                "provider": "rule_template",
                "previous_provider": "gemini",
                "retry_arguments": retry_arguments,
            },
        )
        self.assertEqual(result["result"]["status"], RepairStatus.repair_succeeded.value)
        self.assertTrue(result["result"]["original_failure_resolved"])
        self.assertEqual(result["result"]["retry_arguments"]["provider"], "rule_template")

    def test_market_data_repair_recollects_only_missing_symbols_then_resumes(self):
        collected = []
        resumed = []
        controller = self.controller(RepairAdapters(
            collect_market_quotes=lambda symbols: collected.append(symbols) or {"status": "success", "market_data_version": "v2", "quotes": symbols},
            validate_market_data=lambda data: {"status": "pass"},
            resume_market_pipeline=lambda steps, data: resumed.append((steps, data["market_data_version"])) or {"status": "success"},
        ))
        result = controller.repair("f3", "validate_market_data", "market data incomplete", {"missing_symbols": ["SPX"]})
        self.assertEqual(collected, [["SPX"]])
        self.assertEqual(resumed[0][1], "v2")
        self.assertEqual(result["result"]["status"], RepairStatus.repair_succeeded.value)

    def test_market_data_repair_blocks_when_validation_fails(self):
        controller = self.controller(RepairAdapters(
            collect_market_quotes=lambda symbols: {"status": "partial", "quotes": []},
            validate_market_data=lambda data: {"status": "fail", "reason": "missing SPX"},
            resume_market_pipeline=lambda steps, data: {"status": "success"},
        ))
        result = controller.repair("f4", "validate_market_data", "market data incomplete")
        self.assertEqual(result["result"]["status"], RepairStatus.repair_failed.value)
        self.assertFalse(result["result"]["resume_succeeded"])

    def test_l5_selected_market_plan_drives_existing_repair_adapter(self):
        collected = []
        resumed = []
        selected = classify_error("market_data_missing")
        plan = select_repair_plan(selected, {"step": "validate_market_data"}, execution_mode="automatic")
        controller = self.controller(RepairAdapters(
            collect_market_quotes=lambda symbols: collected.append(symbols) or {"status": "success", "market_data_version": "v3", "quotes": symbols},
            validate_market_data=lambda data: {"status": "pass"},
            resume_market_pipeline=lambda steps, data: resumed.append(steps) or {"status": "success"},
        ))
        result = controller.repair(
            "l5_data",
            "validate_market_data",
            "opaque source failure",
            {
                "error_classification": selected,
                "repair_selection": plan,
                "missing_symbols": ["SPX"],
            },
        )
        assert result["classification"]["failure_category"] == "market_data_incomplete"
        assert collected == [["SPX"]]
        assert resumed and result["result"]["status"] == "repair_succeeded"

    def test_l5_selected_json_plan_retries_and_revalidates_content(self):
        attempts = []
        selected = classify_error("json_parse_failed")
        plan = select_repair_plan(selected, {"step": "generate_content"}, execution_mode="automatic")
        controller = self.controller(RepairAdapters(request_gemini=lambda attempt: attempts.append(attempt) or '{"ok": true}'))
        result = controller.repair(
            "l5_json",
            "generate_content",
            "opaque model failure",
            {"error_classification": selected, "repair_selection": plan},
        )
        assert result["classification"]["failure_category"] == "gemini_json_parse_failure"
        assert attempts == [1]
        assert result["result"]["status"] == "repair_succeeded"
        assert result["result"]["post_repair_validation"]["status"] == "pass"

    def test_gemini_json_repair_retries_twice(self):
        attempts = []
        controller = self.controller(RepairAdapters(request_gemini=lambda attempt: attempts.append(attempt) or ("bad" if attempt == 1 else '{"ok": true}')))
        result = controller.repair("f5", "generate_content", "Gemini JSON parse failure")
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(result["result"]["status"], RepairStatus.repair_succeeded.value)
        self.assertEqual(result["result"]["parsed_output"], {"ok": True})

    def test_gemini_json_repair_uses_rule_template_after_retries(self):
        controller = self.controller(RepairAdapters(
            request_gemini=lambda attempt: "bad",
            use_rule_template=lambda: {"status": "success"},
        ))
        result = controller.repair("f6", "generate_content", "Gemini JSON parse failure")
        self.assertEqual(result["result"]["selected_fallback"], "rule_template")
        self.assertTrue(result["result"]["repair_action_succeeded"])

    def test_repair_limit_stops_third_same_category_attempt(self):
        controller = self.controller(RepairAdapters(retry_collector=lambda step: {"status": "success"}))
        self.assertEqual(controller.repair("a", "collect_news", "collector timeout")["result"]["status"], RepairStatus.repair_succeeded.value)
        self.assertEqual(controller.repair("b", "collect_news", "collector timeout")["result"]["status"], RepairStatus.repair_succeeded.value)
        third = controller.repair("c", "collect_news", "collector timeout")
        self.assertEqual(third["result"]["status"], RepairStatus.repair_failed.value)
        self.assertEqual(third["result"]["blocking_reason"], "repair_limit_exceeded")

    def test_unknown_failure_waits_for_human_approval(self):
        controller = self.controller(RepairAdapters())
        result = controller.repair("f7", "generate_content", "unknown internal condition")
        self.assertEqual(result["result"]["status"], RepairStatus.waiting_human_approval.value)
        self.assertFalse(result["result"]["repair_action_succeeded"])

    def test_json_repair_does_not_invent_values(self):
        self.assertEqual(repair_json_response("说明\n```json\n{\"value\": 3}\n```"), {"value": 3})
        self.assertIsNone(repair_json_response("not json"))

    def test_canary_requires_explicit_mode_and_keeps_delivery_false(self):
        with patch.dict(os.environ, {"SELF_HEALING_CANARY_MODE": "false"}, clear=False):
            with self.assertRaises(RuntimeError):
                run_fixture_suite(Path(tempfile.mkdtemp()))
        with patch.dict(os.environ, {"SELF_HEALING_CANARY_MODE": "true"}, clear=False):
            root = Path(tempfile.mkdtemp())
            report = run_fixture_suite(root)
            self.assertEqual(report["fault_case_count"], 5)
            self.assertTrue(report["repair_agent_ready"])
            self.assertTrue(report["resume_pipeline_ready"])
            self.assertTrue(report["fixture_ready"])
            self.assertFalse(report["production_ready"])
            self.assertFalse(report["delivered"])

    def test_baseline_diff_separates_shared_and_new_failures(self):
        result = compare_runs(
            {"run_id": "baseline", "end_to_end_passed": False, "downstream_failures": ["text_qa"]},
            {"run_id": "fault", "end_to_end_passed": False, "original_failure_resolved": True, "repair_action_succeeded": True, "downstream_failures": ["text_qa"]},
        )
        self.assertEqual(result["causal_relation"], "pre_existing_baseline_failure")
        self.assertEqual(result["shared_failures_with_baseline"], ["text_qa"])
        self.assertEqual(result["repair_induced_failures"], [])

    def test_production_entry_rejects_fault_injection(self):
        with patch.dict(os.environ, {"SELF_HEALING_CANARY_MODE": "false", "SELF_HEALING_FAULT": "collector_timeout"}, clear=False):
            with self.assertRaises(SystemExit):
                build_main(["--edition", "morning_close_review"])

    def test_canary_mode_requires_shadow_run(self):
        with patch.dict(os.environ, {"SELF_HEALING_CANARY_MODE": "true", "SELF_HEALING_FAULT": "none"}, clear=False):
            with self.assertRaises(SystemExit):
                build_main(["--edition", "morning_close_review"])

    def test_task_event_uses_wall_clock_for_daily_metrics(self):
        root = Path(tempfile.mkdtemp())
        with patch("healthcheck.TASK_LOG", root / "task_runs.jsonl"):
            record_task_event("success", time.monotonic() - 0.1, "evening_premarket_watch", started_epoch=time.time() - 0.1)
            metrics = _task_metrics()
        self.assertEqual(metrics["completed_task_count"], 1)
        self.assertEqual(metrics["task_success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
