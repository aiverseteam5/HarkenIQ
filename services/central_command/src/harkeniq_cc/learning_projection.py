"""What ONE reader may be shown of the fleet's learned knowledge (A30.28).

A fleet pattern is detected over the whole tenant, and the learned signals
derived from it carry its evidence verbatim. That evidence is tenant
knowledge in its CONCLUSION (spec A23: a vendor/model cohort fails this
action) and site-specific in its SUPPORT: which sites failed, how many
times each, how many sites there are, and the tenant totals a hidden
site's number can be subtracted out of. Until S4 the readers selected
ROWS by scope and returned their CONTENT as stored, so a reader holding
site A read ``{"site-a": 3, "site-c": 27}`` and "30 of 40 attempts, across
2 sites" on a row they were entitled to see.

This module is the ONE projection of that content. It is pure: no I/O, no
database, no resolver of its own. The caller says which sites the reader
holds (`governance.learning_view`, which is S2's `read_reach` for
`fleet.view` and nothing else), and:

``visible is None``
    a tenant-wide reader. Every function returns what is stored, unchanged
    and by identity -- this slice narrows by reach, it deletes nothing.

anything else (the exact set of held sites, possibly empty)
    the BOUNDED representation (decided: Vinod, 2026-09-21):

    * a RATE is the cohort conclusion and is kept, rounded to the nearest
      5%; CONFIDENCE is rounded to a 0.25 grid. Exact values are not kept
      because they are counts in disguise: `batch_failure` confidence is
      ``min(1, total/20)``, so 0.70 *is* 14 attempts, and a three-decimal
      rate such as 0.733 *is* 11/15;
    * every COUNT is withheld -- totals, failures, attempts, the number of
      sites -- and the payload says so in a `withheld` list instead of
      inventing a smaller total;
    * a site-keyed map keeps only the reader's own sites, and is always
      marked `partial` whether or not anything was dropped, so the marker
      itself says nothing about the sites the reader does not hold;
    * generated TEXT follows its evidence: it is re-rendered from the
      projected evidence by the same generator that wrote it, never
      returned as stored.

Naming what may pass, never what may not (the A25.3 lesson): an evidence
key this module does not recognise is withheld.

GENERATED CONTENT INHERITS ITS PROJECTION (A30.29). A Site Manager's LLM
diagnosis and the candidate YAML generated from it were written by a model
from a prompt that carried whichever pattern payload the Site Manager
held -- one site's bounded projection since A30.28, the whole tenant's
before it, and on a multi-site Site Manager possibly ANOTHER site's. The
text does not say which. The writer records it as a `generation_visibility`
marker (`harkeniq.generation_provenance`), and `project_generated` /
`project_candidate` show a scoped reader the generated fields only when
that marker names a site the reader holds NOW. Missing, malformed,
`tenant`, or another site: the whole block is withheld, by construction --
the withheld shape is built from constants, so a field a future provider
adds cannot survive.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from harkeniq.generation_provenance import KEY as GENERATION_VISIBILITY_KEY
from harkeniq.generation_provenance import covers as generation_covers
from harkeniq.generation_provenance import parse as parse_generation_visibility
from harkeniq_cc.learned_signals import SCOPE_SITE, render_statement

#: Grid a scoped reader's rates are rounded to.
RATE_STEP = 0.05
#: Grid a scoped reader's confidence is rounded to.
CONFIDENCE_STEP = 0.25

#: Evidence keys that ARE the conclusion: a proportion, never a count.
RATE_KEYS = frozenset({
    "failure_rate",
    "success_rate",
    "current_failure_rate",
    "trend",
    "model_failure_rate",
    "fleet_failure_rate",
})
#: Evidence keys holding a map keyed by site id.
SITE_MAP_KEYS = frozenset({"site_failure_counts"})
#: `affected_scope` key naming sites -- a CSV string in production, a list
#: in some fixtures. Both are handled; the stored type is preserved.
SITE_LIST_KEYS = frozenset({"sites"})
#: Evidence keys stating a fact about the signal's OWN site.
OWN_SITE_KEYS = frozenset({"failures_at_site"})
#: `affected_scope` keys that name the cohort. Tenant knowledge (A23).
COHORT_KEYS = frozenset({"action_type", "vendor", "model"})

MARK_PROJECTION = "projection"
MARK_WITHHELD = "withheld"
MARK_PARTIAL = "partial"
PROJECTION_SCOPED = "scoped"

#: What a scoped reader is told when stored text cannot be PROVEN free of
#: a count after reduction. It says that something is withheld, and
#: nothing about what it is or where.
WITHHELD_STATEMENT = (
    "a learned fleet signal applies here; its supporting detail is outside "
    "your authorized scope"
)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def band_rate(value: Any) -> Optional[float]:
    """A proportion rounded half-up to the nearest `RATE_STEP`; sign kept."""
    number = _number(value)
    if number is None:
        return None
    steps = math.floor(abs(number) / RATE_STEP + 0.5 + 1e-9)
    return round(math.copysign(steps * RATE_STEP, number), 2)


def band_confidence(value: Any) -> float:
    """Confidence on the `CONFIDENCE_STEP` grid, in (0, 1] -- or 0.0."""
    number = _number(value)
    if number is None or number <= 0:
        return 0.0
    steps = math.floor(number / CONFIDENCE_STEP + 0.5 + 1e-9)
    return round(min(1.0, max(CONFIDENCE_STEP, steps * CONFIDENCE_STEP)), 2)


def _frozen(visible: Optional[Iterable[str]]) -> Optional[frozenset[str]]:
    return None if visible is None else frozenset(visible)


# ---------------------------------------------------------------------------
# Instants
# ---------------------------------------------------------------------------

#: Grain a scoped reader's learning timestamps are floored to, in seconds.
INSTANT_STEP = 60


def bound_instant(value: Any) -> Any:
    """A learning timestamp floored to the minute; the stored TYPE is kept.

    WHEN the tenant concluded something is part of the conclusion and is
    kept. Its sub-second part is not: `OutcomeAggregator.get_metrics`
    sorts cohorts by their tenant attempt TOTAL, the detector stamps each
    pattern with `time.time()` in that order, and every cycle and signal
    of the pass is written in it -- so within one pass the microseconds
    RANK the cohorts by the very totals this module withholds. Patterns of
    one pass floor to one instant (a pass is in-memory and takes
    milliseconds); patterns of different passes keep their real order.
    """
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        floored = math.floor(value / INSTANT_STEP) * INSTANT_STEP
        return type(value)(floored)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).replace(
                second=0, microsecond=0).isoformat()
        except ValueError:
            return None
    return value


def _instant_key(value: Any) -> float:
    """Sort key for a (bounded) instant of any stored type; newest first."""
    if isinstance(value, datetime):
        return -value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return -float(value)
    if isinstance(value, str) and value:
        try:
            return -datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Structured evidence
# ---------------------------------------------------------------------------


def project_evidence(
    evidence: Optional[dict],
    visible: Optional[Iterable[str]],
    *,
    own_site: Optional[str] = None,
) -> dict:
    """A signal's or pattern's `evidence`, as this reader may read it."""
    stored = evidence or {}
    held = _frozen(visible)
    if held is None or not stored:
        return stored
    out: dict[str, Any] = {}
    withheld: list[str] = []
    partial: list[str] = []
    for key in sorted(stored):
        value = stored[key]
        if key in RATE_KEYS:
            banded = band_rate(value)
            if banded is None:
                withheld.append(key)
            else:
                out[key] = banded
        elif key in SITE_MAP_KEYS and isinstance(value, dict):
            kept = {site: value[site] for site in sorted(value) if site in held}
            if kept:
                out[key] = kept
            partial.append(key)
        elif key in OWN_SITE_KEYS and own_site is not None and own_site in held:
            out[key] = value
        else:
            withheld.append(key)
    out[MARK_PROJECTION] = PROJECTION_SCOPED
    out[MARK_WITHHELD] = withheld
    out[MARK_PARTIAL] = partial
    return out


def _named_sites(value: Any) -> list[str]:
    if isinstance(value, str):
        return [site for site in value.split(",") if site]
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [site for site in value if isinstance(site, str)]
    return []


def project_affected_scope(
    affected_scope: Optional[dict], visible: Optional[Iterable[str]],
) -> dict:
    """A pattern's `affected_scope`: the cohort, and the reader's own sites."""
    stored = affected_scope or {}
    held = _frozen(visible)
    if held is None or not stored:
        return stored
    out: dict[str, Any] = {}
    for key in sorted(stored):
        value = stored[key]
        if key in COHORT_KEYS:
            out[key] = value
        elif key in SITE_LIST_KEYS:
            kept = [site for site in _named_sites(value) if site in held]
            # The stored TYPE is preserved: production writes a CSV string
            # (which the old `_narrow_sites` never narrowed at all).
            out[key] = ",".join(kept) if isinstance(value, str) else kept
    return out


def pattern_named_sites(affected_scope: Optional[dict],
                        evidence: Optional[dict]) -> set[str]:
    """Every site a pattern's own payload names."""
    named: set[str] = set()
    for key in SITE_LIST_KEYS:
        named |= set(_named_sites((affected_scope or {}).get(key)))
    for key in SITE_MAP_KEYS:
        named |= set(_named_sites((evidence or {}).get(key)))
    return named


