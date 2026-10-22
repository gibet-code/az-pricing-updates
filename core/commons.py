

import glob
import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

DEFAULT_CACHE_PATH_DIRECTORY = r"C:\_finops_cache_"

# ---------------------------------------------------------------------------
# Custom parquet schema metadata
# ---------------------------------------------------------------------------
# Prefix used to tag custom metadata entries written into the schema of cached
# parquet files. Stripping the prefix on read isolates our keys from arrow /
# pandas built-in metadata (e.g. b'pandas', b'ARROW:schema').
CUSTOM_METADATA_PREFIX = "custom_meta_"


def write_parquet_with_metadata(
    df: pd.DataFrame,
    path: str,
    custom_metadata: dict[str, Any],
) -> None:
    """
    Write `df` to `path` as parquet, embedding `custom_metadata` into the schema.

    Each (key, value) pair becomes a schema metadata entry with the key prefixed
    by CUSTOM_METADATA_PREFIX and the value JSON-encoded so non-string types
    (lists, dicts, ints, None) round-trip cleanly.
    Existing schema metadata produced by pyarrow / pandas is preserved.
    """
    table = pa.Table.from_pandas(df)
    existing = dict(table.schema.metadata or {})
    for key, value in custom_metadata.items():
        existing[f"{CUSTOM_METADATA_PREFIX}{key}".encode()] = json.dumps(
            value, default=_json_default
        ).encode()
    table = table.replace_schema_metadata(existing)
    pq.write_table(table, path)


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for types not natively serializable.

    Timestamps / datetimes / dates are emitted as ISO 8601 strings; consumers
    are expected to coerce them back via ``pd.Timestamp(...)`` if they need a
    ``Timestamp`` object.
    """
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def read_parquet_custom_metadata(path: str) -> dict[str, Any]:
    """
    Read custom metadata from the schema of a parquet file.

    Only entries whose key starts with CUSTOM_METADATA_PREFIX are returned;
    arrow / pandas built-in metadata is filtered out. Keys are decoded and the
    prefix is stripped; values are JSON-decoded.
    Returns an empty dict if the file has no schema metadata or no entries
    match the prefix (e.g. cache files written by an earlier version).
    """
    raw = pq.read_schema(path).metadata or {}
    prefix_bytes = CUSTOM_METADATA_PREFIX.encode()
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k.startswith(prefix_bytes):
            out[k[len(prefix_bytes):].decode()] = json.loads(v.decode())
    return out


def compute_date_range(
    source_files: list[str],
    source_format: str,
    date_columns: list[str],
    csv_date_format: str | list[str],
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """
    Compute (min, max) across one or more date columns of `source_files`.

    Reads only what is strictly necessary:
    - Parquet: parses ONLY the file footer statistics (no row data) for each
      requested column. Falls back to a single-column scan for files where
      statistics are missing for that column.
    - CSV: reads ONLY the requested column(s) via ``pd.read_csv(usecols=...)``
      with the pyarrow engine, then casts to datetime.

    `date_columns` is a list of column names; the global min/max are taken
    across all listed columns (e.g. for pricesheets that have separate
    EffectiveStartDate / EffectiveEndDate columns).

    Returns ``(None, None)`` if `date_columns` is empty / falsy.
    """
    if not date_columns:
        return None, None

    overall_min: pd.Timestamp | None = None
    overall_max: pd.Timestamp | None = None

    def _update(lo: pd.Timestamp | None, hi: pd.Timestamp | None) -> None:
        nonlocal overall_min, overall_max
        if lo is not None:
            overall_min = lo if overall_min is None else min(overall_min, lo)
        if hi is not None:
            overall_max = hi if overall_max is None else max(overall_max, hi)

    for f in source_files:
        if source_format == 'parquet':
            pf = pq.ParquetFile(f)
            schema_names = pf.schema_arrow.names
            for col in date_columns:
                if col not in schema_names:
                    continue
                col_idx = schema_names.index(col)
                stats_min: pd.Timestamp | None = None
                stats_max: pd.Timestamp | None = None
                stats_complete = True
                for rg_idx in range(pf.num_row_groups):
                    stats = pf.metadata.row_group(rg_idx).column(col_idx).statistics
                    if stats is None or not stats.has_min_max:
                        stats_complete = False
                        break
                    try:
                        lo = pd.Timestamp(stats.min)
                        hi = pd.Timestamp(stats.max)
                    except (pa.ArrowNotImplementedError, NotImplementedError):
                        # e.g. INT96 timestamps (physical_type 3) — pyarrow
                        # cannot expose min/max as scalars. Fall back to scan.
                        stats_complete = False
                        break
                    stats_min = lo if stats_min is None else min(stats_min, lo)
                    stats_max = hi if stats_max is None else max(stats_max, hi)
                if stats_complete and stats_min is not None:
                    _update(stats_min, stats_max)
                else:
                    # Fallback: single-column scan via pyarrow.
                    table = pq.read_table(f, columns=[col])
                    series = table.column(0).to_pandas()
                    series = pd.to_datetime(series, errors='coerce')
                    _update(series.min(), series.max())
        else:  # csv
            # Use pyarrow.csv directly: multithreaded reader + native min_max,
            # avoiding the pandas DataFrame materialization and pd.to_datetime
            # overhead. This is ~5-10x faster on multi-GB usage CSVs.
            present = [
                c for c in date_columns
                if c in pd.read_csv(f, nrows=0).columns
            ]
            if not present:
                continue
            read_opts    = pa_csv.ReadOptions(use_threads=True)
            convert_opts = pa_csv.ConvertOptions(
                include_columns=present,
                column_types={c: pa.timestamp('s') for c in present},
                timestamp_parsers=(
                    [csv_date_format] if isinstance(csv_date_format, str)
                    else list(csv_date_format)
                ),
            )
            table = pa_csv.read_csv(f, read_options=read_opts, convert_options=convert_opts)
            for col in present:
                lo_hi = pc.min_max(table.column(col)).as_py()
                lo, hi = lo_hi['min'], lo_hi['max']
                _update(
                    pd.Timestamp(lo) if lo is not None else None,
                    pd.Timestamp(hi) if hi is not None else None,
                )

    return overall_min, overall_max


def _column_equiv_key(name: str, synonyms: dict[str, list[str]]) -> str:
    """
    Return a stable identity for `name` under case-insensitive + synonym
    equivalence. Two column names are equivalent iff they share the same
    equiv key. The key is the lowercased canonical name when `name` matches
    a canonical or one of its aliases (case-insensitively); otherwise it is
    the lowercased name itself.
    """
    lower = name.lower()
    for canonical, aliases in synonyms.items():
        if lower == canonical.lower() or any(lower == a.lower() for a in aliases):
            return canonical.lower()
    return lower


def resolve_column_name(
    target: str,
    candidates: list[str],
    synonyms: dict[str, list[str]] | None = None,
) -> str | None:
    """
    Find the candidate name that represents the same logical column as `target`.

    `synonyms` (canonical → list of aliases) declares bi-directional name
    equivalence: a canonical and any of its aliases are interchangeable for
    matching purposes. Both the target and the candidates are evaluated
    against this equivalence relation.

    Match priority (first hit wins):
      1. Exact: `target` is in `candidates`.
      2. Synonym / case-insensitive: any candidate whose equivalence key
         (see :func:`_column_equiv_key`) equals the target's equivalence key.

    Returns the matched candidate name, or ``None`` if no match. Raises
    ``ValueError`` if more than one non-exact candidate matches (defensive
    guard against ambiguous schemas).
    """
    synonyms = synonyms or {}
    candidates = list(candidates)

    # 1. Exact match takes precedence even when other candidates are also
    #    equivalence-key-matches (e.g. file contains both 'Cost' and 'cost').
    if target in candidates:
        return target

    target_key = _column_equiv_key(target, synonyms)
    matches = [c for c in candidates if _column_equiv_key(c, synonyms) == target_key]

    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous column resolution for {target!r}: multiple candidates "
            f"match: {matches}."
        )
    return matches[0] if matches else None


def build_rename_map(
    canonical_cols: list[str] | None,
    detected_cols: list[str],
    synonyms: dict[str, list[str]],
) -> dict[str, str]:
    """
    Build a {source_name: canonical_name} rename map for column synonym resolution.

    When canonical_cols is None (load-all), every canonical name in `synonyms`
    that is absent from the detected columns but has a matching alias / case
    variant present is mapped to its canonical form.

    When canonical_cols is provided, each requested canonical name that is
    absent from the detected columns is resolved via :func:`resolve_column_name`
    (alias or case-insensitive match against detected_cols).

    All matching is delegated to :func:`resolve_column_name` so file-side and
    user-side resolution share identical rules (exact > synonym > case-insensitive).
    """
    rename_map: dict[str, str] = {}
    detected_set = set(detected_cols)

    targets = list(synonyms.keys()) if canonical_cols is None else canonical_cols
    for canonical in targets:
        if canonical in detected_set:
            continue
        match = resolve_column_name(canonical, detected_cols, synonyms)
        if match is not None and match != canonical:
            rename_map[match] = canonical

    return rename_map


class UserColumnResolver:
    """
    Stateful helper that resolves user-supplied column names against canonical
    pools and tracks the inverse rename to apply on the returned DataFrame.

    Loaders (e.g. ``load_usage_details``, ``load_pricesheet``) feed every
    user-supplied name (`columns=`, `filters=` keys, `aggregate=`, …) through
    this resolver. Each name is resolved via :func:`resolve_column_name`, so
    matching rules (exact > synonym > case-insensitive) are identical to
    file-side resolution in :func:`build_rename_map`.

    The resolver accumulates:
      • the canonical-named replacements for each input list / dict
      • an `output_rename_map` (canonical → user casing) to apply just before
        returning the DataFrame, so the caller sees the casing they asked for
      • any unresolved names, raised in a single aggregated ``ValueError`` from
        :meth:`finalize`

    Conflict policy: when several inputs resolve to the same canonical with
    different casings, the first input that sets ``priority=True`` wins;
    otherwise first-seen wins.
    """

    def __init__(self, contract_type_label: str) -> None:
        self._label = contract_type_label
        self._output_rename_map: dict[str, str] = {}
        self._priority_set: set[str] = set()
        self._unresolved: list[tuple[str, str]] = []  # (user_name, source_arg)

    def _resolve_one(
        self,
        name: str,
        candidates: list[str],
        synonyms: dict[str, list[str]],
        source_arg: str,
        priority: bool,
    ) -> str | None:
        try:
            canonical = resolve_column_name(name, candidates, synonyms)
        except ValueError as e:
            raise ValueError(f"{source_arg}=: {e}") from None
        if canonical is None:
            self._unresolved.append((name, source_arg))
            return None
        # First-seen wins overall, but a `priority=True` input overrides any
        # earlier non-priority entry for the same canonical.
        if canonical not in self._output_rename_map or (
            priority and canonical not in self._priority_set
        ):
            self._output_rename_map[canonical] = name
            if priority:
                self._priority_set.add(canonical)
        return canonical

    def resolve_list(
        self,
        names: list[str] | None,
        candidates: list[str],
        synonyms: dict[str, list[str]],
        source_arg: str,
        priority: bool = False,
    ) -> list[str] | None:
        """Resolve every name in `names`; deduplicate while preserving order."""
        if names is None:
            return None
        resolved = [
            self._resolve_one(n, candidates, synonyms, source_arg, priority)
            for n in names
        ]
        seen: set = set()
        return [c for c in resolved if c is not None and not (c in seen or seen.add(c))]

    def resolve_dict_keys(
        self,
        mapping: dict[str, list] | None,
        candidates: list[str],
        synonyms: dict[str, list[str]],
        source_arg: str,
    ) -> dict[str, list] | None:
        """Resolve dict keys; values pass through unchanged."""
        if mapping is None:
            return None
        out: dict[str, list] = {}
        for k, v in mapping.items():
            canonical = self._resolve_one(k, candidates, synonyms, source_arg, False)
            if canonical is not None:
                out[canonical] = v
        return out

    def finalize(self) -> dict[str, str]:
        """
        Raise an aggregated ``ValueError`` for any unresolved names and return
        the `output_rename_map` (canonical → user casing), with no-op pairs
        stripped so callers can treat an empty dict as "no rename needed".
        """
        if self._unresolved:
            details = ", ".join(f"{n!r} (in {s})" for n, s in self._unresolved)
            raise ValueError(
                f"Unknown column(s) for contract_type={self._label!r}: {details}."
            )
        return {k: v for k, v in self._output_rename_map.items() if k != v}


# ---------------------------------------------------------------------------
# Shared path-resolution + contract-type detection helpers used by
# load_usage_details / load_pricesheet / get_contract_type in both modules.
# ---------------------------------------------------------------------------
def resolve_source_files(
    path: str, source_format: str = 'auto'
) -> tuple[list[str], str, str]:
    """
    Resolve `path` to a concrete list of CSV or Parquet files.

    The path is handled in three mutually exclusive cases, evaluated in order:

      Case A — Wildcard / glob pattern (path contains ``*``, ``?``, or ``[``):
        expand with :func:`glob.glob`; if no files match, raise
        ``FileNotFoundError``. If ``source_format`` is ``'csv'`` or
        ``'parquet'``, keep only matches with that extension (raise
        ``ValueError`` if none remain). If ``source_format='auto'``, inspect
        extensions of all matches; raise ``ValueError`` on unrecognised
        extensions or on a mix of ``.csv`` and ``.parquet`` files.

      Case B — Single file (path is not a glob and is an existing file):
        determine format from extension (``.csv`` or ``.parquet``); raise
        ``ValueError`` for anything else. If ``source_format`` was supplied
        explicitly it must agree with the extension.

      Case C — Directory (path is an existing directory):
        glob ``*.csv`` and/or ``*.parquet`` inside it; for ``'auto'``,
        raise ``ValueError`` on ambiguity (both present) or emptiness
        (neither present).

    Returns
    -------
    tuple
        ``(source_files, actual_source_format, source_type)`` where
        ``actual_source_format`` is ``'csv'`` or ``'parquet'`` and
        ``source_type`` is ``'glob'``, ``'single_file'``, or ``'directory'``.

    Raises
    ------
    FileNotFoundError
        If the path does not exist and is not a valid glob pattern, or if
        a glob pattern matches nothing.
    ValueError
        If ``source_format`` is invalid, if the file extension / detected
        format is inconsistent with the requested format, or if mixed file
        formats are found.
    """
    _VALID_SOURCE_FORMATS = {'csv', 'parquet', 'auto'}
    if source_format not in _VALID_SOURCE_FORMATS:
        raise ValueError(
            f"Invalid source_format={source_format!r}. Expected one of: {sorted(_VALID_SOURCE_FORMATS)}."
        )

    source_files: list[str] = []
    actual_source_format: str | None = None

    _WILDCARDS = ('*', '?', '[')
    is_glob = any(c in path for c in _WILDCARDS)

    if is_glob:
        source_type = 'glob'
        matched = glob.glob(path)
        if not matched:
            raise FileNotFoundError(f"No files matched the glob pattern: {path!r}")

        if source_format == 'csv':
            source_files = [f for f in matched if f.lower().endswith('.csv')]
            if not source_files:
                raise ValueError(f"source_format='csv' but no .csv files matched: {path!r}")
            actual_source_format = 'csv'
        elif source_format == 'parquet':
            source_files = [f for f in matched if f.lower().endswith('.parquet')]
            if not source_files:
                raise ValueError(f"source_format='parquet' but no .parquet files matched: {path!r}")
            actual_source_format = 'parquet'
        else:  # auto
            extensions = {os.path.splitext(f)[1].lower() for f in matched}
            has_csv = '.csv' in extensions
            has_parquet = '.parquet' in extensions
            unrecognised = extensions - {'.csv', '.parquet'}
            if unrecognised:
                raise ValueError(
                    f"Glob pattern matched files with unrecognised extensions: {unrecognised}"
                )
            if has_csv and has_parquet:
                raise ValueError(
                    f"Glob pattern matched both .csv and .parquet files; "
                    f"set source_format explicitly to resolve the ambiguity."
                )
            actual_source_format = 'csv' if has_csv else 'parquet'
            source_files = matched

    elif os.path.isfile(path):
        source_type = 'single_file'
        ext = os.path.splitext(path)[1].lower()
        if ext not in ('.csv', '.parquet'):
            raise ValueError(f"Unsupported file extension {ext!r} for {path!r}. Expected .csv or .parquet.")
        file_format = ext.lstrip('.')

        if source_format == 'auto':
            actual_source_format = file_format
            source_files = [path]
        elif source_format == file_format:
            actual_source_format = source_format
            source_files = [path]
        else:
            raise ValueError(
                f"source_format={source_format!r} but file extension is {ext!r}: {path!r}"
            )

    elif os.path.isdir(path):
        source_type = 'directory'
        if source_format == 'csv':
            source_files = glob.glob(os.path.join(path, '*.csv'))
            if not source_files:
                raise ValueError(f"No .csv files found in directory: {path!r}")
            actual_source_format = 'csv'
        elif source_format == 'parquet':
            source_files = glob.glob(os.path.join(path, '*.parquet'))
            if not source_files:
                raise ValueError(f"No .parquet files found in directory: {path!r}")
            actual_source_format = 'parquet'
        else:  # auto
            csv_files = glob.glob(os.path.join(path, '*.csv'))
            parquet_files = glob.glob(os.path.join(path, '*.parquet'))
            if csv_files and parquet_files:
                raise ValueError(
                    f"Directory {path!r} contains both .csv and .parquet files; "
                    f"set source_format explicitly to resolve the ambiguity."
                )
            if not csv_files and not parquet_files:
                raise ValueError(f"No .csv or .parquet files found in directory: {path!r}")
            if csv_files:
                actual_source_format = 'csv'
                source_files = csv_files
            else:
                actual_source_format = 'parquet'
                source_files = parquet_files

    else:
        raise FileNotFoundError(
            f"Path does not exist and is not a valid glob pattern: {path!r}"
        )

    logging.debug(
        "Source resolved — type: %s | format: %s | %d file(s).",
        source_type, actual_source_format, len(source_files),
    )
    return source_files, actual_source_format, source_type


def detect_contract_type(
    source_files: list[str],
    actual_source_format: str,
    ea_marker: str,
    mca_marker: str,
) -> Literal['ea', 'mca', 'unknown']:
    """
    Detect the contract type from the column headers of ``source_files[0]``.

    Reads only the headers (no data rows): for CSV via
    ``pd.read_csv(nrows=0)``, for Parquet via ``pq.read_schema``.

    Returns ``'ea'`` if only ``ea_marker`` is present, ``'mca'`` if only
    ``mca_marker`` is present, ``'unknown'`` if both or neither are present.
    Never raises on ambiguity — callers decide whether 'unknown' is fatal.
    """
    first_file = source_files[0]
    if actual_source_format == 'csv':
        detected_columns = pd.read_csv(first_file, nrows=0).columns.tolist()
    else:
        detected_columns = pq.read_schema(first_file).names

    is_ea = ea_marker in detected_columns
    is_mca = mca_marker in detected_columns
    if is_ea and not is_mca:
        return 'ea'
    if is_mca and not is_ea:
        return 'mca'
    return 'unknown'
