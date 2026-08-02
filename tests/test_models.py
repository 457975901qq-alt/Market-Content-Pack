import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from models.recovery_models import RecoveryError
from models.reviewer_models import ReviewResult
from models.runtime_models import RuntimeState
from models.validation_models import QualityStatus, ValidationReport


RUN_ID = "market_20260719_2315"
HASH = "a" * 64


def runtime_payload():
    now = datetime.now(timezone.utc)
    return {
        "run_id": RUN_ID,
        "edition": "evening_premarket_watch",
        "output_root": "/tmp/run",
        "started_at": now,
        "updated_at": now,
        "steps": {"health_check": {"step": "health_check", "status": "pending"}},
    }


class ModelTests(unittest.TestCase):
    def test_runtime_state_rejects_bad_run_id(self):
        payload = runtime_payload()
        payload["run_id"] = "bad"
        with self.assertRaises(ValidationError):
            RuntimeState.model_validate(payload)

    def test_recovery_rejects_retry_over_limit(self):
        with self.assertRaises(ValidationError):
            RecoveryError(
                error_type="transient_error", error_code="timeout", step="collect", message="x",
                retryable=True, retry_count=3, max_retries=2, occurred_at=datetime.now(timezone.utc),
            )

    def test_approve_and_pass_are_fail_closed(self):
        with self.assertRaises(ValidationError):
            ReviewResult(
                run_id=RUN_ID, content_hash=HASH,
                reviewer={"type": "deterministic", "tool": "review", "version": "1"},
                decision="approve", confidence=1, critical_findings=["bad"], reviewed_at=datetime.now(timezone.utc),
            )
        with self.assertRaises(ValidationError):
            ValidationReport(run_id=RUN_ID, status=QualityStatus.passed, critical_errors=["bad"], validated_at=datetime.now(timezone.utc), validator_version="1")

    def test_datetime_and_json_round_trip(self):
        state = RuntimeState.model_validate(runtime_payload())
        restored = RuntimeState.model_validate_json(state.model_dump_json())
        self.assertEqual(restored.run_id, RUN_ID)


if __name__ == "__main__":
    unittest.main()