def pattern_visible(affected_scope: Optional[dict], evidence: Optional[dict],
                    visible: Optional[Iterable[str]]) -> bool:
    """A23-1's rule, unchanged: a pattern that names sites is visible when
    at least one is the reader's; one that names no site is cohort
    knowledge and visible."""
    held = _frozen(visible)
    if held is None:
        return True
    named = pattern_named_sites(affected_scope, evidence)
    return not named or bool(named & held)


# ---------------------------------------------------------------------------
# Generated text
# ---------------------------------------------------------------------------


def evidence_kind(evidence: Optional[dict]) -> str:
    """The pattern type a signal's evidence came from.

    `cc_learned_signals` has no pattern-type column, and one cohort row is
    overwritten by whichever pattern fired last, so the evidence keys are
    the only record of which generator wrote the stored statement.
    """
    keys = set(evidence or {})
    if "trend" in keys:
        return "anomaly"
    if keys & {"fleet_failure_rate", "model_failure_rate"}:
        return "reliability"
    if keys & {"sites_affected", "site_failure_counts"}:
        return "cross_site_batch"
    return "batch_failure"


def _percent(rate: Any, *, signed: bool = False) -> str:
    number = _number(rate) or 0.0
    return f"{number:+.0%}" if signed else f"{number:.0%}"


