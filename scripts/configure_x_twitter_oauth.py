#!/usr/bin/env python3
"""Store X OAuth 1.0a user-context credentials in macOS Keychain.

The values are entered through hidden prompts and are never written to files,
environment variables, command arguments, stdout, or logs.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys


SERVICE = "ara.daily-market-content"
CREDENTIALS = (
    ("x_consumer_key", "X Consumer Key"),
    ("x_consumer_secret", "X Consumer Secret"),
    ("x_publish_access_token", "X Access Token"),
    ("x_publish_access_token_secret", "X Access Token Secret"),
)


def store(account: str, prompt: str, value: str) -> None:
    completed = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w"],
        input=f"{value}\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"keychain_store_failed:{account}")


def main() -> int:
    if sys.platform != "darwin" or shutil.which("security") is None:
        print("This command requires macOS Keychain.", file=sys.stderr)
        return 2
    for account, label in CREDENTIALS:
        value = getpass.getpass(f"{label}: ").strip()
        if not value:
            print(f"Missing value: {label}", file=sys.stderr)
            return 2
        store(account, label, value)
    print("X OAuth 1.0a credentials stored in macOS Keychain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
