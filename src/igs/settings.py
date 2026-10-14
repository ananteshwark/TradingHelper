"""Settings changed on the UI's Settings page.

Each config file the page can change (config.EDITABLE) has a local, untracked file of
the same name in data/settings (config.settings_dir) that its loader merges over
config/<name>, so the tracked defaults are never edited and `git pull` never conflicts.
Only values that differ from the defaults are stored, so later changes to the defaults
still apply to everything else. Access (access.yaml) is the exception: the file there
replaces config/access.yaml whole (save_access). API keys and tokens are not settings:
they live in .env (envfile).
"""

from __future__ import annotations

import os
from typing import Any

import yaml
from pydantic import BaseModel

from igs import config
from igs.config import AssistantConfig, _load_yaml, deep_merge, settings_dir

MODELS: dict[str, type[BaseModel]] = {
    "universe.yaml": config.UniverseConfig, "scoring.yaml": config.ScoringConfig,
    "red_flags.yaml": config.RedFlagsConfig, "backtest.yaml": config.BacktestConfig,
    "alerts.yaml": config.AlertsConfig, "sync.yaml": config.SyncConfig,
    "news.yaml": config.NewsConfig, "broker_calls.yaml": config.BrokerCallsConfig,
    "costs.yaml": config.CostsConfig, "plan.yaml": config.PlanConfig,
    "assistant.yaml": AssistantConfig}


def _header(name: str) -> str:
    return ("# Written by the Settings page of the IndiaGrowthScreener UI. Values here override\n"
            f"# config/{name}; delete this file (or use Reset) to go back to it.\n")


HEADER = _header("assistant.yaml")


def _diff(values: dict, base: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            sub = _diff(v, base[k])
            if sub:
                out[k] = sub
        elif base.get(k) != v:
            out[k] = v
    return out


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def path_for(name: str):
    return settings_dir() / name


def save(name: str, values: dict, *, compare_to: dict | None = None) -> BaseModel:
    """Validate config/<name> with `values` over it and store what differs from the
    defaults (`compare_to`, default config/<name> itself). Raises
    pydantic.ValidationError (a ValueError) and writes nothing if the result is
    invalid. Storing nothing removes the file."""
    base = _load_yaml(name)
    cfg = MODELS[name].model_validate(deep_merge(base, values))
    changed = _diff(values, base if compare_to is None else compare_to)
    path = path_for(name)
    if not changed:
        path.unlink(missing_ok=True)
    else:
        _write(path, _header(name) + yaml.safe_dump(changed, sort_keys=True,
                                                     allow_unicode=True))
    return cfg


def reset(name: str) -> None:
    path_for(name).unlink(missing_ok=True)


def assistant_path():
    return path_for("assistant.yaml")


def save_assistant(values: dict) -> AssistantConfig:
    """Validate `values` over the defaults and store what differs from them. Raises
    pydantic.ValidationError (a ValueError) and writes nothing if the result is invalid."""
    base = _load_yaml("assistant.yaml")
    return save("assistant.yaml", values,
                compare_to=AssistantConfig.model_validate(base).model_dump())


def reset_assistant() -> None:
    reset("assistant.yaml")


def save_filing_quarters(quarters: int) -> None:
    """Persist the universe history threshold without modifying tracked defaults."""
    save("universe.yaml", deep_merge(config.overrides("universe.yaml"),
                                     {"min_filing_quarters": quarters}))


def save_access(values: dict, *, keep_admin=None):
    """Validate an access policy and store it whole in data/settings/access.yaml, which
    replaces config/access.yaml (igs.access.load_policy). `keep_admin(policy)` must
    accept the new policy, e.g. that the administrator saving it stays one; otherwise
    nothing is written. Returns the policy."""
    from igs.access import AccessPolicy

    policy = AccessPolicy.model_validate(values)
    if keep_admin is not None and not keep_admin(policy):
        raise ValueError("You would no longer be an administrator under this policy; "
                         "keep your own account among the administrators.")
    _write(path_for("access.yaml"),
           "# Written by the Settings page of the IndiaGrowthScreener UI. It replaces\n"
           "# config/access.yaml whole; delete it to go back to that file.\n"
           + yaml.safe_dump(policy.model_dump(), sort_keys=False, allow_unicode=True))
    return policy
