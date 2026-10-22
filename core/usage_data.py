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
    resolve_source_files,
    write_parquet_with_metadata,
)


# Columns whose string values are lowercased after load for canonical casing
# (Azure exports occasionally emit mixed case for these identifiers — e.g.
# 'NORTHEUROPE' vs 'northeurope' on Microsoft Fabric usage rows). The
# effective intersection with the loaded columns is included in the cache
# key so edits to this list invalidate only caches that actually touched the
# affected columns.
_LOWERCASE_NORMALIZED_COLUMNS: tuple[str, ...] = (
    'ResourceId',
    'ResourceLocation',
    'ResourceGroupName',
    'SubscriptionId',
)


# ---------------------------------------------------------------------------
# Metadata returned alongside the DataFrame when return_metadata=True.
# All fields are optional; missing entries (e.g. on cache hits produced by an
# older version of this module) are populated as None.
# ---------------------------------------------------------------------------
@dataclass
class UsageMetadata:
    contract_type: str | None = None        # 'ea' or 'mca'
    source_files: list[str] | None = None
    usage_start_date: pd.Timestamp | None = None
    usage_end_date: pd.Timestamp | None = None


# Per-format date column(s) used by compute_date_range to derive
# usage_start_date / usage_end_date directly from the source files
# (parquet footer statistics, or a single-column CSV scan).
# MCA placeholder — leave empty until the MCA schema is confirmed.
EA_USAGE_DATE_COLUMNS: list[str] = ['Date']
MCA_USAGE_DATE_COLUMNS: list[str] = ['date']


# Sentinel value used in *_COLUMN_DTYPES to flag date columns.
# These are routed to parse_dates= instead of dtype= in pd.read_csv,
# because pandas does not allow a column to appear in both arguments simultaneously.
_PARSE_DATE_SENTINEL = 'parse_date'

# ---------------------------------------------------------------------------
# EA (Enterprise Agreement) column schema
# ---------------------------------------------------------------------------
# Maps every EA usage detail column to its target pandas dtype, or to
# _PARSE_DATE_SENTINEL for columns that must be parsed as dates.
# EA_DATE_FORMAT is the strptime format string for those date columns.
EA_COLUMN_DTYPES: dict = {
    # --- high-cardinality identifiers: kept as str ---
    'BillingAccountId':       'str',
    'BillingAccountName':     'str',
    'BillingPeriodStartDate': _PARSE_DATE_SENTINEL,
    'BillingPeriodEndDate':   _PARSE_DATE_SENTINEL,
    'BillingProfileId':       'str',
    'BillingProfileName':     'str',
    'AccountOwnerId':         'str',
    'AccountName':            'str',
    'SubscriptionId':         'str',
    'SubscriptionName':       'str',
    'Date':                   _PARSE_DATE_SENTINEL,
    'Product':                'str',
    'PartNumber':             'str',
    'MeterId':                'str',
    # --- low/medium-cardinality dimensions: stored as category to save memory ---
    'ServiceFamily':          'category',
    'MeterCategory':          'category',
    'MeterSubCategory':       'category',
    'MeterRegion':            'category',
    'MeterName':              'str',
    # --- numeric measures ---
    'Quantity':               pd.Float64Dtype(),
    'EffectivePrice':         pd.Float64Dtype(),
    'Cost':                   pd.Float64Dtype(),
    'UnitPrice':              pd.Float64Dtype(),
    # --- low-cardinality codes and enumerations ---
    'BillingCurrency':        'category',
    'ResourceLocation':       'category',
    'AvailabilityZone':       'category',
    'ConsumedService':        'category',
    # --- high-cardinality resource identifiers: kept as str ---
    'ResourceId':             'str',
    'ResourceName':           'str',
    'ServiceInfo1':           'str',
    'ServiceInfo2':           'str',
    'AdditionalInfo':         'str',
    'Tags':                   'str',
    'InvoiceSectionId':       'str',
    'InvoiceSection':         'str',
    'CostCenter':             'str',
    # --- low-cardinality codes and enumerations (continued) ---
    'UnitOfMeasure':          'category',
    # --- high-cardinality resource identifiers (continued) ---
    'ResourceGroup':          'str',
    'ReservationId':          'str',
    'ReservationName':        'str',
    'ProductOrderId':         'str',
    'ProductOrderName':       'str',
    'OfferId':                'str',
    # --- low-cardinality codes and enumerations (continued) ---
    'IsAzureCreditEligible':  'category',
    'Term':                   'category',
    'PublisherName':          'str',
    'PlanName':               'str',
    'ChargeType':             'category',
    'Frequency':              'category',
    'PublisherType':          'category',
    'PayGPrice':              pd.Float64Dtype(),
    'PricingModel':           'category',
    'CostAllocationRuleName': 'str',
    'benefitId':              'str',
    'benefitName':            'str',
}
EA_DATE_FORMAT: str = "%m/%d/%Y"

