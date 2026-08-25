"""Skill YAML loader and validator (Doc 07 §2, §8).

Loads all .yaml files from a skills directory, validates them against the
schema and per-target field lists (load-time DSL field validation), and
parses all rule conditions into ASTs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from harkeniq.errors import SkillValidationError
from harkeniq.models import (
    ActionRecommendation,
    ActionType,
    DebounceConfig,
    SkillDefinition,
    SkillRule,
    TrendingRule,
    VerdictSeverity,
)
from harkeniq.skills.expression import collect_fields, parse

logger = logging.getLogger("harkeniq.skills.loader")

DEFAULT_SKILLS_DIR = "/etc/harkeniq/skills"

VALID_TARGETS = ("fan", "disk", "memory", "psu", "thermal", "interface")

# Baseline-derived fields, available for every target when confidence >= 0.5
# (Doc 07 §3.4)
BASELINE_FIELDS = {"baseline_mean", "baseline_stddev", "deviation"}

# Per-target normalized field names (Doc 07 §3.4, Doc 08)
VALID_FIELDS: dict[str, set[str]] = {
    "fan": {
        "name", "speed_rpm", "speed_pct", "health", "state",
        "threshold_low_critical", "redundancy_health", "location",
    },
    "disk": {
        "name", "serial", "media_type", "protocol", "capacity_bytes",
        "health", "life_left_pct", "smart_alert", "raid_status",
        "temperature_c", "slot",
    },
    "memory": {
        "name", "capacity_mib", "type", "speed_mhz", "health", "state",
        "socket", "channel", "slot",
        "ecc_correctable_lifetime", "ecc_uncorrectable_lifetime",
        "ecc_correctable_current",
        "alarm_ecc_correctable", "alarm_ecc_uncorrectable", "alarm_temperature",
    },
    "psu": {
        "name", "member_id", "type", "capacity_watts", "output_watts",
        "input_voltage", "health", "state", "model", "serial",
        "redundancy_health", "redundancy_mode",
    },
    "thermal": {
        "name", "reading_c", "health",
        "threshold_warning", "threshold_critical", "threshold_fatal",
        "threshold_cold_warning", "threshold_cold_critical", "context",
    },
    # R6: network port. Skills see RATES, never raw counters (design doc §7
    # decision 7); *_total fields are deliberately absent from this list.
    "interface": {
        "name", "admin_state", "oper_state", "speed_mbps", "health",
        "in_error_rate", "out_error_rate", "in_discard_rate",
        "out_discard_rate", "crc_error_rate", "in_octet_rate",
        "out_octet_rate",
        "queue_occupancy_max_pct", "crc_error_rate_max", "ber_trend",
        "optics_tx_power_dbm", "optics_rx_power_dbm", "optics_temperature_c",
        "pre_fec_ber", "lag_name",
    },
}

VALID_VERDICTS = ("CRITICAL", "WARNING", "HEALTHY", "TRENDING", "UNKNOWN")
VALID_TRENDING_DIRECTIONS = ("declining", "rising")

_SKILL_KEYS = {"name", "version", "target", "description", "rules", "trending", "default_verdict"}
_RULE_KEYS = {"condition", "verdict", "message", "debounce", "action"}
_TRENDING_KEYS = {"field", "direction", "verdict", "message", "threshold_field", "counter"}
_DEBOUNCE_KEYS = {"count", "window"}
_ACTION_KEYS = {"type", "params"}


def _fields_for(target: str) -> set[str]:
    return VALID_FIELDS[target] | BASELINE_FIELDS


def _validate_condition_fields(name: str, target: str, condition: str, ast) -> None:
    """Load-time DSL field validation (Doc 07 §8.2)."""
    valid = _fields_for(target)
    for field_name in sorted(collect_fields(ast)):
        if field_name not in valid:
            raise SkillValidationError(
                name, f"unknown field '{field_name}' for target '{target}' "
                f"in condition: {condition}"
            )


def _parse_verdict(name: str, raw: Any) -> VerdictSeverity:
    if not isinstance(raw, str) or raw not in VALID_VERDICTS:
        raise SkillValidationError(
            name, f"unknown verdict '{raw}' (expected {', '.join(VALID_VERDICTS)})"
        )
    return VerdictSeverity(raw)


def _parse_rule(name: str, target: str, index: int, raw: Any) -> SkillRule:
    if not isinstance(raw, dict):
        raise SkillValidationError(name, f"rule {index} must be a mapping")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise SkillValidationError(name, f"rule {index} has unknown fields: {sorted(unknown)}")

    condition = raw.get("condition")
    if not condition or not isinstance(condition, str):
        raise SkillValidationError(name, f"rule {index}: condition is required")
    ast = parse(condition)  # raises SkillParseError on bad syntax
    _validate_condition_fields(name, target, condition, ast)

    verdict = _parse_verdict(name, raw.get("verdict"))

    message = raw.get("message")
    if not message or not isinstance(message, str):
        raise SkillValidationError(name, f"rule {index}: message is required")

    debounce: Optional[DebounceConfig] = None
    if "debounce" in raw:
        d = raw["debounce"]
        if not isinstance(d, dict) or set(d) - _DEBOUNCE_KEYS or "count" not in d or "window" not in d:
            raise SkillValidationError(name, f"rule {index}: debounce requires count and window")
        count, window = d["count"], d["window"]
        if not isinstance(count, int) or not isinstance(window, int) or count < 1 or window < 1:
            raise SkillValidationError(name, f"rule {index}: debounce count/window must be positive integers")
        if count > window:
            raise SkillValidationError(name, f"debounce count {count} exceeds window {window}")
        debounce = DebounceConfig(count=count, window=window)

    action: Optional[ActionRecommendation] = None
    if "action" in raw:
        a = raw["action"]
        if not isinstance(a, dict) or set(a) - _ACTION_KEYS or "type" not in a:
            raise SkillValidationError(name, f"rule {index}: action requires a type")
        try:
            action_type = ActionType(a["type"])
        except ValueError:
            allowed = ", ".join(t.value for t in ActionType)
            raise SkillValidationError(
                name, f"unknown action type '{a['type']}' (allowed: {allowed})"
            )
        params = a.get("params", {})
        if not isinstance(params, dict):
            raise SkillValidationError(name, f"rule {index}: action params must be a mapping")
        action = ActionRecommendation(type=action_type, params={str(k): str(v) for k, v in params.items()})

    return SkillRule(
        condition=condition,
        parsed_ast=ast,
        verdict=verdict,
        message_template=message,
        debounce=debounce,
        action=action,
    )


def _parse_trending(name: str, target: str, index: int, raw: Any) -> TrendingRule:
    if not isinstance(raw, dict):
        raise SkillValidationError(name, f"trending {index} must be a mapping")
    unknown = set(raw) - _TRENDING_KEYS
    if unknown:
        raise SkillValidationError(name, f"trending {index} has unknown fields: {sorted(unknown)}")

    field_name = raw.get("field")
    if not field_name or not isinstance(field_name, str):
        raise SkillValidationError(name, f"trending {index}: field is required")
    if field_name not in _fields_for(target):
        raise SkillValidationError(
            name, f"unknown field '{field_name}' for target '{target}' in trending"
        )

    direction = raw.get("direction")
    if direction not in VALID_TRENDING_DIRECTIONS:
        raise SkillValidationError(name, "trending direction must be 'declining' or 'rising'")

    verdict = _parse_verdict(name, raw.get("verdict", "TRENDING"))
    if verdict != VerdictSeverity.TRENDING:
        raise SkillValidationError(name, f"trending verdict must be TRENDING, got '{verdict.value}'")

    message = raw.get("message")
    if not message or not isinstance(message, str):
        raise SkillValidationError(name, f"trending {index}: message is required")

    threshold_field: Optional[str] = None
    if "threshold_field" in raw:
        tf = raw["threshold_field"]
        # A numeric constant (e.g. 0 = project SSD wear toward 0, Doc 07 §7.2)
        # or a normalized field name.
        if isinstance(tf, (int, float)) and not isinstance(tf, bool):
            threshold_field = str(tf)
        elif isinstance(tf, str) and tf in _fields_for(target):
            threshold_field = tf
        else:
            raise SkillValidationError(
                name, f"trending threshold_field '{tf}' is neither a number "
                f"nor a known field for target '{target}'"
            )

    counter = raw.get("counter", False)
    if not isinstance(counter, bool):
        raise SkillValidationError(
            name, f"trending {index}: counter must be a boolean"
        )

    return TrendingRule(
        field=field_name,
        direction=direction,
        verdict=verdict,
        message_template=message,
        threshold_field=threshold_field,
        counter=counter,
    )


def parse_skill(data: Any, source: str = "<memory>") -> SkillDefinition:
    """Validate a parsed YAML document and build a SkillDefinition.

    Raises SkillValidationError / SkillParseError per Doc 07 §8.2.
    """
    if not isinstance(data, dict):
        raise SkillValidationError(source, "skill file must contain a YAML mapping")

    name = data.get("name")
    if not name or not isinstance(name, str):
        raise SkillValidationError(source, "name is required")

    unknown = set(data) - _SKILL_KEYS
    if unknown:
        raise SkillValidationError(name, f"unknown fields: {sorted(unknown)}")

    version = data.get("version")
    if version != 1:
        raise SkillValidationError(name, f"unsupported schema version {version!r}")

    target = data.get("target")
    if target not in VALID_TARGETS:
        raise SkillValidationError(name, f"unknown target '{target}'")

    raw_rules = data.get("rules")
    if not raw_rules or not isinstance(raw_rules, list):
        raise SkillValidationError(name, "at least one rule is required")
    rules = [_parse_rule(name, target, i, r) for i, r in enumerate(raw_rules)]

    trending = []
    if "trending" in data:
        raw_trending = data["trending"]
        if not isinstance(raw_trending, list):
            raise SkillValidationError(name, "trending must be a list")
        trending = [_parse_trending(name, target, i, t) for i, t in enumerate(raw_trending)]

    default_verdict = VerdictSeverity.HEALTHY
    if "default_verdict" in data:
        default_verdict = _parse_verdict(name, data["default_verdict"])

    description = data.get("description", "")

    return SkillDefinition(
        name=name,
        version=version,
        target=target,
        description=description,
        rules=rules,
        trending=trending,
        default_verdict=default_verdict,
    )


def load_skill_file(path: str | Path) -> SkillDefinition:
    """Load and validate a single skill YAML file."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise SkillValidationError(path.name, f"invalid YAML: {e}")
    return parse_skill(data, source=path.name)


