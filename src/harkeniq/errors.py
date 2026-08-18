"""HarkenIQ exception hierarchy (Doc 10 §3)."""

from __future__ import annotations

from typing import Optional


class HarkenIQError(Exception):
    """Base exception for all HarkenIQ agent errors."""


# -- Configuration --


class ConfigError(HarkenIQError):
    """Configuration loading, parsing, or validation failure."""


# -- Redfish Communication --


class RedfishError(HarkenIQError):
    """Base class for Redfish BMC communication errors."""


class RedfishConnectionError(RedfishError):
    """TCP/TLS connection failure to the BMC."""

    def __init__(self, host: str, port: int, cause: Optional[Exception] = None):
        self.host = host
        self.port = port
        self.cause = cause
        super().__init__(f"Cannot connect to BMC at {host}:{port}: {cause}")


class RedfishAuthError(RedfishError):
    """BMC authentication failure (HTTP 401/403)."""

    def __init__(self, host: str, status_code: int):
        self.host = host
        self.status_code = status_code
        super().__init__(f"BMC authentication failed at {host}: HTTP {status_code}")


class RedfishTimeoutError(RedfishError):
    """Redfish HTTP request timed out."""

    def __init__(self, host: str, endpoint: str, timeout_seconds: float):
        self.host = host
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Redfish request to {host}{endpoint} timed out after {timeout_seconds}s")


class RedfishResponseError(RedfishError):
    """Unexpected HTTP response (status >= 400, except 401/404/503)."""

    def __init__(self, host: str, endpoint: str, status_code: int, body: str = ""):
        self.host = host
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = body[:500]
        super().__init__(f"Redfish error from {host}{endpoint}: HTTP {status_code}")


# -- Skill Errors --


class SkillError(HarkenIQError):
    """Base class for skill loading and evaluation errors."""


class SkillParseError(SkillError):
    """Skill condition expression parsing failure."""

    def __init__(self, expression: str, detail: str):
        self.expression = expression
        self.detail = detail
        super().__init__(f"Invalid expression '{expression}': {detail}")


class SkillValidationError(SkillError):
    """Skill YAML validation failure."""

    def __init__(self, skill_name: str, detail: str):
        self.skill_name = skill_name
        self.detail = detail
        super().__init__(f"Skill '{skill_name}' validation failed: {detail}")


# -- Heartbeat --


class HeartbeatError(HarkenIQError):
    """Heartbeat protocol error."""


# -- Checkpoint --


class CheckpointError(HarkenIQError):
    """Checkpoint persistence error."""


# -- Action --


class ActionError(HarkenIQError):
    """Action execution error."""
