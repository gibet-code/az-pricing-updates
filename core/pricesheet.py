import glob
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from typing import Literal
import pyarrow.parquet as pq

import pandas as pd

from core._sources_common import (
    ColumnFilter,
    apply_in_memory as _apply_in_memory_filters,
    normalize_filters as _normalize_filters,
    serialize_for_cache_key as _serialize_filters_for_cache,
    to_parquet_pushdown as _to_parquet_pushdown,
)
from core.commons import (
    DEFAULT_CACHE_PATH_DIRECTORY,
    UserColumnResolver,
    build_rename_map,
    compute_date_range,
    detect_contract_type,
    read_parquet_custom_metadata,
    resolve_column_name,
    resolve_source_files,
    write_parquet_with_metadata,
)


# Columns whose string values are lowercased after load for canonical casing.
# Empty by default — no candidate column on the pricesheet today; populating
# the tuple is a one-line edit symmetric with :mod:`core.usage_data`. The
# effective intersection with the loaded columns is included in the cache
# key so edits to this list invalidate only caches that actually touched the
# affected columns.
_LOWERCASE_NORMALIZED_COLUMNS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Metadata returned alongside the DataFrame when return_metadata=True.
# All fields are optional; missing entries (e.g. on cache hits produced by an
# older version of this module) are populated as None.
# ---------------------------------------------------------------------------
@dataclass
class PricesheetMetadata:
    contract_type: str | None = None        # 'ea' or 'mca'
    source_files: list[str] | None = None
    effective_start_date: pd.Timestamp | None = None
    effective_end_date: pd.Timestamp | None = None


# Per-format date column(s) used by compute_date_range to derive
# effective_start_date / effective_end_date directly from the source files.
# Both EffectiveStartDate and EffectiveEndDate participate: the global min
# is taken across both columns (start date) and the global max as well
# (end date), so the metadata reflects the full validity window covered.
# MCA placeholder — leave empty until the MCA schema is confirmed.
EA_PRICESHEET_DATE_COLUMNS: list[str] = ['EffectiveStartDate', 'EffectiveEndDate']
MCA_PRICESHEET_DATE_COLUMNS: list[str] = ['EffectiveStartDate', 'EffectiveEndDate']


# Sentinel value used in *_COLUMN_DTYPES to flag date columns.
# These are routed to parse_dates= instead of dtype= in pd.read_csv,
# because pandas does not allow a column to appear in both arguments simultaneously.
_PARSE_DATE_SENTINEL = 'parse_date'

# ---------------------------------------------------------------------------
# EA (Enterprise Agreement) column schema
# ---------------------------------------------------------------------------
# Maps every EA pricesheet column to its target pandas dtype, or to
# _PARSE_DATE_SENTINEL for columns that must be parsed as dates.
# EA_DATE_FORMAT is the strptime format string for those date columns.
EA_COLUMN_DTYPES: dict = {
    # --- high-cardinality identifiers: kept as str ---
    'EnrollmentNumber':   'str',
    'MeterId':            'str',
    'MeterName':          'str',
    # --- low/medium-cardinality dimensions: stored as category to save memory ---
    'MeterType':          'category',
    'MeterCategory':      'category',
    'MeterSubCategory':   'category',
    'ServiceFamily':      'category',
    # --- high-cardinality identifiers (continued) ---
    'Product':            'str',
    'SkuId':              'str',
    'ProductId':          'str',
    # --- low/medium-cardinality dimensions (continued) ---
    'MeterRegion':        'category',
    'UnitOfMeasure':      'category',
    # --- high-cardinality identifiers (continued) ---
    'PartNumber':         'str',
    # --- date columns: routed to pd.to_datetime post-load ---
    'EffectiveStartDate': _PARSE_DATE_SENTINEL,
    'EffectiveEndDate':   _PARSE_DATE_SENTINEL,
    # --- numeric measures ---
    'UnitPrice':          pd.Float64Dtype(),
    'BasePrice':          pd.Float64Dtype(),
    'MarketPrice':        pd.Float64Dtype(),
    # --- low-cardinality codes and enumerations ---
    'CurrencyCode':       'category',
    # --- numeric measures (continued) ---
    'IncludedQuantity':   pd.Float64Dtype(),
    # --- high-cardinality identifiers (continued) ---
    'OfferId':            'str',
    # --- low-cardinality codes and enumerations (continued) ---
    'Term':               'category',
    'PriceType':          'category',
}
EA_DATE_FORMAT: str = "%m/%d/%Y %H:%M:%S"

# ---------------------------------------------------------------------------
# MCA (Microsoft Customer Agreement) column schema
# ---------------------------------------------------------------------------
# Maps every MCA pricesheet column to its target pandas dtype, or to
# _PARSE_DATE_SENTINEL for columns that must be parsed as dates.
# MCA_DATE_FORMAT is the strptime format string for those date columns
# (only used for CSV; parquet stores timestamps natively).
# Schema derived from the parquet export; CSV columns are assumed identical.
MCA_COLUMN_DTYPES: dict = {
    # --- high-cardinality identifiers: kept as str ---
    'BillingAccountId':   'category',
    'BillingAccountName': 'category',
    'BillingProfileId':   'category',
    'BillingProfileName': 'category',
    # --- low/medium-cardinality dimensions: stored as category to save memory ---
    'ServiceFamily':      'category',
    # --- high-cardinality identifiers (continued) ---
    'Product':            'str',
    'ProductId':          'str',
    'SkuId':              'str',
    # --- low/medium-cardinality dimensions (continued) ---
    'UnitOfMeasure':      'category',
    # --- high-cardinality identifiers (continued) ---
    'MeterId':            'str',
    'MeterName':          'str',
    # --- low/medium-cardinality dimensions (continued) ---
    'MeterType':          'category',
    'MeterCategory':      'category',
    'MeterSubCategory':   'category',
    'MeterRegion':        'category',
    # --- numeric measures ---
    'TierMinimumUnits':   pd.Float64Dtype(),
    # --- date columns: routed to pd.to_datetime post-load ---
    'EffectiveStartDate': _PARSE_DATE_SENTINEL,
    'EffectiveEndDate':   _PARSE_DATE_SENTINEL,
    # --- numeric measures (continued) ---
    'UnitPrice':          pd.Float64Dtype(),
    'BasePrice':          pd.Float64Dtype(),
    'MarketPrice':        pd.Float64Dtype(),
    # --- low-cardinality codes and enumerations ---
    'Currency':           'category',
    'BillingCurrency':    'category',
    'Term':               'category',
    'PriceType':          'category',
}
# MCA pricesheets have been observed in both ISO ("%Y-%m-%d %H:%M:%S") and
# US-locale ("%m/%d/%Y %H:%M:%S") flavours depending on the export tooling and
# billing-account locale. Both are tried in order.
MCA_DATE_FORMATS: tuple[str, ...] = ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S")

