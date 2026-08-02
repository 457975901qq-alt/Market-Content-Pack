"""Bounded, fail-closed repair controller.

Only the callbacks in RepairAdapters can perform work. No model output is
treated as executable code and no arbitrary shell command is accepted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .gap_analyzer import analyze_gap
from .repair_planner import RepairPlanner
from market_quotes import CORE_SYMBOLS, collect_quotes
from repair_validation import validate_repair_result


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "self_healing_policy.json"
RUN_ID_RE = re.compile(r"^market_\d{8}_\d{4}$")


class FailureCategory(str, Enum):
    ollama_unavailable = "ollama_unavailable"
    temporary_network_failure = "temporary_network_failure"
    market_data_incomplete = "market_data_incomplete"
    gemini_json_parse_failure = "gemini_json_parse_failure"
    unknown_failure = "unknown_failure"


class RepairStatus(str, Enum):
    failure_detected = "failure_detected"
    failure_classified = "failure_classified"
    repair_planned = "repair_planned"
    repair_executing = "repair_executing"
    repair_validating = "repair_validating"
    repair_succeeded = "repair_succeeded"
    repair_failed = "repair_failed"
    waiting_human_approval = "waiting_human_approval"


@dataclass
class FailureClassification:
    failure_id: str
    run_id: str
    failure_category: str
    failed_step: str
    root_cause: str
    confidence: float
    risk_level: str
    recommended_action: str
    resume_from: str
    requires_human_approval: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairPlan:
    repair_id: str
    run_id: str
    trigger_error: str
    repair_scope: list[str]
    preserved_steps: list[str]
    reset_steps: list[str]
    affected_artifacts: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    selected_tools: list[str] = field(default_factory=list)
    max_attempts: int = 2
    status: str = "planned"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairAdapters:
    """Fixed callback bindings for repair actions.

    Tests and integrations inject these callbacks. The controller never
    accepts a callable path from a model or a config file.
    """

    health_check_ollama: Callable[[], dict[str, Any]] | None = None
    restart_ollama_once: Callable[[], dict[str, Any]] | None = None
    select_gemini_fallback: Callable[[], dict[str, Any]] | None = None
    retry_collector: Callable[[str], dict[str, Any]] | None = None
    collect_market_quotes: Callable[[list[str]], dict[str, Any]] | None = None
    validate_market_data: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    resume_market_pipeline: Callable[[list[str], dict[str, Any]], dict[str, Any]] | None = None
    request_gemini: Callable[[int], str] | None = None
    use_rule_template: Callable[[], dict[str, Any]] | None = None
    validate_repair_result: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def classify_failure(run_id: str, failure_id: str, step: str, message: str, context: dict[str, Any] | None = None) -> FailureClassification:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must match market_YYYYMMDD_HHMM")
    text = message.lower()
    if "quality_gate" in text or "qa_report" in text:
        category, resume, action = FailureCategory.unknown_failure, step, "stop and request human approval for the quality-gate artifact failure"
    elif "ollama" in text:
        category, resume, action = FailureCategory.ollama_unavailable, "generate_content", "healthcheck Ollama, restart once, then select Gemini"
    elif "timeout" in text or "503" in text or "connection" in text:
        category, resume, action = FailureCategory.temporary_network_failure, step, "retry the failed collector with bounded backoff and use configured backup"
    elif ("market_data" in text or "market data" in text) and ("missing" in text or "incomplete" in text):
        category, resume, action = FailureCategory.market_data_incomplete, "collect_market_quotes", "recollect only missing market fields, validate, then resume dependent steps"
    elif "json" in text or "parse" in text:
        category, resume, action = FailureCategory.gemini_json_parse_failure, "generate_content", "repair JSON deterministically, retry twice, then use rule template"
    else:
        category, resume, action = FailureCategory.unknown_failure, step, "stop and request human approval"
    human = category == FailureCategory.unknown_failure
    return FailureClassification(
        failure_id=failure_id,
        run_id=run_id,
        failure_category=category.value,
        failed_step=step,
        root_cause=message,
        confidence=0.95 if not human else 0.5,
        risk_level="high" if human else "low",
        recommended_action=action,
        resume_from=resume,
        requires_human_approval=human,
    )


def _apply_selected_classification(
    classification: FailureClassification,
    selected: dict[str, Any] | None,
    *,
    step: str,
    context: dict[str, Any],
) -> FailureClassification:
    """Translate L5 classifier output into the controller's fixed strategies."""

    if not isinstance(selected, dict):
        return classification
    category = str(selected.get("category") or "UNKNOWN")
    error_code = str((context.get("error_classification") or {}).get("error_code") or "")
    retry_step = str(selected.get("retry_step") or step)
    classification.recommended_action = str(
        (context.get("error_classification") or {}).get("recommended_action")
        or classification.recommended_action
    )
    classification.resume_from = retry_step

    # These are the only L5-3 mappings with existing fixed adapters. Other
    # categories remain human-gated until a corresponding safe adapter exists.
    if category == "DATA_SOURCE":
        classification.failure_category = FailureCategory.market_data_incomplete.value
        classification.requires_human_approval = False
        classification.confidence = 0.98
        classification.risk_level = "low"
    elif category == "MODEL_OUTPUT" and error_code in {
        "empty_response",
        "json_parse_failed",
        "empty_required_field",
        "schema_validation_failed",
    }:
        classification.failure_category = FailureCategory.gemini_json_parse_failure.value
        classification.requires_human_approval = False
        classification.confidence = 0.95
        classification.risk_level = "low"
        classification.resume_from = "generate_content"
    elif category == "MODEL_OUTPUT" and error_code == "provider_unavailable" and context.get("provider", "ollama") == "ollama":
        classification.failure_category = FailureCategory.ollama_unavailable.value
        classification.requires_human_approval = False
        classification.confidence = 0.95
        classification.risk_level = "low"
        classification.resume_from = "generate_content"
    elif category == "MODEL_OUTPUT" and error_code in {
        "provider_http_error",
        "content_provider_chain_exhausted",
    } and step == "generate_content":
        # A provider HTTP failure is recoverable only through the already
        # selected, fixed fallback chain. The executor will retry with the
        # replacement arguments supplied by the controller.
        classification.failure_category = FailureCategory.temporary_network_failure.value
        classification.requires_human_approval = False
        classification.confidence = 0.95
        classification.risk_level = "low"
        classification.resume_from = "generate_content"
    elif category == "MODEL_OUTPUT" and error_code == "timeout" and step in {
        "collect_sources",
        "collect_news",
        "extract_web_content",
    }:
        classification.failure_category = FailureCategory.temporary_network_failure.value
        classification.requires_human_approval = False
        classification.confidence = 0.9
        classification.risk_level = "low"
    return classification


