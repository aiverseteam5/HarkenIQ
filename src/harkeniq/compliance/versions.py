"""Cross-vendor firmware version comparison (R4-2 P14, R-AGENT-18).

Vendors format firmware versions inconsistently (Dell "2.83.83.83",
HPE "2.78", "U32 v2.68 (04/22/2026)", drives "HPD0"). This module
normalizes them into comparable keys and evaluates version-range
expressions for CVE matching.

Rules:
  - A version splits on "." and "-" into segments.
  - Each segment splits into leading integer + suffix ("25b" -> (25, "b")).
  - Non-numeric segments compare as (-1, text) -- ordered before any
    numeric segment, compared lexically among themselves.
  - Shorter versions compare as if padded with zeros ("2.5" == "2.5.0").
  - A leading vendor prefix like "U32 v" is stripped when followed by a
    digit ("U32 v2.68 (04/22/2026)" -> "2.68").

Range expressions (for CVE affected-version matching):
  "*"                      -- matches everything
  "< 7.10.30.00"           -- comparison operators: < <= > >= == !=
  ">= 1.0, < 2.0"          -- comma = AND
"""

from __future__ import annotations

import re
from typing import Union

_SEGMENT_RE = re.compile(r"^(\d+)(.*)$")
# "U32 v2.68 (04/22/2026)" -> capture "2.68": explicit v-prefixed version
_V_PREFIXED_RE = re.compile(r"\bv(\d+(?:[.\-]\w+)*)")
# fallback: any dotted numeric token ("BIOS 2.68 build 4" -> "2.68")
_DOTTED_RE = re.compile(r"\d+(?:\.\w+)+")
_OPS = ("<=", ">=", "==", "!=", "<", ">")


def normalize_version(raw: str) -> str:
    """Extract the comparable core of a vendor version string.

    Strings with no recognizable version token (drive firmware like
    "HPD0") pass through unchanged and compare lexically.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if _SEGMENT_RE.match(s):
        return s.split(" ", 1)[0].split("(", 1)[0].strip()
    v_prefixed = _V_PREFIXED_RE.search(s)
    if v_prefixed:
        return v_prefixed.group(1)
    dotted = _DOTTED_RE.search(s)
    if dotted:
        return dotted.group(0)
    return s


def parse_version(raw: str) -> tuple:
    """Parse a version string into a comparable tuple."""
    normalized = normalize_version(raw)
    if not normalized:
        return ()
    segments = []
    for part in re.split(r"[.\-]", normalized):
        match = _SEGMENT_RE.match(part)
        if match:
            segments.append((int(match.group(1)), match.group(2)))
        else:
            segments.append((-1, part))
    return tuple(segments)


def compare_versions(a: str, b: str) -> int:
    """Return -1 / 0 / 1 for a < b / a == b / a > b."""
    va, vb = parse_version(a), parse_version(b)
    length = max(len(va), len(vb))
    pad = (0, "")
    va = va + (pad,) * (length - len(va))
    vb = vb + (pad,) * (length - len(vb))
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def version_in_range(version: str, expr: str) -> bool:
    """Evaluate a range expression against a version.

    Unparseable expressions return False (a CVE entry with a broken
    range must never silently match the whole fleet).
    """
    expr = (expr or "").strip()
    if expr == "*":
        return True
    if not expr or not version:
        return False
    for clause in expr.split(","):
        clause = clause.strip()
        op = next((o for o in _OPS if clause.startswith(o)), None)
        if op is None:
            return False
        target = clause[len(op):].strip()
        if not target:
            return False
        cmp = compare_versions(version, target)
        ok = {
            "<": cmp < 0, "<=": cmp <= 0, ">": cmp > 0,
            ">=": cmp >= 0, "==": cmp == 0, "!=": cmp != 0,
        }[op]
        if not ok:
            return False
    return True


VersionLike = Union[str, tuple]