# ---------------------------------------------------------------------------
# EA column synonyms — maps each canonical column name to its known aliases
# found in various source formats (e.g. Parquet exports vs CSV exports).
# ---------------------------------------------------------------------------
EA_COLUMN_SYNONYMS: dict[str, list[str]] = {

}
# MCA columns use a single canonical casing across CSV and parquet exports;
# no synonyms required at this time.
MCA_COLUMN_SYNONYMS: dict[str, list[str]] = {}

# ---------------------------------------------------------------------------
# Derived column definitions — format-agnostic computed columns that can be
# requested via the derived_columns= parameter of load_pricesheet().
#
# Each entry maps a derived column name to a dict with:
#   'required_cols' : list[str]  — source columns that must be loaded from the
#                                   file (force-loaded if not in columns=, then
#                                   dropped after computation if not requested).
#   'compute'       : Callable[[pd.DataFrame], pd.Series]
#                                 — receives the concatenated DataFrame and
#                                   returns a Series for the new column.
#                                   Called after concat, before deduplication.
# ---------------------------------------------------------------------------
def _compute_region_neutral_product(df: pd.DataFrame) -> pd.Series:
    # Strips the MeterRegion substring (when non-empty) from Product, then trims
    # trailing spaces / hyphens. Rows with NaN/empty MeterRegion are passed
    # through as Product unchanged.
    product = df['Product'].astype('string').fillna('')
    region  = df['MeterRegion'].astype('string').fillna('')
    out = pd.Series(
        [p.replace(r, '') if r else p for p, r in zip(product, region)],
        index=df.index, dtype='string',
    )
    return out.str.rstrip(' -')


DERIVED_COLUMN_DEFINITIONS: dict[str, dict] = {
    'RegionNeutralProduct': {
        'required_cols': ['Product', 'MeterRegion'],
        'compute':       _compute_region_neutral_product,
    },
}


def _compute_cache_key(
    source_files: list[str],
    actual_source_format: str,
    actual_contract_type: str,
    columns: list[str] | None,
    filters: dict[str, ColumnFilter] | None,
    derived_columns: list[str] | None,
    lowercased_columns: list[str] | None = None,
) -> str:
    """
    Compute a SHA-256 cache key from the resolved load parameters.

    The key is derived from:
    - The absolute path and byte size of every resolved source file (sorted).
    - The resolved source and data format names.
    - The sorted column selection list (None if not specified).
    - The filters dict serialized via :func:`serialize_for_cache_key` so the
      operator (``in`` / ``iin`` / etc.) participates in the hash.
    - The sorted derived_columns list (None if not specified).
    - The list of columns actually lowercased post-load (intersection of
      :data:`_LOWERCASE_NORMALIZED_COLUMNS` with the effective loaded
      columns); only included when non-empty so the empty default doesn't
      invalidate existing caches.

    File sizes are used instead of content hashes for performance; a size
    collision (file replaced with one of identical size) is acceptable given
    the typical size of Azure pricesheet CSVs.
    """
    file_entries = sorted(
        [os.path.abspath(f), os.path.getsize(f)]
        for f in source_files
    )
    payload = {
        'files':           file_entries,
        'source_format':   actual_source_format,
        'contract_type':   actual_contract_type,
        'columns':         sorted(columns)         if columns         is not None else None,
        'filters':         _serialize_filters_for_cache(filters),
        'derived_columns': sorted(derived_columns) if derived_columns is not None else None,
    }
    if lowercased_columns:
        payload['lowercased_columns'] = sorted(lowercased_columns)
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode()).hexdigest()


# Markers used to disambiguate EA vs MCA pricesheet files from their column
# headers (see :func:`commons.detect_contract_type`).
_PRICESHEET_EA_MARKER  = 'EnrollmentNumber'
_PRICESHEET_MCA_MARKER = 'BillingAccountId'


def get_contract_type(
    path: str, source_format: str = 'auto'
) -> Literal['ea', 'mca', 'unknown']:
    """
    Detect the Azure billing contract type of pricesheet files at ``path``.

    Resolves ``path`` (single file, directory, or glob) the same way
    :func:`load_pricesheet` does, then inspects only the column headers of
    the first file to decide between EA and MCA.

    Parameters
    ----------
    path : str
        Path to a single file, a directory, or a glob pattern.
    source_format : str
        ``'csv'``, ``'parquet'``, or ``'auto'`` (default).

    Returns
    -------
    Literal['ea', 'mca', 'unknown']
        ``'ea'`` if only ``'EnrollmentNumber'`` is present in the headers,
        ``'mca'`` if only ``'BillingAccountId'`` is present, ``'unknown'``
        if both or neither are present.

    Raises
    ------
    FileNotFoundError
        If the path does not exist and is not a valid glob pattern, or a glob
        matches nothing.
    ValueError
        If ``source_format`` is invalid, file extensions are inconsistent, or
        mixed file formats are found.
    """
    source_files, actual_source_format, _ = resolve_source_files(path, source_format)
    return detect_contract_type(
        source_files, actual_source_format,
        _PRICESHEET_EA_MARKER, _PRICESHEET_MCA_MARKER,
    )


