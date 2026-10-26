"""The Resource Catalogue vocabulary (TX-1 spec A31, TX-2 spec A32, TX-3
spec A33).

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

Identity, names and evidence, apart (TX-3, A33)
-----------------------------------------------
What a resource IS -- its opaque id and its one identifier -- is kept apart
from what it is CALLED (aliases: any number, never unique) and from where
each fact CAME FROM (provenance). An alias can surface a reconciliation
candidate; nothing here can merge two resources.
"""

from __future__ import annotations

import re
from datetime import timedelta

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
# Provenance, bounded (TX-3, A33.4). Every fact records its writer
# (`source`, above), where it came from (`source_system` and
# `source_namespace`) and when its source observed it. The mirror's facts
# come from HarkenIQ itself, through one of three feeds.
# ---------------------------------------------------------------------------

#: Central Command's own site registry (`cc_sites`).
FEED_SITE_REGISTRY = "site_registry"
#: Each site's devices, as its Site Manager reports them (`cc_fleet_cache`).
FEED_FLEET_SNAPSHOT = "fleet_snapshot"
#: Each site's racks, as its Site Manager reports them (the snapshot's
#: topology block, A32.4).
FEED_SITE_TOPOLOGY = "site_topology"
MIRROR_FEEDS = frozenset({FEED_SITE_REGISTRY, FEED_FLEET_SNAPSHOT, FEED_SITE_TOPOLOGY})

#: The feed a mirrored resource of each legacy kind comes from.
LEGACY_FEEDS: dict[str, str] = {
    LEGACY_SITE: FEED_SITE_REGISTRY,
    LEGACY_DEVICE: FEED_FLEET_SNAPSHOT,
    LEGACY_RACK: FEED_SITE_TOPOLOGY,
}

#: How stale a stored `last_observed_at` may grow before the writer advances
#: it. The mirror re-reads every fact each pass; stamping each one each time
#: would make it rewrite its whole estate every poll interval, so the stamp
#: is accurate to within this bound and an unchanged estate still writes
#: nothing in between.
OBSERVATION_REFRESH = timedelta(hours=1)

# ---------------------------------------------------------------------------
# Types. `namespace:domain.type`, in three statuses (TX-3, A33.5):
#
# * REGISTERED -- `BUILTIN_TYPES`: EXACTLY the types the mirror writes.
# * RESERVED   -- `RESERVED_TYPES`: named in the `harkeniq` namespace, each
#   with its fixed kind, and refused on write. The contextual containers
#   above and around a Site (A32.6) and the finer device types: nothing in
#   the platform is an authoritative source for any of them, and a registry
#   entry with no writer is a declaration the platform cannot keep. The
#   slice that adds a writer registers its types, with that writer's trust
#   rules.
# * UNKNOWN    -- everything else, refused. Tenant and vendor namespaces
#   (open decision 4) open only with the integration slice that writes
#   them; none is open.
#
# No type is ever inferred -- not from a vendor, a model, an inventory or a
# name. A mirrored device's type is its class default, below.
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

#: REGISTERED: type -> the one kind it may describe.
BUILTIN_TYPES: dict[str, str] = {
    TYPE_SITE: KIND_PHYSICAL_CONTAINER,
    TYPE_RACK: KIND_PHYSICAL_CONTAINER,
    TYPE_SERVER: KIND_PHYSICAL_DEVICE,
    TYPE_SWITCH: KIND_PHYSICAL_DEVICE,
    TYPE_UNCLASSIFIED_DEVICE: KIND_PHYSICAL_DEVICE,
}

