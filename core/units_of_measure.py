"""
units_of_measure.py
────────────────────────────────────────────────────────────────────────────────
Parse Azure-style Unit-of-Measure (UoM) strings and convert quantities
between compatible UoMs.

A UoM string is parsed into:
    (canonical_magnitude, dimension)
where:
  canonical_magnitude : float
      The amount of canonical base units (bytes for data, seconds for time)
      represented by ONE source unit. For pure-count UoMs this is just the
      leading numeric factor.
  dimension           : (data_power, time_power)
      Integer powers; e.g. GB/Month -> (1, -1), GB·Hours -> (1, 1),
      Hours -> (0, 1), pure count -> (0, 0).

Conversion between two UoMs is valid only when their dimensions match.
The result of converting `quantity` from source to target is:
    quantity * src_canonical_magnitude / tgt_canonical_magnitude
"""

from __future__ import annotations

import re


# Numeric multiplier suffixes for the leading factor (e.g. "10K" -> 10000).
# Matched only when directly adjacent to the digits (no whitespace).
_FACTOR_SUFFIX_MULTIPLIERS = {
    'k': 1e3, 'm': 1e6, 'b': 1e9, 'g': 1e9, 't': 1e12,
}

# Peel off the leading factor: digits (optionally decimal) and an optional
# single-letter multiplier suffix that must be followed by whitespace, '/', or
# end-of-string. This avoids eating the 'G' of '1 GB' or the 'K' of '1 KB'.
_FACTOR_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)([kmbgt])?(?=\s|/|$)',
    re.IGNORECASE,
)

# Data units -> bytes (decimal SI and binary IEC).
_DATA_UNITS = {
    'b':  1, 'byte': 1, 'bytes': 1,
    'kb': 1e3,  'mb': 1e6,  'gb': 1e9,  'tb': 1e12, 'pb': 1e15,
    'kib': 2**10, 'mib': 2**20, 'gib': 2**30, 'tib': 2**40, 'pib': 2**50,
}

# Time units -> seconds. Month=30 days, Year=365 days (Azure billing convention).
_TIME_UNITS = {
    'second': 1,           'seconds': 1,
    'minute': 60,          'minutes': 60,
    'hour':   3600,        'hours':   3600,
    'day':    86400,       'days':    86400,
    'month':  30 * 86400,  'months':  30 * 86400,
    'year':   365 * 86400, 'years':   365 * 86400,
}

# Tokens that contribute no dimension and no magnitude (pure-count modifiers).
_COUNT_TOKENS = {
    'unit', 'units', 'count', 'counts',
    'api', 'call', 'calls',
    'rotation', 'rotations',
    'iops',
    'transaction', 'transactions',
}


def _parse_factor(s: str) -> tuple[float, str]:
    """Peel the leading numeric factor (with optional K/M/B/G/T suffix) off `s`.

    Returns (factor, remainder). When `s` has no leading number, factor is 1.0
    and the remainder is `s` unchanged.
    """
    m = _FACTOR_RE.match(s)
    if not m:
        return 1.0, s
    factor = float(m.group(1))
    suffix = m.group(2)
    if suffix:
        factor *= _FACTOR_SUFFIX_MULTIPLIERS[suffix.lower()]
    return factor, s[m.end():]


def _parse_dim_part(part: str) -> tuple[float, tuple[int, int]]:
    """Parse a numerator or denominator part: a sequence of unit tokens.

    Returns (magnitude, (data_power, time_power)).
    Empty input yields (1.0, (0, 0)). Unknown tokens raise ValueError.
    """
    magnitude = 1.0
    data_p = 0
    time_p = 0
    for tok in part.split():
        tok_l = tok.lower()
        if tok_l in _DATA_UNITS:
            magnitude *= _DATA_UNITS[tok_l]
            data_p += 1
        elif tok_l in _TIME_UNITS:
            magnitude *= _TIME_UNITS[tok_l]
            time_p += 1
        elif tok_l in _COUNT_TOKENS:
            pass  # dimensionless contribution
        else:
            raise ValueError(f"Unknown unit token: {tok!r}")
    return magnitude, (data_p, time_p)


