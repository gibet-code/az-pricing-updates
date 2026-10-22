"""Common base types shared by every upstream-data source in the toolbox.

These are intentionally domain-agnostic. Each consumer (e.g.
``core.locations.regions``, ``core.latency.pairs``) keeps its own typed
result dataclass that carries a ``metadata: SourceMetadata`` field — the
cache-policy dispatcher in :mod:`._policy` operates on any such dataclass
generically via :func:`dataclasses.replace`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class CachePolicy(str, Enum):
    """How a source resolves its data."""

    LIVE = "live"
    """Always fetch from upstream; raise on failure."""

    CACHE = "cache"
    """Always read the on-disk snapshot; raise if missing."""

    LIVE_WITH_FALLBACK = "live_with_fallback"
    """Try upstream first; on any error log a warning and fall back to cache."""


class SnapshotNotFoundError(FileNotFoundError):
    """Raised when policy=CACHE but the source has no on-disk snapshot."""


@dataclass(frozen=True)
class SourceMetadata:
    """Provenance for a batch of source data."""

    source: str
    """Short module-level identifier, e.g. ``"list_locations_api"``."""

    url: str | None
    """Canonical upstream URL (None for ``merged`` and pure-snapshot sources)."""

    fetched_at: datetime | None
    """When this data was originally retrieved from upstream (UTC)."""

    is_cached: bool
    """True when the data was loaded from the on-disk snapshot."""

    snapshot_path: Path | None
    """Path to the snapshot file used (or written), if any."""

    fallback_used: bool = False
    """True when ``LIVE_WITH_FALLBACK`` degraded to the cached snapshot."""

    fallback_reason: str | None = None
    """Repr of the exception that triggered the fallback, if any."""

    contributing_sources: tuple["SourceMetadata", ...] = field(default_factory=tuple)
    """For ``merged`` sources: the per-source metadata of each contributor."""


def meta_to_provenance(meta: "SourceMetadata") -> dict[str, Any]:
    """Compact dict representation of :class:`SourceMetadata` for embedding.

    Used by ``merged`` sources to attach per-cell provenance to consolidated
    entries. The returned dict is JSON-friendly (no datetime objects) and
    omits the recursive ``contributing_sources`` field.
    """
    return {
        "source": meta.source,
        "url": meta.url,
        "fetched_at": meta.fetched_at.isoformat() if meta.fetched_at else None,
        "is_cached": meta.is_cached,
        "fallback_used": meta.fallback_used,
    }
