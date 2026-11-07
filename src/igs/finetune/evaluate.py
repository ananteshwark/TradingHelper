"""Score a local Ollama model on the evaluation calls, next to the cloud model's own calls.

Both are judged the same way as the app's track record: a buy is right if it beat the Nifty
500 at the call's horizon, a sell if it lagged; holds are counted but not scored. The local
model only counts as better if it is right more often on the SAME calls; a small evaluation
set is reported with its size so a lucky streak is not mistaken for skill.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

# OpenAI-compatible Chat Completions: Ollama serves it at :11434/v1; Strata (the app's
# `local` provider, docs/LOCAL_AI.md) at its own port. Loopback only, so held-out prompts
# never leave this computer.
OLLAMA = "http://127.0.0.1:11434/v1"


def endpoint(base_url: str) -> str:
    if urlparse(base_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Evaluation only talks to a model on this computer (loopback URL)")
    return base_url.rstrip("/")


def ask(client: httpx.Client, base_url: str, model: str, messages: list[dict],
        num_ctx: int) -> str | None:
    """The local model's action, or None if it did not return a valid call."""
    r = client.post(f"{base_url}/chat/completions", json={
        "model": model, "messages": messages, "temperature": 0,
        "response_format": {"type": "json_object"},
        "options": {"num_ctx": num_ctx}})          # honoured by Ollama, ignored elsewhere
    r.raise_for_status()
    try:
        action = json.loads(r.json()["choices"][0]["message"]["content"]).get("action")
    except (ValueError, KeyError, IndexError, AttributeError):
        return None
    return action if action in ("buy", "hold", "sell") else None


def score(actions: list[str | None], excess: list[float]) -> dict:
    scored = [(a, e) for a, e in zip(actions, excess, strict=True) if a in ("buy", "sell")]
    right = [(e > 0) if a == "buy" else (e < 0) for a, e in scored]
    buys = [e for a, e in scored if a == "buy"]
    return {"calls": len(actions), "invalid": sum(a is None for a in actions),
            "buy": sum(a == "buy" for a in actions), "hold": sum(a == "hold" for a in actions),
            "sell": sum(a == "sell" for a in actions), "scored": len(scored),
            "right_pct": round(100 * sum(right) / len(right), 1) if right else None,
            "mean_buy_excess_pct": round(sum(buys) / len(buys), 2) if buys else None}


def run(data: Path, model: str, num_ctx: int = 16384, limit: int | None = None,
        base_url: str = OLLAMA, key_env: str | None = None) -> dict:
    base_url = endpoint(base_url)
    items = [json.loads(line) for line in
             (data / "eval.jsonl").read_text(encoding="utf-8").splitlines()][:limit]
    if not items:
        raise ValueError("eval.jsonl is empty; choose an earlier cutoff")
    key = os.environ.get(key_env) if key_env else None
    if key_env and not key:
        raise ValueError(f"{key_env} is not set")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    local, rows = [], []
    with httpx.Client(timeout=600, headers=headers, follow_redirects=False) as client:
        for item in items:
            action = ask(client, base_url, model, item["messages"], num_ctx)
            local.append(action)
            rows.append({**item["meta"], "local_action": action})
    excess = [i["meta"]["excess_pct"] for i in items]
    report = {"model": model, "local": score(local, excess),
              "cloud": score([i["meta"]["cloud_action"] for i in items], excess)}
    (data / "eval_results.jsonl").write_text(
        "".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
    (data / "eval_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
