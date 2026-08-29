"""Learned signals: durable knowledge derived from patterns and outcomes.

S3 (2026-08-29). Keeps the concepts distinct, per the ratified vocabulary:

  outcome   what actually happened after an action
  pattern   an evidence-derived recurring relationship, detected fleet-wide
  SIGNAL    the knowledge that relationship yields, projected onto the
            scope its evidence supports, carried forward so it can inform
            tomorrow's attention and diagnosis
  candidate a proposed reusable behaviour derived from learning
  promoted  a governed, approved capability available for future use

This module owns only the pattern → signal step. It is pure: no I/O, no
database, no clock beyond what the caller passes.

Scope is evidence-bound
-----------------------
A pattern's `affected_scope` always carries action_type/vendor/model, so
COHORT scope is always justified. Only `cross_site_batch` names the sites
that actually failed, so SITE scope is derived only there. Device and
tenant scope are NOT derived: pattern evidence aggregates by cohort and
says nothing about an individual device, and making every signal global
would be exactly the over-reach the scope rule forbids.

A signal is knowledge, never authority. Nothing here permits an action;
it is evidence a human or an agent may reason with, and every consumer
still passes through the same governed capability path.
"""

from __future__ import annotations

from typing import Any, Optional

#: Signals below this confidence are not worth carrying forward as
#: knowledge — they would add noise to attention rather than insight.
MIN_SIGNAL_CONFIDENCE = 0.15

SCOPE_COHORT = "cohort"
SCOPE_SITE = "site"


def cohort_ref(vendor: str, model: str) -> str:
    """Stable cohort identity. Lowercased so 'Dell'/'dell' are one cohort."""
    return f"{(vendor or '').strip().lower()}/{(model or '').strip().lower()}"


def signal_key(scope_type: str, scope_ref: str, action_type: str) -> str:
    """Stable identity for upsert: the same knowledge refreshes in place."""
    return f"{scope_type}:{scope_ref}:{(action_type or '').strip().upper()}"


def _statement(pattern_type: str, action_type: str, vendor: str, model: str,
               evidence: dict, scope_type: str, scope_label: str) -> str:
    """Plain language a human reads and an agent can quote.

    Every number here comes from the pattern's own evidence.
    """
    rate = evidence.get("failure_rate")
    total = evidence.get("total")
    where = f" at {scope_label}" if scope_type == SCOPE_SITE else ""
    cohort = f"{vendor} {model}".strip() or "this hardware"

    if pattern_type == "anomaly":
        trend = evidence.get("trend")
        if trend is not None:
            return (
                f"{action_type} on {cohort}{where} is failing more often than "
                f"it was (failure rate moved {trend:+.0%})."
            )
        return f"{action_type} on {cohort}{where} is failing more often than it was."

    if rate is None:
        return f"{action_type} on {cohort}{where} shows a recurring failure pattern."

    base = f"{action_type} on {cohort}{where} fails {rate:.0%} of the time"
    if total:
        base += f" ({evidence.get('failures', '?')} of {total} attempts)"
    if pattern_type == "reliability":
        fleet = evidence.get("fleet_failure_rate")
        if fleet is not None:
            base += f", against a fleet average of {fleet:.0%}"
    elif pattern_type == "cross_site_batch":
        sites = evidence.get("sites_affected")
        if sites:
            base += f", across {sites} sites"
    return base + "."


def _failing_sites(pattern) -> list[str]:
    """Sites the pattern's own evidence names as failing. Nothing inferred."""
    raw = (pattern.affected_scope or {}).get("sites", "")
    return [s for s in (raw.split(",") if raw else []) if s]


def derive_signals(pattern, now: Optional[Any] = None) -> list[dict]:
    """Turn one detected pattern into the learned signals it justifies.

    Returns dicts ready for upsert. Empty when the pattern is too weak to
    be worth carrying, or carries no cohort to attach knowledge to.
    """
    scope = pattern.affected_scope or {}
    evidence = dict(pattern.evidence or {})
    action_type = scope.get("action_type", "")
    vendor = scope.get("vendor", "")
    model = scope.get("model", "")

    if pattern.confidence < MIN_SIGNAL_CONFIDENCE:
        return []
    if not action_type or not (vendor or model):
        # Without a cohort there is nothing to attach the knowledge to.
        return []

    ref = cohort_ref(vendor, model)
    signals: list[dict] = [{
        "signal_key": signal_key(SCOPE_COHORT, ref, action_type),
        "scope_type": SCOPE_COHORT,
        "scope_ref": ref,
        "action_type": action_type,
        "vendor": vendor,
        "model": model,
        "statement": _statement(
            pattern.pattern_type, action_type, vendor, model, evidence,
            SCOPE_COHORT, "",
        ),
        "evidence": evidence,
        "confidence": round(float(pattern.confidence), 4),
        "source_pattern_id": pattern.pattern_id,
    }]

    # Site scope ONLY where the pattern names failing sites. A cross-site
    # batch failure is knowledge about those sites specifically, which is
    # what lets a site-scoped consumer see its own learning.
    if pattern.pattern_type == "cross_site_batch":
        per_site = evidence.get("site_failure_counts") or {}
        for site_id in _failing_sites(pattern):
            site_evidence = dict(evidence)
            if site_id in per_site:
                site_evidence["failures_at_site"] = per_site[site_id]
            signals.append({
                "signal_key": signal_key(SCOPE_SITE, site_id, action_type),
                "scope_type": SCOPE_SITE,
                "scope_ref": site_id,
                "action_type": action_type,
                "vendor": vendor,
                "model": model,
                "statement": _statement(
                    pattern.pattern_type, action_type, vendor, model,
                    site_evidence, SCOPE_SITE, "this site",
                ),
                "evidence": site_evidence,
                "confidence": round(float(pattern.confidence), 4),
                "source_pattern_id": pattern.pattern_id,
            })
    return signals


def signals_for_device(signals, vendor: str, model: str,
                       site_id: str) -> list[dict]:
    """Select the signals that apply to one device, most specific first.

    A device inherits site knowledge and cohort knowledge; it never
    inherits knowledge derived from a different cohort or a site it is not
    in. Callers pass already tenant-scoped rows.
    """
    ref = cohort_ref(vendor, model)
    hits = []
    for s in signals:
        if s.scope_type == SCOPE_SITE and s.scope_ref == site_id:
            hits.append((0, s))
        elif s.scope_type == SCOPE_COHORT and s.scope_ref == ref:
            hits.append((1, s))
    hits.sort(key=lambda t: (t[0], -t[1].confidence, t[1].signal_key))
    return [
        {
            "signal_key": s.signal_key,
            "scope_type": s.scope_type,
            "scope_ref": s.scope_ref,
            "action_type": s.action_type,
            "statement": s.statement,
            "confidence": s.confidence,
            "evidence": s.evidence or {},
            "observation_count": s.observation_count,
            "source_pattern_id": s.source_pattern_id,
            "source_cycle_id": s.source_cycle_id,
            "last_confirmed_at": (
                s.last_confirmed_at.isoformat() if s.last_confirmed_at else None
            ),
        }
        for _, s in hits
    ]