# ---------------------------------------------------------------------------
# MCA (Microsoft Customer Agreement) column schema
# ---------------------------------------------------------------------------
# Maps every MCA usage detail column to its target pandas dtype, or to
# _PARSE_DATE_SENTINEL for columns that must be parsed as dates.
# Dtype choices mirror EA_COLUMN_DTYPES for equivalent columns.
MCA_COLUMN_DTYPES: dict = {
    # --- invoice / billing account identifiers (high-cardinality: str) ---
    'InvoiceId':                    'category',
    'BillingAccountId':             'category',
    'BillingAccountName':           'category',
    'PreviousInvoiceId':            'category',
    'BillingProfileId':             'category',
    'BillingProfileName':           'category',
    'InvoiceSectionId':             'category',
    'InvoiceSectionName':           'category',
    # --- partner / reseller / customer identifiers ---
    'PartnerTenantId':              'str',
    'PartnerName':                  'str',
    'ResellerName':                 'str',
    'ResellerMpnId':                'str',
    'CustomerTenantId':             'category',
    'CustomerName':                 'category',
    'CostCenter':                   'category',
    # --- date columns ---
    'BillingPeriodStartDate':       _PARSE_DATE_SENTINEL,
    'BillingPeriodEndDate':         _PARSE_DATE_SENTINEL,
    'ServicePeriodStartDate':       _PARSE_DATE_SENTINEL,
    'ServicePeriodEndDate':         _PARSE_DATE_SENTINEL,
    'Date':                         _PARSE_DATE_SENTINEL,
    # --- low/medium-cardinality dimensions: category ---
    'ServiceFamily':                'category',
    # --- product order identifiers ---
    'ProductOrderId':               'str',
    'ProductOrderName':             'str',
    'ConsumedService':              'category',
    # --- meter identifiers ---
    'MeterId':                      'str',
    'MeterName':                    'str',
    'MeterCategory':                'category',
    'MeterSubCategory':             'category',
    'MeterRegion':                  'category',
    # --- product identifiers ---
    'ProductId':                    'str',
    'Product':                      'str',
    # --- subscription identifiers ---
    'SubscriptionId':               'str',
    'SubscriptionName':             'str',
    # --- publisher info ---
    'PublisherType':                'category',
    'PublisherId':                  'str',
    'PublisherName':                'str',
    # --- resource identifiers (high-cardinality: str) ---
    'ResourceGroupName':            'str',
    'ResourceId':                   'str',
    'ResourceLocation':             'category',
    'Location':                     'category',
    # --- numeric measures ---
    'EffectivePrice':               pd.Float64Dtype(),
    'Quantity':                     pd.Float64Dtype(),
    'UnitOfMeasure':                'category',
    'ChargeType':                   'category',
    # --- currency / cost columns ---
    'BillingCurrency':              'category',
    'PricingCurrency':              'category',
    'CostInBillingCurrency':        pd.Float64Dtype(),
    'CostInPricingCurrency':        pd.Float64Dtype(),
    'CostInUsd':                    pd.Float64Dtype(),
    'PaygCostInBillingCurrency':    pd.Float64Dtype(),
    'PaygCostInUsd':                pd.Float64Dtype(),
    'ExchangeRatePricingToBilling': pd.Float64Dtype(),
    'ExchangeRateDate':             _PARSE_DATE_SENTINEL,
    # --- low-cardinality flags / enumerations ---
    'IsAzureCreditEligible':        'category',
    # --- free-form service info / tags ---
    'ServiceInfo1':                 'str',
    'ServiceInfo2':                 'str',
    'AdditionalInfo':               'str',
    'Tags':                         'str',
    # --- partner earned credit ---
    'PartnerEarnedCreditRate':      pd.Float64Dtype(),
    'PartnerEarnedCreditApplied':   pd.Float64Dtype(),
    # --- pricing fields ---
    'PayGPrice':                    pd.Float64Dtype(),
    'Frequency':                    'category',
    'Term':                         'category',
    # --- reservation / benefit identifiers ---
    'ReservationId':                'str',
    'ReservationName':              'str',
    'PricingModel':                 'category',
    'UnitPrice':                    pd.Float64Dtype(),
    'CostAllocationRuleName':       'str',
    'Provider':                     'category',
    'BenefitId':                    'str',
    'BenefitName':                  'str',
}
MCA_DATE_FORMAT: str    = "%m/%d/%Y"

# ---------------------------------------------------------------------------
# EA column synonyms — maps each canonical column name to its known aliases
# found in various source formats (e.g. Parquet exports vs CSV exports).
# ---------------------------------------------------------------------------
EA_COLUMN_SYNONYMS: dict[str, list[str]] = {
    'Cost':     ['CostInBillingCurrency', 'CostInPricingCurrency'], # In case user request MCA col names
    'ResourceGroup': ['ResourceGroupName'], # In case user request MCA col name
    'BillingCurrency': ['PricingCurrency'], # EA bills in a single currency; treat MCA's PricingCurrency request as equivalent
}
# MCA placeholder — TODO: populate once MCA column synonyms are confirmed.
MCA_COLUMN_SYNONYMS: dict[str, list[str]] = {
    'CostInBillingCurrency': ['Cost'], # In case user request EA col names
    'ResourceGroupName': ['ResourceGroup'], # In case user request EA col name
    'Product': ['ProductName'],
}

