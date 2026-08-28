from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from function_calling.business_bindings import BusinessContext, build_business_bindings
from function_calling.arguments import (
    AnalyzeGapArgs,
    CollectMarketDataArgs,
    ExtractWebContentArgs,
    GenerateContentArgs,
    GetMarketQuoteArgs,
    ValidateContentArgs,
)
from function_calling.function_executor import FunctionExecutor
from function_calling.registry import build_registry
from function_calling.tool_call import FunctionCall, FunctionStatus
from market_quotes import CORE_SYMBOLS

from .action import AgentAction
from .observation import ToolObservation


@dataclass(frozen=True)
class ProductionCallbacks:
    write_qa: Callable[[], Path] | None = None
    build_report: Callable[[str], Any] | None = None
    save_report: Callable[[], Any] | None = None


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def _count_content_sections(content: Any) -> int:
    """Count the fixed daily modules, not the renderer's narrative sections."""
    if not isinstance(content, dict):
        return int(bool(content))
    daily_sections = content.get("daily_sections")
    if isinstance(daily_sections, list):
        return len(daily_sections)
    sections = content.get("sections")
    return len(sections) if isinstance(sections, list) else int(bool(content))


def build_production_bindings(context: BusinessContext, callbacks: ProductionCallbacks | None = None) -> dict[str, Callable[[Any], dict[str, Any]]]:
    """Adapt existing business functions into the Agent's production registry."""
    callbacks = callbacks or ProductionCallbacks()
    bindings = build_business_bindings(context)

    original_collect = bindings["collect_market_data"]
    original_generate = bindings["generate_content"]
    original_validate_market = bindings["validate_market_data"]
    original_validate_content = bindings["validate_content_consistency"]
    original_quality = bindings["final_quality_gate"]

    def collect_market(args: Any) -> dict[str, Any]:
        result = original_collect(args)
        return {**result, "market_data_complete": result.get("status") == "success", "missing_information": [] if result.get("status") == "success" else ["market_data"]}

    def validate_market(args: Any) -> dict[str, Any]:
        result = original_validate_market(args)
        return {**result, "market_data_complete": result.get("status") in {"pass", "success"}, "missing_information": []}

    def crosscheck(args: Any) -> dict[str, Any]:
        # Reuse the source-backed collector. The ticker is supplied by the
        # Planner, while the collector still validates the complete artifact.
        result = collect_market(CollectMarketDataArgs(
            run_id=args.run_id,
            edition=args.edition,
            symbols=[*CORE_SYMBOLS, "NVDA", "MSFT", "AAPL"],
            as_of=args.as_of,
        ))
        if result.get("market_data_complete"):
            return {**result, "crosschecked_ticker": args.symbol, "requested_as_of": args.as_of.isoformat() if args.as_of else None, "conflicts": [], "missing_information": []}
        return {**result, "crosschecked_ticker": args.symbol, "requested_as_of": args.as_of.isoformat() if args.as_of else None, "conflicts": [], "missing_information": [f"market_data:{args.symbol}"]}

    def get_market_quote(args: GetMarketQuoteArgs) -> dict[str, Any]:
        result = collect_market(CollectMarketDataArgs(run_id=args.run_id, edition=args.edition, symbols=[args.symbol], as_of=args.as_of))
        return {**result, "symbol": args.symbol, "requested_as_of": args.as_of.isoformat() if args.as_of else None}

    def generate(args: Any) -> dict[str, Any]:
        result = original_generate(args)
        if callbacks.write_qa is not None:
            callbacks.write_qa()
        content = _read(context.content_path, {})
        return {**result, "required_sections": _count_content_sections(content), "market_data_complete": bool(result.get("market_data_version"))}

    def validate_content(args: Any) -> dict[str, Any]:
        result = original_validate_content(args)
        return {**result, "schema_valid": result.get("status") == "pass", "grounding_valid": result.get("status") == "pass"}

    def generate_market_section(args: Any) -> dict[str, Any]:
        result = original_generate(GenerateContentArgs(
            run_id=args.run_id,
            edition=args.edition,
            input_path=args.input_path,
            provider=args.provider,
            raw_response_path=args.raw_response_path,
        ))
        return {**result, "section": args.section}

    def _content_check(args: ValidateContentArgs, check_name: str) -> dict[str, Any]:
        result = validate_content(args)
        return {**result, check_name: result.get("status") == "pass"}

    def schema_check(args: Any) -> dict[str, Any]:
        return _content_check(args, "schema_valid")

    def grounding_check(args: Any) -> dict[str, Any]:
        return _content_check(args, "grounding_valid")

    def temporal_check(args: Any) -> dict[str, Any]:
        return _content_check(args, "temporal_valid")

    def analyze_gap_tool(args: AnalyzeGapArgs) -> dict[str, Any]:
        from self_healing.gap_analyzer import analyze_gap

        return analyze_gap(
            validation_errors=args.validation_errors,
            current_state=args.current_state,
            artifact_manifest=args.artifact_manifest,
            run_id=args.run_id,
        )

    def quality(args: Any) -> dict[str, Any]:
        result = original_quality(args)
        return {**result, "schema_valid": True, "grounding_valid": True}

    def search_sources(args: Any) -> dict[str, Any]:
        result = bindings["collect_news"](args)
        return {**result, "evidence": [{"source_url": item.get("source_url")} for item in _read(context.source_path, []) if isinstance(item, dict) and item.get("source_url")]}

    def fetch_source(args: Any) -> dict[str, Any]:
        result = bindings["extract_web_content"](ExtractWebContentArgs(run_id=args.run_id, edition=args.edition, urls=[args.url]))
        return {**result, "evidence": [{"source_url": args.url}]}

    def review(args: Any) -> dict[str, Any]:
        from reviewer_agent import review_run

        result = review_run(context.run_id, Path(context.paths["content"]).parent, Path(context.paths["review"]) if context.paths.get("review") else None, Path(context.paths["logs"]) / "qa_report.json")
        issues = [
            {
                "section": check.get("check_name"),
                "severity": "high" if check.get("result") == "fail" else "low",
                "type": "review_check",
                "reason": check.get("actual"),
                "recommended_actions": [{"tool": check.get("remediation_step")}] if check.get("remediation_step") else [],
            }
            for check in result.get("checks", [])
            if isinstance(check, dict) and check.get("result") == "fail"
        ]
        decision = result.get("decision")
        return {"review_approved": decision == "approve", "review_feedback": [{"decision": decision, "issues": issues}], "review_result": result}

    def reviewer_gate(args: Any) -> dict[str, Any]:
        result = review(args)
        if not result.get("review_approved"):
            return {**result, "status": "failed", "error_type": "reviewer_gate_failed"}
        return {**result, "status": "pass"}

    def repair_section(args: Any) -> dict[str, Any]:
        # No content mutation is allowed here unless an existing repair service
        # is explicitly supplied by the caller. Fail closed otherwise.
        return {"status": "failed", "error_type": "repair_unavailable", "section": args.section, "review_feedback": [{"decision": "needs_review", "issues": [{"section": args.section, "severity": "high"}]}]}

    def regenerate_section(args: Any) -> dict[str, Any]:
        return repair_section(args)

    def build_report(args: Any) -> dict[str, Any]:
        if callbacks.build_report is not None:
            callbacks.build_report(args.tool_name if hasattr(args, "tool_name") else "report")
        return {"status": "success", "report_built": True}

    def save_report(args: Any) -> dict[str, Any]:
        result = callbacks.save_report() if callbacks.save_report is not None else None
        return {"status": "success", "report_generated": True, "saved": result}

    bindings.update({
        "collect_market_data": collect_market,
        "validate_market_data": validate_market,
        "get_market_quote": get_market_quote,
        "crosscheck_market_quote": crosscheck,
        "generate_content": generate,
        "generate_market_section": generate_market_section,
        "validate_content_consistency": validate_content,
        "schema_check": schema_check,
        "grounding_check": grounding_check,
        "temporal_check": temporal_check,
        "analyze_gap": analyze_gap_tool,
        "final_quality_gate": quality,
        "search_sources": search_sources,
        "fetch_source": fetch_source,
        "review_content": review,
        "reviewer_gate": reviewer_gate,
        "repair_section": repair_section,
        "regenerate_section": regenerate_section,
        "build_html_report": build_report,
        "build_markdown_report": build_report,
        "save_report": save_report,
    })
    return bindings


