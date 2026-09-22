"""Generation provenance: which projection an artifact was generated from.

A Site Manager generates text -- an incident's LLM diagnosis, the YAML of
a candidate skill -- from a prompt that carries the device's own telemetry
AND the fleet patterns Central Command pushed to it. Since A30.28 a pushed
pattern is one site's bounded projection; before it, the pattern was the
whole tenant's ("... across 3 sites (35/54)"). Once the model has written
the sentence there is no telling, from the sentence, which it saw.

So the writer records it (spec A30.29). ``generation_visibility`` is a
marker that describes the AUTHORIZATION BOUNDARY of the evidence
projection an artifact was generated from -- never the evidence itself:

    {"scope": "site",   "site_id": "<canonical Central Command site id>", "projection_version": 1}
    {"scope": "tenant", "site_id": null,                                    "projection_version": 1}

It carries no site list, no count, no total, no fault name, no metric, no
rationale and no signal content. A reader compares it against the
reader's CURRENT canonical reach; it is evidence of how the artifact was
made and confers nothing.

This module is the ONE definition of the marker's vocabulary and algebra.
It lives in the `harkeniq` package because the Site Manager writes the
marker and Central Command reads it, and two copies of the rule would be
two rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

#: The key an incident explanation carries the marker under, the column a
#: candidate carries it in, and the key a distributed pattern carries it
#: under. One name everywhere.
KEY = "generation_visibility"

SCOPE_SITE = "site"
SCOPE_TENANT = "tenant"
SCOPES = frozenset({SCOPE_SITE, SCOPE_TENANT})

#: The version of the bounded projection the marker vouches for. A reader
#: refuses a marker from a NEWER version than it understands: it cannot
#: know what that projection admitted.
PROJECTION_VERSION = 1

#: Exactly the keys a marker may carry. Anything else in a stored marker
#: is refused whole -- a marker is a boundary, and a boundary that grew an
#: evidence key would be the leak wearing the badge that says it is not.
MARKER_KEYS = frozenset({"scope", "site_id", "projection_version"})


@dataclass(frozen=True)
class GenerationVisibility:
    """The projection boundary an artifact was generated from."""

    scope: str
    site_id: Optional[str]
    projection_version: int = PROJECTION_VERSION

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "site_id": self.site_id,
            "projection_version": self.projection_version,
        }

    def covered_by(self, sites: Optional[Iterable[str]]) -> bool:
        """May a reader holding `sites` be shown what this marker covers?

        ``None`` is a tenant-wide reader, who is shown everything. Any other
        value is the exact set of sites the reader holds now (A30.28's
        `LearningView.sites`): a `site` marker is covered when its site is
        in that set; a `tenant` marker is covered by nobody narrower than
        the tenant. No marker at all is handled by the caller (`parse`
        returns ``None``, and ``None`` covers nothing).
        """
        if sites is None:
            return True
        if self.scope != SCOPE_SITE or not self.site_id:
            return False
        return self.site_id in frozenset(sites)


def site_visibility(site_id: str) -> GenerationVisibility:
    """The facts of ONE canonical Central Command site."""
    if not site_id:
        raise ValueError("a site visibility needs a canonical site id")
    return GenerationVisibility(SCOPE_SITE, str(site_id))


def tenant_visibility() -> GenerationVisibility:
    """Evidence bounded to nothing narrower than the tenant."""
    return GenerationVisibility(SCOPE_TENANT, None)


def parse(value: Any) -> Optional[GenerationVisibility]:
    """A stored or pushed marker, or ``None`` for UNKNOWN.

    Fails closed on everything it does not recognise: a missing marker,
    a non-mapping, an unknown scope, a `site` scope without a site id, a
    `tenant` scope WITH one, a projection version newer than this reader,
    or any key outside `MARKER_KEYS`. Unknown provenance withholds
    generated content from every scoped reader (spec A30.29).
    """
    if not isinstance(value, Mapping):
        return None
    if set(value) - MARKER_KEYS:
        return None
    scope = value.get("scope")
    if scope not in SCOPES:
        return None
    version = value.get("projection_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    if version < 1 or version > PROJECTION_VERSION:
        return None
    site_id = value.get("site_id")
    if scope == SCOPE_SITE:
        if not isinstance(site_id, str) or not site_id:
            return None
        return GenerationVisibility(SCOPE_SITE, site_id, version)
    if site_id is not None:
        return None
    return GenerationVisibility(SCOPE_TENANT, None, version)


def combine(parts: Iterable[Optional[GenerationVisibility]]) -> Optional[GenerationVisibility]:
    """The visibility of an artifact generated from several inputs: their JOIN.

    * unknown (``None``) anywhere  -> unknown. It is contagious: one input
      the writer cannot vouch for makes the whole artifact unvouchable;
    * `tenant` anywhere            -> `tenant`;
    * one site throughout          -> that site;
    * two different sites          -> `tenant`. Only tenant-wide authority
      covers evidence of more than one site, and the vocabulary has no
      multi-site form on purpose: a list of sites in the marker would be a
      site list, which the marker may not carry;
    * no inputs at all             -> unknown. An artifact generated from
      nothing is not a thing the writer has described.
    """
    seen_any = False
    site: Optional[str] = None
    tenant = False
    for part in parts:
        seen_any = True
        if part is None:
            return None
        if part.scope == SCOPE_TENANT:
            tenant = True
        elif site is None:
            site = part.site_id
        elif part.site_id != site:
            tenant = True
    if not seen_any:
        return None
    if tenant:
        return tenant_visibility()
    return site_visibility(site) if site else None


def covers(marker: Any, sites: Optional[Iterable[str]]) -> bool:
    """One call for readers: parse the stored marker and ask `covered_by`.

    A tenant-wide reader (``sites is None``) is covered whatever is stored,
    including nothing; everyone else needs a marker that parses AND names a
    site they hold now.
    """
    if sites is None:
        return True
    parsed = parse(marker)
    return parsed is not None and parsed.covered_by(sites)