# ---------------------------------------------------------------------------
# Aggregatable columns — only these may be passed as aggregate= to
# load_usage_details. All aggregations use SUM.
# ---------------------------------------------------------------------------
EA_AGGREGATABLE_COLUMNS: list[str] = ['Quantity', 'Cost', 'PayGPrice']
# MCA placeholder — TODO: populate once MCA column layout is confirmed.
MCA_AGGREGATABLE_COLUMNS: list[str] = ['Quantity','CostInBillingCurrency', 'CostInPricingCurrency', 'CostInUsd', 'PaygCostInBillingCurrency', 'PaygCostInUsd', 'PayGPrice']

# ---------------------------------------------------------------------------
# Derived column definitions — format-agnostic computed columns that can be
# requested via the derived_columns= parameter of load_usage_details().
#
# Each entry maps a derived column name to a dict with:
#   'required_cols' : list[str]  — source columns that must be loaded from the
#                                   file (force-loaded if not in columns=, then
#                                   dropped after computation if not requested).
#   'compute'       : Callable[[pd.DataFrame], pd.Series]
#                                 — receives the concatenated DataFrame and
#                                   returns a Series for the new column.
#                                   Called after concat, before final groupby.
# ---------------------------------------------------------------------------
def _compute_term_pricesheet(df: pd.DataFrame) -> pd.Series:
    # Mirrors the legacy ``pricesheet_term_to_usage_details_term`` mapping in
    # region_cost_simulation_legacy.py (lines 81-85), inverted.
    _TERM_MAP = {
        '1Month': 'P1M',
        '1Year':  'P1Y',
        '3Years': 'P3Y',
        '5Years': 'P5Y',
        '10Years': 'P10Y'
    }
    return df['Term'].astype(str).map(_TERM_MAP).fillna('').astype('category')


def _compute_pricing_model_pricesheet(df: pd.DataFrame) -> pd.Series:
    # ``Spot`` is intentionally not mapped: spot pricing has no row in the
    # pricesheet (see region_cost_simulation_legacy.py line 115, which skips
    # Spot rows before joining to the pricesheet). Spot rows therefore yield
    # NaN here and can be filtered out downstream.
    _PRICING_MODEL_MAP = {
        'OnDemand':    'Consumption',
        'Reservation': 'ReservedInstance',
        'SavingsPlan': 'SavingsPlan'
    }
    return df['PricingModel'].astype(str).map(_PRICING_MODEL_MAP).astype('category')

def _compute_resource_provider(df: pd.DataFrame) -> pd.Series:
    # returns Microsoft.Compute for a ResourceId like /subscriptions/.../resourceGroups/.../providers/Microsoft.Compute/virtualMachines/...
    return df['ResourceId'].str.split('/providers/').str[1].str.split('/').str[0].astype('category')

def _compute_resource_type(df: pd.DataFrame) -> pd.Series:
    # returns Microsoft.Compute/virtualMachines for a ResourceId like /subscriptions/.../resourceGroups/.../providers/Microsoft.Compute/virtualMachines/...
    after_providers = df['ResourceId'].str.split('/providers/').str[1].str.split('/')
    return (after_providers.str[0] + '/' + after_providers.str[1]).astype('category')

def _compute_resource_name(df: pd.DataFrame) -> pd.Series:
    # returns the last segment of the ResourceId, which is often the most human-readable identifier available for a resource
    return df['ResourceId'].str.split('/').str[-1].str.lower()

def _compute_region_neutral_product(df: pd.DataFrame) -> pd.Series:
    # Strips the MeterRegion substring (when non-empty) from Product, then trims trailing
    # spaces / hyphens. Rows with NaN/empty MeterRegion are passed through as Product.
    product = df['Product'].astype('string').fillna('')
    region  = df['MeterRegion'].astype('string').fillna('')
    out = pd.Series(
        [p.replace(r, '') if r else p for p, r in zip(product, region)],
        index=df.index, dtype='string',
    )
    return out.str.rstrip(' -')