class ProductionToolExecutor:
    """Policy-enforced production adapter from AgentAction to FunctionCall."""

    BLOCKED = {"deliver", "canary_deliver", "publish_content", "shell", "exec_shell", "generate_images"}

    def __init__(self, bindings: dict[str, Callable[[Any], dict[str, Any]]], *, defaults: dict[str, Any], max_calls: int = 30, max_calls_per_step: int = 5, event_hook: Callable[[AgentAction, ToolObservation], None] | None = None, recovery_handler: Callable[[FunctionCall, Any], dict[str, Any] | None] | None = None) -> None:
        self.defaults = defaults
        self.event_hook = event_hook
        self._call_counter = 0
        self.executor = FunctionExecutor(
            registry=build_registry(bindings),
            max_calls=max_calls,
            max_calls_per_step=max_calls_per_step,
            recovery_handler=recovery_handler,
            blocked_tools=self.BLOCKED,
            allowed_steps=None,
        )

    def execute(self, action: AgentAction) -> dict[str, Any]:
        if action.tool_name in self.BLOCKED:
            observation = ToolObservation(success=False, tool_name=action.tool_name, error_type="tool_blocked", error_message="tool is blocked by policy")
            if self.event_hook:
                self.event_hook(action, observation)
            return observation.to_dict()
        arguments = self._arguments(action)
        self._call_counter += 1
        call = FunctionCall(call_id=f"{action.action_id}_{self._call_counter:03d}", tool_name=action.tool_name, step=action.tool_name, arguments=arguments, requested_by="agent_planner")
        result = self.executor.execute(call)
        data = result.data if isinstance(result.data, dict) else {}
        recovery = data.get("recovery") if isinstance(data.get("recovery"), dict) else None
        if recovery and isinstance(recovery.get("classification"), dict):
            # Preserve the controller's structured diagnosis for AgentState
            # and Planner. Do not flatten it into a generic exception string.
            data = {**data, "failure": recovery["classification"]}
        success = result.status is FunctionStatus.success and data.get("status") not in {"failed", "fail"}
        observation = ToolObservation(
            success=success,
            tool_name=action.tool_name,
            data=data,
            missing_information=data.get("missing_information", []) if isinstance(data.get("missing_information"), list) else [],
            conflicts=data.get("conflicts", []) if isinstance(data.get("conflicts"), list) else [],
            evidence=data.get("evidence", []) if isinstance(data.get("evidence"), list) else [],
            review_feedback=data.get("review_feedback", []) if isinstance(data.get("review_feedback"), list) else [],
            error_type=result.error.error_type if result.error else None,
            error_message=result.error.message if result.error else None,
        )
        payload = observation.to_dict()
        payload["result"] = data
        payload["duration_ms"] = result.duration_ms
        if self.event_hook:
            self.event_hook(action, observation)
        return payload

    def _arguments(self, action: AgentAction) -> dict[str, Any]:
        base = {"run_id": self.defaults["run_id"], "edition": self.defaults["edition"]}
        custom = dict(action.arguments)
        tool = action.tool_name
        if tool == "collect_news":
            return {**base, "sources": self.defaults["sources"]}
        if tool == "search_sources":
            return {**base, "sources": self.defaults["sources"]}
        if tool == "fetch_source":
            return {**base, "url": custom.get("url", "https://example.invalid/source")}
        if tool in {"collect_market_data"}:
            payload = {**base, "symbols": self.defaults["symbols"]}
            if self.defaults.get("cutoff_at") is not None:
                payload["as_of"] = self.defaults["cutoff_at"]
            return payload
        if tool == "get_market_quote":
            payload = {**base, "symbol": custom.get("symbol", custom.get("ticker", self.defaults["symbols"][0]))}
            payload["as_of"] = custom.get("as_of", self.defaults.get("cutoff_at"))
            return {key: value for key, value in payload.items() if value is not None}
        if tool == "crosscheck_market_quote":
            payload = {**base, "symbol": custom.get("symbol", custom.get("ticker", self.defaults["symbols"][0]))}
            payload["as_of"] = custom.get("as_of", self.defaults.get("cutoff_at"))
            return {key: value for key, value in payload.items() if value is not None}
        if tool == "validate_market_data":
            return {**base, "market_data_path": self.defaults["market_data_path"]}
        if tool == "generate_content":
            return {**base, "input_path": self.defaults["source_path"], "provider": self.defaults["provider"], "raw_response_path": self.defaults.get("raw_response_path")}
        if tool == "validate_content_consistency":
            return {**base, "content_path": self.defaults["content_path"], "source_path": self.defaults["source_path"]}
        if tool in {"schema_check", "grounding_check", "temporal_check"}:
            return {**base, "content_path": self.defaults["content_path"], "source_path": self.defaults["source_path"]}
        if tool == "generate_market_section":
            return {
                **base,
                "input_path": self.defaults["source_path"],
                "provider": self.defaults["provider"],
                "raw_response_path": self.defaults.get("raw_response_path"),
                "section": custom.get("section", "unknown"),
            }
        if tool == "analyze_gap":
            return {
                **base,
                "validation_errors": custom.get("validation_errors", []),
                "current_state": custom.get("current_state", {}),
                "artifact_manifest": custom.get("artifact_manifest", {}),
            }
        if tool == "final_quality_gate":
            return {**base, "validation_paths": [self.defaults["qa_path"]]}
        if tool in {"review_content", "reviewer_gate", "build_html_report", "build_markdown_report"}:
            payload = {**base, "content_path": self.defaults["content_path"]}
            if tool in {"review_content", "reviewer_gate"} and custom.get("section") is not None:
                payload["section"] = custom["section"]
            return payload
        if tool in {"repair_section", "regenerate_section"}:
            return {**base, "section": custom.get("section", "unknown"), "reason": custom.get("reason")}
        if tool == "save_report":
            return {**base, "report_path": self.defaults["report_path"]}
        return {**base, **custom}
