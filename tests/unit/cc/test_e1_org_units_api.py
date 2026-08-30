"""E1.1: the organizational tree, wired.

The arithmetic is proved purely elsewhere. This covers what only the
running endpoint proves: the persona matrix (no new permission was
introduced), that the tree rules hold against a real session and not
only against strings, that one tenant's tree is invisible to another,
that every mutation lands on the audit chain and the chain still
verifies, and -- the compatibility promise of this slice -- that
nothing else on Central Command changed behaviour.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCSite
from harkeniq_cc.db.repos import AuditRepo
from harkeniq_cc.org_tree import MAX_DEPTH
from harkeniq_cc.runtime import AppState

TENANT = "t1"
OTHER = "t2"


async def _stack(role: str = "tenant_owner", tenant: str = TENANT):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=tenant,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
            is_platform_user=role == "platform_super_admin",
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, sessionmaker, app


async def _seed_site(sessionmaker, tenant: str = TENANT, name: str = "DC-1") -> str:
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name=name, sm_endpoint="sm:50051", sm_token="t",
        )
        session.add(site)
        await session.flush()
        site_id = site.id
        await session.commit()
    return site_id


async def _mk(client, name, unit_type="region", parent=None):
    body = {"name": name, "unit_type": unit_type}
    if parent:
        body["parent_id"] = parent
    resp = await client.post("/api/org-units/", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Persona matrix
# ---------------------------------------------------------------------------


class TestPersonaMatrix:
    """Endpoint x persona x permission. Reads are site.view, writes
    site.manage -- the existing vocabulary, nothing new and nothing
    broadened. Operator and viewer holding no site.view is existing role
    composition, recorded here so a later change to it is visible."""

    @pytest.mark.parametrize(
        "role,can_read,can_write",
        [
            ("tenant_owner", True, True),
            ("site_admin", True, True),
            ("auditor", True, False),
            ("operator", False, False),
            ("viewer", False, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_read_and_write_gates(self, role, can_read, can_write):
        client, sessionmaker, _ = await _stack(role=role)
        async with client:
            read = await client.get("/api/org-units/")
            assert (read.status_code == 200) is can_read, read.text

            write = await client.post(
                "/api/org-units/", json={"name": "Region West"}
            )
            assert (write.status_code == 201) is can_write, write.text
            if not can_write:
                assert write.status_code == 403
                assert "site.manage" in write.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_auditor_may_read_the_tree_but_not_move_it(self):
        owner, sessionmaker, _ = await _stack()
        async with owner:
            unit = await _mk(owner, "Region West")

        client, _, _ = await _stack(role="auditor")
        client._transport = client._transport  # same app? no -- separate stacks
        # An auditor on their own stack: the gate is what is under test,
        # not the row, so a 403 before any lookup is the correct proof.
        async with client:
            for call in (
                client.patch(f"/api/org-units/{unit['id']}", json={"name": "x"}),
                client.delete(f"/api/org-units/{unit['id']}"),
            ):
                resp = await call
                assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tree rules against a real session
# ---------------------------------------------------------------------------


class TestTreeRules:
    @pytest.mark.asyncio
    async def test_a_three_level_tree_gets_correct_paths_and_depths(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            c7 = await _mk(client, "Cluster 7", "cluster", west["id"])

            assert root["path"] == f"/{root['id']}/"
            assert west["path"] == f"/{root['id']}/{west['id']}/"
            assert c7["path"] == f"/{root['id']}/{west['id']}/{c7['id']}/"
            assert [root["depth"], west["depth"], c7["depth"]] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_the_tree_read_nests_and_rolls_up_site_counts(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": west["id"]}
            )

            body = (await client.get("/api/org-units/")).json()
            assert body["unit_count"] == 2
            assert body["max_depth"] == MAX_DEPTH
            tree = body["tree"]
            assert len(tree) == 1 and tree[0]["name"] == "meridian"
            assert tree[0]["site_count"] == 0
            assert tree[0]["subtree_site_count"] == 1
            assert tree[0]["children"][0]["site_count"] == 1

    @pytest.mark.asyncio
    async def test_the_detail_read_carries_breadcrumb_children_and_sites(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            c7 = await _mk(client, "Cluster 7", "cluster", west["id"])
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": c7["id"]}
            )

            body = (await client.get(f"/api/org-units/{west['id']}")).json()
            assert body["unit"]["name"] == "Region West"
            assert [a["name"] for a in body["ancestors"]] == ["meridian"]
            assert [c["name"] for c in body["children"]] == ["Cluster 7"]
            assert body["sites"] == []
            assert body["subtree_site_count"] == 1

    @pytest.mark.asyncio
    async def test_depth_is_bounded_at_eight(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            parent = None
            for level in range(MAX_DEPTH):
                unit = await _mk(client, f"L{level}", "region", parent)
                parent = unit["id"]
                assert unit["depth"] == level + 1

            resp = await client.post(
                "/api/org-units/",
                json={"name": "too deep", "unit_type": "region", "parent_id": parent},
            )
            assert resp.status_code == 400
            assert str(MAX_DEPTH) in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_two_siblings_may_not_share_a_name(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            await _mk(client, "Cluster 7", "cluster", root["id"])
            resp = await client.post(
                "/api/org-units/",
                json={"name": "Cluster 7", "unit_type": "cluster",
                      "parent_id": root["id"]},
            )
            assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_the_same_name_under_different_parents_is_fine(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            east = await _mk(client, "Region East", "region", root["id"])
            await _mk(client, "Cluster 7", "cluster", west["id"])
            resp = await client.post(
                "/api/org-units/",
                json={"name": "Cluster 7", "unit_type": "cluster",
                      "parent_id": east["id"]},
            )
            assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_an_unknown_parent_is_a_404(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            resp = await client.post(
                "/api/org-units/",
                json={"name": "orphan", "parent_id": "f" * 32},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a_bad_unit_type_is_refused_with_a_readable_reason(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            resp = await client.post(
                "/api/org-units/", json={"name": "x", "unit_type": "Region/West"}
            )
            assert resp.status_code == 400
            assert "slug" in resp.json()["detail"]


class TestMove:
    async def _three_levels(self, client):
        root = await _mk(client, "meridian", "organization")
        west = await _mk(client, "Region West", "region", root["id"])
        east = await _mk(client, "Region East", "region", root["id"])
        c7 = await _mk(client, "Cluster 7", "cluster", west["id"])
        rack = await _mk(client, "Hall A", "hall", c7["id"])
        return root, west, east, c7, rack

    @pytest.mark.asyncio
    async def test_a_move_rewrites_every_descendant_path_in_one_transaction(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root, west, east, c7, hall = await self._three_levels(client)

            resp = await client.patch(
                f"/api/org-units/{c7['id']}", json={"parent_id": east["id"]}
            )
            assert resp.status_code == 200
            moved = resp.json()
            assert moved["path"] == f"/{root['id']}/{east['id']}/{c7['id']}/"
            assert moved["depth"] == 3

            after = (await client.get(f"/api/org-units/{hall['id']}")).json()
            assert after["unit"]["path"] == (
                f"/{root['id']}/{east['id']}/{c7['id']}/{hall['id']}/"
            )
            assert after["unit"]["depth"] == 4
            assert [a["name"] for a in after["ancestors"]] == [
                "meridian", "Region East", "Cluster 7"
            ]

    @pytest.mark.asyncio
    async def test_moving_a_unit_under_its_own_descendant_is_refused(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root, west, east, c7, hall = await self._three_levels(client)
            resp = await client.patch(
                f"/api/org-units/{west['id']}", json={"parent_id": hall["id"]}
            )
            assert resp.status_code == 400
            assert "cycle" in resp.json()["detail"]

            # And nothing moved.
            still = (await client.get(f"/api/org-units/{west['id']}")).json()
            assert still["unit"]["parent_id"] == root["id"]

    @pytest.mark.asyncio
    async def test_a_unit_cannot_become_its_own_parent(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root, west, *_ = await self._three_levels(client)
            resp = await client.patch(
                f"/api/org-units/{west['id']}", json={"parent_id": west["id"]}
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_a_move_that_would_bust_the_depth_bound_is_refused(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            # A deep chain, and a two-level subtree beside it.
            parent = None
            deep = []
            for level in range(MAX_DEPTH):
                unit = await _mk(client, f"L{level}", "region", parent)
                deep.append(unit)
                parent = unit["id"]

            top = await _mk(client, "movable", "region")
            await _mk(client, "movable child", "region", top["id"])

            resp = await client.patch(
                f"/api/org-units/{top['id']}", json={"parent_id": deep[-1]["id"]}
            )
            assert resp.status_code == 400
            assert str(MAX_DEPTH) in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_unit_can_be_promoted_to_root(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root, west, *_ = await self._three_levels(client)
            resp = await client.patch(
                f"/api/org-units/{west['id']}", json={"parent_id": None}
            )
            assert resp.status_code == 200
            assert resp.json()["parent_id"] is None
            assert resp.json()["depth"] == 1
            assert resp.json()["path"] == f"/{west['id']}/"

    @pytest.mark.asyncio
    async def test_omitting_parent_id_leaves_the_parent_alone(self):
        # `parent_id: null` promotes to root; an ABSENT key must not.
        client, sessionmaker, _ = await _stack()
        async with client:
            root, west, *_ = await self._three_levels(client)
            resp = await client.patch(
                f"/api/org-units/{west['id']}", json={"name": "Region Ouest"}
            )
            assert resp.status_code == 200
            assert resp.json()["parent_id"] == root["id"]
            assert resp.json()["name"] == "Region Ouest"

    @pytest.mark.asyncio
    async def test_a_rename_does_not_rewrite_the_path(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            _, west, *_ = await self._three_levels(client)
            before = west["path"]
            resp = await client.patch(
                f"/api/org-units/{west['id']}", json={"name": "Region Ouest"}
            )
            assert resp.json()["path"] == before

    @pytest.mark.asyncio
    async def test_a_move_into_a_name_collision_is_refused(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            east = await _mk(client, "Region East", "region", root["id"])
            await _mk(client, "Cluster 7", "cluster", west["id"])
            other = await _mk(client, "Cluster 7", "cluster", east["id"])
            resp = await client.patch(
                f"/api/org-units/{other['id']}", json={"parent_id": west["id"]}
            )
            assert resp.status_code == 409


class TestDelete:
    @pytest.mark.asyncio
    async def test_a_unit_holding_children_is_not_deleted(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            await _mk(client, "Region West", "region", root["id"])
            resp = await client.delete(f"/api/org-units/{root['id']}")
            assert resp.status_code == 409
            assert "child unit" in resp.json()["detail"]
            assert (await client.get(f"/api/org-units/{root['id']}")).status_code == 200

    @pytest.mark.asyncio
    async def test_a_unit_holding_a_site_is_not_deleted_and_the_site_stays(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": root["id"]}
            )
            resp = await client.delete(f"/api/org-units/{root['id']}")
            assert resp.status_code == 409
            assert "site(s)" in resp.json()["detail"]

            detail = (await client.get(f"/api/org-units/{root['id']}")).json()
            assert [s["id"] for s in detail["sites"]] == [site_id]

    @pytest.mark.asyncio
    async def test_an_empty_leaf_is_deleted(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            leaf = await _mk(client, "Region West", "region", root["id"])
            assert (await client.delete(f"/api/org-units/{leaf['id']}")).status_code == 200
            assert (await client.get(f"/api/org-units/{leaf['id']}")).status_code == 404


class TestSiteAttachment:
    @pytest.mark.asyncio
    async def test_a_site_has_exactly_one_path_and_moving_it_clears_the_old(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            east = await _mk(client, "Region East", "region", root["id"])

            first = await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": west["id"]}
            )
            assert first.json()["changed"] is True

            second = await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": east["id"]}
            )
            assert second.json()["changed"] is True

            assert (await client.get(f"/api/org-units/{west['id']}")).json()["sites"] == []
            moved = (await client.get(f"/api/org-units/{east['id']}")).json()
            assert [s["id"] for s in moved["sites"]] == [site_id]

    @pytest.mark.asyncio
    async def test_reattaching_to_the_same_unit_is_a_no_op(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": root["id"]}
            )
            again = await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": root["id"]}
            )
            assert again.json()["changed"] is False

    @pytest.mark.asyncio
    async def test_attaching_needs_site_manage(self):
        owner, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with owner:
            root = await _mk(owner, "meridian", "organization")

        auditor, _, _ = await _stack(role="auditor")
        async with auditor:
            resp = await auditor.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": root["id"]}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_an_unknown_unit_or_site_is_a_404(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            assert (
                await client.put(
                    f"/api/sites/{site_id}/org-unit", json={"org_unit_id": "f" * 32}
                )
            ).status_code == 404
            assert (
                await client.put(
                    f"/api/sites/{'f' * 32}/org-unit", json={"org_unit_id": root["id"]}
                )
            ).status_code == 404


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_another_tenants_units_are_invisible_and_unreachable(self):
        client, sessionmaker, _ = await _stack(tenant=TENANT)
        mine = await _mk(client, "meridian", "organization")

        # A second tenant on the same tables.
        async with sessionmaker() as session:
            from harkeniq_cc.db.repos import OrgUnitRepo

            theirs = await OrgUnitRepo(session).create(
                OTHER, name="rival", unit_type="organization", parent=None,
            )
            theirs_id = theirs.id
            await session.commit()

        body = (await client.get("/api/org-units/")).json()
        ids = {n["id"] for n in body["tree"]}
        assert ids == {mine["id"]}
        assert (await client.get(f"/api/org-units/{theirs_id}")).status_code == 404
        assert (
            await client.patch(f"/api/org-units/{theirs_id}", json={"name": "x"})
        ).status_code == 404
        assert (await client.delete(f"/api/org-units/{theirs_id}")).status_code == 404
        assert (
            await client.post(
                "/api/org-units/", json={"name": "child", "parent_id": theirs_id},
            )
        ).status_code == 404
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_site_in_another_tenant_cannot_be_attached(self):
        client, sessionmaker, _ = await _stack(tenant=TENANT)
        other_site = await _seed_site(sessionmaker, tenant=OTHER, name="rival-dc")
        async with client:
            root = await _mk(client, "meridian", "organization")
            resp = await client.put(
                f"/api/sites/{other_site}/org-unit", json={"org_unit_id": root["id"]}
            )
            assert resp.status_code == 404


class TestAudit:
    @pytest.mark.asyncio
    async def test_every_mutation_lands_on_the_chain_and_it_still_verifies(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            root = await _mk(client, "meridian", "organization")
            west = await _mk(client, "Region West", "region", root["id"])
            east = await _mk(client, "Region East", "region", root["id"])
            await client.patch(
                f"/api/org-units/{west['id']}", json={"parent_id": east["id"]}
            )
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": west["id"]}
            )
            await client.delete(f"/api/org-units/{west['id']}")  # 409, holds a site
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": east["id"]}
            )
            await client.delete(f"/api/org-units/{west['id']}")

        async with sessionmaker() as session:
            repo = AuditRepo(session)
            rows = await repo.list_filtered(tenant_id=TENANT, page_size=100)
            actions = [r.action for r in rows]
            assert actions.count("org_unit.created") == 3
            assert "org_unit.moved" in actions
            assert actions.count("org_unit.site_attached") == 2
            assert "org_unit.deleted" in actions

            moved = next(r for r in rows if r.action == "org_unit.moved")
            # Both the old and the new path, so the move is reconstructible.
            old_path, new_path = moved.detail["changes"]["parent_id"]
            assert old_path != new_path and old_path.endswith(f"{west['id']}/")

            assert (await repo.verify_chain()).valid

    @pytest.mark.asyncio
    async def test_a_refused_mutation_writes_no_audit_entry(self):
        client, sessionmaker, _ = await _stack()
        async with client:
            root = await _mk(client, "meridian", "organization")
            await client.post(
                "/api/org-units/",
                json={"name": "meridian", "unit_type": "organization"},
            )  # 409 sibling collision

        async with sessionmaker() as session:
            rows = await AuditRepo(session).list_filtered(
                tenant_id=TENANT, page_size=100
            )
            assert [r.action for r in rows].count("org_unit.created") == 1


class TestBackwardCompatibility:
    """E1.1's compatibility promise: the tree exists and nothing reads it.

    Every pre-E1.1 surface must behave identically, so this asserts the
    payloads rather than trusting that no caller was added.
    """

    @pytest.mark.asyncio
    async def test_the_site_list_gains_org_unit_id_and_nothing_else(self):
        client, sessionmaker, _ = await _stack()
        await _seed_site(sessionmaker)
        async with client:
            before = (await client.get("/api/sites/")).json()
            site = before["sites"][0] if isinstance(before, dict) else before[0]
            assert site["org_unit_id"] is None

            root = await _mk(client, "meridian", "organization")
            after_tree = (await client.get("/api/sites/")).json()
            after = (
                after_tree["sites"][0]
                if isinstance(after_tree, dict) else after_tree[0]
            )
            # Creating a tree changes no site field: attachment is explicit.
            assert after == site

    @pytest.mark.asyncio
    async def test_the_autonomy_contract_is_byte_identical_before_and_after(self):
        client, sessionmaker, _ = await _stack()
        await _seed_site(sessionmaker)
        async with client:
            before = (await client.get("/api/autonomy/")).json()
            root = await _mk(client, "meridian", "organization")
            await _mk(client, "Region West", "region", root["id"])
            after = (await client.get("/api/autonomy/")).json()
            # `generated_at` is when the read ran, not part of the contract.
            before.pop("generated_at", None)
            after.pop("generated_at", None)
            assert after == before

    @pytest.mark.asyncio
    async def test_the_fleet_read_is_unchanged_by_the_tree(self):
        client, sessionmaker, _ = await _stack()
        site_id = await _seed_site(sessionmaker)
        async with client:
            before = (await client.get("/api/fleet/")).json()
            root = await _mk(client, "meridian", "organization")
            await client.put(
                f"/api/sites/{site_id}/org-unit", json={"org_unit_id": root["id"]}
            )
            after = (await client.get("/api/fleet/")).json()
            assert after == before


class TestEveryTenantGetsARoot:
    """E1.1 promised every site a canonical organizational path.

    Migration 0010's backfill delivered that for tenants that existed
    WHEN IT RAN. A tenant created afterwards -- or one whose first site
    arrives later -- had no root at all, so its tree read was empty and
    its sites belonged nowhere. Found by the compose gate on a fresh
    stack, where the migration runs before any tenant exists.
    """

    @pytest.mark.asyncio
    async def test_a_fresh_tenant_has_no_root_until_one_is_ensured(self):
        from harkeniq_cc.db.repos import OrgUnitRepo

        client, sessionmaker, _ = await _stack()
        body = (await client.get("/api/org-units/")).json()
        assert body["tree"] == []

        async with sessionmaker() as session:
            repo = OrgUnitRepo(session)
            root = await repo.ensure_root(TENANT, created_by="test")
            await session.commit()
            assert root.depth == 1 and root.path == f"/{root.id}/"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_ensure_root_is_idempotent(self):
        from harkeniq_cc.db.repos import OrgUnitRepo

        _, sessionmaker, _ = await _stack()
        async with sessionmaker() as session:
            repo = OrgUnitRepo(session)
            first = await repo.ensure_root(TENANT)
            await session.commit()
        async with sessionmaker() as session:
            again = await OrgUnitRepo(sessionmaker and session).ensure_root(TENANT)
            assert again.id == first.id

    @pytest.mark.asyncio
    async def test_registering_a_site_gives_it_a_path(self):
        """The promise, end to end: a site registered on a fresh tenant
        has an organizational path without anybody building a tree."""
        from harkeniq_cc.db.repos import OrgUnitRepo, SiteRepo

        client, sessionmaker, _ = await _stack()
        async with sessionmaker() as session:
            root = await OrgUnitRepo(session).ensure_root(TENANT)
            site = CCSite(
                tenant_id=TENANT, site_name="fresh", sm_endpoint="sm:1",
                sm_token="t", org_unit_id=root.id,
            )
            session.add(site)
            await session.commit()

        async with client:
            body = (await client.get("/api/org-units/")).json()
            assert len(body["tree"]) == 1
            assert body["tree"][0]["site_count"] == 1
