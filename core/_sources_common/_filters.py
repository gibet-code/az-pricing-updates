"""Uniform column-filter spec shared by usage and pricesheet loaders.

A :class:`ColumnFilter` carries both the filter values and the operator
to apply (``in``, ``not_in``, ``iin``, ``not_iin``). The ``i*`` variants
are case-insensitive string matches; they cannot be pushed down to the
parquet reader and run as a post-load pandas mask instead.

Loaders accept either the modern ``{col: ColumnFilter(...)}`` form or the
legacy ``{col: [values]}`` shape (treated as case-sensitive ``in``).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import pandas as pd


FilterOp = Literal['in', 'not_in', 'iin', 'not_iin']


@dataclass(frozen=True)
class ColumnFilter:
    """Filter spec for one column.

    ``op='in'`` / ``'not_in'`` are pushed down to the parquet reader when
    the source is parquet. ``op='iin'`` / ``'not_iin'`` are case-insensitive
    on string columns and always run in memory after load (no parquet
    pushdown equivalent).
    """
    values: Sequence
    op: FilterOp = 'in'


# Map our op names to the pyarrow dataset filter operator strings.
_PUSHDOWN_OP_NAMES: dict[FilterOp, str] = {
    'in':     'in',
    'not_in': 'not in',
}


def normalize_filters(
    filters: dict[str, ColumnFilter | Sequence] | None,
) -> dict[str, ColumnFilter] | None:
    """Coerce a mixed-shape filters dict to all-:class:`ColumnFilter` values.

    Bare sequences (legacy shape) become ``ColumnFilter(values, op='in')``.
    Returns ``None`` if ``filters`` is ``None`` or empty.
    """
    if not filters:
        return None
    normalized: dict[str, ColumnFilter] = {}
    for col, spec in filters.items():
        if isinstance(spec, ColumnFilter):
            normalized[col] = spec
        elif isinstance(spec, (list, tuple, set, frozenset)):
            normalized[col] = ColumnFilter(values=list(spec), op='in')
        else:
            raise TypeError(
                f"filters[{col!r}] must be a ColumnFilter or a sequence of values, "
                f"got {type(spec).__name__}."
            )
    return normalized


def to_parquet_pushdown(
    filters: dict[str, ColumnFilter] | None,
) -> tuple[list[tuple] | None, dict[str, ColumnFilter]]:
    """Split filters into a parquet-pushdown list and an in-memory residual.

    Returns ``(pq_filters, residual)`` where ``pq_filters`` is a list of
    ``(col, op, values)`` tuples suitable for ``pyarrow.dataset`` (or
    ``None`` when nothing is pushable), and ``residual`` is the subset of
    filters that must be applied in memory after read.
    """
    if not filters:
        return None, {}
    pq: list[tuple] = []
    residual: dict[str, ColumnFilter] = {}
    for col, spec in filters.items():
        pushdown_op = _PUSHDOWN_OP_NAMES.get(spec.op)
        if pushdown_op is not None:
            pq.append((col, pushdown_op, list(spec.values)))
        else:
            residual[col] = spec
    return (pq or None), residual


def apply_in_memory(
    df: pd.DataFrame,
    filters: dict[str, ColumnFilter] | None,
) -> pd.DataFrame:
    """Apply ``filters`` against ``df`` using pandas boolean indexing.

    Silently ignores filter columns that are absent from ``df`` (mirrors
    the prior behavior for the CSV branch when callers selected fewer
    columns than they filtered on).
    """
    if not filters:
        return df
    for col, spec in filters.items():
        if col not in df.columns:
            continue
        if spec.op in ('iin', 'not_iin'):
            lowered_vals = [v.lower() if isinstance(v, str) else v for v in spec.values]
            col_lower    = df[col].astype(str).str.lower()
            mask = col_lower.isin(lowered_vals)
            if spec.op == 'not_iin':
                mask = ~mask
        else:
            mask = df[col].isin(list(spec.values))
            if spec.op == 'not_in':
                mask = ~mask
        df = df[mask]
    return df


def serialize_for_cache_key(
    filters: dict[str, ColumnFilter] | None,
) -> dict | None:
    """Stable JSON-serializable representation for cache-key hashing.

    Sorts both keys and values, and includes the op so different ops produce
    different keys even when values are identical.
    """
    if not filters:
        return None
    return {
        col: {
            'op':     spec.op,
            'values': sorted(spec.values),
        }
        for col, spec in sorted(filters.items())
    }
