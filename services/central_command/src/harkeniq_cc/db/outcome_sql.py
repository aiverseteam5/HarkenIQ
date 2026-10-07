"""A30.41: the two pieces of outcome-aggregate SQL that differ by engine.

`decay_weight(now, recorded_at)` is one outcome row's recency weight,
`0.5 ** (max(0, age_days) / half_life)` -- `predictive.decay_weight`, the one
definition, computed where the rows are instead of after reading them:

* PostgreSQL mirrors the Python float pipeline step for step: the exact
  interval in seconds (`extract(epoch ...)` is numeric, cast once to
  float8, which is how `timedelta.total_seconds()` rounds), `/ 86400.0`,
  clamped at zero, `/ 30.0`, then `power(0.5, x)`. Measured bit-identical
  to Python's per-row rate for 207 of 207 devices at 1.1M rows on the
  production image.
* SQLite (tests and development only) calls a function `make_engine`
  registers on every connection, implemented BY `predictive.decay_weight`.

`ordered_sum(expr, *order_by)` is `sum(expr ORDER BY ...)`: a float sum in a
fixed order, so a rerun -- or a parallel plan -- gives the same bits, and the
order is the one Python sums a device's rows in (`recorded_at`, then the
immutable `id` for ties). SQLite before 3.44 has no ordered aggregates and
sums in scan order instead.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import Float

#: The SQLite function `make_engine` registers.
SQLITE_DECAY_FUNCTION = "harkeniq_decay_weight"

#: SQLite gained `aggregate(expr ORDER BY ...)` in 3.44.0.
SQLITE_ORDERED_AGGREGATES = sqlite3.sqlite_version_info >= (3, 44, 0)


class decay_weight(FunctionElement):
    """`decay_weight(now, recorded_at)`: one row's recency weight, in SQL."""

    name = "decay_weight"
    type = Float()
    inherit_cache = True


class ordered_sum(FunctionElement):
    """`ordered_sum(expr, *order_by)`: `sum(expr ORDER BY order_by...)`."""

    name = "ordered_sum"
    type = Float()
    inherit_cache = True


def _half_life() -> str:
    from harkeniq_cc.predictive import DECAY_HALF_LIFE_DAYS

    return repr(float(DECAY_HALF_LIFE_DAYS))


@compiles(decay_weight)
def _decay_unsupported(element, compiler, **kw):  # pragma: no cover - guard
    raise NotImplementedError(
        f"decay_weight has no rendering for {compiler.dialect.name}"
    )


@compiles(decay_weight, "postgresql")
def _decay_postgresql(element, compiler, **kw):
    now, recorded_at = list(element.clauses)
    return (
        "power(0.5::float8, greatest(0.0::float8, "
        f"extract(epoch from (CAST({compiler.process(now, **kw)} AS timestamptz) - "
        f"{compiler.process(recorded_at, **kw)}))::float8 / 86400.0::float8) "
        f"/ {_half_life()}::float8)"
    )


@compiles(decay_weight, "sqlite")
def _decay_sqlite(element, compiler, **kw):
    now, recorded_at = list(element.clauses)
    return (
        f"{SQLITE_DECAY_FUNCTION}({compiler.process(now, **kw)}, "
        f"{compiler.process(recorded_at, **kw)})"
    )


def _ordered(element, compiler, kw, *, with_order: bool) -> str:
    expr, *order = list(element.clauses)
    rendered = compiler.process(expr, **kw)
    if not with_order or not order:
        return f"sum({rendered})"
    keys = ", ".join(compiler.process(o, **kw) for o in order)
    return f"sum({rendered} ORDER BY {keys})"


@compiles(ordered_sum)
def _ordered_default(element, compiler, **kw):
    return _ordered(element, compiler, kw, with_order=True)


@compiles(ordered_sum, "sqlite")
def _ordered_sqlite(element, compiler, **kw):
    return _ordered(element, compiler, kw, with_order=SQLITE_ORDERED_AGGREGATES)


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _sqlite_decay_weight(now: Any, recorded_at: Any) -> float:
    """The SQLite rendering: `predictive.decay_weight` itself, on the stored
    values (SQLite keeps UTC wall time; a naive value is read as UTC, as
    `predictive._age_days` reads one)."""
    from harkeniq_cc.predictive import decay_weight as weight

    current = _as_datetime(now)
    if current is None:
        current = datetime.now(timezone.utc)
    elif current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return weight(_as_datetime(recorded_at), current)


def register_sqlite_functions(dbapi_connection, _connection_record=None) -> None:
    """Install the A30.41 functions on one SQLite connection."""
    dbapi_connection.create_function(
        SQLITE_DECAY_FUNCTION, 2, _sqlite_decay_weight, deterministic=True,
    )
