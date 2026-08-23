"""Configuration loading (Doc 06 §5).

Precedence: CLI flags > environment variables > config file > defaults
(Doc 06 §5.3). Environment variables use the form ``HARKENIQ_<SECTION>_<KEY>``
(e.g. ``HARKENIQ_BMC_HOST``). Values are coerced to the type of the
corresponding default.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from harkeniq.errors import ConfigError

DEFAULT_CONFIG_PATH = "/etc/harkeniq/config.yaml"

#: Full default configuration (Doc 06 §5.1).
DEFAULTS: dict[str, Any] = {
    "agent": {
        "id": "auto",
        "name": "",
        "log_level": "INFO",
    },
    "bmc": {
        "host": "",
        "port": 443,
        "username": "",
        "password": "",
        "verify_ssl": False,
        "session_timeout": 300,
    },
    "polling": {
        "sensor_interval": 60,
        "log_interval": 300,
        "inventory_interval": 300,
    },
    "heartbeat": {
        "port": 5150,
        "interval": 10,
        "timeout_multiplier": 3,
        "secret": "",
    },
    "peers": [],
    "site_manager": {
        "host": "",
        "port": 50051,
        "heartbeat_interval": 60,
        "token": "",
        "tls": True,
        "tls_ca": "",
        "action_poll_interval": 5,
    },
    "skills": {
        "directory": "/etc/harkeniq/skills",
        "evaluation_on_poll": True,
    },
    "checkpoint": {
        "path": "",  # empty = checkpointing disabled (set in packaged config)
        "interval": 600,
        "max_baseline_age_days": 30,
    },
    "baseline": {
        "window_samples": 1440,
        "min_samples": 60,
        "max_age_days": 30,
        "critical_pause_samples": 5,
    },
    "trending": {
        "min_samples": 60,
        "slope_threshold": 0.05,
        "r_squared_min": 0.5,
        "max_projection_days": 90,
    },
    "debounce": {
        "critical": [2, 3],
        "warning": [3, 5],
        "recovery": [3, 3],
    },
    "actions": {
        "enabled": True,
        "approval_mode": "queue",
        "allow_list": ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET"],
    },
}

_ENV_PREFIX = "HARKENIQ_"


def _deep_merge(base: dict, override: Mapping) -> dict:
    """Return base with override merged in (nested dicts merged, others replaced)."""
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, Mapping)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(raw: str, default: Any) -> Any:
    """Coerce an env-var string to the type of the corresponding default."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError as e:
            raise ConfigError(f"expected integer, got {raw!r}") from e
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as e:
            raise ConfigError(f"expected number, got {raw!r}") from e
    return raw


def _env_overrides(env: Mapping[str, str]) -> dict:
    """Extract HARKENIQ_<SECTION>_<KEY> overrides from an environment map."""
    overrides: dict[str, Any] = {}
    for section, keys in DEFAULTS.items():
        if not isinstance(keys, dict):
            continue
        for key, default in keys.items():
            var = f"{_ENV_PREFIX}{section}_{key}".upper()
            if var in env:
                try:
                    value = _coerce(env[var], default)
                except ConfigError as e:
                    raise ConfigError(f"{var}: {e}") from e
                overrides.setdefault(section, {})[key] = value
    return overrides


def load_config_file(path: str | Path) -> dict:
    """Load and parse a YAML config file. Raises ConfigError on problems."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a YAML mapping: {p}")
    return data


def load_config(
    path: Optional[str] = None,
    cli_overrides: Optional[Mapping] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Build the effective configuration.

    Layers (lowest to highest): DEFAULTS -> config file -> env vars ->
    cli_overrides. If ``path`` is given the file must exist; otherwise
    ``/etc/harkeniq/config.yaml`` is used when present and skipped when not.
    """
    config = copy.deepcopy(DEFAULTS)

    if path is not None:
        config = _deep_merge(config, load_config_file(path))
    elif Path(DEFAULT_CONFIG_PATH).is_file():
        config = _deep_merge(config, load_config_file(DEFAULT_CONFIG_PATH))

    env_map = os.environ if env is None else env
    config = _deep_merge(config, _env_overrides(env_map))

    if cli_overrides:
        config = _deep_merge(config, cli_overrides)

    return config


def validate_config(config: Mapping) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []

    def _require_positive(section: str, key: str) -> None:
        value = config.get(section, {}).get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            errors.append(f"{section}.{key} must be a positive number, got {value!r}")

    if not config.get("bmc", {}).get("host"):
        errors.append("bmc.host is required")

    for section, key in (
        ("bmc", "port"),
        ("polling", "sensor_interval"),
        ("polling", "log_interval"),
        ("polling", "inventory_interval"),
        ("heartbeat", "port"),
        ("heartbeat", "interval"),
        ("heartbeat", "timeout_multiplier"),
        ("checkpoint", "interval"),
        ("site_manager", "port"),
        ("site_manager", "heartbeat_interval"),
        ("site_manager", "action_poll_interval"),
    ):
        _require_positive(section, key)

    sm = config.get("site_manager", {})
    if sm.get("host"):
        if sm.get("tls", True):
            if not sm.get("token"):
                errors.append("site_manager.token is required when site_manager.host is set")
            if not sm.get("tls_ca"):
                errors.append(
                    "site_manager.tls_ca is required when site_manager.host is set "
                    "and site_manager.tls is true"
                )

    for section, key in (("bmc", "port"), ("heartbeat", "port"), ("site_manager", "port")):
        value = config.get(section, {}).get(key)
        if isinstance(value, int) and not 0 < value < 65536:
            errors.append(f"{section}.{key} must be 1-65535, got {value}")

    log_level = config.get("agent", {}).get("log_level", "INFO")
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append(f"agent.log_level must be DEBUG/INFO/WARNING/ERROR, got {log_level!r}")

    peers = config.get("peers", [])
    if not isinstance(peers, list):
        errors.append(f"peers must be a list, got {type(peers).__name__}")
    else:
        for i, peer in enumerate(peers):
            if not isinstance(peer, dict) or not peer.get("host"):
                errors.append(f"peers[{i}] must be a mapping with a 'host' key")

    if config.get("peers") and not config.get("heartbeat", {}).get("secret"):
        errors.append("heartbeat.secret is required when peers are configured")

    mode = config.get("actions", {}).get("approval_mode", "queue")
    if mode != "queue":
        errors.append(f"actions.approval_mode must be 'queue' (R1), got {mode!r}")

    allowed = {
        "IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET",
        "SEL_CLEAR", "BMC_RESET", "POWER_CYCLE", "POWER_CAP_ADJUST",
    }
    for entry in config.get("actions", {}).get("allow_list", []):
        if entry not in allowed:
            errors.append(f"actions.allow_list contains unknown action {entry!r}")

    debounce = config.get("debounce", {})
    for key in ("critical", "warning", "recovery"):
        pair = debounce.get(key)
        if pair is not None and (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(n, int) and n > 0 for n in pair)
            or pair[0] > pair[1]
        ):
            errors.append(f"debounce.{key} must be [threshold, window] with threshold <= window")

    return errors


def render_config(config: Mapping, redact: bool = True) -> str:
    """Serialize the effective config as YAML, redacting secrets by default."""
    data = copy.deepcopy(dict(config))
    if redact:
        for section, key in (
            ("bmc", "password"),
            ("heartbeat", "secret"),
            ("site_manager", "token"),
        ):
            if data.get(section, {}).get(key):
                data[section][key] = "********"
    return yaml.safe_dump(data, default_flow_style=None, sort_keys=False)