def load_skills(directory: str | Path) -> dict[str, SkillDefinition]:
    """Load all .yaml skill files from a directory (Doc 07 §8.1).

    Duplicate skill names are an error.
    Raises SkillValidationError if the directory does not exist.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise SkillValidationError(str(directory), "skills directory does not exist")

    skills: dict[str, SkillDefinition] = {}
    for path in sorted(directory.glob("*.yaml")):
        skill = load_skill_file(path)
        if skill.name in skills:
            raise SkillValidationError(skill.name, f"duplicate skill name (file {path.name})")
        skills[skill.name] = skill
        logger.info("Loaded skill %s (target=%s, %d rules)", skill.name, skill.target, len(skill.rules))

    return skills


def load_skills_with_packages(
    directory: str | Path,
) -> dict[str, tuple["SkillDefinition", "SkillPackage"]]:
    """Load skills and wrap each with lifecycle metadata (R3b-1 C5).

    Returns {name: (SkillDefinition, SkillPackage)}.  File-loaded skills
    are treated as PROMOTED (they come from the operator's skills dir).
    """
    from harkeniq.autonomy.skill_lifecycle import SkillPackage, SkillTier, ValidationState

    skills = load_skills(directory)
    result = {}
    for name, skill_def in skills.items():
        pkg = SkillPackage(
            skill_id=name,
            version=str(skill_def.version),
            vendor="",  # not in current YAML format
            device_types=[],
            tier=SkillTier.CORE,
            validation_state=ValidationState.PROMOTED,
        )
        result[name] = (skill_def, pkg)
    return result