def load_pricesheet(path: str, source_format: str = 'auto', contract_type: str = 'auto',
                    columns: list[str] = None,
                    filters: dict[str, 'ColumnFilter | list'] = None,
                    derived_columns: list[str] = None,
                    cache_path_directory: str = DEFAULT_CACHE_PATH_DIRECTORY,
                    return_metadata: bool = False):
    """
    Load Azure pricesheet data from one or more CSV or Parquet files into a single DataFrame.

    Parameters
    ----------
    path : str
        Path to a single file, a directory, or a glob pattern (e.g. 'dir/pricesheet_*.csv').
    source_format : str
        'csv', 'parquet', or 'auto' (default). When 'auto', the format is inferred
        from the file extensions found.
    contract_type : str
        'ea' (Enterprise Agreement), 'mca' (Microsoft Customer Agreement), or 'auto'
        (default). When 'auto', the type is detected from the column names.
    columns : list[str], optional
        Subset of columns to load. None (default) loads all columns.
        For CSV files, dtype and date parsing are derived from EA_COLUMN_DTYPES /
        MCA_COLUMN_DTYPES filtered to the requested columns; columns marked with
        _PARSE_DATE_SENTINEL are routed to pd.to_datetime instead of dtype=.
        For Parquet files, column selection is applied but stored types are preserved.
    filters : dict[str, ColumnFilter | list], optional
        Row filters to apply, e.g. {'MeterCategory': ['Virtual Machines'], 'MeterRegion': ['EU West']}.
        None (default) loads all rows. Multiple entries are combined with AND semantics.

        Each value may be either a bare sequence (treated as case-sensitive ``in``)
        or a :class:`ColumnFilter` carrying an explicit operator
        (``'in'`` / ``'not_in'`` / ``'iin'`` / ``'not_iin'``). The ``i*`` variants
        are case-insensitive string matches.

        For Parquet files, ``in`` / ``not_in`` filters are pushed down to the
        row-group level via pyarrow predicate pushdown. ``iin`` / ``not_iin``
        cannot be pushed down and are applied in memory after read; this is
        logged at INFO so the cost is visible.
        For CSV files, the full file is loaded then every filter is applied in
        memory.
        If a filter column is absent from columns=, it is loaded temporarily and
        dropped from the result after filtering.
    derived_columns : list[str], optional
        Additional computed columns to materialize on the returned DataFrame. Each
        name must be a key of DERIVED_COLUMN_DEFINITIONS (e.g. 'RegionNeutralProduct').
        Source columns required by the requested derived columns are force-loaded
        from the file, used to compute the derived value, and then dropped from the
        output if they were not also requested via columns=.
    cache_path_directory : str, optional
        Path to a directory used for caching results. None (default) disables caching.
        On each call a SHA-256 cache key is computed from the resolved source files
        (absolute path + size), the resolved source/data formats, the columns list,
        and the filters. If a matching Parquet file already exists in the directory it
        is returned immediately (cache hit). On a cache miss the data is loaded normally
        and the result is written to the cache directory as 'pricesheet_<hash>.parquet'
        before being returned. The directory is created automatically on the first cache
        write.
    return_metadata : bool, optional
        When True (default False), returns a tuple ``(df, metadata)`` instead of just
        ``df``. ``metadata`` is a :class:`PricesheetMetadata` dataclass exposing
        ``contract_type`` ('ea' / 'mca') and ``source_files`` (sorted absolute paths).
        On a cache hit the metadata is hydrated from the cached parquet's custom
        schema metadata; fields absent from older cache files resolve to ``None``.

    Returns
    -------
    pd.DataFrame or tuple[pd.DataFrame, PricesheetMetadata]
        Concatenated, deduplicated DataFrame of all matched files, with a uniform
        column schema. When ``return_metadata=True``, returns a 2-tuple of
        (DataFrame, metadata).

    Raises
    ------
    FileNotFoundError
        If a given file or directory does not exist, or a glob pattern matches nothing.
    ValueError
        If the file extension / detected format is inconsistent with the requested format,
        if mixed file formats are found, or if contract-type detection is inconclusive.
    """

    _VALID_SOURCE_FORMATS = {'csv', 'parquet', 'auto'}
    _VALID_CONTRACT_TYPES = {'ea', 'mca', 'auto'}

    _t0 = time.perf_counter()

    if source_format not in _VALID_SOURCE_FORMATS:
        raise ValueError(
            f"Invalid source_format={source_format!r}. Expected one of: {sorted(_VALID_SOURCE_FORMATS)}."
        )
    if contract_type not in _VALID_CONTRACT_TYPES:
        raise ValueError(
            f"Invalid contract_type={contract_type!r}. Expected one of: {sorted(_VALID_CONTRACT_TYPES)}."
        )

    # ------------------------------------------------------------------
    # Step 1 — Resolve path to a concrete list of files (source_files)
    # ------------------------------------------------------------------
    source_files, actual_source_format, _source_type = resolve_source_files(
        path, source_format,
    )

    # ------------------------------------------------------------------
    # Step 2 — Detect contract type (EA vs MCA) using column names only
    # ------------------------------------------------------------------
    # contract_type can be 'ea', 'mca', or 'auto'.
    # Detection reads only the column headers of the first file (no full load):
    #   EA  => 'EnrollmentNumber' column is present
    #   MCA => 'BillingAccountId' column is present
    #
    # • If contract_type is 'auto': set actual_contract_type to the detected type.
    #     If both or neither column is found, raise ValueError (inconclusive).
    # • If contract_type is 'ea' or 'mca': still run detection, then compare.
    #     If the detected type differs from the requested one, log a warning
    #     and continue with actual_contract_type = contract_type (honour caller intent).

    _EA_MARKER  = _PRICESHEET_EA_MARKER
    _MCA_MARKER = _PRICESHEET_MCA_MARKER
    first_file = source_files[0]
    detected = detect_contract_type(
        source_files, actual_source_format, _EA_MARKER, _MCA_MARKER,
    )

    if contract_type == 'auto':
        if detected == 'unknown':
            raise ValueError(
                f"Could not auto-detect contract type for {first_file!r}. "
                f"Expected exactly one of {_EA_MARKER!r} (EA) or {_MCA_MARKER!r} (MCA) columns."
            )
        actual_contract_type = detected
    else:
        if detected != 'unknown' and detected != contract_type:
            logging.warning(
                "contract_type=%r was requested but detected type is %r in %s. "
                "Continuing with requested type.",
                contract_type, detected, first_file
            )
        actual_contract_type = contract_type

    logging.debug("Contract type detected: %s.", actual_contract_type)

    # Re-read first-file headers; downstream synonym resolution / schema-mismatch
    # checks need the full header list (detect_contract_type only returns the
    # contract type literal, not the columns).
    if actual_source_format == 'csv':
        detected_columns = pd.read_csv(first_file, nrows=0).columns.tolist()
    else:
        detected_columns = pq.read_schema(first_file).names

    # ------------------------------------------------------------------
    # Step 2a — Resolve user-supplied column names against the canonical
    # schema (case-insensitive + synonym-aware).
    # ------------------------------------------------------------------
    # Every name the user provides in columns= or filters= keys is resolved
    # against the format's canonical pool via commons.UserColumnResolver
    # (exact > synonym > case-insensitive). The local `columns` and `filters`
    # are rewritten with their canonical names, so every downstream step
    # (cache key, dtype lookup, file-side synonym rename) sees a single
    # canonical vocabulary.
    #
    # `output_rename_map` (canonical → user-supplied casing) is applied as the
    # final step before returning, on both cache-hit and cache-miss paths. The
    # cache parquet itself stores canonical names so calls differing only by
    # user casing share a single cache entry.
    #
    # Conflict policy: when the same canonical is requested under multiple
    # casings across inputs, `columns=` wins; otherwise first-seen wins.
    canonical_pool = list(
        EA_COLUMN_DTYPES.keys() if actual_contract_type == 'ea' else MCA_COLUMN_DTYPES.keys()
    )
    user_synonyms_map = (
        EA_COLUMN_SYNONYMS if actual_contract_type == 'ea' else MCA_COLUMN_SYNONYMS
    )

    resolver = UserColumnResolver(actual_contract_type)
    columns         = resolver.resolve_list(columns,         canonical_pool, user_synonyms_map, 'columns', priority=True)
    filters         = resolver.resolve_dict_keys(filters,    canonical_pool, user_synonyms_map, 'filters')
    derived_pool    = list(DERIVED_COLUMN_DEFINITIONS.keys())
    derived_columns = resolver.resolve_list(derived_columns, derived_pool,  {},                'derived_columns')
    output_rename_map = resolver.finalize()
    # Coerce filters to a uniform {col: ColumnFilter} shape; legacy bare-list
    # values become ColumnFilter(values, op='in').
    filters         = _normalize_filters(filters)

    # ------------------------------------------------------------------
    # Validate derived_columns and identify required-only source columns.
    # required-only = source columns needed to compute a derived column that
    # the user did NOT request via columns=. These are force-loaded from the
    # source file and dropped from the result after the derived column is
    # computed (post-concat, pre-deduplicate).
    # ------------------------------------------------------------------
    derived_required_only_cols: list[str] = []
    if derived_columns is not None:
        unknown = [dc for dc in derived_columns if dc not in DERIVED_COLUMN_DEFINITIONS]
        if unknown:
            raise ValueError(
                f"Unknown derived_columns: {unknown}. "
                f"Valid entries are: {sorted(DERIVED_COLUMN_DEFINITIONS.keys())}."
            )
        if columns is not None:
            required: list[str] = []
            for dc in derived_columns:
                for src in DERIVED_COLUMN_DEFINITIONS[dc]['required_cols']:
                    if src not in columns and src not in required:
                        required.append(src)
            derived_required_only_cols = required
            if derived_required_only_cols:
                logging.debug(
                    "Derived-required-only columns %s will be force-loaded and dropped after computation.",
                    derived_required_only_cols,
                )
    if output_rename_map:
        logging.debug(
            "User column casing rename map (canonical → user): %s",
            output_rename_map,
        )

    # ------------------------------------------------------------------
    # Step 2b — Determine filter-only columns, build pyarrow filters, cache check
    # ------------------------------------------------------------------
    # filter_only_cols: filter keys absent from the requested output columns.
    # These are loaded temporarily, used for filtering, then dropped from the result.
    # When columns=None all columns are loaded anyway, so nothing extra is needed.
    #
    # Cache key uses the ORIGINAL columns (before filter-only expansion) plus filters,
    # so the key correctly reflects the final output shape.

    filter_only_cols = []
    if filters is not None and columns is not None:
        filter_only_cols = [c for c in filters if c not in columns]
        if filter_only_cols:
            logging.debug(
                "Filter-only columns %s will be loaded temporarily and dropped after filtering.",
                filter_only_cols,
            )

    # Build pyarrow DNF filter list for Parquet predicate pushdown.
    # Operators 'in' / 'not_in' are pushed down; 'iin' / 'not_iin' are kept
    # as residual filters applied in memory after read_parquet. For CSV every
    # filter is applied in memory via apply_in_memory below.
    pq_filters, residual_filters = _to_parquet_pushdown(filters)
    if residual_filters and actual_source_format == 'parquet':
        for _rcol, _rspec in residual_filters.items():
            logging.info(
                "filter on %r uses op=%r — no parquet pushdown, "
                "applied in memory after read.",
                _rcol, _rspec.op,
            )

    # Effective loaded columns — intersection with _LOWERCASE_NORMALIZED_COLUMNS
    # feeds the cache key so future edits to the constant invalidate only the
    # caches whose loaded columns actually changed.
    effective_loaded_cols = list(columns) if columns is not None else list(canonical_pool)
    lowercased_columns    = [
        c for c in _LOWERCASE_NORMALIZED_COLUMNS if c in effective_loaded_cols
    ]

    # Build metadata describing this load. Populated up front so it is
    # available for both cache hits (overwritten from cached parquet metadata)
    # and cache misses (written into the cache parquet file below).
    metadata = PricesheetMetadata(
        contract_type=actual_contract_type,
        source_files=sorted(os.path.abspath(f) for f in source_files),
    )

    if cache_path_directory is not None:
        cache_key  = _compute_cache_key(
            source_files, actual_source_format, actual_contract_type, columns, filters, derived_columns,
            lowercased_columns=lowercased_columns,
        )
        cache_file = os.path.join(cache_path_directory, f"pricesheet_{cache_key}.parquet")
        logging.debug("Cache key: %s", cache_key)
        if os.path.isfile(cache_file):
            df_cached = pd.read_parquet(cache_file, engine='pyarrow')
            # Cache parquet stores canonical column names; restore the user's
            # casing here so cache hits behave identically to cache misses.
            if output_rename_map:
                df_cached = df_cached.rename(columns=output_rename_map)
            # Hydrate metadata from the cached parquet's custom schema metadata.
            # Keys not present in the cached file (e.g. produced by an older
            # version) resolve to None via dict.get().  Timestamp fields are
            # serialized as ISO strings and need to be coerced back.
            cached_meta_raw = read_parquet_custom_metadata(cache_file)
            for ts_field in ('effective_start_date', 'effective_end_date'):
                if cached_meta_raw.get(ts_field) is not None:
                    cached_meta_raw[ts_field] = pd.Timestamp(cached_meta_raw[ts_field])
            cached_metadata = PricesheetMetadata(
                **{f.name: cached_meta_raw.get(f.name) for f in fields(PricesheetMetadata)}
            )
            logging.info(
                "Returned %d rows in %.3fs — cache hit: %s",
                len(df_cached), time.perf_counter() - _t0, cache_file,
            )
            if return_metadata:
                return df_cached, cached_metadata
            return df_cached
        logging.debug("Cache miss: %s", cache_file)

    # ------------------------------------------------------------------
    # Compute effective date range from source files (cache-miss path only).
    # Reads only the date columns (or just the parquet footer stats), not the
    # full dataset.  On cache hits the values come from the parquet metadata
    # and this is skipped entirely.
    # ------------------------------------------------------------------
    date_cols = (
        EA_PRICESHEET_DATE_COLUMNS if actual_contract_type == 'ea' else MCA_PRICESHEET_DATE_COLUMNS
    )
    metadata.effective_start_date, metadata.effective_end_date = compute_date_range(
        source_files, actual_source_format, date_cols,
        EA_DATE_FORMAT if actual_contract_type == 'ea' else list(MCA_DATE_FORMATS),
    )
    logging.debug(
        "Pricesheet effective date range: %s → %s",
        metadata.effective_start_date, metadata.effective_end_date,
    )

    # Expand columns to include filter-only and derived-required-only columns for
    # loading (after cache check, so the cache key reflects the requested output
    # columns, not the expanded set).
    if filter_only_cols:
        columns = list(columns) + filter_only_cols
    if derived_required_only_cols:
        columns = list(columns) + derived_required_only_cols

    # ------------------------------------------------------------------
    # Step 2c — Resolve column synonyms
    # ------------------------------------------------------------------
    # Build a {source_name: canonical_name} map for columns present in the source
    # file under an alternative name (e.g. 'MeterID' instead of 'MeterId').
    # resolved_columns substitutes source names for the file reader; per-frame
    # renaming back to canonical names happens immediately after loading.
    synonyms_map = EA_COLUMN_SYNONYMS if actual_contract_type == 'ea' else MCA_COLUMN_SYNONYMS
    rename_map = build_rename_map(columns, detected_columns, synonyms_map)
    canonical_to_source = {v: k for k, v in rename_map.items()}
    resolved_columns = (
        [canonical_to_source.get(c, c) for c in columns]
        if columns is not None else None
    )
    if rename_map:
        for src_name, canonical_name in rename_map.items():
            logging.debug(
                "Column synonym resolved: %r (source) → %r (canonical)",
                src_name, canonical_name,
            )
        if pq_filters is not None:
            pq_filters = [
                (canonical_to_source.get(col, col), op, vals)
                for col, op, vals in pq_filters
            ]

    # ------------------------------------------------------------------
    # Step 3 — Validate that all files share the same column schema
    # ------------------------------------------------------------------
    # Read only the headers (no data rows) for efficiency.
    # If any file has a different set of columns, raise ValueError.

    if len(source_files) > 1:
        reference_columns = set(detected_columns)
        for f in source_files[1:]:
            if actual_source_format == 'csv':
                file_columns = set(pd.read_csv(f, nrows=0).columns.tolist())
            else:
                file_columns = set(pq.read_schema(f).names)
            if file_columns != reference_columns:
                raise ValueError(
                    f"Column schema mismatch: {f!r} has different columns than {first_file!r}."
                )

    # ------------------------------------------------------------------
    # Step 4 — Build read parameters, then load and concatenate
    # ------------------------------------------------------------------
    # For CSV:
    #   • Select the schema map for the detected data format (EA or MCA).
    #   • If columns= is provided, restrict the map to the requested subset.
    #   • Split the (possibly filtered) map into two parts:
    #       - active_dtype : {col: dtype}  for non-date columns → passed to dtype=
    #         Includes 'category' dtypes for low-cardinality columns; pandas applies
    #         these as a post-read conversion after the pyarrow engine finishes.
    #       - active_dates : [col, ...]    for sentinel columns
    #         parse_dates= is NOT supported by engine='pyarrow'; date columns are
    #         cast to datetime post-load with pd.to_datetime instead.
    # For Parquet: column selection only; dtypes stored in the file are preserved.

    if actual_source_format == 'csv':
        schema_map      = EA_COLUMN_DTYPES if actual_contract_type == 'ea' else MCA_COLUMN_DTYPES
        schema_date_fmt = EA_DATE_FORMAT   if actual_contract_type == 'ea' else MCA_DATE_FORMATS

        # Restrict to requested columns, or use every column present in the schema map.
        # active_dtype keys are remapped to source names so pd.read_csv dtype= matches
        # the actual column names in the file. active_dates stays canonical: date
        # casting runs after the per-frame synonyms rename.
        active_map   = {c: v for c, v in schema_map.items() if columns is None or c in columns}
        active_dtype = {canonical_to_source.get(c, c): v for c, v in active_map.items() if v != _PARSE_DATE_SENTINEL}
        active_dates = [c for c, v in active_map.items() if v == _PARSE_DATE_SENTINEL]

    frames = []
    for f in source_files:
        logging.info("Loading %s file: %s", actual_source_format, f)
        if actual_source_format == 'csv':
            frame = pd.read_csv(
                f,
                usecols=resolved_columns,
                dtype=active_dtype or None,
                engine='pyarrow',
                na_values=['', 'null', 'NaN', 'nan'],
            )
        else:
            # Parquet: predicate pushdown via pyarrow (row-group level filtering).
            frame = pd.read_parquet(f, engine='pyarrow', columns=resolved_columns, filters=pq_filters)

        # Rename synonym columns to their canonical names before any further processing.
        if rename_map:
            frame = frame.rename(columns=rename_map)

        if actual_source_format == 'csv':
            # parse_dates= and date_format= are not supported by engine='pyarrow'.
            # Cast sentinel-marked date columns to datetime after loading.
            for col in active_dates:
                if col in frame.columns:
                    if isinstance(schema_date_fmt, str):
                        frame[col] = pd.to_datetime(frame[col], format=schema_date_fmt)
                    else:
                        # Try each supported format in order; coerce-and-fill
                        # so rows matching any one format are parsed.
                        raw = frame[col]
                        parsed = pd.to_datetime(raw, format=schema_date_fmt[0], errors='coerce')
                        for fmt in schema_date_fmt[1:]:
                            if parsed.isna().any():
                                fallback = pd.to_datetime(raw, format=fmt, errors='coerce')
                                parsed = parsed.fillna(fallback)
                        frame[col] = parsed
            # CSV has no predicate pushdown — apply every filter in memory.
            frame = _apply_in_memory_filters(frame, filters)
        else:
            # Parquet read above already applied pq_filters; apply any residual
            # filters (e.g. 'iin' / 'not_iin') that pyarrow can't push down.
            frame = _apply_in_memory_filters(frame, residual_filters)

        # Canonicalize casing on identifier columns. Runs *after* filtering so
        # case-sensitive filter ops see raw values; callers that need
        # case-insensitive matching should use ColumnFilter(op='iin') instead.
        for _norm_col in _LOWERCASE_NORMALIZED_COLUMNS:
            if _norm_col in frame.columns:
                frame[_norm_col] = frame[_norm_col].str.lower()

        ### Specific processing to align formating
        if 'Term' in frame.columns:
            frame['Term'] = frame['Term'].astype(str).fillna('').replace('nan', '').astype('category')

        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------------------
    # Post-concat: compute derived columns then drop their required-only sources.
    # Runs before deduplication so derived columns participate in the row-equality
    # check (rows differing only in a dropped source column still collapse).
    # ------------------------------------------------------------------
    if derived_columns is not None:
        for dc in derived_columns:
            df[dc] = DERIVED_COLUMN_DEFINITIONS[dc]['compute'](df)
            logging.debug("Computed derived column '%s'.", dc)
        if derived_required_only_cols:
            df = df.drop(columns=derived_required_only_cols)
            logging.debug(
                "Dropped derived-required-only column(s) %s from result.",
                derived_required_only_cols,
            )

    # ------------------------------------------------------------------
    # Step 4b — Deduplicate
    # ------------------------------------------------------------------
    # Pricesheets split across multiple files may contain overlapping rows
    # (e.g. the same meter appearing in two monthly export shards).
    # Drop exact duplicates before returning / caching.

    before = len(df)
    df = df.drop_duplicates(ignore_index=True)
    dropped = before - len(df)
    if dropped:
        logging.info("Dropped %d duplicate row(s) after concat.", dropped)

    # Drop columns that were loaded only for filtering (not part of requested output).
    if filter_only_cols:
        df = df.drop(columns=filter_only_cols)
        logging.debug("Dropped filter-only column(s) %s from result.", filter_only_cols)

    logging.info(
        "Returned %d rows in %.3fs — %d file(s) loaded as %s/%s.",
        len(df), time.perf_counter() - _t0, len(source_files), actual_source_format, actual_contract_type,
    )

    if cache_path_directory is not None:
        os.makedirs(cache_path_directory, exist_ok=True)
        # Persist metadata into the cached parquet's schema. Skip None-valued
        # fields so the cache stays minimal and forward-compatible.
        # Cache is written with canonical column names BEFORE the output
        # rename, so different user casings of the same logical query share
        # a single cache entry.
        custom_metadata = {k: v for k, v in asdict(metadata).items() if v is not None}
        write_parquet_with_metadata(df, cache_file, custom_metadata)
        logging.info("Cache written: %s", cache_file)

    # Final step: restore the user's column casing on the returned DataFrame.
    if output_rename_map:
        df = df.rename(columns=output_rename_map)

    if return_metadata:
        return df, metadata
    return df


