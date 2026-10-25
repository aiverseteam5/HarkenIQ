"""The Resource Catalogue vocabulary (TX-1 spec A31, TX-2 spec A32).

Pure: no session, no request. The repository enforces these rules on
every write; the mirror is the only writer.

What the catalogue is
---------------------
A DESCRIPTION of the estate -- what a thing is and how things relate --
kept beside Site and Device, never in place of them. It decides nothing.
Site and Device stay authoritative for authorization, execution, safety,
dispatch and correlation (A31.1), and the catalogue could be deleted
without any of those answers moving.

Separate dimensions, deliberately
---------------------------------
``resource_kind`` (structural category), ``resource_type`` (namespaced,
extensible), ``device_class`` (the existing, authorization-sensitive
class), role, vendor/model and capabilities are different questions.
`resource_type` is never an authorization input, `device_class` is not
read here for any decision, and no capability reader consults any of it
(A17: capabilities are executor-declared).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Kinds: the stable structural categories (A31.2). Closed -- a new kind is
# an amendment, not a string somebody types.
# ---------------------------------------------------------------------------

KIND_PHYSICAL_CONTAINER = "physical_container"
KIND_PHYSICAL_DEVICE = "physical_device"
#: Vocabulary only in TX-1: nothing writes a component, nothing reads one.
KIND_COMPONENT = "component"
#: Vocabulary only in TX-1: nothing writes a logical resource, nothing reads one.
KIND_LOGICAL_RESOURCE = "logical_resource"

RESOURCE_KINDS = frozenset({
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    KIND_COMPONENT,
    KIND_LOGICAL_RESOURCE,
})

#: Primary containment is PHYSICAL containment. Its parent must be a
#: physical container or device -- which is what keeps a Site Manager or an
#: Operational Agent from ever being anything's physical parent, however a
#: later slice represents them -- and its child a physical kind or a
#: component. A logical resource relates through typed edges (HOSTS,
#: RUNS_ON, MEMBER_OF ...), never as a physical child (A31.2).
PRIMARY_PARENT_KINDS = frozenset({KIND_PHYSICAL_CONTAINER, KIND_PHYSICAL_DEVICE})
PRIMARY_CHILD_KINDS = frozenset({
    KIND_PHYSICAL_CONTAINER, KIND_PHYSICAL_DEVICE, KIND_COMPONENT,
})

# ---------------------------------------------------------------------------
# Lifecycle. `active` while the legacy object exists, `retired` once it is
# gone. A retired resource is a tombstone: it keeps its id, its last
# placement and its legacy link, so a historical deep link still resolves.
# ---------------------------------------------------------------------------

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_STATES = frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED})

# ---------------------------------------------------------------------------
# Relationships, read as "<from> TYPE <to>": site CONTAINS device,
# vm RUNS_ON host, host HOSTS vm, port CONNECTED_TO port, device MEMBER_OF
# group, site MANAGED_BY site manager. A relationship describes; it never
# authorizes, in any direction (A31.1).
#
# An edge carries the resource lifecycle vocabulary too (TX-2, A32.2): it is
# `active` while its source asserts it and `retired` once that source stops
# -- a former placement, kept rather than rewritten or deleted. One row per
# (from, to, type) for life: asserted again, the same row revives.
# ---------------------------------------------------------------------------

REL_CONTAINS = "CONTAINS"
REL_HOSTS = "HOSTS"
REL_RUNS_ON = "RUNS_ON"
REL_CONNECTED_TO = "CONNECTED_TO"
REL_MEMBER_OF = "MEMBER_OF"
REL_MANAGED_BY = "MANAGED_BY"

RELATIONSHIP_TYPES = frozenset({
    REL_CONTAINS, REL_HOSTS, REL_RUNS_ON,
    REL_CONNECTED_TO, REL_MEMBER_OF, REL_MANAGED_BY,
})

#: How far up a containment chain a cycle check walks before it refuses. A
#: real estate is a handful of levels deep; a chain longer than this is a
#: defect, and refusing is the fail-closed answer to it.
MAX_CONTAINMENT_DEPTH = 32
#: How many ancestors one cycle check may visit (TX-2, A32.3). The walk
#: follows every ACTIVE `CONTAINS` edge, secondary ones included, so it is a
#: breadth-first search rather than one chain; a search this wide is a
#: defect too, and is refused rather than allowed to run unbounded.
MAX_CONTAINMENT_WALK = 1024

#: The topology reads' bounds (TX-2, A32.8). A subtree is cut at this
#: depth and this many VISIBLE nodes; `truncated` reports the cut and counts
#: nothing the reader could not already see.
MAX_TOPOLOGY_DEPTH = 8
MAX_TOPOLOGY_NODES = 500

# ---------------------------------------------------------------------------
# The legacy link: which canonical object a resource mirrors. Written by
# the mirror and by nothing else, and the ONLY input to visibility.
#
# `rack` is A31.3's contract, implemented by TX-2 (A32.4): a Site Manager
# rack, by its opaque id. A resource carrying a legacy kind outside this set
# is visible to no scoped reader.
# ---------------------------------------------------------------------------

LEGACY_SITE = "site"
LEGACY_DEVICE = "device"
LEGACY_RACK = "rack"
LEGACY_KINDS = frozenset({LEGACY_SITE, LEGACY_DEVICE, LEGACY_RACK})

#: Provenance of every row the mirror writes.
SOURCE_LEGACY_MIRROR = "legacy_mirror"

# ---------------------------------------------------------------------------
# Types. `namespace:domain.type`. The `harkeniq` namespace is reserved for
# the built-in registry, which holds EXACTLY the types the mirror writes.
# Tenant and vendor namespaces (open decision 4) are not opened, so a type
# outside the registry is refused rather than accepted on its say-so.
#
# The other contextual containers -- region, campus, datacenter, building,
# room, hall, zone, row, chassis -- are allowed by the model (any physical
# container may contain), and their type names are RESERVED in A32.6, but
# none is registered here: nothing in the platform is an authoritative
# source for one, and a registry entry with no writer is a declaration the
# platform cannot keep. The slice that adds a writer registers its types.
# ---------------------------------------------------------------------------

HARKENIQ_NAMESPACE = "harkeniq"

#: An operational site -- deliberately not "datacenter": a site may be a
#: datacenter, an edge location or a building, and which is open decision 1.
TYPE_SITE = "harkeniq:operational.site"
TYPE_SERVER = "harkeniq:compute.server"
TYPE_SWITCH = "harkeniq:network.switch"
#: What a mirrored device is when its class says nothing usable. Never
#: "server": a blank class is not evidence of anything (A30.25 R1).
TYPE_UNCLASSIFIED_DEVICE = "harkeniq:device.unclassified"
#: A Site Manager rack, operator-assigned (TX-2, A32.4).
TYPE_RACK = "harkeniq:placement.rack"

#: type -> the one kind it may describe.
BUILTIN_TYPES: dict[str, str] = {
    TYPE_SITE: KIND_PHYSICAL_CONTAINER,
    TYPE_RACK: KIND_PHYSICAL_CONTAINER,
    TYPE_SERVER: KIND_PHYSICAL_DEVICE,
    TYPE_SWITCH: KIND_PHYSICAL_DEVICE,
    TYPE_UNCLASSIFIED_DEVICE: KIND_PHYSICAL_DEVICE,
}

#: A mirrored device's DESCRIPTIVE default type, from its current class.
#: Never read back: visibility and authority come from the fleet row's own
#: class, through the canonical predicates, and never from this.
LEGACY_DEVICE_CLASS_TYPES: dict[str, str] = {
    "server": TYPE_SERVER,
    "switch": TYPE_SWITCH,
}

_TYPE_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}:[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,63})+$"
)
MAX_TYPE_LENGTH = 128

# ---------------------------------------------------------------------------
# External identifiers HarkenIQ issues itself. The only ones bound: their
# uniqueness is guaranteed by HarkenIQ, so binding them can never merge two
# things. Vendor identifiers (service tags) need reconciliation rules and
# are not bound (A31.9).
# ---------------------------------------------------------------------------

EXTERNAL_SOURCE_HARKENIQ = "harkeniq"
NAMESPACE_SITE = "site"
NAMESPACE_HARKEN_NODE = "harken_node"
#: The Site Manager's rack id (TX-2, A32.4).
NAMESPACE_RACK = "rack"


class CatalogueError(ValueError):
    """A catalogue rule was violated. Nothing was written."""


class IdentityConflict(CatalogueError):
    """An external identifier already names a DIFFERENT resource.

    Refused, never resolved: merging two resources because one identifier
    claims both is exactly the silent identity merge A31.2 forbids.
    """


def device_type_for_class(device_class: str | None) -> str:
    """The descriptive default type for a mirrored device's class."""
    return LEGACY_DEVICE_CLASS_TYPES.get(
        (device_class or "").strip().lower(), TYPE_UNCLASSIFIED_DEVICE
    )


