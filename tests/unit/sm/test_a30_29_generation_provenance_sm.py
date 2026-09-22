"""A30.29 at the Site Manager: the writer of generation provenance.

The real `SiteManagerServiceServicer.PushPolicy` stores what Central
Command pushes PER SITE; the real `IngestService._enrich_verdict` builds
the prompt from the device's own site's projection, and records the
projection boundary on the explanation and on the candidate. The model is
a fake that ECHOES its prompt, so the persisted summary provably carries
whatever pattern text the model was shown -- which is exactly the property
the marker exists to describe.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from harkeniq.generation_provenance import KEY, parse
from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import CandidateSkillRow, Device, Incident, Site
from harkeniq_sm.db.repos import (
    DeviceRepo,
    SMFleetPatternRepo,
    SMSitePatternRepo,
    StatusRepo,
)
from harkeniq_sm.grpc_server import SiteManagerServiceServicer
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.reasoning import LLMReasoner, ReasoningPipeline
from harkeniq_sm.skill_generator import SkillGenerator

CC_A, CC_B = "cc-site-alpha", "cc-site-bravo"
SENTINEL_A = "PROJECTED-FOR-ALPHA-ONLY"
SENTINEL_B = "PROJECTED-FOR-BRAVO-ONLY"
LEGACY_TEXT = "SEL_CLEAR fails at 65% on Dell R750 across 3 sites (35/54) LEGACY-UNBOUNDED"

GOOD_YAML = (
    "```yaml\n"
    "name: auto-fan-bearing-wear\n"
    "version: 1\n"
    "target: fan\n"
    "description: {echo}\n"
    "rules:\n"
    "  - condition: \"speed_rpm < 3000\"\n"
    "    verdict: WARNING\n"
    "    message: \"Fan {{name}} RPM below threshold\"\n"
    "```"
)


class EchoProvider:
    """A model that repeats the evidence it was shown, and remembers every
    prompt. For the skill prompt it returns a valid YAML whose
    description is the root cause it was given -- so the YAML carries
    whatever the diagnosis carried."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages):
        user = messages[-1]["content"]
        self.prompts.append(user)
        if "Generate the skill YAML" in user:
            root_cause = user.split("Root cause: ", 1)[1].split("\n", 1)[0]
            # The YAML's description is every sentinel the diagnosis
            # carried, so the YAML provably inherits what the model saw.
            found = [tok for tok in (SENTINEL_A, SENTINEL_B, "LEGACY-UNBOUNDED")
                     if tok in root_cause]
            return GOOD_YAML.format(echo=" ".join(found) or "no sentinel")
        evidence = user.split("Current evidence:\n", 1)[1].split("\n\nProvide:", 1)[0]
        return "DIAGNOSIS " + evidence.replace("\n", " ")


def _config(**overrides):
    defaults = dict(insecure=True, site_token="test-token")
    defaults.update(overrides)
    return SMConfig(**defaults)


@pytest.fixture
async def sm(db):
    """One Site Manager serving TWO bound sites, one Dell R750 at each,
    each with an open fan incident the enrichment path can explain."""
    config = _config()
    provider = EchoProvider()
    ingest = IngestService(db, config)
    pipeline = ReasoningPipeline()
    pipeline.add_provider(LLMReasoner(provider))
    ingest.reasoning_pipeline = pipeline
    ingest.skill_generator = SkillGenerator(provider)
    servicer = SiteManagerServiceServicer(
        db, ApprovalService(db, config), config, autonomy=SMAutonomyEnforcer(),
    )
    servicer.ingest = ingest
    ids = {}
    async with db() as session:
        for key, cc in (("A", CC_A), ("B", CC_B)):
            site = Site(name=f"site-{key.lower()}", cc_site_id=cc)
            session.add(site)
            await session.flush()
            device = Device(
                site_id=site.id, agent_id=f"agent-{key.lower()}",
                agent_name=f"srv-{key.lower()}", vendor="Dell", model="R750",
            )
            session.add(device)
            await session.flush()
            session.add(Incident(
                site_id=site.id, kind="device", status="open",
                device_id=device.id, subsystem="fan", title="Fan duty rising",
            ))
            ids[key] = {"site": site.id, "device": device.id, "agent": device.agent_id}
        await session.commit()
    return {"servicer": servicer, "ingest": ingest, "provider": provider,
            "db": db, **ids}


