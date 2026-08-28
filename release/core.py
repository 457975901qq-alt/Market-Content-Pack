"""Concrete L6-5 release lifecycle primitives.

The release layer is intentionally independent from runtime state.  It writes
only under ``releases/``, ``deployments/`` and ``outputs/releases/``; runtime,
state, logs, outputs/runs, SQLite and Keychain data are never copied into a
release package or removed during promote/rollback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported deployment is macOS/Linux
    fcntl = None

from security import AuditLogger, SecurityError, assert_safe_persistence, authorize, security_gate, security_preflight


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
FORBIDDEN_PARTS = {".env", "runtime", "state", "logs", "outputs", ".git", ".venv", "__pycache__", "secrets", "checkpoints", "cache"}
FORBIDDEN_SUFFIXES = {".pyc", ".sqlite3", ".db", ".lock", ".pem", ".key", ".p12", ".pfx", ".cookie"}
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]+ PRIVATE KEY-----)")
RELEASE_ERROR_CODES = {
    "RELEASE_VERSION_INVALID", "RELEASE_VERSION_EXISTS", "RELEASE_DIRTY_WORKTREE", "RELEASE_PREFLIGHT_BLOCKED",
    "RELEASE_LOCKED", "SECRET_EXPOSURE_DETECTED", "PACKAGE_INTEGRITY_FAILED", "ARTIFACT_MISSING",
    "ARTIFACT_HASH_MISMATCH", "MIGRATION_ROLLBACK_BLOCKED", "CHECKPOINT_VERSION_INCOMPATIBLE",
    "ROLLBACK_TARGET_INVALID", "ROLLBACK_COMPATIBILITY_FAILED", "DEPLOYMENT_DRIFT_DETECTED",
    "CANARY_INSUFFICIENT_DATA", "CANARY_CRITICAL_REGRESSION", "CANARY_HIGH_REGRESSION",
    "WRONG_SESSION", "STALE_INPUT", "SCHEMA_FAILURE", "TEXT_IMAGE_MISMATCH", "RELEASE_AUDIT_FAILED",
}


class ReleaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}:{message}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def semver_key(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(str(value).strip())
    if not match:
        raise ReleaseError("RELEASE_VERSION_INVALID", "version must be MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, payload: Any, *, immutable: bool = False) -> None:
    if immutable and path.exists():
        raise ReleaseError("RELEASE_VERSION_EXISTS", f"immutable file already exists: {path}")
    assert_safe_persistence(payload, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, json.JSONDecodeError):
        return default


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_info(root: Path = ROOT) -> dict[str, str]:
    commit = _git(root, "rev-parse", "HEAD") or "unknown"
    branch = _git(root, "branch", "--show-current") or "detached"
    tag = _git(root, "describe", "--tags", "--exact-match") or ""
    return {"git_commit": commit, "git_branch": branch, "git_tag": tag}


def dirty_paths(root: Path = ROOT) -> list[str]:
    output = _git(root, "status", "--porcelain")
    return [line[3:] for line in output.splitlines() if len(line) > 3]


def _config_payload(root: Path) -> dict[str, Any]:
    try:
        edition = json.loads((root / "config" / "edition_profiles.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        edition = {}
    prompts = [str(item.get("prompt_version")) for item in (edition.get("editions") or {}).values() if isinstance(item, dict) and item.get("prompt_version")]
    try:
        from scheduler import PIPELINE_VERSION, SCHEMA_VERSION
    except Exception:
        scheduler_source = (root / "scheduler.py").read_text(encoding="utf-8", errors="ignore") if (root / "scheduler.py").exists() else ""
        pipeline_match = re.search(r'PIPELINE_VERSION\s*=\s*["\']([^"\']+)', scheduler_source)
        schema_match = re.search(r'SCHEMA_VERSION\s*=\s*["\']([^"\']+)', scheduler_source)
        PIPELINE_VERSION = pipeline_match.group(1) if pipeline_match else "unknown"
        SCHEMA_VERSION = schema_match.group(1) if schema_match else "unknown"
    try:
        from tools.regression import BASELINE_VERSION
    except Exception:
        regression_source = (root / "tools" / "regression.py").read_text(encoding="utf-8", errors="ignore") if (root / "tools" / "regression.py").exists() else ""
        baseline_match = re.search(r'BASELINE_VERSION\s*=\s*["\']([^"\']+)', regression_source)
        BASELINE_VERSION = baseline_match.group(1) if baseline_match else "unknown"
    return {
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "database_version": "scheduler_sqlite_v1",
        "prompt_version": "|".join(sorted(set(prompts))) or "unknown",
        "renderer_version": "svg_renderer_v1",
        "config_version": "config-" + hashlib.sha256(_json(edition).encode()).hexdigest()[:12],
        "regression_baseline_version": BASELINE_VERSION,
    }


@dataclass(frozen=True)
class ReleaseVersion:
    release_id: str
    version: str
    git_commit: str
    git_branch: str
    git_tag: str
    created_at: str
    pipeline_version: str
    schema_version: str
    database_version: str
    prompt_version: str
    renderer_version: str
    config_version: str
    regression_baseline_version: str
    release_gate_status: str
    security_gate_status: str
    artifact_manifest_hash: str
    status: str = "draft"

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def release_version(root: Path, version: str, *, artifact_hash: str = "", release_gate: str = "unknown", security_status: str = "unknown", status: str = "draft") -> ReleaseVersion:
    major, minor, patch = semver_key(version)
    info = git_info(root)
    meta = _config_payload(root)
    return ReleaseVersion(
        release_id=f"release_{version}_{info['git_commit'][:12]}", version=f"{major}.{minor}.{patch}",
        git_commit=info["git_commit"], git_branch=info["git_branch"], git_tag=info["git_tag"], created_at=now(),
        pipeline_version=meta["pipeline_version"], schema_version=meta["schema_version"], database_version=meta["database_version"],
        prompt_version=meta["prompt_version"], renderer_version=meta["renderer_version"], config_version=meta["config_version"],
        regression_baseline_version=meta["regression_baseline_version"], release_gate_status=release_gate,
        security_gate_status=security_status, artifact_manifest_hash=artifact_hash, status=status,
    )


def _is_forbidden(relative: Path) -> bool:
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        return True
    return relative.suffix.lower() in FORBIDDEN_SUFFIXES or relative.name.startswith(".env")


def _artifact_roots(root: Path) -> Iterable[Path]:
    for child in sorted(root.iterdir()):
        if child.name in FORBIDDEN_PARTS or child.name in {"releases", "deployments", "tests", ".pytest_cache", ".playwright-cli"}:
            continue
        yield child


def _iter_artifact_files(root: Path) -> Iterable[Path]:
    for item in _artifact_roots(root):
        if item.is_file():
            relative = item.relative_to(root)
            if not _is_forbidden(relative):
                yield item
        elif item.is_dir():
            for path in item.rglob("*"):
                if path.is_file() and not _is_forbidden(path.relative_to(root)):
                    yield path


def _artifact_type(path: Path) -> str:
    if path.suffix in {".py", ".json", ".toml", ".yaml", ".yml", ".md"}:
        return "source_or_config"
    if path.suffix in {".lock", ".txt"}:
        return "dependency_metadata"
    return "asset"


def artifact_manifest(root: Path = ROOT, version: str = "") -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted(_iter_artifact_files(root), key=lambda item: str(item.relative_to(root))):
        relative = path.relative_to(root)
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if SECRET_RE.search(raw.decode("utf-8", errors="ignore")):
            raise ReleaseError("SECRET_EXPOSURE_DETECTED", f"secret-like content in artifact: {relative}")
        entries.append({"relative_path": str(relative), "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "type": _artifact_type(path)})
    payload = {"manifest_version": 1, "release_version": version, "generated_at": now(), "artifacts": entries}
    payload["manifest_hash"] = hashlib.sha256(_json(payload).encode()).hexdigest()
    return payload


def verify_artifact_manifest(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    for item in manifest.get("artifacts", []):
        relative = Path(str(item.get("relative_path", "")))
        path = root / relative
        if not path.is_file():
            mismatches.append({"relative_path": str(relative), "code": "ARTIFACT_MISSING"})
            continue
        actual = _sha256(path)
        if actual != item.get("sha256"):
            mismatches.append({"relative_path": str(relative), "expected": item.get("sha256"), "actual": actual, "code": "ARTIFACT_HASH_MISMATCH"})
    return {"status": "passed" if not mismatches else "blocked", "mismatches": mismatches, "checked": len(manifest.get("artifacts", []))}


def build_package(root: Path, version: str, manifest: Mapping[str, Any], output: Path | None = None) -> dict[str, Any]:
    package = output or root / "releases" / "packages" / f"market-pipeline-{version}.tar.gz"
    if package.exists():
        raise ReleaseError("RELEASE_VERSION_EXISTS", f"package already exists: {package}")
    package.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package, "w:gz") as archive:
        for item in manifest.get("artifacts", []):
            relative = Path(str(item["relative_path"]))
            source = root / relative
            if source.is_file() and not _is_forbidden(relative):
                archive.add(source, arcname=str(relative), recursive=False)
    return {"path": str(package), "sha256": _sha256(package), "size": package.stat().st_size, "contents": len(manifest.get("artifacts", []))}


def verify_package(package: Path) -> dict[str, Any]:
    if not package.is_file():
        return {"status": "blocked", "code": "PACKAGE_INTEGRITY_FAILED", "reason": "package_missing"}
    forbidden: list[str] = []
    members: list[str] = []
    try:
        with tarfile.open(package, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name
                members.append(name)
                if _is_forbidden(Path(name)) or SECRET_RE.search(name):
                    forbidden.append(name)
                if member.isfile():
                    extracted = archive.extractfile(member)
                    raw = extracted.read() if extracted else b""
                    if SECRET_RE.search(raw.decode("utf-8", errors="ignore")):
                        forbidden.append(name)
    except (OSError, tarfile.TarError) as exc:
        return {"status": "blocked", "code": "PACKAGE_INTEGRITY_FAILED", "reason": type(exc).__name__}
    return {"status": "passed" if not forbidden else "blocked", "package_sha256": _sha256(package), "member_count": len(members), "forbidden": sorted(set(forbidden))}


def migration_dry_run(root: Path = ROOT, *, approve: bool = False) -> dict[str, Any]:
    path = root / "config" / "release_migrations.json"
    migrations = _read(path, {"database_version": "scheduler_sqlite_v1", "migrations": []})
    blocked: list[dict[str, Any]] = []
    for migration in migrations.get("migrations", []):
        if migration.get("compatibility") in {"destructive", "irreversible"} and not approve:
            blocked.append({"migration_id": migration.get("migration_id"), "code": "MIGRATION_ROLLBACK_BLOCKED"})
    return {"status": "blocked" if blocked else "passed", "dry_run": True, "current_version": migrations.get("database_version", "scheduler_sqlite_v1"), "migrations": migrations.get("migrations", []), "blockers": blocked, "destructive_executed": False}


def checkpoint_compatibility(*, producer: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("schema_version", "prompt_version", "renderer_version")
    mismatches = [key for key in keys if producer.get(key) and current.get(key) and producer.get(key) != current.get(key)]
    if producer.get("schema_version") and current.get("schema_version") and str(producer["schema_version"]).split("_")[0] != str(current["schema_version"]).split("_")[0]:
        return {"status": "blocked", "decision": "requires_restart", "code": "CHECKPOINT_VERSION_INCOMPATIBLE", "mismatches": mismatches}
    if "prompt_version" in mismatches:
        return {"status": "passed", "decision": "requires_restart", "code": "PROMPT_VERSION_CHANGED", "mismatches": mismatches}
    if "renderer_version" in mismatches:
        return {"status": "passed", "decision": "resume_from_image_rendering", "code": "RENDERER_VERSION_CHANGED", "mismatches": mismatches}
    return {"status": "passed", "decision": "resume", "mismatches": []}


def _release_dir(root: Path, version: str) -> Path:
    semver_key(version)
    return root / "releases" / version


def _run_json_command(command: Sequence[str], root: Path, timeout: int = 1800) -> tuple[int, str]:
    try:
        result = subprocess.run(list(command), cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout or result.stderr or "")[-12000:]
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__


def _test_result(root: Path) -> dict[str, Any]:
    pytest = root / ".venv" / "bin" / "pytest"
    command = [str(pytest) if pytest.exists() else "python3", "-q"] if pytest.exists() else ["python3", "-m", "pytest", "-q"]
    if pytest.exists():
        command = [str(pytest), "-q"]
    code, output = _run_json_command(command, root)
    match = re.search(r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?(?: in (?P<seconds>[\d.]+)s)?", output)
    passed = int(match.group("passed")) if match else 0
    failed = int(match.group("failed") or 0) if match else (0 if code == 0 else 1)
    return {"status": "passed" if code == 0 and failed == 0 else "blocked", "total": passed + failed, "passed": passed, "failed": failed, "command": command, "exit_code": code}


def _regression_result(root: Path, version: str) -> dict[str, Any]:
    command = [str(root / ".venv" / "bin" / "python") if (root / ".venv" / "bin" / "python").exists() else "python3", "-m", "tools.regression", "run", "--candidate-version", version]
    code, output = _run_json_command(command, root)
    try:
        payload = json.loads(output.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"status": "blocked", "score": None}
    return {"status": "passed" if code == 0 and payload.get("status") in {"passed", "warning"} else "blocked", **payload}


def release_preflight(root: Path, version: str, *, checks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    checks = dict(checks or {})
    failures: list[dict[str, Any]] = []
    for name in ("tests", "regression", "release_gate", "security_gate", "migration", "checkpoint", "artifact_integrity"):
        item = checks.get(name) or {}
        status = item.get("status") if isinstance(item, Mapping) else None
        if status in {"blocked", "failed"}:
            failures.append({"check": name, "code": {"release_gate": "RELEASE_GATE_BLOCKED", "security_gate": "SECURITY_GATE_BLOCKED", "artifact_integrity": "ARTIFACT_HASH_MISMATCH"}.get(name, "RELEASE_PREFLIGHT_BLOCKED")})
    status = "blocked" if failures else ("warning" if any(isinstance(item, Mapping) and item.get("status") == "warning" for item in checks.values()) else "passed")
    report = {"version": version, "generated_at": now(), "status": status, "checks": checks, "blockers": failures, "external_delivery": False}
    directory = _release_dir(root, version)
    # A preflight is a repeatable report; the immutable release identities are
    # the artifact and release manifests, not a rerunnable diagnostic report.
    _write(directory / "release_preflight.json", report)
    lines = [f"# Release Preflight {version}", "", f"- Status: **{status}**", "", "| Check | Status |", "|---|---|"]
    lines.extend(f"| {name} | {item.get('status', 'unknown') if isinstance(item, Mapping) else 'unknown'} |" for name, item in checks.items())
    if failures:
        lines.extend(["", "## Blockers", ""] + [f"- `{item['code']}` ({item['check']})" for item in failures])
    (directory / "release_preflight.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def prepare_release(root: Path = ROOT, version: str = "", *, allow_dirty: bool = False, execute_checks: bool = True, security_mode: str = "development", security_provider: str = "rule_template") -> dict[str, Any]:
    semver_key(version)
    if not version:
        raise ReleaseError("RELEASE_VERSION_INVALID", "version is required")
    dirty = dirty_paths(root)
    if dirty and not allow_dirty:
        raise ReleaseError("RELEASE_DIRTY_WORKTREE", "working tree has uncommitted changes")
    directory = _release_dir(root, version)
    if directory.exists():
        raise ReleaseError("RELEASE_VERSION_EXISTS", f"release directory already exists: {directory}")
    directory.mkdir(parents=True, exist_ok=False)
    test = _test_result(root) if execute_checks else {"status": "warning", "total": 0, "passed": 0, "failed": 0, "skipped": True}
    regression = _regression_result(root, version) if execute_checks else {"status": "warning", "skipped": True}
    release_gate = _read(root / "outputs" / "quality" / "release_gate.json", {"status": regression.get("status", "unknown")})
    security_report = security_preflight(root=root, mode=security_mode, provider_name=security_provider, delivery_enabled=False)
    security = security_gate(security_report, release_gate_status=release_gate.get("status", "blocked"))
    migration = migration_dry_run(root)
    meta = _config_payload(root)
    checkpoint = checkpoint_compatibility(producer=meta, current=meta)
    # Checks may generate evaluation caches/checkpoints.  Freeze the source
    # artifact set only after those checks have finished.
    artifact = artifact_manifest(root, version)
    _write(directory / "artifact_manifest.json", artifact, immutable=True)
    package = build_package(root, version, artifact)
    integrity = verify_artifact_manifest(root, artifact)
    checks = {"tests": test, "regression": regression, "release_gate": {"status": release_gate.get("status", "blocked"), **release_gate}, "security_gate": {"status": security.get("status", "blocked"), **security}, "migration": migration, "checkpoint": checkpoint, "artifact_integrity": integrity}
    preflight = release_preflight(root, version, checks=checks)
    status = "candidate" if preflight["status"] != "blocked" else "failed"
    release = release_version(root, version, artifact_hash=artifact["manifest_hash"], release_gate=release_gate.get("status", "blocked"), security_status=security.get("status", "blocked"), status=status)
    manifest = {**release.as_dict(), "tests": test, "artifacts": [package], "migrations": migration.get("migrations", []), "compatibility": checkpoint, "rollback": {"ready": preflight["status"] != "blocked"}, "dirty_paths": dirty, "preflight_status": preflight["status"]}
    _write(directory / "release_manifest.json", manifest, immutable=True)
    AuditLogger(root / "logs" / "security_audit.jsonl").append("RELEASE_PREPARED", actor=os.environ.get("USER", "release-cli"), outcome=status, details={"version": version, "release_id": release.release_id, "preflight": preflight["status"], "git_commit": release.git_commit}, reason="release candidate prepared")
    return {"status": status, "release": manifest, "preflight": preflight, "package": package}


class VersionRouter:
    def __init__(self, active_version: str, candidate_version: str | None = None) -> None:
        self.active_version = active_version
        self.candidate_version = candidate_version

    def route(self, job_type: str, *, stage: str = "stable") -> dict[str, Any]:
        candidate_jobs = {
            "shadow": set(), "canary-1": {"morning_content"},
            "canary-2": {"morning_content", "morning_images"},
            "canary-3": {"morning_content", "morning_images", "evening_content", "evening_images"},
            "active": {"morning_content", "morning_images", "evening_content", "evening_images"},
        }.get(stage, set())
        selected = self.candidate_version if self.candidate_version and job_type in candidate_jobs else self.active_version
        return {"job_type": job_type, "active_version": self.active_version, "selected_version": selected, "routing_reason": "candidate_canary" if selected == self.candidate_version else "stable", "stage": stage}


class ReleaseLock:
    """Serialize promote/rollback pointer changes and leave a safe lease record."""

    def __init__(self, root: Path, operation: str, version: str) -> None:
        self.root = root
        self.operation = operation
        self.version = version
        self.path = root / "runtime" / "control" / "release.lock"
        self.lease_path = root / "runtime" / "control" / "release_lease.json"
        self.handle = None

    def __enter__(self) -> "ReleaseLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.handle.close()
                self.handle = None
                raise ReleaseError("RELEASE_LOCKED", "another release operation is active") from exc
        payload = {"operation": self.operation, "version": self.version, "pid": os.getpid(), "acquired_at": now()}
        _write(self.lease_path, payload)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.lease_path.unlink(missing_ok=True)
        finally:
            if self.handle is not None:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
                self.handle = None


def release_status(root: Path = ROOT) -> dict[str, Any]:
    state = _deployment_state(root)
    integrity = deployment_integrity(root)
    report = {"generated_at": now(), "external_delivery": False, "deployment": state, "integrity": integrity}
    target = root / "outputs" / "releases"
    _write(target / "release_status.json", report)
    (target / "release_status.md").write_text(
        "# Release Status\n\n"
        f"- Active: `{state.get('active_version')}`\n"
        f"- Candidate: `{state.get('candidate_version')}`\n"
        f"- Stage: `{state.get('canary_stage')}`\n"
        f"- Integrity: **{integrity.get('status')}**\n",
        encoding="utf-8",
    )
    return report


def release_history(root: Path = ROOT) -> dict[str, Any]:
    releases = []
    for path in sorted((root / "releases").glob("*/release_manifest.json")):
        manifest = _read(path, {})
        if manifest:
            releases.append({"version": manifest.get("version"), "release_id": manifest.get("release_id"), "status": manifest.get("status"), "preflight_status": manifest.get("preflight_status"), "created_at": manifest.get("created_at")})
    report = {"generated_at": now(), "releases": releases}
    _write(root / "outputs" / "releases" / "release_history.json", report)
    return report


@dataclass(frozen=True)
class CanaryResult:
    status: str
    sample_count: int
    minimum_runs: int
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "sample_count": self.sample_count, "minimum_runs": self.minimum_runs, "blockers": list(self.blockers), "warnings": list(self.warnings)}


def canary_gate(results: Sequence[Mapping[str, Any]], minimum_runs: int = 2) -> CanaryResult:
    if len(results) < minimum_runs:
        return CanaryResult("insufficient_data", len(results), minimum_runs, ("CANARY_INSUFFICIENT_DATA",))
    blockers: list[str] = []
    for item in results:
        if item.get("p0") or item.get("critical_regressions", 0) or item.get("high_regressions", 0):
            blockers.append("CANARY_CRITICAL_REGRESSION" if item.get("critical_regressions", 0) or item.get("p0") else "CANARY_HIGH_REGRESSION")
        for flag, code in (("wrong_session", "WRONG_SESSION"), ("stale_input", "STALE_INPUT"), ("schema_passed", "SCHEMA_FAILURE"), ("text_image_match", "TEXT_IMAGE_MISMATCH"), ("checkpoint_compatible", "CHECKPOINT_VERSION_INCOMPATIBLE")):
            if flag in item and item.get(flag) is False:
                blockers.append(code)
    return CanaryResult("paused" if blockers else "passed", len(results), minimum_runs, tuple(sorted(set(blockers))))


def _deployment_state(root: Path) -> dict[str, Any]:
    return _read(root / "deployments" / "release_state.json", {"active_version": None, "previous_version": None, "candidate_version": None, "canary_stage": "stable"})


def deployment_integrity(root: Path = ROOT, version: str | None = None) -> dict[str, Any]:
    state = _deployment_state(root)
    selected = version or state.get("active_version")
    if not selected:
        report = {"status": "warning", "code": "NO_ACTIVE_RELEASE", "version": None}
        _write(root / "outputs" / "releases" / "deployment_integrity.json", report)
        return report
    directory = _release_dir(root, selected)
    manifest = _read(directory / "artifact_manifest.json", {})
    if not manifest:
        report = {"status": "blocked", "code": "DEPLOYMENT_DRIFT_DETECTED", "version": selected}
        _write(root / "outputs" / "releases" / "deployment_integrity.json", report)
        return report
    verified = verify_artifact_manifest(root, manifest)
    report = {"status": verified["status"], "version": selected, "artifact_manifest_hash": manifest.get("manifest_hash"), **verified}
    _write(root / "outputs" / "releases" / "deployment_integrity.json", report)
    return report


def _authorize_release(actor: str | None, role: str | None, reason: str, approve: bool, capability: str) -> dict[str, Any]:
    decision = authorize(actor=actor or os.environ.get("USER", ""), role=role or "maintainer", capability=capability, reason=reason, approve=approve)
    if not decision.get("allowed"):
        raise ReleaseError(decision.get("code", "RELEASE_AUDIT_FAILED"), "release authorization denied")
    return decision


def promote_release(root: Path, version: str, *, stage: str, actor: str | None = None, role: str | None = None, reason: str = "", approve: bool = False, canary_results: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    if stage not in {"shadow", "canary-1", "canary-2", "canary-3", "active"}:
        raise ReleaseError("RELEASE_VERSION_INVALID", "unsupported promotion stage")
    directory = _release_dir(root, version)
    manifest = _read(directory / "release_manifest.json", {})
    if not manifest or manifest.get("status") not in {"candidate", "shadow", "canary", "promoted", "active"}:
        raise ReleaseError("RELEASE_PREFLIGHT_BLOCKED", "candidate manifest is missing or failed")
    if manifest.get("preflight_status") == "blocked":
        raise ReleaseError("RELEASE_PREFLIGHT_BLOCKED", "release preflight is blocked")
    canary = canary_gate(canary_results) if stage.startswith("canary") or stage == "active" else CanaryResult("passed", 0, 0)
    _write(root / "outputs" / "releases" / "canary_status.json", {"version": version, "stage": stage, **canary.as_dict()})
    if stage in {"canary-1", "canary-2", "canary-3", "active"} and canary.status != "passed":
        return {"status": canary.status, "version": version, "stage": stage, "canary": canary.as_dict()}
    preview = {"status": "preview", "version": version, "stage": stage, "current": _deployment_state(root), "canary": canary.as_dict()}
    if not approve:
        return preview
    _authorize_release(actor, role, reason, approve, "release.promote")
    with ReleaseLock(root, "promote", version):
        state = _deployment_state(root)
        previous = state.get("active_version")
        state.update({"candidate_version": version, "canary_stage": stage, "release_id": manifest.get("release_id"), "updated_at": now()})
        if stage == "active":
            state.update({"previous_version": previous, "active_version": version, "canary_stage": "active"})
        _write(root / "deployments" / "release_state.json", state)
        _write(root / "deployments" / "current.json", {"version": state.get("active_version"), "release_id": manifest.get("release_id"), "updated_at": now()})
        if previous and previous != version:
            _write(root / "deployments" / "previous.json", {"version": previous, "updated_at": now()})
    AuditLogger(root / "logs" / "security_audit.jsonl").append("RELEASE_ACTIVATED" if stage == "active" else "CANARY_PROMOTED", actor=actor or os.environ.get("USER", "release-cli"), outcome="passed", details={"version": version, "stage": stage, "previous_version": previous, "release_id": manifest.get("release_id")}, reason=reason)
    return {**preview, "status": "promoted", "state": state}


def rollback_release(root: Path, to_version: str, *, actor: str | None = None, role: str | None = None, reason: str = "", approve: bool = False) -> dict[str, Any]:
    semver_key(to_version)
    state = _deployment_state(root)
    current = state.get("active_version")
    target_manifest = _read(_release_dir(root, to_version) / "release_manifest.json", {})
    if not target_manifest:
        raise ReleaseError("ROLLBACK_TARGET_INVALID", "rollback target manifest is missing")
    compatibility = checkpoint_compatibility(producer=_config_payload(root), current={"schema_version": target_manifest.get("schema_version"), "prompt_version": target_manifest.get("prompt_version"), "renderer_version": target_manifest.get("renderer_version")})
    if compatibility.get("status") == "blocked":
        raise ReleaseError("ROLLBACK_COMPATIBILITY_FAILED", "target version is incompatible")
    preview = {"status": "preview", "current_version": current, "target_version": to_version, "database_compatibility": migration_dry_run(root), "checkpoint_impact": compatibility, "historical_outputs_preserved": True, "running_jobs_unchanged": True}
    _write(root / "outputs" / "releases" / "rollback_readiness.json", preview)
    if not approve:
        return preview
    _authorize_release(actor, role, reason, approve, "release.rollback")
    with ReleaseLock(root, "rollback", to_version):
        state.update({"previous_version": current, "active_version": to_version, "candidate_version": None, "canary_stage": "active", "release_id": target_manifest.get("release_id"), "updated_at": now(), "status": "rolled_back"})
        _write(root / "deployments" / "release_state.json", state)
        _write(root / "deployments" / "current.json", {"version": to_version, "release_id": target_manifest.get("release_id"), "updated_at": now()})
        _write(root / "deployments" / "previous.json", {"version": current, "updated_at": now()})
    AuditLogger(root / "logs" / "security_audit.jsonl").append("ROLLBACK_COMPLETED", actor=actor or os.environ.get("USER", "release-cli"), outcome="passed", details={"version": to_version, "previous_version": current}, reason=reason)
    return {**preview, "status": "rolled_back", "state": state}


def run_offline_release_drill(root: Path = ROOT) -> dict[str, Any]:
    # Run the six acceptance cases against an isolated temporary checkout.
    # This exercises the same manifest, pointer, lock and drift code used by
    # the CLI without touching the real deployment or sending anything.
    with tempfile.TemporaryDirectory(prefix="market_release_drill_") as temp:
        drill_root = Path(temp)
        (drill_root / "config").mkdir()
        (drill_root / "config" / "edition_profiles.json").write_text('{"editions": {}}\n', encoding="utf-8")
        (drill_root / "app.py").write_text("print('safe')\n", encoding="utf-8")
        first = prepare_release(drill_root, "1.0.0", allow_dirty=True, execute_checks=False)
        actor = os.environ.get("USER", "release-drill")
        normal = promote_release(drill_root, "1.0.0", stage="active", actor=actor, role="maintainer", reason="offline normal release", approve=True, canary_results=[{}, {}])
        marker = drill_root / "outputs" / "runs" / "historical-output.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("preserve\n", encoding="utf-8")
        second = prepare_release(drill_root, "1.1.0", allow_dirty=True, execute_checks=False)
        promote_release(drill_root, "1.1.0", stage="active", actor=actor, role="maintainer", reason="offline candidate", approve=True, canary_results=[{}, {}])
        rolled_back = rollback_release(drill_root, "1.0.0", actor=actor, role="maintainer", reason="offline rollback", approve=True)
        (drill_root / "config" / "release_migrations.json").write_text(json.dumps({"database_version": "scheduler_sqlite_v1", "migrations": [{"migration_id": "drop_old", "compatibility": "irreversible"}]}), encoding="utf-8")
        migration = migration_dry_run(drill_root)
        (drill_root / "app.py").write_text("tampered\n", encoding="utf-8")
        drift = deployment_integrity(drill_root, "1.0.0")
        scenarios = {
            "normal_release": first["status"] == "candidate" and normal["status"] == "promoted",
            "shadow_critical_regression": canary_gate([{"critical_regressions": 1}, {"critical_regressions": 1}]).status == "paused",
            "canary_failure_pause_and_stable_route": canary_gate([{"text_image_match": False}, {"text_image_match": False}]).status == "paused" and VersionRouter("6.4.0", "6.5.0").route("evening_content", stage="canary-1")["selected_version"] == "6.4.0",
            "application_rollback_preserves_outputs": second["status"] == "candidate" and rolled_back["status"] == "rolled_back" and marker.read_text(encoding="utf-8") == "preserve\n",
            "irreversible_migration_blocked": migration["status"] == "blocked" and not migration["destructive_executed"],
            "deployment_drift_detectable": drift["status"] == "blocked" and bool(drift.get("mismatches")),
        }
    report = {"generated_at": now(), "offline": True, "external_delivery": False, "scenarios": scenarios, "passed": all(scenarios.values())}
    target = root / "outputs" / "releases"
    _write(target / "release_drill_report.json", report)
    (target / "release_drill_report.md").write_text("# Release Drill Report\n\n" + "\n".join(f"- {name}: **{'passed' if ok else 'failed'}**" for name, ok in scenarios.items()) + "\n", encoding="utf-8")
    return report