def is_well_formed_type(resource_type: str) -> bool:
    return (
        bool(resource_type)
        and len(resource_type) <= MAX_TYPE_LENGTH
        and _TYPE_RE.match(resource_type) is not None
    )


def validate_kind(kind: str) -> str:
    if kind not in RESOURCE_KINDS:
        raise CatalogueError(
            f"unknown resource_kind {kind!r}; the vocabulary is closed: "
            f"{sorted(RESOURCE_KINDS)}"
        )
    return kind


def validate_lifecycle(state: str) -> str:
    if state not in LIFECYCLE_STATES:
        raise CatalogueError(
            f"unknown lifecycle_state {state!r}; expected one of "
            f"{sorted(LIFECYCLE_STATES)}"
        )
    return state


def validate_type(resource_type: str, kind: str) -> str:
    """A type TX-1 may write: well-formed, built in, and of `kind`."""
    validate_kind(kind)
    if not is_well_formed_type(resource_type):
        raise CatalogueError(
            f"resource_type {resource_type!r} is not 'namespace:domain.type'"
        )
    if resource_type not in BUILTIN_TYPES:
        raise CatalogueError(
            f"resource_type {resource_type!r} is not in the built-in registry; "
            "tenant and vendor type namespaces are not open in TX-1 (A31.2)"
        )
    if BUILTIN_TYPES[resource_type] != kind:
        raise CatalogueError(
            f"resource_type {resource_type!r} describes a "
            f"{BUILTIN_TYPES[resource_type]}, not a {kind}"
        )
    return resource_type


def validate_relationship_type(relationship_type: str) -> str:
    if relationship_type not in RELATIONSHIP_TYPES:
        raise CatalogueError(
            f"unknown relationship_type {relationship_type!r}; expected one of "
            f"{sorted(RELATIONSHIP_TYPES)}"
        )
    return relationship_type


def validate_primary_containment(parent_kind: str, child_kind: str) -> None:
    """Primary containment is physical containment (A31.2)."""
    if parent_kind not in PRIMARY_PARENT_KINDS:
        raise CatalogueError(
            f"a {parent_kind} cannot be a primary containment parent: primary "
            "containment is physical, and only a physical container or device "
            "contains anything"
        )
    if child_kind not in PRIMARY_CHILD_KINDS:
        raise CatalogueError(
            f"a {child_kind} cannot be physically contained: it relates through "
            "typed relationships, never as a physical child"
        )
