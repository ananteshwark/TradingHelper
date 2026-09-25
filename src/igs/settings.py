"""Settings changed on the UI's Settings page.

They are written to a local, untracked file (data/settings/assistant.yaml, see
config.settings_dir) that load_assistant merges over config/assistant.yaml, so the tracked
defaults are never edited and `git pull` never conflicts. Only values that differ from the
defaults are stored, so later changes to the defaults still apply to everything else.
The API key is not a setting: it lives in .env (envfile).
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from igs.config import AssistantConfig, _load_yaml, deep_merge, settings_dir

HEADER = ("# Written by the Settings page of the IndiaGrowthScreener UI. Values here override\n"
          "# config/assistant.yaml; delete this file (or use Reset) to go back to it.\n")


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


def assistant_path():
    return settings_dir() / "assistant.yaml"


def save_assistant(values: dict) -> AssistantConfig:
    """Validate `values` over the defaults and store what differs from them. Raises
    pydantic.ValidationError (a ValueError) and writes nothing if the result is invalid."""
    base = _load_yaml("assistant.yaml")
    cfg = AssistantConfig.model_validate(deep_merge(base, values))
    changed = _diff(values, base)
    path = assistant_path()
    if not changed:
        path.unlink(missing_ok=True)
        return cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(HEADER + yaml.safe_dump(changed, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return cfg


def reset_assistant() -> None:
    assistant_path().unlink(missing_ok=True)
