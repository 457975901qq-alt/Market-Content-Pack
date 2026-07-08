import json
import time
import tomllib
from pathlib import Path


path = Path("/Users/ara/.codex/automations/automation/automation.toml")
data = tomllib.loads(path.read_text())

data["kind"] = "cron"
data["status"] = "ACTIVE"
data["model"] = data.get("model", "gpt-5")
data["reasoning_effort"] = data.get("reasoning_effort", "high")
data["execution_environment"] = data.get("execution_environment", "local")
data["cwds"] = data.get("cwds", ["/Users/ara/Documents/新闻搜索"])
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
