#!/usr/bin/env python3
"""Local Obsidian vault adapter.

Obsidian stores notes as plain Markdown files. This adapter keeps the
integration local and file-based: it checks the configured vault and can write
small notes into a Codex-owned folder inside that vault.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
TOKYO = ZoneInfo("Asia/Tokyo")


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def vault_path() -> Path:
    return Path(os.environ.get("OBSIDIAN_VAULT_PATH", "~/Documents/Ara-Knowledge")).expanduser().resolve()


def inbox_dir(vault: Path | None = None) -> Path:
    vault = vault or vault_path()
    folder = os.environ.get("OBSIDIAN_CODEX_FOLDER", "Codex")
    return vault / folder


def health_check() -> dict[str, Any]:
    vault = vault_path()
    obsidian_config = vault / ".obsidian"
    notes = sorted(vault.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True) if vault.exists() else []
    return {
        "service": "obsidian",
        "status": "healthy" if vault.exists() and obsidian_config.exists() else "unhealthy",
        "vault_path": str(vault),
        "vault_exists": vault.exists(),
        "obsidian_config_exists": obsidian_config.exists(),
        "codex_folder": str(inbox_dir(vault)),
        "note_count": len(notes),
        "recent_notes": [str(path.relative_to(vault)) for path in notes[:10]] if vault.exists() else [],
        "checked_at": datetime.now(TOKYO).isoformat(),
    }


def write_note(title: str, body: str) -> Path:
    vault = vault_path()
    if not vault.exists() or not (vault / ".obsidian").exists():
        raise RuntimeError(f"obsidian_vault_not_ready:{vault}")
    target_dir = inbox_dir(vault)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(ch for ch in title if ch not in "/:").strip() or "Codex Note"
    date_token = datetime.now(TOKYO).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"{date_token}-{safe_title}.md"
    content = (
        "---\n"
        "source: codex\n"
        f"created: {datetime.now(TOKYO).isoformat()}\n"
        "---\n\n"
        f"# {safe_title}\n\n"
        f"{body.rstrip()}\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Obsidian local vault adapter")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--write-test", action="store_true")
    parser.add_argument("--title", default="Codex Obsidian 连接测试")
    parser.add_argument("--body", default="这是一条由 Codex 写入 Obsidian vault 的连接测试笔记。")
    args = parser.parse_args(argv)
    if args.health:
        print(json.dumps(health_check(), ensure_ascii=False, indent=2))
        return 0
    if args.write_test:
        path = write_note(args.title, args.body)
        print(json.dumps({"status": "success", "path": str(path)}, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
