"""Nightly read-only paper-call analytics with a local-first server fallback."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OUTPUT = REPO_ROOT / "reports" / "hybrid-analytics"
TRACKS = (
    ("morning", "ml_intraday_pick", "session_date"),
    ("evening", "evening_pick", "signal_date"),
    ("spike", "intraday_spike_ml", "trade_date"),
)
PREVIOUS = {"prev_r1", "prev_clv", "prev_last_hour", "prev_intraday", "last30",
            "rvol_last30", "close_vwap", "rel1", "f_prev_ret"}
MARKET = {"f_nifty_day", "f_nifty_30", "f_crowd", "f_crowd_side"}
MAX_UPLOAD = 1024 * 1024


def today() -> dt.date:
    return dt.datetime.now(IST).date()


def mirror_fresh(state: dict, day: dt.date) -> bool:
    try:
        synced = dt.datetime.fromisoformat(state["synced_at"])
        return (state.get("state") == "ready" and synced.tzinfo is not None
                and synced.astimezone(IST).date() == day)
    except (KeyError, ValueError, TypeError):
        return False


def read_mirror_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def category(name: str) -> str:
    if name.startswith("n_") or name in MARKET:
        return "market"
    return "previous_session" if name in PREVIOUS else "stock"


def report(conn, day: dt.date, source: str, mirror_synced_at: str | None = None) -> dict:
    """Aggregate completed, costed paper calls; never alter calls or models."""
    tracks = {}
    for name, table, date_col in TRACKS:
        extra = "and takes is true" if name == "spike" else ""
        side = "-side" if name == "spike" else "side"
        rows = conn.execute(
            f"select {date_col}, {side}, net_inr::float8, features from {table} "
            f"where status='closed' and net_inr is not null {extra} "
            f"and {date_col} <= %s",
            (day,),
        ).fetchall()
        sides = {}
        for direction, label in ((1, "buy"), (-1, "short")):
            chosen = [(d, net, features or {}) for d, s, net, features in rows if s == direction]
            names = sorted({key for _, _, features in chosen for key, value in features.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value)})
            gaps = []
            for key in names:
                wins = [float(features[key]) for _, net, features in chosen if net > 0
                        and isinstance(features.get(key), (int, float))
                        and not isinstance(features[key], bool) and math.isfinite(features[key])]
                losses = [float(features[key]) for _, net, features in chosen if net <= 0
                          and isinstance(features.get(key), (int, float))
                          and not isinstance(features[key], bool) and math.isfinite(features[key])]
                if len(wins) < 3 or len(losses) < 3:
                    continue
                wm, lm = statistics.median(wins), statistics.median(losses)
                gaps.append({"feature": key, "category": category(key),
                             "winners": len(wins), "losers": len(losses),
                             "winner_median": wm, "loser_median": lm,
                             "median_gap": wm - lm})
            sides[label] = {"closed": len(chosen), "winners": sum(net > 0 for _, net, _ in chosen),
                            "net_inr": round(sum(net for _, net, _ in chosen), 2),
                            "dates": len({str(d) for d, _, _ in chosen}), "feature_gaps": gaps}
        tracks[name] = sides
    return {"schema_version": 1, "analysis_sha256": ANALYSIS_SHA256,
            "report_date": day.isoformat(), "source": source,
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "mirror_synced_at": mirror_synced_at, "tracks": tracks,
            "interpretation": "Descriptive completed paper outcomes after recorded costs; "
            "feature gaps are not validated trade filters or model promotion evidence."}


def valid_local(payload: dict, day: dt.date) -> bool:
    try:
        generated = dt.datetime.fromisoformat(payload["generated_at"])
        synced = dt.datetime.fromisoformat(payload["mirror_synced_at"])
        return (payload["schema_version"] == 1
                and payload["analysis_sha256"] == ANALYSIS_SHA256
                and payload["report_date"] == day.isoformat()
                and payload["source"] == "local" and isinstance(payload["tracks"], dict)
                and generated.tzinfo is not None and synced.tzinfo is not None
                and generated.astimezone(IST).date() == day
                and synced.astimezone(IST).date() == day)
    except (KeyError, ValueError, TypeError):
        return False


def save(payload: dict, day: dt.date) -> str:
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = OUTPUT / ".handoff.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        target = OUTPUT / f"{day}.json"
        if target.exists():
            try:
                existing = json.loads(target.read_text())
            except (ValueError, OSError):
                existing = {}
            if valid_local(existing, day) or payload["source"] == "local":
                return "already completed"
        with tempfile.NamedTemporaryFile("w", dir=OUTPUT, prefix=".report-", delete=False) as tmp:
            path = Path(tmp.name)
            try:
                path.chmod(0o600)
                json.dump(payload, tmp, indent=2, allow_nan=False)
                tmp.write("\n")
            except Exception:
                path.unlink(missing_ok=True)
                raise
        os.replace(path, target)
        return str(target)


def receive(day: dt.date) -> str:
    raw = sys.stdin.buffer.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise ValueError("Research report exceeds 1 MiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not valid_local(payload, day) or day != today():
        raise ValueError("Local report date or mirror freshness is invalid")
    return save(payload, day)


def fallback(day: dt.date) -> str:
    if day != today():
        raise ValueError("Fallback must run for the current India date")
    target = OUTPUT / f"{day}.json"
    if target.is_file():
        try:
            if valid_local(json.loads(target.read_text()), day):
                return "local report already received"
        except (ValueError, OSError):
            pass
    from igs import envfile
    from igs.db import connect

    envfile.load(envfile.default_path())
    with connect() as conn:
        conn.execute("set transaction isolation level repeatable read, read only")
        payload = report(conn, day, "server")
    # A local upload that wins the lock during computation takes precedence.
    return save(payload, day)


def local(day: dt.date, host: str, key: Path) -> str:
    if day != today() or not key.is_file() or not host:
        raise ValueError("Current date, host and dedicated SSH key are required")
    from igs.local_research.sync import DB, STATE, connection

    state = read_mirror_state(STATE / "status.json")
    if not mirror_fresh(state, day):
        # A persistent timer may fire on boot before the missed sync timer finishes.
        # Start/wait for that one-shot service so an online computer gets first chance.
        refresh = subprocess.run(
            ["systemctl", "--user", "start", "tradinghelper-research-sync.service"],
            capture_output=True, timeout=7200, check=False,
        )
        if refresh.returncode:
            raise RuntimeError("Local mirror refresh failed; server will run fallback")
        state = read_mirror_state(STATE / "status.json")
    if not mirror_fresh(state, day):
        raise ValueError("Local mirror was not refreshed today; server will run fallback")
    synced = state["synced_at"]
    with connection(DB) as conn:
        conn.execute("begin isolation level repeatable read read only")
        payload = report(conn, day, "local", synced)
    raw = json.dumps(payload, allow_nan=False).encode()
    if len(raw) > MAX_UPLOAD:
        raise ValueError("Research report exceeds 1 MiB")
    result = subprocess.run(
        ["ssh", "-T", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
         "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes", host,
         f"upload {day.isoformat()}"],
        input=raw, capture_output=True, timeout=300, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-300:]
        raise RuntimeError(f"Local report upload failed: {detail}")
    return result.stdout.decode().strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("local", "receive", "fallback"))
    parser.add_argument("--date", type=dt.date.fromisoformat, default=today())
    parser.add_argument("--host")
    parser.add_argument("--key", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "local":
            from igs.local_research.sync import STATE

            result = local(args.date, args.host, args.key or STATE / "id_ed25519")
        elif args.mode == "receive":
            result = receive(args.date)
        else:
            result = fallback(args.date)
        print(result)
        return 0
    except Exception as exc:
        print(f"Hybrid analytics: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
