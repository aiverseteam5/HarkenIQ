"""CC -> SM autonomy policy push (QA-022, R-C5).

Builds the ``autonomy_budgets_json`` payload for the PushPolicy RPC from
the persisted stop switch + autonomy budget rows, and pushes it to every
registered Site Manager. Called from the stop-switch endpoints (so a flip
propagates immediately, not on the next poll tick) and from the fleet
poll loop (so SM restarts and new sites converge).

Budget-row -> policy mapping (conservative, documented):
  - Only the ``device_type="*"`` row maps; the SM enforcer is site-wide
    per action type and has no device dimension yet (R3b deferral).
  - Which classes each level grants is NOT decided here. S5 promoted that
    mapping to ``harkeniq_cc.autonomy.grants_for_level`` — the same object
    the ``/api/autonomy`` contract reports — so the posture an operator
    reads and the policy an enforcer receives cannot drift apart. A test
    fails if this module and that contract ever disagree.
"""

from __future__ import annotations

import json
import logging

from harkeniq_cc.autonomy import grants_for_level
from harkeniq_cc.db.repos import AutonomyBudgetRepo, SiteRepo, StopSwitchRepo

logger = logging.getLogger("harkeniq.cc.policy_push")

_PERIOD_SECONDS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,
}


def budget_row_to_policies(budget) -> list[dict]:
    """Map one CCAutonomyBudget row to SM enforcer policy dicts."""
    granted = grants_for_level(budget.level)
    if not granted:
        return []
    window = _PERIOD_SECONDS.get(budget.budget_period, 86400)
    return [
        {
            "action_type": action_type,
            "max_per_window": budget.budget_limit,
            "window_seconds": window,
            "risk_level": risk,
        }
        for action_type, risk in granted.items()
    ]


async def build_autonomy_payload(session, tenant_id: str) -> str:
    """Assemble the PushPolicy autonomy_budgets_json for a tenant."""
    stop_row = await StopSwitchRepo(session).get(tenant_id)
    policies: list[dict] = []
    for budget in await AutonomyBudgetRepo(session).list_all(tenant_id):
        if budget.device_type != "*":
            # SM enforcement has no device dimension yet; skip rather
            # than silently widen a device-scoped budget to the site.
            logger.debug(
                "Skipping device-scoped budget %s (%s) in policy push",
                budget.id, budget.device_type,
            )
            continue
        policies.extend(budget_row_to_policies(budget))
    return json.dumps({
        "stop_switch": bool(stop_row.active) if stop_row else False,
        "stop_switch_by": (stop_row.changed_by if stop_row else ""),
        "policies": policies,
    })


async def push_policy_to_all_sites(
    config, sessionmaker, client=None
) -> int:
    """Push the tenant's current autonomy policy to every registered SM.

    Returns the number of sites successfully pushed. Per-site failures
    are logged and skipped (an unreachable SM converges on the next
    fleet-poll cycle).
    """
    from harkeniq_cc.sm_client import SMClient

    client = client or SMClient(config.sm_tls_ca)
    pushed = 0
    async with sessionmaker() as session:
        payload = await build_autonomy_payload(session, config.tenant_id)
        sites = await SiteRepo(session).list_all(config.tenant_id)
        for site in sites:
            try:
                result = await client.push_policy(
                    site.sm_endpoint,
                    site.sm_token,
                    config.tenant_id,
                    site.id,
                    autonomy_budgets_json=payload,
                )
                if result.get("accepted"):
                    pushed += 1
                else:
                    logger.warning(
                        "Policy push refused by %s: %s",
                        site.site_name, result.get("reason", ""),
                    )
            except Exception as exc:
                logger.warning(
                    "Policy push failed for %s: %s", site.site_name, exc
                )
    return pushed
