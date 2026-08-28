#!/usr/bin/env python3
"""Fetch and rank AI-related GitHub repositories for the daily market pack."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "github_ai_projects"
ERROR_LOG = ROOT / "logs" / "error.log"
KEYWORDS = ["AI Agent", "LLM", "MCP", "RAG", "workflow automation"]
PER_KEYWORD = 5
FINAL_LIMIT = 3
API_URL = "https://api.github.com/search/repositories"


def log_error(message: str, exc: BaseException | None = None) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with ERROR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{now}] {message}\n")
        if exc is not None:
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            fh.write("\n")


def github_request(keyword: str, token: str) -> dict[str, Any]:
    query = f'{keyword} in:name,description,topics archived:false'
    params = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": str(PER_KEYWORD),
        }
    )
    req = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "daily-market-pack-github-source",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_repo(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    description = (item.get("description") or "").strip()
    if not description:
        return None
    return {
        "name": item.get("name") or "",
        "full_name": item.get("full_name") or "",
        "html_url": item.get("html_url") or "",
        "description": description,
        "stargazers_count": int(item.get("stargazers_count") or 0),
        "forks_count": int(item.get("forks_count") or 0),
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "updated_at": item.get("updated_at") or "",
        "matched_keyword": keyword,
    }


def repo_score(repo: dict[str, Any]) -> float:
    updated_at = repo.get("updated_at") or ""
    recency_bonus = 0.0
    try:
        updated = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age_days = max((dt.datetime.now(dt.timezone.utc) - updated).days, 0)
        recency_bonus = max(0, 30 - min(age_days, 30)) * 3
    except ValueError:
        pass

    topic_text = " ".join(repo.get("topics") or []).lower()
    keyword_bonus = 0
    for term in ["agent", "llm", "mcp", "rag", "workflow", "automation", "ai"]:
        if term in topic_text or term in repo["description"].lower() or term in repo["full_name"].lower():
            keyword_bonus += 25

    return (
        repo["stargazers_count"] * 1.0
        + repo["forks_count"] * 2.0
        + recency_bonus
        + keyword_bonus
    )


def fetch_repositories(token: str) -> list[dict[str, Any]]:
    repos: dict[str, dict[str, Any]] = {}
    for keyword in KEYWORDS:
        try:
            data = github_request(keyword, token)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            log_error(f"GitHub search failed for keyword={keyword!r}", exc)
            continue

        for item in data.get("items", []):
            repo = normalize_repo(item, keyword)
            if repo is None:
                continue
            full_name = repo["full_name"]
            existing = repos.get(full_name)
            if existing is None:
                repo["matched_keywords"] = [keyword]
                repos[full_name] = repo
            elif keyword not in existing["matched_keywords"]:
                existing["matched_keywords"].append(keyword)

    ranked = sorted(repos.values(), key=repo_score, reverse=True)
    return ranked


def write_outputs(repos: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = repos[:FINAL_LIMIT]
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "GitHub REST API search repositories",
        "keywords": KEYWORDS,
        "per_keyword": PER_KEYWORD,
        "selected_count": len(selected),
        "selected": selected,
    }

    json_path = output_dir / "ai_open_source_projects.json"
    md_path = output_dir / "ai_open_source_projects.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# AI开源项目",
        "",
        f"- 数据源：GitHub REST API search repositories",
        f"- 生成时间：{payload['generated_at']}",
        f"- 关键词：{', '.join(KEYWORDS)}",
        "",
    ]
    for i, repo in enumerate(selected, 1):
        topics = ", ".join(repo.get("topics") or []) or "无"
        language = repo.get("language") or "Unknown"
        lines.extend(
            [
                f"## {i}. [{repo['full_name']}]({repo['html_url']})",
                "",
                f"- 描述：{repo['description']}",
                f"- Stars/Forks：{repo['stargazers_count']} / {repo['forks_count']}",
                f"- 语言：{language}",
                f"- Topics：{topics}",
                f"- Updated：{repo['updated_at']}",
                f"- 匹配关键词：{', '.join(repo.get('matched_keywords') or [repo.get('matched_keyword', '')])}",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch GitHub AI repositories for the daily market pack.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        msg = "GITHUB_TOKEN is not set; cannot call GitHub REST API with authentication."
        log_error(msg)
        print(msg, file=sys.stderr)
        return 2

    try:
        repos = fetch_repositories(token)
        json_path, md_path = write_outputs(repos, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - command should log unexpected failures.
        log_error("Unexpected GitHub AI project fetch failure", exc)
        print(f"GitHub AI project fetch failed; see {ERROR_LOG}", file=sys.stderr)
        return 1

    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
