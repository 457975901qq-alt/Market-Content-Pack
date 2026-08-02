import json
import os
import time
import tomllib
from pathlib import Path


path = Path(os.environ.get("CODEX_AUTOMATION_FILE", "/Users/ara/.codex/automations/automation/automation.toml")).expanduser()
data = tomllib.loads(path.read_text())

data["kind"] = "cron"
data["status"] = "ACTIVE"
data["model"] = data.get("model", "gpt-5")
data["reasoning_effort"] = data.get("reasoning_effort", "high")
data["execution_environment"] = data.get("execution_environment", "local")
project_root = str(Path(__file__).resolve().parent)
data["cwds"] = [project_root]
data["updated_at"] = int(time.time() * 1000)

order = [
    "version",
    "id",
    "kind",
    "name",
    "prompt",
    "status",
    "rrule",
    "model",
    "reasoning_effort",
    "execution_environment",
    "cwds",
    "target_thread_id",
    "created_at",
    "updated_at",
]

lines = []
for key in order:
    value = data[key]
    lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")

path.write_text("\n".join(lines) + "\n")
print("fixed")