def bounded_description(pattern_type: str, affected_scope: Optional[dict],
                        projected_evidence: Optional[dict]) -> str:
    """A pattern's `description`, from its PROJECTED evidence.

    Mirrors the four f-strings in `pattern_detector`, minus every count
    and the number of sites. "at more than one site" restates the pattern
    TYPE (which is returned beside it, and is the conclusion itself); it
    is a constant and does not vary with what the reader cannot see.
    """
    scope = affected_scope or {}
    evidence = projected_evidence or {}
    action = scope.get("action_type", "") or "this action"
    cohort = f"{scope.get('vendor', '')} {scope.get('model', '')}".strip() or "this hardware"
    if pattern_type == "anomaly":
        if "trend" in evidence:
            return (f"{action} failure rate increased about "
                    f"{_percent(evidence['trend'], signed=True)} on {cohort}")
        return f"{action} failure rate is rising on {cohort}"
    if pattern_type == "reliability":
        if "model_failure_rate" in evidence and "fleet_failure_rate" in evidence:
            return (f"{cohort} has about {_percent(evidence['model_failure_rate'])} "
                    f"failure rate for {action} vs about "
                    f"{_percent(evidence['fleet_failure_rate'])} fleet average")
        return f"{cohort} fails {action} more often than the fleet average"
    where = ", at more than one site" if pattern_type == "cross_site_batch" else ""
    if "failure_rate" in evidence:
        return (f"{action} fails at about {_percent(evidence['failure_rate'])} "
                f"on {cohort}{where}")
    return f"{action} shows a recurring failure pattern on {cohort}{where}"


_PCT = r"([+-]?\d+(?:\.\d+)?)%"


def _about(match: "re.Match[str]", template: str, *, signed: bool = False) -> str:
    banded = [
        _percent(band_rate(float(group) / 100.0), signed=signed)
        for group in match.groups()
    ]
    return template.format(*banded)


