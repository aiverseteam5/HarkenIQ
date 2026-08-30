"""The generic organizational tree: containment, and nothing else.

E1.1 (2026-08-30). A tenant describes its own organization here --
regions, clusters, circles, trusts, territories, availability zones --
and attaches each site to exactly one node of it. That is the whole
job of this module.

What this is NOT
----------------
**Containment is not authorization.** Ratified decision B: the tree
says where a site sits, a scope grant says who may reach it. E1.2
introduces `cc_scope_grants`, and a grant happens to reference an org
unit the way it could reference a site. Nothing in this file resolves,
widens or implies permission, and nothing later may make it do so.

**Containment is not blast radius.** The Site Manager's rack and
fault-domain model stays exactly where it is. A power domain is a
physical fact about which devices die together; an org unit is an
administrative fact about who owns them. Conflating them would let an
org chart edit change what an action is allowed to touch.

Design notes
------------
Paths are materialized: a unit's `path` is the ids of its ancestors and
itself, each wrapped in delimiters -- ``/root/region/cluster/``. Three
properties follow, and all three are load-bearing:

* **A subtree is one prefix match.** No recursive CTE, so PostgreSQL
  and the sqlite the unit tests run on cannot diverge.
* **The trailing delimiter is what makes siblings safe.** ``/u1/u7/``
  does not prefix-match ``/u1/u70/``; without the trailing slash it
  would, and a scope over Cluster 7 would silently cover Cluster 70.
* **Ids, never names.** `new_id()` is `uuid4().hex` -- 32 lowercase hex
  characters -- so `/`, `%` and `_` cannot occur inside a segment. The
  delimiter cannot be forged and the LIKE wildcards cannot be smuggled
  in through a unit name. That is structural, not a convention someone
  can break later. It also means a rename never rewrites a path.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

# The path delimiter. Safe because ids are hex.
DELIM = "/"

#: A tenant may nest eight levels. Deep enough for
#: organization > country > region > metro > campus > building > hall >
#: cluster, and shallow enough that a path stays a short string and a
#: recursive display never becomes a denial of service.
MAX_DEPTH = 8

#: `unit_type` is the customer's own word for this level, not ours.
#: Validated as a short slug and never as an enum -- an enum here would
#: be exactly the hard-coded Region/Cluster model decision A rejected.
UNIT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: The root unit every tenant gets at migration time.
ROOT_UNIT_TYPE = "organization"

MAX_NAME = 255


class OrgTreeError(ValueError):
    """A tree rule was violated. Routers map this to 4xx."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_unit_type(raw: str) -> str:
    """Lowercase and validate the customer's level word.

    The word itself is theirs; only its shape is ours.
    """
    value = (raw or "").strip().lower()
    if not UNIT_TYPE_RE.match(value):
        raise OrgTreeError(
            "unit_type must be a short slug: a lowercase letter followed by "
            "up to 31 letters, digits, hyphens or underscores "
            f"(got {raw!r})"
        )
    return value


