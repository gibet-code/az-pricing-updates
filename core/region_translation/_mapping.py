"""Repo-owned Azure region mapping.

Backs :mod:`core.region_translation.uniformize`. Loads ``data/region_mapping.json`` (a
hand-curatable table keyed by canonical ARM location id) and exposes lookup
indexes that resolve any known label — ARM location id, billing meter region,
display name, or alias — to a canonical ARM location, plus a forward map from
that ARM location to each vocabulary's value.

The JSON entry shape, keyed by ``armLocation``::

    "northeurope": {
        "meterRegion": "EU North",
        "displayName": "North Europe",
        "armLocationAliases": ["eunorth", "ne", ...],
        "meterRegionAliases": [],
        "otherAliases": ["dublin", "db"]
    }

Lookups come in two strictnesses:

- **strict** (default): the three canonical values (``armLocation`` key,
  ``meterRegion``, ``displayName``) plus ``armLocationAliases`` and
  ``meterRegionAliases``.
- **loose**: strict plus ``otherAliases`` (city names, airport codes, and
  other approximate spellings).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

MAPPING_PATH: Path = Path(__file__).parent / "data" / "region_mapping.json"

# Canonical vocabulary field names (must match RegionVocabulary values in
# uniformize.py and the JSON entry keys, except ARM_LOCATION which is the key).
_ARM_LOCATION = "armLocation"
_METER_REGION = "meterRegion"
_DISPLAY_NAME = "displayName"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load and cache the raw region mapping JSON (keyed by ARM location)."""
    with MAPPING_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"region_mapping.json is empty or malformed: {MAPPING_PATH}")
    return data


@lru_cache(maxsize=1)
def get_forward() -> dict[str, dict[str, str | None]]:
    """Return ``armLocation -> {armLocation, meterRegion, displayName}``."""
    data = _load()
    return {
        arm: {
            _ARM_LOCATION: arm,
            _METER_REGION: entry.get("meterRegion"),
            _DISPLAY_NAME: entry.get("displayName"),
        }
        for arm, entry in data.items()
    }


def _build_index(*, loose: bool) -> dict[str, str]:
    """Build a ``label.lower() -> armLocation`` index.

    Includes the canonical primaries and the strict alias buckets; ``loose``
    additionally folds in ``otherAliases``.
    """
    data = _load()
    index: dict[str, str] = {}

    def _add(label: str | None, arm: str) -> None:
        if not label:
            return
        key = label.lower()
        # First writer wins: primaries are added before aliases, so a value
        # that is a primary of one region won't be hijacked by an alias of
        # another.
        index.setdefault(key, arm)

    # Pass 1: canonical primaries (highest priority).
    for arm, entry in data.items():
        _add(arm, arm)
        _add(entry.get("meterRegion"), arm)
        _add(entry.get("displayName"), arm)
    # Pass 2: strict alias buckets.
    for arm, entry in data.items():
        for alias in entry.get("armLocationAliases", []):
            _add(alias, arm)
        for alias in entry.get("meterRegionAliases", []):
            _add(alias, arm)
    # Pass 3: loose-only aliases.
    if loose:
        for arm, entry in data.items():
            for alias in entry.get("otherAliases", []):
                _add(alias, arm)
    return index


@lru_cache(maxsize=2)
def _get_index(loose: bool) -> dict[str, str]:
    return _build_index(loose=loose)


def get_indexes(*, strict: bool) -> tuple[dict[str, str], dict[str, dict[str, str | None]]]:
    """Return ``(lookup, forward)`` for the requested strictness.

    ``lookup`` maps a lowercased input label to a canonical ARM location;
    ``forward`` maps that ARM location to each vocabulary's value.
    """
    return _get_index(loose=not strict), get_forward()


def clear_cache() -> None:
    """Drop cached mapping + indexes (e.g. after editing the JSON in tests)."""
    _load.cache_clear()
    get_forward.cache_clear()
    _get_index.cache_clear()
