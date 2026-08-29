"""Attention: what deserves attention first in this tenant, and why.

S2 (2026-08-29). This is the PRIORITIZE / EXPLAIN / RECOMMEND layer over
evidence that already exists — predictive risk scores, CVE exposure,
warranty, fleet patterns, current health, and actions already waiting for
a human. It computes no new intelligence: `score_device` remains the only
risk model, and nothing here re-implements it.

Why this lives at Central Command rather than in the Console
------------------------------------------------------------
The answer composes five separate reads. Joining them in the browser
would strand the intelligence where only a browser can reach it: a named
agent, an MCP tool, or the future intent compiler would each have to
re-derive the same joins and could drift from what the operator sees. One
capability, one governed contract, many consumers — the UI is the first
consumer, not the owner.

What it deliberately does NOT do
--------------------------------
* No mutation, ever. `recommended_next` names a capability and whether it
  needs approval; it never performs one and confers no authority.
* No risk TREND. Risk is computed on demand and no per-device history is
  persisted, so a trend line would be invented. Where the fleet has real
  trend evidence (an `anomaly` pattern for the device's vendor/model) it
  is surfaced as evidence, attributed as a cohort signal, not a device one.
* No fabricated confidence. `insufficient_data` stays exactly that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq_cc.learned_signals import signals_for_device

#: Ranking order for bands. Lower sorts first (needs attention sooner).
_BAND_ORDER = {"high": 0, "medium": 1, "low": 2, "insufficient_data": 3}

#: Attention drivers, in the order a human would work them. Lower sorts
#: first. Attention order is deliberately NOT the risk band alone: the
#: predictive model scores FUTURE failure, so a device that is failing
#: RIGHT NOW with no failure history scores only the health bump and would
#: otherwise sort below a healthy device with a flaky past — burying the
#: broken machine on page two. Stated as one sentence for the operator:
#: currently failing first, then work already waiting on a human, then
#: predicted risk. The risk band itself is unchanged and stays visible.
DRIVER_CURRENT_FAILURE = "current_failure"
DRIVER_AWAITING_APPROVAL = "awaiting_approval"
DRIVER_DEGRADED = "degraded_now"
DRIVER_PREDICTED_RISK = "predicted_risk"
DRIVER_INSUFFICIENT = "insufficient_evidence"

_DRIVER_ORDER = {
    DRIVER_CURRENT_FAILURE: 0,
    DRIVER_AWAITING_APPROVAL: 1,
    DRIVER_DEGRADED: 2,
    DRIVER_PREDICTED_RISK: 3,
    DRIVER_INSUFFICIENT: 4,
}

_DRIVER_LABEL = {
    DRIVER_CURRENT_FAILURE: "Failing now",
    DRIVER_AWAITING_APPROVAL: "Waiting on a human decision",
    DRIVER_DEGRADED: "Degraded now",
    DRIVER_PREDICTED_RISK: "Predicted failure risk",
    DRIVER_INSUFFICIENT: "Not enough evidence to judge",
}


def _driver(health: str, band: str, pending: list) -> str:
    """Why this device is where it is in the list. Deterministic."""
    if (health or "").lower() == "critical":
        return DRIVER_CURRENT_FAILURE
    if pending:
        return DRIVER_AWAITING_APPROVAL
    if (health or "").lower() == "warning":
        return DRIVER_DEGRADED
    if band == "insufficient_data":
        return DRIVER_INSUFFICIENT
    return DRIVER_PREDICTED_RISK

#: Severities that make a CVE worth naming in the "why" line.
_SERIOUS_CVE = {"critical", "high"}


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _confidence(risk) -> dict:
    """Data sufficiency, stated plainly.

    `basis` comes from the scoring model: device_history (the device's own
    outcomes), cohort_prior (its vendor/model peers), or absent entirely
    when there was nothing to score on.
    """
    basis = risk.factors.get("basis", "insufficient_data")
    return {
        "basis": basis,
        "sample_count": risk.sample_count,
        "sufficient": basis == "device_history",
        "explanation": {
            "device_history": "Scored from this device's own outcome history.",
            "cohort_prior": (
                "Too few outcomes for this device; scored from the failure "
                "rate of the same vendor and model across the fleet."
            ),
            "insufficient_data": (
                "Not enough evidence to score this device. This is not a "
                "clean bill of health — it is an absence of data."
            ),
        }.get(basis, "Unknown basis."),
    }


def _reasons(risk, cves: list[dict], warranty: Optional[dict],
             patterns: list[dict], health: str, driver: str = "",
             learned: Optional[list[dict]] = None) -> list[str]:
    """Human-readable 'why does this matter', derived only from evidence.

    Every string here traces to a value the caller supplied. Nothing is
    inferred beyond restating the evidence in plain language.
    """
    out: list[str] = []
    f = risk.factors or {}

    # Lead with present-tense trouble. A predicted-risk sentence first
    # would bury the fact that the machine is broken right now.
    if driver == DRIVER_CURRENT_FAILURE:
        out.append("This device is reporting CRITICAL health right now.")
    elif driver == DRIVER_DEGRADED:
        out.append("This device is reporting degraded health right now.")

    if f.get("basis") == "device_history":
        rate = f.get("weighted_failure_rate")
        if rate is not None:
            out.append(
                f"{round(rate * 100)}% of this device's recent remediation "
                f"attempts failed (recency-weighted, {risk.sample_count} outcomes)."
            )
    elif f.get("basis") == "cohort_prior":
        rate = f.get("cohort_failure_rate")
        if rate is not None:
            out.append(
                f"No history for this device yet; {risk.vendor} {risk.model} "
                f"peers fail at {round(rate * 100)}%."
            )
    else:
        out.append(
            "No outcome history for this device or its model — unscored, "
            "not proven healthy."
        )

    if f.get("health_bump"):
        out.append(f"Current health is {health or f.get('health', 'degraded')}.")
    if f.get("warranty_expired_bump"):
        end = (warranty or {}).get("end_date") or "an unknown date"
        out.append(f"Warranty expired ({end}) — no vendor remedy path.")

    serious = [c for c in cves if (c.get("severity") or "").lower() in _SERIOUS_CVE]
    if serious:
        ids = ", ".join(sorted({c["cve_id"] for c in serious})[:3])
        more = len(serious) - 3
        out.append(
            f"{len(serious)} high-or-critical CVE match(es) in installed "
            f"firmware: {ids}{f' and {more} more' if more > 0 else ''}."
        )
    elif cves:
        out.append(f"{len(cves)} lower-severity CVE match(es) in installed firmware.")

    for p in patterns:
        if p.get("pattern_type") == "anomaly":
            out.append(
                "Fleet-wide failure rate for this vendor/model is rising "
                "(anomaly pattern detected across the fleet)."
            )
            break

    # S3: prior learning, stated as learning. This is what makes the loop
    # visible to the operator — yesterday's outcomes speaking to today's
    # decision, most specific scope first.
    for signal in (learned or [])[:2]:
        scope_word = "this site" if signal["scope_type"] == "site" else "this model"
        out.append(
            f"Learned for {scope_word}: {signal['statement']} "
            f"(confidence {signal['confidence']:.0%}, "
            f"seen {signal['observation_count']}x)"
        )
    return out


def _recommend(risk, cves: list[dict], pending: list[dict]) -> dict:
    """The next governed capability, chosen deterministically.

    Ordered by what a human would actually do first. Every branch names a
    capability that exists in the platform and says whether invoking it
    needs human approval. `available` reports whether this Central Command
    can currently invoke it, so a consumer is never promised a door that
    is not yet cut.
    """
    if pending:
        return {
            "capability": "review_pending_approval",
            "summary": (
                f"{len(pending)} action already waiting on a human decision "
                f"for this device."
            ),
            "requires_approval": True,
            "available": True,
            "refs": [p["action_id"] for p in pending],
        }

    fixable = [c for c in cves if c.get("fixed_version")]
    if fixable:
        return {
            "capability": "plan_firmware_remediation",
            "summary": (
                f"{len(fixable)} CVE match(es) have a fixed firmware version "
                f"available."
            ),
            # Firmware is never budget-granted (A10.4): campaign-level human
            # approval always. Campaign mediation reaches the tenant surface
            # in S6, so this is honest about not being invocable here yet.
            "requires_approval": True,
            "available": False,
            "unavailable_reason": (
                "Firmware campaigns are executed at the Site Manager; tenant-"
                "facing mediation arrives in a later slice."
            ),
            "refs": sorted({c["cve_id"] for c in fixable}),
        }

    basis = (risk.factors or {}).get("basis")
    if risk.band in ("high", "medium") and basis == "device_history":
        return {
            "capability": "investigate_device",
            "summary": "Scored on this device's own failure history — inspect it.",
            "requires_approval": False,
            "available": True,
            "refs": [risk.agent_id],
        }
    if risk.band == "insufficient_data":
        return {
            "capability": "collect_evidence",
            "summary": (
                "Not enough outcome history to judge this device. Keep it "
                "observed; no action is justified yet."
            ),
            "requires_approval": False,
            "available": True,
            "refs": [],
        }
    return {
        "capability": "monitor",
        "summary": "No action indicated by current evidence.",
        "requires_approval": False,
        "available": True,
        "refs": [],
    }


def _needs_attention(item: dict) -> bool:
    """Does a human need to look at this device?

    Not a band test: a device failing right now, or one with an action
    already waiting on a decision, needs attention whatever its PREDICTED
    failure risk says.
    """
    if item["attention_driver"] in (
        DRIVER_CURRENT_FAILURE, DRIVER_AWAITING_APPROVAL, DRIVER_DEGRADED,
    ):
        return True
    return item["band"] in ("high", "medium")


def _patterns_for(patterns, vendor: str, model: str) -> list[dict]:
    """Fleet patterns whose affected scope covers this device's cohort."""
    hits = []
    for p in patterns:
        scope = p.affected_scope or {}
        s_vendor = scope.get("vendor")
        s_model = scope.get("model")
        if s_vendor and s_vendor != vendor:
            continue
        if s_model and s_model != model:
            continue
        if not s_vendor and not s_model:
            continue  # unscoped patterns say nothing about THIS device
        hits.append({
            # The PERSISTED row (CCFleetPattern) keys on `id`; the in-memory
            # detector dataclass (FleetPattern) keys on `pattern_id`. This
            # function is handed rows in production and dataclasses in some
            # tests, so accept both. Reading only `pattern_id` 500'd the
            # whole attention endpoint the moment a real pattern existed —
            # invisible until the fleet actually learned something.
            "pattern_id": getattr(p, "pattern_id", None) or getattr(p, "id", ""),
            "pattern_type": p.pattern_type,
            "description": p.description,
            "confidence": p.confidence,
        })
    return hits


