"""Explicit, optional MLflow tracking export. Local artifacts remain authoritative."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from igs.lab.artifacts import digest, verify
from igs.timeutil import utc_now


def publish(path: Path, url: str, experiment_id: str, *, transport=None) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Tracking URL must not contain credentials, query or fragment")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("Remote MLflow requires HTTPS")
    manifest = verify(path)
    if manifest.get("kind") != "igs-model-experiment":
        raise ValueError("Expected a model experiment bundle")
    metrics = json.loads((path / "metrics.json").read_text())
    headers = {}
    if token := os.environ.get("MLFLOW_TRACKING_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(
        base_url=url.rstrip("/") + "/",
        headers=headers,
        timeout=30,
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    ) as client:

        def post(route, body):
            if route not in ('runs/create', 'runs/log-batch', 'runs/update'):
                raise ValueError('Unsupported MLflow tracking route')
            response = client.post("api/2.0/mlflow/" + route, json=body)
            # Do not expose server response bodies (they may echo credentials).
            if not 200 <= response.status_code < 300:
                raise ValueError(f"MLflow {route} returned HTTP {response.status_code}")
            return response.json()

        stamp = int(utc_now().timestamp() * 1000)
        run_id = post(
            "runs/create",
            {
                "experiment_id": experiment_id,
                "start_time": stamp,
                "tags": [
                    {"key": "igs.stage", "value": "research"},
                    {"key": "igs.paper_only", "value": "true"},
                    {"key": "igs.manifest_sha256", "value": digest(path / "manifest.json")},
                ],
            },
        )["run"]["info"]["run_id"]
        try:
            post(
                "runs/log-batch",
                {
                    "run_id": run_id,
                    "params": [
                        {"key": key, "value": str(value)}
                        for key, value in manifest["parameters"].items()
                    ],
                    "metrics": [
                        {"key": f"{split}.{key}", "value": value, "timestamp": stamp, "step": 0}
                        for split, report in metrics.items()
                        for key, value in report.items()
                        if isinstance(value, (int, float))
                    ],
                },
            )
            post("runs/update", {"run_id": run_id, "status": "FINISHED", "end_time": stamp})
        except Exception:
            # Never blindly retry a create: it may have succeeded on the server.
            try:
                post("runs/update", {"run_id": run_id, "status": "FAILED", "end_time": stamp})
            except (httpx.HTTPError, ValueError):
                pass
            raise
    return run_id
