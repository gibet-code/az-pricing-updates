"""Cache policy state and dispatcher used by every source module."""
from __future__ import annotations

import dataclasses
import logging
from typing import Callable, TypeVar

from ._base import CachePolicy, SnapshotNotFoundError

logger = logging.getLogger(__name__)

# Module-global default. Overridable via set_active_cache_policy(...).
_active_policy: CachePolicy = CachePolicy.LIVE_WITH_FALLBACK


def get_active_cache_policy() -> CachePolicy:
    """Return the currently active default cache policy."""
    return _active_policy


def set_active_cache_policy(policy: CachePolicy | str) -> None:
    """Set the global default cache policy used by every source."""
    global _active_policy
    if isinstance(policy, str):
        try:
            policy = CachePolicy(policy)
        except ValueError as exc:
            raise ValueError(
                f"Invalid cache policy {policy!r}; expected one of "
                f"{[p.value for p in CachePolicy]}."
            ) from exc
    _active_policy = policy


def resolve_policy(policy: CachePolicy | str | None) -> CachePolicy:
    """Resolve an effective policy: explicit > module global > default."""
    if policy is None:
        return _active_policy
    if isinstance(policy, str):
        try:
            return CachePolicy(policy)
        except ValueError as exc:
            raise ValueError(
                f"Invalid cache policy {policy!r}; expected one of "
                f"{[p.value for p in CachePolicy]}."
            ) from exc
    return policy


T = TypeVar("T")


def run_with_policy(
    policy: CachePolicy | str | None,
    *,
    source: str,
    fetch_live: Callable[[], T],
    load_cache: Callable[[], T],
) -> T:
    """Run ``fetch_live`` / ``load_cache`` per the resolved cache policy.

    ``T`` is expected to be a frozen dataclass with a ``metadata`` field
    typed as :class:`~core._sources_common.SourceMetadata`. For
    ``LIVE_WITH_FALLBACK`` failures, the cached result's metadata is
    annotated with ``fallback_used=True`` / ``fallback_reason=<repr(exc)>``
    via :func:`dataclasses.replace` and a WARNING is logged.
    """
    effective = resolve_policy(policy)

    if effective is CachePolicy.CACHE:
        return load_cache()

    if effective is CachePolicy.LIVE:
        return fetch_live()

    # LIVE_WITH_FALLBACK
    try:
        return fetch_live()
    except Exception as exc:  # noqa: BLE001 — intentional broad catch for fallback
        logger.warning(
            "Live fetch for source %r failed (%r); falling back to cached snapshot.",
            source, exc,
        )
        try:
            cached = load_cache()
        except SnapshotNotFoundError:
            logger.error(
                "Cached snapshot for source %r is missing; cannot fall back.",
                source,
            )
            raise exc from None
        annotated_meta = dataclasses.replace(
            cached.metadata,
            is_cached=True,
            fallback_used=True,
            fallback_reason=repr(exc),
        )
        return dataclasses.replace(cached, metadata=annotated_meta)