#: (pattern, replacement) for the two generators' grammars -- the signal
#: `statement` and the pattern `description`. Counts are removed; a
#: percentage is kept, banded, behind the word "about".
_REDUCTIONS: tuple[tuple["re.Pattern[str]", Any], ...] = (
    (re.compile(r" \((?:\d+|\?) of \d+ attempts\)"), ""),
    (re.compile(r",? across \d+ sites"), ""),
    (re.compile(r" \(\d+\s*/\s*\d+\)"), ""),
    (re.compile(r", seen \d+x"), ""),
    (re.compile(r"fails " + _PCT + r" of the time"),
     lambda m: _about(m, "fails about {0} of the time")),
    (re.compile(r"against a fleet average of " + _PCT),
     lambda m: _about(m, "against a fleet average of about {0}")),
    (re.compile(r"failure rate moved " + _PCT),
     lambda m: _about(m, "failure rate moved about {0}", signed=True)),
    (re.compile(r"fails at " + _PCT + r" on"),
     lambda m: _about(m, "fails at about {0} on")),
    (re.compile(r"failure rate increased " + _PCT + r" on"),
     lambda m: _about(m, "failure rate increased about {0} on", signed=True)),
    (re.compile(r"has " + _PCT + r" failure rate for"),
     lambda m: _about(m, "has about {0} failure rate for")),
    (re.compile(r"vs " + _PCT + r" fleet average"),
     lambda m: _about(m, "vs about {0} fleet average")),
)

#: What must NOT survive a reduction. If any of these is still present the
#: text is replaced whole: fail closed.
_RESIDUE: tuple["re.Pattern[str]", ...] = (
    re.compile(r"\d+ of \d+"),
    re.compile(r"\d\s*/\s*\d"),
    re.compile(r"\d+ sites?\b"),
    re.compile(r"\d+ attempts?\b"),
    re.compile(r"(?<!about )(?<![\d.+-])[+-]?\d+(?:\.\d+)?%"),
)


def reduce_text(text: Any, visible: Optional[Iterable[str]]) -> Any:
    """Generated text for which NO typed evidence survives, made bounded.

    The frozen copy of a signal on a proposal keeps its statement and not
    its evidence, and a Site Manager's diagnosis cites a pattern by its
    description, so neither can be re-rendered. Both were written by one
    of two generators with a small fixed grammar; this removes the counts
    that grammar can state and bands the percentages. It then PROVES the
    result: anything that still looks like a count replaces the whole
    text with `WITHHELD_STATEMENT`. Text outside the grammar that states
    no count passes unchanged.
    """
    if _frozen(visible) is None or not isinstance(text, str) or not text:
        return text
    reduced = text
    for pattern, replacement in _REDUCTIONS:
        reduced = pattern.sub(replacement, reduced)
    if any(residue.search(reduced) for residue in _RESIDUE):
        return WITHHELD_STATEMENT
    return reduced


def project_statement(
    statement: Any,
    stored_evidence: Optional[dict],
    projected_evidence: Optional[dict],
    *,
    action_type: str,
    vendor: str,
    model: str,
    scope_type: str,
    visible: Optional[Iterable[str]],
) -> Any:
    """A signal's `statement`. THE TEXT FOLLOWS ITS EVIDENCE.

    Where typed evidence exists the statement is re-rendered from the
    PROJECTED evidence by the generator that wrote it, so it cannot state
    a number the structured payload withheld. Where none exists there is
    nothing to render from, and the stored text is reduced instead.
    """
    if _frozen(visible) is None:
        return statement
    if stored_evidence:
        return render_statement(
            evidence_kind(stored_evidence), action_type, vendor, model,
            projected_evidence or {}, scope_type,
            "this site" if scope_type == SCOPE_SITE else "",
            approximate=True,
        )
    return reduce_text(statement, visible)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

_SIGNAL_FIELDS = (
    "id", "tenant_id", "signal_key", "scope_type", "scope_ref", "action_type",
    "vendor", "model", "statement", "evidence", "confidence",
    "source_pattern_id", "source_cycle_id", "status", "observation_count",
    "first_observed_at", "last_confirmed_at",
)
_PATTERN_FIELDS = (
    "id", "pattern_id", "tenant_id", "pattern_type", "description",
    "affected_scope", "confidence", "evidence", "status", "detected_at",
    "resolved_at",
)


