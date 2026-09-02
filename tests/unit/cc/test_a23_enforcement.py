"""A23-1: declared scope treatments are TRUE at runtime (spec A23.2, A23.3, A23.4, A23.5).

The persona matrix (E1.2) asserted 403-versus-not-403 per route and
called that a scope matrix. This file is the other half:

* **The narrowing sweep** -- every declared READ route, driven by a
  strict, site-scoped persona, against an estate seeded with a
  distinguishable object of every kind at an out-of-scope site. The
  response body must never carry an out-of-scope identifier: not a site,
  a device, an incident, a campaign, an agent, a proposal, a grant, an
  approval subject, an outcome, a warranty tag, a candidate skill, a
  learned signal, nor a sibling org unit. Derived from ROUTE_CONTRACT,
  so a new read route is swept the moment it is declared.
* **The mutation probe** -- every declared mutation, driven by a
  narrowed tenant owner (every permission, cluster-A1 reach), against
  an out-of-scope target. Refused as 403 or absent as 404; NEVER 2xx,
  and never 422/400 either, because a probe that fails validation
  before the gate proves nothing.
* **The campaign invariant** -- a one-site campaign preflighted by a
  tenant-wide owner targets ONE site.
* **The realm and metering contracts** -- secure mode refuses an unset
  realm; the usage reporter stays outside user authorization.

Every assertion is an HTTP request against the real ASGI app, or an
execution of the real function. Nothing asserts that a UI hid anything.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCCandidateSkill,
    CCFleetCache,
    CCFleetPattern,
    CCLearnedSignal,
    CCOutcomeHistory,
    CCWarranty,
)
from harkeniq_cc.db.repos import ApprovalRouteRepo, ScopeGrantRepo
from harkeniq_cc.route_contract import (
    OBJECT_GATED,
    READ_SCOPED,
    ROUTE_CONTRACT,
    TENANT_GATED,
    UNSCOPED,
)
from harkeniq_cc.scope import SCOPE_ORG_UNIT, SCOPE_SITE, SCOPE_TENANT

from tests.unit.cc.test_e1_persona_matrix import (
    TENANT,
    _client,
    _grant,
    _stack,
    _strict,
)


# ---------------------------------------------------------------------------
# The estate: one distinguishable object of every kind, at site-1 (in
# scope for the cluster-A1 personas) and at site-3 (out of scope).
# ---------------------------------------------------------------------------


class A23Estate:
    def __init__(self, base):
        self.base = base            # the E1.2 Estate: units, sites, devices
        self.campaigns: dict[str, str] = {}
        self.agents: dict[str, str] = {}
        self.proposals: dict[str, str] = {}
        self.grants: dict[str, str] = {}
        self.grant_principals: dict[str, str] = {}

    @property
    def units(self):
        return self.base.units

    @property
    def sites(self):
        return self.base.sites

    @property
    def devices(self):
        return self.base.devices

    def out_of_scope_identifiers(self) -> list[str]:
        """Everything a cluster-A1 or site-1 principal must never see."""
        return [
            self.sites["site-3"],
            self.units["region_b"],
            self.units["b1"],
            "node-site-3",
            "inc-site-3",
            "act-site-3",
            "oc-site-3",
            "TAG-site-3",
            "cand-site-3",
            "sig-site-3",
            "kc-person-site-3",
            self.campaigns["site-3"],
            self.agents["site-3"],
            self.proposals["site-3"],
            self.grants["site-3"],
        ]


async def _seed_a23(app, sessionmaker, base) -> A23Estate:
    estate = A23Estate(base)
    owner = _client(app, "tenant_owner", "kc-seed-owner")

    async with sessionmaker() as session:
        # Service tags, so warranty rows can be tied to devices.
        for key in ("site-1", "site-2", "site-3"):
            device = (await session.execute(
                __import__("sqlalchemy").select(CCFleetCache).where(
                    CCFleetCache.agent_id == base.devices[key]
                )
            )).scalar_one()
            device.service_tag = f"TAG-{key}"
            session.add(CCWarranty(
                tenant_id=TENANT, service_tag=f"TAG-{key}", vendor="Dell",
                service_level="ProSupport", start_date="2025-01-01",
                end_date="2028-01-01", source="import",
            ))
            session.add(CCCandidateSkill(
                skill_id=f"cand-{key}", tenant_id=TENANT,
                site_id=base.sites[key], yaml_text="name: x",
                source_device=base.devices[key], source_component="disk",
                status="received",
            ))
            session.add(CCOutcomeHistory(
                site_id=base.sites[key], action_id=f"oc-{key}",
                action_type="SEL_CLEAR", device_agent_id=base.devices[key],
                vendor="Dell", model="R750", outcome="success", actor="seed",
            ))
            session.add(CCLearnedSignal(
                tenant_id=TENANT, signal_key=f"sig-{key}", scope_type="site",
                scope_ref=base.sites[key], action_type="SEL_CLEAR",
                vendor="Dell", model="R750", statement=f"signal at {key}",
                evidence={}, confidence=0.6,
            ))
            await ApprovalRouteRepo(session).create(
                site_id=base.sites[key], action_id=f"act-{key}",
                action_type="SEL_CLEAR", device_agent_id=base.devices[key],
            )
            grant = await ScopeGrantRepo(session).grant(
                tenant_id=TENANT, principal_type="user",
                principal_ref=f"kc-person-{key}", scope_type=SCOPE_SITE,
                scope_ref=base.sites[key], role="operator", granted_by="seed",
            )
            estate.grants[key] = grant.id
            estate.grant_principals[key] = f"kc-person-{key}"
        session.add(CCFleetPattern(
            tenant_id=TENANT, pattern_type="cross_site_batch",
            description="SEL_CLEAR failing across sites",
            affected_scope={"vendor": "Dell", "model": "R750",
                            "action_type": "SEL_CLEAR"},
            confidence=0.9,
            evidence={
                "total": 6, "failures": 3,
                "site_failure_counts": {base.sites["site-1"]: 1,
                                        base.sites["site-3"]: 2},
                "sites_affected": 2,
            },
        ))
        await session.commit()

    async with owner:
        for key in ("site-1", "site-3"):
            # A campaign and an operational agent scoped to each site,
            # created through the real API by a tenant-wide owner.
            resp = await owner.post("/api/campaigns/", json={
                "name": f"campaign-{key}", "description": "a23",
                "action_type": "IDENTIFY_LED", "params": {"target": "Drive 0"},
                "scopes": [{"scope_type": "site", "scope_ref": base.sites[key]}],
            })
            assert resp.status_code == 201, resp.text
            estate.campaigns[key] = resp.json()["id"]
            resp = await owner.post(
                f"/api/campaigns/{estate.campaigns[key]}/preflight"
            )
            assert resp.status_code == 200, resp.text

            resp = await owner.post("/api/operational-agents/", json={
                "name": f"agent-{key}", "description": "a23",
                "scopes": [{"scope_type": "site", "scope_ref": base.sites[key]}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
                ],
            })
            assert resp.status_code == 201, resp.text
            estate.agents[key] = resp.json()["id"]

    async with sessionmaker() as session:
        for key in ("site-1", "site-3"):
            proposal = CCAgentProposal(
                tenant_id=TENANT, agent_id=estate.agents[key],
                actor=f"op-agent:{estate.agents[key]}@v1", agent_version=1,
                site_id=base.sites[key], device_agent_id=base.devices[key],
                action_type="SEL_CLEAR", params={}, rationale="a23",
                evidence={}, disposition="requires_approval", status="proposed",
            )
            session.add(proposal)
            await session.flush()
            estate.proposals[key] = proposal.id
        await session.commit()
    return estate


async def _a23_stack():
    app, sessionmaker, base = await _stack()
    estate = await _seed_a23(app, sessionmaker, base)
    await _strict(sessionmaker)
    return app, sessionmaker, estate


#: persona -> (role, scope_type, ref key). Cluster A1 holds site-1 and
#: site-2; site-3 hangs from Cluster B1 under Region B.
SCOPED_READERS = {
    "cluster_manager": ("site_admin", SCOPE_ORG_UNIT, "a1"),
    "site_operator":   ("operator", SCOPE_SITE, "site-1"),
    "cluster_auditor": ("auditor", SCOPE_ORG_UNIT, "a1"),
}

#: The narrowed administrator: every permission a tenant owner holds,
#: reach limited to Cluster A1. What the delegation ceiling is FOR.
CLUSTER_OWNER = ("tenant_owner", SCOPE_ORG_UNIT, "a1")


async def _persona(app, sessionmaker, estate, role, scope_type, ref_key, name):
    user_id = f"kc-{name}"
    ref = ""
    if ref_key and ref_key.startswith("site-"):
        ref = estate.sites[ref_key]
    elif ref_key:
        ref = estate.units[ref_key]
    if scope_type:
        await _grant(sessionmaker, user_id, scope_type, ref, role=role)
    return _client(app, role, user_id)


def _path_for(path: str, estate: A23Estate, key: str) -> str:
    """Substitute every path parameter with the object at `key`'s site."""
    if path.startswith("/api/operational-agents"):
        path = path.replace("{agent_id}", estate.agents[key])
    else:
        path = path.replace("{agent_id}", estate.devices[key])
    return (
        path.replace("{device_id}", estate.devices[key])
        .replace("{site_id}", estate.sites[key])
        .replace("{campaign_id}", estate.campaigns[key])
        .replace("{incident_id}", f"inc-{key}")
        .replace("{action_id}", f"act-{key}")
        .replace("{grant_id}", estate.grants[key])
        .replace("{unit_id}", estate.units["a1" if key == "site-1" else "b1"])
        .replace("{policy_id}", "unknown-policy")
        .replace("{group_id}", "unknown-group")
        .replace("{budget_id}", "unknown-budget")
        .replace("{member_id}", "unknown-member")
        .replace("{transition}", "activate")
    )


