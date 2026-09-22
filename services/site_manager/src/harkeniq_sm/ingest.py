"""Telemetry ingest: registration, heartbeats, verdicts (R-S2, R-S3).

All timestamps recorded here are Site Manager receive time — agent
clocks are untrusted (spec §7). Subsystem onset state is the substrate
the correlation engine reads: onset_at is set when a subsystem
transitions out of OK and preserved until it returns to OK.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from harkeniq.generation_provenance import (
    KEY as GENERATION_VISIBILITY_KEY,
    GenerationVisibility,
    combine,
    parse as parse_visibility,
    site_visibility,
    tenant_visibility,
)
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    DeviceRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
    TelemetryRepo,
)
from harkeniq_sm.sitemodel.discovery import rack_hint

logger = logging.getLogger("harkeniq.sm.ingest")

# health_summary values use "OK"; verdict severities use "HEALTHY".
_OK_VALUES = {"OK", "HEALTHY", ""}

OnsetHook = Callable[[str, str, str, datetime], Awaitable[None]]


def _normalize(severity: str) -> str:
    return "OK" if severity in _OK_VALUES else severity


class PatternMirror:
    """In-memory mirror of the pattern stores the enrichment path reads.

    A30.29: keyed by the site a pattern was pushed FOR. `for_site` is the
    ONLY read, and it answers with the site's own rows first; a legacy row
    (`sm_fleet_patterns`, pushed before the store was per-site, carrying
    no site and no marker) is consulted only where the site holds no row
    for that pattern id. Each entry says whether it is marked, because an
    artifact generated from an unmarked payload must be recorded as
    `tenant`-visible (the truth about an unbounded payload) and one from a
    marked payload as whatever the marker says.
    """

    def __init__(self) -> None:
        self._by_site: dict[str, dict[str, dict]] = {}
        self._legacy: dict[str, dict] = {}

    def put(self, site_id: str, pattern: dict) -> None:
        """A pattern as pushed to `site_id` (its `generation_visibility` key,
        if any, rides inside the dict exactly as pushed)."""
        self._by_site.setdefault(site_id, {})[str(pattern.get("pattern_id", ""))] = dict(pattern)

    def put_legacy(self, pattern: dict) -> None:
        self._legacy[str(pattern.get("pattern_id", ""))] = dict(pattern)

    def for_site(self, site_id: str) -> list[tuple[dict, Optional[GenerationVisibility]]]:
        """(pattern, its parsed marker or None) for every pattern this
        site's reasoning may consume. `None` is an unmarked payload."""
        own = self._by_site.get(site_id, {})
        out: list[tuple[dict, Optional[GenerationVisibility]]] = []
        for pattern_id, pattern in own.items():
            out.append((pattern, parse_visibility(pattern.get("generation_visibility"))))
        for pattern_id, pattern in self._legacy.items():
            if pattern_id not in own:
                out.append((pattern, None))
        return out

    def __len__(self) -> int:
        return sum(len(rows) for rows in self._by_site.values()) + len(self._legacy)

    def __bool__(self) -> bool:
        return len(self) > 0