def _copy(row: Any, fields: Iterable[str]) -> SimpleNamespace:
    return SimpleNamespace(**{
        name: getattr(row, name) for name in fields if hasattr(row, name)
    })


def project_signal(row: Any, visible: Optional[Iterable[str]]) -> Optional[Any]:
    """One learned-signal row for one reader; ``None`` when not theirs.

    A site-scoped signal follows its site (A23, unchanged). A cohort
    signal is tenant knowledge and is always returned -- with its
    supporting evidence projected.
    """
    held = _frozen(visible)
    if held is None:
        return row
    scope_type = getattr(row, "scope_type", "") or ""
    scope_ref = getattr(row, "scope_ref", "") or ""
    if scope_type == SCOPE_SITE and scope_ref not in held:
        return None
    stored = getattr(row, "evidence", None) or {}
    projected = project_evidence(
        stored, held, own_site=scope_ref if scope_type == SCOPE_SITE else None,
    )
    out = _copy(row, _SIGNAL_FIELDS)
    out.evidence = projected
    out.statement = project_statement(
        getattr(row, "statement", ""), stored, projected,
        action_type=getattr(row, "action_type", "") or "",
        vendor=getattr(row, "vendor", "") or "",
        model=getattr(row, "model", "") or "",
        scope_type=scope_type, visible=held,
    )
    out.confidence = band_confidence(getattr(row, "confidence", 0.0))
    for name in ("first_observed_at", "last_confirmed_at"):
        if hasattr(out, name):
            setattr(out, name, bound_instant(getattr(out, name)))
    return out


def project_signals(rows: Iterable[Any],
                    visible: Optional[Iterable[str]]) -> list:
    """Every signal a reader may see, bounded -- and RE-ORDERED.

    The repository orders by the exact stored confidence. Returned in that
    order, two signals in one confidence band would still be ranked by the
    exact value the band exists to withhold, so a scoped reader's rows are
    ordered by what they are actually shown.
    """
    held = _frozen(visible)
    if held is None:
        return list(rows)
    projected = [project_signal(row, held) for row in rows]
    kept = [row for row in projected if row is not None]
    kept.sort(key=lambda row: (
        -float(getattr(row, "confidence", 0.0) or 0.0),
        str(getattr(row, "signal_key", "") or ""),
    ))
    return kept


def project_pattern(row: Any, visible: Optional[Iterable[str]], *,
                    hide_unnamed: bool = True) -> Optional[Any]:
    """One fleet-pattern row for one reader; ``None`` when not visible.

    `hide_unnamed` is A23-1's READ rule: a pattern whose named sites are
    all outside the reader's scope is absent. Distribution to a Site
    Manager passes ``False``: a site that holds the cohort and is not yet
    failing is exactly who the fleet learning loop exists to tell (R-C2),
    and what it is told is the bounded cohort conclusion and nothing else.
    """
    held = _frozen(visible)
    if held is None:
        return row
    affected_scope = getattr(row, "affected_scope", None) or {}
    stored = getattr(row, "evidence", None) or {}
    if hide_unnamed and not pattern_visible(affected_scope, stored, held):
        return None
    projected = project_evidence(stored, held)
    out = _copy(row, _PATTERN_FIELDS)
    out.evidence = projected
    out.affected_scope = project_affected_scope(affected_scope, held)
    out.description = (
        bounded_description(getattr(row, "pattern_type", ""), affected_scope, projected)
        if stored else reduce_text(getattr(row, "description", ""), held)
    )
    out.confidence = band_confidence(getattr(row, "confidence", 0.0))
    for name in ("detected_at", "resolved_at"):
        if hasattr(out, name):
            setattr(out, name, bound_instant(getattr(out, name)))
    return out


