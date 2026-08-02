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
    ValidateContentArgs,
    ValidateMarketDataArgs,
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
