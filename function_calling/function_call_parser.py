"""Compatibility entry point for strict Function Call parsing."""

from .function_executor import FunctionExecutor


parse_function_calls = FunctionExecutor.parse

__all__ = ["parse_function_calls"]