def build_attention(
    devices,
    risks,
    exposures: list[dict],
    warranty_map: dict,
    pending_routes,
    patterns,
    sites,
    tenant_id: str,
    now: Optional[datetime] = None,
    learned_signals=None,
) -> dict:
    """Compose the tenant's attention answer. Pure: no I/O, no DB.

    Every input is already tenant-scoped by its repository; this function
    performs no cross-tenant lookup and cannot widen scope.
    """
    ts = _now(now)
    site_names = {s.id: s.site_name for s in sites}
    dev_by_agent = {d.agent_id: d for d in devices}

    cves_by_agent: dict[str, list[dict]] = {}
    for e in exposures:
        cves_by_agent.setdefault(e["agent_id"], []).append(e)

    pending_by_agent: dict[str, list[dict]] = {}
    for r in pending_routes:
        pending_by_agent.setdefault(r.device_agent_id, []).append({
            "action_id": r.action_id,
            "action_type": r.action_type,
            "routed_at": r.routed_at.isoformat() if r.routed_at else None,
        })

    items: list[dict] = []
    for risk in risks:
        dev = dev_by_agent.get(risk.agent_id)
        cves = cves_by_agent.get(risk.agent_id, [])
        pending = pending_by_agent.get(risk.agent_id, [])
        warranty = warranty_map.get(getattr(dev, "service_tag", "")) if dev else None
        warranty_d = (
            {"status": None, "end_date": warranty.end_date} if warranty else None
        )
        cohort_patterns = _patterns_for(patterns, risk.vendor, risk.model)
        site_id = risk.site_id or (dev.site_id if dev else "")
        health = (dev.health if dev else "") or "unknown"
        driver = _driver(health, risk.band, pending)
        # S3: what the fleet has already LEARNED that applies to this
        # device — site knowledge first, then cohort. This is the edge that
        # closes the loop: yesterday's outcomes change what today's
        # attention says, for humans and agents alike.
        device_signals = signals_for_device(
            learned_signals or [], risk.vendor, risk.model, site_id,
        )

        items.append({
            "agent_id": risk.agent_id,
            "agent_name": risk.agent_name or (dev.agent_name if dev else ""),
            "device_id": dev.id if dev else None,
            "site_id": site_id,
            "site_name": site_names.get(site_id, ""),
            "vendor": risk.vendor,
            "model": risk.model,
            "device_class": (dev.device_class or "server") if dev else "server",
            "health": health,
            "observation": (dev.observation if dev else "") or "unknown",
            "risk_score": round(risk.risk_score, 4),
            "band": risk.band,
            # Why this sits where it sits. Predicted risk is one driver
            # among several, not the whole ordering.
            "attention_driver": driver,
            "attention_driver_label": _DRIVER_LABEL[driver],
            "confidence": _confidence(risk),
            "factors": risk.factors or {},
            "reasons": _reasons(
                risk, cves, warranty_d, cohort_patterns, health, driver,
                device_signals,
            ),
            "evidence": {
                "learned_signals": device_signals,
                "cves": [
                    {
                        "cve_id": c["cve_id"],
                        "severity": c.get("severity", ""),
                        "component": c.get("component_name") or c.get("component", ""),
                        "version": c.get("version", ""),
                        "fixed_version": c.get("fixed_version", ""),
                    }
                    for c in cves
                ],
                "warranty": warranty_d,
                "fleet_patterns": cohort_patterns,
            },
            "current_state": {
                "pending_approvals": pending,
                "open_action_count": len(pending),
            },
            "recommended_next": _recommend(risk, cves, pending),
        })

    # Rank: driver first (failing now > waiting on a human > degraded >
    # predicted > unscored), then band, then score, then a stable tiebreak
    # on agent_id so two devices never swap places between polls.
    items.sort(key=lambda i: (
        _DRIVER_ORDER.get(i["attention_driver"], 9),
        _BAND_ORDER.get(i["band"], 9),
        -i["risk_score"],
        i["agent_id"],
    ))
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx

    # Site rollup, derived from the same items so the two can never disagree.
    site_rollup: dict[str, dict] = {}
    for item in items:
        sid = item["site_id"]
        row = site_rollup.setdefault(sid, {
            "site_id": sid,
            "site_name": site_names.get(sid, ""),
            "device_count": 0,
            "needs_attention": 0,
            "by_band": {"high": 0, "medium": 0, "low": 0, "insufficient_data": 0},
            "top_risk_score": 0.0,
            "top_device": None,
            "cve_count": 0,
            "pending_approvals": 0,
        })
        row["device_count"] += 1
        if _needs_attention(item):
            row["needs_attention"] += 1
        if item["band"] in row["by_band"]:
            row["by_band"][item["band"]] += 1
        if item["risk_score"] > row["top_risk_score"]:
            row["top_risk_score"] = item["risk_score"]
        if row["top_device"] is None:
            row["top_device"] = item["agent_name"] or item["agent_id"]
        row["cve_count"] += len(item["evidence"]["cves"])
        row["pending_approvals"] += item["current_state"]["open_action_count"]

    site_list = sorted(
        site_rollup.values(),
        key=lambda s: (-s["needs_attention"], -s["by_band"]["high"],
                       -s["top_risk_score"], s["site_id"]),
    )

    attention_required = sum(1 for i in items if _needs_attention(i))
    return {
        "tenant_id": tenant_id,
        "generated_at": ts.isoformat(),
        "sites": site_list,
        "items": items,
        "summary": {
            "devices_scored": len(items),
            "attention_required": attention_required,
            "insufficient_data_count": sum(
                1 for i in items if i["band"] == "insufficient_data"
            ),
            "devices_with_cves": sum(
                1 for i in items if i["evidence"]["cves"]
            ),
            "actions_awaiting_approval": sum(
                i["current_state"]["open_action_count"] for i in items
            ),
        },
    }