# ---------------------------------------------------------------------------
# Pricesheet hygiene helpers
# ---------------------------------------------------------------------------
# Canonical names of the per-row price columns. Excluded from the natural
# dedup key so that two rows differing only in their price values (i.e.
# an intra-period price change) collapse into a single representative row.
_PRICE_COLUMN_CANONICALS: tuple[str, ...] = ('UnitPrice', 'BasePrice', 'MarketPrice')


def deduplicate_prices_by_effective_date(
    df: pd.DataFrame,
    *,
    keep: Literal['first', 'last'] = 'first',
) -> pd.DataFrame:
    """
    Collapse rows that share the same natural key but differ in their
    ``EffectiveStartDate``, keeping a single representative per group.

    Use case: a pricesheet export may contain several rows for the same
    meter/SKU/term tuple because Microsoft re-published the price during
    the billing period. Joining usage against such a frame would multiply
    matches. This helper deduplicates to one row per natural-key tuple,
    picking either the earliest (``keep='first'``) or latest
    (``keep='last'``) ``EffectiveStartDate``.

    Auto-discovery (no column parameters required):
      - The effective-start-date column is resolved against ``df.columns``
        using the same exact > synonym > case-insensitive mechanism as
        :func:`load_pricesheet` (canonical name ``EffectiveStartDate``).
        Raises ``KeyError`` if no equivalent column is present.
      - The price columns (``UnitPrice``, ``BasePrice``, ``MarketPrice``)
        are resolved the same way; each is excluded from the natural key
        if (and only if) it is present in ``df.columns``.
      - The natural key is every other column in ``df.columns``.

    Parameters
    ----------
    df : pandas.DataFrame
        Pricesheet frame as returned by :func:`load_pricesheet` (any column
        casing supported by the loader's synonym resolution).
    keep : {'first', 'last'}, default 'first'
        ``'first'`` keeps the row with the earliest ``EffectiveStartDate``,
        ``'last'`` the row with the latest.

    Returns
    -------
    pandas.DataFrame
        A frame with the same columns as ``df`` and one row per natural-key
        tuple. Row order is otherwise preserved (stable sort).
    """
    if keep not in ('first', 'last'):
        raise ValueError(f"keep must be 'first' or 'last', got {keep!r}")

    # Merge pricesheet synonyms (both contracts) for forward-compatibility.
    synonyms = {**EA_COLUMN_SYNONYMS, **MCA_COLUMN_SYNONYMS}
    detected = list(df.columns)

    date_col = resolve_column_name('EffectiveStartDate', detected, synonyms)
    if date_col is None:
        raise KeyError(
            "deduplicate_prices_by_effective_date: no 'EffectiveStartDate' "
            f"(or equivalent) column found in df.columns={detected!r}."
        )

    price_cols: list[str] = []
    for canonical in _PRICE_COLUMN_CANONICALS:
        match = resolve_column_name(canonical, detected, synonyms)
        if match is not None:
            price_cols.append(match)

    excluded = {date_col, *price_cols}
    key_cols = [c for c in detected if c not in excluded]
    if not key_cols:
        raise ValueError(
            "deduplicate_prices_by_effective_date: no columns remain for the "
            f"natural key after excluding {sorted(excluded)!r}."
        )

    rows_before = len(df)
    # Sort so that drop_duplicates(keep='first') yields the desired extremum.
    sorted_df = df.sort_values(date_col, ascending=(keep == 'first'), kind='stable')
    out = sorted_df.drop_duplicates(subset=key_cols, keep='first')
    rows_after = len(out)
    collapsed = rows_before - rows_after

    logging.info(
        "deduplicate_prices_by_effective_date: %d -> %d rows "
        "(%d duplicates collapsed by '%s' on %r; key=%d cols, ignored=%r).",
        rows_before, rows_after, collapsed, keep, date_col,
        len(key_cols), sorted(excluded),
    )
    return out.reset_index(drop=True)


