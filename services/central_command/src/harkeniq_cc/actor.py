"""The ONE canonical actor identity for audit (A23-2, spec A23.7).

``cc_audit_log.actor`` has always been a display string, and it was
written in three forms by different call sites: the Keycloak subject,
the email address, and ``email or subject``. The migration census in
A22.10 compared those strings to subject-keyed grants, so a person who
was granted and recorded by email read as ungranted. Governance decisions
cannot stand on a mixture like that.

:func:`actor_of` is the only definition of "which principal did this".
It returns the STABLE reference -- the same value ``cc_scope_grants.
principal_ref`` and ``cc_approval_records.approver_ref`` already carry:

* a human principal → the Keycloak subject (``UserContext.user_id``);
* a machine principal → the Operational Agent id (``UserContext.user_id``
  is the agent id for a machine, by A3's design, because grants for an
  agent are keyed on it);
* an Operational Agent attribution key (``op-agent:<id>@v<n>``) → the
  agent id, because identity binds to the agent, not the version (A20.7);
* a campaign attribution key (``campaign:<id>@v<n>``) → ``campaign:<id>``,
  versionless, so one campaign is one actor across its edits;
* ``system`` and ``system:*`` → themselves;
* a bare subject that already looks like one → itself;
* an email address → ``None``. An address is a mutable display snapshot;
  it cannot be turned into a subject without the identity provider, and
  guessing would be exactly the fuzzy matching A23.7 forbids.

Email and display name are mutable snapshots and may live in ``actor``
or ``detail``; they are never the identity. There is deliberately no
second helper and no identity class: every writer and every reader asks
this one function.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_OP_AGENT_PREFIX = "op-agent:"
_CAMPAIGN_PREFIX = "campaign:"
_MACHINE_PREFIX = "machine:"


def actor_of(principal: Any) -> Optional[str]:
    """The canonical stable actor reference for `principal`, or ``None``.

    `principal` is a ``UserContext`` (human or machine) or a legacy actor
    string. ``None`` means "this representation cannot be resolved to a
    stable identity" -- a reader must report it as unresolved rather
    than treat it as a different person.
    """
    if principal is None:
        return None
    user_id = getattr(principal, "user_id", None)
    if user_id is not None and hasattr(principal, "tenant_id"):
        # A UserContext. For a human this is the Keycloak subject; for a
        # machine principal A3 sets it to the agent id on purpose, which
        # is what makes the one scope resolver work unchanged.
        return str(user_id) or None
    if not isinstance(principal, str):
        return None
    actor = principal.strip()
    if not actor:
        return None
    if actor.startswith(_OP_AGENT_PREFIX):
        body = actor[len(_OP_AGENT_PREFIX):]
        agent_id = body.partition("@v")[0] if "@v" in body else body
        return agent_id or None
    if actor.startswith(_CAMPAIGN_PREFIX):
        body = actor[len(_CAMPAIGN_PREFIX):]
        campaign_id = body.rpartition("@v")[0] if "@v" in body else body
        return f"{_CAMPAIGN_PREFIX}{campaign_id}" if campaign_id else None
    if actor.startswith(_MACHINE_PREFIX):
        # `machine:<sub>` names a Keycloak service-account subject, not
        # an agent id; the writer that has the identity row passes the
        # agent id explicitly. Without it, unresolved.
        return None
    if actor == "system" or actor.startswith("system:"):
        return actor
    if "@" in actor:
        return None  # an email address: display, never identity
    if _UUID.match(actor) or _HEX32.match(actor):
        return actor
    # Anything else (a seed label, a test fixture name, an operator's
    # free-text id from the lab context) is a legacy display string.
    # It is not an identity we can vouch for.
    return None

