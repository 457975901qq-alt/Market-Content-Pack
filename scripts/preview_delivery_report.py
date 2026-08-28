#!/usr/bin/env python3
"""Generate and serve a delivery report without running the market pipeline."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from delivery_report import render_delivery_report, render_delivery_report_html


def _port_available(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.15):
                return False
        except OSError:
            pass
    sockets = []
    try:
        for family, address in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port))):
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(address)
            sockets.append(sock)
        return True
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_status(root: Path, manifest: dict) -> dict:
    return {
        "run_id": manifest.get("run_id"),
        "qa_status": manifest.get("qa_status"),
        "delivered": manifest.get("delivered", False),
        "image_generation_enabled": manifest.get("image_generation_enabled", manifest.get("mode") == "image"),
        "external_publish_enabled": manifest.get("external_publish_enabled", manifest.get("external_publish") not in {None, "removed", "disabled", "off"}),
        "output_root": str(root.resolve()),
        "content_path": str((root / "market_content/market_content.json").resolve()),
        "manifest_path": str((root / "logs/run_manifest.json").resolve()),
        "log_path": str((root / "logs/market_content_errors.log").resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview a saved delivery report only")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()

    project_root = PROJECT_ROOT
    run_root = project_root / "outputs" / "runs" / args.run_id
    content_path = run_root / "market_content" / "market_content.json"
    manifest_path = run_root / "logs" / "run_manifest.json"
    if not content_path.exists() or not manifest_path.exists():
        print(f"run_not_found:{run_root}", file=sys.stderr)
        return 2
    if not _port_available(args.port):
        print(f"port_in_use:{args.port}; choose another --port", file=sys.stderr)
        return 2

    content = _read_json(content_path)
    manifest = _read_json(manifest_path)
    status = _build_status(run_root, manifest)
    delivery_dir = run_root / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = delivery_dir / "delivery_report_latest.md"
    html_path = delivery_dir / "delivery_report_latest.html"
    markdown_path.write_text(render_delivery_report(content, manifest, status, rich_text=False), encoding="utf-8")
    html_path.write_text(render_delivery_report_html(content, manifest, status), encoding="utf-8")

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.port), "--directory", str(delivery_dir)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.25)
    if server.poll() is not None:
        print(f"server_start_failed:{args.port}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{args.port}/delivery_report_latest.html"
    if args.open_browser:
        webbrowser.open(url)
    print(f"html_path={html_path.resolve()}")
    print(f"markdown_path={markdown_path.resolve()}")
    print(f"url={url}")
    print(f"server_pid={server.pid}")
    print(f"stop=kill {server.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
