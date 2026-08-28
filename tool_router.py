"""Deterministic, allowlisted provider routing for the market pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import get_secret


ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "config" / "tool_routing_policy.json"


class RouterBlocked(RuntimeError):
    def __init__(self, task: str, rejected_tools: list[dict[str, Any]]) -> None:
        self.task = task
        self.rejected_tools = rejected_tools
        super().__init__(f"no_available_tool:{task}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("tool_routing_policy_must_be_object")
    return data


class ToolRouter:
    """Select only registered tools and retain rejection evidence."""

    def __init__(self, health_report: dict[str, Any] | None = None, policy_path: Path = POLICY_PATH) -> None:
        self.policy = _load_policy(policy_path)
        self.health_report = health_report or {}
        self.decisions: list[dict[str, Any]] = []
        self.runtime_failed_tools: dict[str, str] = {}

    def mark_runtime_failure(self, tool: str, reason: str) -> None:
        """Exclude a failed provider from the rest of this run.

        The static health report remains an input snapshot; runtime failures
        are tracked separately so a temporary failure cannot be selected again
        before the next health check.
        """
        if not tool:
            return
        self.runtime_failed_tools[tool] = reason[:300]
        self.decisions.append({
            "task": "runtime_failure",
            "candidates": [tool],
            "rejected_tools": [{"tool": tool, "reason": reason[:300]}],
            "selected_tool": None,
            "decision_factors": {"runtime_failure": True},
            "selected_reason": "tool_disabled_for_current_run",
            "fallback_chain": [],
            "previous_tool": tool,
            "switch_reason": "runtime_failure",
            "timestamp": _now(),
        })

    def _health(self, tool: str) -> tuple[str, str | None]:
        source_item = (self.health_report.get("sources") or {}).get(tool)
        if isinstance(source_item, dict):
            status = str(source_item.get("status") or "unknown")
            return status, source_item.get("blocking_reason") or source_item.get("reason") or "source_unavailable"
        # Source health is often unavailable before the collector runs. Use the
        # same explicit switches as source_router so planning does not claim an
        # unavailable route is healthy merely because no report exists yet.
        if tool in {"x", "exa"} and os.environ.get("SOURCE_ROUTER_LIVE", "true").lower() != "true":
            return "unavailable", "SOURCE_ROUTER_LIVE=false"
        if tool == "jina" and os.environ.get("JINA_ENRICH", "true").lower() != "true":
            return "unavailable", "JINA_ENRICH=false"
        if tool == "rss" and os.environ.get("RSS_FEEDS") == "":
            return "unavailable", "RSS_FEEDS_not_configured"
        service_map = {
            "ollama": "ollama",
            "gemini": "gemini",
            "openai": "openai",
            "docker": "docker",
            "phoenix": "phoenix",
        }
        service = service_map.get(tool)
        if service is None:
            return "healthy", None
        item = (self.health_report.get("services") or {}).get(service) or {}
        status = str(item.get("status") or "")
        if tool == "openai":
            return ("healthy", None) if get_secret("OPENAI_API_KEY", consumer="content_generator", purpose="generate_market_content", run_id="tool-router") else ("unavailable", "OPENAI_API_KEY_missing")
        if status in {"healthy", "configured"}:
            return status, None
        return status or "unknown", item.get("blocking_reason") or "health_status_unavailable"

    def choose(
        self,
        task: str,
        *,
        preferred: str | None = None,
        used_tools: set[str] | None = None,
        previous_tool: str | None = None,
    ) -> dict[str, Any]:
        routes = self.policy.get("routes") or {}
        configured = list(routes.get(task) or [])
        candidates = [preferred] if preferred and preferred != "auto" else configured
        used_tools = used_tools or set()
        rejected: list[dict[str, Any]] = []
        accepted: list[tuple[int, str, dict[str, Any]]] = []
        for name in candidates:
            if not name or name in used_tools:
                rejected.append({"tool": name, "reason": "already_used" if name in used_tools else "empty_tool_name"})
                continue
            if name in self.runtime_failed_tools:
                rejected.append({"tool": name, "reason": self.runtime_failed_tools[name]})
                continue
            config = (self.policy.get("tools") or {}).get(name)
            if not isinstance(config, dict):
                rejected.append({"tool": name, "reason": "not_registered"})
                continue
            if not config.get("enabled", False):
                rejected.append({"tool": name, "reason": "disabled"})
                continue
            if task not in list(config.get("supported_tasks") or []):
                rejected.append({"tool": name, "reason": "task_not_supported"})
                continue
            health, reason = self._health(name)
            if health in {"unhealthy", "unavailable", "failed", "stale"}:
                rejected.append({"tool": name, "reason": reason or health})
                continue
            accepted.append((int(config.get("priority", 0)), name, config))
        if not accepted:
            decision = {
                "task": task,
                "candidates": candidates,
                "rejected_tools": rejected,
                "selected_tool": None,
                "decision_factors": {"health_checked": True, "used_tools": sorted(used_tools)},
                "selected_reason": "no_registered_healthy_candidate",
                "fallback_chain": [],
                "previous_tool": previous_tool,
                "switch_reason": "all_candidates_rejected",
                "timestamp": _now(),
            }
            self.decisions.append(decision)
            raise RouterBlocked(task, rejected)
        accepted.sort(key=lambda item: (-item[0], candidates.index(item[1])))
        selected_priority, selected, selected_config = accepted[0]
        fallback_chain = [name for _, name, _ in accepted[1:]]
        decision = {
            "task": task,
            "candidates": candidates,
            "rejected_tools": rejected,
            "selected_tool": selected,
            "decision_factors": {
                "health_checked": True,
                "health_status": self._health(selected)[0],
                "priority": selected_priority,
                "quality_level": selected_config.get("quality_level"),
                "used_tools": sorted(used_tools),
            },
            "selected_reason": "highest_priority_healthy_registered_tool",
            "fallback_chain": fallback_chain,
            "previous_tool": previous_tool,
            "switch_reason": None if previous_tool in {None, selected} else "previous_tool_not_selected",
            "timestamp": _now(),
        }
        self.decisions.append(decision)
        return decision


__all__ = ["RouterBlocked", "ToolRouter"]