READ_ROUTES = sorted(
    (m, p) for (m, p), (_, t, _a) in ROUTE_CONTRACT.items()
    if m == "GET" and t in (READ_SCOPED, UNSCOPED)
)
MUTATION_ROUTES = sorted(
    (m, p) for (m, p), (_, t, _a) in ROUTE_CONTRACT.items()
    if m != "GET" and t in (OBJECT_GATED, TENANT_GATED)
)


# ---------------------------------------------------------------------------
# The narrowing sweep
# ---------------------------------------------------------------------------


class TestNarrowingSweep:
    """Every read route x every scoped persona: no out-of-scope identifier."""

    @pytest.mark.parametrize("persona", sorted(SCOPED_READERS))
    @pytest.mark.asyncio
    async def test_in_scope_reads_carry_no_out_of_scope_identifier(self, persona):
        role, scope_type, ref_key = SCOPED_READERS[persona]
        app, sessionmaker, estate = await _a23_stack()
        client = await _persona(
            app, sessionmaker, estate, role, scope_type, ref_key, persona,
        )
        forbidden = estate.out_of_scope_identifiers()
        leaks: list[str] = []
        async with client:
            for method, path in READ_ROUTES:
                resp = await client.get(_path_for(path, estate, "site-1"))
                if resp.status_code == 403:
                    continue  # the permission gate; the E1.2 sweep owns it
                body = resp.text
                for ident in forbidden:
                    if ident in body:
                        leaks.append(f"{method} {path} -> {ident!r}")
        assert not leaks, (
            f"{persona} received out-of-scope identifiers from routes that "
            "declare READ_SCOPED or UNSCOPED:\n  " + "\n  ".join(leaks)
        )

    @pytest.mark.parametrize("persona", sorted(SCOPED_READERS))
    @pytest.mark.asyncio
    async def test_out_of_scope_objects_read_as_absent(self, persona):
        """Address the site-3 object directly: 404, or a body that says
        nothing about it beyond echoing the id the caller supplied."""
        role, scope_type, ref_key = SCOPED_READERS[persona]
        app, sessionmaker, estate = await _a23_stack()
        client = await _persona(
            app, sessionmaker, estate, role, scope_type, ref_key, persona,
        )
        forbidden = estate.out_of_scope_identifiers()
        leaks: list[str] = []
        async with client:
            for method, path in READ_ROUTES:
                if "{" not in path:
                    continue
                target = _path_for(path, estate, "site-3")
                resp = await client.get(target)
                if resp.status_code in (403, 404):
                    continue
                body = resp.text
                for ident in forbidden:
                    if ident in body and ident not in target:
                        leaks.append(f"{method} {path} ({resp.status_code}) -> {ident!r}")
        assert not leaks, (
            f"{persona} learned about out-of-scope objects by addressing "
            "them:\n  " + "\n  ".join(leaks)
        )

    @pytest.mark.asyncio
    async def test_a_tenant_wide_reader_still_sees_everything(self):
        """The sweep must not pass because the estate is empty."""
        app, sessionmaker, estate = await _a23_stack()
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, "", role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        seen: set[str] = set()
        async with client:
            for key in ("site-1", "site-3"):
                for method, path in READ_ROUTES:
                    resp = await client.get(_path_for(path, estate, key))
                    if resp.status_code == 200:
                        for ident in estate.out_of_scope_identifiers():
                            if ident in resp.text:
                                seen.add(ident)
        # Everything seeded is reachable by SOMEBODY through the API;
        # otherwise the negative assertions above prove nothing. The one
        # exception is the outcome row: /api/outcomes/metrics aggregates
        # and never returns an action id to anybody.
        missing = set(estate.out_of_scope_identifiers()) - seen - {"oc-site-3"}
        assert not missing, f"seeded objects no read route ever returns: {missing}"


