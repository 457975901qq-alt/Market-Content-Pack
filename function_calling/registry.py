from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Type

from pydantic import BaseModel

from .arguments import (
    CollectMarketDataArgs,
    CollectNewsArgs,
    ExtractWebContentArgs,
    FinalQualityGateArgs,
    GenerateContentArgs,
    GenerateMarketSectionArgs,
    ValidateContentArgs,
    ValidateMarketDataArgs,
    CrosscheckMarketQuoteArgs,
    GetMarketQuoteArgs,
    SchemaCheckArgs,
    GroundingCheckArgs,
    TemporalCheckArgs,
    AnalyzeGapArgs,
    SearchSourcesArgs,
    FetchSourceArgs,
    ReviewContentArgs,
    ReviewerGateArgs,
    RepairSectionArgs,
    RegenerateSectionArgs,
    ReportArgs,
    SaveReportArgs,
)
from .tool_schema import ToolFunctionSchema
from .tool_result import FunctionResult


@dataclass(frozen=True)
class RegisteredFunction:
    schema: ToolFunctionSchema
    argument_model: Type[BaseModel]
    result_model: Type[BaseModel]
    callable: Callable[[BaseModel], dict[str, Any]]


ALLOWED_FUNCTIONS = (
    "collect_market_data",
    "collect_news",
    "extract_web_content",
    "generate_content",
    "validate_market_data",
    "validate_content_consistency",
    "final_quality_gate",
    "get_market_quote",
    "crosscheck_market_quote",
    "generate_market_section",
    "schema_check",
    "grounding_check",
    "temporal_check",
    "analyze_gap",
    "search_sources",
    "fetch_source",
    "review_content",
    "reviewer_gate",
    "repair_section",
    "regenerate_section",
    "build_html_report",
    "build_markdown_report",
    "save_report",
)


def _mock_binding(args: BaseModel) -> dict[str, Any]:
    return {"accepted": True, "argument_model": type(args).__name__}


def build_registry(bindings: dict[str, Callable[[BaseModel], dict[str, Any]]] | None = None) -> dict[str, RegisteredFunction]:
    bindings = bindings or {}
    definitions: list[tuple[str, str, Type[BaseModel], list[str]]] = [
        ("collect_market_data", "Collect validated market data", CollectMarketDataArgs, ["collect_market_data"]),
        ("collect_news", "Collect news through the selected provider route", CollectNewsArgs, ["collect_news"]),
        ("extract_web_content", "Extract source-backed web content", ExtractWebContentArgs, ["extract_web_content"]),
        ("generate_content", "Generate the structured market content pack", GenerateContentArgs, ["generate_content"]),
        ("validate_market_data", "Validate market data and source freshness", ValidateMarketDataArgs, ["validate_market_data"]),
        ("validate_content_consistency", "Validate content against source data", ValidateContentArgs, ["validate_content_consistency"]),
        ("final_quality_gate", "Run the final quality gate", FinalQualityGateArgs, ["final_quality_gate"]),
        ("get_market_quote", "Read the latest quote, or the last real observation at or before a timezone-aware as_of cutoff", GetMarketQuoteArgs, ["get_market_quote"]),
        ("crosscheck_market_quote", "Cross-check a quote using the same timezone-aware as_of cutoff", CrosscheckMarketQuoteArgs, ["crosscheck_market_quote"]),
        ("generate_market_section", "Generate one bounded market section", GenerateMarketSectionArgs, ["generate_market_section"]),
        ("schema_check", "Check the content schema", SchemaCheckArgs, ["schema_check"]),
        ("grounding_check", "Check content grounding against evidence", GroundingCheckArgs, ["grounding_check"]),
        ("temporal_check", "Check report and market-data timing", TemporalCheckArgs, ["temporal_check"]),
        ("analyze_gap", "Analyze a validated upstream gap", AnalyzeGapArgs, ["analyze_gap"]),
        ("search_sources", "Search configured source routes for missing evidence", SearchSourcesArgs, ["search_sources"]),
        ("fetch_source", "Fetch one source already selected by the planner", FetchSourceArgs, ["fetch_source"]),
        ("review_content", "Run the independent content reviewer", ReviewContentArgs, ["review_content"]),
        ("reviewer_gate", "Enforce the independent reviewer decision", ReviewerGateArgs, ["reviewer_gate"]),
        ("repair_section", "Repair only the section identified by the reviewer", RepairSectionArgs, ["repair_section"]),
        ("regenerate_section", "Regenerate only the section identified by the reviewer", RegenerateSectionArgs, ["regenerate_section"]),
        ("build_html_report", "Render the validated HTML report", ReportArgs, ["build_html_report"]),
        ("build_markdown_report", "Render the text fallback report", ReportArgs, ["build_markdown_report"]),
        ("save_report", "Persist the validated delivery report", SaveReportArgs, ["save_report"]),
    ]
    result: dict[str, RegisteredFunction] = {}
    for name, description, model, steps in definitions:
        result[name] = RegisteredFunction(
            schema=ToolFunctionSchema(
                name=name,
                description=description,
                parameters=model.model_json_schema(),
                supported_steps=steps,
                enabled=True,
                timeout_seconds=120,
                requires_approval=False,
            ),
            argument_model=model,
            result_model=FunctionResult,
            callable=bindings.get(name, _mock_binding),
        )
    return result