DERIVED_COLUMN_DEFINITIONS: dict[str, dict] = {
    'TermPricesheet': {
        'required_cols': ['Term'],
        'compute':       _compute_term_pricesheet,
    },
    'PricingModelPricesheet': {
        'required_cols': ['PricingModel'],
        'compute':       _compute_pricing_model_pricesheet,
    },
    'ResourceProvider': {
        'required_cols': ['ResourceId'],
        'compute':       _compute_resource_provider,
    },
    'ResourceType': {
        'required_cols': ['ResourceId'],
        'compute':       _compute_resource_type,
    },
    'ResourceName': {
        'required_cols': ['ResourceId'],
        'compute':       _compute_resource_name,
    },
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
    aggregate: list[str] | None,
    filters: dict[str, ColumnFilter] | None,
    derived_columns: list[str] | None,
    deduplicate: bool | list[str] = False,
    lowercased_columns: list[str] | None = None,
) -> str:
    """
    Compute a SHA-256 cache key from the resolved load parameters.

    The key is derived from:
    - The absolute path and byte size of every resolved source file (sorted).
    - The resolved source and data format names.
    - The sorted column selection list (None if not specified).
    - The sorted aggregate column list (None if not specified).
    - The filters dict serialized via :func:`serialize_for_cache_key` so the
      operator (``in`` / ``iin`` / etc.) participates in the hash.
    - The sorted derived_columns list (None if not specified).
    - The deduplicate setting, only when truthy (omitted when False to keep
      the cache hash stable for callers that don't use this feature).
    - The list of columns actually lowercased post-load (intersection of
      :data:`_LOWERCASE_NORMALIZED_COLUMNS` with the effective loaded
      columns); only included when non-empty so adding the field doesn't
      invalidate every existing cache when the list happens to be empty.

    File sizes are used instead of content hashes for performance; a size
    collision (file replaced with one of identical size) is acceptable given
    the typical size of Azure usage CSVs.
    """
    file_entries = sorted(
        [os.path.abspath(f), os.path.getsize(f)]
        for f in source_files
    )
    payload = {
        'cache_version':   2,  # bump to invalidate caches when load semantics change
        'files':           file_entries,
        'source_format':   actual_source_format,
        'contract_type':   actual_contract_type,
        'columns':         sorted(columns)         if columns         is not None else None,
        'aggregate':       sorted(aggregate)       if aggregate       is not None else None,
        'filters':         _serialize_filters_for_cache(filters),
        'derived_columns': sorted(derived_columns) if derived_columns is not None else None,
    }
    # Only include `deduplicate` in the payload when truthy, so existing
    # callers that don't pass this argument keep producing the same cache key.
    if deduplicate:
        payload['deduplicate'] = (
            '__all__' if deduplicate is True else sorted(deduplicate)
        )
    if lowercased_columns:
        payload['lowercased_columns'] = sorted(lowercased_columns)
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode()).hexdigest()


# Markers used to disambiguate EA vs MCA usage-detail files from their column
# headers (see :func:`commons.detect_contract_type`).
_USAGE_EA_MARKER  = 'Cost'
_USAGE_MCA_MARKER = 'costInPricingCurrency'