def drop_dev_test_pricing(
    df: pd.DataFrame,
    *,
    contract_type: Literal['ea', 'mca'],
) -> pd.DataFrame:
    """
    Remove Dev/Test (a.k.a. non-production) prices from a pricesheet frame.

    Behaviour is contract-aware (logic hard-coded; no dispatch table):

    - **EA**: the ``OfferId`` column distinguishes Dev/Test offers from the
      production EA offer ``MS-AZR-0017P``. We apply the legacy allowlist:
      keep only rows where ``OfferId == 'MS-AZR-0017P'`` OR ``OfferId`` is
      null/blank (empty/whitespace-only strings are treated as null —
      newer SKUs such as Microsoft Fabric Capacity Reservation carry no
      offer code and must not be dropped as Dev/Test). Note that
      ``MS-AZR-0017P`` is the **production** offer; Dev/Test rows
      (e.g. ``MS-AZR-0148P``) are dropped as a side-effect of this
      allowlist. If ``OfferId`` (or a known synonym) is absent from
      ``df.columns``, a :class:`KeyError` is raised — this is a hard
      requirement for EA frames.

    - **MCA**: today's MCA pricesheets do not expose a Dev/Test
      discriminator column, even though they silently contain both
      Azure-Plan (production) and Dev/Test prices for the same meter.
      We therefore apply **Policy A**: collapse natural-key duplicate
      groups by keeping the row with the highest ``UnitPrice`` (the
      Azure-Plan / production price). This over-prices Dev/Test workloads,
      which is safe for a region-cost simulation.

      This helper is intended to run **after**
      :func:`deduplicate_prices_by_effective_date`, so ``EffectiveStartDate``
      is constant within each remaining natural-key dup group and does not
      need special handling.

    The Policy A natural key is built the same way as in
    :func:`deduplicate_prices_by_effective_date`: every column in
    ``df.columns`` *except* the resolved price columns
    (``UnitPrice`` / ``BasePrice`` / ``MarketPrice``). ``UnitPrice`` (or a
    known synonym) must be present in ``df.columns`` — :class:`KeyError`
    otherwise.

    Parameters
    ----------
    df : pandas.DataFrame
        Pricesheet frame as returned by :func:`load_pricesheet`.
    contract_type : {'ea', 'mca'}
        Drives which branch above is applied.

    Returns
    -------
    pandas.DataFrame
        A frame with the same columns as ``df`` and Dev/Test prices removed
        (or collapsed, for MCA).
    """
    if contract_type not in ('ea', 'mca'):
        raise ValueError(
            f"contract_type must be 'ea' or 'mca', got {contract_type!r}"
        )

    synonyms = {**EA_COLUMN_SYNONYMS, **MCA_COLUMN_SYNONYMS}
    detected = list(df.columns)

    if contract_type == 'ea':
        offer_col = resolve_column_name('OfferId', detected, synonyms)
        if offer_col is None:
            raise KeyError(
                "drop_dev_test_pricing: EA pricesheet is missing the 'OfferId' "
                f"(or equivalent) column in df.columns={detected!r}. "
                "Add 'offerId' to the requested pricesheet columns."
            )
        offer_vals = df[offer_col]
        is_blank = offer_vals.isna() | (offer_vals.astype(str).str.strip() == '')
        mask = (offer_vals == 'MS-AZR-0017P') | is_blank
        rows_before = len(df)
        out = df.loc[mask].reset_index(drop=True)
        rows_after = len(out)
        logging.info(
            "drop_dev_test_pricing[ea]: %d -> %d rows "
            "(%d Dev/Test or non-prod offer rows dropped via allowlist "
            "on %r == 'MS-AZR-0017P' | isna()).",
            rows_before, rows_after, rows_before - rows_after, offer_col,
        )
        return out

    # MCA: Policy A — collapse natural-key dup groups, keep highest UnitPrice.
    price_cols: list[str] = []
    unit_price_col: str | None = None
    for canonical in _PRICE_COLUMN_CANONICALS:
        match = resolve_column_name(canonical, detected, synonyms)
        if match is not None:
            price_cols.append(match)
            if canonical == 'UnitPrice':
                unit_price_col = match
    if unit_price_col is None:
        raise KeyError(
            "drop_dev_test_pricing: MCA pricesheet is missing the 'UnitPrice' "
            f"(or equivalent) column in df.columns={detected!r}; required for "
            "Policy A natural-key dedup."
        )

    excluded = set(price_cols)
    key_cols = [c for c in detected if c not in excluded]
    if not key_cols:
        raise ValueError(
            "drop_dev_test_pricing: no columns remain for the natural key "
            f"after excluding price columns {sorted(excluded)!r}."
        )

    rows_before = len(df)
    # Most rows are unique on the natural key; only sort/dedup the duplicate
    # subset to avoid an O(N log N) sort over the full frame.
    dup_mask = df.duplicated(subset=key_cols, keep=False)
    if dup_mask.any():
        uniques = df.loc[~dup_mask]
        dups = df.loc[dup_mask]
        resolved = (
            dups.sort_values(unit_price_col, ascending=False, kind='stable')
                .drop_duplicates(subset=key_cols, keep='first')
        )
        out = pd.concat([uniques, resolved], ignore_index=True)
    else:
        out = df.reset_index(drop=True)
    rows_after = len(out)
    logging.info(
        "drop_dev_test_pricing[mca]: %d -> %d rows "
        "(%d duplicates collapsed via Policy A — kept highest %r per natural "
        "key; key=%d cols, ignored=%r).",
        rows_before, rows_after, rows_before - rows_after, unit_price_col,
        len(key_cols), sorted(excluded),
    )
    return out


