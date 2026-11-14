"""Run local research commands in the background with an inspectable result log."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from igs.local_research.sync import DB, ROOT, SOCKET

ARTIFACTS = ROOT / "reports" / "local-research"
JOBS = ARTIFACTS / "jobs"
KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def artifact(kind: str, name: str) -> Path:
    if not KIND.fullmatch(name):
        raise ValueError("Invalid artifact name")
    return ARTIFACTS / kind / name


def launch(action: str, **options: str) -> str:
    job_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    args = [sys.executable, "-m", "igs.local_research.jobs", action, "--job-id", job_id]
    for key, value in options.items():
        args += ["--" + key.replace("_", "-"), str(value)]
    subprocess.Popen(
        args,
        cwd=ROOT,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return job_id


def _write(path: Path, payload: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temp, path)


def execute(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"\d{8}T\d{12}Z", args.job_id):
        raise ValueError("Invalid job ID")
    JOBS.mkdir(parents=True, exist_ok=True)
    output = ARTIFACTS / args.action / args.job_id
    command = [str(ROOT / ".venv" / "bin" / "igs")]
    if args.action == "gate":
        command += ["gate", "run"]
        output = JOBS / args.job_id
    elif args.action == "features":
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
        if start >= end or end >= dt.datetime.now(dt.UTC).date() or not 1 <= args.horizon <= 756:
            raise ValueError("Use completed dates and a horizon from 1 to 756 sessions")
        command += [
            "research",
            "features",
            "--start",
            str(start),
            "--end",
            str(end),
            "--horizon",
            str(args.horizon),
            "--frequency",
            args.frequency,
            "--output",
            str(output),
        ]
    elif args.action == "train":
        dataset = artifact("features", args.dataset)
        if not (dataset / "manifest.json").is_file():
            raise ValueError("Feature dataset is missing")
        validation = dt.date.fromisoformat(args.validation)
        holdout = dt.date.fromisoformat(args.holdout)
        if validation >= holdout:
            raise ValueError("Validation must precede holdout")
        command += [
            "research",
            "train",
            "--dataset",
            str(dataset),
            "--validation",
            str(validation),
            "--holdout",
            str(holdout),
            "--algorithm",
            args.algorithm,
            "--output",
            str(output),
        ]
    elif args.action == "predict":
        dataset = artifact("features", args.dataset)
        model = artifact("train", args.model)
        if not (dataset / "manifest.json").is_file() or not (model / "manifest.json").is_file():
            raise ValueError("Model and feature dataset are required")
        command += [
            "research",
            "predict",
            "--model",
            str(model),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ]
    elif args.action == "portfolio":
        dataset = artifact("features", args.dataset)
        predictions = artifact("predict", args.predictions)
        if (
            not (dataset / "manifest.json").is_file()
            or not (predictions / "manifest.json").is_file()
        ):
            raise ValueError("Predictions and feature dataset are required")
        command += [
            "research",
            "portfolio",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
            "--output",
            str(output),
        ]
    else:
        raise ValueError("Unsupported job")
    env = dict(os.environ)
    env["IGS_DATABASE_URL"] = f"postgresql:///{DB}?host={SOCKET}&port=55438"
    record = {
        "action": args.action,
        "job_id": args.job_id,
        "state": "running",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "output": str(output),
        "command": command[1:],
    }
    info = JOBS / f"{args.job_id}.json"
    _write(info, record)
    with (JOBS / f"{args.job_id}.log").open("w") as log:
        result = subprocess.run(
            command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    record.update(
        state="passed" if result.returncode == 0 else "failed",
        finished_at=dt.datetime.now(dt.UTC).isoformat(),
        exit_code=result.returncode,
    )
    _write(info, record)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("gate", "features", "train", "predict", "portfolio"))
    p.add_argument("--job-id", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--horizon", type=int, default=21)
    p.add_argument("--frequency", choices=("daily", "weekly", "monthly"), default="monthly")
    p.add_argument("--dataset")
    p.add_argument("--model")
    p.add_argument("--predictions")
    p.add_argument("--validation")
    p.add_argument("--holdout")
    p.add_argument("--algorithm", choices=("ridge", "lightgbm"), default="ridge")
    args = p.parse_args(argv)
    try:
        return execute(args)
    except Exception as exc:
        if re.fullmatch(r"\d{8}T\d{12}Z", args.job_id):
            JOBS.mkdir(parents=True, exist_ok=True)
            _write(
                JOBS / f"{args.job_id}.json",
                {
                    "action": args.action,
                    "job_id": args.job_id,
                    "state": "failed",
                    "finished_at": dt.datetime.now(dt.UTC).isoformat(),
                    "error": str(exc),
                },
            )
        print(f"Research job stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
