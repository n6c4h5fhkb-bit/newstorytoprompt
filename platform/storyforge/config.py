from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os

import yaml

from storyforge import ROOT


class SflError(Exception):
    """An actionable workflow error suitable for the CLI and Inbox."""


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise SflError(f"Expected a YAML mapping: {path}")
    return value


def merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def configuration(project: Path | None = None, path: Path | None = None) -> dict:
    selected = path or Path(os.environ.get("SFL_CONFIG", ROOT / "config.yaml"))
    config = load_yaml(selected)
    if project and (project / "project.yaml").exists():
        config = merge(config, load_yaml(project / "project.yaml"))
    if config["output"]["ratio"] != "16:9":
        raise SflError("This version supports 16:9 only.")
    return config


def model_card(config: dict) -> dict:
    name = config["output"]["model_card"]
    if not isinstance(name, str) or not name or Path(name).name != name or any(c in name for c in "/\\:"):
        raise SflError("Invalid model card name.")
    card = load_yaml(ROOT / "model_cards" / f"{name}.yaml")
    if card.get("max_seconds", 0) <= 0:
        raise SflError("Set verified max_seconds in the model card before running.")
    if any(card.get("refs", {}).get(k) is None for k in ("images", "videos", "audio")):
        raise SflError("Set your account's reference limits in the model card before running.")
    return card