# ---------------------------------------------------------------------------
# The mutation probe
# ---------------------------------------------------------------------------


def _probe_body(method: str, path: str, estate: A23Estate) -> dict | None:
    """A VALID body aimed at the out-of-scope site, per route.

    Validity matters: a 422 proves the body was wrong, not that the gate
    held. Every body here passes the route's schema.
    """
    s3 = estate.sites["site-3"]
    b1 = estate.units["b1"]
    if path == "/api/campaigns/":
        return {"name": "probe", "description": "", "action_type": "IDENTIFY_LED",
                "params": {"target": "Drive 0"},
                "scopes": [{"scope_type": "site", "scope_ref": s3}]}
    if path == "/api/campaigns/{campaign_id}/acknowledge":
        return {"confirm": True, "exclude": []}
    if path == "/api/capabilities/catalogue":
        return {"entries": [{"subsystem": "disk", "action_type": "IDENTIFY_LED"}]}
    if path == "/api/approvals/batch":
        return {"action_ids": ["act-site-3"], "decision": "approved"}
    if path == "/api/org-units/" and method == "POST":
        return {"name": "probe", "unit_type": "cluster", "parent_id": b1}
    if path == "/api/org-units/{unit_id}" and method == "PATCH":
        return {"name": "renamed-by-probe"}
    if path == "/api/sites/{site_id}/org-unit":
        return {"org_unit_id": b1}
    if path == "/api/scope-grants/":
        return {"principal_ref": "kc-somebody", "scope_type": "site",
                "scope_ref": s3, "role": "operator"}
    if path == "/api/tenant-settings/scope-enforcement":
        return {"mode": "strict"}
    if path == "/api/sites/register":
        return {"site_name": "probe-site", "sm_endpoint": "sm:50051"}
    if path == "/api/policies/" and method == "POST":
        return {"name": "probe"}
    if path == "/api/policies/{policy_id}":
        return {"name": "probe"}
    if path == "/api/policies/autonomy" and method == "POST":
        return {"device_type": "*", "level": 1}
    if path == "/api/policies/groups" and method == "POST":
        return {"name": "probe"}
    if path == "/api/policies/groups/{group_id}" and method == "PATCH":
        return {"name": "probe"}
    if path == "/api/policies/groups/{group_id}/members" and method == "POST":
        return {"email": "probe@example.com"}
    if path == "/api/policies/stop-switch" and method == "POST":
        return {"reason": "probe"}
    if path == "/api/operational-agents/" and method == "POST":
        return {"name": "probe", "description": "",
                "scopes": [{"scope_type": "site", "scope_ref": s3}],
                "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}]}
    if path == "/api/operational-agents/{agent_id}" and method == "PATCH":
        return {"description": "probe"}
    if path == "/api/operational-agents/{agent_id}/bindings":
        return {"scopes": [{"scope_type": "site", "scope_ref": s3}],
                "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}]}
    if path == "/api/operational-agents/{agent_id}/identity/revoke":
        return {"reason": "probe"}
    if path == "/api/firmware/cve-feed":
        return {"entries": []}
    if path == "/api/warranty/import":
        return {"records": []}
    return None


class TestMutationProbe:
    """Every mutation x the narrowed owner x an out-of-scope target."""

    @pytest.mark.asyncio
    async def test_no_mutation_reaches_an_out_of_scope_target(self):
        app, sessionmaker, estate = await _a23_stack()
        role, scope_type, ref_key = CLUSTER_OWNER
        client = await _persona(
            app, sessionmaker, estate, role, scope_type, ref_key, "cluster-owner",
        )
        reached: list[str] = []
        unproven: list[str] = []
        async with client:
            for method, path in MUTATION_ROUTES:
                target = _path_for(path, estate, "site-3")
                body = _probe_body(method, path, estate)
                kwargs = {"json": body} if body is not None else {}
                resp = await client.request(method, target, **kwargs)
                if path == "/api/approvals/batch" and resp.status_code == 200:
                    # Batch reports per item; every item must have been
                    # refused for scope, none processed.
                    results = resp.json()["results"]
                    if any(r.get("ok") for r in results):
                        reached.append(f"{method} {path} -> processed an item")
                    elif not all(
                        "scope" in json.dumps(r).lower() or "not found" in json.dumps(r).lower()
                        for r in results
                    ):
                        unproven.append(f"{method} {path} -> {results}")
                    continue
                if 200 <= resp.status_code < 300:
                    reached.append(f"{method} {path} -> {resp.status_code}")
                elif resp.status_code not in (403, 404):
                    # 422/400/409 mean the gate was never reached, or was
                    # reached after something else refused. Neither proves
                    # the scope gate.
                    unproven.append(
                        f"{method} {path} -> {resp.status_code} {resp.text[:120]}"
                    )
        assert not reached, (
            "a narrowed owner mutated an out-of-scope target:\n  "
            + "\n  ".join(reached)
        )
        assert not unproven, (
            "these probes did not reach the scope gate (fix the probe body, "
            "or the route refuses for a reason that is not scope):\n  "
            + "\n  ".join(unproven)
        )

    @pytest.mark.asyncio
    async def test_the_same_owner_may_operate_inside_their_cluster(self):
        """The probe is not passing because everything is refused."""
        app, sessionmaker, estate = await _a23_stack()
        role, scope_type, ref_key = CLUSTER_OWNER
        client = await _persona(
            app, sessionmaker, estate, role, scope_type, ref_key, "cluster-owner",
        )
        async with client:
            s1 = estate.sites["site-1"]
            resp = await client.post("/api/campaigns/", json={
                "name": "inside", "description": "", "action_type": "IDENTIFY_LED",
                "params": {"target": "Drive 0"},
                "scopes": [{"scope_type": "site", "scope_ref": s1}],
            })
            assert resp.status_code == 201, resp.text
            cid = resp.json()["id"]
            assert (await client.post(f"/api/campaigns/{cid}/preflight")).status_code == 200
            assert (await client.post(f"/api/campaigns/{cid}/cancel")).status_code == 200
            resp = await client.patch(
                f"/api/operational-agents/{estate.agents['site-1']}",
                json={"description": "edited inside the cluster"},
            )
            assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Campaign target invariant (A23.3)
# ---------------------------------------------------------------------------


class TestCampaignTargetInvariant:
    @pytest.mark.asyncio
    async def test_a_one_site_campaign_by_a_tenant_owner_targets_one_site(self):
        """The union that made a one-site campaign a whole-estate one."""
        app, sessionmaker, estate = await _a23_stack()
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, "", role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.get(
                f"/api/campaigns/{estate.campaigns['site-1']}/targets"
            )
            assert resp.status_code == 200
            sites = {t["site_id"] for t in resp.json()["targets"]}
            devices = {t["device_agent_id"] for t in resp.json()["targets"]}
        assert sites == {estate.sites["site-1"]}, sites
        assert devices == {"node-site-1"}, devices

    @pytest.mark.asyncio
    async def test_an_org_unit_rule_expands_to_its_own_sites_and_no_more(self):
        app, sessionmaker, estate = await _a23_stack()
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, "", role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.post("/api/campaigns/", json={
                "name": "region-a", "description": "", "action_type": "IDENTIFY_LED",
                "params": {"target": "Drive 0"},
                "scopes": [{"scope_type": "org_unit",
                            "scope_ref": estate.units["region_a"]}],
            })
            assert resp.status_code == 201, resp.text
            cid = resp.json()["id"]
            assert (await client.post(f"/api/campaigns/{cid}/preflight")).status_code == 200
            resp = await client.get(f"/api/campaigns/{cid}/targets")
            devices = {t["device_agent_id"] for t in resp.json()["targets"]}
        assert devices == {"node-site-1", "node-site-2"}, devices

    @pytest.mark.asyncio
    async def test_caller_scope_may_constrain_and_never_enlarge(self):
        """The runner's intersection, executed directly."""
        from types import SimpleNamespace as NS

        from harkeniq_cc.scope import expand_rules_to_site_ids, resolve

        units = [NS(id="root", path="/root/"), NS(id="a1", path="/root/a1/")]
        sites = [NS(id="s1", org_unit_id="a1"), NS(id="s2", org_unit_id="a1"),
                 NS(id="s3", org_unit_id="root")]
        rules = [NS(scope_type="site", scope_ref="s1")]
        # The campaign's own reach never includes the caller's.
        assert expand_rules_to_site_ids(rules, units, sites) == {"s1"}
        region = [NS(scope_type="org_unit", scope_ref="a1")]
        assert expand_rules_to_site_ids(region, units, sites) == {"s1", "s2"}
        gone = [NS(scope_type="org_unit", scope_ref="vanished")]
        assert expand_rules_to_site_ids(gone, units, sites) == frozenset()

        # A caller narrower than the campaign constrains it.
        caller = resolve(
            tenant_id="t", principal_type="user", principal_ref="u",
            role_permissions=["site.manage"],
            grant_rows=[NS(scope_type="site", scope_ref="s1",
                           permission_subset=None, revoked_at=None, expires_at=None)],
            org_units=units, sites=sites, enforcement="strict",
        )
        assert caller.covers_device("d1", "s1", "server")
        assert not caller.covers_device("d2", "s2", "server")


# ---------------------------------------------------------------------------
# Operational agents and grants are scoped objects
# ---------------------------------------------------------------------------


class TestScopedObjects:
    @pytest.mark.asyncio
    async def test_an_out_of_scope_agent_is_absent_not_forbidden(self):
        app, sessionmaker, estate = await _a23_stack()
        client = await _persona(
            app, sessionmaker, estate, "site_admin", SCOPE_ORG_UNIT, "a1", "cm",
        )
        async with client:
            listed = {a["id"] for a in (await client.get("/api/operational-agents/")).json()["agents"]}
            assert estate.agents["site-1"] in listed
            assert estate.agents["site-3"] not in listed
            for sub in ("", "/proposals", "/preflight", "/runtime", "/identity", "/dry-run"):
                resp = await client.get(f"/api/operational-agents/{estate.agents['site-3']}{sub}")
                assert resp.status_code == 404, (sub, resp.status_code, resp.text[:100])
            resp = await client.get(f"/api/operational-agents/{estate.agents['site-1']}")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_grant_listing_is_narrowed_to_what_the_caller_could_have_made(self):
        app, sessionmaker, estate = await _a23_stack()
        client = await _persona(
            app, sessionmaker, estate, "site_admin", SCOPE_ORG_UNIT, "a1", "cm",
        )
        async with client:
            rows = (await client.get("/api/scope-grants/")).json()["grants"]
        refs = {g["scope_ref"] for g in rows}
        assert estate.sites["site-1"] in refs
        assert estate.sites["site-3"] not in refs
        assert all(g["scope_type"] != "tenant" for g in rows)


# ---------------------------------------------------------------------------
# Realm (A23.4) and metering (A23.5)
# ---------------------------------------------------------------------------


class TestSecureRealm:
    def test_secure_mode_refuses_an_unset_realm(self):
        from harkeniq_cc.config import CCConfig

        errors = CCConfig(tenant_id="t", insecure=False, keycloak_realm="").validate()
        assert any("keycloak_realm is required" in e for e in errors), errors

    def test_lab_mode_still_boots_without_one(self):
        from harkeniq_cc.config import CCConfig

        assert CCConfig(tenant_id="t", insecure=True, keycloak_realm="").validate() == []

    def test_create_app_has_no_platform_realm_fallback(self):
        import harkeniq_cc.app as app_module

        source = inspect.getsource(app_module.create_app)
        assert 'or "harkeniq-platform"' not in source
        assert "keycloak_realm or" not in source


class TestMeteringIsScopeFree:
    """A23.5: `scope=None` on the billing path is a contract, not a gap."""

    def test_the_usage_reporter_never_touches_user_authorization(self):
        import harkeniq_cc.usage_reporter as reporter

        tree = ast.parse(inspect.getsource(reporter))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not imported & {"get_scope", "load_scope", "resolve", "ResolvedScope"}, imported
        assert "scope=" not in inspect.getsource(reporter)

    @pytest.mark.asyncio
    async def test_a_strict_grantless_tenant_still_reports_every_site(self):
        from harkeniq_cc.db.repos import SiteRepo
        from harkeniq_cc.scope import empty_scope

        app, sessionmaker, estate = await _a23_stack()   # strict, no grants
        async with sessionmaker() as session:
            for_billing = await SiteRepo(session).list_all(TENANT)            # scope=None
            for_a_user = await SiteRepo(session).list_all(TENANT, scope=empty_scope(TENANT))
        assert {s.id for s in for_billing} == set(estate.sites.values())
        assert for_a_user == []

    def test_the_payload_shape_carries_the_full_count(self):
        from harkeniq_cc.usage_reporter import build_console_usage_payload

        payload = build_console_usage_payload("t", "DC-1", "2026-09-01", {"node_count": 3})
        assert payload["events"][0]["node_count"] == 3
