"""Training examples from matured AI stock calls.

Each example is the prompt the call was made from (rebuilt from the inputs stored with it)
and the call the cloud model gave, kept only when the realised excess return over the Nifty
500 at the call's horizon agreed with it (filtered supervised fine-tuning): a buy that beat
the index by more than `band`, a sell that lagged it by more than `band`, a hold that stayed
within it. The evaluation split keeps every matured call, right or wrong, so the local model
is scored on the same calls the cloud model is.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from igs.assistant.calls import HORIZONS, PROMPT_VERSION, outcome, system
from igs.config import load_assistant

CALL_FIELDS = ("action", "confidence", "horizon_months", "stage", "target_price", "summary",
               "reasons", "risks", "buy_when", "sell_when", "data_gaps", "vs_growth",
               "vs_brokers")


@dataclass(frozen=True)
class Split:
    cutoff: dt.date                 # calls made on/after this date are evaluation calls
    band_pct: float = 2.0           # excess return within +/- band counts as a hold
    embargo_days: int = 365         # training calls must mature before the cutoff

    def __post_init__(self):
        if not 0 <= self.band_pct < 50 or self.embargo_days < 0:
            raise ValueError('band_pct must be in [0, 50) and embargo_days non-negative')


def horizon_key(months: int) -> str:
    """The longest measured horizon not beyond the call's own (1m, 3m, 6m, 12m)."""
    days = months * 30.4
    keys = [k for k, d in HORIZONS.items() if d <= days + 1]
    return keys[-1] if keys else "1m"


def realised(excess_pct: float, band_pct: float) -> str:
    return "buy" if excess_pct > band_pct else "sell" if excess_pct < -band_pct else "hold"


def prompt(row: dict) -> str:
    """The user message, in make_call's layout. The previous call and last buy shown at the
    time are not stored, so they are omitted."""
    inputs = row["inputs"]
    sections = {"why_now": row.get("reason") or "asked for a call", **inputs}
    body = "\n".join(f"<{name}>\n{json.dumps(value, default=str)}\n</{name}>"
                     for name, value in sections.items() if value is not None)
    run = inputs.get("run") or {}
    return (f"Score run {run.get('run_id', row['run_id'])}, as of {run.get('as_of', '')}. "
            f"Everything the app holds on {row['symbol']} at that date:\n{body}\n"
            f"Make your call on {row['symbol']}.")


def answer(row: dict) -> str:
    call = {k: row[k] for k in CALL_FIELDS}
    call["action"] = row.get("ai_action") or row["action"]   # what the model itself said
    call["target_price"] = call["target_price"] or 0
    call["broker_verdicts"] = []
    return json.dumps(call, ensure_ascii=False)


def matured(conn, today: dt.date | None = None) -> list[dict]:
    """Calls in the current output schema whose own horizon has been measured."""
    with conn.cursor() as cur:
        cur.execute(f"""select call_id, company_id, symbol, run_id, created_at, reason,
                               inputs, model, prompt_version, price_date, entry_source,
                               entry_date, entry_price, entry_benchmark, ai_action,
                               {', '.join(CALL_FIELDS)}
                        from ai_call where stage is not null and vs_growth is not null
                          and vs_brokers is not null
                        order by created_at, call_id""")
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    out = []
    for row in rows:
        key = horizon_key(row["horizon_months"])
        measured = outcome(conn, row, today)["horizons"].get(key)
        if measured and measured["excess_pct"] is not None:
            out.append({**row, "horizon": key, "excess_pct": measured["excess_pct"]})
    return out


def build(rows: list[dict], split: Split, minimum: float) -> dict[str, list[dict]]:
    sys_prompt = system(minimum)
    train, evaluation = [], []
    for row in rows:
        made = row["created_at"].date()
        truth = realised(row["excess_pct"], split.band_pct)
        given = row.get("ai_action") or row["action"]
        meta = {"call_id": row["call_id"], "symbol": row["symbol"], "made": made.isoformat(),
                "horizon": row["horizon"], "excess_pct": row["excess_pct"],
                "realised": truth, "cloud_action": given, "cloud_model": row["model"],
                "prompt_version": row["prompt_version"]}
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt(row)}]
        if made >= split.cutoff:
            evaluation.append({"messages": messages, "meta": meta})
        elif (made + dt.timedelta(days=split.embargo_days) < split.cutoff
              and given == truth):
            train.append({"messages": [*messages,
                                       {"role": "assistant", "content": answer(row)}],
                          "meta": meta})
    return {"train": train, "eval": evaluation}


def export(conn, out: Path, split: Split, today: dt.date | None = None) -> dict:
    minimum = load_assistant().features.call.min_buy_target_pct_a_year
    parts = build(matured(conn, today), split, minimum)
    out.mkdir(parents=True, exist_ok=True)
    for name, items in parts.items():
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    summary = {"cutoff": split.cutoff.isoformat(), "band_pct": split.band_pct,
               "embargo_days": split.embargo_days, "prompt_version_now": PROMPT_VERSION,
               "train": len(parts["train"]), "eval": len(parts["eval"])}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
