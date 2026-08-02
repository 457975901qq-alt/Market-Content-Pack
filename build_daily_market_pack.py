#!/usr/bin/env python3
"""Resumable, fail-closed runner for text market analysis artifacts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run_state
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
from observability import TraceSession
from repair_selector import select_repair_plan
from self_healing.agent import RepairAdapters, RepairController
from self_healing.gap_analyzer import GapAnalyzer
from self_healing.repair_planner import RepairPlanner
from tool_router import RouterBlocked, ToolRouter


ROOT = Path(__file__).resolve().parent
TOKYO = ZoneInfo("Asia/Tokyo")
_TRACE_SESSION: TraceSession | None = None
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


def run_command(command: list[str], env: dict[str, str], log_path: Path) -> tuple[int, str, str]:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"command": command, "returncode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}, ensure_ascii=False) + "\n")
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode, completed.stdout, completed.stderr


def _run_id() -> str:
    return f"market_{datetime.now(TOKYO).strftime('%Y%m%d_%H%M')}"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
        "market_quotes": output_root / "market_sources" / "market_quotes.json",
        "logs": log_root,
        "review": review_root,
        "evaluation": log_root / "evaluation_report.json",
        "shadow_root": shadow_root,
        "is_shadow": shadow,
    }


def _base_env(paths: dict[str, Path], edition: str, prompt_context: str, prompt_version: str, provider: str) -> dict[str, str]:
    env = os.environ.copy()
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
    })
    return env


def _transition(state: dict, step: str, status: str, state_root: Path, log_path: Path, error: dict | None = None, artifacts: list[Path] | None = None) -> None:
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

    run_state.mark(state, step, status, state_root, error=enriched_error, artifacts=artifacts)
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
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**event, "error": enriched_error}, ensure_ascii=False) + "\n")
    if _TRACE_SESSION is not None:
        _TRACE_SESSION.step(step, status, {"error_type": (enriched_error or {}).get("error_type", ""), "category": (classification or {}).get("category", ""), "retry_count": state.get("retry_count", 0)})


def _step_artifacts(paths: dict[str, Path], step: str) -> list[Path]:
    if step == "generate_content":
        return [paths["content"] / "market_content.json", paths["content"] / "douyin.md"]
    if step == "collect_github":
        return [paths["github"] / "ai_open_source_projects.json"]
    if step == "collect_sources":
        return [paths["sources"] / "normalized_materials.json", paths["sources"] / "filtered_materials.json", paths["sources"] / "source_status.json", paths["sources"] / "web_content.json"]
    if step == "collect_market_quotes":
        return [paths["market_quotes"]]
    if step == "final_validation":
        return [paths["logs"] / "qa_report.json"]
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
        "platform_copy_files": [str(paths["content"] / "douyin.md")],
        "output_mode": "text",
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
        paths["content"] / "douyin.md",
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


def _health_report(paths: dict[str, Path]) -> Path:
    # Read-only checks are captured inside the run directory; no production
    # health report is overwritten by a Shadow run.
    import healthcheck

    report = paths["logs"] / "healthcheck.json"
    run_state.atomic_write_json(report, healthcheck.collect_report())
    return report


def _write_manifest(state: dict, paths: dict[str, Path], qa_ok: bool, edition: str, provider: str | None = None) -> Path:
    _sync_execution_plan(state, paths)
    content = paths["content"] / "market_content.json"
    content_data = _read_json(content) if content.exists() else {}
    artifact_hashes = {}
    for target in [
        content, paths["content"] / "douyin.md",
        paths["market_quotes"],
        paths.get("plan"), paths.get("decisions"),
        paths["sources"] / "normalized_materials.json",
        paths["sources"] / "filtered_materials.json",
        paths["sources"] / "source_status.json",
    ]:
        if isinstance(target, Path) and target.exists() and target.is_file():
            artifact_hashes[target.name] = run_state.sha256(target)
    selected_provider = provider or os.environ.get("MARKET_CONTENT_PROVIDER", "openai")
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
        "fallback_used": selected_provider in {"auto", "rule_template"},
        "data_cutoff": content_data.get("data_cutoff"),
        "scheduled_local_time": content_data.get("scheduled_local_time"),
        "started_at": state["started_at"],
        "finished_at": run_state.now(),
        "content_hash": run_state.sha256(content) if content.exists() else None,
        "market_data_hash": run_state.sha256(paths["market_quotes"]) if paths["market_quotes"].exists() else None,
        "artifact_hashes": artifact_hashes,
        "source_status": _read_json(paths["sources"] / "source_status.json") if (paths["sources"] / "source_status.json").exists() else {"status": "unavailable"},
        "qa_status": "pass" if qa_ok else "fail",
        "mode": "text",
        "external_publish": "removed",
        "delivered": False,
        "auto_optimization_enabled": _auto_optimization_enabled(os.environ),
        "state_path": str(run_state.path(state["run_id"], Path(state["state_root"]))),
        "output_root": str(paths["content"].parent),
        "created_at": run_state.now(),
    }
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


def execute(args: argparse.Namespace) -> int:
    global _TRACE_SESSION
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

    # Image generation and external publishing are intentionally removed from
    # the runtime. The pipeline produces text and analysis artifacts only.
    text_only = True
    state["text_only"] = True
    state["output_mode"] = "text"

    started_at = datetime.fromisoformat(state["started_at"]) if args.resume and state.get("started_at") else None
    context = resolve_edition_context(args.edition, started_at=started_at)
    if args.enforce_schedule and not is_schedule_slot(args.edition):
        raise RuntimeError(f"outside_schedule_window:{args.edition}:{context.scheduled_local_time}")
    paths = _paths(output_root, run_id, shadow=shadow_mode, legacy_shadow=False, canary=canary_mode)
    paths["plan"] = state_root / "plans" / f"{run_id}.json"
    paths["decisions"] = state_root / "decisions" / f"{run_id}.json"
    paths["decision_log"] = (paths["logs"] / "market_content_decisions.log") if shadow_mode else ROOT / "logs" / "market_content_decisions.log"
    for key in ("content", "github", "sources", "logs", "review", "shadow_root", "plan", "decisions", "decision_log"):
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
    run_state.atomic_write_json(Path(paths["decisions"]), {
        "run_id": state["run_id"],
        "plan_path": str(paths["plan"]),
        "decisions": router.decisions,
        "created_at": run_state.now(),
    })
    with Path(paths["decision_log"]).open("a", encoding="utf-8") as handle:
        for decision in router.decisions:
            handle.write(json.dumps({"run_id": state["run_id"], **decision}, ensure_ascii=False) + "\n")
    selected_provider = str(plan.get("selected_provider") or args.provider)
    args.provider = selected_provider
    env = _base_env(paths, args.edition, context.prompt_text, context.prompt_version, selected_provider)
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
        result = business_bindings["collect_market_data"](CollectMarketDataArgs(run_id=state["run_id"], edition=args.edition, symbols=symbols))
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
                text_only=True,
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
                    "fallback_used": bool(result_data.get("recovery")) if result_data else False,
                },
            )
        with (paths["logs"] / "function_calls.jsonl").open("a", encoding="utf-8") as handle:
            event = {
                "run_id": state["run_id"],
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "step": call.step,
                "status": status,
                "error": result.error.model_dump(mode="json") if result and result.error else None,
                "timestamp": run_state.now(),
            }
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
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

    def execute_function_chain(step: str):
        source_urls: list[str] = []
        if function_context.source_path.exists():
            try:
                source_urls = [str(item.get("source_url")) for item in _read_json(function_context.source_path) if isinstance(item, dict) and item.get("source_url")]
            except (OSError, ValueError):
                source_urls = []
        symbols = ["SPX", "NDX", "DJI", "NVDA", "MSFT", "AAPL"]
        if step == "collect_sources":
            first = executor.execute(make_call("collect_news", {"run_id": state["run_id"], "edition": args.edition, "sources": [str(plan.get("news_discovery") or "rss")] }))
            if first.status is not FunctionStatus.success:
                return first
            try:
                source_urls = [str(item.get("source_url")) for item in _read_json(function_context.source_path) if isinstance(item, dict) and item.get("source_url")]
            except (OSError, ValueError):
                source_urls = []
            if not source_urls:
                return first
            return executor.execute(make_call("extract_web_content", {"run_id": state["run_id"], "edition": args.edition, "urls": source_urls}))
        elif step == "collect_market_quotes":
            calls = [
                make_call("collect_market_data", {"run_id": state["run_id"], "edition": args.edition, "symbols": symbols}),
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
            last = executor.execute(call)
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
    for index, step in enumerate(run_state.STEPS):
        if index < start_index:
            continue
        if step == "health_check":
            _transition(state, step, "running", state_root, transition_log)
            # The preflight report is already the same read-only health check
            # used by the planner. Reuse it instead of probing every service
            # a second time in the same run.
            health_path = health_path if health_path.exists() else _health_report(paths)
            _transition(state, step, "success", state_root, transition_log, artifacts=[health_path])
            continue
        if state["steps"].get(step, {}).get("status") in {"success", "skipped"} and not args.from_step:
            continue
        if step in {"collect_sources", "collect_market_quotes", "generate_content", "final_validation"}:
            _transition(state, step, "running", state_root, transition_log)
            result = execute_function_chain(step)
            if step == "generate_content" and result is not None and result.status is FunctionStatus.success:
                _write_text_qa_report(paths)
                qa_ok = True
            artifacts = _step_artifacts(paths, step)
            if result is None or result.status is not FunctionStatus.success:
                error = result.error.model_dump(mode="json") if result and result.error else {"error_type": "code_error", "error_code": "function_call_missing", "step": step, "message": "function chain did not return success", "retryable": False}
                _transition(state, step, "failed", state_root, transition_log, error=error, artifacts=artifacts)
                state["delivered"] = False
                run_state.save(state, state_root)
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=artifacts)
            continue
        if step == "archive":
            _transition(state, step, "running", state_root, transition_log)
            manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
            _transition(state, step, "success", state_root, transition_log, artifacts=[manifest_path])
            continue
        if step == "build_review_package":
            _transition(state, step, "running", state_root, transition_log)
            manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
            package_path = _write_review_package(state, paths, manifest_path)
            _transition(state, step, "success", state_root, transition_log, artifacts=[package_path])
            continue
        if step == "reviewer_agent":
            _transition(state, step, "running", state_root, transition_log)
            review_root = paths["review"]
            code, stdout, stderr = run_command([sys.executable, "reviewer_agent.py", "--run-id", state["run_id"], "--output-root", str(output_root), "--review-root", str(review_root), "--qa-path", str(paths["logs"] / "qa_report.json")], env, paths["logs"] / "commands.jsonl")
            result_path = review_root / "review_result.json"
            if code != 0:
                _transition(state, step, "failed", state_root, transition_log, error={"error_type": "review_rejected", "message": stderr[-1000:] or stdout[-1000:]}, artifacts=[result_path])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[result_path])
            continue
        if step == "reviewer_gate":
            _transition(state, step, "running", state_root, transition_log)
            result_path = paths["review"] / "review_result.json"
            result = _read_json(result_path) if result_path.exists() else {}
            if result.get("decision") != "approve":
                _transition(state, step, "failed", state_root, transition_log, error={"error_type": "reviewer_gate_failed", "message": str(result.get("critical_findings") or "review result missing")}, artifacts=[result_path])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[result_path])
            continue
        if step == "offline_evaluation":
            _transition(state, step, "running", state_root, transition_log)
            from evals.evaluators.deterministic import evaluate_case

            source_items = _read_json(paths["sources"] / "normalized_materials.json") if (paths["sources"] / "normalized_materials.json").exists() else []
            content_data = _read_json(paths["content"] / "market_content.json") if (paths["content"] / "market_content.json").exists() else {}
            source_urls = [item.get("source_url") for item in source_items if item.get("source_url")]
            case = {
                "case_id": state["run_id"],
                "edition": state["edition"],
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
                    "expected_theme": (content_data.get("analysis_text") or {}).get("title") or "market_content_pack",
                    "allowed_tickers": [],
                    "forbidden_claims": [],
                    "expected_result": "fail",
                },
            }
            evaluation = evaluate_case(case)
            evaluation_ok = all(item.get("score") == 1.0 for name, item in evaluation.items() if name != "delivery_decision_accuracy") and evaluation["delivery_decision_accuracy"].get("score") == 1.0
            evaluation_payload = {"case_id": case["case_id"], "candidate": "current_run", "deterministic": evaluation, "status": "pass" if evaluation_ok else "fail", "delivered": False, "created_at": run_state.now()}
            run_state.atomic_write_json(paths["evaluation"], evaluation_payload)
            if not evaluation_ok:
                _transition(state, step, "failed", state_root, transition_log, error={"error_type": "evaluation_failed", "message": "deterministic evaluation gate failed"}, artifacts=[paths["evaluation"]])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
                return 1
            _transition(state, step, "success", state_root, transition_log, artifacts=[paths["evaluation"]])
            continue
        if step == "final_validation":
            _transition(state, step, "running", state_root, transition_log)
            final_ok, final_message = _final_validation_result(paths, qa_ok)
            if final_ok:
                _transition(state, step, "success", state_root, transition_log, artifacts=[paths["logs"] / "qa_report.json"])
            else:
                _transition(state, step, "failed", state_root, transition_log, error={"error_type": "quality_gate_failed", "message": final_message}, artifacts=[paths["logs"] / "qa_report.json"])
                _write_failure_reports(state, paths, False, args.edition, args.provider)
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
            return code or 1
        _transition(state, step, "success", state_root, transition_log, artifacts=artifacts)

    manifest_path = _write_manifest(state, paths, qa_ok, args.edition, args.provider)
    state["delivered"] = False
    run_state.save(state, state_root)
    print(manifest_path)
    print("输出模式：文字与分析；外部发布功能已移除")
    return 0 if qa_ok else 1


def main(argv: list[str] | None = None) -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(description="可恢复的每日市场内容包构建器")
    parser.add_argument("--edition", choices=["morning_close_review", "evening_premarket_watch"], default=os.environ.get("MARKET_EDITION"))
    parser.add_argument("--provider", choices=["openai", "ollama", "gemini", "rule_template", "auto"], default=os.environ.get("MARKET_CONTENT_PROVIDER", "auto"))
    parser.add_argument("--enforce-schedule", action="store_true", help="只允许在版本配置的本地调度时间窗口内运行")
    parser.add_argument("--shadow-run", action="store_true")
    parser.add_argument("--canary-run", action="store_true", help="执行隔离 Self-Healing Canary；始终 dry-run，不发送")
    parser.add_argument("--run-id", help="可选的唯一运行 ID，格式为 market_YYYYMMDD_HHMM")
    parser.add_argument("--resume")
    parser.add_argument("--from-step")
    parser.add_argument("--raw-response-file")
    args = parser.parse_args(argv)
    if args.from_step and not args.resume:
        parser.error("--from-step requires --resume")
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
    os.environ["DRY_RUN"] = "true"
    started = time.monotonic()
    started_epoch = time.time()
    result = 1
    try:
        result = execute(args)
        return result
    finally:
        global _TRACE_SESSION
        if _TRACE_SESSION is not None:
            _TRACE_SESSION.close("success" if result == 0 else "failed")
            _TRACE_SESSION = None
        try:
            from healthcheck import record_task_event, write_report

            record_task_event("success" if result == 0 else "failed", started, args.edition, started_epoch=started_epoch)
            write_report()
        except Exception as exc:  # health reporting must not alter build status
            print(f"health report failed: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