def expand_included_quantity_to_tier_rows(
    df: pd.DataFrame,
    *,
    contract_type: Literal['ea', 'mca'],
) -> pd.DataFrame:
    """
    EA-only: convert the EA ``IncludedQuantity`` column into MCA-style
    ``TierMinimumUnits`` tier rows, so EA and MCA pricesheets share a single
    tiered-pricing representation.

    Why
    ---
    EA encodes tiered pricing inline: a single row carries an
    ``IncludedQuantity`` (the number of units billed for free) plus a
    ``UnitPrice`` (the per-unit price beyond that free allowance).

    MCA encodes the same idea with multiple rows per meter and a
    ``TierMinimumUnits`` column: one row per pricing tier, applied to a
    quantity ``Q`` by selecting the row with the highest ``TierMinimumUnits``
    that is still ``<= Q``.

    To unify the downstream pricing logic against the MCA shape, this
    helper expands each EA row with ``IncludedQuantity > 0`` into two rows:

    - **Original (over-tier)**: ``TierMinimumUnits = IncludedQuantity``,
      ``UnitPrice`` unchanged (the price for units beyond the free tier).
    - **New (free tier)**: ``TierMinimumUnits = 0``, ``UnitPrice = 0``
      (the free allowance from unit 0 up to ``IncludedQuantity - 1``).

    Rows with ``IncludedQuantity == 0`` (or NaN) only receive
    ``TierMinimumUnits = IncludedQuantity`` (effectively 0/NaN) — no duplicate
    is emitted. The ``IncludedQuantity`` column is dropped from the output.

    Matches the legacy semantics in
    ``region_strategy/region_cost_simulation_legacy.py`` (``TierMinimumUnits``
    expansion block): only ``UnitPrice`` is zeroed on the free-tier row;
    ``MarketPrice`` / ``BasePrice`` are left untouched.

    Behaviour by contract:

    - **EA**: applies the expansion described above. Raises :class:`KeyError`
      if ``IncludedQuantity`` (or a known synonym) or ``UnitPrice`` is missing.
      Also raises if a ``TierMinimumUnits``-like column already exists
      (refuses to overwrite).
    - **MCA**: no-op — MCA pricesheets already expose ``TierMinimumUnits``
      directly.

    Parameters
    ----------
    df : pandas.DataFrame
        Pricesheet frame as returned by :func:`load_pricesheet`.
    contract_type : {'ea', 'mca'}
        Drives which branch above is applied.

    Returns
    -------
    pandas.DataFrame
        Frame with ``IncludedQuantity`` replaced by ``TierMinimumUnits`` (EA)
        or unchanged (MCA).
    """
    if contract_type not in ('ea', 'mca'):
        raise ValueError(
            f"contract_type must be 'ea' or 'mca', got {contract_type!r}"
        )

    if contract_type == 'mca':
        logging.info(
            "expand_included_quantity_to_tier_rows[mca]: no-op "
            "(MCA pricesheets already encode tiers via 'TierMinimumUnits')."
        )
        return df

    # EA-only past this point.
    detected = list(df.columns)

    inc_col = resolve_column_name('IncludedQuantity', detected, EA_COLUMN_SYNONYMS)
    if inc_col is None:
        raise KeyError(
            "expand_included_quantity_to_tier_rows: EA pricesheet is missing "
            f"the 'IncludedQuantity' (or equivalent) column in "
            f"df.columns={detected!r}. Add 'includedQuantity' to the "
            "requested pricesheet columns."
        )
    up_col = resolve_column_name('UnitPrice', detected, EA_COLUMN_SYNONYMS)
    if up_col is None:
        raise KeyError(
            "expand_included_quantity_to_tier_rows: pricesheet is missing the "
            f"'UnitPrice' (or equivalent) column in df.columns={detected!r}."
        )

    existing_tier = resolve_column_name('TierMinimumUnits', detected, EA_COLUMN_SYNONYMS)
    if existing_tier is not None:
        raise ValueError(
            "expand_included_quantity_to_tier_rows: a 'TierMinimumUnits'-like "
            f"column ({existing_tier!r}) already exists in the EA frame; "
            "refusing to overwrite."
        )

    # Output column name: match the casing convention of IncludedQuantity
    # (camelCase post-loader for camelCase requests, PascalCase otherwise).
    tier_col = 'tierMinimumUnits' if inc_col[:1].islower() else 'TierMinimumUnits'

    rows_before = len(df)

    out = df.copy()
    out[tier_col] = out[inc_col]

    over_tier_mask = out[inc_col].fillna(0) != 0
    free_tier = out.loc[over_tier_mask].copy()
    free_tier[up_col] = 0
    free_tier[tier_col] = 0

    out = pd.concat([out, free_tier], ignore_index=True)
    out = out.drop(columns=[inc_col])

    rows_after = len(out)
    logging.info(
        "expand_included_quantity_to_tier_rows[ea]: %d -> %d rows "
        "(%d free-tier rows added; %r dropped, %r added).",
        rows_before, rows_after, rows_after - rows_before, inc_col, tier_col,
    )
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')

    df = load_pricesheet(r'Pricesheet', columns=["MeterCategory","MeterName"], filters={
        "MeterCategory": ["Virtual Machines"],
    }, cache_path_directory=r"C:\_finops_cache_")
