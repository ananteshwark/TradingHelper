"""Score a local Ollama model on the evaluation calls, next to the cloud model's own calls.

Both are judged the same way as the app's track record: a buy is right if it beat the Nifty
500 at the call's horizon, a sell if it lagged; holds are counted but not scored. The local
model only counts as better if it is right more often on the SAME calls; a small evaluation
set is reported with its size so a lucky streak is not mistaken for skill.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

OLLAMA = "http://127.0.0.1:11434"      # local only; prompts never leave this computer


def ask(client: httpx.Client, model: str, messages: list[dict], num_ctx: int) -> str | None:
    """The local model's action, or None if it did not return a valid call."""
    r = client.post(f"{OLLAMA}/api/chat", json={
        "model": model, "messages": messages, "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": num_ctx}})
    r.raise_for_status()
    try:
        action = json.loads(r.json()["message"]["content"]).get("action")
    except (ValueError, KeyError, AttributeError):
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


def run(data: Path, model: str, num_ctx: int = 16384, limit: int | None = None) -> dict:
    items = [json.loads(line) for line in
             (data / "eval.jsonl").read_text(encoding="utf-8").splitlines()][:limit]
    if not items:
        raise ValueError("eval.jsonl is empty; choose an earlier cutoff")
    local, rows = [], []
    with httpx.Client(timeout=600) as client:
        for item in items:
            action = ask(client, model, item["messages"], num_ctx)
            local.append(action)
            rows.append({**item["meta"], "local_action": action})
    excess = [i["meta"]["excess_pct"] for i in items]
    report = {"model": model, "local": score(local, excess),
              "cloud": score([i["meta"]["cloud_action"] for i in items], excess)}
    (data / "eval_results.jsonl").write_text(
        "".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
    (data / "eval_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