def _push(cc_site_id: str, description: str, *, marked: bool = True,
          pattern_id: str = "pat-P") -> harkeniq_pb2.PolicyUpdate:
    pattern = {
        "pattern_id": pattern_id, "pattern_type": "cross_site_batch",
        "description": description,
        "affected_scope": {"vendor": "Dell", "model": "R750", "action_type": "SEL_CLEAR"},
        "confidence": 0.75, "evidence": {"failure_rate": 0.65}, "detected_at": 1.0,
    }
    if marked:
        pattern[KEY] = {"scope": "site", "site_id": cc_site_id, "projection_version": 1}
    return harkeniq_pb2.PolicyUpdate(
        tenant_id="t1", site_id=cc_site_id, learned_patterns_json=json.dumps([pattern]),
    )


async def _explanation(db, device_id: str) -> dict:
    async with db() as session:
        row = (await session.execute(
            select(Incident).where(Incident.device_id == device_id)
        )).scalar_one()
        return dict(row.explanation or {})


async def _candidate(db, agent_id: str) -> CandidateSkillRow | None:
    async with db() as session:
        return (await session.execute(
            select(CandidateSkillRow).where(CandidateSkillRow.source_device == agent_id)
        )).scalar_one_or_none()


class TestMultiSiteSiteManager:
    """Vinod's §14: one Site Manager owns A and B; P pushed projected for
    each with a distinct sentinel."""

    async def test_each_site_consumes_its_own_projection_and_records_it(self, sm):
        servicer, ingest, provider, db = sm["servicer"], sm["ingest"], sm["provider"], sm["db"]
        assert (await servicer.PushPolicy(_push(CC_A, SENTINEL_A), None)).accepted
        assert (await servicer.PushPolicy(_push(CC_B, SENTINEL_B), None)).accepted

        # Neither push replaced the other: two rows for one pattern id.
        async with db() as session:
            rows = await SMSitePatternRepo(session).list_all()
        assert {(r.site_id, r.description) for r in rows} == {
            (sm["A"]["site"], SENTINEL_A), (sm["B"]["site"], SENTINEL_B)}
        assert all(parse(r.visibility) is not None for r in rows)

        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        await ingest._enrich_verdict(sm["B"]["agent"], "fan:Fan1B", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2400})

        # The PROMPTS: A's carried A's projection only, B's carried B's only.
        diag_a, _skill_a, diag_b, _skill_b = provider.prompts
        assert SENTINEL_A in diag_a and SENTINEL_B not in diag_a
        assert SENTINEL_B in diag_b and SENTINEL_A not in diag_b

        # The persisted artifacts carry what the model saw...
        expl_a = await _explanation(db, sm["A"]["device"])
        expl_b = await _explanation(db, sm["B"]["device"])
        assert SENTINEL_A in expl_a["summary"] and SENTINEL_B not in expl_a["summary"]
        assert SENTINEL_B in expl_b["summary"] and SENTINEL_A not in expl_b["summary"]
        # ...and record the projection actually consumed, by CANONICAL id.
        assert expl_a[KEY] == {"scope": "site", "site_id": CC_A, "projection_version": 1}
        assert expl_b[KEY] == {"scope": "site", "site_id": CC_B, "projection_version": 1}

        # The candidates too, with the site E1.3 declared and never wrote.
        cand_a, cand_b = await _candidate(db, sm["A"]["agent"]), await _candidate(db, sm["B"]["agent"])
        assert cand_a is not None and cand_b is not None
        assert cand_a.site_id == sm["A"]["site"] and cand_b.site_id == sm["B"]["site"]
        assert cand_a.generation_visibility == expl_a[KEY]
        assert cand_b.generation_visibility == expl_b[KEY]
        assert SENTINEL_A in cand_a.yaml_text and SENTINEL_B not in cand_a.yaml_text
        assert SENTINEL_B in cand_b.yaml_text and SENTINEL_A not in cand_b.yaml_text

    async def test_a_re_push_for_one_site_leaves_the_other_untouched(self, sm):
        servicer, db = sm["servicer"], sm["db"]
        assert (await servicer.PushPolicy(_push(CC_A, SENTINEL_A), None)).accepted
        assert (await servicer.PushPolicy(_push(CC_B, SENTINEL_B), None)).accepted
        assert (await servicer.PushPolicy(_push(CC_B, SENTINEL_B + "-v2"), None)).accepted
        async with db() as session:
            rows = {r.site_id: r.description for r in await SMSitePatternRepo(session).list_all()}
        assert rows == {sm["A"]["site"]: SENTINEL_A, sm["B"]["site"]: SENTINEL_B + "-v2"}


