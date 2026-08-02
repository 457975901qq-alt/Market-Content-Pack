"""Fixed bindings from business Functions to the existing market pipeline.

The model never supplies a module path or callable.  This module owns the
only bindings that may be registered for the production pipeline.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .arguments import (
    CollectMarketDataArgs,
    CollectNewsArgs,
    ExtractWebContentArgs,
    FinalQualityGateArgs,
    GenerateContentArgs,
    ValidateContentArgs,
    ValidateMarketDataArgs,
)


class BusinessFunctionError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "code_error", error_code: str = "business_function_failed", retryable: bool = False) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True)
class BusinessContext:
    run_id: str
    edition: str
    paths: dict[str, Path | bool]
    environment: dict[str, str]
    provider: str

    @property
    def content_path(self) -> Path:
        return Path(self.paths["content"]) / "market_content.json"

    @property
    def source_path(self) -> Path:
        return Path(self.paths["sources"]) / "normalized_materials.json"

    @property
    def market_data_path(self) -> Path:
        return Path(self.paths["market_quotes"])

    @contextlib.contextmanager
    def activated_environment(self):
        previous = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _require_file(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        raise BusinessFunctionError(f"{label}_missing:{path}", error_type="configuration_error", error_code=f"{label}_missing")


def _collect_market_data(context: BusinessContext, args: CollectMarketDataArgs) -> dict[str, Any]:
    from market_quotes import collect_quotes, write_artifact

    payload = collect_quotes(args.edition.value, symbols=args.symbols)
    write_artifact(payload, context.market_data_path)
    if payload.get("status") != "success":
        raise BusinessFunctionError("market_data_not_validated", error_type="data_validation_error", error_code="market_data_incomplete", retryable=True)
    return {"status": "success", "output": str(context.market_data_path), "quote_count": len(payload.get("quotes", [])), "market_data_version": payload.get("market_data_version")}


def _collect_news(context: BusinessContext, args: CollectNewsArgs) -> dict[str, Any]:
    import source_router

    with context.activated_environment():
        result = source_router.collect(Path(context.paths["sources"]), edition=args.edition.value)
    if not isinstance(result, dict):
        raise BusinessFunctionError("source_router_invalid_result", error_type="code_error", error_code="source_router_invalid_result")
    return {"status": "success", **result}


def _extract_web_content(context: BusinessContext, args: ExtractWebContentArgs) -> dict[str, Any]:
    # source_router performs configured Jina enrichment during collection. This
    # Function validates and materializes the source-backed extraction artifact
    # without fetching a second time or inventing article text.
    _require_file(context.source_path, "normalized_materials")
    materials = json.loads(context.source_path.read_text(encoding="utf-8"))
    by_url = {str(item.get("source_url")): item for item in materials if isinstance(item, dict) and item.get("source_url")}
    selected = [by_url[url] for url in args.urls if url in by_url]
    output = Path(context.paths["sources"]) / "web_content.json"
    _write_json(output, {"run_id": context.run_id, "urls": args.urls, "materials": selected, "source_count": len(selected)})
    return {"status": "success", "output": str(output), "source_count": len(selected)}


def _generate_content(context: BusinessContext, args: GenerateContentArgs) -> dict[str, Any]:
    import market_content_openai as content_module
    from model_providers import ProviderError

    input_path = Path(args.input_path)
    _require_file(input_path, "content_input")
    with context.activated_environment():
        content_module.OUTPUT_DIR = Path(context.paths["content"])
        content_module.OUTPUT_JSON = context.content_path
        content_module.PLATFORM_COPY_MD = Path(context.paths["content"]) / "douyin.md"
        edition_context = content_module._edition_context(args.edition.value)
        market_context = content_module.read_context(input_path, context.market_data_path)

        def attempt(provider: str) -> dict[str, Any]:
            if args.raw_response_path:
                raw = Path(args.raw_response_path).read_text(encoding="utf-8")
            elif provider == "rule_template":
                # The fallback is deterministic, but it must carry forward
                # validated symbols so the data-lineage gate can still pass.
                market_data = json.loads(context.market_data_path.read_text(encoding="utf-8"))
                raw = json.dumps(
                    content_module.rule_template_response(edition_context, market_data=market_data),
                    ensure_ascii=False,
                )
            else:
                raw = content_module.generate_with_provider(market_context, edition_context, provider)
            return content_module.run(raw, edition=args.edition.value, market_data_path=context.market_data_path)

        providers = [args.provider]
        if not args.raw_response_path:
            # A healthy provider can still return schema-invalid JSON. Route
            # that bounded failure through the already registered fallback
            # chain before failing the business Function.
            providers.extend({
                "ollama": ["gemini", "rule_template"],
                "gemini": ["rule_template"],
                "auto": ["gemini", "rule_template"],
            }.get(args.provider, []))
        primary_error: Exception | None = None
        used_provider = args.provider
        parsed: dict[str, Any] | None = None
        for provider in dict.fromkeys(providers):
            try:
                parsed = attempt(provider)
                used_provider = provider
                break
            except (content_module.MarketContentError, ProviderError, OSError, ValueError) as exc:
                primary_error = primary_error or exc
                continue
        if parsed is None:
            if isinstance(primary_error, content_module.MarketContentError):
                raise BusinessFunctionError(
                    str(primary_error),
                    error_type="data_validation_error" if primary_error.error_type in {"empty_required_field", "date_mismatch", "edition_fields_missing", "edition_metadata_mismatch", "json_parse_failed", "market_data_not_propagated"} else "dependency_error",
                    error_code=primary_error.error_type,
                    retryable=False,
                ) from primary_error
            raise BusinessFunctionError("content_provider_chain_exhausted", error_type="dependency_error", error_code="content_provider_chain_exhausted", retryable=False) from primary_error
    return {"status": "success", "output": str(context.content_path), "content_hash": hashlib.sha256(context.content_path.read_bytes()).hexdigest(), "market_data_version": parsed.get("market_data_version"), "provider_used": used_provider, "fallback_used": used_provider != args.provider}


def _validate_market_data(context: BusinessContext, args: ValidateMarketDataArgs) -> dict[str, Any]:
    path = Path(args.market_data_path)
    _require_file(path, "market_data")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = set(payload.get("required_symbols") or {"SPX", "NDX", "DJI"})
    actual = {str(item.get("symbol")) for item in payload.get("quotes", []) if isinstance(item, dict)}
    missing = sorted(required - actual)
    if payload.get("status") != "success" or missing:
        raise BusinessFunctionError(f"market_data_missing:{missing}", error_type="data_validation_error", error_code="market_data_incomplete", retryable=True)
    return {"status": "pass", "market_data_version": payload.get("market_data_version"), "validated_symbols": sorted(actual)}


def _validate_content(context: BusinessContext, args: ValidateContentArgs) -> dict[str, Any]:
    import market_content_openai as content_module

    content_path = Path(args.content_path)
    _require_file(content_path, "market_content")
    data = json.loads(content_path.read_text(encoding="utf-8"))
    content_module.validate_market_content(data, json.dumps(data, ensure_ascii=False), content_module._edition_context(args.edition.value))
    return {"status": "pass", "content_hash": hashlib.sha256(content_path.read_bytes()).hexdigest()}


def _final_quality_gate(context: BusinessContext, args: FinalQualityGateArgs) -> dict[str, Any]:
    from text_validation import validate_text_artifacts

    qa_path = Path(args.validation_paths[0]) if args.validation_paths else None
    result = validate_text_artifacts(context.content_path, Path(context.paths["content"]) / "douyin.md", expected_edition=args.edition.value)
    if qa_path is None or not qa_path.exists() or qa_path.stat().st_size == 0:
        result["critical_errors"].append("qa_report_file")
        result["status"] = "fail"
    if result["status"] != "pass":
        raise BusinessFunctionError(f"quality_gate_failed:{result['critical_errors']}", error_type="data_validation_error", error_code="final_quality_gate_failed")
    return {"status": "pass", "validated_artifacts": [str(context.content_path), str(qa_path)]}


def build_business_bindings(context: BusinessContext) -> dict[str, Callable[[Any], dict[str, Any]]]:
    return {
        "collect_market_data": lambda args: _collect_market_data(context, args),
        "collect_news": lambda args: _collect_news(context, args),
        "extract_web_content": lambda args: _extract_web_content(context, args),
        "generate_content": lambda args: _generate_content(context, args),
        "validate_market_data": lambda args: _validate_market_data(context, args),
        "validate_content_consistency": lambda args: _validate_content(context, args),
        "final_quality_gate": lambda args: _final_quality_gate(context, args),
    }


__all__ = ["BusinessContext", "BusinessFunctionError", "build_business_bindings"]
