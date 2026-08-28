#!/usr/bin/env python3
"""Optional source layer with one normalized material contract.

The collector is deliberately conservative: unconfigured routes are recorded
as unavailable, and no source is represented by synthetic market facts.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from edition_profiles import resolve_edition_context
from security import get_secret, validate_url


ROOT = Path(__file__).resolve().parent
SOURCE_POLICY_PATH = ROOT / "config" / "source_policy.json"
_CANARY_FAULTS_INJECTED: set[str] = set()
DEFAULT_RSS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://blogs.nvidia.com/feed/",
    "https://openai.com/news/rss.xml",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
]


def _source_policy() -> dict[str, Any]:
    try:
        payload = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _material(source_type: str, item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or item.get("summary") or "").strip()
    url = str(item.get("url") or "").strip()
    source_id = hashlib.sha256(f"{source_type}|{url}|{title}".encode()).hexdigest()[:16]
    return {
        "material_id": source_id,
        "source_type": source_type,
        "source_id": source_id,
        "source_url": url,
        "title": title,
        "body": body,
        "published_at": item.get("published_at"),
        "collected_at": _now(),
        "relevance_score": 0.0,
        "credibility_level": "source_declared",
        "tickers": [],
        "topic": item.get("topic") or "unclassified",
    }


def _rss(url: str) -> list[dict[str, Any]]:
    limit = int(os.environ.get("RSS_ITEMS_PER_FEED", "8"))
    validate_url(url, consumer="source_collector", purpose="collect_sources")
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    output: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:limit]:
        def value(name: str) -> str:
            node = item.find(name)
            return (node.text or "").strip() if node is not None else ""
        output.append({"title": value("title"), "url": value("link"), "summary": value("description"), "published_at": value("pubDate")})
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", namespace)[:limit]:
        def atom_value(name: str) -> str:
            node = entry.find(f"atom:{name}", namespace)
            return (node.text or "").strip() if node is not None else ""
        link = entry.find("atom:link[@rel='alternate']", namespace)
        if link is None:
            link = entry.find("atom:link", namespace)
        output.append({
            "title": atom_value("title"),
            "url": link.get("href", "").strip() if link is not None else "",
            "summary": atom_value("summary") or atom_value("content"),
            "published_at": atom_value("updated") or atom_value("published"),
        })
    return output


def _rss_feeds() -> list[str]:
    raw = os.environ.get("RSS_FEEDS")
    if raw is None:
        return DEFAULT_RSS_FEEDS
    return [item.strip() for item in raw.split(",") if item.strip()]


def _command_text(command: list[str], timeout: int = 45) -> str:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command_failed:{command[0]}:{completed.stderr[-300:]}")
    return completed.stdout


def _agent_reach_doctor() -> dict[str, Any]:
    """Read the active Agent Reach backends once per collection run."""
    try:
        payload = json.loads(_command_text(["agent-reach", "doctor", "--json"], timeout=20))
        return payload if isinstance(payload, dict) else {}
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def _twitter_serenity(cutoff: dt.datetime | None = None) -> list[dict[str, Any]]:
    raw = _command_text(["twitter", "user-posts", "@aleabitoreddit", "-n", "50", "--json"])
    payload = json.loads(raw)
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    latest = (cutoff or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    earliest = latest - dt.timedelta(hours=float(os.environ.get("X_SOURCE_WINDOW_HOURS", "24")))
    result: list[dict[str, Any]] = []
    for entry in entries:
        created = entry.get("createdAtISO")
        try:
            created_at = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_at < earliest or created_at > latest:
            continue
        tweet_id = str(entry.get("id") or "")
        result.append({
            "title": f"@aleabitoreddit {tweet_id}",
            "body": entry.get("text") or "",
            "url": f"https://x.com/aleabitoreddit/status/{tweet_id}" if tweet_id else "",
            "published_at": created,
            "topic": "serenity_x",
        })
    return result


def _exa_search() -> list[dict[str, Any]]:
    raw = _command_text(["mcporter", "call", 'exa.web_search_exa(query: "AI semiconductor market catalyst", numResults: 5)'], timeout=90)
    result: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if line.startswith("Title: "):
            if current.get("url"):
                result.append(current)
            current = {"title": line[7:].strip(), "topic": "market_news"}
        elif line.startswith("URL: "):
            current["url"] = line[5:].strip()
        elif current and line.strip() and not line.startswith(("Published", "Author", "Highlights", "---")):
            current["body"] = f"{current.get('body', '')} {line.strip()}".strip()
    if current.get("url"):
        result.append(current)
    return result


def _jina_read(url: str) -> str:
    return _command_text(["curl", "-L", "--max-time", "30", "-sS", f"https://r.jina.ai/{url}"], timeout=40)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _filter_to_cutoff(
    materials: list[dict[str, Any]],
    cutoff: dt.datetime,
    window_start: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Keep source items at or before the edition cutoff.

    Missing timestamps are retained for traceability but counted explicitly;
    future-dated items are never allowed into the content context.
    """
    kept: list[dict[str, Any]] = []
    future_count = 0
    missing_count = 0
    stale_count = 0
    cutoff_utc = cutoff.astimezone(dt.timezone.utc)
    start_utc = window_start.astimezone(dt.timezone.utc) if window_start else None
    for item in materials:
        timestamp = _parse_timestamp(item.get("published_at"))
        if timestamp is None:
            missing_count += 1
            kept.append(item)
        elif timestamp <= cutoff_utc and (start_utc is None or timestamp >= start_utc):
            kept.append(item)
        elif timestamp < (start_utc or cutoff_utc):
            stale_count += 1
        else:
            future_count += 1
    return kept, future_count, missing_count, stale_count


