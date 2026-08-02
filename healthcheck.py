#!/usr/bin/env python3
"""Read-only service checks and runtime metrics for system_health.md."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
TASK_LOG = LOGS / "task_runs.jsonl"
TOKYO = ZoneInfo("Asia/Tokyo")


def _env_file() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, _env_file().get(name, default)).strip()


def _request(url: str, timeout: float = 5.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "market-content-healthcheck/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
            return {"status": "healthy", "http_status": response.status, "latency_ms": int((time.monotonic() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"status": "unhealthy", "http_status": exc.code, "latency_ms": int((time.monotonic() - started) * 1000), "blocking_reason": f"HTTP_{exc.code}"}
    except Exception as exc:  # health reporting must not break the market job
        return {"status": "unhealthy", "latency_ms": int((time.monotonic() - started) * 1000), "blocking_reason": f"{type(exc).__name__}: {exc}"}


def check_ollama() -> dict[str, Any]:
    base_url = setting("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    result = _request(f"{base_url}/api/tags")
    selected_model = setting("OLLAMA_MODEL", "qwen3.5:9b")
    # The tags endpoint is already part of the health check. Use it to report
    # the actual locally installed model instead of claiming an unknown model
    # merely because OLLAMA_MODEL was not exported in the shell.
    available_models: list[str] = []
    try:
        request = urllib.request.Request(f"{base_url}/api/tags", headers={"User-Agent": "market-content-healthcheck/1.0"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        available_models = [str(item.get("name")) for item in payload.get("models", []) if isinstance(item, dict) and item.get("name")]
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        available_models = []
    result.update({
        "service": "ollama",
        "base_url": base_url,
        "model": selected_model if selected_model in available_models or not available_models else None,
        "configured_model": selected_model,
        "available_models": available_models,
        "model_available": selected_model in available_models,
    })
    return result


def check_gemini() -> dict[str, Any]:
    model = setting("GEMINI_MODEL", "gemini-3.5-flash")
    key = setting("GEMINI_API_KEY")
    if not key:
        return {"service": "gemini", "status": "unavailable", "model": model, "credential_configured": False, "blocking_reason": "GEMINI_API_KEY_missing"}
    # The key is used only in the request URL and is never returned or logged.
    result = _request(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}")
    result.update({"service": "gemini", "model": model, "credential_configured": True})
    return result


def check_docker() -> dict[str, Any]:
    result: dict[str, Any] = {"service": "docker", "status": "unavailable", "docker_version": None, "compose_version": None}
    try:
        version = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=8, check=False)
        result["docker_version"] = version.stdout.strip() or version.stderr.strip() or None
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=8, check=False)
        if info.returncode != 0:
            result["blocking_reason"] = "docker_daemon_unavailable"
            return result
        compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=8, check=False)
        result["compose_version"] = compose.stdout.strip() or compose.stderr.strip() or None
        result["status"] = "healthy" if compose.returncode == 0 else "degraded"
        if compose.returncode != 0:
            result["blocking_reason"] = "docker_compose_unavailable"
    except FileNotFoundError:
        result["blocking_reason"] = "docker_command_missing"
    except Exception as exc:
        result["blocking_reason"] = f"{type(exc).__name__}: {exc}"
    return result


def check_phoenix() -> dict[str, Any]:
    configured = setting("PHOENIX_URL") or setting("PHOENIX_COLLECTOR_ENDPOINT")
    url = configured or "http://127.0.0.1:6006"
    if "phoenix:" in url:
        url = "http://127.0.0.1:6006"
    result = _request(url)
    result.update({"service": "phoenix", "url": url, "configured_endpoint": configured or None})
    return result


def record_task_event(status: str, started: float, edition: str = "", started_epoch: float | None = None) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "edition": edition,
        "started_at": datetime.fromtimestamp(started_epoch if started_epoch is not None else time.time(), timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    with TASK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _task_metrics() -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    if TASK_LOG.exists():
        for line in TASK_LOG.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append(item)
            except json.JSONDecodeError:
                continue
    today = datetime.now(TOKYO).date()

    def is_today(item: dict[str, Any]) -> bool:
        raw = item.get("started_at")
        if not isinstance(raw, str):
            return False
        try:
            return datetime.fromisoformat(raw).astimezone(TOKYO).date() == today
        except ValueError:
            return False

    completed = [item for item in events if item.get("status") in {"success", "failed"} and is_today(item)]
    success_count = sum(item.get("status") == "success" for item in completed)
    durations = [float(item["duration_seconds"]) for item in completed if isinstance(item.get("duration_seconds"), (int, float))]
    fallback_count = 0
    structured_event_count = 0
    if LOGS.exists():
        for path in LOGS.rglob("*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                raw_time = event.get("timestamp") or event.get("started_at") or event.get("finished_at")
                try:
                    event_date = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).astimezone(TOKYO).date() if raw_time else None
                except ValueError:
                    event_date = None
                if event_date != today:
                    continue
                structured_event_count += 1
                attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else event
                if bool(attributes.get("fallback_used")) or attributes.get("event") == "fallback":
                    fallback_count += 1
    return {
        "task_success_rate": round(success_count / len(completed), 4) if completed else None,
        "completed_task_count": len(completed),
        "failure_count": sum(item.get("status") == "failed" for item in completed),
        "fallback_count": fallback_count,
        "structured_event_count": structured_event_count,
        "average_runtime_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "metrics_source": str(TASK_LOG),
    }


def collect_report() -> dict[str, Any]:
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(ROOT),
        "services": {"ollama": check_ollama(), "gemini": check_gemini(), "docker": check_docker(), "phoenix": check_phoenix()},
        "metrics": _task_metrics(),
        "output": {"mode": "text", "external_publish": "removed"},
        "sensitive_values_logged": False,
    }


def _display(value: Any) -> str:
    return "暂无运行数据" if value is None else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    services = report["services"]
    metrics = report["metrics"]
    def service_status(name: str) -> str:
        item = services[name]
        reason = item.get("blocking_reason") or item.get("http_status")
        return f"{item.get('status', 'unknown')}{f' ({reason})' if reason else ''}"

    return "\n".join([
        "# System Health", "", f"检查时间：{report['checked_at']}", f"项目目录：`{report['project_root']}`", "",
        "## 服务状态", "",
        f"- Ollama：**{service_status('ollama')}**，模型：`{services['ollama'].get('model') or '未配置'}`",
        f"- Gemini：**{service_status('gemini')}**，模型：`{services['gemini'].get('model') or '未配置'}`",
        f"- Docker：**{service_status('docker')}**，{services['docker'].get('docker_version') or '版本不可读'}",
        f"- Phoenix：**{service_status('phoenix')}**，地址：`{services['phoenix'].get('url')}`", "",
        "## 今日任务指标", "",
        f"- 任务成功率：{_display(metrics.get('task_success_rate'))}",
        f"- 已完成任务数：{metrics.get('completed_task_count', 0)}",
        f"- 今日失败次数：{metrics.get('failure_count', 0)}",
        f"- Fallback 次数：{metrics.get('fallback_count', 0)}",
        "- 图片生成：已移除，当前仅输出文字与分析",
        f"- 平均运行时间：{_display(metrics.get('average_runtime_seconds'))} 秒", "",
        "## 安全", "", "- 外部发布：已移除", "- 敏感值写入报告：否", "",
    ])


def write_report(report: dict[str, Any] | None = None) -> Path:
    report = report or collect_report()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "system_health.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path = REPORTS / "system_health.md"
    path.write_text(render_markdown(report), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(write_report())