class TestWhatIsRecorded:
    async def test_no_fleet_evidence_is_the_devices_own_site(self, sm):
        """A diagnosis from the device's telemetry alone is that site's."""
        ingest, db = sm["ingest"], sm["db"]
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        expl = await _explanation(db, sm["A"]["device"])
        assert expl[KEY] == {"scope": "site", "site_id": CC_A, "projection_version": 1}
        assert "speed_rpm" in expl["summary"]

    async def test_an_unmarked_push_makes_the_artifact_tenant_visible(self, sm):
        """An older Central Command pushes no marker. The artifact is
        generated from an unbounded payload, and says so."""
        servicer, ingest, db = sm["servicer"], sm["ingest"], sm["db"]
        assert (await servicer.PushPolicy(_push(CC_A, LEGACY_TEXT, marked=False), None)).accepted
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        expl = await _explanation(db, sm["A"]["device"])
        assert "LEGACY-UNBOUNDED" in expl["summary"]
        assert expl[KEY] == {"scope": "tenant", "site_id": None, "projection_version": 1}
        cand = await _candidate(db, sm["A"]["agent"])
        assert cand.generation_visibility == expl[KEY]

    async def test_a_legacy_store_row_makes_the_artifact_tenant_visible(self, sm):
        """The pre-A30.29 store, reloaded at boot: consumed as unmarked,
        recorded as tenant -- until the site's own push supersedes it."""
        servicer, ingest, db = sm["servicer"], sm["ingest"], sm["db"]
        ingest.fleet_patterns.put_legacy({
            "pattern_id": "pat-P", "pattern_type": "cross_site_batch",
            "description": LEGACY_TEXT, "affected_scope": {"vendor": "Dell"},
            "confidence": 0.9,
        })
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        expl = await _explanation(db, sm["A"]["device"])
        assert "LEGACY-UNBOUNDED" in expl["summary"]
        assert expl[KEY]["scope"] == "tenant"

        # The site's own projection arrives: it supersedes the legacy row
        # for THIS site, and the next artifact is site-visible.
        assert (await servicer.PushPolicy(_push(CC_A, SENTINEL_A), None)).accepted
        async with db() as session:
            row = (await session.execute(
                select(Incident).where(Incident.device_id == sm["A"]["device"]))).scalar_one()
            row.explanation = None
            await session.commit()
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        expl = await _explanation(db, sm["A"]["device"])
        assert SENTINEL_A in expl["summary"] and "LEGACY-UNBOUNDED" not in expl["summary"]
        assert expl[KEY] == {"scope": "site", "site_id": CC_A, "projection_version": 1}

    async def test_an_unbound_site_records_nothing_rather_than_a_guess(self, sm):
        """A site with no Central Command identity has no canonical site
        to name. The key is ABSENT, which every reader treats as unknown."""
        ingest, db = sm["ingest"], sm["db"]
        async with db() as session:
            site = Site(name="site-unbound")
            session.add(site)
            await session.flush()
            device = Device(site_id=site.id, agent_id="agent-u", agent_name="u",
                            vendor="Dell", model="R750")
            session.add(device)
            await session.flush()
            session.add(Incident(site_id=site.id, kind="device", status="open",
                                 device_id=device.id, subsystem="fan", title="x"))
            device_id = device.id
            await session.commit()
        await ingest._enrich_verdict("agent-u", "fan:Fan1", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        expl = await _explanation(db, device_id)
        assert expl["summary"].startswith("DIAGNOSIS")
        assert KEY not in expl
        cand = await _candidate(db, "agent-u")
        assert cand is not None and cand.generation_visibility is None

    async def test_the_marker_is_never_quoted_into_the_prompt(self, sm):
        """The prompt carries the citation shape A30.28's read grammar
        knows; the marker names a site id and does not belong in text."""
        servicer, ingest, provider = sm["servicer"], sm["ingest"], sm["provider"]
        assert (await servicer.PushPolicy(_push(CC_A, SENTINEL_A), None)).accepted
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        for prompt in provider.prompts:
            assert KEY not in prompt and "projection_version" not in prompt


class TestSnapshotUpflow:
    async def test_the_candidate_rides_the_snapshot_with_its_marker(self, sm):
        servicer, ingest, db = sm["servicer"], sm["ingest"], sm["db"]
        assert (await servicer.PushPolicy(_push(CC_A, SENTINEL_A), None)).accepted
        await ingest._enrich_verdict(sm["A"]["agent"], "fan:Fan1A", "fan-health",
                                     "CRITICAL", {"speed_rpm": 2500})
        async with db() as session:
            from datetime import datetime, timezone
            await StatusRepo(session).upsert(
                sm["A"]["device"], datetime.now(timezone.utc), "OBSERVING", {"fan": "CRITICAL"}, {})
            await session.commit()
        snap = await servicer.GetFleetSnapshot(
            harkeniq_pb2.FleetSnapshotRequest(tenant_id="t1", site_id=CC_A), None)
        assert snap.site_resolved
        (cand,) = snap.candidate_skills
        assert json.loads(cand.generation_visibility_json) == {
            "scope": "site", "site_id": CC_A, "projection_version": 1}
        (inc,) = snap.incidents
        assert json.loads(inc.explanation_json)[KEY]["site_id"] == CC_A

    async def test_a_pre_a30_29_candidate_rides_with_an_empty_marker(self, sm):
        """Old producer shape, new consumer: the field is empty, and
        Central Command reads UNKNOWN. Nothing is invented on the wire."""
        servicer, db = sm["servicer"], sm["db"]
        async with db() as session:
            session.add(CandidateSkillRow(
                skill_id="cand-old", yaml_text="name: old\n", source_device=sm["A"]["agent"],
                source_component="fan:Fan1A", validation_state="DRAFT", dry_run_matches=0,
            ))
            from datetime import datetime, timezone
            await StatusRepo(session).upsert(
                sm["A"]["device"], datetime.now(timezone.utc), "OBSERVING", {}, {})
            await session.commit()
        snap = await servicer.GetFleetSnapshot(
            harkeniq_pb2.FleetSnapshotRequest(tenant_id="t1", site_id=CC_A), None)
        (cand,) = snap.candidate_skills
        assert cand.generation_visibility_json == ""


class TestBootReload:
    async def test_runtime_loads_both_stores_per_site(self, sm):
        """What `runtime.py` does at boot, exercised through the repos it
        reads: per-site rows land under their site with their marker;
        legacy rows land unmarked."""
        db = sm["db"]
        async with db() as session:
            await SMSitePatternRepo(session).upsert(
                sm["A"]["site"], {"pattern_id": "pat-P", "description": SENTINEL_A},
                {"scope": "site", "site_id": CC_A, "projection_version": 1})
            await SMSitePatternRepo(session).upsert(
                sm["B"]["site"], {"pattern_id": "pat-P", "description": SENTINEL_B},
                {"scope": "site", "site_id": CC_B, "projection_version": 1})
            from harkeniq_sm.db.models import SMFleetPatternRow
            session.add(SMFleetPatternRow(pattern_id="pat-L", description=LEGACY_TEXT))
            await session.commit()

        # Mirror the runtime's load exactly (see runtime.py).
        ingest = IngestService(db, _config())
        async with db() as session:
            for row in await SMSitePatternRepo(session).list_all():
                pattern = {"pattern_id": row.pattern_id, "description": row.description,
                           "affected_scope": row.affected_scope}
                if row.visibility is not None:
                    pattern[KEY] = row.visibility
                ingest.fleet_patterns.put(row.site_id, pattern)
            for row in await SMFleetPatternRepo(session).list_all():
                ingest.fleet_patterns.put_legacy(
                    {"pattern_id": row.pattern_id, "description": row.description,
                     "affected_scope": row.affected_scope})

        for key, cc, sentinel in (("A", CC_A, SENTINEL_A), ("B", CC_B, SENTINEL_B)):
            consumed = ingest.fleet_patterns.for_site(sm[key]["site"])
            by_id = {p["pattern_id"]: (p["description"], m) for p, m in consumed}
            assert by_id["pat-P"][0] == sentinel and by_id["pat-P"][1].site_id == cc
            assert by_id["pat-L"] == (LEGACY_TEXT, None)


class TestRealBoot:
    async def test_make_state_reloads_both_stores(self, tmp_path):
        """The production boot (`runtime.make_state`) on a database that
        holds a per-site row and a legacy row: the mirror is per site,
        marked, and the legacy row is unmarked."""
        from harkeniq_sm.db.models import SMFleetPatternRow
        from harkeniq_sm.runtime import make_state

        config = _config(site_name="lab", dsn=f"sqlite+aiosqlite:///{tmp_path}/sm.db")
        first = await make_state(config)
        async with first.sessionmaker() as session:
            site = Site(name="site-a", cc_site_id=CC_A)
            session.add(site)
            await session.flush()
            await SMSitePatternRepo(session).upsert(
                site.id, {"pattern_id": "pat-P", "description": SENTINEL_A},
                {"scope": "site", "site_id": CC_A, "projection_version": 1})
            session.add(SMFleetPatternRow(pattern_id="pat-L", description=LEGACY_TEXT))
            await session.commit()
            site_id = site.id
        await first.engine.dispose()

        state = await make_state(config)
        try:
            consumed = state.ingest.fleet_patterns.for_site(site_id)
            by_id = {p["pattern_id"]: (p["description"], m) for p, m in consumed}
            assert by_id["pat-P"][0] == SENTINEL_A and by_id["pat-P"][1].site_id == CC_A
            assert by_id["pat-L"] == (LEGACY_TEXT, None)
            # Another site on the same Site Manager sees only the legacy row.
            assert [p["pattern_id"] for p, _ in state.ingest.fleet_patterns.for_site("elsewhere")] == ["pat-L"]
        finally:
            await state.engine.dispose()