def get_contract_type(
    path: str, source_format: str = 'auto'
) -> Literal['ea', 'mca', 'unknown']:
    """
    Detect the Azure billing contract type of usage-detail files at ``path``.

    Resolves ``path`` (single file, directory, or glob) the same way
    :func:`load_usage_details` does, then inspects only the column headers
    of the first file to decide between EA and MCA.

    Parameters
    ----------
    path : str
        Path to a single file, a directory, or a glob pattern.
    source_format : str
        ``'csv'``, ``'parquet'``, or ``'auto'`` (default).

    Returns
    -------
    Literal['ea', 'mca', 'unknown']
        ``'ea'`` if only ``'Cost'`` is present in the headers, ``'mca'`` if only
        ``'costInPricingCurrency'`` is present, ``'unknown'`` if both or neither
        are present.

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
        source_files, actual_source_format, _USAGE_EA_MARKER, _USAGE_MCA_MARKER,
    )


def load_usage_details(path: str, source_format: str = 'auto', contract_type: str = 'auto',
                       columns: list[str] = None, aggregate: list[str] = None,
                       filters: dict[str, 'ColumnFilter | list'] = None,
                       derived_columns: list[str] = None,
                       deduplicate: bool | list[str] = False,
                       cache_path_directory: str = DEFAULT_CACHE_PATH_DIRECTORY,
                       return_metadata: bool = False):
    """
    Load Azure usage details from one or more CSV or Parquet files into a single DataFrame.

    Parameters
    ----------
    path : str
        Path to a single file, a directory, or a glob pattern (e.g. 'dir/usage_*.csv').
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
        _PARSE_DATE_SENTINEL are routed to parse_dates= instead of dtype=.
        For Parquet files, column selection is applied but stored types are preserved
        (dtype maps are intentionally ignored).
    aggregate : list[str], optional
        Columns to aggregate by summing (e.g. ['Cost', 'Quantity']). None (default)
        disables aggregation. Only columns listed in EA_AGGREGATABLE_COLUMNS /
        MCA_AGGREGATABLE_COLUMNS are accepted; passing any other column raises ValueError.
        If a column in aggregate is absent from columns=, it is automatically added to
        the set of columns to load (no error is raised).
        The group-by dimensions are all loaded columns not present in this list.
        When multiple files are loaded, aggregation runs per-file first (to reduce
        memory before concat), then once more after concat to merge cross-file duplicates.
        For a single file, aggregation runs only once.
    filters : dict[str, ColumnFilter | list], optional
        Row filters to apply, e.g. {'MeterCategory': ['Virtual Machines'], 'SubscriptionId': ['abc']}.
        None (default) loads all rows. Multiple entries are combined with AND semantics.

        Each value may be either a bare sequence (treated as case-sensitive ``in``)
        or a :class:`ColumnFilter` carrying an explicit operator
        (``'in'`` / ``'not_in'`` / ``'iin'`` / ``'not_iin'``). The ``i*`` variants
        are case-insensitive string matches.

        For Parquet files, ``in`` / ``not_in`` filters are pushed down to the
        row-group level via pyarrow predicate pushdown, so unmatched row groups
        are never read from disk. ``iin`` / ``not_iin`` cannot be pushed down
        (parquet has no case-insensitive predicate) and are applied in memory
        after read; this is logged at INFO so the cost is visible.
        For CSV files, the full file is loaded then every filter is applied in
        memory.
        If a filter column is absent from columns=, it is loaded temporarily and
        dropped from the result after filtering and aggregation.

        Example:
            filters={
                'publisherName':    ['Microsoft'],                              # pushdown
                'resourceLocation': ColumnFilter(['northeurope'], op='iin'),    # post-load
            }
    derived_columns : list[str], optional
        Names of columns to compute from other source columns after loading and
        concatenation but before the final groupby aggregation. None (default) adds
        no derived columns. Each name must be a key in DERIVED_COLUMN_DEFINITIONS;
        passing an unknown name raises ValueError. The source columns required to
        compute each derived column are force-loaded even if absent from columns=,
        then dropped from the result after computation (unless the user also requested
        them in columns=). Derived columns are NOT aggregatable and cannot appear in
        aggregate=. They participate as groupby dimensions when aggregate= is set.
    deduplicate : bool or list[str], optional
        Drop duplicate rows from the result before caching and returning.
        False (default) disables deduplication. True drops exact duplicates across
        all final output columns (df.drop_duplicates()). A list[str] drops duplicates
        over that subset of columns; the first occurrence is kept (keep='first').
        Subset names are resolved against the canonical schema (case-insensitive +
        synonyms) like every other column input, and must all be present in the
        final output (post filter-only drop) — otherwise ValueError is raised.
        Deduplication runs after the final aggregate= groupby, so when both are
        set the dedup operates on the post-aggregation shape (typically a no-op).
        The cache key includes this setting only when truthy, so callers that
        omit deduplicate= keep their existing cache hashes.
    cache_path_directory : str, optional
        Path to a directory used for caching results. None (default) disables caching.
        On each call a SHA-256 cache key is computed from the resolved source files
        (absolute path + size), the resolved source/data formats, and the final
        columns/aggregate/filters. If a matching Parquet file already exists in the
        directory it is returned immediately (cache hit). On a cache miss the data is
        loaded normally and the result is written to the cache directory as
        'usage_details_<hash>.parquet' before being returned. The directory is created
        automatically on the first cache write.
    return_metadata : bool, optional
        When True (default False), returns a tuple ``(df, metadata)`` instead of just
        ``df``. ``metadata`` is a :class:`UsageMetadata` dataclass exposing
        ``contract_type`` ('ea' / 'mca') and ``source_files`` (sorted absolute paths).
        On a cache hit the metadata is hydrated from the cached parquet's custom
        schema metadata; fields absent from older cache files resolve to ``None``.

    Returns
    -------
    pd.DataFrame or tuple[pd.DataFrame, UsageMetadata]
        Concatenated DataFrame of all matched files, with a uniform column schema.
        When ``return_metadata=True``, returns a 2-tuple of (DataFrame, metadata).

    Raises
    ------
    FileNotFoundError
        If a given file or directory does not exist, or a glob pattern matches nothing.
    ValueError
        If the file extension / detected format is inconsistent with the requested format,
        if mixed file formats are found, if contract-type detection is inconclusive, or if
        aggregate contains columns that are not aggregatable or will not be loaded.
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
    #   EA  => 'Cost' column is present
    #   MCA => 'costInPricingCurrency' column is present
    #
    # • If contract_type is 'auto': set actual_contract_type to the detected type.
    #     If both or neither column is found, raise ValueError (inconclusive).
    # • If contract_type is 'ea' or 'mca': still run detection, then compare.
    #     If the detected type differs from the requested one, log a warning
    #     and continue with actual_contract_type = contract_type (honour caller intent).

    _EA_MARKER  = _USAGE_EA_MARKER
    _MCA_MARKER = _USAGE_MCA_MARKER
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
    # Every name the user provides in columns=, filters= keys, aggregate=, or
    # derived_columns= is resolved against the format's canonical pool via
    # commons.UserColumnResolver (exact > synonym > case-insensitive). The
    # local `columns`, `filters`, `aggregate`, `derived_columns` are rewritten
    # with their canonical names, so every downstream step (cache key,
    # validation, dtype lookup, groupby, derived computations, file-side
    # synonym rename) sees a single canonical vocabulary.
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
    derived_pool = list(DERIVED_COLUMN_DEFINITIONS.keys())

    resolver        = UserColumnResolver(actual_contract_type)
    columns         = resolver.resolve_list(columns,         canonical_pool, user_synonyms_map, 'columns', priority=True)
    filters         = resolver.resolve_dict_keys(filters,    canonical_pool, user_synonyms_map, 'filters')
    aggregate       = resolver.resolve_list(aggregate,       canonical_pool, user_synonyms_map, 'aggregate')
    derived_columns = resolver.resolve_list(derived_columns, derived_pool,   {},                'derived_columns')
    # Coerce filters to a uniform {col: ColumnFilter} shape; legacy bare-list
    # values become ColumnFilter(values, op='in') so all downstream code can
    # assume the ColumnFilter API.
    filters         = _normalize_filters(filters)
    if isinstance(deduplicate, list):
        deduplicate = resolver.resolve_list(
            deduplicate, canonical_pool + derived_pool, user_synonyms_map, 'deduplicate'
        )
    output_rename_map = resolver.finalize()
    if output_rename_map:
        logging.debug(
            "User column casing rename map (canonical → user): %s",
            output_rename_map,
        )

    # ------------------------------------------------------------------
    # Step 2b — Validate aggregate columns against the detected data format
    # ------------------------------------------------------------------
    # If aggregate= is provided:
    #   • All requested columns must belong to the format's aggregatable set
    #     (EA_AGGREGATABLE_COLUMNS or MCA_AGGREGATABLE_COLUMNS); raise ValueError otherwise.
    #   • If a column in aggregate is absent from columns= (explicit column selection),
    #     it is silently added to columns so it gets loaded — no error is raised.
    #     (When columns=None all file columns are loaded, so nothing can be missing.)

    if aggregate is not None:
        valid_agg_cols = set(
            EA_AGGREGATABLE_COLUMNS if actual_contract_type == 'ea' else MCA_AGGREGATABLE_COLUMNS
        )
        invalid_agg = set(aggregate) - valid_agg_cols
        if invalid_agg:
            raise ValueError(
                f"aggregate contains columns not aggregatable for {actual_contract_type!r} format: "
                f"{sorted(invalid_agg)}. Valid aggregatable columns: {sorted(valid_agg_cols)}."
            )
        if columns is not None:
            missing_agg = [c for c in aggregate if c not in columns]
            if missing_agg:
                logging.debug(
                    "Aggregate columns %s not in columns= list; adding them for loading.",
                    missing_agg
                )
                columns = list(columns) + missing_agg

    # ------------------------------------------------------------------
    # Step 2c — Validate derived_columns and identify required source columns
    # ------------------------------------------------------------------
    # derived_required_only_cols: source columns required by derived computations
    # that are NOT already in the user's columns= list.  These are force-loaded,
    # used during computation, then dropped from the result (same pattern as
    # filter_only_cols).  When columns=None all file columns are loaded anyway,
    # so nothing extra is needed.

    derived_required_only_cols: list[str] = []
    if derived_columns is not None:
        unknown = [c for c in derived_columns if c not in DERIVED_COLUMN_DEFINITIONS]
        if unknown:
            raise ValueError(
                f"derived_columns contains unknown column(s): {sorted(unknown)}. "
                f"Available derived columns: {sorted(DERIVED_COLUMN_DEFINITIONS)}."
            )
        if columns is not None:
            all_required_src = [
                src_col
                for dc in derived_columns
                for src_col in DERIVED_COLUMN_DEFINITIONS[dc]['required_cols']
            ]
            derived_required_only_cols = [c for c in all_required_src if c not in columns]
            if derived_required_only_cols:
                logging.debug(
                    "Derived-required columns %s will be loaded temporarily and dropped after computation.",
                    derived_required_only_cols,
                )

    # ------------------------------------------------------------------
    # Step 2d — Determine filter-only columns, build pyarrow filters, cache check
    # ------------------------------------------------------------------
    # filter_only_cols: filter keys absent from the requested output columns.
    # These are loaded temporarily, used for filtering, then dropped from the result
    # (after aggregation, so they don't accidentally participate in the groupby).
    # When columns=None all columns are loaded anyway, so nothing extra is needed.
    #
    # Cache key uses columns AFTER aggregate expansion but BEFORE filter-only /
    # derived-required expansion, so the key reflects the final output shape.

    filter_only_cols = []
    if filters is not None and columns is not None:
        filter_only_cols = [c for c in filters if c not in columns]
        if filter_only_cols:
            logging.debug(
                "Filter-only columns %s will be loaded temporarily and dropped after filtering.",
                filter_only_cols,
            )

    # ------------------------------------------------------------------
    # Step 2e — Validate deduplicate subset against the final output columns
    # ------------------------------------------------------------------
    # When deduplicate is a list, every column in it must appear in the final
    # output of this call (i.e. the columns= selection minus filter-only and
    # derived-required-only columns, plus any derived_columns). When columns
    # is None all canonical columns are loaded so any canonical name is OK.
    if isinstance(deduplicate, list):
        if columns is not None:
            final_output_cols = (
                [c for c in columns
                 if c not in filter_only_cols and c not in derived_required_only_cols]
                + (list(derived_columns) if derived_columns is not None else [])
            )
            missing_dedup = [c for c in deduplicate if c not in final_output_cols]
            if missing_dedup:
                raise ValueError(
                    f"deduplicate contains column(s) not present in the final output: "
                    f"{sorted(missing_dedup)}. Final output columns: {sorted(final_output_cols)}."
                )

    # Build pyarrow DNF filter list for Parquet predicate pushdown.
    # Operators 'in' / 'not_in' are pushed down; 'iin' / 'not_iin' are kept
    # as residual filters applied in memory after read_parquet (parquet has
    # no case-insensitive predicate). For CSV every filter is applied in
    # memory via apply_in_memory below.
    pq_filters, residual_filters = _to_parquet_pushdown(filters)
    if residual_filters and actual_source_format == 'parquet':
        for _rcol, _rspec in residual_filters.items():
            logging.info(
                "filter on %r uses op=%r — no parquet pushdown, "
                "applied in memory after read.",
                _rcol, _rspec.op,
            )

    # Effective loaded columns (used both for the cache-key lowercased-columns
    # intersection and ignored when `columns` is None — in which case the full
    # canonical pool is loaded).
    effective_loaded_cols = list(columns) if columns is not None else list(canonical_pool)
    lowercased_columns    = [
        c for c in _LOWERCASE_NORMALIZED_COLUMNS if c in effective_loaded_cols
    ]

    # Build metadata describing this load. Populated up front so it is
    # available for both cache hits (overwritten from cached parquet metadata)
    # and cache misses (written into the cache parquet file below).
    metadata = UsageMetadata(
        contract_type=actual_contract_type,
        source_files=sorted(os.path.abspath(f) for f in source_files),
    )

    if cache_path_directory is not None:
        cache_key  = _compute_cache_key(
            source_files, actual_source_format, actual_contract_type, columns, aggregate, filters,
            derived_columns, deduplicate,
            lowercased_columns=lowercased_columns,
        )
        cache_file = os.path.join(cache_path_directory, f"usage_details_{cache_key}.parquet")
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
            for ts_field in ('usage_start_date', 'usage_end_date'):
                if cached_meta_raw.get(ts_field) is not None:
                    cached_meta_raw[ts_field] = pd.Timestamp(cached_meta_raw[ts_field])
            cached_metadata = UsageMetadata(
                **{f.name: cached_meta_raw.get(f.name) for f in fields(UsageMetadata)}
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
    # Compute usage date range from source files (cache-miss path only).
    # Reads only the Date column (or just the parquet footer stats), not the
    # full dataset.  On cache hits the values come from the parquet metadata
    # and this is skipped entirely.
    # ------------------------------------------------------------------
    date_cols = (
        EA_USAGE_DATE_COLUMNS if actual_contract_type == 'ea' else MCA_USAGE_DATE_COLUMNS
    )
    date_format = EA_DATE_FORMAT if actual_contract_type == 'ea' else MCA_DATE_FORMAT
    metadata.usage_start_date, metadata.usage_end_date = compute_date_range(
        source_files, actual_source_format, date_cols, date_format,
    )
    # Drop the time component when it's midnight (usage details are date-only).
    for _f in ('usage_start_date', 'usage_end_date'):
        _v = getattr(metadata, _f)
        if _v is not None and _v == _v.normalize():
            setattr(metadata, _f, _v.date())
    logging.debug(
        "Usage date range: %s → %s",
        metadata.usage_start_date, metadata.usage_end_date,
    )

    # Expand columns to include filter-only and derived-required columns for loading
    # (after cache check, so the cache key reflects the requested output columns).
    if filter_only_cols:
        columns = list(columns) + filter_only_cols
    if derived_required_only_cols:
        columns = list(columns) + derived_required_only_cols

    # ------------------------------------------------------------------
    # Step 2d — Resolve column synonyms
    # ------------------------------------------------------------------
    # Build a {source_name: canonical_name} map for columns present in the source
    # file under an alternative name (e.g. 'MeterID' instead of 'MeterId').
    # resolved_columns substitutes source names for the file reader; per-frame
    # renaming back to canonical names happens immediately after loading, before
    # derived column computation and per-file aggregation.
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
    #
    # Aggregation strategy (when aggregate= is provided):
    #   • Two groupby column lists are used when derived_columns is set:
    #       - pre_agg_groupby_cols: loaded cols minus agg set; includes
    #         derived_required_only_cols as pass-through dims for per-file pre-agg.
    #       - final_groupby_cols: same, minus derived_required_only_cols, plus the
    #         names of derived columns (computed post-concat, used as final dims).
    #   • Multi-file (map-reduce): each file is aggregated individually right after
    #     loading, then a final groupby after concat merges cross-file duplicates.
    #     This limits peak memory by keeping per-file frames small before concat.
    #   • Single file: only the final groupby runs — no double aggregation.

    if actual_source_format == 'csv':
        schema_map      = EA_COLUMN_DTYPES if actual_contract_type == 'ea' else MCA_COLUMN_DTYPES
        schema_date_fmt = EA_DATE_FORMAT   if actual_contract_type == 'ea' else MCA_DATE_FORMAT

        # Restrict to requested columns, or use every column present in the schema map.
        # active_dtype keys are remapped to source names so pd.read_csv dtype= matches
        # the actual column names in the file. active_dates stays canonical: date
        # casting runs after the per-frame synonyms rename.
        active_map   = {c: v for c, v in schema_map.items() if columns is None or c in columns}
        active_dtype = {canonical_to_source.get(c, c): v for c, v in active_map.items() if v != _PARSE_DATE_SENTINEL}
        active_dates = [c for c, v in active_map.items() if v == _PARSE_DATE_SENTINEL]

    if aggregate is not None:
        agg_set      = set(aggregate)
        agg_dict     = {c: 'sum' for c in aggregate}
        # Derive groupby columns preserving the original column order.
        loaded_cols          = columns if columns is not None else detected_columns
        pre_agg_groupby_cols = [c for c in loaded_cols if c not in agg_set]
        derived_req_set      = set(derived_required_only_cols)
        derived_col_names    = list(derived_columns) if derived_columns is not None else []
        final_groupby_cols   = (
            [c for c in pre_agg_groupby_cols if c not in derived_req_set]
            + derived_col_names
        )
        # When columns=None, loaded_cols are source column names; remap any synonym
        # source names to canonical so groupby works correctly after per-frame rename.
        if rename_map:
            pre_agg_groupby_cols = [rename_map.get(c, c) for c in pre_agg_groupby_cols]
            final_groupby_cols   = [rename_map.get(c, c) for c in final_groupby_cols]

    multi_file = len(source_files) > 1
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
                    frame[col] = pd.to_datetime(frame[col], format=schema_date_fmt)
            # CSV has no predicate pushdown — apply every filter in memory.
            frame = _apply_in_memory_filters(frame, filters)
        else:
            # Parquet read above already applied pq_filters; apply any residual
            # filters (e.g. 'iin' / 'not_iin') that pyarrow can't push down.
            frame = _apply_in_memory_filters(frame, residual_filters)

        # Canonicalize casing on identifier columns (Azure exports occasionally
        # emit mixed case — e.g. 'NORTHEUROPE' on Microsoft Fabric rows).
        # Runs *after* filtering so case-sensitive filter ops see raw values;
        # use ColumnFilter(op='iin') if the filter itself must be case-insensitive.
        for _norm_col in _LOWERCASE_NORMALIZED_COLUMNS:
            if _norm_col in frame.columns:
                frame[_norm_col] = frame[_norm_col].str.lower()

        if aggregate is not None and multi_file:
            # Per-file pre-aggregation: shrinks each frame before it enters concat.
            # Uses pre_agg_groupby_cols so derived_required_only_cols survive as dims.
            frame = frame.groupby(pre_agg_groupby_cols, dropna=False, observed=True).agg(agg_dict).reset_index()

        if 'Term' in frame.columns:
            # Convert the Term to values : 1Year, 3Years, 5Years (from 12, 36, 60)
            frame['Term'] = frame['Term'].astype('str').replace({'12': '1Year', '36': '3Years', '60': '5Years'}).astype('category')

        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------------------
    # Post-concat: compute derived columns then drop their required-only sources.
    # This happens before the final groupby so derived cols can act as dimensions.
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

    if aggregate is not None:
        # Final aggregation: merges cross-file duplicates (multi-file) or performs
        # the sole aggregation (single file). Uses final_groupby_cols which includes
        # derived column names (computed above) instead of their source columns.
        df = df.groupby(final_groupby_cols, dropna=False, observed=True).agg(agg_dict).reset_index()
        logging.info(
            "Aggregated to %d rows (groupby on %d column(s), summing %d column(s)).",
            len(df), len(final_groupby_cols), len(aggregate)
        )

    # Drop columns that were loaded only for filtering (not part of requested output).
    if filter_only_cols:
        df = df.drop(columns=filter_only_cols)
        logging.debug("Dropped filter-only column(s) %s from result.", filter_only_cols)

    # Deduplicate the final result. Runs after filter_only_cols are dropped so
    # subset checks and the resulting frame both reflect the user's output shape.
    if deduplicate:
        before = len(df)
        if deduplicate is True:
            df = df.drop_duplicates(keep='first', ignore_index=True)
        else:
            df = df.drop_duplicates(subset=list(deduplicate), keep='first', ignore_index=True)
        logging.info(
            "Deduplicated %d → %d rows (subset=%s).",
            before, len(df),
            'all columns' if deduplicate is True else list(deduplicate),
        )

    logging.info(
        "Returned %d rows in %.3fs — %d file(s) loaded as %s/%s.",
        len(df), time.perf_counter() - _t0, len(source_files), actual_source_format, actual_contract_type,
    )

    if cache_path_directory is not None:
        os.makedirs(cache_path_directory, exist_ok=True)
        # Persist metadata into the cached parquet's schema. Skip None-valued
        # fields so the cache stays minimal and forward-compatible: readers from
        # an older version of this module simply ignore unknown keys, and a
        # later version that adds new fields will resolve them to None when
        # absent from older cache files.
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


def parse_additional_info(raw, key: str) -> str | None:
    """Extract a value for ``key`` from an AdditionalInfo JSON string.

    Returns None if ``raw`` is empty, not a string, not valid JSON, not a JSON
    object, or does not contain ``key``.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get(key)


def parse_tags(raw) -> dict:
    """Convert a tags value to a dict.

    Handles three formats stored in parquet:
    * Already a dict  → returned as-is.
    * JSON object string (``{...}``) → parsed with json.loads.
    * Bare key/value pairs (``"k": "v", ...``) without outer braces → wrapped and parsed.
    * Anything else / parse errors → empty dict.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.strip()
    if not text.startswith("{"):
        text = "{" + text + "}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('azure').setLevel(logging.WARNING)

    # Smoke test: load a usage-details file specified via the
    # USAGE_DETAILS_PATH environment variable. No customer-specific path is
    # checked in.
    usage_details_path = os.environ.get('USAGE_DETAILS_PATH')
    if not usage_details_path:
        raise SystemExit('Set USAGE_DETAILS_PATH before running this module directly.')

    df = load_usage_details(
        path=usage_details_path,
        filters={
            'MeterCategory': ['Virtual Machines'],
            'ChargeType': ['UnusedReservation', 'UnusedSavingsPlan'],
        },
        cache_path_directory=None,
    )
    print(df.head())
