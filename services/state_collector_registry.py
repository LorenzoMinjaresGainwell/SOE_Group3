from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "state_collectors.json"
FAMILY_FETCHERS = {
    "opportunities": "fetch_opportunities",
    "contracts": "fetch_contracts",
    "updates": "fetch_updates",
}
_STATE_TAG = re.compile(r"^[A-Z]{2}$")
_MODULE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def load_state_collector_config(path: Path = CONFIG_PATH) -> dict[str, dict[str, str]]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load state collector config {path}: {exc}") from exc

    if not isinstance(raw, dict) or set(raw) != set(FAMILY_FETCHERS):
        expected = ", ".join(FAMILY_FETCHERS)
        raise ValueError(f"State collector config must contain exactly these families: {expected}")

    config: dict[str, dict[str, str]] = {}
    for family in FAMILY_FETCHERS:
        mappings = raw[family]
        if not isinstance(mappings, dict):
            raise ValueError(f"State collector family {family!r} must be an object")
        validated: dict[str, str] = {}
        for tag, module_name in mappings.items():
            if not isinstance(tag, str) or not _STATE_TAG.fullmatch(tag):
                raise ValueError(f"Invalid state collector tag in {family!r}: {tag!r}")
            if not isinstance(module_name, str) or not _MODULE_NAME.fullmatch(module_name):
                raise ValueError(f"Invalid state collector module for {family!r}/{tag}: {module_name!r}")
            validated[tag] = module_name
        config[family] = validated
    return config


def configured_state_tags(family: str) -> tuple[str, ...]:
    try:
        return tuple(load_state_collector_config()[family])
    except KeyError as exc:
        raise ValueError(f"Unknown state collector family: {family!r}") from exc


def load_state_collectors(family: str) -> dict[str, Callable[..., Any]]:
    if family not in FAMILY_FETCHERS:
        raise ValueError(f"Unknown state collector family: {family!r}")

    fetcher_name = FAMILY_FETCHERS[family]
    clients: dict[str, Callable[..., Any]] = {}
    for tag, module_name in load_state_collector_config()[family].items():
        qualified_name = f"services.state_{family}.{module_name}"
        try:
            module = importlib.import_module(qualified_name)
            fetcher = getattr(module, fetcher_name)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(f"Unable to register {family} collector {tag} from {qualified_name}: {exc}") from exc
        if not callable(fetcher):
            raise TypeError(f"Collector {qualified_name}.{fetcher_name} is not callable")
        clients[tag] = fetcher
    return clients


def select_state_tags(family: str, states: str | None, all_states: bool) -> list[str]:
    configured = configured_state_tags(family)
    if all_states:
        return list(configured)

    selected = [tag.strip().upper() for tag in (states or "").split(",") if tag.strip()]
    if "ALL" in selected:
        if len(selected) != 1:
            raise ValueError("The special state tag 'all' cannot be combined with other state tags.")
        return list(configured)
    unknown = sorted(set(selected) - set(configured))
    if unknown:
        raise ValueError(
            f"Unsupported or unregistered {family} state tag(s): {','.join(unknown)}. "
            f"Configured tags: {','.join(configured)}"
        )
    return selected
