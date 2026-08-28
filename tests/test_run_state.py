import json
import tempfile
import unittest
from pathlib import Path

import run_state


class RunStateTests(unittest.TestCase):
    def test_atomic_state_round_trip_and_hash_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "outputs"
            state = run_state.create("market_20260719_1800", "evening_premarket_watch", root / "runtime", output)
            state["state_root"] = str(root / "runtime")
            run_state.save(state, root / "runtime")
            artifact = output / "market_content.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"ok":true}\n', encoding="utf-8")
            run_state.mark(state, "generate_content", "success", root / "runtime", artifacts=[artifact])
            restored = run_state.load(state["run_id"], root / "runtime")
            self.assertEqual(run_state.first_resume_step(restored), None)
            self.assertFalse((root / "runtime" / "state" / "market_20260719_1800.json.tmp").exists())

    def test_tampered_artifact_resets_from_own_step(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = run_state.create("market_20260719_1801", "morning_close_review", root / "runtime", root / "outputs")
            state["state_root"] = str(root / "runtime")
            report = root / "outputs" / "qa_report.json"
            report.parent.mkdir(parents=True)
            report.write_bytes(b"original")
            run_state.mark(state, "final_validation", "success", root / "runtime", artifacts=[report])
            report.write_bytes(b"tampered")
            restored = run_state.load(state["run_id"], root / "runtime")
            self.assertEqual(run_state.first_resume_step(restored), "final_validation")

    def test_invalid_artifact_reset_clears_downstream_successes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            output = root / "outputs"
            output.mkdir(parents=True)
            state = run_state.create("market_20260719_1804", "morning_close_review", runtime, output)
            quote = output / "market_quotes.json"
            content = output / "market_content.json"
            quote.write_text("quote-v1", encoding="utf-8")
            content.write_text("content-v1", encoding="utf-8")
            run_state.mark(state, "collect_market_quotes", "success", runtime, artifacts=[quote])
            run_state.mark(state, "generate_content", "success", runtime, artifacts=[content])
            quote.write_text("tampered", encoding="utf-8")
            restored = run_state.load(state["run_id"], runtime)
            resume_step = run_state.first_resume_step(restored)
            run_state.reset_from(restored, resume_step, runtime)
            self.assertEqual(restored["steps"]["collect_market_quotes"]["status"], "pending")
            self.assertEqual(restored["steps"]["generate_content"]["status"], "pending")

    def test_same_run_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime"
            with run_state.run_lock("market_20260719_1805", root):
                with self.assertRaises(RuntimeError):
                    with run_state.run_lock("market_20260719_1805", root):
                        pass

    def test_unknown_step_is_rejected_and_delivery_stays_false(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = run_state.create("market_20260719_1802", "morning_close_review", root / "runtime", root / "outputs")
            with self.assertRaises(ValueError):
                run_state.mark(state, "shell", "success", root / "runtime")
            self.assertFalse(state["delivered"])

    def test_failed_step_clears_after_successful_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = run_state.create("market_20260719_1803", "morning_close_review", root / "runtime", root / "outputs")
            run_state.mark(state, "generate_content", "failed", root / "runtime", error={"error_type": "temporary"})
            run_state.mark(state, "generate_content", "success", root / "runtime")
            restored = run_state.load(state["run_id"], root / "runtime")
            self.assertIsNone(restored["failed_step"])

    def test_canary_state_isolated_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state" / "canary"
            state = run_state.create("market_20260720_1200", "evening_premarket_watch", root, Path(temp) / "outputs" / "canary")
            self.assertEqual(run_state.path(state["run_id"], root), root / "market_20260720_1200.json")


if __name__ == "__main__":
    unittest.main()
