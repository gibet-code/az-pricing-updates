"""Shared infrastructure used by upstream-data sources across the toolbox.

Centralizes the bits that were originally implemented for
``core.locations.regions`` so other domains (e.g. ``core.latency``) can
reuse them without copy-paste:

- :class:`CachePolicy` / :func:`get_active_cache_policy` /
  :func:`set_active_cache_policy` / :func:`run_with_policy`
- :class:`SourceMetadata` / :class:`SnapshotNotFoundError`
- JSON-envelope and raw + sidecar snapshot I/O helpers.

Each consumer keeps its own typed result dataclass (e.g.
:class:`core.locations.regions.RegionsCatalog`,
:class:`core.latency.pairs.LatencyResult`); :func:`run_with_policy`
operates on any frozen dataclass with a ``metadata: SourceMetadata`` field
via :func:`dataclasses.replace`.
"""
from __future__ import annotations

from . import _snapshot_io as snapshot_io
from ._base import CachePolicy, SnapshotNotFoundError, SourceMetadata, meta_to_provenance
from ._filters import (
    ColumnFilter,
    apply_in_memory,
    normalize_filters,
    serialize_for_cache_key,
    to_parquet_pushdown,
)
from ._policy import (
    get_active_cache_policy,
    resolve_policy,
    run_with_policy,
    set_active_cache_policy,
)

__all__ = [
    "CachePolicy",
    "ColumnFilter",
    "SnapshotNotFoundError",
    "SourceMetadata",
    "apply_in_memory",
    "meta_to_provenance",
    "normalize_filters",
    "serialize_for_cache_key",
    "to_parquet_pushdown",
    "get_active_cache_policy",
    "set_active_cache_policy",
    "resolve_policy",
    "run_with_policy",
    "snapshot_io",
]
