#!/usr/bin/env python3
"""Resumable, fail-closed runner for text market analysis artifacts."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from security import build_subprocess_env
import run_state
from agent.action import AgentAction
from agent_loop import ControlledAgentLoop, load_agent_policy
from edition_profiles import is_schedule_slot, resolve_edition_context
from error_classifier import classify_error
from execution_planner import ExecutionPlanner, executor_step_for
from function_calling.arguments import (
    CollectMarketDataArgs,
    CollectNewsArgs,
    ExtractWebContentArgs,
    FinalQualityGateArgs,
    GenerateContentArgs,
    ValidateContentArgs,
    ValidateMarketDataArgs,
)
from function_calling.business_bindings import BusinessContext, build_business_bindings
from function_calling.function_executor import FunctionExecutor
from function_calling.registry import build_registry
from function_calling.tool_call import FunctionCall, FunctionStatus
from observability import (
    LOGICAL_STEP_TO_STAGE,
    STEP_TO_STAGE,
    PIPELINE_VERSION,
    RunContext,
    RunObserver,
    TraceSession,
    redact,
)
from repair_selector import select_repair_plan
from self_healing.agent import RepairAdapters, RepairController
from self_healing.gap_analyzer import GapAnalyzer
from self_healing.repair_planner import RepairPlanner
from tool_router import RouterBlocked, ToolRouter
from delivery_report import render_delivery_report, render_delivery_report_html
from configuration import load_runtime_policy
from market_quotes import CORE_SYMBOLS
from agent import AgentCheckpointStore, AgentState, DailyMarketAgent, FinishPolicy, ModelAssistedAgentPlanner, RuleBasedAgentPlanner
from agent.production import ProductionCallbacks, ProductionToolExecutor, build_production_bindings
from canary_controls import (
    RunMode,
    RunModeConflict,
    build_readiness_evidence,
    delivery_preflight,
    evaluate_canary_stability,
    read_delivery_controls,
    record_canary_run,
    resolve_run_mode,
)


ROOT = Path(__file__).resolve().parent
TOKYO = ZoneInfo("Asia/Tokyo")
_TRACE_SESSION: TraceSession | None = None
_RUN_LOCK_CONTEXT = None
_HEALTH_CONTEXT: dict[str, Path] | None = None
_OBSERVABILITY: RunObserver | None = None
_OBSERVABILITY_STATE: dict | None = None
SELF_HEALING_FAULTS = {
    "none",
    "ollama_unavailable",
    "collector_timeout",
    "collector_http_503",
    "market_data_incomplete",
    "gemini_invalid_json",
}


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_command(command: list[str], env: dict[str, str], log_path: Path, timeout_seconds: float | None = None) -> tuple[int, str, str]:
    if timeout_seconds is None:
        timeout_seconds = float(env.get("STEP_TIMEOUT_SECONDS", "600"))
    try:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env, check=False, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        stderr = f"step_timeout_after_{timeout_seconds:g}s: {stderr}".strip()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"command": command, "returncode": 124, "status": "timeout", "timeout_seconds": timeout_seconds, "stdout": stdout[-4000:], "stderr": stderr[-4000:]}, ensure_ascii=False) + "\n")
        print(stderr, file=sys.stderr)
        return 124, stdout, stderr
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"command": command, "returncode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}, ensure_ascii=False) + "\n")
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode, completed.stdout, completed.stderr


def _run_id() -> str:
    return f"market_{datetime.now(TOKYO).strftime('%Y%m%d_%H%M')}_{secrets.token_hex(2)}"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_offline_evaluation_case(
    run_id: str,
    edition: str,
    source_items: list[dict],
    content_data: dict,
) -> dict:
    """Build the complete deterministic-evaluation case for a pipeline run.

    Keep the reference schema explicit here so the offline evaluator cannot
    silently receive an incomplete case when the content shape changes.
    """
    source_urls = [item.get("source_url") for item in source_items if item.get("source_url")]
    analysis_text = content_data.get("analysis_text")
    if isinstance(analysis_text, dict):
        expected_theme = analysis_text.get("title") or "market_content_pack"
    elif isinstance(analysis_text, str) and analysis_text.strip():
        expected_theme = analysis_text.strip()
    else:
        expected_theme = "market_content_pack"

    return {
        "case_id": run_id,
        "edition": edition,
        "input": {
            "source_urls": source_urls,
            "source_ids": [],
            "tickers": [],
            "report_date": content_data.get("date"),
            "data_cutoff_date": content_data.get("date"),
            "delivery_allowed": False,
            "text": content_data.get("summary", ""),
        },
        "reference": {
            "required_facts": [],
            "required_sources": [],
            "expected_theme": expected_theme,
            "allowed_tickers": [],
            "forbidden_claims": [],
            "expected_result": "fail",
        },
    }


def _function_calling_policy() -> dict:
    path = ROOT / "config" / "function_calling_policy.json"
    try:
        value = _read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _auto_optimization_enabled(env: dict[str, str]) -> bool:
    """Return whether bounded automatic recovery is enabled for this run.

    The policy file remains the hard safety boundary. The environment flag is
    an explicit operator switch; it cannot enable recovery when the policy
    disables it, and it never enables delivery.
    """
    policy_path = ROOT / "config" / "self_healing_policy.json"
    try:
        policy = _read_json(policy_path)
    except (OSError, ValueError):
        policy = {}
    policy_enabled = bool(policy.get("enabled", True))
    requested = str(env.get("AUTO_OPTIMIZATION_ENABLED", "true")).strip().lower()
    self_healing = str(env.get("SELF_HEALING_ENABLED", "true")).strip().lower()
    enabled_values = {"1", "true", "yes", "on"}
    return policy_enabled and requested in enabled_values and self_healing in enabled_values


def _paths(output_root: Path, run_id: str, shadow: bool = False, legacy_shadow: bool = False, canary: bool = False) -> dict[str, Path | bool]:
    shadow_root = ROOT / "runtime" / ("canary" if canary else "shadow") / run_id
    log_root = ROOT / "logs" / ("canary" if canary else "shadow") / run_id if shadow and not legacy_shadow else output_root / "logs"
    review_root = shadow_root if shadow and not legacy_shadow else ROOT / "runtime" / "reviews" / run_id
    return {
        "content": output_root / "market_content",
        "github": output_root / "github_ai_projects",
        "sources": output_root / "market_sources",
        "images": output_root / "images",
        "market_quotes": output_root / "market_sources" / "market_quotes.json",
        "logs": log_root,
        "review": review_root,
        "evaluation": log_root / "evaluation_report.json",
        "shadow_root": shadow_root,
        "is_shadow": shadow,
    }


def _base_env(paths: dict[str, Path], edition: str, prompt_context: str, prompt_version: str, provider: str) -> dict[str, str]:
    safe_keys = {
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
        "OLLAMA_BASE_URL", "OLLAMA_MODEL", "GEMINI_MODEL", "OPENAI_MODEL",
        "MARKET_RUN_ID", "MARKET_SOURCE_MODE", "MARKET_SOURCE_TIMEOUT_SECONDS",
        "MARKET_SOURCE_MAX_ITEMS", "MARKET_ARTICLE_LIMIT", "MARKET_NEWS_QUERY",
        "MARKET_TEXT_ONLY", "MARKET_DISABLE_EXTERNAL_SOURCES", "MARKET_PROMPT_VERSION",
        "MARKET_SECONDARY_PROVIDER", "MASSIVE_BASE_URL", "MASSIVE_MARKET_INTERVAL",
        "SELF_HEALING_ENABLED", "SELF_HEALING_CANARY_MODE", "SELF_HEALING_FAULT",
        "REVIEWER_PROVIDER", "REVIEWER_MODEL", "TOOL_ROUTER_MODE", "DRY_RUN",
    }
    policy = load_runtime_policy()
    secret_names = ["GITHUB_TOKEN", "EXA_API_KEY", "X_SOURCE_TOKEN"]
    secondary_provider = os.environ.get("MARKET_SECONDARY_PROVIDER", "massive").strip().lower()
    if secondary_provider == "massive":
        secret_names.append("MASSIVE_API_KEY")
    if provider in {"gemini", "auto"}:
        secret_names.append("GEMINI_API_KEY")
    if provider in {"openai", "auto"}:
        secret_names.append("OPENAI_API_KEY")
    env = build_subprocess_env(
        allowed_keys=sorted(safe_keys),
        secret_names=secret_names,
        consumer="subprocess",
        purpose="child_process",
        run_id=os.environ.get("MARKET_RUN_ID", "unspecified"),
    )
    env.update({
        "DRY_RUN": "true",
        "MARKET_CONTENT_OUTPUT_DIR": str(paths["content"]),
        "MARKET_CONTENT_JSON": str(paths["content"] / "market_content.json"),
        "MARKET_SOURCE_OUTPUT_DIR": str(paths["sources"]),
        "MARKET_QUOTES_JSON": str(paths["market_quotes"]),
        "GITHUB_ERROR_LOG": str(paths["logs"] / "github_errors.log"),
        "GITHUB_OUTPUT_DIR": str(paths["github"]),
        "MARKET_CONTENT_ERROR_LOG": str(paths["logs"] / "market_content_errors.log"),
        "MARKET_EDITION": edition,
        "MARKET_EDITION_CONTEXT": prompt_context,
        "MARKET_PROMPT_VERSION": prompt_version,
        "MARKET_CONTENT_PROVIDER": provider,
        "MARKET_IMAGE_OUTPUT_DIR": str(paths["images"]),
        "STEP_TIMEOUT_SECONDS": str(policy.get("step_timeout_seconds", 600)),
    })
    return env


def _transition(state: dict, step: str, status: str, state_root: Path, log_path: Path, error: dict | None = None, artifacts: list[Path] | None = None, metadata: dict[str, Any] | None = None) -> None:
    classification = None
    repair_selection = None
    enriched_error = error
    if error is not None or status == "failed":
        error_payload = error if isinstance(error, dict) else {}
        raw_code = error_payload.get("error_code") or error_payload.get("error_type") or "unknown_error"
        classification = classify_error(raw_code, {**error_payload, "step": step})
        if status == "failed":
            repair_selection = select_repair_plan(classification, {**error_payload, "step": step})
        raw_message = str(error_payload.get("message") or error_payload.get("raw_message") or "")[:4000]
        enriched_error = {**error_payload, **classification, "raw_message": raw_message}
        if repair_selection is not None:
            enriched_error["repair_selection"] = repair_selection
        enriched_error = redact(enriched_error)

    run_state.mark(state, step, status, state_root, error=enriched_error, artifacts=artifacts)
    if step in run_state.LOGICAL_STEP_TO_EXECUTOR and run_state.LOGICAL_STEP_TO_EXECUTOR[step] == step:
        run_state.mark_logical(state, step, status, state_root, error=enriched_error, artifacts=artifacts)
    event = {
        "run_id": state["run_id"],
        "timestamp": run_state.now(),
        "step": step,
        "status": status,
        "error_code": classification["error_code"] if classification else None,
        "category": classification["category"] if classification else None,
        "recoverable": classification["recoverable"] if classification else None,
        "recommended_action": classification["recommended_action"] if classification else None,
        "retry_step": classification["retry_step"] if classification else None,
        "fallback_target": classification["fallback_target"] if classification else None,
        "raw_message": (str((error or {}).get("message") or (error or {}).get("raw_message") or "")[:4000] if isinstance(error, dict) else ""),
        "repair_selection": repair_selection,
        "agent_action": (metadata or {}).get("agent_action"),
        "tool": (metadata or {}).get("tool"),
        "agent_stage": (metadata or {}).get("agent_stage"),
        "failure_type": ((error or {}).get("failure") or {}).get("failure_category") if isinstance(error, dict) else None,
        "failure_context": ((error or {}).get("failure") or {}).get("details") if isinstance(error, dict) else None,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**event, "error": enriched_error}, ensure_ascii=False) + "\n")
    if _TRACE_SESSION is not None:
        _TRACE_SESSION.step(step, status, {"error_type": (enriched_error or {}).get("error_type", ""), "category": (classification or {}).get("category", ""), "failure_type": event["failure_type"], "failure_context": event["failure_context"], "retry_count": state.get("retry_count", 0), **(metadata or {})})
    if _OBSERVABILITY is not None:
        _OBSERVABILITY.transition(step, status, enriched_error, {"retry_count": state.get("retry_count", 0), "failure_type": event["failure_type"], "failure_context": event["failure_context"], **(metadata or {})})


def _step_artifacts(paths: dict[str, Path], step: str) -> list[Path]:
    if step == "generate_content":
        return [paths["content"] / "market_content.json"]
    if step == "collect_github":
        return [paths["github"] / "ai_open_source_projects.json"]
    if step == "collect_sources":
        return [paths["sources"] / "normalized_materials.json", paths["sources"] / "filtered_materials.json", paths["sources"] / "source_status.json", paths["sources"] / "web_content.json"]
    if step == "collect_market_quotes":
        return [paths["market_quotes"]]
    if step == "final_validation":
        artifacts = [paths["logs"] / "qa_report.json"]
        image_path = paths["images"] / "market_content.svg"
        image_qa = paths["logs"] / "image_qa.json"
        if image_path.exists():
            artifacts.append(image_path)
        if image_qa.exists():
            artifacts.append(image_qa)
        return artifacts
    if step == "build_review_package":
        return [paths["review"] / "review_package.json"]
    if step in {"reviewer_agent", "reviewer_gate"}:
        return [paths["review"] / "review_result.json"]
    if step == "offline_evaluation":
        return [paths["evaluation"]]
    return []


def _write_review_package(state: dict, paths: dict[str, Path | bool], manifest_path: Path) -> Path:
    review_root = paths["review"]
    review_root.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    package = {
        "run_id": state["run_id"],
        "edition": state["edition"],
        "content_hash": manifest.get("content_hash"),
        "market_content_pack": str(paths["content"] / "market_content.json"),
        "validated_market_data": str(paths["market_quotes"]),
        "source_urls": [],
        "output_mode": "image" if not state.get("text_only", True) else "text",
        "image_files": [str(paths["images"] / "market_content.svg")] if not state.get("text_only", True) else [],
        "validation_report": str(paths["logs"] / "qa_report.json"),
        "quality_gate_result": manifest.get("qa_status"),
        "tool_decision_history": str(paths["logs"] / "commands.jsonl"),
        "warnings": [],
        "created_at": run_state.now(),
    }
    source_manifest = paths["sources"] / "normalized_materials.json"
    if source_manifest.exists():
        materials = _read_json(source_manifest)
        package["source_urls"] = [item.get("source_url") for item in materials if item.get("source_url")]
    target = review_root / "review_package.json"
    run_state.atomic_write_json(target, package)
    return target


def _write_text_qa_report(paths: dict[str, Path]) -> Path:
    """Write the shared text validation artifact."""
    from text_validation import validate_text_artifacts

    report = paths["logs"] / "qa_report.json"
    result = validate_text_artifacts(
        paths["content"] / "market_content.json",
    )
    result["created_at"] = run_state.now()
    run_state.atomic_write_json(report, result)
    return report


def _final_validation_result(paths: dict[str, Path], qa_ok: bool) -> tuple[bool, str]:
    """Read the shared text validation result for the final gate."""
    report_path = paths.get("logs", paths["content"].parent / "logs") / "qa_report.json"
    try:
        report = _read_json(report_path)
    except (OSError, ValueError):
        report = {}
    if qa_ok and report.get("status") == "pass":
        return True, "text content and text QA passed"
    return False, "shared text validation must pass"


def _reviewer_gate_result(result_path: Path, content_path: Path, run_id: str) -> tuple[bool, str]:
    """Validate the immutable reviewer result against current artifacts."""
    try:
        from models.reviewer_models import ReviewDecision, ReviewResult

        result = ReviewResult.model_validate(_read_json(result_path))
    except Exception as exc:
        return False, f"review_result_invalid:{type(exc).__name__}"
    if result.run_id != run_id:
        return False, "review_run_id_mismatch"
    if not content_path.exists() or run_state.sha256(content_path) != result.content_hash:
        return False, "review_content_hash_mismatch"
    if result.decision is not ReviewDecision.approve:
        return False, f"review_decision_{result.decision.value}"
    if result.critical_findings:
        return False, "review_critical_findings_present"
    if not result.checks:
        return False, "review_checks_missing"
    return True, "reviewer approval matches current artifact"


def _health_report(paths: dict[str, Path]) -> Path:
    # Read-only checks are captured inside the run directory; no production
    # health report is overwritten by a Shadow run.
    import healthcheck

    report = paths["logs"] / "healthcheck.json"
    run_state.atomic_write_json(
        report,
        healthcheck.collect_report(
            task_log=paths["logs"] / "task_runs.jsonl",
            logs_root=paths["logs"],
        ),
    )
    return report


def _write_manifest(state: dict, paths: dict[str, Path], qa_ok: bool, edition: str, provider: str | None = None) -> Path:
    _sync_execution_plan(state, paths)
    content = paths["content"] / "market_content.json"
    content_data = _read_json(content) if content.exists() else {}
    artifact_hashes = {}
    for target in [
        content,
        paths["market_quotes"],
        paths.get("plan"), paths.get("decisions"),
        paths["sources"] / "normalized_materials.json",
        paths["sources"] / "filtered_materials.json",
        paths["sources"] / "source_status.json",
        paths["images"] / "market_content.svg",
        paths["logs"] / "image_qa.json",
    ]:
        if isinstance(target, Path) and target.exists() and target.is_file():
            artifact_hashes[target.name] = run_state.sha256(target)
    selected_provider = str(state.get("actual_provider") or provider or os.environ.get("MARKET_CONTENT_PROVIDER", "openai"))
    manifest = {
        "run_id": state["run_id"],
        "edition": edition,
        "market_session": content_data.get("market_session"),
        "prompt_version": content_data.get("prompt_version"),
        "llm_provider": selected_provider,
        "llm_model": {
            "ollama": os.environ.get("OLLAMA_MODEL", "qwen3.5:9b"),
            "gemini": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
            "openai": os.environ.get("OPENAI_MODEL", "gpt-5"),
        }.get(selected_provider),
        "fallback_used": bool(state.get("fallback_used", selected_provider in {"auto", "rule_template"})),
        "data_cutoff": content_data.get("data_cutoff"),
        "scheduled_local_time": content_data.get("scheduled_local_time"),
        "started_at": state["started_at"],
        "finished_at": run_state.now(),
        "content_hash": run_state.sha256(content) if content.exists() else None,
        "market_data_hash": run_state.sha256(paths["market_quotes"]) if paths["market_quotes"].exists() else None,
        "artifact_hashes": artifact_hashes,
        "source_status": _read_json(paths["sources"] / "source_status.json") if (paths["sources"] / "source_status.json").exists() else {"status": "unavailable"},
        "qa_status": "pass" if qa_ok else "fail",
        "mode": "image" if not state.get("text_only", True) else "text",
        "image_qa_status": (_read_json(paths["logs"] / "image_qa.json").get("status") if (paths["logs"] / "image_qa.json").exists() else None),
        "external_publish": "removed",
        "run_mode": state.get("run_mode", RunMode.DRY_RUN.value),
        "run_mode_resolved_from": state.get("run_mode_resolved_from", "default"),
        "external_delivery_enabled": read_delivery_controls(ROOT).get("external_delivery_enabled", False),
        "production_ready": False,
        "failed_step": state.get("failed_step"),
        "delivered": False,
        "auto_optimization_enabled": _auto_optimization_enabled(os.environ),
        "state_path": str(run_state.path(state["run_id"], Path(state["state_root"]))),
        "output_root": str(paths["content"].parent),
        "created_at": run_state.now(),
    }
    if manifest["run_mode"] == RunMode.SHADOW_CANARY.value:
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step("canary.readiness", "running", {"run_id": state["run_id"], "run_mode": manifest["run_mode"]})
        if _OBSERVABILITY is not None:
            _OBSERVABILITY.stage_started("canary.readiness", {"run_id": state["run_id"], "run_mode": manifest["run_mode"]})
        evidence = build_readiness_evidence(root=ROOT, state=state, paths=paths, run_mode=manifest["run_mode"])
        evidence_path = paths["logs"] / "canary_readiness_evidence.json"
        run_state.atomic_write_json(evidence_path, evidence)
        manifest["canary_readiness_evidence"] = evidence
        manifest["canary_technical_ready"] = bool(evidence.get("canary_technical_ready"))
        manifest["artifact_hashes"][evidence_path.name] = run_state.sha256(evidence_path)
        if _OBSERVABILITY is not None:
            _OBSERVABILITY.stage_finished("canary.readiness", "success", metadata={"pass": manifest["canary_technical_ready"], "run_id": state["run_id"]})
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step("canary.readiness", "success", {"run_id": state["run_id"], "pass": manifest["canary_technical_ready"]})
        record_canary_run(ROOT, evidence, manifest)
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step("canary.stability", "running", {"run_id": state["run_id"], "window_size": 10})
        if _OBSERVABILITY is not None:
            _OBSERVABILITY.stage_started("canary.stability", {"run_id": state["run_id"], "window_size": 10})
        stability = evaluate_canary_stability(ROOT)
        manifest["canary_stability"] = stability
        manifest["canary_stability_pass"] = bool(stability.get("canary_stability_pass"))
        manifest["blocking_reasons"] = list(dict.fromkeys(list(evidence.get("blocking_reasons", [])) + list(stability.get("blocking_reasons", []))))
        if _OBSERVABILITY is not None:
            _OBSERVABILITY.stage_finished("canary.stability", "success", metadata={"pass": manifest["canary_stability_pass"], "eligible_runs": stability.get("eligible_runs")})
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step("canary.stability", "success", {"run_id": state["run_id"], "pass": manifest["canary_stability_pass"], "window_size": stability.get("window_size"), "eligible_runs": stability.get("eligible_runs")})
        # Shadow runs execute the non-mutating delivery preflight so the
        # release boundary is observable.  A denial is the expected result:
        # shadow mode can never publish and the global kill switch remains
        # active.  This call never invokes a delivery adapter.
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step("delivery.preflight", "running", {"run_id": state["run_id"], "run_mode": manifest["run_mode"]})
        try:
            preflight = delivery_preflight(
                ROOT,
                run_id=state["run_id"],
                run_mode=manifest["run_mode"],
                manifest=manifest,
                approval=None,
                target=None,
            )
            manifest["delivery_preflight"] = preflight.as_dict()
            evidence["delivery_preflight"] = "pass" if preflight.allowed else "blocked"
            evidence["delivery_preflight_decision"] = preflight.as_dict()
            evidence["blocking_reasons"] = list(dict.fromkeys(list(evidence.get("blocking_reasons", [])) + list(preflight.blockers)))
            run_state.atomic_write_json(evidence_path, evidence)
            manifest["artifact_hashes"][evidence_path.name] = run_state.sha256(evidence_path)
            if _TRACE_SESSION is not None:
                _TRACE_SESSION.step(
                    "delivery.preflight",
                    "success" if preflight.allowed else "blocked",
                    {
                        "run_id": state["run_id"],
                        "run_mode": manifest["run_mode"],
                        "pass": preflight.allowed,
                        "reason_code": preflight.reason_code,
                        "kill_switch_active": preflight.kill_switch_active,
                        "external_delivery_enabled": preflight.external_delivery_enabled,
                    },
                )
        except Exception as exc:
            # A preflight implementation error must fail closed and be
            # visible in local trace/audit output without stopping archiving.
            manifest["delivery_preflight"] = {
                "allowed": False,
                "run_id": state["run_id"],
                "run_mode": manifest["run_mode"],
                "delivery_state": "PREFLIGHT_DENIED",
                "reason_code": "DELIVERY_PREFLIGHT_ERROR",
                "blockers": ["DELIVERY_PREFLIGHT_ERROR"],
                "kill_switch_active": True,
                "external_delivery_enabled": False,
                "human_approval_valid": False,
                "artifact_integrity_valid": False,
                "target": None,
                "approval_id": None,
            }
            evidence["delivery_preflight"] = "blocked"
            evidence.setdefault("blocking_reasons", []).append("DELIVERY_PREFLIGHT_ERROR")
            run_state.atomic_write_json(evidence_path, evidence)
            manifest["artifact_hashes"][evidence_path.name] = run_state.sha256(evidence_path)
            if _TRACE_SESSION is not None:
                _TRACE_SESSION.step("delivery.preflight", "failed", {"run_id": state["run_id"], "error_type": type(exc).__name__})
        try:
            from runtime_index import StateIndex

            index = StateIndex(ROOT / "runtime" / "state_index.sqlite3")
            index.audit(state["run_id"], "CANARY_EVIDENCE_GENERATED", {"run_id": state["run_id"], "run_mode": manifest["run_mode"], "canary_technical_ready": manifest["canary_technical_ready"]})
            index.audit(state["run_id"], "CANARY_STABILITY_EVALUATED", {"run_id": state["run_id"], "window_size": stability.get("window_size"), "eligible_runs": stability.get("eligible_runs"), "pass": manifest["canary_stability_pass"]})
            index.audit(state["run_id"], "DELIVERY_PREFLIGHT_DENIED", manifest["delivery_preflight"])
            if manifest["delivery_preflight"].get("kill_switch_active"):
                index.audit(state["run_id"], "DELIVERY_KILL_SWITCH_ACTIVE", {"run_id": state["run_id"], "reason_code": "KILL_SWITCH_ACTIVE", "run_mode": manifest["run_mode"]})
        except Exception:
            pass
    else:
        manifest["canary_technical_ready"] = False
        manifest["canary_stability_pass"] = False
        manifest["blocking_reasons"] = ["RUN_MODE_NOT_SHADOW_CANARY"]
    target = paths["logs"] / "run_manifest.json"
    run_state.atomic_write_json(target, manifest)
    return target


def _sync_execution_plan(state: dict, paths: dict[str, Path | bool]) -> None:
    """Project actual executor state back into the planner artifact."""
    plan_path = paths.get("plan")
    if not isinstance(plan_path, Path) or not plan_path.exists():
        return
    try:
        plan = _read_json(plan_path)
    except (OSError, ValueError):
        return
    for item in plan.get("steps", []):
        executor_step = item.get("executor_step") or item.get("step")
        executor_state = (state.get("steps") or {}).get(executor_step) or {}
        item["status"] = executor_state.get("status", item.get("status", "pending"))
        if executor_state.get("error"):
            item["error"] = executor_state["error"]
    plan["status"] = "failed" if state.get("failed_step") else "completed"
    plan["updated_at"] = run_state.now()
    run_state.atomic_write_json(plan_path, plan)


def _write_failure_reports(state: dict, paths: dict[str, Path], qa_ok: bool, edition: str, provider: str) -> None:
    _write_manifest(state, paths, qa_ok, edition, provider)


def _write_delivery_report(
    state: dict,
    paths: dict[str, Path | bool],
    manifest_path: Path,
    qa_ok: bool,
    error: dict | None = None,
) -> Path:
    """Persist and print the final notification without changing pipeline state."""
    content_path = paths["content"] / "market_content.json"
    content = _read_json(content_path) if content_path.exists() else {}
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    status = {
        "run_id": state["run_id"],
        "qa_status": "pass" if qa_ok else "fail",
        "delivered": False,
        "image_generation_enabled": manifest.get("image_generation_enabled", manifest.get("mode") == "image"),
        "external_publish_enabled": manifest.get("external_publish_enabled", manifest.get("external_publish") not in {None, "removed", "disabled", "off"}),
        "output_root": manifest.get("output_root") or state.get("output_root"),
        "content_path": str(content_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "log_path": str((paths["logs"] / "market_content_errors.log").resolve()),
    }
    if error:
        status.update({
            "failed_step": error.get("step") or state.get("failed_step"),
            "error_reason": error.get("message") or error.get("raw_message") or error.get("error_code"),
            "missing_fields": error.get("missing_fields") or error.get("conflicts") or [],
        })
    delivery_root = paths["content"].parent / "delivery"
    delivery_root.mkdir(parents=True, exist_ok=True)
    markdown_report = render_delivery_report(content, manifest, status, rich_text=False)
    html_report = render_delivery_report_html(content, manifest, status)
    markdown_target = delivery_root / "delivery_report_latest.md"
    html_target = delivery_root / "delivery_report_latest.html"
    markdown_target.write_text(markdown_report, encoding="utf-8")
    html_target.write_text(html_report, encoding="utf-8")
    manifest["delivery_report_path"] = str(html_target.resolve())
    manifest["delivery_report_html_path"] = str(html_target.resolve())
    manifest["delivery_report_markdown_path"] = str(markdown_target.resolve())
    manifest["delivery_report_html_sha256"] = run_state.sha256(html_target)
    manifest["delivery_report_markdown_sha256"] = run_state.sha256(markdown_target)
    run_state.atomic_write_json(manifest_path, manifest)
    archive_state = state.get("steps", {}).get("archive")
    if isinstance(archive_state, dict) and archive_state.get("status") == "success" and state.get("state_root"):
        archive_state["artifacts"] = [run_state.artifact_record(manifest_path, state["run_id"])]
        run_state.save(state, Path(state["state_root"]))
    print(f"可视化报告：{html_target.resolve()}")
    print(f"纯文本降级：{markdown_target.resolve()}")
    return html_target


def _legacy_execute(args: argparse.Namespace) -> int:
    global _TRACE_SESSION, _RUN_LOCK_CONTEXT, _HEALTH_CONTEXT, _OBSERVABILITY, _OBSERVABILITY_STATE
    run_id = args.resume or args.run_id or _run_id()
    canary_mode = bool(getattr(args, "canary_run", False))
    shadow_mode = bool(args.shadow_run or canary_mode)
    state_root = (ROOT / "state" / "canary") if canary_mode else ((ROOT / "runtime" / "shadow" / run_id) if shadow_mode else (ROOT / "runtime"))
    if not args.resume and not args.edition:
        raise RuntimeError("--edition is required for a new run")
    if not args.resume and args.enforce_schedule and not is_schedule_slot(args.edition):
        raise RuntimeError(f"outside_schedule_window:{args.edition}")
    if args.resume:
        state_candidates = [ROOT / "state" / "canary", ROOT / "runtime" / "shadow" / run_id, state_root, ROOT / "runtime"]
        state = None
        for candidate in state_candidates:
            try:
                state = run_state.load(run_id, candidate)
                state_root = candidate
                break
            except FileNotFoundError:
                continue
        if state is None:
            raise FileNotFoundError(f"state_not_found:{run_id}")
        canary_mode = canary_mode or state_root == ROOT / "state" / "canary" or "outputs/canary" in state.get("output_root", "")
        shadow_mode = shadow_mode or state_root == ROOT / "runtime" / "shadow" / run_id or "shadow" in Path(state.get("output_root", "")).parts or "canary" in Path(state.get("output_root", "")).parts
        if args.edition and state.get("edition") and args.edition != state["edition"]:
            raise RuntimeError(f"resume_edition_mismatch:{args.edition}:{state['edition']}")
        if not args.edition:
            args.edition = state.get("edition")
        if not args.edition:
            raise RuntimeError("edition_missing_in_state")
        output_root = Path(state["output_root"])
        start_step = executor_step_for(args.from_step) if args.from_step else run_state.first_resume_step(state)
        if args.from_step:
            run_state.reset_from(state, start_step, state_root)
        elif start_step is not None:
            # A failed/running step or invalid artifact invalidates every
            # dependent artifact. Keep upstream successes, but never resume
            # into a stale downstream success chain.
            run_state.reset_from(state, start_step, state_root)
        elif start_step is None:
            existing_manifest = Path(state["output_root"]) / "logs" / "run_manifest.json"
            if existing_manifest.exists():
                print(existing_manifest)
            return 0
    else:
        if not args.edition:
            raise RuntimeError("--edition is required for a new run")
        output_root = (ROOT / "outputs" / "canary" / run_id) if canary_mode else ((ROOT / "outputs" / "shadow" / run_id) if shadow_mode else (ROOT / "outputs" / "runs" / run_id))
        if output_root.exists() and any(output_root.iterdir()):
            raise RuntimeError(f"run_id_output_exists:{run_id}")
        state = run_state.create(run_id, args.edition, state_root, output_root)
        state["state_root"] = str(state_root)
        run_state.save(state, state_root)
        start_step = "health_check"

    if _RUN_LOCK_CONTEXT is not None:
        raise RuntimeError(f"run_lock_already_held:{run_id}")
    _RUN_LOCK_CONTEXT = run_state.run_lock(run_id, state_root)
    _RUN_LOCK_CONTEXT.__enter__()

    text_only = not args.enable_images
    state["text_only"] = text_only
    state["output_mode"] = "text" if text_only else "image"

    started_at = datetime.fromisoformat(state["started_at"]) if args.resume and state.get("started_at") else None
    context = resolve_edition_context(args.edition, started_at=started_at)
    if args.enforce_schedule and not is_schedule_slot(args.edition):
        raise RuntimeError(f"outside_schedule_window:{args.edition}:{context.scheduled_local_time}")
    paths = _paths(output_root, run_id, shadow=shadow_mode, legacy_shadow=False, canary=canary_mode)
    health_root = ROOT / "reports" if not shadow_mode else output_root / "reports"
    _HEALTH_CONTEXT = {
        "report_root": health_root,
        "logs_root": paths["logs"],
        "task_log": paths["logs"] / "task_runs.jsonl",
    }
    runtime_policy = load_runtime_policy()
    _OBSERVABILITY_STATE = state
    _OBSERVABILITY = RunObserver(
        RunContext(
            run_id=state["run_id"],
            task_type="market_content",
            target_date=context.scheduled_cutoff.date().isoformat(),
            session=context.market_session,
            scheduled_at=context.scheduled_cutoff.isoformat(),
            started_at=state["started_at"],
            prompt_version=context.prompt_version,
            renderer_version="svg_renderer_v1" if not text_only else "disabled",
            delivery_enabled=False,
            checkpoint_resumed=bool(args.resume),
            metadata={"edition": args.edition, "timezone": "Asia/Tokyo"},
        ),
        output_root,
        paths["logs"],
        thresholds=runtime_policy.get("monitoring") if isinstance(runtime_policy.get("monitoring"), dict) else None,
    )
    _OBSERVABILITY.stage_started("input_selection", {"edition": args.edition})
    _OBSERVABILITY.stage_finished(
        "input_selection",
        "success",
        metadata={"edition": args.edition, "scheduled_local_time": context.scheduled_local_time, "target_date": context.scheduled_cutoff.date().isoformat()},
    )
    paths["plan"] = state_root / "plans" / f"{run_id}.json"
    paths["decisions"] = state_root / "decisions" / f"{run_id}.json"
    paths["decision_log"] = (paths["logs"] / "market_content_decisions.log") if shadow_mode else ROOT / "logs" / "market_content_decisions.log"
    for key in ("content", "github", "sources", "images", "logs", "review", "shadow_root", "plan", "decisions", "decision_log"):
        target = paths[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        if key not in {"plan", "decisions", "decision_log"}:
            target.mkdir(parents=True, exist_ok=True)
    _TRACE_SESSION = TraceSession(
        state["run_id"],
        {
            "run_id": state["run_id"],
            "edition": args.edition,
            "started_at": state["started_at"],
            "dry_run": True,
            "timezone": "Asia/Tokyo",
            "market_session": context.market_session,
        },
        paths["logs"] / "trace.jsonl",
    )
    _TRACE_SESSION.start()
    transition_log = paths["logs"] / "steps.jsonl"
    health_path = _health_report(paths)
    health_report = _read_json(health_path)
    prior_plan = None
    if Path(paths["plan"]).exists():
        try:
            prior_plan = _read_json(Path(paths["plan"]))
        except (OSError, ValueError):
            prior_plan = None
    router = ToolRouter(health_report)
    planner = ExecutionPlanner(router)
    try:
        plan = planner.build(
            run_id=state["run_id"],
            edition=args.edition,
            state=state,
            preferred_provider=args.provider,
            prior_plan=prior_plan,
            text_only=text_only,
        )
    except RouterBlocked as exc:
        plan = {
            "run_id": state["run_id"],
            "edition": args.edition,
            "goal": "generate_market_content_pack",
            "status": "blocked",
            "blocking_task": exc.task,
            "rejected_tools": exc.rejected_tools,
            "created_at": run_state.now(),
            "planner_version": "controlled-planner-v1",
        }
        ExecutionPlanner.write(Path(paths["plan"]), plan)
        run_state.atomic_write_json(Path(paths["decisions"]), {"run_id": state["run_id"], "decisions": router.decisions, "created_at": run_state.now()})
        raise RuntimeError(f"execution_plan_blocked:{exc.task}") from exc
    ExecutionPlanner.write(Path(paths["plan"]), plan)
    try:
        agent_policy = load_agent_policy(ROOT / "config" / "agent_policy.json")
    except RuntimeError as exc:
        raise RuntimeError(f"agent_loop_policy_blocked:{exc}") from exc
    # The agent controller inherits the stricter runtime planner limits.  A
    # policy file can reduce capability, but cannot expand the existing
    # Function Calling or runtime budgets.
    constraints = plan.get("constraints") or {}
    agent_policy.max_tool_calls = min(agent_policy.max_tool_calls, int(constraints.get("max_tool_calls", agent_policy.max_tool_calls)))
    agent_policy.max_runtime_seconds = min(agent_policy.max_runtime_seconds, int(constraints.get("max_runtime_seconds", agent_policy.max_runtime_seconds)))
    agent_audit_path = paths["logs"] / "agent_loop.jsonl"
    agent_loop = ControlledAgentLoop(agent_policy, audit_path=agent_audit_path, run_id=state["run_id"])
    state["execution_plan"] = plan
    state["agent_loop"] = {
        "mode": agent_policy.mode,
        "enabled": agent_policy.enabled,
        "mandatory_gates": sorted(agent_loop.mandatory_steps),
        "blocked_tools": agent_policy.blocked_tools,
        "audit_path": str(agent_audit_path),
    }
    run_state.save(state, state_root)
    run_state.atomic_write_json(Path(paths["decisions"]), {
        "run_id": state["run_id"],
        "plan_path": str(paths["plan"]),
        "decisions": router.decisions,
        "created_at": run_state.now(),
    })
    with Path(paths["decision_log"]).open("a", encoding="utf-8") as handle:
        for decision in router.decisions:
            handle.write(json.dumps({"run_id": state["run_id"], **decision}, ensure_ascii=False) + "\n")
    run_state.mark_logical(state, "inspect_environment", "success", state_root, artifacts=[health_path])
    run_state.mark_logical(state, "build_execution_plan", "success", state_root, artifacts=[Path(paths["plan"])])
    run_state.mark_logical(state, "select_tools", "success", state_root, artifacts=[Path(paths["decisions"])])
    run_state.mark_logical(state, "execute_plan", "running", state_root)
    selected_provider = str(plan.get("selected_provider") or args.provider)
    args.provider = selected_provider
    state["selected_provider"] = selected_provider
    state["actual_provider"] = selected_provider
    state["fallback_used"] = False
    if _OBSERVABILITY is not None:
        model_names = {
            "ollama": os.environ.get("OLLAMA_MODEL", "qwen3.5:9b"),
            "gemini": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
            "openai": os.environ.get("OPENAI_MODEL", "gpt-5"),
            "rule_template": "rule_template",
        }
        _OBSERVABILITY.context.model_name = model_names.get(selected_provider, selected_provider)
    env = _base_env(paths, args.edition, context.prompt_text, context.prompt_version, selected_provider)
    env["MARKET_RUN_ID"] = state["run_id"]
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["MARKET_TRACE_PATH"] = str(paths["logs"] / "trace.jsonl")
    state["auto_optimization_enabled"] = _auto_optimization_enabled(env)
    run_state.save(state, state_root)
    if canary_mode:
        env["SELF_HEALING_CANARY_MODE"] = "true"
        env["DRY_RUN"] = "true"
    # Keep generator and reviewer independent whenever a second model is
    # healthy; otherwise use the deterministic reviewer, which fail-closes.
    reviewer_health = (health_report.get("services", {}).get("gemini", {}) or {}).get("status")
    env["REVIEWER_PROVIDER"] = "gemini" if selected_provider == "ollama" and reviewer_health == "healthy" else "deterministic"

    start_index = run_state.STEPS.index(start_step) if start_step in run_state.STEPS else 0
    commands = {
        "generate_content": [sys.executable, "market_content_openai.py", "--edition", args.edition, "--provider", selected_provider, "--market-context-file", str(paths["sources"] / "normalized_materials.json"), "--market-data-file", str(paths["market_quotes"])],
        "collect_github": [sys.executable, "github_ai_projects.py"],
        "collect_sources": [sys.executable, "source_router.py"],
        "collect_market_quotes": [sys.executable, "market_quotes.py", "--edition", args.edition, "--output", str(paths["market_quotes"])],
    }
    if args.raw_response_file:
        commands["generate_content"].extend(["--raw-response-file", str(Path(args.raw_response_file).resolve())])

    function_context = BusinessContext(
        run_id=state["run_id"],
        edition=args.edition,
        paths=paths,
        environment=env,
        provider=selected_provider,
    )
    business_bindings = build_business_bindings(function_context)

    def recovery_retry_collector(step: str) -> dict:
        if step not in {"collect_sources", "collect_news"}:
            return {"status": "failed", "error_type": "configuration_error", "blocking_reason": f"collector_not_registered:{step}"}
        result = business_bindings["collect_news"](CollectNewsArgs(run_id=state["run_id"], edition=args.edition, sources=[str(plan.get("news_discovery") or "rss")]))
        return result

    def recovery_market_quotes(symbols: list[str]) -> dict:
        result = business_bindings["collect_market_data"](CollectMarketDataArgs(run_id=state["run_id"], edition=args.edition, symbols=symbols, as_of=context.scheduled_cutoff))
        return result

    def recovery_validate_market(data: dict) -> dict:
        try:
            return business_bindings["validate_market_data"](ValidateMarketDataArgs(run_id=state["run_id"], edition=args.edition, market_data_path=str(function_context.market_data_path)))
        except Exception as exc:
            return {"status": "fail", "message": str(exc)}

    recovery_controller = RepairController(
        state["run_id"],
        Path(paths["logs"]) / "recovery",
        adapters=RepairAdapters(
            retry_collector=recovery_retry_collector,
            collect_market_quotes=recovery_market_quotes,
            validate_market_data=recovery_validate_market,
            resume_market_pipeline=lambda steps, data: {"status": "success", "resumed_steps": steps},
            request_gemini=lambda attempt: __import__("model_providers").call_gemini("Return the existing market content JSON only.", timeout=60),
        ),
    )
    gap_analyzer = GapAnalyzer()
    repair_planner = RepairPlanner((Path(paths["shadow_root"]) / "repairs") if shadow_mode else (ROOT / "runtime" / "repairs"))

    def recover(call: FunctionCall, error) -> dict:
        nonlocal plan, selected_provider
        if not _auto_optimization_enabled(env):
            return {
                "status": "repair_failed",
                "repair_action_succeeded": False,
                "original_failure_resolved": False,
                "resume_succeeded": False,
                "validation_passed": False,
                "blocking_reason": "auto_optimization_disabled",
            }
        error_payload = error.model_dump(mode="json") if hasattr(error, "model_dump") else {"message": str(error)}
        error_classification = classify_error(
            error_payload.get("error_code") or error_payload.get("error_type") or "unknown_error",
            {**error_payload, "step": call.step},
        )
        repair_selection = select_repair_plan(
            error_classification,
            {**error_payload, "step": call.step, "provider": selected_provider},
            execution_mode="automatic",
        )
        previous_provider = selected_provider
        state["retry_count"] = int(state.get("retry_count", 0)) + 1
        run_state.save(state, state_root)
        decision_start = len(router.decisions)
        logical_tool_map = {
            "generate_content": selected_provider,
            "collect_news": plan.get("news_discovery"),
            "extract_web_content": plan.get("web_extraction"),
            "collect_market_data": plan.get("market_primary"),
            "validate_market_data": plan.get("market_primary"),
        }
        failed_tool = str(logical_tool_map.get(call.step) or "")
        router.mark_runtime_failure(failed_tool, error_payload.get("error_code") or error_payload.get("error_type") or "runtime_failure")
        try:
            plan = planner.build(
                run_id=state["run_id"],
                edition=args.edition,
                state=state,
                preferred_provider="auto",
                prior_plan=plan,
                text_only=text_only,
            )
            ExecutionPlanner.write(Path(paths["plan"]), plan)
            run_state.atomic_write_json(Path(paths["decisions"]), {
                "run_id": state["run_id"],
                "plan_path": str(paths["plan"]),
                "decisions": router.decisions,
                "created_at": run_state.now(),
            })
            for decision in router.decisions[decision_start:]:
                with Path(paths["decision_log"]).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"run_id": state["run_id"], **decision}, ensure_ascii=False) + "\n")
            selected_provider = str(plan.get("selected_provider") or selected_provider)
            args.provider = selected_provider
            env["MARKET_CONTENT_PROVIDER"] = selected_provider
        except (RouterBlocked, OSError, ValueError) as exc:
            return {
                "status": "repair_failed",
                "repair_action_succeeded": False,
                "original_failure_resolved": False,
                "resume_succeeded": False,
                "validation_passed": False,
                "blocking_reason": f"replan_failed:{type(exc).__name__}",
            }
        gap = gap_analyzer.analyze(
            validation_errors=[{**error_payload, "step": call.step}],
            current_state=state,
            artifact_manifest={"affected_artifacts": [str(item) for item in _step_artifacts(paths, executor_step_for(call.step))]},
            tool_decision_history=router.decisions,
            run_id=state["run_id"],
        )
        try:
            plan_artifact = repair_planner.build(
                run_id=state["run_id"],
                trigger_error=error_payload.get("error_code") or error_payload.get("message", "function_failure"),
                gap=gap,
                current_state=state,
                selected_tools=[item.get("selected_tool") for item in plan.get("steps", []) if item.get("selected_tool")],
            )
        except (OSError, ValueError) as exc:
            return {
                "status": "repair_failed",
                "reason": f"repair_plan_failed:{type(exc).__name__}",
                "gap": gap,
                "error_classification": error_classification,
                "repair_selection": repair_selection,
            }
        result = recovery_controller.repair(
            f"function_{call.call_id}",
            call.step,
            error_payload.get("message", "function failure"),
            {
                "current_state": state,
                "selected_tools": [item.get("selected_tool") for item in plan.get("steps", []) if item.get("selected_tool")],
                "tool_decision_history": router.decisions,
                "missing_fields": gap.get("missing_fields", []),
                "missing_symbols": gap.get("missing_fields", []),
                "error_classification": error_classification,
                "repair_selection": repair_selection,
                "provider": selected_provider,
                "previous_provider": previous_provider,
                "retry_arguments": {
                    **call.arguments,
                    "provider": selected_provider,
                } if call.step == "generate_content" else dict(call.arguments),
            },
        )
        if isinstance(result, dict):
            result["repair_plan"] = plan_artifact
            result["gap_analysis"] = gap
            result["error_classification"] = error_classification
            result["repair_selection"] = repair_selection
        return result

    # Resume must not reuse a prior call_id.  Function results and audit
    # records are append-only within a run, so continue from the largest
    # persisted suffix instead of restarting at call_001.
    function_call_counter = 0
    existing_calls = paths["logs"] / "function_calls.jsonl"
    if existing_calls.exists():
        try:
            for line in existing_calls.read_text(encoding="utf-8").splitlines():
                try:
                    call_id = str(json.loads(line).get("call_id") or "")
                    suffix = int(call_id.rsplit("_", 1)[-1])
                    function_call_counter = max(function_call_counter, suffix)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        except OSError:
            pass

    def function_event(call: FunctionCall, status: str, result) -> None:
        if _TRACE_SESSION is not None:
            result_data = result.data if result and isinstance(result.data, dict) else {}
            _TRACE_SESSION.step(
                call.tool_name,
                status,
                {
                    "call_id": call.call_id,
                    "input_count": len(call.arguments),
                    "output_count": len(result_data),
                    "duration_ms": result.duration_ms if result else 0,
                    "error_type": result.error.error_type if result and result.error else "",
                    "error_message": result.error.message[:300] if result and result.error else "",
                    "fallback_used": bool(result_data.get("fallback_used") or result_data.get("recovery")) if result_data else False,
                },
            )
        with (paths["logs"] / "function_calls.jsonl").open("a", encoding="utf-8") as handle:
            event = {
                "run_id": state["run_id"],
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "step": call.step,
                "status": status,
                "error": redact(result.error.model_dump(mode="json")) if result and result.error else None,
                "timestamp": run_state.now(),
            }
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        if _OBSERVABILITY is not None:
            result_data = result.data if result and isinstance(result.data, dict) else {}
            _OBSERVABILITY.event(
                stage=LOGICAL_STEP_TO_STAGE.get(call.step, STEP_TO_STAGE.get(call.step, call.step)),
                event="function_call",
                status=status,
                metadata={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "attempt": 1,
                    "duration_ms": result.duration_ms if result else None,
                    "fallback_used": bool(result_data.get("fallback_used") or result_data.get("recovery")) if result_data else False,
                },
                error=redact(result.error.model_dump(mode="json")) if result and result.error else None,
            )
        try:
            from runtime_index import index_for_state_root

            index_for_state_root(state_root).audit(state["run_id"], "function_call", event)
        except Exception:
            pass

    executor = FunctionExecutor(
        registry=build_registry(business_bindings),
        max_calls=int(plan["constraints"]["max_tool_calls"]),
        max_calls_per_step=int(_function_calling_policy().get("max_calls_per_step", 5)),
        recovery_handler=recover,
        state_hook=function_event,
        blocked_tools=set(_function_calling_policy().get("blocked_tools") or []),
        allowed_steps=set(_function_calling_policy().get("allowed_steps") or []),
    )

    def make_call(tool_name: str, arguments: dict[str, object]) -> FunctionCall:
        nonlocal function_call_counter
        function_call_counter += 1
        return FunctionCall(
            call_id=f"{state['run_id']}_call_{function_call_counter:03d}",
            tool_name=tool_name,
            step=tool_name,
            arguments=arguments,
            requested_by="planner",
        )

    def mark_logical(step_name: str, status: str, error: dict | None = None) -> None:
        run_state.mark_logical(state, step_name, status, state_root, error=error)

    def mark_plan_failed(error: dict | None = None) -> None:
        mark_logical("execute_plan", "failed", error=error)

    def execute_tracked(call: FunctionCall):
        agent_loop.register_tool_calls(1)
        if call.tool_name in run_state.LOGICAL_STEP_TO_EXECUTOR:
            mark_logical(call.tool_name, "running")
        logical_stage = LOGICAL_STEP_TO_STAGE.get(call.tool_name)
        executor_step = run_state.LOGICAL_STEP_TO_EXECUTOR.get(call.tool_name, call.tool_name)
        outer_stage = STEP_TO_STAGE.get(executor_step, executor_step)
        monitor_logical_stage = _OBSERVABILITY is not None and logical_stage and logical_stage != outer_stage
        if monitor_logical_stage:
            _OBSERVABILITY.stage_started(logical_stage, {"logical_step": call.tool_name, "tool_name": call.tool_name})
        try:
            result = executor.execute(call)
        except Exception as exc:
            if monitor_logical_stage:
                _OBSERVABILITY.stage_finished(logical_stage, "failed", {"error_type": type(exc).__name__, "message": str(exc), "traceback": __import__("traceback").format_exc()})
            raise
        if call.tool_name == "generate_content" and result.status is FunctionStatus.success:
            result_data = result.data if isinstance(result.data, dict) else {}
            state["actual_provider"] = result_data.get("provider_used") or selected_provider
            state["fallback_used"] = bool(result_data.get("fallback_used", False))
            args.provider = state["actual_provider"]
            run_state.save(state, state_root)
        if monitor_logical_stage:
            _OBSERVABILITY.stage_finished(
                logical_stage,
                "success" if result.status is FunctionStatus.success else "failed",
                redact(result.error.model_dump(mode="json")) if result.error else None,
                {"logical_step": call.tool_name, "tool_name": call.tool_name},
            )
        if call.tool_name in run_state.LOGICAL_STEP_TO_EXECUTOR:
            if result.status is FunctionStatus.success:
                mark_logical(call.tool_name, "success")
            else:
                mark_logical(
                    call.tool_name,
                    "failed",
                    error=redact(result.error.model_dump(mode="json")) if result.error else {"message": "function call failed"},
                )
        return result

    def execute_function_chain(step: str):
        source_urls: list[str] = []
        if function_context.source_path.exists():
            try:
                source_urls = [str(item.get("source_url")) for item in _read_json(function_context.source_path) if isinstance(item, dict) and item.get("source_url")]
            except (OSError, ValueError):
                source_urls = []
        symbols = [*CORE_SYMBOLS, "NVDA", "MSFT", "AAPL"]
        if step == "collect_sources":
            selected_routes = [
                str(plan.get("news_discovery") or "rss"),
                *[str(item) for item in plan.get("news_fallback_chain", []) if item],
                str(plan.get("web_extraction") or "jina"),
                *[str(item) for item in plan.get("web_fallback_chain", []) if item],
            ]
            selected_routes = list(dict.fromkeys(selected_routes))
            first = execute_tracked(make_call("collect_news", {"run_id": state["run_id"], "edition": args.edition, "sources": selected_routes}))
            if first.status is not FunctionStatus.success:
                return first
            try:
                source_urls = [str(item.get("source_url")) for item in _read_json(function_context.source_path) if isinstance(item, dict) and item.get("source_url")]
            except (OSError, ValueError):
                source_urls = []
            if not source_urls:
                return first
            return execute_tracked(make_call("extract_web_content", {"run_id": state["run_id"], "edition": args.edition, "urls": source_urls}))
        elif step == "collect_market_quotes":
            calls = [
                make_call("collect_market_data", {"run_id": state["run_id"], "edition": args.edition, "symbols": symbols, "as_of": context.scheduled_cutoff.isoformat()}),
                make_call("validate_market_data", {"run_id": state["run_id"], "edition": args.edition, "market_data_path": str(function_context.market_data_path)}),
            ]
        elif step == "generate_content":
            calls = [
                make_call("generate_content", {"run_id": state["run_id"], "edition": args.edition, "input_path": str(function_context.source_path), "provider": selected_provider, "raw_response_path": str(Path(args.raw_response_file).resolve()) if args.raw_response_file else None}),
                make_call("validate_content_consistency", {"run_id": state["run_id"], "edition": args.edition, "content_path": str(function_context.content_path), "source_path": str(function_context.source_path)}),
            ]
        elif step == "final_validation":
            validation_paths = [str(function_context.paths["logs"]) + "/qa_report.json"]
            calls = [make_call("final_quality_gate", {"run_id": state["run_id"], "edition": args.edition, "validation_paths": validation_paths})]
        else:
            return None
        last = None
        for call in calls:
            last = execute_tracked(call)
            if last.status is not FunctionStatus.success:
                return last
        return last

    qa_report_existing = paths["logs"] / "qa_report.json"
    qa_ok = False
    if qa_report_existing.exists():
        try:
            qa_ok = _read_json(qa_report_existing).get("status") == "pass"
        except (OSError, ValueError):
            qa_ok = False
    last_agent_step: str | None = None
    last_agent_status: str | None = None
    last_agent_error: dict | None = None
    candidate_steps = run_state.STEPS[start_index:]
    while True:
        decision = agent_loop.select_next_step(
            state,
            candidate_steps,
            last_step=last_agent_step,
            last_status=last_agent_status,
            last_error=last_agent_error,
        )
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.step(
                "agent_loop",
                "success" if decision.action.value != "stop" or decision.reason == "all_steps_complete" else "failed",
                {
                    "iteration": decision.iteration,
                    "action": decision.action.value,
                    "selected_step": decision.selected_step or "",
                    "selected_tool": decision.selected_tool or "",
                    "reason": decision.reason,
                    "mandatory": decision.mandatory,
                    "blocked": decision.blocked,
                },
            )
        if decision.action.value == "stop":
            if decision.reason == "all_steps_complete":
                break
            error = {
                "error_type": "agent_loop_blocked",
                "error_code": decision.reason,
                "step": decision.selected_step or state.get("current_step"),
                "message": decision.reason,
                "retryable": False,
            }
            mark_plan_failed(error)
            state["delivered"] = False
            run_state.save(state, state_root)
            _write_failure_reports(state, paths, False, args.edition, args.provider)
            _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
            return 1
        step = decision.selected_step
        if not step:
            error = {"error_type": "agent_loop_blocked", "error_code": "missing_selected_step", "message": "agent loop returned no step", "retryable": False}
            mark_plan_failed(error)
            state["delivered"] = False
            run_state.save(state, state_root)
            _write_failure_reports(state, paths, False, args.edition, args.provider)
            _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
            return 1
        agent_action = AgentAction(
            action_id=f"{state['run_id']}_agent_{decision.iteration:03d}",
            tool_name=step,
            arguments={},
            reason=decision.reason,
            expected_result=f"{step} completed",
            priority=5 if decision.mandatory else 100,
        )
        state.setdefault("agent_actions", []).append(agent_action.model_dump(mode="json"))
        run_state.save(state, state_root)
        try:
            from runtime_index import index_for_state_root

            index_for_state_root(state_root).audit(state["run_id"], "agent_action", {
                "step": decision.iteration,
                "action": agent_action.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
            })
        except Exception:
            pass
        last_agent_step = step
        last_agent_status = None
        last_agent_error = None
        if step == "health_check":
            _transition(state, step, "running", state_root, transition_log)
            # The preflight report is already the same read-only health check
            # used by the planner. Reuse it instead of probing every service
            # a second time in the same run.
            health_path = health_path if health_path.exists() else _health_report(paths)
            _transition(state, step, "success", state_root, transition_log, artifacts=[health_path])
            last_agent_status = "success"
            continue
        if state["steps"].get(step, {}).get("status") in {"success", "skipped"} and not args.from_step:
            continue
        if step in {"collect_sources", "collect_market_quotes", "generate_content", "final_validation"}:
            _transition(state, step, "running", state_root, transition_log)
            result = execute_function_chain(step)
            if step == "generate_content" and result is not None and result.status is FunctionStatus.success:
                result_data = result.data if isinstance(result.data, dict) else {}
                state["actual_provider"] = result_data.get("provider_used") or selected_provider
                state["fallback_used"] = bool(result_data.get("fallback_used", False))
                args.provider = state["actual_provider"]
                _write_text_qa_report(paths)
                qa_ok = True
            artifacts = _step_artifacts(paths, step)
            image_error = None
            if result is not None and result.status is FunctionStatus.success and step == "final_validation" and not text_only:
                from image_renderer import render_image_pack, validate_image_pack

                image_path = paths["images"] / "market_content.svg"
                image_qa_path = paths["logs"] / "image_qa.json"
                try:
                    if _OBSERVABILITY is not None:
                        _OBSERVABILITY.stage_started("image_rendering", {"renderer_version": "svg_renderer_v1"})
                    render_image_pack(function_context.content_path, paths["images"], state["run_id"])
                    if _OBSERVABILITY is not None:
                        _OBSERVABILITY.stage_finished("image_rendering", "success", metadata={"renderer_version": "svg_renderer_v1"})
                    if _OBSERVABILITY is not None:
                        _OBSERVABILITY.stage_started("image_qa", {"renderer_version": "svg_renderer_v1"})
                    image_qa = validate_image_pack(image_path, function_context.content_path, state["run_id"])
                    run_state.atomic_write_json(image_qa_path, image_qa)
                    if image_qa.get("status") != "pass":
                        if _OBSERVABILITY is not None:
                            _OBSERVABILITY.stage_finished("image_qa", "failed", {"error_code": "image_qa_failed", "message": "image QA gate failed"})
                        image_error = {"error_type": "quality_error", "error_code": "image_qa_failed", "step": step, "message": "image QA gate failed", "retryable": False}
                    elif _OBSERVABILITY is not None:
                        _OBSERVABILITY.stage_finished("image_qa", "success", metadata={"qa_status": "pass"})
                except (OSError, ValueError, TypeError) as exc:
                    if _OBSERVABILITY is not None:
                        if "image_rendering" in _OBSERVABILITY._started:
                            _OBSERVABILITY.stage_finished("image_rendering", "failed", {"error_type": type(exc).__name__, "message": str(exc), "traceback": __import__("traceback").format_exc()})
                        elif "image_qa" in _OBSERVABILITY._started:
                            _OBSERVABILITY.stage_finished("image_qa", "failed", {"error_code": "IMAGE_QA_FAILED", "error_type": type(exc).__name__, "message": str(exc), "traceback": __import__("traceback").format_exc()})
                    image_error = {"error_type": "rendering_error", "error_code": "renderer_not_registered", "step": step, "message": str(exc), "retryable": False}
            if result is None or result.status is not FunctionStatus.success or image_error:
                error = image_error or (result.error.model_dump(mode="json") if result and result.error else {"error_type": "code_error", "error_code": "function_call_missing", "step": step, "message": "function chain did not return success", "retryable": False})
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=artifacts)
                state["delivered"] = False
                run_state.save(state, state_root)
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                mark_plan_failed(error)
                _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=artifacts)
            last_agent_status = "success"
            continue
        if step == "archive":
            _transition(state, step, "running", state_root, transition_log)
            manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
            _transition(state, step, "success", state_root, transition_log, artifacts=[manifest_path])
            last_agent_status = "success"
            continue
        if step == "build_review_package":
            _transition(state, step, "running", state_root, transition_log)
            manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
            package_path = _write_review_package(state, paths, manifest_path)
            _transition(state, step, "success", state_root, transition_log, artifacts=[package_path])
            last_agent_status = "success"
            continue
        if step == "reviewer_agent":
            _transition(state, step, "running", state_root, transition_log)
            review_root = paths["review"]
            code, stdout, stderr = run_command([sys.executable, "reviewer_agent.py", "--run-id", state["run_id"], "--output-root", str(output_root), "--review-root", str(review_root), "--qa-path", str(paths["logs"] / "qa_report.json")], env, paths["logs"] / "commands.jsonl")
            result_path = review_root / "review_result.json"
            if code != 0:
                error = {"error_type": "review_rejected", "step": step, "message": stderr[-1000:] or stdout[-1000:]}
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=[result_path])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                mark_plan_failed(error)
                _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[result_path])
            last_agent_status = "success"
            continue
        if step == "reviewer_gate":
            _transition(state, step, "running", state_root, transition_log)
            result_path = paths["review"] / "review_result.json"
            result = _read_json(result_path) if result_path.exists() else {}
            review_ok, review_message = _reviewer_gate_result(result_path, paths["content"] / "market_content.json", state["run_id"])
            if not review_ok:
                error = {"error_type": "reviewer_gate_failed", "step": step, "message": review_message}
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=[result_path])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                mark_plan_failed(error)
                _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[result_path])
            last_agent_status = "success"
            continue
        if step == "offline_evaluation":
            _transition(state, step, "running", state_root, transition_log)
            from evals.evaluators.deterministic import evaluate_case

            source_items = _read_json(paths["sources"] / "normalized_materials.json") if (paths["sources"] / "normalized_materials.json").exists() else []
            content_data = _read_json(paths["content"] / "market_content.json") if (paths["content"] / "market_content.json").exists() else {}
            case = _build_offline_evaluation_case(state["run_id"], state["edition"], source_items, content_data)
            evaluation = evaluate_case(case)
            evaluation_ok = all(item.get("score") == 1.0 for name, item in evaluation.items() if name != "delivery_decision_accuracy") and evaluation["delivery_decision_accuracy"].get("score") == 1.0
            evaluation_payload = {"case_id": case["case_id"], "candidate": "current_run", "input": case["input"], "reference": case["reference"], "deterministic": evaluation, "status": "pass" if evaluation_ok else "fail", "delivered": False, "created_at": run_state.now()}
            run_state.atomic_write_json(paths["evaluation"], evaluation_payload)
            if not evaluation_ok:
                error = {"error_type": "evaluation_failed", "step": step, "message": "deterministic evaluation gate failed"}
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=[paths["evaluation"]])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                mark_plan_failed(error)
                _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[paths["evaluation"]])
            last_agent_status = "success"
            continue
        if step == "final_validation":
            _transition(state, step, "running", state_root, transition_log)
            final_ok, final_message = _final_validation_result(paths, qa_ok)
            if final_ok:
                _transition(state, step, "success", state_root, transition_log, artifacts=[paths["logs"] / "qa_report.json"])
                last_agent_status = "success"
            else:
                error = {"error_type": "quality_gate_failed", "step": step, "message": final_message}
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=[paths["logs"] / "qa_report.json"])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                mark_plan_failed(error)
                _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
                return 1
            continue
        if step == "collect_github" and not env.get("GITHUB_TOKEN", "").strip() and not shutil.which("gh"):
            _transition(state, step, "skipped", state_root, transition_log, error={"error_type": "dependency_error", "message": "GITHUB_TOKEN missing; optional enrichment skipped"})
            continue
        _transition(state, step, "running", state_root, transition_log)
        code, stdout, stderr = run_command(commands[step], env, paths["logs"] / "commands.jsonl")
        artifacts = _step_artifacts(paths, step)
        if code != 0:
            error = {"error_type": "step_failed", "error_code": str(code), "step": step, "message": stderr[-1000:] or stdout[-1000:], "retryable": step in {"generate_content", "collect_github"}}
            _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=artifacts)
            state["delivered"] = False
            run_state.save(state, state_root)
            _write_failure_reports(state, paths, False, args.edition, args.provider)
            mark_plan_failed(error)
            _write_delivery_report(state, paths, paths["logs"] / "run_manifest.json", False, error)
            return code or 1
        _transition(state, step, "success", state_root, transition_log, artifacts=artifacts)
        last_agent_status = "success"

    manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
    mark_logical("execute_plan", "success")
    state["delivered"] = False
    run_state.save(state, state_root)
    _write_delivery_report(state, paths, manifest_path, qa_ok)
    return 0 if qa_ok else 1


def _agent_runtime_paths(output_root: Path, run_id: str, *, shadow: bool, canary: bool, state_root: Path) -> dict[str, Path | bool]:
    paths = _paths(output_root, run_id, shadow=shadow, legacy_shadow=False, canary=canary)
    paths.update({
        "plan": state_root / "plans" / f"{run_id}.json",
        "decisions": state_root / "decisions" / f"{run_id}.json",
        "decision_log": paths["logs"] / "agent_decisions.jsonl",
        "state_root": state_root,
    })
    for key in ("content", "github", "sources", "images", "logs", "review", "plan", "decisions", "decision_log"):
        target = paths[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        if key not in {"plan", "decisions", "decision_log"}:
            target.mkdir(parents=True, exist_ok=True)
    return paths


def _agent_runtime_state(state: dict, state_root: Path) -> AgentState:
    checkpoint = state_root / "agent_checkpoint.json"
    if checkpoint.exists():
        try:
            restored = AgentCheckpointStore(checkpoint).load()
            # The production runtime historically used one shared checkpoint
            # path. Never let a previous run's AgentState leak into a new run;
            # only --resume for the same run may restore it.
            if restored.run_id == state.get("run_id") and restored.edition == state.get("edition"):
                return restored
        except (OSError, ValueError):
            pass
    completed: list[dict[str, object]] = []
    logical_map = {
        "collect_sources": "collect_news",
        "collect_market_quotes": "collect_market_data",
        "generate_content": "generate_content",
        "final_validation": "final_quality_gate",
        "reviewer_agent": "review_content",
        "reviewer_gate": "reviewer_gate",
        "archive": "save_report",
    }
    for executor_step, action_name in logical_map.items():
        if (state.get("steps", {}).get(executor_step) or {}).get("status") == "success":
            completed.append({"tool_name": action_name})
    return AgentState(
        goal="生成每日市场内容包",
        run_id=state["run_id"],
        edition=state.get("edition"),
        completed_actions=completed,
        step_count=len(completed),
    )


def _execute_agent(args: argparse.Namespace) -> int:
    """Production Agent V1 entry. Legacy functions are only Tool adapters."""
    global _TRACE_SESSION, _RUN_LOCK_CONTEXT, _HEALTH_CONTEXT, _OBSERVABILITY, _OBSERVABILITY_STATE
    run_id = args.resume or args.run_id or _run_id()
    run_mode = RunMode(str(getattr(args, "run_mode", RunMode.DRY_RUN.value)))
    canary_mode = bool(getattr(args, "canary_run", False))
    shadow_mode = bool(args.shadow_run or canary_mode or run_mode is RunMode.SHADOW_CANARY)
    state_root = (ROOT / "state" / "canary") if canary_mode else ((ROOT / "runtime" / "shadow" / run_id) if shadow_mode else (ROOT / "runtime"))
    if not args.resume and not args.edition:
        raise RuntimeError("--edition is required for a new run")
    if not args.resume and args.enforce_schedule and not is_schedule_slot(args.edition):
        raise RuntimeError(f"outside_schedule_window:{args.edition}")

    if args.resume:
        candidates = [ROOT / "state" / "canary", ROOT / "runtime" / "shadow" / run_id, state_root, ROOT / "runtime"]
        state = None
        for candidate in candidates:
            try:
                state = run_state.load(run_id, candidate)
                state_root = candidate
                break
            except FileNotFoundError:
                continue
        if state is None:
            raise FileNotFoundError(f"state_not_found:{run_id}")
        if args.edition and state.get("edition") and args.edition != state["edition"]:
            raise RuntimeError(f"resume_edition_mismatch:{args.edition}:{state['edition']}")
        args.edition = args.edition or state.get("edition")
        output_root = Path(state["output_root"])
    else:
        output_root = (ROOT / "outputs" / "canary" / run_id) if canary_mode else ((ROOT / "outputs" / "shadow" / run_id) if shadow_mode else (ROOT / "outputs" / "runs" / run_id))
        if output_root.exists() and any(output_root.iterdir()):
            raise RuntimeError(f"run_id_output_exists:{run_id}")
        state = run_state.create(run_id, args.edition, state_root, output_root)
        state["state_root"] = str(state_root)
        run_state.save(state, state_root)

    # Run mode is resolved before execution and re-read on resume; checkpoint
    # data cannot upgrade a run into a delivery-capable mode.
    state["run_mode"] = run_mode.value
    state["run_mode_resolved_from"] = getattr(args, "run_mode_resolved_from", "default")
    state["production_ready"] = False
    state["external_delivery_enabled"] = read_delivery_controls(ROOT).get("external_delivery_enabled", False)
    run_state.save(state, state_root)

    if _RUN_LOCK_CONTEXT is not None:
        raise RuntimeError(f"run_lock_already_held:{run_id}")
    _RUN_LOCK_CONTEXT = run_state.run_lock(run_id, state_root)
    _RUN_LOCK_CONTEXT.__enter__()
    text_only = not args.enable_images
    state["text_only"] = text_only
    state["output_mode"] = "text" if text_only else "image"
    started_at = datetime.fromisoformat(state["started_at"]) if args.resume and state.get("started_at") else None
    context = resolve_edition_context(args.edition, started_at=started_at)
    if args.enforce_schedule and not is_schedule_slot(args.edition):
        raise RuntimeError(f"outside_schedule_window:{args.edition}:{context.scheduled_local_time}")
    paths = _agent_runtime_paths(output_root, run_id, shadow=shadow_mode, canary=canary_mode, state_root=state_root)
    # Agent V1 uses the same optional Phoenix/local trace session as the
    # legacy executor. This keeps canary readiness, stability, and delivery
    # preflight spans in the authoritative run trace without making tracing a
    # prerequisite for the business workflow.
    controls = read_delivery_controls(ROOT)
    _TRACE_SESSION = TraceSession(
        state["run_id"],
        {
            "run_id": state["run_id"],
            "edition": args.edition,
            "run_mode": run_mode.value,
            "started_at": state["started_at"],
            "dry_run": True,
            "deliver_enabled": False,
            "external_delivery_enabled": controls.get("external_delivery_enabled", False),
            "timezone": "Asia/Tokyo",
            "market_session": context.market_session,
        },
        paths["logs"] / "trace.jsonl",
    )
    _TRACE_SESSION.start()
    health_root = ROOT / "reports" if not shadow_mode else output_root / "reports"
    _HEALTH_CONTEXT = {"report_root": health_root, "logs_root": paths["logs"], "task_log": paths["logs"] / "task_runs.jsonl"}
    runtime_policy = load_runtime_policy()
    _OBSERVABILITY_STATE = state
    _OBSERVABILITY = RunObserver(
        RunContext(
            run_id=run_id, task_type="market_content", target_date=context.scheduled_cutoff.date().isoformat(), session=context.market_session,
            scheduled_at=context.scheduled_cutoff.isoformat(), started_at=state["started_at"], prompt_version=context.prompt_version,
            renderer_version="disabled" if text_only else "svg_renderer_v1", delivery_enabled=False, checkpoint_resumed=bool(args.resume),
            metadata={"edition": args.edition, "timezone": "Asia/Tokyo", "controller": "DailyMarketAgent"},
        ), output_root, paths["logs"], thresholds=runtime_policy.get("monitoring") if isinstance(runtime_policy.get("monitoring"), dict) else None,
    )
    _OBSERVABILITY.stage_started("input_selection", {"edition": args.edition})
    _OBSERVABILITY.stage_finished("input_selection", "success", metadata={"scheduled_local_time": context.scheduled_local_time})
    health_path = _health_report(paths)
    health_report = _read_json(health_path)
    try:
        selected_provider = ToolRouter(health_report).choose("content_model", preferred=args.provider)["selected_tool"]
    except (RouterBlocked, KeyError, TypeError):
        selected_provider = "rule_template"
    args.provider = selected_provider
    state["selected_provider"] = selected_provider
    state["actual_provider"] = selected_provider
    state["agent_controller"] = "DailyMarketAgent"
    env = _base_env(paths, args.edition, context.prompt_text, context.prompt_version, selected_provider)
    env["MARKET_RUN_ID"] = run_id
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["MARKET_TRACE_PATH"] = str(paths["logs"] / "trace.jsonl")
    if canary_mode:
        env["SELF_HEALING_CANARY_MODE"] = "true"
        env["DRY_RUN"] = "true"
    env["REVIEWER_PROVIDER"] = "deterministic"
    function_context = BusinessContext(run_id=run_id, edition=args.edition, paths=paths, environment=env, provider=selected_provider)

    qa_path = paths["logs"] / "qa_report.json"
    manifest_path = paths["logs"] / "run_manifest.json"

    def write_qa() -> Path:
        return _write_text_qa_report(paths)

    def save_report() -> str:
        qa_ok = _read_json(qa_path).get("status") == "pass" if qa_path.exists() else False
        manifest = _write_manifest(state, paths, qa_ok, args.edition, selected_provider)
        _write_delivery_report(state, paths, manifest, qa_ok)
        return str(manifest)

    callbacks = ProductionCallbacks(write_qa=write_qa, save_report=save_report)
    bindings = build_production_bindings(function_context, callbacks)

    recovery_controller = RepairController(
        run_id,
        paths["logs"] / "recovery",
        adapters=RepairAdapters(
            retry_collector=lambda step: bindings["collect_news"](
                CollectNewsArgs(run_id=run_id, edition=args.edition, sources=["rss", "jina"])
            ) if step in {"collect_news", "collect_sources", "extract_web_content"} else {"status": "failed", "error_type": "configuration_error"},
            collect_market_quotes=lambda symbols: bindings["collect_market_data"](
                CollectMarketDataArgs(
                    run_id=run_id,
                    edition=args.edition,
                    symbols=symbols,
                    # Recovery must preserve the same temporal contract as
                    # the failed call; never replace an as-of snapshot with
                    # a latest quote during retry.
                    as_of=context.scheduled_cutoff,
                )
            ),
            validate_market_data=lambda data: bindings["validate_market_data"](
                ValidateMarketDataArgs(run_id=run_id, edition=args.edition, market_data_path=str(function_context.market_data_path))
            ),
            resume_market_pipeline=lambda steps, data: {"status": "success", "resumed_steps": steps},
            use_rule_template=lambda: bindings["generate_content"](
                GenerateContentArgs(run_id=run_id, edition=args.edition, input_path=str(function_context.source_path), provider="rule_template")
            ),
        ),
    )

    def recover_function_call(call: FunctionCall, error: Any) -> dict[str, Any]:
        if not _auto_optimization_enabled(env):
            return {"status": "repair_failed", "repair_action_succeeded": False, "blocking_reason": "auto_optimization_disabled"}
        message = str(getattr(error, "message", None) or error)
        repair_context: dict[str, Any] = {
            "provider": selected_provider,
            "current_state": {"run_id": run_id, "failed_step": call.step},
            "error_type": getattr(error, "error_type", None),
            "error_code": getattr(error, "error_code", None),
        }
        if call.tool_name in {"collect_market_data", "validate_market_data", "crosscheck_market_quote"}:
            market_artifact = _read_json(function_context.market_data_path)
            repair_context.update({
                "market_data": market_artifact,
                "validation_errors": market_artifact.get("errors", []) if isinstance(market_artifact, dict) else [],
                "cutoff": market_artifact.get("data_cutoff") if isinstance(market_artifact, dict) else None,
            })
        return recovery_controller.repair(
            f"function_{call.call_id}",
            call.step,
            message,
            context=repair_context,
        )

    state["agent_plan"] = {"goal": "generate_market_content_pack", "planner": "RuleBasedAgentPlanner", "dynamic": True, "status": "running"}
    run_state.atomic_write_json(paths["plan"], state["agent_plan"])

    def event_hook(action: AgentAction, observation) -> None:
        state.setdefault("agent_actions", []).append(action.model_dump(mode="json"))
        state.setdefault("agent_observations", []).append(observation.to_dict())
        mapping = {"collect_news": "collect_sources", "search_sources": "collect_sources", "collect_market_data": "collect_market_quotes", "crosscheck_market_quote": "collect_market_quotes", "validate_market_data": "collect_market_quotes", "generate_content": "generate_content", "validate_content_consistency": "generate_content", "final_quality_gate": "final_validation", "review_content": "reviewer_agent", "reviewer_gate": "reviewer_gate", "build_html_report": "archive", "build_markdown_report": "archive", "save_report": "archive"}
        executor_step = mapping.get(action.tool_name)
        if executor_step in (state.get("steps") or {}):
            status = "success" if observation.success else "failed"
            failure = observation.data.get("failure") if isinstance(observation.data, dict) else None
            error_payload = None
            if not observation.success:
                error_payload = {"error_type": observation.error_type, "message": observation.error_message}
                if isinstance(failure, dict):
                    error_payload["failure"] = failure
            _transition(
                state,
                executor_step,
                status,
                state_root,
                paths["logs"] / "steps.jsonl",
                error=error_payload,
                artifacts=_step_artifacts(paths, executor_step),
                metadata={"agent_action": action.tool_name, "tool": action.tool_name, "agent_stage": "market_data" if executor_step == "collect_market_quotes" else None},
            )
        run_state.save(state, state_root)

    agent_state = _agent_runtime_state(state, state_root)
    edition_context = resolve_edition_context(args.edition)
    agent_state.timezone_name = edition_context.timezone_name
    agent_state.cutoff_at = edition_context.scheduled_cutoff
    agent_state.require_market_validation = True
    agent_state.require_reviewer_gate = True
    executor = ProductionToolExecutor(
        bindings,
        defaults={"run_id": run_id, "edition": args.edition, "cutoff_at": edition_context.scheduled_cutoff.isoformat(), "provider": selected_provider, "symbols": [*CORE_SYMBOLS, "NVDA", "MSFT", "AAPL"], "sources": ["rss", "jina"], "source_path": str(function_context.source_path), "market_data_path": str(function_context.market_data_path), "content_path": str(function_context.content_path), "qa_path": str(qa_path), "report_path": str(manifest_path), "raw_response_path": str(Path(args.raw_response_file).resolve()) if args.raw_response_file else None},
        max_calls=int(runtime_policy.get("monitoring", {}).get("max_stage_retries", 30)) * 10,
        event_hook=event_hook,
        recovery_handler=recover_function_call,
    )
    # Hybrid is the safe default: it uses a model only when the selected
    # provider is healthy and falls back to the deterministic planner on any
    # provider, JSON, permission, or gate-order failure.
    planner_mode = os.environ.get("AGENT_PLANNER_MODE", "hybrid").strip().lower()
    planner_provider = os.environ.get("AGENT_PLANNER_PROVIDER", "auto").strip().lower()
    if planner_provider in {"", "auto"}:
        planner_provider = selected_provider
    fallback_planner = RuleBasedAgentPlanner(provider=selected_provider)
    if planner_mode in {"hybrid", "model"} and planner_provider in {"ollama", "gemini"}:
        planner = ModelAssistedAgentPlanner(
            provider=planner_provider,
            allowed_tools=build_registry(bindings).keys(),
            fallback=fallback_planner,
        )
        state["planner_mode"] = "model_assisted_hybrid"
        state["planner_provider"] = planner_provider
    else:
        planner = fallback_planner
        state["planner_mode"] = "rule_based"
        state["planner_provider"] = selected_provider
    run_state.save(state, state_root)
    agent = DailyMarketAgent(planner, executor, FinishPolicy(), AgentCheckpointStore(state_root / "agent_checkpoint.json"))
    result = agent.run(agent_state.goal, agent_state)
    state["agent_state"] = result.model_dump(mode="json")
    state["agent_plan"]["status"] = result.status
    state["delivered"] = False
    run_state.save(state, state_root)
    if result.status != "finished":
        error = {"error_type": "agent_blocked", "step": state.get("failed_step"), "message": result.failure_reason or "agent did not reach goal"}
        manifest = _write_manifest(state, paths, False, args.edition, selected_provider)
        _write_delivery_report(state, paths, manifest, False, error)
        return 1
    if not manifest_path.exists():
        save_report()
    return 0


def execute(args: argparse.Namespace) -> int:
    return _execute_agent(args)


def main(argv: list[str] | None = None) -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(description="可恢复的每日市场内容包构建器")
    parser.add_argument("--edition", choices=["morning_close_review", "evening_premarket_watch"], default=os.environ.get("MARKET_EDITION"))
    parser.add_argument("--provider", choices=["openai", "ollama", "gemini", "rule_template", "auto"], default=os.environ.get("MARKET_CONTENT_PROVIDER", "auto"))
    parser.add_argument("--dry-run", action="store_true", help="显式声明仅运行本地 dry-run；当前模式始终不发布")
    parser.add_argument("--run-mode", choices=[item.value for item in RunMode], help="统一运行模式；与 shadow/canary 标志冲突时 fail-closed")
    parser.add_argument("--enforce-schedule", action="store_true", help="只允许在版本配置的本地调度时间窗口内运行")
    parser.add_argument("--shadow-run", action="store_true")
    parser.add_argument("--enable-images", action="store_true", help="生成本地 SVG 图片并执行图片 QA；不启用外部发布")
    parser.add_argument("--canary-run", action="store_true", help="执行隔离 Self-Healing Canary；始终 dry-run，不发送")
    parser.add_argument("--run-id", help="可选的唯一运行 ID，格式为 market_YYYYMMDD_HHMM[_short_id]")
    parser.add_argument("--resume")
    parser.add_argument("--from-step")
    parser.add_argument("--raw-response-file")
    args = parser.parse_args(argv)
    if args.from_step and not args.resume:
        parser.error("--from-step requires --resume")
    try:
        mode_resolution = resolve_run_mode(
            cli_mode=args.run_mode,
            shadow_run=args.shadow_run,
            canary_run=args.canary_run,
            dry_run=args.dry_run,
            env=os.environ,
        )
    except RunModeConflict as exc:
        parser.error(str(exc))
    args.run_mode = mode_resolution.mode.value
    args.run_mode_resolved_from = mode_resolution.resolved_from
    canary_mode = os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() == "true"
    injected_fault = os.environ.get("SELF_HEALING_FAULT", "none").strip() or "none"
    if injected_fault not in SELF_HEALING_FAULTS:
        parser.error(f"unsupported_self_healing_fault:{injected_fault}")
    if injected_fault != "none" and not canary_mode:
        parser.error("SELF_HEALING_FAULT_requires_SELF_HEALING_CANARY_MODE=true")
    if canary_mode and not (args.shadow_run or args.canary_run or args.resume):
        parser.error("SELF_HEALING_CANARY_MODE_requires---shadow-run-or---canary-run")
    if args.canary_run and not canary_mode:
        parser.error("--canary-run_requires_SELF_HEALING_CANARY_MODE=true")
    try:
        runtime_policy = load_runtime_policy()
    except RuntimeError as exc:
        parser.error(str(exc))
    if args.enable_images and not bool(runtime_policy.get("allow_image_generation", False)):
        parser.error("image_generation_disabled_by_runtime_policy")
    os.environ["DRY_RUN"] = "true"
    started = time.monotonic()
    started_epoch = time.time()
    result = 1
    try:
        result = execute(args)
        return result
    finally:
        global _TRACE_SESSION, _RUN_LOCK_CONTEXT, _HEALTH_CONTEXT, _OBSERVABILITY, _OBSERVABILITY_STATE
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.close("success" if result == 0 else "failed")
            _TRACE_SESSION = None
        if _OBSERVABILITY is not None and _OBSERVABILITY_STATE is not None:
            try:
                _OBSERVABILITY.finalize(_OBSERVABILITY_STATE, result)
            except Exception as exc:  # observability must not alter build status
                print(f"observability report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                _OBSERVABILITY = None
                _OBSERVABILITY_STATE = None
        try:
            from healthcheck import record_task_event, write_report

            health_context = _HEALTH_CONTEXT or {
                "report_root": ROOT / "reports",
                "logs_root": ROOT / "logs",
                "task_log": ROOT / "logs" / "task_runs.jsonl",
            }
            record_task_event("success" if result == 0 else "failed", started, args.edition, started_epoch=started_epoch, log_path=health_context["task_log"])
            write_report(
                report_root=health_context["report_root"],
                task_log=health_context["task_log"],
                logs_root=health_context["logs_root"],
            )
        except Exception as exc:  # health reporting must not alter build status
            print(f"health report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            if _RUN_LOCK_CONTEXT is not None:
                _RUN_LOCK_CONTEXT.__exit__(None, None, None)
                _RUN_LOCK_CONTEXT = None
            _HEALTH_CONTEXT = None


if __name__ == "__main__":
    raise SystemExit(main())