class IngestService:
    """Persists agent-reported telemetry; one commit per event."""

    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        # Set by the correlation engine (phase: correlation); called after
        # commit for every onset transition (device_id, subsystem,
        # severity, onset_at).
        self.on_onset: Optional[OnsetHook] = None
        # R3b-1 C1: reasoning pipeline for LLM enrichment (set by runtime)
        self.reasoning_pipeline = None
        # QA-033: CC-pushed fleet patterns, mirrored in memory for the
        # (sync-shaped) enrichment path. Loaded at startup from the
        # per-site store (and the legacy one); updated live by PushPolicy.
        # A30.29: keyed by the site the pattern was pushed FOR.
        self.fleet_patterns = PatternMirror()
        # QA-033 feedback half: candidate skill generation (set by runtime
        # when the LLM is enabled). None = generation off.
        self.skill_generator = None

    async def _site_for_device(self, session, agent_id: str) -> str:
        """The site of an ALREADY-REGISTERED device. E1.3.

        Heartbeats and verdicts come from a device that enrolled earlier,
        and its site is a fact on its own row -- there is nothing to
        resolve and nothing to guess. Sending these through `_site()`
        made a multi-site Site Manager refuse every heartbeat, which
        stopped verdicts, onsets, incidents and therefore proposals: the
        compose gate caught it as a silence rather than an error.
        """
        device = await DeviceRepo(session).get_by_agent_id(agent_id)
        if device is not None and device.site_id:
            return device.site_id
        # Not registered yet. Fall back to the unambiguous single site,
        # which still refuses to guess when there is more than one.
        return await self._site(session)

    async def _site(self, session) -> str:
        """The site for callers that legitimately have only one.

        E1.3: this is NO LONGER a memo. It used to cache the configured
        site's id on the instance for the life of the process, which made
        "the site" a property of the Site Manager rather than of the
        device -- so two sites on one process would have put every device
        into one site row.

        Callers that know their site (registration, correlation, an API
        request that names one) pass it explicitly. This remains for the
        genuinely SM-level paths, and it refuses to guess when the answer
        is ambiguous.
        """
        from harkeniq_sm.db.models import Site
        from sqlalchemy import select

        sites = (
            await session.execute(select(Site).where(Site.status == "active"))
        ).scalars().all()
        if len(sites) == 1:
            return sites[0].id
        if not sites:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            return site.id
        raise ValueError(
            "this Site Manager serves several sites; the caller must name "
            "one rather than fall back to a configured default"
        )

    async def register(
        self,
        agent_id: str,
        agent_name: str = "",
        vendor: str = "",
        model: str = "",
        service_tag: str = "",
        bmc_location_json: str = "",
        peers: Optional[list[str]] = None,
        firmware_json: str = "",
        device_class: str = "",
        capabilities_json: str = "",
        site_id: str = "",
        site_name: str = "",
    ) -> str:
        """Upsert the device row; returns the site name (RegistrationAck).

        E1.3: `site_id` is resolved from the device's SITE-BOUND
        enrollment credential before this is called, and is the only
        thing that decides where the device lands. When it is absent the
        Site Manager falls back to its single active site, which keeps a
        pre-E1.3 deployment working and refuses when that is ambiguous.
        """
        bmc_location = None
        if bmc_location_json:
            try:
                bmc_location = json.loads(bmc_location_json)
            except ValueError:
                logger.warning("Unparseable bmc_location_json from %s", agent_id)
        firmware = None
        if firmware_json:
            try:
                parsed = json.loads(firmware_json)
                if isinstance(parsed, list):
                    firmware = parsed
            except ValueError:
                logger.warning("Unparseable firmware_json from %s", agent_id)
        capabilities = None
        if capabilities_json:
            try:
                parsed = json.loads(capabilities_json)
                if isinstance(parsed, dict):
                    capabilities = parsed
            except ValueError:
                logger.warning(
                    "Unparseable capabilities_json from %s", agent_id
                )
        async with self.sessionmaker() as session:
            resolved_site = site_id or await self._site(session)
            await DeviceRepo(session).upsert_registration(
                site_id=resolved_site,
                agent_id=agent_id,
                agent_name=agent_name,
                vendor=vendor,
                model=model,
                service_tag=service_tag,
                bmc_location=bmc_location,
                peers=peers,
                rack_suggestion=rack_hint(agent_name),
                firmware=firmware,
                device_class=device_class,
                capabilities=capabilities,
            )
            await session.commit()
        return site_name or self.config.site_name

    async def heartbeat(
        self,
        agent_id: str,
        agent_name: str,
        state: str,
        health_summary: dict[str, str],
        peer_status: dict[str, str],
    ) -> bool:
        now = datetime.now(timezone.utc)
        onsets: list[tuple[str, str, str, datetime]] = []
        async with self.sessionmaker() as session:
            site_id = await self._site_for_device(session, agent_id)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site_id, agent_id=agent_id, agent_name=agent_name
            )
            await StatusRepo(session).upsert(
                device.id, now, state, dict(health_summary), dict(peer_status)
            )
            await TelemetryRepo(session).add_heartbeat(
                device.id, now, state, dict(health_summary), dict(peer_status)
            )
            for subsystem, severity in health_summary.items():
                onset = await self._apply_subsystem(
                    session, device.id, subsystem, severity, now
                )
                if onset:
                    onsets.append(onset)
            await session.commit()
        await self._fire(onsets)
        return True

    async def verdict(
        self,
        agent_id: str,
        sensor_id: str,
        skill_name: str,
        severity: str,
        evidence_json: str = "",
        message: str = "",
    ) -> bool:
        now = datetime.now(timezone.utc)
        evidence = None
        if evidence_json:
            try:
                evidence = json.loads(evidence_json)
            except ValueError:
                logger.warning("Unparseable evidence_json from %s", agent_id)
        subsystem = sensor_id.split(":", 1)[0]
        onsets: list[tuple[str, str, str, datetime]] = []
        async with self.sessionmaker() as session:
            site_id = await self._site_for_device(session, agent_id)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site_id, agent_id=agent_id
            )
            await TelemetryRepo(session).add_verdict(
                device.id, now, sensor_id, skill_name, severity, message, evidence
            )
            onset = await self._apply_subsystem(
                session, device.id, subsystem, severity, now
            )
            if onset:
                onsets.append(onset)
            await session.commit()
        await self._fire(onsets)
        # R3b-1 C1: enrich WARNING/CRITICAL verdicts with LLM explanation
        if severity not in _OK_VALUES and self.reasoning_pipeline:
            import asyncio
            asyncio.create_task(
                self._enrich_verdict(agent_id, sensor_id, skill_name, severity, evidence)
            )
        return True

    async def _enrich_verdict(
        self, agent_id: str, sensor_id: str, skill_name: str,
        severity: str, evidence: Any,
    ) -> None:
        """Run the reasoning pipeline to produce an LLM explanation.

        Best-effort: failures are logged, never block verdict ingestion.
        """
        try:
            from harkeniq_sm.reasoning import LLMReasoner, ReasoningContext
            context = ReasoningContext(
                device_id=agent_id,
                component=sensor_id,
                severity=severity,
                evidence=[{"skill": skill_name, "data": evidence}] if evidence else [],
            )
            # QA-033: fleet knowledge from CC informs the explanation.
            # A30.29: and the explanation records which projection of it
            # the model saw. The device's own telemetry and history are
            # its site's facts; every consumed pattern contributes the
            # marker it was pushed with, or `tenant` if it had none.
            fleet_evidence, consumed = await self._matching_fleet_patterns(agent_id)
            context.evidence.extend(fleet_evidence)
            visibility = combine(
                [await self._device_visibility(agent_id)]
                + [marker if marker is not None else tenant_visibility()
                   for marker in consumed]
            )
            # Check for LLMReasoner in the pipeline and call async directly
            for provider in self.reasoning_pipeline._providers:
                if isinstance(provider, LLMReasoner):
                    result = await provider.analyze_async(context)
                    if result and result.provider == "llm":
                        await self._store_explanation(
                            agent_id, sensor_id, result, visibility,
                        )
                        # QA-033 feedback half: an LLM diagnosis is the
                        # candidate-skill trigger (R3b-1 C2, R-C1).
                        await self._generate_candidate_skill(
                            agent_id, sensor_id, severity, result, context,
                            visibility,
                        )
                    break
        except Exception as e:
            logger.warning("LLM enrichment failed for %s/%s: %s", agent_id, sensor_id, e)

    async def _device_visibility(self, agent_id: str) -> Optional[GenerationVisibility]:
        """The projection boundary of a device's OWN facts: its site.

        Named by Central Command's site id (E0.2), because that is what a
        reader's reach is expressed in. A device at a site that is not
        bound to a Central Command identity has no canonical site to name;
        the answer is UNKNOWN and the artifact reads as such. Central
        Command never ingests from an unbound site anyway.
        """
        from harkeniq_sm.db.repos import SiteRepo
        async with self.sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None or not device.site_id:
                return None
            site = await SiteRepo(session).get(device.site_id)
        if site is None or not site.cc_site_id:
            logger.debug(
                "device %s: site has no Central Command identity; "
                "generated content will read as unknown provenance", agent_id,
            )
            return None
        return site_visibility(site.cc_site_id)

    async def _generate_candidate_skill(
        self, agent_id: str, sensor_id: str, severity: str, result, context,
        visibility: Optional[GenerationVisibility] = None,
    ) -> None:
        """Generate, validate, and persist a candidate skill (QA-033).

        Best-effort. Dedup: one un-reported candidate per (device,
        component) — a flapping verdict must not spam the CC queue.
        validate_and_promote runs static analysis plus a dry-run against
        the evidence state that triggered generation; failures are logged
        and the candidate is dropped.

        A30.29: `visibility` is the projection boundary of everything the
        skill prompt carried -- the diagnosis and the same evidence -- and
        is persisted beside the YAML. None is recorded as NULL (unknown),
        never guessed.
        """
        if self.skill_generator is None:
            return
        try:
            from sqlalchemy import select

            from harkeniq_sm.db.models import CandidateSkillRow
            from harkeniq_sm.skill_validation import SkillValidator

            async with self.sessionmaker() as session:
                existing = (
                    await session.execute(
                        select(CandidateSkillRow).where(
                            CandidateSkillRow.source_device == agent_id,
                            CandidateSkillRow.source_component == sensor_id,
                            CandidateSkillRow.reported_to_cc == False,  # noqa: E712
                        )
                    )
                ).scalars().first()
                if existing is not None:
                    return

            candidate = await self.skill_generator.generate(
                device_id=agent_id,
                component=sensor_id,
                severity=severity,
                root_cause=result.diagnosis,
                suggested_action=result.suggested_action or "",
                evidence=context.evidence,
            )
            if candidate is None:
                return

            # Dry-run against the evidence states that triggered this —
            # the only ground truth available at generation time.
            historical = [
                e["data"] for e in context.evidence
                if isinstance(e, dict) and isinstance(e.get("data"), dict)
            ]
            validation, package = SkillValidator().validate_and_promote(
                candidate.yaml_text, candidate.package,
                historical_states=historical or None,
            )
            if not validation.passed:
                logger.info(
                    "Candidate skill for %s/%s failed %s: %s",
                    agent_id, sensor_id, validation.stage, validation.errors,
                )
                return

            async with self.sessionmaker() as session:
                # E1.3 declared `site_id` and nothing wrote it (the snapshot
                # joined through the device). The device's own row is the
                # site, and it is written now.
                device = await DeviceRepo(session).get_by_agent_id(agent_id)
                session.add(CandidateSkillRow(
                    skill_id=candidate.skill_id,
                    site_id=device.site_id if device is not None else None,
                    yaml_text=candidate.yaml_text,
                    source_device=agent_id,
                    source_component=sensor_id,
                    validation_state=package.validation_state.value,
                    warnings=validation.warnings or None,
                    dry_run_matches=validation.dry_run_matches,
                    generation_visibility=(
                        visibility.to_dict() if visibility is not None else None
                    ),
                ))
                await session.commit()
            logger.info(
                "Candidate skill %s generated for %s/%s (state=%s)",
                candidate.skill_id, agent_id, sensor_id,
                package.validation_state.value,
            )
        except Exception as e:
            logger.warning(
                "Candidate skill generation failed for %s/%s: %s",
                agent_id, sensor_id, e,
            )

    async def _matching_fleet_patterns(
        self, agent_id: str,
    ) -> tuple[list[dict], list[Optional[GenerationVisibility]]]:
        """Fleet patterns whose scope matches this device (QA-033).

        Empty vendor/model in affected_scope is a wildcard. Best-effort:
        any failure returns no extra evidence, never blocks enrichment.

        A30.29: the device's SITE decides which projection is consumed --
        `PatternMirror.for_site` answers with the rows pushed for that
        site, and a legacy row only where the site holds none. Returns
        the evidence in the citation shape A30.28's read grammar knows,
        and, beside it, the marker of each consumed pattern (None for an
        unmarked payload) so the caller can record what the model saw.
        """
        if not self.fleet_patterns:
            return [], []
        try:
            async with self.sessionmaker() as session:
                device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None or not device.site_id:
                return [], []
            matches: list[dict] = []
            consumed: list[Optional[GenerationVisibility]] = []
            for pattern, marker in self.fleet_patterns.for_site(device.site_id):
                scope = pattern.get("affected_scope") or {}
                vendor = scope.get("vendor", "")
                model = scope.get("model", "")
                if vendor and vendor != device.vendor:
                    continue
                if model and model != device.model:
                    continue
                matches.append({"fleet_pattern": {
                    "pattern_id": pattern.get("pattern_id", ""),
                    "pattern_type": pattern.get("pattern_type", ""),
                    "description": pattern.get("description", ""),
                    "confidence": pattern.get("confidence", 0.0),
                }})
                consumed.append(marker)
            return matches, consumed
        except Exception as e:
            logger.debug("Fleet pattern matching failed: %s", e)
            return [], []

    async def _store_explanation(
        self, agent_id: str, sensor_id: str, result,
        visibility: Optional[GenerationVisibility] = None,
    ) -> None:
        """Store the LLM explanation on the open incident for this device+subsystem.

        A30.29: the explanation carries the projection boundary it was
        generated from, INSIDE the same document and in the same
        assignment as the generated text, so an explanation cannot exist
        with text and without the provenance of its own creation. An
        unknown boundary is recorded by ABSENCE of the key, never guessed.
        """
        from harkeniq_sm.db.repos import DeviceRepo, IncidentRepo
        subsystem = sensor_id.split(":", 1)[0]
        async with self.sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None:
                return
            repo = IncidentRepo(session)
            incident = await repo.open_device_incident(device.id, subsystem)
            if incident is None:
                return
            explanation = {
                "provider": result.provider,
                "summary": result.diagnosis,
                "confidence": result.confidence,
                "evidence_cited": result.evidence_cited,
                "reasoning_steps": result.reasoning_steps,
                "suggested_action": result.suggested_action,
                "similar_past_incidents": result.similar_past_incidents,
            }
            if visibility is not None:
                explanation[GENERATION_VISIBILITY_KEY] = visibility.to_dict()
            incident.explanation = explanation
            await session.commit()
            logger.info("LLM explanation stored for incident %s", incident.id)

    async def _apply_subsystem(
        self, session, device_id: str, subsystem: str, severity: str, now: datetime
    ) -> Optional[tuple[str, str, str, datetime]]:
        """Update onset state; returns an onset event on OK→non-OK transition."""
        severity = _normalize(severity)
        repo = SubsystemStateRepo(session)
        current = await repo.get(device_id, subsystem)
        if severity == "OK":
            if current is not None and current.severity != "OK":
                await repo.set(device_id, subsystem, "OK", None)
            elif current is None:
                await repo.set(device_id, subsystem, "OK", None)
            return None
        if current is not None and current.severity != "OK" and current.onset_at:
            # Continuing fault: keep the original onset, refresh severity.
            await repo.set(device_id, subsystem, severity, current.onset_at)
            return None
        await repo.set(device_id, subsystem, severity, now)
        return (device_id, subsystem, severity, now)

    async def _fire(self, onsets) -> None:
        if self.on_onset is None:
            return
        for device_id, subsystem, severity, onset_at in onsets:
            try:
                await self.on_onset(device_id, subsystem, severity, onset_at)
            except Exception:  # pragma: no cover - correlation must not break ingest
                logger.exception("onset hook failed")
