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


class IngestService:
    """Persists agent-reported telemetry; one commit per event."""

    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self._site_id: Optional[str] = None
        # Set by the correlation engine (phase: correlation); called after
        # commit for every onset transition (device_id, subsystem,
        # severity, onset_at).
        self.on_onset: Optional[OnsetHook] = None
        # R3b-1 C1: reasoning pipeline for LLM enrichment (set by runtime)
        self.reasoning_pipeline = None
        # QA-033: CC-pushed fleet patterns, mirrored in memory for the
        # (sync-shaped) enrichment path. Loaded from sm_fleet_patterns at
        # startup; updated live by PushPolicy.
        self.fleet_patterns: dict[str, dict] = {}
        # QA-033 feedback half: candidate skill generation (set by runtime
        # when the LLM is enabled). None = generation off.
        self.skill_generator = None

    async def _site(self, session) -> str:
        if self._site_id is None:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            self._site_id = site.id
        return self._site_id

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
    ) -> str:
        """Upsert the device row; returns the site name (RegistrationAck)."""
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
        async with self.sessionmaker() as session:
            site_id = await self._site(session)
            await DeviceRepo(session).upsert_registration(
                site_id=site_id,
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
            )
            await session.commit()
        return self.config.site_name

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
            site_id = await self._site(session)
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
            site_id = await self._site(session)
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
            # QA-033: fleet knowledge from CC informs the explanation
            context.evidence.extend(
                await self._matching_fleet_patterns(agent_id)
            )
            # Check for LLMReasoner in the pipeline and call async directly
            for provider in self.reasoning_pipeline._providers:
                if isinstance(provider, LLMReasoner):
                    result = await provider.analyze_async(context)
                    if result and result.provider == "llm":
                        await self._store_explanation(agent_id, sensor_id, result)
                        # QA-033 feedback half: an LLM diagnosis is the
                        # candidate-skill trigger (R3b-1 C2, R-C1).
                        await self._generate_candidate_skill(
                            agent_id, sensor_id, severity, result, context,
                        )
                    break
        except Exception as e:
            logger.warning("LLM enrichment failed for %s/%s: %s", agent_id, sensor_id, e)

    async def _generate_candidate_skill(
        self, agent_id: str, sensor_id: str, severity: str, result, context,
    ) -> None:
        """Generate, validate, and persist a candidate skill (QA-033).

        Best-effort. Dedup: one un-reported candidate per (device,
        component) — a flapping verdict must not spam the CC queue.
        validate_and_promote runs static analysis plus a dry-run against
        the evidence state that triggered generation; failures are logged
        and the candidate is dropped.
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
                session.add(CandidateSkillRow(
                    skill_id=candidate.skill_id,
                    yaml_text=candidate.yaml_text,
                    source_device=agent_id,
                    source_component=sensor_id,
                    validation_state=package.validation_state.value,
                    warnings=validation.warnings or None,
                    dry_run_matches=validation.dry_run_matches,
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

    async def _matching_fleet_patterns(self, agent_id: str) -> list[dict]:
        """Fleet patterns whose scope matches this device (QA-033).

        Empty vendor/model in affected_scope is a wildcard. Best-effort:
        any failure returns no extra evidence, never blocks enrichment.
        """
        if not self.fleet_patterns:
            return []
        try:
            from harkeniq_sm.db.repos import DeviceRepo
            async with self.sessionmaker() as session:
                device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None:
                return []
            matches = []
            for pattern in self.fleet_patterns.values():
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
            return matches
        except Exception as e:
            logger.debug("Fleet pattern matching failed: %s", e)
            return []

    async def _store_explanation(self, agent_id: str, sensor_id: str, result) -> None:
        """Store the LLM explanation on the open incident for this device+subsystem."""
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
            incident.explanation = {
                "provider": result.provider,
                "summary": result.diagnosis,
                "confidence": result.confidence,
                "evidence_cited": result.evidence_cited,
                "reasoning_steps": result.reasoning_steps,
                "suggested_action": result.suggested_action,
                "similar_past_incidents": result.similar_past_incidents,
            }
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
