"""A2: fetch a bound skill's definition from the Console.

Central Command holds skill BINDINGS; the Console owns the marketplace
that holds the YAML. This is the fetch path E0.3 named as missing, and
it deliberately reuses the R5-2 CC->Console credential pair on the
existing `/api/internal` router rather than opening a new channel.

`parse_skill` remains the untrusted-YAML safety boundary R4-3
established. A skill that will not parse is not "probably fine": it is
unusable, and the caller is told so rather than handed a half-built
object.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("harkeniq.cc.skill_fetch")

_TIMEOUT = 10.0


async def fetch_skill_definition(
    state, tenant_id: str, skill_id: str
) -> tuple[Optional[Any], str]:
    """Return (SkillDefinition, "") or (None, reason).

    Every failure returns a REASON rather than raising, because a skill
    that cannot be fetched is an UNKNOWN dimension in the preflight, not
    an exception that aborts it: an operator still needs to see the rest
    of the readiness result.
    """
    from harkeniq.skills.loader import parse_skill

    console_url = getattr(state.config, "console_url", "") or ""
    api_key = getattr(state.config, "console_api_key", "") or ""
    if not console_url:
        return None, "no Console URL is configured, so skills cannot be fetched"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{console_url.rstrip('/')}/api/internal/marketplace/skills/{skill_id}",
                params={"tenant_id": tenant_id},
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    except Exception as exc:  # noqa: BLE001 -- transport failure is UNKNOWN
        logger.warning("skill %s fetch failed: %s", skill_id, exc)
        return None, f"the Console could not be reached: {exc}"

    if response.status_code == 404:
        return None, "this skill is not in the marketplace for this tenant"
    if response.status_code != 200:
        return None, f"the Console answered {response.status_code}"

    body = response.json()
    yaml_content = body.get("yaml_content") or ""
    if not yaml_content:
        return None, "the marketplace entry carries no YAML"

    import yaml as _yaml

    try:
        # safe_load, then parse_skill: the schema validation R4-3 made the
        # untrusted-YAML boundary. Neither step is optional.
        definition = parse_skill(_yaml.safe_load(yaml_content), source=skill_id)
    except Exception as exc:  # noqa: BLE001
        return None, f"the skill did not validate: {exc}"
    return definition, ""