def pattern_order(row: Any) -> tuple:
    """Where a PROJECTED pattern sorts: by what the reader is shown.

    Newest first, by the floored instant -- then by the cohort it names,
    which is a function of the conclusion and never of the estate. The
    repository's order is by the exact `detected_at`, which within one
    pass is the cohorts' rank by tenant attempt total (`bound_instant`).
    The id is last and only makes the order total: it is random.
    """
    scope = getattr(row, "affected_scope", None) or {}
    return (
        _instant_key(getattr(row, "detected_at", None)),
        str(getattr(row, "pattern_type", "") or ""),
        str(scope.get("action_type", "") or ""),
        str(scope.get("vendor", "") or ""),
        str(scope.get("model", "") or ""),
        str(getattr(row, "pattern_id", "") or getattr(row, "id", "") or ""),
    )


def project_patterns(rows: Iterable[Any],
                     visible: Optional[Iterable[str]], *,
                     limit: Optional[int] = None) -> list:
    """Every pattern a reader may see, bounded -- and RE-ORDERED.

    `limit` is applied AFTER projection for a scoped reader. Applied
    before it (in SQL, over the exact `detected_at`), `?limit=1` hands
    back the cohort with the SMALLEST tenant total of its pass.
    """
    held = _frozen(visible)
    if held is None:
        rows = list(rows)
        return rows if limit is None else rows[:limit]
    projected = (project_pattern(row, held) for row in rows)
    kept = sorted((row for row in projected if row is not None), key=pattern_order)
    return kept if limit is None else kept[:limit]


# ---------------------------------------------------------------------------
# Frozen copies
# ---------------------------------------------------------------------------


def project_frozen_signals(entries: Optional[Iterable[Any]],
                           visible: Optional[Iterable[str]]) -> list:
    """The learned signals a proposal RECORDED, for one reader.

    The evaluator stores `{signal_id, statement, confidence, scope_type,
    scope_ref}` for every signal it reasoned with, and it reasons over the
    whole tenant. What was written is a decision record and is not
    rewritten; what is returned drops a site-scoped entry whose site the
    reader does not hold (A30.26, unchanged) and bounds the rest.

    It also RE-ORDERS them. The evaluator wrote the list in the
    repository's order, which is by exact confidence, so two entries in
    one band would still be ranked by the number the band withholds. The
    order returned is by what is shown, then by the signal's own id.
    """
    rows = list(entries or [])
    held = _frozen(visible)
    if held is None:
        return rows
    out = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        if (entry.get("scope_type") == SCOPE_SITE
                and entry.get("scope_ref") not in held):
            continue
        bounded = dict(entry)
        if "statement" in bounded:
            bounded["statement"] = reduce_text(bounded["statement"], held)
        if "confidence" in bounded:
            bounded["confidence"] = band_confidence(bounded["confidence"])
        if "evidence" in bounded:
            bounded["evidence"] = project_evidence(
                bounded["evidence"], held,
                own_site=(entry.get("scope_ref")
                          if entry.get("scope_type") == SCOPE_SITE else None),
            )
        out.append(bounded)
    # By what is SHOWN; the row id is a random uuid and only makes it total.
    out.sort(key=lambda entry: (
        -float(_number(entry.get("confidence")) or 0.0),
        str(entry.get("scope_type") or ""),
        str(entry.get("scope_ref") or ""),
        str(entry.get("statement") or ""),
        str(entry.get("signal_id") or ""),
    ))
    return out


# ---------------------------------------------------------------------------
# A pattern cited by a Site Manager's diagnosis
# ---------------------------------------------------------------------------

#: The key the Site Manager wraps a cited pattern in (`ingest.py`,
#: `_matching_fleet_patterns`); `reasoning.py` stores `str(dict)`.
_CITATION_MARK = "fleet_pattern"
_CITED_CONFIDENCE = re.compile(r"('confidence':\s*)(\d+(?:\.\d+)?)")

#: What replaces a pattern citation that cannot be proven bounded.
WITHHELD_CITATION = (
    "{'fleet_pattern': 'a fleet pattern was cited; its detail is outside "
    "your authorized scope'}"
)


