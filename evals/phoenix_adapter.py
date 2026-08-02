"""Best-effort Phoenix REST integration for offline evaluation.

The local evaluator remains authoritative. Phoenix is an observability and
comparison sink only: an unavailable server must never fail an evaluation.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _enabled() -> bool:
    return os.environ.get("PHOENIX_EVAL_ENABLED", "false").lower() == "true"


def _base_url() -> str:
    return os.environ.get("PHOENIX_URL", "http://127.0.0.1:6006").rstrip("/")


def _request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_base_url()}{path}",
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is controlled by config
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _dataset_payload(dataset_name: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    example_ids: list[str] = []
    for case in cases:
        case_id = str(case.get("case_id", ""))
        example_ids.append(case_id)
        inputs.append({"case_id": case_id, "input": case.get("input", {})})
        outputs.append({"reference": case.get("reference", {}), "expected_result": case.get("reference", {}).get("expected_result")})
        metadata.append({"case_id": case_id, **(case.get("metadata", {}) or {})})
    return {
        "action": "create",
        "name": dataset_name,
        "description": "Daily market content offline golden dataset; fixture/history only.",
        "inputs": inputs,
        "outputs": outputs,
        "metadata": metadata,
        "example_ids": example_ids,
    }


def _find_dataset(dataset_name: str) -> dict[str, Any] | None:
    query = urlencode({"name": dataset_name, "limit": 10})
    response = _request("GET", f"/v1/datasets?{query}")
    datasets = response.get("data") or []
    return datasets[0] if datasets else None


def _find_or_create_dataset(dataset_name: str, cases: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    existing = _find_dataset(dataset_name)
    if existing:
        return existing, "existing"
    response = _request("POST", "/v1/datasets/upload?sync=true", _dataset_payload(dataset_name, cases))
    data = response.get("data") or {}
    dataset_id = data.get("dataset_id")
    if not dataset_id:
        raise RuntimeError("Phoenix upload response missing dataset_id")
    dataset = _request("GET", f"/v1/datasets/{dataset_id}").get("data") or {"id": dataset_id, "name": dataset_name}
    dataset["version_id"] = data.get("version_id")
    return dataset, "created"


def _find_or_create_experiment(dataset: dict[str, Any], report: dict[str, Any]) -> tuple[dict[str, Any], str]:
    dataset_id = str(dataset["id"])
    experiment_name = str(report.get("experiment_name") or "market_content_offline")
    dataset_version = dataset.get("version_id")
    existing = _request("GET", f"/v1/datasets/{dataset_id}/experiments?limit=100").get("data") or []
    for item in existing:
        if item.get("name") == experiment_name and (not dataset_version or item.get("dataset_version_id") == dataset_version):
            return item, "existing"
    payload = {
        "name": experiment_name,
        "description": "Deterministic and model-assisted evaluation; does not update production configuration.",
        "metadata": {
            "dataset_version": report.get("dataset_version"),
            "candidates": report.get("candidates", []),
            "delivered": False,
            "source": "offline_evaluation",
        },
        "version_id": dataset_version,
        "repetitions": 1,
    }
    created = _request("POST", f"/v1/datasets/{dataset_id}/experiments", payload).get("data") or {}
    return created, "created"


def create_dataset_and_experiment(dataset_name: str, cases: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    """Register a local evaluation dataset and experiment in Phoenix.

    Returns a structured status and never raises network/API errors into the
    evaluation runner. No credentials, prompts, or candidate outputs are
    logged by this adapter.
    """
    if not _enabled():
        return {"status": "skipped", "reason": "PHOENIX_EVAL_ENABLED=false", "dataset": dataset_name}
    try:
        dataset, dataset_status = _find_or_create_dataset(dataset_name, cases)
        experiment, experiment_status = _find_or_create_experiment(dataset, report)
        dataset_id = dataset.get("id")
        experiment_id = experiment.get("id")
        return {
            "status": "created" if "created" in {dataset_status, experiment_status} else "existing",
            "dataset": dataset_name,
            "dataset_id": dataset_id,
            "dataset_status": dataset_status,
            "version_id": dataset.get("version_id"),
            "experiment": report.get("experiment_name"),
            "experiment_id": experiment_id,
            "experiment_status": experiment_status,
            "case_count": len(cases),
            "url": f"{_base_url()}/datasets/{dataset_id}" if dataset_id else _base_url(),
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "reason": type(exc).__name__,
            "dataset": dataset_name,
            "endpoint": _base_url(),
        }
