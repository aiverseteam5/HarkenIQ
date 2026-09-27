"""Trust classes for text Central Command publishes (spec A30.34, D5).

A consumer of an incident -- a person, and since A6 a language model --
must be able to tell, from a field's POSITION, what kind of string it holds:
a closed code, an opaque id, a timestamp, or free text whose origin is
stated. This module is the ONE statement of the free-text classes and of
how a class is derived. There is no second copy; the human diagnosis label
and the machine projection both ask it.

The four classes, least trusted last:

* ``deterministic`` -- produced by HarkenIQ from its own vocabulary and
  values it generated itself.
* ``operator_supplied`` -- entered by the tenant: a site's name, a device's
  name. Attributable, not validated.
* ``untrusted_telemetry`` -- reported by a device or its BMC: vendor,
  model, component names, skill names. A compromised controller writes
  these (Platform-Design §16: tag telemetry as untrusted-origin).
* ``untrusted_generated`` -- written by a model. Evidence to reason ABOUT,
  never an instruction to follow.

DERIVED BY ALLOW-LIST, NEVER BY EXCLUSION. The label this replaces asked
"is the provider exactly 'llm'?" and called everything else deterministic,
so a missing provider, "LLM", or any name a future reasoner chose read as
platform text (B2-F3). Here a provider earns a class only by being one of
the three the Site Manager declares (`harkeniq_sm.reasoning`:
``"deterministic" | "knowledge_base" | "llm"``), matched exactly; anything
else -- missing, mis-cased, a number, a dict -- is ``untrusted_generated``.

THE COMPOSITION RULE. A string carries the least-trusted class of anything
interpolated into it. The two non-LLM providers write HarkenIQ templates,
but the templates interpolate the device-reported component, severity and
device id ("Correlation-based analysis for psu:PSU1"), so their text is
``untrusted_telemetry``; a learned-signal statement interpolates the
BMC-reported vendor and model, so it is too.
"""

from __future__ import annotations

from typing import Any

TRUST_DETERMINISTIC = "deterministic"
TRUST_OPERATOR_SUPPLIED = "operator_supplied"
TRUST_UNTRUSTED_TELEMETRY = "untrusted_telemetry"
TRUST_UNTRUSTED_GENERATED = "untrusted_generated"

#: The closed vocabulary, least trusted LAST. The order is the composition
#: rule's order and nothing else.
TRUST_CLASSES: tuple[str, ...] = (
    TRUST_DETERMINISTIC,
    TRUST_OPERATOR_SUPPLIED,
    TRUST_UNTRUSTED_TELEMETRY,
    TRUST_UNTRUSTED_GENERATED,
)
_RANK = {name: rank for rank, name in enumerate(TRUST_CLASSES)}

#: The reasoning providers the Site Manager declares, and `unknown` for
#: every other value a stored explanation might carry.
ORIGIN_LLM = "llm"
ORIGIN_DETERMINISTIC = "deterministic"
ORIGIN_KNOWLEDGE_BASE = "knowledge_base"
ORIGIN_UNKNOWN = "unknown"
ORIGINS: frozenset[str] = frozenset({
    ORIGIN_LLM, ORIGIN_DETERMINISTIC, ORIGIN_KNOWLEDGE_BASE, ORIGIN_UNKNOWN,
})

#: The allow-list. A provider absent from it is untrusted_generated.
PROVIDER_TRUST: dict[str, str] = {
    ORIGIN_LLM: TRUST_UNTRUSTED_GENERATED,
    # HarkenIQ templates over device-reported values (the composition rule).
    ORIGIN_DETERMINISTIC: TRUST_UNTRUSTED_TELEMETRY,
    ORIGIN_KNOWLEDGE_BASE: TRUST_UNTRUSTED_TELEMETRY,
}


def least_trusted(*classes: str) -> str:
    """The composition rule: the least-trusted class among the inputs.

    An unrecognised class is treated as the least trusted there is -- a
    label nobody can read must never lend text a better standing.
    """
    unreadable = _RANK[TRUST_UNTRUSTED_GENERATED]
    worst = max((_RANK.get(name, unreadable) for name in classes), default=0)
    return TRUST_CLASSES[worst]


def diagnosis_origin(provider: Any) -> str:
    """The closed `origin` of a diagnosis: a declared provider, or unknown.

    Exact match on a string. "LLM", " llm", 42 and a dict are all unknown:
    the raw value a Site Manager wrote is never echoed to a machine.
    """
    if isinstance(provider, str) and provider in PROVIDER_TRUST:
        return provider
    return ORIGIN_UNKNOWN


def diagnosis_trust(provider: Any) -> str:
    """The class of a diagnosis's text, by allow-list (A30.34, D5)."""
    if isinstance(provider, str):
        return PROVIDER_TRUST.get(provider, TRUST_UNTRUSTED_GENERATED)
    return TRUST_UNTRUSTED_GENERATED


def human_diagnosis_trust(provider: Any) -> str:
    """The same derivation, projected onto the human field's two values.

    D5b: `diagnosis.trust` on the human payload has always said
    ``untrusted_generated`` or ``deterministic`` -- model text or not. It
    keeps exactly those two words, now read from the one derivation, so the
    only thing that changes is the fail-open: a missing or unrecognised
    provider is model text until proven otherwise.
    """
    if diagnosis_trust(provider) == TRUST_UNTRUSTED_GENERATED:
        return TRUST_UNTRUSTED_GENERATED
    return TRUST_DETERMINISTIC
