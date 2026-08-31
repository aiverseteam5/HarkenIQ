"""A3: machine identity — authentication, and only authentication.

An Operational Agent has never held a credential. It is a row evaluated
by a Central Command-resident loop, and its identity is an attribution
string. A3 gives it one that can AUTHENTICATE, and the whole design
problem is doing that without the credential quietly becoming authority.

WHAT A MACHINE IDENTITY ANSWERS
-------------------------------
One question: *who is this runtime?*

It grants no permission, no scope, no capability authority, no autonomy,
no approval authority and no execution authority (A20.2). Those come from
where they already come from: the fixed permission vocabulary,
`cc_scope_grants`, A0 capability bindings, the S5 autonomy contract, the
E0.1 approval ledger, and the node's own funnel.

THE DEFECT THIS MODULE IS DESIGNED AROUND
-----------------------------------------
`governance.load_agent_scope` passes `role_permissions=["*"]`, and its
docstring says why: *"it does not call the HTTP API, the CC-resident
evaluator does."* That was accurate. A credential removes the premise.

Resolved that way, an authenticated agent principal satisfies EVERY route
guard in the platform -- `site.manage`, `tenant.manage`, `role.manage`,
`audit.export`, and `action.approve`. It could approve its own proposals.

So the machine principal never resolves with `["*"]`. It resolves with
the intersection below, and nothing else.
"""

from __future__ import annotations

from typing import Any, Iterable

# ---------------------------------------------------------------------------
# The ceiling (A20.3)
# ---------------------------------------------------------------------------

#: The HARD, INDEPENDENT ceiling on what any machine principal may hold.
#:
#: Deliberately NOT "whatever today's A0 bindings imply". It is its own
#: constant, and `machine_permissions` INTERSECTS with it -- so no future
#: binding, however written or mapped, can widen machine-principal
#: authority. E1.4 learned this shape on a different subject: a custom
#: role bundle used to OR its permissions into the role and could
#: therefore WIDEN it. Bundles now intersect. So does this.
#:
#: Changing this set is a spec amendment, not a code change.
MACHINE_PRINCIPAL_CEILING: frozenset[str] = frozenset({
    "fleet.view",
    "incident.view",
})

#: Which permission each A0 read binding would imply, before the ceiling.
#: Kept separate from the ceiling ON PURPOSE: if this table and the
#: ceiling were one object, adding a binding here would silently raise
#: the ceiling, which is exactly the failure the intersection prevents.
READ_BINDING_PERMISSIONS: dict[str, frozenset[str]] = {
    "attention": frozenset({"fleet.view"}),
    "fleet": frozenset({"fleet.view"}),
    "incidents": frozenset({"incident.view"}),
    # /api/autonomy/ and /api/learning/* are gated on fleet.view today;
    # this table says what the ROUTE demands, never what feels related.
    "autonomy": frozenset({"fleet.view"}),
    "learning": frozenset({"fleet.view"}),
}

#: Species of principal. A human is `user`; an authenticated Operational
#: Agent is `agent`. One `UserContext` carries both -- a second context
#: type would be a second authorization model by the back door.
SPECIES_USER = "user"
SPECIES_AGENT = "agent"

#: Identity status. `revoked` and `retired` both refuse; they are kept
#: apart because "an operator revoked this" and "the agent it belonged to
#: was retired" are different facts an auditor needs to tell apart.
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_RETIRED = "retired"

#: Client ids are minted, never accepted from a caller.
CLIENT_ID_PREFIX = "op-agent-"


def client_id_for(agent_id: str) -> str:
    """The Keycloak client id for an agent. Derived, never chosen."""
    return f"{CLIENT_ID_PREFIX}{agent_id}"


def is_machine_client_id(client_id: str) -> bool:
    """Does this `azp` look like one of ours?

    A shape check only. It decides whether to LOOK UP an identity, never
    whether to trust one: the authoritative answer is a row in
    `cc_agent_identities` whose status is active.
    """
    return bool(client_id) and client_id.startswith(CLIENT_ID_PREFIX)


# ---------------------------------------------------------------------------
# The intersection (A20.3)
# ---------------------------------------------------------------------------


def machine_permissions(read_bindings: Iterable[str]) -> list[str]:
    """What an authenticated agent may do over HTTP.

        effective = A0 read bindings  ∩  MACHINE_PRINCIPAL_CEILING

    Two properties this function exists to guarantee:

    * It can never return `"*"`. A machine principal that held the
      wildcard would satisfy every route guard in the platform.
    * It can never return a permission outside the ceiling, whatever
      `READ_BINDING_PERMISSIONS` grows to contain.

    An unknown binding contributes nothing rather than raising: bindings
    are customer configuration and a name this table does not know is a
    read the agent simply does not get, not a failed request.
    """
    implied: set[str] = set()
    for binding in read_bindings:
        implied |= READ_BINDING_PERMISSIONS.get(str(binding), frozenset())
    return sorted(implied & MACHINE_PRINCIPAL_CEILING)


def ceiling_admits(permission: str) -> bool:
    """Could a machine principal EVER hold this permission? (A20.3.)"""
    return permission in MACHINE_PRINCIPAL_CEILING


# ---------------------------------------------------------------------------
# Authentication verdict (A20.5)
# ---------------------------------------------------------------------------