def project_citation(text: Any, visible: Optional[Iterable[str]]) -> Any:
    """One `evidence_cited` entry of an incident diagnosis.

    ONLY an entry that cites a fleet pattern is touched. Everything else in
    that list is the device's own telemetry -- "3 of 5 fans", "95%" -- and
    reducing it would destroy the diagnosis to protect nothing.
    """
    if _frozen(visible) is None or not isinstance(text, str):
        return text
    if _CITATION_MARK not in text:
        return text
    reduced = reduce_text(text, visible)
    if reduced == WITHHELD_STATEMENT:
        return WITHHELD_CITATION
    return _CITED_CONFIDENCE.sub(
        lambda m: f"{m.group(1)}{band_confidence(float(m.group(2)))}", reduced,
    )


_CITED_ID = re.compile(r"'pattern_id':\s*'[^']*',?\s*")


def _citation_order(text: Any) -> tuple:
    """By what the citation SAYS; its random pattern id only breaks ties."""
    shown = str(text)
    return (_CITED_ID.sub("", shown), shown)


def project_citations(entries: Any, visible: Optional[Iterable[str]]) -> Any:
    """`evidence_cited`, with its pattern citations bounded AND re-ordered.

    A Site Manager cites patterns in the order Central Command pushed
    them, which was detection order -- the rank `bound_instant` describes.
    The citations are sorted by their bounded text and put back into the
    slots citations occupied, so the device's own telemetry keeps its
    place and its order.
    """
    if _frozen(visible) is None or not isinstance(entries, (list, tuple)):
        return entries
    out = [project_citation(entry, visible) for entry in entries]
    slots = [
        i for i, entry in enumerate(entries)
        if isinstance(entry, str) and _CITATION_MARK in entry
    ]
    ordered = sorted((out[i] for i in slots), key=_citation_order)
    for slot, text in zip(slots, ordered):
        out[slot] = text
    return out


# ---------------------------------------------------------------------------
# A learning cycle
# ---------------------------------------------------------------------------

#: Cycle fields that COUNT the estate: how many sites hold the cohort, how
#: many devices match it.
CYCLE_COUNT_FIELDS = ("sites_distributed", "devices_applied")
#: Grid a scoped reader's `improvement_pct` (percentage points) is rounded to.
IMPROVEMENT_STEP = 5.0
#: Cycle fields that are instants.
CYCLE_INSTANT_FIELDS = ("started_at", "updated_at", "completed_at")


def project_cycle(payload: dict, visible: Optional[Iterable[str]]) -> dict:
    """One learning-cycle payload for one reader.

    A cycle names no site, which is why the route was declared UNSCOPED.
    It does COUNT them: `sites_distributed` is how many sites hold the
    cohort, `devices_applied` how many devices match it, and
    `outcomes_before/after.total` are tenant attempt totals. Same bounded
    rule as every other learning payload.
    """
    held = _frozen(visible)
    if held is None:
        return payload
    out = dict(payload)
    withheld = []
    for name in CYCLE_COUNT_FIELDS:
        if name in out:
            out[name] = None
            withheld.append(name)
    for name in ("outcomes_before", "outcomes_after"):
        if name in out:
            out[name] = project_evidence(out.get(name) or {}, held)
    improvement = _number(out.get("improvement_pct"))
    if improvement is not None:
        steps = math.floor(abs(improvement) / IMPROVEMENT_STEP + 0.5 + 1e-9)
        out["improvement_pct"] = math.copysign(steps * IMPROVEMENT_STEP, improvement)
    for name in CYCLE_INSTANT_FIELDS:
        if name in out:
            out[name] = bound_instant(out[name])
    out[MARK_PROJECTION] = PROJECTION_SCOPED
    out[MARK_WITHHELD] = withheld
    return out


def project_cycles(payloads: Iterable[dict],
                   visible: Optional[Iterable[str]]) -> list:
    """Every cycle payload, bounded -- and RE-ORDERED for a scoped reader.

    A cycle is opened per detected pattern, in detection order, so the
    repository's `started_at DESC` is the same rank `bound_instant`
    describes. Ordered by what is shown; the random id only makes it total.
    """
    held = _frozen(visible)
    if held is None:
        return list(payloads)
    projected = [project_cycle(payload, held) for payload in payloads]
    projected.sort(key=lambda c: (
        _instant_key(c.get("started_at")),
        str(c.get("pattern_type") or ""),
        repr(sorted((c.get("outcomes_before") or {}).items(), key=str)),
        str(c.get("status") or ""),
        str(c.get("cycle_id") or ""),
    ))
    return projected


