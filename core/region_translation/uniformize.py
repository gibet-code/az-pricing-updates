"""Region vocabulary translation.

Resolves any known region label — ARM location id (``northeurope``), billing
meter region (``EU North``), display name (``North Europe``), or an alias — to
a canonical Azure region, then emits the requested target vocabulary.

Backed by the repo-owned mapping in :mod:`core.region_translation._mapping`.

Public surface:

- :class:`RegionVocabulary` — the target vocabularies.
- :func:`uniformize_location_values` — core list translator.
- :func:`to_arm_location_value` / :func:`to_meter_region_value` /
  :func:`to_display_name_value` — single-value translators.
- :func:`translate_location_column` — DataFrame column translator.

Two cross-cutting options:

- ``strict`` (default ``True``): match only the canonical values and the
  ``armLocation`` / ``meterRegion`` alias buckets. ``strict=False`` also
  matches the loose ``otherAliases`` (city names, airport codes, …).
- ``passthrough_on_miss`` (default ``False``): on an unmatched value return
  ``None``. Set ``True`` to return the original value unchanged instead.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Iterable

import pandas as pd

from . import _mapping

logger = logging.getLogger(__name__)


class RegionVocabulary(str, Enum):
    """Target vocabularies a region label can be translated into."""

    ARM_LOCATION = "armLocation"
    METER_REGION = "meterRegion"
    DISPLAY_NAME = "displayName"


def uniformize_location_values(
    values: Iterable[str | None],
    *,
    target: RegionVocabulary = RegionVocabulary.ARM_LOCATION,
    strict: bool = True,
    passthrough_on_miss: bool = False,
    context: str = "uniformize_location_values",
) -> list[str | None]:
    """Translate an iterable of region labels into ``target`` vocabulary.

    Each value is resolved (case-insensitively) to a canonical ARM location,
    then mapped to the requested ``target`` vocabulary. ``None`` / empty /
    pandas-missing inputs pass through unchanged.

    Parameters
    ----------
    values : Iterable[str | None]
        Region labels to translate (ARM ids, meter regions, display names, or
        aliases).
    target : RegionVocabulary
        Output vocabulary. Defaults to :attr:`RegionVocabulary.ARM_LOCATION`.
    strict : bool
        When ``True`` (default), only canonical values and the ``armLocation``
        / ``meterRegion`` alias buckets are eligible for a match. When
        ``False``, loose ``otherAliases`` are also matched.
    passthrough_on_miss : bool
        On an unmatched value, return ``None`` when ``False`` (default) or the
        original value unchanged when ``True``.
    context : str
        Short label included in the unmatched-values WARNING (typically the
        caller's column or source name).

    Returns
    -------
    list[str | None]
        Translated values, in the same order as ``values``.
    """
    target_key = RegionVocabulary(target).value
    lookup, forward = _mapping.get_indexes(strict=strict)

    out: list[str | None] = []
    unmatched: set[str] = set()
    for v in values:
        # Treat pandas missing-values (NaN / pd.NA) the same as None/"":
        # pass them through unchanged. Categorical / object columns loaded
        # from CSV surface empty cells as float NaN, which would otherwise
        # crash on v.lower() below.
        if v is None or v is pd.NA or v == "":
            out.append(v)
            continue
        try:
            if pd.isna(v):
                out.append(None)
                continue
        except (TypeError, ValueError):
            pass

        arm = lookup.get(v.lower())
        if arm is None:
            unmatched.add(v)
            out.append(v if passthrough_on_miss else None)
            continue
        out.append(forward[arm].get(target_key))

    if unmatched:
        logger.warning(
            "%s: %d value(s) could not be matched against the region mapping "
            "(strict=%s): %s",
            context, len(unmatched), strict, sorted(unmatched),
        )
    return out


def _translate_one(
    value: str | None,
    *,
    target: RegionVocabulary,
    strict: bool,
    passthrough_on_miss: bool,
    context: str,
) -> str | None:
    return uniformize_location_values(
        [value],
        target=target,
        strict=strict,
        passthrough_on_miss=passthrough_on_miss,
        context=context,
    )[0]


def to_arm_location_value(
    value: str | None, *, strict: bool = True, passthrough_on_miss: bool = False,
) -> str | None:
    """Translate a single label to its canonical ARM location id."""
    return _translate_one(
        value,
        target=RegionVocabulary.ARM_LOCATION,
        strict=strict,
        passthrough_on_miss=passthrough_on_miss,
        context="to_arm_location_value",
    )


def to_meter_region_value(
    value: str | None, *, strict: bool = True, passthrough_on_miss: bool = False,
) -> str | None:
    """Translate a single label to its canonical billing meter region."""
    return _translate_one(
        value,
        target=RegionVocabulary.METER_REGION,
        strict=strict,
        passthrough_on_miss=passthrough_on_miss,
        context="to_meter_region_value",
    )


def to_display_name_value(
    value: str | None, *, strict: bool = True, passthrough_on_miss: bool = False,
) -> str | None:
    """Translate a single label to its canonical display name."""
    return _translate_one(
        value,
        target=RegionVocabulary.DISPLAY_NAME,
        strict=strict,
        passthrough_on_miss=passthrough_on_miss,
        context="to_display_name_value",
    )


def resolve_location_values(
    values: Iterable[str | None],
    *,
    target: RegionVocabulary = RegionVocabulary.ARM_LOCATION,
    strict: bool = True,
    context: str = "resolve_location_values",
) -> list[str]:
    """Strictly resolve region labels, raising on any that cannot be matched.

    Like :func:`uniformize_location_values`, but treats every unresolvable
    input (including ``None`` / empty / pandas-missing) as an error rather
    than passing it through. Use this when a caller needs hard validation of
    user-supplied region labels *before* handing them to a downstream
    consumer (e.g. the region cost simulation).

    Parameters
    ----------
    values : Iterable[str | None]
        Region labels to resolve (ARM ids, meter regions, display names, or
        aliases). Order and duplicates are preserved in the output.
    target, strict :
        See :func:`uniformize_location_values`.
    context : str
        Short label included in the :class:`ValueError` message (typically the
        caller's name) so failures are self-describing.

    Returns
    -------
    list[str]
        Resolved values in the ``target`` vocabulary, one per input.

    Raises
    ------
    ValueError
        If any input cannot be resolved. The message names every unmatched
        label.
    """
    labels = list(values)
    resolved = uniformize_location_values(
        labels,
        target=target,
        strict=strict,
        passthrough_on_miss=False,
        context=context,
    )
    unmatched = [
        label for label, value in zip(labels, resolved) if value is None
    ]
    if unmatched:
        raise ValueError(
            f"{context}: {len(unmatched)} region label(s) could not be "
            f"resolved to {RegionVocabulary(target).value}: {unmatched}. "
            "Provide ARM location ids (e.g. 'northeurope'), billing meter "
            "regions (e.g. 'EU North'), or display names (e.g. 'North "
            "Europe')."
        )
    return [value for value in resolved if value is not None]


def resolve_location_value(
    value: str | None,
    *,
    target: RegionVocabulary = RegionVocabulary.ARM_LOCATION,
    strict: bool = True,
    context: str = "resolve_location_value",
) -> str:
    """Strictly resolve a single region label (see :func:`resolve_location_values`)."""
    return resolve_location_values(
        [value], target=target, strict=strict, context=context,
    )[0]


def translate_location_column(
    df: pd.DataFrame,
    source_column: str,
    target_column: str | None = None,
    *,
    target: RegionVocabulary = RegionVocabulary.ARM_LOCATION,
    strict: bool = True,
    passthrough_on_miss: bool = False,
) -> pd.DataFrame:
    """Translate a DataFrame location column into ``target`` vocabulary.

    Thin wrapper around :func:`uniformize_location_values`. Returns a copy of
    ``df`` with ``target_column`` (defaults to ``source_column``, in-place
    rewrite) holding the translated values as a pandas ``string`` array.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame to update.
    source_column : str
        Column holding the region labels to translate.
    target_column : str, optional
        Column to write the translated values into. Defaults to
        ``source_column``.
    target, strict, passthrough_on_miss :
        See :func:`uniformize_location_values`.
    """
    df = df.copy()
    mapped = uniformize_location_values(
        df[source_column].tolist(),
        target=target,
        strict=strict,
        passthrough_on_miss=passthrough_on_miss,
        context=f"translate_location_column({source_column!r})",
    )
    df[target_column or source_column] = pd.array(mapped, dtype="string")
    return df