def _parse_qualifier(qual: str) -> tuple[float, tuple[int, int]]:
    """Parse the post-factor qualifier; supports one optional '/' separator."""
    qual = qual.strip()
    if not qual:
        return 1.0, (0, 0)
    parts = qual.split('/')
    if len(parts) > 2:
        raise ValueError(f"Cannot parse qualifier (too many '/'): {qual!r}")
    num_mag, num_dim = _parse_dim_part(parts[0])
    if len(parts) == 2:
        den_mag, den_dim = _parse_dim_part(parts[1])
        return num_mag / den_mag, (num_dim[0] - den_dim[0], num_dim[1] - den_dim[1])
    return num_mag, num_dim


def _parse_uom(uom: str) -> tuple[float, tuple[int, int]]:
    """Parse a UoM string into (canonical_magnitude, (data_power, time_power))."""
    if uom is None:
        return 1.0, (0, 0)
    factor, rest = _parse_factor(uom)
    qual_mag, qual_dim = _parse_qualifier(rest)
    return factor * qual_mag, qual_dim


def get_quantity(source_unit_of_measure: str, quantity: float, target_unit_of_measure: str) -> float:
    """Convert `quantity` from `source_unit_of_measure` to `target_unit_of_measure`.

    A UoM string follows the pattern "<factor><qualifier>" where:
      - <factor> is an optional decimal number, optionally followed (no space)
        by a multiplier suffix (K, M, B/G, T). Examples: "100", "10K", "1.5".
        When absent, the factor defaults to 1.
      - <qualifier> describes the unit. It may contain data-size tokens
        (B/KB/MB/GB/TB/PB and their KiB/MiB/... binary counterparts), time
        tokens (Second(s), Minute(s), Hour(s), Day(s), Month(s), Year(s)), and
        pure-count modifiers (Unit, Count, API Calls, Rotations, IOPS, ...).
        A single '/' separates numerator from denominator.

    Conversion is valid only when source and target share the same dimension
    signature (data_power, time_power); otherwise ValueError is raised.

    Examples
    --------
    get_quantity('100',          1, '1000')         -> 0.1
    get_quantity('1000',         5, '100')          -> 50.0
    get_quantity('10K',          1, '10000')        -> 1.0
    get_quantity('1 GB/Month',   1, '1 TB/Month')   -> 0.001
    get_quantity('1 TB/Month', 100, '1 GB/Month')   -> 100000.0
    get_quantity('1 Hour',       2, '1 Minute')     -> 120.0
    get_quantity('1 GB Hours',   1, '1 TB Hours')   -> 0.001

    Raises
    ------
    ValueError
        If either UoM cannot be parsed, or if the two UoMs have incompatible
        dimensions (e.g. "100" cannot be converted to "1 GB/Month").
    """
    src_mag, src_dim = _parse_uom(source_unit_of_measure)
    tgt_mag, tgt_dim = _parse_uom(target_unit_of_measure)
    if src_dim != tgt_dim:
        raise ValueError(
            f"Incompatible UoM dimensions: {source_unit_of_measure!r} {src_dim} "
            f"-> {target_unit_of_measure!r} {tgt_dim}."
        )
    return quantity * src_mag / tgt_mag


def get_unit_price(
    source_unit_of_measure: str,
    unit_price: float,
    target_unit_of_measure: str,
) -> float:
    """Convert a per-unit price from one UoM to another.

    Prices scale inversely to quantities: if ``1 source_uom == K target_uom``
    (i.e. ``get_quantity(src, 1, tgt) == K``), then ``price_per_tgt =
    price_per_src / K``.

    Examples
    --------
    get_unit_price('1 TB/Month', 1000, '1 GB/Month')  -> 1.0
    get_unit_price('1 GB/Month',    1, '1 TB/Month')  -> 1000.0
    get_unit_price('1 Hour',       60, '1 Minute')    -> 1.0

    Raises
    ------
    ValueError
        If either UoM cannot be parsed, or the two UoMs have incompatible
        dimensions.
    """
    return unit_price / get_quantity(source_unit_of_measure, 1.0, target_unit_of_measure)