def _github_cache_is_current(artifact: Path, current_run_root: Path, cutoff: dt.datetime) -> bool:
    """Allow current-run artifacts; shared cache requires explicit opt-in."""
    try:
        resolved = artifact.resolve()
        if current_run_root.resolve() in resolved.parents:
            return True
        policy = _source_policy()
        shared_cache = os.environ.get("GITHUB_SHARED_CACHE_ENABLED", str(policy.get("github_shared_cache_enabled", False))).lower() == "true"
        if not shared_cache:
            return False
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        generated_at = _parse_timestamp(payload.get("generated_at"))
        if generated_at is None or generated_at > cutoff.astimezone(dt.timezone.utc):
            return False
        max_age_hours = float(os.environ.get("GITHUB_CACHE_MAX_AGE_HOURS", policy.get("github_cache_max_age_hours", 6)))
        return (cutoff.astimezone(dt.timezone.utc) - generated_at).total_seconds() <= max_age_hours * 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _github_projects(output_dir: Path, cutoff: dt.datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the configured GitHub route, falling back to the live gh search.

    The source router must not silently downgrade a healthy GitHub backend to
    an absent local artifact.  ``github_ai_projects`` already owns the
    provider selection (token first, authenticated gh CLI second), so this
    wrapper only adapts its normalized output into the shared material
    contract and keeps the raw project artifact isolated to this run.
    """
    artifact = Path(os.environ.get("GITHUB_OUTPUT_DIR", str(ROOT / "outputs" / "github_ai_projects"))) / "ai_open_source_projects.json"
    try:
        if artifact.exists() and _github_cache_is_current(artifact, output_dir.parent, cutoff):
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            selected = payload.get("selected", []) if isinstance(payload, dict) else []
            if selected:
                return selected, {"status": "healthy", "backend": "artifact", "count": len(selected), "cache_hit": True}

        from github_ai_projects import fetch_repositories, write_outputs

        github_secret = get_secret("GITHUB_TOKEN", consumer="source_collector", purpose="collect_sources", run_id="source-router")
        projects = fetch_repositories(github_secret.reveal("collect_sources") if github_secret else "")
        github_dir = output_dir / "github_ai_projects"
        write_outputs(projects, github_dir)
        selected = projects[:3]
        return selected, {"status": "healthy" if selected else "partial", "backend": "gh CLI" if github_secret is None else "GitHub REST API", "count": len(selected), "cache_hit": False}
    except Exception as exc:  # route failure is recorded, never fabricated
        return [], {"status": "failed", "error_type": type(exc).__name__, "message": str(exc)[:300]}


def _dedupe(materials: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for item in materials:
        duplicate = None
        for previous in accepted:
            same_url = item["source_url"] and item["source_url"] == previous["source_url"]
            title_score = difflib.SequenceMatcher(None, item["title"].lower(), previous["title"].lower()).ratio()
            body_score = difflib.SequenceMatcher(None, item["body"].lower(), previous["body"].lower()).ratio()
            if same_url or title_score >= 0.92 or body_score >= 0.95:
                duplicate = previous
                break
        if duplicate:
            filtered.append({"material": item, "reason": "duplicate_url_or_similarity", "duplicate_of": duplicate["material_id"]})
        else:
            accepted.append(item)
    return accepted, filtered


def collect(output_dir: Path, edition: str | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    if os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() == "true":
        fault = os.environ.get("SELF_HEALING_FAULT", "none").strip()
        if fault in {"collector_timeout", "collector_http_503"} and fault not in _CANARY_FAULTS_INJECTED:
            _CANARY_FAULTS_INJECTED.add(fault)
            raise TimeoutError("collector timeout (controlled Canary fault injection)") if fault == "collector_timeout" else RuntimeError("HTTP 503 (controlled Canary fault injection)")
    statuses: dict[str, dict[str, Any]] = {}
    materials: list[dict[str, Any]] = []
    edition_context = resolve_edition_context(edition) if edition else None
    cutoff = edition_context.scheduled_cutoff if edition_context else dt.datetime.now(dt.timezone.utc)
    window_start = edition_context.source_window_start if edition_context else None
    doctor = _agent_reach_doctor()
    route_backend = {
        "x": (doctor.get("twitter") or {}).get("active_backend"),
        "exa": (doctor.get("exa_search") or {}).get("active_backend"),
        "jina": (doctor.get("web") or {}).get("active_backend"),
        "github": (doctor.get("github") or {}).get("active_backend"),
        "rss": (doctor.get("rss") or {}).get("active_backend"),
    }
    live = os.environ.get("SOURCE_ROUTER_LIVE", "true").lower() == "true"
    selected_sources = {
        str(item).strip().lower()
        for item in (sources or ["rss", "x", "exa", "jina", "github"])
        if str(item).strip()
    }
    use_rss = bool({"rss", "rss_summary"} & selected_sources)
    use_x = "x" in selected_sources
    use_exa = "exa" in selected_sources
    use_jina = "jina" in selected_sources
    use_github = "github" in selected_sources
    feeds = _rss_feeds()
    if feeds and use_rss:
        def load_feed(feed: str) -> tuple[str, list[dict[str, Any]], Exception | None]:
            try:
                return feed, _rss(feed), None
            except Exception as exc:  # source failures stay local to the route
                return feed, [], exc

        policy = _source_policy()
        workers = max(1, min(int(os.environ.get("SOURCE_MAX_WORKERS", policy.get("max_workers", 4))), len(feeds)))
        # map() preserves feed order, keeping dedupe and output deterministic
        # while network waits happen concurrently.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-source") as pool:
            loaded = list(pool.map(load_feed, feeds))
        rss_item_count = 0
        rss_failure_count = 0
        for feed, items, failure in loaded:
            if failure is not None:
                rss_failure_count += 1
                statuses[feed] = {"source_type": "rss", "status": "failed", "error_type": type(failure).__name__, "message": str(failure)[:300]}
                continue
            rss_item_count += len(items)
            for item in items:
                materials.append(_material("rss", item))
            statuses[feed] = {"source_type": "rss", "status": "healthy", "backend": route_backend.get("rss") or "urllib", "item_count": len(items)}
        statuses["rss"] = {
            "source_type": "rss",
            "status": "healthy" if rss_failure_count == 0 else ("partial" if rss_item_count else "failed"),
            "backend": route_backend.get("rss") or "urllib",
            "feed_count": len(feeds),
            "failed_feed_count": rss_failure_count,
            "item_count": rss_item_count,
        }
    else:
        statuses["rss"] = {
            "source_type": "rss",
            "status": "unavailable" if use_rss else "not_selected",
            "reason": "RSS_FEEDS_not_configured" if use_rss else "route_not_selected",
        }

    live_loaders: list[tuple[str, Any]] = []
    if use_x:
        live_loaders.append(("x", lambda: _twitter_serenity(cutoff)))
    if use_exa:
        live_loaders.append(("exa", _exa_search))
    if live and live_loaders:
        def load_live(source_type: str, loader: Any) -> tuple[str, list[dict[str, Any]], Exception | None]:
            try:
                return source_type, loader(), None
            except Exception as exc:
                return source_type, [], exc

        with ThreadPoolExecutor(max_workers=min(2, len(live_loaders)), thread_name_prefix="market-live-source") as pool:
            loaded = list(pool.map(lambda item: load_live(*item), live_loaders))
        for source_type, items, failure in loaded:
            if failure is not None:
                statuses[source_type] = {"source_type": source_type, "status": "failed", "error_type": type(failure).__name__, "message": str(failure)[:300]}
                continue
            for item in items:
                materials.append(_material(source_type, item))
            statuses[source_type] = {"source_type": source_type, "status": "healthy", "backend": route_backend.get(source_type), "item_count": len(items)}
    else:
        status_reason = "SOURCE_ROUTER_LIVE=false" if live_loaders else "route_not_selected"
        status_value = "unavailable" if live_loaders else "not_selected"
        statuses["x"] = {"source_type": "x", "status": status_value, "reason": status_reason, "backend": route_backend.get("x")}
        statuses["exa"] = {"source_type": "exa", "status": status_value, "reason": status_reason, "backend": route_backend.get("exa")}
    materials, future_count, missing_timestamp_count, stale_count = _filter_to_cutoff(materials, cutoff, window_start)
    if use_jina and live and os.environ.get("JINA_ENRICH", "true").lower() == "true":
        jina_count = 0
        jina_error = None
        for item in materials[:5]:
            url = str(item.get("source_url") or "")
            if not url:
                continue
            try:
                body = _jina_read(url)
                if body.strip():
                    materials.append(_material("jina", {"title": item.get("title"), "body": body[:12000], "url": url, "topic": item.get("topic"), "published_at": item.get("published_at")}))
                    jina_count += 1
            except Exception as exc:
                jina_error = f"{type(exc).__name__}: {exc}"[:300]
        statuses["jina"] = {"source_type": "jina", "status": "healthy" if jina_count else "partial", "count": jina_count, "error": jina_error, "backend": route_backend.get("jina") or "Jina Reader"}
    else:
        statuses["jina"] = {
            "source_type": "jina",
            "status": "unavailable" if use_jina else "not_selected",
            "reason": "JINA_ENRICH=false" if use_jina else "route_not_selected",
            "backend": route_backend.get("jina"),
        }
    if live and use_github:
        projects, github_status = _github_projects(output_dir, cutoff)
        for item in projects:
            materials.append(_material("github", {"title": item.get("full_name"), "body": item.get("description"), "url": item.get("html_url"), "topic": "ai_open_source"}))
        statuses["github"] = {"source_type": "github", **github_status, "backend": github_status.get("backend") or route_backend.get("github")}
    else:
        statuses["github"] = {
            "source_type": "github",
            "status": "unavailable" if use_github else "not_selected",
            "reason": "SOURCE_ROUTER_LIVE=false" if use_github else "route_not_selected",
            "backend": route_backend.get("github"),
        }

    accepted, filtered = _dedupe(materials)
    _write(output_dir / "normalized_materials.json", accepted)
    _write(output_dir / "filtered_materials.json", filtered)
    _write(output_dir / "source_status.json", {"collected_at": _now(), "edition": edition, "data_cutoff": cutoff.isoformat(), "source_window_start": window_start.isoformat() if window_start else None, "selected_sources": sorted(selected_sources), "sources": statuses, "agent_reach_doctor": {name: {"status": item.get("status"), "active_backend": item.get("active_backend")} for name, item in doctor.items() if isinstance(item, dict)}, "source_count": len(accepted), "filtered_count": len(filtered), "future_items_discarded": future_count, "stale_items_discarded": stale_count, "missing_timestamp_count": missing_timestamp_count})
    return {"source_count": len(accepted), "filtered_count": len(filtered), "sources": statuses, "data_cutoff": cutoff.isoformat(), "future_items_discarded": future_count, "stale_items_discarded": stale_count}


def main() -> int:
    output = Path(os.environ.get("MARKET_SOURCE_OUTPUT_DIR", str(ROOT / "outputs" / "market_sources"))).expanduser().resolve()
    result = collect(output, os.environ.get("MARKET_EDITION"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
