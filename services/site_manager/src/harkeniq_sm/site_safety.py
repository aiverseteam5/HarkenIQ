"""A site's own safety facts, as the Site Manager emits them (S3-E1-0, A30.36).

A Site Manager emits a site's safety facts in two places: the
`FleetSafetyState` it reports to Central Command for that site, and the
signed lease it issues to a device at that site. S3-E1 will make Central
Command read the first as the TARGET site's local assessment, so both have
to be true of that site and of nothing else.

THE INVARIANT (A30.36): a site-local safety fact at site S -- halt,
suppression, budget window, drop-back -- does not change when only another
site's state changes. The budget windows live in `SMAutonomyEnforcer`
(keyed by site) and drop-back in `sm_error_budgets` (keyed by site since
E0.2); the two facts that needed a per-site READ live here, once, so the
report and the lease cannot choose differently.
"""

from __future__ import annotations

from typing import Any, Optional


def site_halted(halt, enforcer: Optional[Any]) -> bool:
    """Is this site halted -- exactly as the Site Manager ENFORCES it?

    `halt` is the site's `HaltState` (the persisted tenant, site and Site
    Manager-emergency halts in force for it). The enforcer's in-memory flag
    is set only by the Central Command tenant push and the Site
    Manager-local break-glass switch, and the Site Manager's own dispatch
    refuses at EVERY site it serves while it is set, so it halts this site
    too. These are the same two sources `DispatchAction` refuses on
    (`site_stop`, `manager_halt`) and the lease has always carried
    (`_agent_site_halted`): what is reported cannot differ from what is
    enforced. A halt at another site never appears here.
    """
    flag = bool(enforcer is not None and enforcer.stop_switch_active)
    return bool(halt.halted) or flag


async def site_suppressions(session, engine, site_id: str) -> list[dict]:
    """The active suppressions of THIS site's fault domains, in domain order.

    The engine keys a suppression by fault-domain id and carries no site;
    a fault domain belongs to exactly one site (`fault_domains.site_id`).
    Before S3-E1-0 every active suppression was reported under every site
    a Site Manager served, so site B's domain id, trigger and device count
    reached site A's readers and routed site A's proposals to a human.

    A suppression whose fault domain no longer exists belongs to no site
    and is returned for none. The engine's own evaluation, hair-trigger and
    recovery are untouched -- this only decides what is EMITTED for a site.
    """
    if engine is None or not site_id:
        return []
    from harkeniq_sm.db.repos import DomainRepo

    own = {d.id for d in await DomainRepo(session).list_for_site(site_id)}
    active = engine.get_state()["active_suppressions"]
    return [
        {"domain_id": domain_id, **active[domain_id]}
        for domain_id in sorted(active)
        if domain_id in own
    ]
