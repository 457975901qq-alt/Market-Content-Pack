#!/usr/bin/env python3
"""Send the latest generated market content image to Telegram."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
PRIMARY_OUTPUT_DIR = ROOT / "outputs" / "market_content"
OUTPUT_DIR = ROOT / "outputs"
LOG_FILE = ROOT / "logs" / "telegram_send.log"
TOKYO = ZoneInfo("Asia/Tokyo")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def now_tokyo() -> str:
    return dt.datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S Asia/Tokyo")


def log_event(status: str, image_path: str = "", reason: str = "") -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "time": now_tokyo(),
        "image_path": image_path,
        "status": status,
        "reason": reason,
    }
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def find_latest_image() -> Path | None:
    candidates: list[Path] = []
    search_roots = [PRIMARY_OUTPUT_DIR, OUTPUT_DIR]
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                candidates.append(path)
        if candidates:
            break
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def require_env() -> tuple[str, str]:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")
    return token, chat_id


def send_photo(token: str, chat_id: str, image_path: Path) -> dict[str, Any]:
    boundary = "----MarketContentPackBoundary"
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    fields = {
        "chat_id": chat_id,
        "caption": f"每日市场内容包\n{now_tokyo()}",
    }

    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="photo"; filename="{image_path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    body.extend(image_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    image_path = find_latest_image()
    if image_path is None:
        reason = "no image found under outputs/market_content or outputs"
        log_event("failure", "", reason)
        print(reason, file=sys.stderr)
        return 1

    try:
        token, chat_id = require_env()
    except RuntimeError as exc:
        log_event("failure", str(image_path), str(exc))
        print(str(exc), file=sys.stderr)
        return 1

    try:
        result = send_photo(token, chat_id, image_path)
    except urllib.error.HTTPError as exc:
        reason = exc.read().decode("utf-8", errors="replace")
        log_event("failure", str(image_path), reason)
        print(f"Telegram send failed: {reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log_event("failure", str(image_path), str(exc))
        print(f"Telegram send failed: {exc}", file=sys.stderr)
        return 1

    if not result.get("ok"):
        reason = json.dumps(result, ensure_ascii=False)
        log_event("failure", str(image_path), reason)
        print(f"Telegram send failed: {reason}", file=sys.stderr)
        return 1

    log_event("success", str(image_path), "")
    print(f"Telegram image sent: {image_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
