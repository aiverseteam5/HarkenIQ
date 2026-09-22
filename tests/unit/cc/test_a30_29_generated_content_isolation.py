"""A30.29 at Central Command: generated content inherits its projection.

The review's HIGH, reproduced on `540a0cd`: a site-A principal read
"... across 3 sites (35/54)" through an incident's generated `summary` and
through a candidate's `yaml_text`, because A30.28 bounded the pattern
CITATION and left the PROSE the pattern was quoted into as stored.

What is proven here, on the production app over the S3/S4 estate under
STRICT with persisted grants:

* the HISTORICAL ATTACK (Vinod's §12): a row with no provenance whose every
  generated field carries a hidden-site sentinel -- a site human, a device
  human, a device-class human and a machine principal get the row where
  authorized, its safe local facts, and neither sentinel; the tenant-wide
  reader gets the original;
* the NEW-WRITE MATRIX (§13): marked for A -> site-A, org-AB and tenant see
  it; a reader who holds the row and not `fleet.view` at A, an org-CD
  reader and a site-B reader do not; revoke A -> withheld; restore -> shown;
* PROVIDER INDEPENDENCE (§15): a provider that populates every generated
  field, and one this slice never names, leaves nothing;
* the WIRE: the marker survives SM -> CC through a real servicer, and an
  older Site Manager's empty field arrives as None, never as a marker;
* STRUCTURE: no route reads a generated field off a row except through the
  projection.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timezone

import grpc
import pytest

import harkeniq_cc
from harkeniq.generation_provenance import KEY, site_visibility, tenant_visibility
from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc import learning_projection as LP
from harkeniq_cc.api import incidents as incidents_api
from harkeniq_cc.db.models import CCCandidateSkill, CCIncident
from harkeniq_cc.db.repos import CandidateSkillRepo
from harkeniq_cc.governance import LearningView
from harkeniq_cc.sm_client import SMClient
from tests.unit.cc import s3_estate as E
from tests.unit.cc import s4_estate as S
from tests.unit.cc.s3_estate import SITES

SECRET = "SECRET_SITE_C"
PHRASE = "30 of 40 attempts across 2 sites"
#: A field no provider of today writes and this slice never names. A
#: future provider may; it must not pass either.
UNNAMED_FIELD = "operator_notes"


def _hostile_explanation(marker=None) -> dict:
    """Every generated field poisoned, plus one unnamed one; the device's
    own telemetry cited beside it (which must survive)."""
    explanation = {
        "provider": "llm", "confidence": 0.8,
        "summary": f"Fleet-wide: {SECRET} -- {PHRASE}",
        "suggested_action": f"Replace the PSU as at {SECRET}",
        "reasoning_steps": [f"{PHRASE}", f"compared with {SECRET}"],
        UNNAMED_FIELD: f"{SECRET} {PHRASE}",
        "evidence_cited": [S.INCIDENT_TELEMETRY],
        "similar_past_incidents": [{"action_id": "act-own-1", "action_type": "SEL_CLEAR"}],
    }
    if marker is not None:
        explanation[KEY] = marker
    return explanation


async def _seed(stack, key="A", *, marker=None, tag="") -> tuple[str, str]:
    """One incident and one candidate at site `key`, generated fields
    poisoned, provenance as given (None = historical, no marker)."""
    incident_id = stack.tagged(f"a3029-inc-{key.lower()}{tag}")
    skill_id = stack.tagged(f"a3029-cand-{key.lower()}{tag}")
    now = datetime.now(timezone.utc)
    async with stack.sessionmaker() as session:
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=stack.tenant,
            site_id=stack.site(key), kind="device", status="open",
            title="Fan duty rising", device_agent_id=stack.device(key),
            subsystem="fan", confidence=0.9,
            explanation=_hostile_explanation(marker),
        ))
        session.add(CCCandidateSkill(
            skill_id=skill_id, tenant_id=stack.tenant, site_id=stack.site(key),
            source_device=stack.device(key), source_component="fan:Fan1",
            validation_state="valid", dry_run_matches=2, status="received",
            warnings=[f"rule message quotes {SECRET}", PHRASE],
            generated_at=now, received_at=now,
            yaml_text=f"name: fan-x\nversion: 1\ntarget: fan\ndescription: {SECRET} {PHRASE}\nrules: []\n",
            generation_visibility=marker,
        ))
        await session.commit()
    return incident_id, skill_id


def _leaves(payload, path=""):
    if isinstance(payload, dict):
        for k, v in payload.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, payload


def _sentinels_in(payload) -> list[str]:
    """Every leaf -- and every KEY -- carrying either sentinel."""
    hits = [p for p, v in _leaves(payload) if SECRET in str(v) or PHRASE in str(v)]
    hits += [p for p, _ in _leaves(payload) if SECRET in p or PHRASE in p]
    return hits


async def _reads(stack, subject, role, incident_id):
    """(list body or None, detail body or None, candidates body or None)."""
    who = stack.as_person(subject, role)
    listed = await who.get("/api/incidents/", status="all")
    detail = await who.get(f"/api/incidents/{incident_id}")
    cands = await who.get("/api/learning/candidates")
    return (
        listed.json() if listed.status_code == 200 else None,
        detail.json() if detail.status_code == 200 else None,
        cands.json() if cands.status_code == 200 else None,
    )


async def _machine(stack, keys, *, name) -> str:
    from harkeniq_cc.db.models import CCOperationalAgent

    async with stack.as_person().client() as client:
        res = await client.post("/api/operational-agents/", json={
            "name": name,
            "scopes": [{"scope_type": "site", "scope_ref": stack.site(k)} for k in keys],
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        })
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()
    return agent_id


def _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id):
    """The row where present, its local facts, and NEITHER sentinel."""
    for body in (listed, detail, cands):
        if body is not None:
            assert _sentinels_in(body) == [], _sentinels_in(body)
    if detail is not None:
        assert detail["incident_id"] == incident_id
        assert detail["title"] == "Fan duty rising"                      # local fact
        diag = detail["diagnosis"]
        assert diag["generated"] == LP.WITHHELD_GENERATED_BLOCK
        assert diag["generation_visibility"] is None
        assert diag["evidence_cited"][0] == S.INCIDENT_TELEMETRY         # local fact
        assert diag["similar_past_incidents"][0]["action_id"] == "act-own-1"
        assert diag["origin"] == "llm" and diag["trust"] == "untrusted_generated"
    if listed is not None:
        mine = [i for i in listed["incidents"] if i["incident_id"] == incident_id]
        for i in mine:
            assert i["diagnosis"]["generated"] == LP.WITHHELD_GENERATED_BLOCK
    if cands is not None:
        mine = [c for c in cands["candidates"] if c["skill_id"] == skill_id]
        for c in mine:
            assert c["yaml_text"] == "" and c["warnings"] == []
            assert c["generated_withheld"] is True and c["generation_visibility"] is None
            assert c["dry_run_matches"] == 2 and c["validation_state"] == "valid"  # local facts


def _assert_visible(detail, cands, skill_id, marker):
    diag = detail["diagnosis"]
    assert SECRET in diag["generated"]["summary"] and PHRASE in diag["generated"]["summary"]
    assert diag["generated"]["withheld"] is False
    assert set(diag["generated"]) == set(LP.GENERATED_FIELDS) | {"withheld"}
    assert UNNAMED_FIELD not in diag["generated"]           # naming what may pass
    assert diag["generation_visibility"] == marker
    cand = next(c for c in cands["candidates"] if c["skill_id"] == skill_id)
    assert SECRET in cand["yaml_text"] and cand["generated_withheld"] is False
    assert cand["warnings"] and cand["generation_visibility"] == marker


# ---------------------------------------------------------------------------
# §12: the historical attack
# ---------------------------------------------------------------------------


class TestHistoricalAttack:
    async def test_site_human_gets_the_row_and_neither_sentinel(self):
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack)          # NO provenance
        subject, _ = await E.persona(stack, "site_a")
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        assert detail is not None and listed is not None and cands is not None
        assert any(i["incident_id"] == incident_id for i in listed["incidents"])
        assert any(c["skill_id"] == skill_id for c in cands["candidates"])
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)
        assert detail["diagnosis"]["generated"]["summary"] == LP.WITHHELD_GENERATED

    @pytest.mark.parametrize("persona", ["device_a", "class_server"])
    async def test_device_and_class_humans_get_the_same_confidentiality(self, persona):
        """F1 is open: these readers hold no site in `read_reach`, so today
        they read no incident at all -- and if general B0b gives them the
        row, the generated block is withheld under the same rule. Both
        halves are asserted: nothing they DO receive carries a sentinel."""
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack)
        subject, _ = await E.persona(stack, persona)
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)
        # The rule itself, for the reach these readers WILL have: an empty
        # site set covers no marker, a site set covers only its own.
        assert LearningView(sites=frozenset()).generated(_hostile_explanation())[0] \
            == LP.WITHHELD_GENERATED_BLOCK

    async def test_machine_principal_gets_the_same_confidentiality(self):
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack)
        agent_id = await _machine(stack, ("A",), name="A30.29 machine")
        who = stack.as_machine(agent_id)
        detail = await who.get(f"/api/incidents/{incident_id}")
        listed = await who.get("/api/incidents/", status="all")
        assert detail.status_code == 200, detail.text
        _assert_withheld_everywhere(
            listed.json() if listed.status_code == 200 else None,
            detail.json(), None, incident_id, skill_id,
        )

    async def test_tenant_wide_reader_keeps_the_original(self):
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack)
        _listed, detail, cands = await _reads(stack, E.OWNER, "tenant_owner", incident_id)
        diag = detail["diagnosis"]
        assert diag["generated"]["summary"] == f"Fleet-wide: {SECRET} -- {PHRASE}"
        assert diag["generated"]["suggested_action"] == f"Replace the PSU as at {SECRET}"
        assert diag["generated"]["reasoning_steps"] == [PHRASE, f"compared with {SECRET}"]
        assert diag["generated"]["withheld"] is False
        assert diag["generation_visibility"] is None        # nothing was recorded; nothing invented
        cand = next(c for c in cands["candidates"] if c["skill_id"] == skill_id)
        assert SECRET in cand["yaml_text"] and cand["warnings"][0].endswith(SECRET)
        assert cand["generated_withheld"] is False and cand["generation_visibility"] is None

    async def test_the_stored_rows_are_not_rewritten(self):
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack)
        subject, _ = await E.persona(stack, "site_a")
        await _reads(stack, subject, "site_admin", incident_id)
        async with stack.sessionmaker() as session:
            row = await session.get(CCIncident, incident_id)
            cand = await session.get(CCCandidateSkill, (skill_id, stack.tenant))
        assert row.explanation == _hostile_explanation()
        assert SECRET in cand.yaml_text and cand.generation_visibility is None


# ---------------------------------------------------------------------------
# §13: the new-write matrix, and provenance != authority
# ---------------------------------------------------------------------------


def _marker(stack, key):
    return site_visibility(stack.site(key)).to_dict()


class TestNewWriteMatrix:
    async def test_covering_readers_see_and_others_do_not(self):
        stack = await S.build("X")
        marker = _marker(stack, "A")
        incident_id, skill_id = await _seed(stack, marker=marker)

        for persona, role in (("site_a", "site_admin"), ("org_ab", "site_admin")):
            subject, _ = await E.persona(stack, persona)
            _l, detail, cands = await _reads(stack, subject, role, incident_id)
            assert detail is not None, persona
            _assert_visible(detail, cands, skill_id, marker)
        _l, detail, cands = await _reads(stack, E.OWNER, "tenant_owner", incident_id)
        _assert_visible(detail, cands, skill_id, marker)

        # An org reader NOT covering A, and a site-B reader: no row at all.
        await stack.grant("kc-org-cd", "org_unit", stack.regions["cd"])
        for subject in ("kc-org-cd", (await E.persona(stack, "site_b"))[0]):
            listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
            assert detail is None
            _assert_withheld_everywhere(listed, None, cands, incident_id, skill_id)

    async def test_the_row_being_readable_does_not_make_its_text_readable(self):
        """The interesting case: `incident.view` at A without `fleet.view`
        at A. The reader holds the ROW; the generated block is a
        `fleet.view` fact (A30.28 (3)) and is withheld."""
        stack = await S.build("X")
        marker = _marker(stack, "A")
        incident_id, skill_id = await _seed(stack, marker=marker)
        await stack.grant("kc-row-only", "site", stack.site("A"), subset=["incident.view"])
        listed, detail, cands = await _reads(stack, "kc-row-only", "site_admin", incident_id)
        assert detail is not None
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)

    async def test_a_marker_for_another_site_on_my_incident_is_withheld(self):
        """The multi-site defect's residue: an artifact at site A whose
        provenance names site C (generated from C's projection before the
        store was per-site). The site-A reader holds the row and not the
        projection -- and is not told WHICH site the marker names."""
        stack = await S.build("X")
        incident_id, skill_id = await _seed(stack, marker=_marker(stack, "C"))
        subject, _ = await E.persona(stack, "site_a")
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        assert detail is not None
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)
        for body in (listed, detail, cands):
            assert stack.site("C") not in json.dumps(body)
        # The org-AB reader holds A and B and not C: withheld as well.
        subject, _ = await E.persona(stack, "org_ab")
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)
        # The tenant-wide reader sees it, and sees which projection it was.
        _l, detail, cands = await _reads(stack, E.OWNER, "tenant_owner", incident_id)
        _assert_visible(detail, cands, skill_id, _marker(stack, "C"))

    async def test_tenant_generated_content_reaches_only_the_tenant(self):
        stack = await S.build("X")
        marker = tenant_visibility().to_dict()
        incident_id, skill_id = await _seed(stack, marker=marker)
        for persona in ("site_a", "org_ab"):
            subject, _ = await E.persona(stack, persona)
            listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
            assert detail is not None, persona
            _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)
        _l, detail, cands = await _reads(stack, E.OWNER, "tenant_owner", incident_id)
        _assert_visible(detail, cands, skill_id, marker)

    async def test_revoke_withholds_and_restore_shows_the_same_artifact(self):
        """Provenance is evidence, not authority. The reader keeps the ROW
        throughout (a permanent `incident.view` grant at A) and gains and
        loses `fleet.view` at A -- the marker never changes."""
        stack = await S.build("X")
        marker = _marker(stack, "A")
        incident_id, skill_id = await _seed(stack, marker=marker)
        subject = "kc-revoke-restore"
        # The permanent row-holding grant is a DIFFERENT row from the one
        # revoked below: the repository revives an identical (principal,
        # scope) grant in place (A23-3), so it is an org-unit grant here.
        await stack.grant(subject, "org_unit", stack.regions["ab"], subset=["incident.view"])
        full = await stack.grant(subject, "site", stack.site("A"))

        _l, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        _assert_visible(detail, cands, skill_id, marker)

        await stack.lapse(full, how="revoked")
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        assert detail is not None                           # the row is still theirs
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)

        await stack.lapse(full, how="expired")              # expiry is the other lifecycle end
        listed, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        _assert_withheld_everywhere(listed, detail, cands, incident_id, skill_id)

        # Restore: the same stored artifact, shown again. Nothing was
        # re-generated and nothing was re-marked.
        await stack.grant(subject, "site", stack.site("A"))
        _l, detail, cands = await _reads(stack, subject, "site_admin", incident_id)
        _assert_visible(detail, cands, skill_id, marker)
        async with stack.sessionmaker() as session:
            row = await session.get(CCIncident, incident_id)
        assert row.explanation[KEY] == marker


# ---------------------------------------------------------------------------
# §15: provider-independent
# ---------------------------------------------------------------------------


class TestProviderIndependence:
    @pytest.mark.parametrize("marker", [
        None, "not-a-dict",
        {"scope": "tenant", "site_id": None, "projection_version": 1},
        {"scope": "site", "site_id": "some-other-site", "projection_version": 1},
        {"scope": "site", "site_id": "site-a", "projection_version": 99},
        {"scope": "site", "site_id": "site-a", "projection_version": 1, "sites": ["site-c"]},
    ])
    def test_nothing_survives_for_a_reader_the_marker_does_not_cover(self, marker):
        """Every field poisoned, INCLUDING one this slice never names.
        The withheld block is constants: nothing from the document is in it."""
        explanation = _hostile_explanation(marker)
        block, visible_marker = LP.project_generated(explanation, frozenset({"site-a"}))
        assert block == LP.WITHHELD_GENERATED_BLOCK
        assert visible_marker is None
        assert _sentinels_in(block) == []

    def test_a_covered_reader_gets_the_named_fields_and_nothing_else(self):
        explanation = _hostile_explanation(site_visibility("site-a").to_dict())
        block, marker = LP.project_generated(explanation, frozenset({"site-a"}))
        assert set(block) == set(LP.GENERATED_FIELDS) | {"withheld"}
        assert block["withheld"] is False and UNNAMED_FIELD not in block
        assert marker == {"scope": "site", "site_id": "site-a", "projection_version": 1}

    def test_the_tenant_wide_reader_gets_the_named_fields_whatever_the_marker(self):
        for marker in (None, "garbage", tenant_visibility().to_dict(),
                       site_visibility("elsewhere").to_dict()):
            block, _ = LP.project_generated(_hostile_explanation(marker), None)
            assert block["summary"].startswith("Fleet-wide") and block["withheld"] is False

    def test_the_withheld_block_is_built_from_constants(self):
        """Structural: the shape a scoped reader gets names every generated
        field and copies none; a provider cannot add to it."""
        assert set(LP.WITHHELD_GENERATED_BLOCK) == set(LP.GENERATED_FIELDS) | {"withheld"}
        assert LP.WITHHELD_GENERATED_BLOCK["withheld"] is True
        assert LP.WITHHELD_GENERATED_BLOCK["summary"] == LP.WITHHELD_GENERATED
        assert LP.WITHHELD_GENERATED_BLOCK["suggested_action"] == ""
        assert LP.WITHHELD_GENERATED_BLOCK["reasoning_steps"] == []
        # The route asks the view and nothing else for the block.
        assert incidents_api._diagnosis(_hostile_explanation(), LearningView(sites=frozenset()))[
            "generated"] == LP.WITHHELD_GENERATED_BLOCK

    def test_a_candidate_is_withheld_the_same_way(self):
        class Row:
            yaml_text = f"description: {SECRET}"
            warnings = [SECRET]
            generation_visibility = None
        out = LP.project_candidate(Row(), frozenset({"site-a"}))
        assert out == {"yaml_text": "", "warnings": [], "generated_withheld": True,
                       "generation_visibility": None}
        Row.generation_visibility = site_visibility("site-a").to_dict()
        out = LP.project_candidate(Row(), frozenset({"site-a"}))
        assert out["yaml_text"].endswith(SECRET) and out["generated_withheld"] is False

    def test_no_projection_without_a_view(self):
        with pytest.raises(TypeError):
            incidents_api._diagnosis(_hostile_explanation(), None)
        with pytest.raises(TypeError):
            incidents_api._diagnosis(_hostile_explanation(), frozenset({"site-a"}))


# ---------------------------------------------------------------------------
# The wire and the ingest
# ---------------------------------------------------------------------------


@pytest.fixture
async def sm_wire():
    """A real Site Manager servicer with one candidate that carries a
    marker and one that predates it."""
    from harkeniq_sm.approvals import ApprovalService
    from harkeniq_sm.config import SMConfig
    from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
    from harkeniq_sm.db.models import CandidateSkillRow, Device, Site
    from harkeniq_sm.grpc_server import SiteManagerServiceServicer

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    async with db() as session:
        site = Site(name="site-1", cc_site_id="cc-site-1")
        session.add(site)
        await session.flush()
        session.add(Device(id="dev-1", site_id=site.id, agent_id="node-1", agent_name="n1"))
        session.add(CandidateSkillRow(
            skill_id="cand-marked", yaml_text="name: a\n", source_device="node-1",
            source_component="fan:1", validation_state="DRAFT", dry_run_matches=0,
            generation_visibility=site_visibility("cc-site-1").to_dict(),
        ))
        session.add(CandidateSkillRow(
            skill_id="cand-old", yaml_text="name: b\n", source_device="node-1",
            source_component="fan:2", validation_state="DRAFT", dry_run_matches=0,
        ))
        await session.commit()
    config = SMConfig(insecure=True, site_name="site-1")
    servicer = SiteManagerServiceServicer(db, ApprovalService(db, config), config)
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield f"127.0.0.1:{port}"
    await server.stop(grace=None)
    await engine.dispose()


class TestTheWire:
    async def test_the_marker_survives_and_absence_arrives_as_none(self, sm_wire):
        snapshot = await SMClient().get_fleet_snapshot(sm_wire, "tok", "t1", "cc-site-1")
        cands = {c["skill_id"]: c for c in snapshot["candidate_skills"]}
        assert cands["cand-marked"]["generation_visibility"] == \
            {"scope": "site", "site_id": "cc-site-1", "projection_version": 1}
        assert cands["cand-old"]["generation_visibility"] is None

    async def test_ingest_stores_a_marker_and_never_erases_one(self):
        stack = await S.build("X")
        marker = site_visibility(stack.site("A")).to_dict()
        async with stack.sessionmaker() as session:
            repo = CandidateSkillRepo(session)
            await repo.upsert(stack.tenant, stack.site("A"), {
                "skill_id": "c1", "yaml_text": "name: a\n", "generation_visibility": marker})
            # A re-poll from an older Site Manager carries none: the
            # recorded marker stays. (A poll never rewrites provenance.)
            await repo.upsert(stack.tenant, stack.site("A"), {
                "skill_id": "c1", "yaml_text": "name: a\n", "generation_visibility": None})
            await repo.upsert(stack.tenant, stack.site("A"), {
                "skill_id": "c2", "yaml_text": "name: b\n"})
            await session.commit()
            c1 = await session.get(CCCandidateSkill, ("c1", stack.tenant))
            c2 = await session.get(CCCandidateSkill, ("c2", stack.tenant))
        assert c1.generation_visibility == marker
        assert c2.generation_visibility is None


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


_API = pathlib.Path(harkeniq_cc.__file__).parent / "api"
_DIRECT_READS = (
    re.compile(r'\.get\(\s*"summary"'),
    re.compile(r'\.get\(\s*"suggested_action"'),
    re.compile(r'\.get\(\s*"reasoning_steps"'),
    re.compile(r'\["summary"\]'),
    re.compile(r'\.yaml_text\b'),
    re.compile(r'\b\w\.warnings\b'),
)


class TestStructure:
    def test_no_route_reads_a_generated_field_off_a_row(self):
        """The only reader of a generated field is the projection. A route
        that reached for `explanation["summary"]` or `row.yaml_text`
        would be the HIGH again, whatever its intent."""
        offenders = []
        for path in sorted(_API.glob("*.py")):
            source = path.read_text()
            for pattern in _DIRECT_READS:
                if pattern.search(source):
                    offenders.append((path.name, pattern.pattern))
        assert offenders == [], offenders

    def test_the_marker_is_read_through_one_rule(self):
        """`learning_projection` asks `generation_provenance.covers` and
        nothing else decides coverage."""
        source = (pathlib.Path(harkeniq_cc.__file__).parent / "learning_projection.py").read_text()
        assert "generation_covers(" in source
        assert source.count("def generation_visible") == 1
        for name in ("project_generated", "project_candidate", "project_generation_visibility"):
            body = source.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
            assert "generation_visible(" in body, name

    def test_the_view_exposes_both_projections(self):
        view = LearningView(sites=None)
        assert view.generated(_hostile_explanation())[0]["withheld"] is False
        assert hasattr(view, "candidate")