#: RESERVED (A33.5): type -> its fixed kind. Never written until registered.
RESERVED_TYPES: dict[str, str] = {
    # A32.6's contextual containers. Reserving a chassis container type
    # leaves open decision 7 open: a chassis may also be a device.
    "harkeniq:geography.region": KIND_PHYSICAL_CONTAINER,
    "harkeniq:facility.campus": KIND_PHYSICAL_CONTAINER,
    "harkeniq:facility.datacenter": KIND_PHYSICAL_CONTAINER,
    "harkeniq:facility.building": KIND_PHYSICAL_CONTAINER,
    "harkeniq:facility.room": KIND_PHYSICAL_CONTAINER,
    "harkeniq:facility.hall": KIND_PHYSICAL_CONTAINER,
    "harkeniq:placement.zone": KIND_PHYSICAL_CONTAINER,
    "harkeniq:placement.row": KIND_PHYSICAL_CONTAINER,
    "harkeniq:enclosure.chassis": KIND_PHYSICAL_CONTAINER,
    # Finer device types. A device's class says `server` or `switch` and
    # nothing about form factor, accelerators or link layer, so no writer
    # can assign one of these honestly yet.
    "harkeniq:compute.rack_server": KIND_PHYSICAL_DEVICE,
    "harkeniq:compute.gpu_server": KIND_PHYSICAL_DEVICE,
    "harkeniq:network.ethernet_switch": KIND_PHYSICAL_DEVICE,
    "harkeniq:storage.block_array": KIND_PHYSICAL_DEVICE,
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

# ---------------------------------------------------------------------------
# Aliases (TX-3, A33.3): what a resource is CALLED, kept apart from what it
# IS. Never unique across resources, never an identity, never a visibility
# or authorization input, never a merge key.
#
# The mirror writes two types. `service_tag` is the Harken Node's reported
# field verbatim -- a Dell service tag, an HPE or IPMI serial number, or a
# SONiC switch's base MAC, depending on the protocol that read it -- which
# is one more reason it is an alias. `historical_name` is a former display
# name. The other four are vocabulary only: no source of them reaches
# Central Command.
# ---------------------------------------------------------------------------

ALIAS_HOSTNAME = "hostname"
ALIAS_FQDN = "fqdn"
ALIAS_SERVICE_TAG = "service_tag"
ALIAS_ASSET_TAG = "asset_tag"
ALIAS_OPERATOR_LABEL = "operator_label"
ALIAS_HISTORICAL_NAME = "historical_name"
ALIAS_TYPES = frozenset({
    ALIAS_HOSTNAME, ALIAS_FQDN, ALIAS_SERVICE_TAG,
    ALIAS_ASSET_TAG, ALIAS_OPERATOR_LABEL, ALIAS_HISTORICAL_NAME,
})

#: Serial-like aliases (A33.7): two resources sharing an ACTIVE one "may be
#: the same physical resource" -- a reconciliation CANDIDATE, never a match.
#: A name, of any kind, never makes a candidate.
IDENTITY_EVIDENCE_ALIAS_TYPES = frozenset({ALIAS_SERVICE_TAG, ALIAS_ASSET_TAG})

MAX_ALIAS_LENGTH = 255
#: How many candidates a resource detail lists; `truncated` reports more.
MAX_IDENTITY_CANDIDATES = 20


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
    """A type a writer may write: well-formed, REGISTERED, and of `kind`."""
    validate_kind(kind)
    if not is_well_formed_type(resource_type):
        raise CatalogueError(
            f"resource_type {resource_type!r} is not 'namespace:domain.type'"
        )
    if resource_type in RESERVED_TYPES:
        raise CatalogueError(
            f"resource_type {resource_type!r} is reserved and not in the "
            "built-in registry: nothing writes it yet, and the slice that adds "
            "its writer registers it (A33.5)"
        )
    if resource_type not in BUILTIN_TYPES:
        raise CatalogueError(
            f"resource_type {resource_type!r} is not in the built-in registry; "
            "tenant and vendor type namespaces are not open (A31.2, A33.1)"
        )
    if BUILTIN_TYPES[resource_type] != kind:
        raise CatalogueError(
            f"resource_type {resource_type!r} describes a "
            f"{BUILTIN_TYPES[resource_type]}, not a {kind}"
        )
    return resource_type


def normalize_alias(value: str) -> str:
    """What two aliases are compared by: whitespace collapsed, case folded.

    Matching only -- the value as reported is what is shown."""
    return " ".join((value or "").split()).casefold()


def validate_alias(alias_type: str, value: str) -> tuple[str, str]:
    """`(value, normalized_value)` for an alias a writer may record."""
    if alias_type not in ALIAS_TYPES:
        raise CatalogueError(
            f"unknown alias_type {alias_type!r}; the vocabulary is closed: "
            f"{sorted(ALIAS_TYPES)}"
        )
    shown = (value or "").strip()
    if not shown:
        raise CatalogueError("an alias has a value")
    if len(shown) > MAX_ALIAS_LENGTH:
        raise CatalogueError(f"an alias is at most {MAX_ALIAS_LENGTH} characters")
    return shown, normalize_alias(shown)


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