#: Every reason a presented machine credential is refused. Ordered most
#: absolute first, and each one is a REASON rather than a bare False --
#: an operator debugging a silent agent needs to know which of these it
#: was, and the audit entry records it.
REFUSE_NO_IDENTITY = "no machine identity is registered for this subject"
REFUSE_REVOKED = "this machine identity has been revoked"
REFUSE_RETIRED = "this machine identity belongs to a retired agent"
REFUSE_NO_AGENT = "the agent this identity belongs to no longer exists"
REFUSE_AGENT_RETIRED = "the agent this identity belongs to is retired"
REFUSE_TENANT = "this machine identity belongs to another tenant"
REFUSE_REALM = "this machine identity was issued in another realm"


def authenticate(identity, agent, *, tenant_id: str, realm: str) -> tuple[bool, str]:
    """May this credential act as this agent, right now?

    CC's row is authoritative over the token, and that is the whole point
    of asking here (A20.5). Keycloak access tokens live 300 seconds on
    the reference stack, so disabling the client alone would leave a
    revoked agent authenticated for up to five minutes. The status is
    checked on EVERY request instead, which is what makes revocation
    immediate.

    Returns (allowed, reason). A refusal is always 401 -- this is
    authentication, and saying 403 here would leak that the subject is a
    known identity that merely lacks something.
    """
    if identity is None:
        return False, REFUSE_NO_IDENTITY
    if identity.status == STATUS_REVOKED:
        return False, REFUSE_REVOKED
    if identity.status == STATUS_RETIRED:
        return False, REFUSE_RETIRED
    if identity.tenant_id != tenant_id:
        return False, REFUSE_TENANT
    # E1.4: an identity is a (realm, subject) fact. A subject id from
    # another realm is a different principal, or nobody.
    if (identity.realm or "") != (realm or ""):
        return False, REFUSE_REALM
    if agent is None:
        return False, REFUSE_NO_AGENT
    if getattr(agent, "status", "") == "retired":
        # Belt and braces: retiring an agent revokes its identity, so
        # this should be unreachable. It is checked anyway because an
        # identity that outlived its agent must never authenticate.
        return False, REFUSE_AGENT_RETIRED
    return True, ""


def is_machine(user: Any) -> bool:
    """Is this principal an authenticated Operational Agent?"""
    return getattr(user, "species", SPECIES_USER) == SPECIES_AGENT


# ---------------------------------------------------------------------------
# Aggregate operational visibility (A20.9)
# ---------------------------------------------------------------------------


def aggregate_summary(identities: Iterable[Any]) -> dict:
    """Counts only. No per-agent detail, ever.

    A12.1 stands: platform and vendor staff get NO live tenant-plane
    identity access. What they may have is an operational aggregate, and
    this function is the only thing that produces it -- so "carries no
    identifier" is a property of one function a test can pin, rather than
    a promise about a payload someone assembles by hand.

    Deliberately returns no ids, no names, no client ids and no subjects.
    """
    rows = list(identities)
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    seen = [r.last_seen_at for r in rows if r.last_seen_at is not None]
    return {
        "identities": len(rows),
        "active": by_status.get(STATUS_ACTIVE, 0),
        "revoked": by_status.get(STATUS_REVOKED, 0),
        "retired": by_status.get(STATUS_RETIRED, 0),
        # Freshness as a COUNT, not a timestamp per agent: "how many have
        # been seen" is an operational signal, "when was agent X last
        # seen" is per-agent detail.
        "ever_seen": len(seen),
        "never_seen": len(rows) - len(seen),
        "most_recent_seen_at": (
            max(seen).isoformat() if seen else None
        ),
    }


# ---------------------------------------------------------------------------
# The aggregate reporting loop (A20.9)
# ---------------------------------------------------------------------------


async def identity_summary_loop(state) -> None:
    """Report aggregate identity counts to the platform plane, forever.

    A12.1 is not amended by A3: platform and vendor staff get NO live
    tenant-plane identity access. What they get is this -- counts,
    produced by `aggregate_summary`, sent on the EXISTING internal
    CC->Console channel but on its own endpoint.

    Deliberately not `/usage-events`. That payload feeds
    `MeteringService.ingest_usage_batch` and therefore billing, and an
    operational signal in a billing ingest could corrupt invoicing.
    """
    import asyncio
    import logging

    from harkeniq_cc import identity_client
    from harkeniq_cc.db.repos import AgentIdentityRepo

    log = logging.getLogger("harkeniq.cc.machine_identity")
    interval = float(getattr(state.config, "identity_report_interval_s", 900.0))
    tenant_id = state.config.tenant_id

    while True:
        try:
            if tenant_id and getattr(state.config, "console_url", ""):
                async with state.sessionmaker() as session:
                    rows = await AgentIdentityRepo(session).list_for_tenant(
                        tenant_id
                    )
                summary = aggregate_summary(rows)
                if summary["identities"]:
                    reason = await identity_client.report_summary(
                        state, tenant_id=tenant_id, summary=summary,
                    )
                    if reason:
                        log.warning("identity summary not reported: %s", reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad pass must not kill the loop
            log.exception("identity summary pass failed")
        await asyncio.sleep(interval)