# ---------------------------------------------------------------------------
# Generated content: a diagnosis block, a candidate's YAML (A30.29)
# ---------------------------------------------------------------------------

#: The generated fields of an incident diagnosis. Named so that the
#: VISIBLE shape and the WITHHELD shape are built from the same list and
#: cannot drift; a provider that writes a field outside this list does
#: not get it shown to anyone through `project_generated` -- it is not in
#: the contract.
GENERATED_FIELDS = ("summary", "suggested_action", "reasoning_steps")

#: What a scoped reader is told in place of a generated block whose
#: projection they are not proven to hold. One sentence for "recorded for
#: a site you do not hold" and for "not recorded": which of the two it is
#: would itself be a fact about the hidden estate.
WITHHELD_GENERATED = (
    "the generated explanation is withheld: it was produced from evidence "
    "whose projection is outside your authorized scope or was not recorded"
)

#: The withheld block. Constants only -- nothing from the stored document
#: is copied into it, which is what makes the withholding structural.
WITHHELD_GENERATED_BLOCK: dict[str, Any] = {
    "summary": WITHHELD_GENERATED,
    "suggested_action": "",
    "reasoning_steps": [],
    "withheld": True,
}


def generation_visible(marker: Any, visible: Optional[Iterable[str]]) -> bool:
    """May THIS reader be shown content generated under `marker`?

    A tenant-wide reader: always. Anyone else: only a marker that parses
    and names a site they hold now. The rule is `generation_provenance.
    covers`; this is the one place the projection asks it.
    """
    held = _frozen(visible)
    return generation_covers(marker, None if held is None else held)


def project_generation_visibility(marker: Any, visible: Optional[Iterable[str]]) -> Optional[dict]:
    """The marker as the reader may see it: the parsed marker when the
    reader is covered by it (or tenant-wide), else ``None``. A marker
    names a site, and a reader who does not hold that site is not told
    which site it is."""
    if not generation_visible(marker, visible):
        return None
    parsed = parse_generation_visibility(marker)
    return parsed.to_dict() if parsed is not None else None


def project_generated(explanation: Optional[dict],
                      visible: Optional[Iterable[str]]) -> tuple[dict, Optional[dict]]:
    """(the `generated` block, the visible marker) of an incident diagnosis.

    The visible block is built by NAMING `GENERATED_FIELDS` -- a field the
    provider wrote outside that list is not carried -- plus
    `withheld: False`. The withheld block is `WITHHELD_GENERATED_BLOCK`,
    constants only. A tenant-wide reader gets the stored fields whatever
    the marker says, including when there is none.
    """
    stored = explanation or {}
    marker = stored.get(GENERATION_VISIBILITY_KEY)
    if not generation_visible(marker, visible):
        return dict(WITHHELD_GENERATED_BLOCK), None
    block: dict[str, Any] = {
        "summary": stored.get("summary", ""),
        "suggested_action": stored.get("suggested_action", ""),
        "reasoning_steps": stored.get("reasoning_steps", []),
        "withheld": False,
    }
    return block, project_generation_visibility(marker, visible)


#: A candidate's generated fields: the YAML, and the validation warnings
#: derived from it.
CANDIDATE_GENERATED_FIELDS = ("yaml_text", "warnings")


def project_candidate(row: Any, visible: Optional[Iterable[str]]) -> dict:
    """The generated part of one candidate row, for one reader.

    Returns `{"yaml_text", "warnings", "generated_withheld",
    "generation_visibility"}`. Withheld: empty text, no warnings, `True`,
    ``None`` -- constants, nothing copied. The row's own site, device,
    component, validation state and match count are not generated and
    are the caller's to return.
    """
    marker = getattr(row, "generation_visibility", None)
    if not generation_visible(marker, visible):
        return {
            "yaml_text": "",
            "warnings": [],
            "generated_withheld": True,
            "generation_visibility": None,
        }
    return {
        "yaml_text": getattr(row, "yaml_text", "") or "",
        "warnings": list(getattr(row, "warnings", None) or []),
        "generated_withheld": False,
        "generation_visibility": project_generation_visibility(marker, visible),
    }