def normalize_name(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise OrgTreeError("name is required")
    if len(value) > MAX_NAME:
        raise OrgTreeError(f"name must be at most {MAX_NAME} characters")
    return value


# ---------------------------------------------------------------------------
# Path arithmetic
# ---------------------------------------------------------------------------


def compose_path(parent_path: Optional[str], unit_id: str) -> str:
    """The canonical path of `unit_id` under `parent_path`.

    A root unit is ``/<id>/``; a child appends its own id and delimiter.
    """
    if not unit_id:
        raise OrgTreeError("unit_id is required to compose a path")
    base = parent_path or DELIM
    if not base.startswith(DELIM) or not base.endswith(DELIM):
        raise OrgTreeError(f"malformed parent path: {parent_path!r}")
    return f"{base}{unit_id}{DELIM}"


def segments(path: str) -> list[str]:
    """The unit ids along `path`, root first, self last."""
    return [seg for seg in (path or "").split(DELIM) if seg]


def depth_of(path: str) -> int:
    """1 for a root unit, 2 for its children, and so on."""
    return len(segments(path))


def ancestor_ids(path: str) -> list[str]:
    """Ancestors of the unit at `path`, root first, EXCLUDING itself."""
    return segments(path)[:-1]


def self_id(path: str) -> str:
    parts = segments(path)
    return parts[-1] if parts else ""


def is_descendant(candidate_path: str, ancestor_path: str) -> bool:
    """True when `candidate_path` sits at or below `ancestor_path`.

    A unit is its own descendant here, which is what the cycle check
    wants: re-parenting a unit under itself must be refused.
    """
    if not candidate_path or not ancestor_path:
        return False
    return candidate_path.startswith(ancestor_path)


def check_depth(parent_depth: int) -> None:
    """Refuse a child that would exceed `MAX_DEPTH`."""
    if parent_depth + 1 > MAX_DEPTH:
        raise OrgTreeError(
            f"organizational depth is bounded at {MAX_DEPTH}; "
            f"this unit would sit at level {parent_depth + 1}"
        )


def check_move(
    moving_path: str,
    moving_id: str,
    destination_path: Optional[str],
    destination_depth: int,
    subtree_height: int,
) -> None:
    """Refuse a move that would cycle or that would bust the depth bound.

    `subtree_height` is how many levels the moving unit carries with it
    (1 when it is a leaf). Moving a three-level subtree under a level-7
    parent would land its leaves at level 10, so the check has to look
    at the whole subtree and not just the node being dragged.
    """
    if destination_path is not None and is_descendant(destination_path, moving_path):
        raise OrgTreeError(
            "a unit cannot be moved beneath itself or one of its own "
            "descendants -- that would make the tree a cycle"
        )
    if destination_path is not None and self_id(destination_path) == moving_id:
        raise OrgTreeError("a unit cannot be its own parent")
    landing = destination_depth + subtree_height
    if landing > MAX_DEPTH:
        raise OrgTreeError(
            f"the move would place descendants at level {landing}, "
            f"past the bound of {MAX_DEPTH}"
        )


def rewrite_descendant_path(
    descendant_path: str, old_prefix: str, new_prefix: str
) -> str:
    """Re-root `descendant_path` from `old_prefix` onto `new_prefix`."""
    if not is_descendant(descendant_path, old_prefix):
        raise OrgTreeError(
            f"{descendant_path!r} is not under {old_prefix!r}; refusing to rewrite"
        )
    return new_prefix + descendant_path[len(old_prefix):]


def subtree_prefix(path: str) -> str:
    """The LIKE prefix that selects a unit and everything beneath it.

    Identical to the path -- the trailing delimiter is already there,
    and that is precisely what keeps a sibling out of the result.
    """
    return path


# ---------------------------------------------------------------------------
# Display assembly
# ---------------------------------------------------------------------------


def _sort_key(unit) -> tuple:
    return (getattr(unit, "sort_order", 0) or 0, (getattr(unit, "name", "") or "").lower())


def assemble_tree(units: Sequence, *, site_counts: Optional[dict] = None) -> list[dict]:
    """Nest a flat unit list into roots-with-children, for display.

    Pure: it takes rows and returns dicts. Any unit whose parent is
    absent from `units` is surfaced as a root rather than dropped -- a
    scoped read in E1.2 hands us a subtree, and silently swallowing its
    top node would make the tree look empty.
    """
    counts = site_counts or {}
    nodes: dict[str, dict] = {}
    for unit in units:
        nodes[unit.id] = {
            "id": unit.id,
            "parent_id": unit.parent_id,
            "unit_type": unit.unit_type,
            "name": unit.name,
            "path": unit.path,
            "depth": unit.depth,
            "sort_order": unit.sort_order,
            "site_count": counts.get(unit.id, 0),
            "children": [],
        }

    roots: list[dict] = []
    for unit in sorted(units, key=_sort_key):
        node = nodes[unit.id]
        parent = nodes.get(unit.parent_id) if unit.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


def total_site_count(node: dict) -> int:
    """Sites at this node plus everything beneath it."""
    return node.get("site_count", 0) + sum(
        total_site_count(child) for child in node.get("children", ())
    )


def flatten(nodes: Iterable[dict]) -> list[dict]:
    """Depth-first walk of an assembled tree, for tests and exports."""
    out: list[dict] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.get("children", ())))
    return out