def repair_json_response(raw: str) -> dict[str, Any] | None:
    """Extract one JSON object without inventing or changing its values."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0:
            return None
        candidate = cleaned[start : end + 1] if end > start else cleaned[start:]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            # Only repair truncated structure. Values and keys are never
            # invented; an unterminated string or malformed token remains a
            # hard failure and is handled by the bounded retry/fallback path.
            repaired = _close_truncated_json(candidate)
            if repaired is None:
                return None
            try:
                parsed = json.loads(repaired)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None


def _close_truncated_json(value: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or (char == "]" and stack[-1] != "[") or (char == "}" and stack[-1] != "{"):
                return None
            stack.pop()
    if in_string:
        return None
    return value + "".join("}" if opening == "{" else "]" for opening in reversed(stack))


def _default_ollama_health() -> dict[str, Any]:
    try:
        from healthcheck import check_ollama

        return check_ollama()
    except Exception as exc:
        return {"status": "unhealthy", "blocking_reason": f"healthcheck_error:{type(exc).__name__}"}


def _restart_ollama_once() -> dict[str, Any]:
    """Start the local service once; never kills an existing user process."""
    if not shutil.which("ollama"):
        return {"status": "unavailable", "blocking_reason": "ollama_command_missing"}
    try:
        process = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"status": "started", "pid": process.pid}
    except OSError as exc:
        return {"status": "failed", "blocking_reason": f"{type(exc).__name__}: {exc}"}


def _wait_for_ollama(health: Callable[[], dict[str, Any]], sleep: Callable[[float], None], first: dict[str, Any]) -> dict[str, Any]:
    """Give a newly started local service a short bounded readiness window."""
    if first.get("status") == "healthy":
        return first
    for delay in (0.5, 1.0, 2.0):
        sleep(delay)
        current = health()
        if current.get("status") == "healthy":
            return current
    return first


def _default_gemini_fallback() -> dict[str, Any]:
    try:
        from healthcheck import check_gemini

        result = check_gemini()
        return {"status": "selected" if result.get("status") == "healthy" else "unavailable", "health": result}
    except Exception as exc:
        return {"status": "unavailable", "blocking_reason": f"gemini_healthcheck_error:{type(exc).__name__}"}


def _default_retry_collector(step: str) -> dict[str, Any]:
    """Retry only the fixed source-router entry point.

    The repair controller deliberately has no generic command execution API.
    A collector is retryable only when it is one of the registered source
    steps, and the callable is fixed to this repository's source_router.py.
    """
    allowed = {"collect_sources", "collect_news", "collect_x", "collect_rss", "collect_exa", "collect_jina"}
    if step not in allowed:
        return {"status": "failed", "error_type": "configuration_error", "blocking_reason": f"collector_not_registered:{step}"}
    script = ROOT / "source_router.py"
    if not script.exists():
        return {"status": "failed", "error_type": "dependency_error", "blocking_reason": "source_router_missing"}
    try:
        completed = subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "error_type": "transient_error", "blocking_reason": type(exc).__name__}
    if completed.returncode == 0:
        return {"status": "success", "selected_fallback": "rss_if_configured", "step": step}
    return {"status": "failed", "error_code": str(completed.returncode), "step": step, "stderr_tail": completed.stderr[-500:]}


def _default_market_quotes(symbols: list[str]) -> dict[str, Any]:
    selected = list(dict.fromkeys([*CORE_SYMBOLS, *(symbol.upper() for symbol in symbols)]))
    edition = os.environ.get("MARKET_EDITION", "evening_premarket_watch")
    return collect_quotes(edition, symbols=selected)


class RepairController:
    def __init__(self, run_id: str, canary_root: Path, adapters: RepairAdapters | None = None, policy_path: Path = DEFAULT_POLICY, sleep: Callable[[float], None] = time.sleep):
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("run_id must match market_YYYYMMDD_HHMM")
        self.run_id = run_id
        self.root = canary_root / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapters = adapters or RepairAdapters()
        self.config = _policy(policy_path)
        self.sleep = sleep
        self.attempts: dict[str, int] = {}
        self.total_repairs = 0

    def _write(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.root / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return path

    def _event(self, status: RepairStatus, classification: FailureClassification, **extra: Any) -> None:
        path = self.root / "repair_events.jsonl"
        payload = {"status": status.value, "run_id": self.run_id, "failure_category": classification.failure_category, "timestamp": _now(), **extra}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # JSON remains the portable source of truth; SQLite is an audit index
        # and is deliberately best-effort so it cannot block recovery.
        self._write("repair_state.json", {"run_id": self.run_id, "status": status.value, "updated_at": payload["timestamp"], "failure_category": classification.failure_category, "failed_step": classification.failed_step, "attempt": extra.get("attempt"), "delivered": False})
        try:
            from runtime_index import index_for_state_root

            index_for_state_root(self.root).audit(self.run_id, "repair_state", payload)
        except Exception:
            pass

    def repair(self, failure_id: str, step: str, message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        classification = classify_failure(self.run_id, failure_id, step, message, context)
        classification = _apply_selected_classification(
            classification,
            context.get("repair_selection"),
            step=step,
            context=context,
        )
        self._write(f"failure_{failure_id}.json", classification.to_dict())
        gap = analyze_gap(
            validation_errors=context.get("validation_errors") or [{"step": step, "message": message}],
            current_state=context.get("current_state") or context,
            artifact_manifest=context.get("artifact_manifest"),
            tool_decision_history=context.get("tool_decision_history"),
            run_id=self.run_id,
        )
        self._write(f"gap_analysis_{failure_id}.json", gap)
        self._event(RepairStatus.failure_detected, classification)
        self._event(RepairStatus.failure_classified, classification, confidence=classification.confidence)
        category = classification.failure_category
        self.attempts[category] = self.attempts.get(category, 0) + 1
        attempt = self.attempts[category]
        if attempt > int(self.config["max_same_category_attempts"]) or self.total_repairs >= int(self.config["max_total_repairs"]):
            return self._blocked(classification, "repair_limit_exceeded", attempt)
        if classification.requires_human_approval:
            return self._blocked(classification, "unknown_failure_requires_human_approval", attempt, waiting=True)
        plan = self._plan(classification, message, context, gap)
        self._event(RepairStatus.repair_planned, classification, repair_id=plan.repair_id, resume_from=classification.resume_from, gap=gap)
        self.total_repairs += 1
        self._event(RepairStatus.repair_executing, classification, attempt=attempt)
        result = self._execute(classification, context)
        validator = self.adapters.validate_repair_result or validate_repair_result
        post_validation = validator(classification.to_dict(), result, context)
        result["post_repair_validation"] = post_validation
        result["validation_passed"] = bool(result.get("validation_passed") is True and post_validation.get("passed") is True)
        self._event(RepairStatus.repair_validating, classification, validation_result=post_validation)
        success = bool(result.get("repair_action_succeeded") and result.get("resume_succeeded", True) and result.get("validation_passed", True))
        status = RepairStatus.repair_succeeded if success else RepairStatus.repair_failed
        self._event(status, classification, attempt=attempt, **result)
        payload = {"classification": classification.to_dict(), "plan": plan.to_dict(), "result": {**result, "status": status.value, "attempt": attempt}}
        self._write("latest_result.json", payload)
        return payload

    def _plan(self, classification: FailureClassification, message: str, context: dict[str, Any], gap: dict[str, Any]) -> RepairPlan:
        plan_data = RepairPlanner(self.root, max_attempts=int(self.config["max_same_category_attempts"])).build(
            run_id=self.run_id,
            trigger_error=message,
            gap=gap,
            current_state=context.get("current_state") or context,
            selected_tools=list(context.get("selected_tools") or []),
        )
        return RepairPlan(**plan_data)

    def _execute(self, classification: FailureClassification, context: dict[str, Any]) -> dict[str, Any]:
        category = classification.failure_category
        if category == FailureCategory.ollama_unavailable.value:
            health = self.adapters.health_check_ollama or _default_ollama_health
            current = health()
            if current.get("status") == "healthy":
                return {"repair_action_succeeded": True, "original_failure_resolved": True, "resume_from": "generate_content", "resume_succeeded": True, "selected_fallback": None, "validation_passed": True}
            restart = self.adapters.restart_ollama_once or _restart_ollama_once
            restarted = restart()
            after = _wait_for_ollama(health, self.sleep, health()) if restarted.get("status") == "started" else health()
            if after.get("status") == "healthy":
                return {"repair_action_succeeded": True, "original_failure_resolved": True, "resume_from": "generate_content", "resume_succeeded": True, "restart_result": restarted, "validation_passed": True}
            fallback = self.adapters.select_gemini_fallback or _default_gemini_fallback
            selected = fallback()
            ok = selected.get("status") == "selected"
            return {"repair_action_succeeded": ok, "original_failure_resolved": ok, "resume_from": "generate_content", "resume_succeeded": ok, "selected_fallback": "gemini" if ok else None, "restart_result": restarted, "validation_passed": ok}
        if category == FailureCategory.temporary_network_failure.value:
            replacement_provider = str(context.get("provider") or "")
            previous_provider = str(context.get("previous_provider") or "")
            retry_arguments = context.get("retry_arguments")
            if (
                classification.failed_step == "generate_content"
                and isinstance(retry_arguments, dict)
            ):
                switched = bool(replacement_provider and replacement_provider != previous_provider)
                return {
                    "repair_action_succeeded": True,
                    "original_failure_resolved": True,
                    "resume_from": "generate_content",
                    "resume_succeeded": True,
                    "retry_count": 1,
                    "selected_fallback": replacement_provider if switched else None,
                    "retry_arguments": retry_arguments,
                    "validation_passed": True,
                }
            retry = self.adapters.retry_collector or _default_retry_collector
            waits = self.config["network_backoff_seconds"]
            for attempt, wait in enumerate(waits, 1):
                self.sleep(wait)
                result = retry(classification.failed_step)
                if result.get("status") == "success":
                    return {"repair_action_succeeded": True, "original_failure_resolved": True, "resume_from": classification.failed_step, "resume_succeeded": True, "retry_count": attempt, "selected_fallback": result.get("selected_fallback"), "validation_passed": True}
            return {"repair_action_succeeded": False, "original_failure_resolved": False, "resume_from": classification.failed_step, "resume_succeeded": False, "retry_count": len(waits), "validation_passed": False}
        if category == FailureCategory.market_data_incomplete.value:
            symbols = list(context.get("missing_symbols") or ["SPX", "NDX", "DJI"])
            collect = self.adapters.collect_market_quotes or _default_market_quotes
            data = collect(symbols)
            validate = self.adapters.validate_market_data
            validation = validate(data) if validate else {"status": "pass" if data.get("status") == "success" else "fail"}
            valid = validation.get("status") == "pass"
            if not valid:
                return {"repair_action_succeeded": False, "original_failure_resolved": False, "resume_from": "collect_market_quotes", "resume_succeeded": False, "validation_passed": False, "market_data": data, "validation_result": validation}
            resume = self.adapters.resume_market_pipeline
            resumed = resume(["validate_market_data", "generate_content", "final_validation"], data) if resume else {"status": "unavailable", "reason": "resume_market_pipeline_binding_missing"}
            ok = resumed.get("status") == "success"
            return {"repair_action_succeeded": True, "original_failure_resolved": True, "resume_from": "collect_market_quotes", "resume_succeeded": ok, "validation_passed": True, "market_data": data, "validation_result": validation, "resume_result": resumed, "market_data_version": data.get("market_data_version")}
        if category == FailureCategory.gemini_json_parse_failure.value:
            request = self.adapters.request_gemini
            if request:
                for attempt in range(1, int(self.config["gemini_max_retries"]) + 1):
                    parsed = repair_json_response(request(attempt))
                    if parsed is not None:
                        return {"repair_action_succeeded": True, "original_failure_resolved": True, "resume_from": "generate_content", "resume_succeeded": True, "retry_count": attempt, "selected_fallback": "gemini", "validation_passed": True, "parsed_output": parsed}
            template = self.adapters.use_rule_template
            if template:
                result = template()
                ok = result.get("status") == "success"
                return {"repair_action_succeeded": ok, "original_failure_resolved": ok, "resume_from": "generate_content", "resume_succeeded": ok, "selected_fallback": "rule_template" if ok else None, "validation_passed": ok}
            return {"repair_action_succeeded": False, "original_failure_resolved": False, "resume_from": "generate_content", "resume_succeeded": False, "validation_passed": False, "blocking_reason": "no_json_repair_or_template_binding"}
        return {"repair_action_succeeded": False, "original_failure_resolved": False, "resume_from": classification.resume_from, "resume_succeeded": False, "validation_passed": False, "blocking_reason": "human_approval_required"}

    def _blocked(self, classification: FailureClassification, reason: str, attempt: int, waiting: bool = False) -> dict[str, Any]:
        status = RepairStatus.waiting_human_approval if waiting else RepairStatus.repair_failed
        payload = {"classification": classification.to_dict(), "result": {"status": status.value, "repair_action_succeeded": False, "original_failure_resolved": False, "resume_succeeded": False, "validation_passed": False, "attempt": attempt, "blocking_reason": reason}}
        self._event(status, classification, attempt=attempt, blocking_reason=reason)
        self._write("latest_result.json", payload)
        return payload
