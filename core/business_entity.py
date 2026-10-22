"""Business entity mapping helpers.

Exposes two public functions:

* :func:`get_business_entity_mapping` — build a ``dict[SubscriptionId, BusinessEntity]``
    from a chosen source (Azure usage details, the live EA billing hierarchy,
    Resource Graph management-group hierarchy, or a user-provided CSV/Excel
    file). Mapping keys are stored lower-cased so the join in
    :func:`add_business_entity_column` can be case-insensitive.
* :func:`add_business_entity_column` — insert a BusinessEntity column into a
  DataFrame, positioned immediately before the SubscriptionId column.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any

import pandas as pd

from core.azure_resource_graph import run_graph_query
from core.usage_data import load_usage_details


DEFAULT_SUBSCRIPTION_COL = 'SubscriptionId'
DEFAULT_BUSINESS_ENTITY_COL = 'BusinessEntity'
DEFAULT_UNKNOWN_VALUE = 'Unknown'

# Placeholder/zero subscription id that some sources emit; never include it in mappings.
_ZERO_SUBSCRIPTION_ID = '00000000-0000-0000-0000-000000000000'

# Method name → (source column in usage details, expected contract type or None).
_USAGE_DETAIL_METHODS: dict[str, tuple[str, str | None]] = {
    'use_ea_department_name_from_usage_details':   ('InvoiceSection',     'ea'),
    'use_mca_invoice_section_from_usage_details':  ('InvoiceSectionName', 'mca'),
    'use_subscription_name_from_usage_details':    ('SubscriptionName',   None),
}

_MG_LEVEL_METHOD_RE = re.compile(r'^use_management_group_hierarchy_level_([0-6])$')

_EA_BILLING_API_METHOD = 'use_ea_department_name_from_billing_api'
_CUSTOM_MAPPING_METHOD = 'use_custom_mapping_file'

_BILLING_API_VERSION = '2024-04-01'
_MANAGEMENT_API_BASE = 'https://management.azure.com'
_MANAGEMENT_TOKEN_SCOPE = 'https://management.azure.com/.default'


def get_business_entity_mapping(method: str, **kwargs: Any) -> dict[str, str]:
    """Return a mapping ``SubscriptionId → BusinessEntity``.

    Keys in the returned dict are lower-cased to enable case-insensitive joins.

    Parameters
    ----------
    method : str
        Loader strategy. Supported values:

        * ``'use_ea_department_name_from_usage_details'`` — use the EA
          ``InvoiceSection`` column from usage details (validates the source
          is an EA contract).
        * ``'use_mca_invoice_section_from_usage_details'`` — use the MCA
          ``InvoiceSectionName`` column from usage details (validates the
          source is an MCA contract).
        * ``'use_subscription_name_from_usage_details'`` — use ``SubscriptionName``
          from usage details.
                * ``'use_ea_department_name_from_billing_api'`` — query the current EA
                    billing hierarchy and map each billing subscription to its enrollment
                    account's department.
        * ``'use_management_group_hierarchy_level_<N>'`` where ``N`` is 0..6 —
          query Azure Resource Graph for each subscription's management-group
          ancestor chain and use the management group at hierarchy level ``N``
          (level 0 = the tenant root group, level 1 = the top-most MG directly
          under the tenant root, higher levels move down toward the
          subscription).
        * ``'use_custom_mapping_file'`` — read a user-supplied CSV or Excel file.

    Other Parameters
    ----------------
    usage_details_path : str
        Required for the ``*_from_usage_details`` methods. Forwarded to
        :func:`usage_data.load_usage_details`.
    load_usage_details_kwargs : dict, optional
        Extra kwargs forwarded to :func:`usage_data.load_usage_details`
        (e.g. ``contract_type``, ``cache_path_directory``).
    credential : TokenCredential
        Required for management-group and Billing API methods.
    billing_account_names : list[str], optional
        EA billing account names to query for the Billing API method. When
        omitted, all accessible Enterprise Agreement billing accounts are
        discovered automatically.
    custom_mapping_file_path : str
        Required for ``use_custom_mapping_file``. Path to a ``.csv``,
        ``.xlsx`` or ``.xls`` file.
    custom_mapping_subscription_id_column : str, optional
        Column name for the subscription id in the custom mapping file
        (default ``'SubscriptionId'``).
    custom_mapping_business_entity_column : str, optional
        Column name for the business entity in the custom mapping file
        (default ``'BusinessEntity'``).

    Returns
    -------
    dict[str, str]
        Mapping ``subscription_id_lower → business_entity``.

    Raises
    ------
    ValueError
        If ``method`` is not recognised or required kwargs are missing.
    """
    if method in _USAGE_DETAIL_METHODS:
        if 'usage_details_path' not in kwargs:
            raise ValueError(
                f"method={method!r} requires keyword argument 'usage_details_path'."
            )
        source_col, expected_contract = _USAGE_DETAIL_METHODS[method]
        return _load_from_usage_details(
            path=kwargs['usage_details_path'],
            source_col=source_col,
            expected_contract=expected_contract,
            load_usage_details_kwargs=kwargs.get('load_usage_details_kwargs') or {},
            subscription_ids=kwargs.get('subscription_ids'),
        )

    if method == _EA_BILLING_API_METHOD:
        if 'credential' not in kwargs:
            raise ValueError(
                f"method={method!r} requires keyword argument 'credential'."
            )
        return _load_ea_departments_from_billing_api(
            credential=kwargs['credential'],
            billing_account_names=kwargs.get('billing_account_names'),
        )

    mg_match = _MG_LEVEL_METHOD_RE.match(method)
    if mg_match:
        if 'credential' not in kwargs:
            raise ValueError(
                f"method={method!r} requires keyword argument 'credential'."
            )
        return _load_from_management_group(
            credential=kwargs['credential'],
            level=int(mg_match.group(1)),
        )

    if method == _CUSTOM_MAPPING_METHOD:
        if 'custom_mapping_file_path' not in kwargs:
            raise ValueError(
                f"method={method!r} requires keyword argument 'custom_mapping_file_path'."
            )
        return _load_from_custom_file(
            path=kwargs['custom_mapping_file_path'],
            subscription_id_column=kwargs.get(
                'custom_mapping_subscription_id_column', DEFAULT_SUBSCRIPTION_COL),
            business_entity_column=kwargs.get(
                'custom_mapping_business_entity_column', DEFAULT_BUSINESS_ENTITY_COL),
        )

    supported = (
        list(_USAGE_DETAIL_METHODS.keys())
        + [_EA_BILLING_API_METHOD]
        + [f'use_management_group_hierarchy_level_{i}' for i in range(0, 7)]
        + [_CUSTOM_MAPPING_METHOD]
    )
    raise ValueError(
        f"Unsupported method {method!r}. Supported methods: {supported}."
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _load_ea_departments_from_billing_api(
    credential,
    billing_account_names: list[str] | None = None,
) -> dict[str, str]:
    """Build a current subscription-to-EA-department mapping from Azure Billing.

    The Billing API exposes the relationship in two collections: enrollment
    accounts carry the department display name, while billing subscriptions
    carry the enrollment-account resource ID. This loader joins those
    collections in memory and therefore performs two list operations per EA
    billing account.
    """
    logging.info("Building EA department mapping from Azure Billing")
    token = credential.get_token(_MANAGEMENT_TOKEN_SCOPE).token
    account_names = list(billing_account_names or [])
    if not account_names:
        account_names = [
            str(account.get('name'))
            for account in _list_billing_api_items(
                token, '/providers/Microsoft.Billing/billingAccounts'
            )
            if account.get('name')
            and str(
                (account.get('properties') or {}).get('agreementType', '')
            ).lower() == 'enterpriseagreement'
        ]

    if not account_names:
        logging.warning(
            "   No accessible Enterprise Agreement billing accounts found."
        )
        return {}

    mapping: dict[str, str] = {}
    for account_name in account_names:
        account = urllib.parse.quote(account_name, safe='')
        enrollment_departments: dict[str, str] = {}
        for enrollment in _list_billing_api_items(
            token,
            f'/providers/Microsoft.Billing/billingAccounts/{account}/enrollmentAccounts',
        ):
            department = str(
                (enrollment.get('properties') or {}).get(
                    'departmentDisplayName', ''
                )
                or ''
            )
            if not department:
                continue
            enrollment_id = str(enrollment.get('id') or '').lower()
            enrollment_name = str(enrollment.get('name') or '').lower()
            if enrollment_id:
                enrollment_departments[enrollment_id] = department
            if enrollment_name:
                enrollment_departments[enrollment_name] = department

        for subscription in _list_billing_api_items(
            token,
            f'/providers/Microsoft.Billing/billingAccounts/{account}/billingSubscriptions',
        ):
            properties = subscription.get('properties') or {}
            subscription_id = str(properties.get('subscriptionId') or '').lower()
            enrollment_id = str(
                properties.get('enrollmentAccountId') or ''
            ).lower()
            if not subscription_id or subscription_id == _ZERO_SUBSCRIPTION_ID:
                continue
            department = enrollment_departments.get(enrollment_id)
            if not department and enrollment_id:
                department = enrollment_departments.get(
                    enrollment_id.rstrip('/').rsplit('/', 1)[-1]
                )
            if not department:
                continue
            previous = mapping.get(subscription_id)
            if previous and previous != department:
                logging.warning(
                    "   Subscription %s maps to multiple EA departments (%r, %r); "
                    "using %r.",
                    subscription_id,
                    previous,
                    department,
                    department,
                )
            mapping[subscription_id] = department

    logging.info(
        "   Built live EA department mapping with %d subscription(s) from "
        "%d billing account(s).",
        len(mapping),
        len(account_names),
    )
    return mapping


def _list_billing_api_items(token: str, path: str) -> list[dict[str, Any]]:
    """Return every item from a paginated Azure Billing REST collection."""
    query = urllib.parse.urlencode({'api-version': _BILLING_API_VERSION})
    url: str | None = f'{_MANAGEMENT_API_BASE}{path}?{query}'
    items: list[dict[str, Any]] = []
    while url:
        request = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
                'User-Agent': 'az-toolbox',
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        items.extend(payload.get('value') or [])
        url = payload.get('nextLink')
        if url and url.startswith('/'):
            url = f'{_MANAGEMENT_API_BASE}{url}'
    return items


def _load_from_usage_details(
    path: str,
    source_col: str,
    expected_contract: str | None,
    load_usage_details_kwargs: dict[str, Any],
    subscription_ids: list[str] | None = None,
) -> dict[str, str]:
    """Build a mapping from the most recent value of ``source_col`` per subscription.

    When ``expected_contract`` is ``'ea'`` or ``'mca'``, raises ValueError if the
    loaded usage details are not of that contract type.
    """
    logging.info(
        "Building business entity mapping from usage details at %s using column %r",
        path, source_col,
    )
    extra = dict(load_usage_details_kwargs or {})
    # Natively scope the usage-details read to ``subscription_ids``. An
    # explicit SubscriptionId filter in load_usage_details_kwargs wins.
    if subscription_ids:
        filters = dict(extra.get('filters') or {})
        filters.setdefault('SubscriptionId', list(subscription_ids))
        extra['filters'] = filters
    df, metadata = load_usage_details(
        path,
        columns=[source_col, 'SubscriptionId', 'Date'],
        return_metadata=True,
        **extra,
    )
    if expected_contract is not None and metadata.contract_type != expected_contract:
        raise ValueError(
            f"Expected {expected_contract!r} contract usage details at {path!r}, "
            f"but detected {metadata.contract_type!r}."
        )

    df = df[['SubscriptionId', source_col, 'Date']].dropna(subset=['SubscriptionId'])
    df = df[df['SubscriptionId'].astype(str).str.lower() != _ZERO_SUBSCRIPTION_ID]
    df = df.drop_duplicates()
    df = df.sort_values('Date', ascending=False)
    df = df.drop_duplicates(subset='SubscriptionId', keep='first')

    null_entities = df[source_col].isna().sum()
    if null_entities:
        logging.warning(
            "   %d subscription(s) have a null %s; they will be omitted from the mapping.",
            null_entities, source_col,
        )
    df = df.dropna(subset=[source_col])

    mapping = {
        str(sub).lower(): str(entity)
        for sub, entity in zip(df['SubscriptionId'], df[source_col])
    }
    logging.info("   Built mapping with %d subscription(s).", len(mapping))
    return mapping


# KQL: project each subscription with its management-group ancestor chain.
# managementGroupAncestorsChain is ordered from immediate parent (index 0)
# up to the tenant root group (last element). We reverse it and drop the
# tenant root so index 0 = top-most MG directly under the tenant root.
_MG_HIERARCHY_KQL = """
resourcecontainers
| where type == 'microsoft.resources/subscriptions'
| extend chain = properties.managementGroupAncestorsChain
| project SubscriptionId = subscriptionId, chain
| order by SubscriptionId asc
""".strip()


def _load_from_management_group(
    credential,
    level: int,
) -> dict[str, str]:
    """Build a mapping from the management group at the given hierarchy level.

    ``level=0`` returns the tenant root group (a many-to-one mapping where
    every subscription resolves to the same value). ``level=1`` returns the
    top-most management group directly under the tenant root; higher levels
    move down toward the subscription. Subscriptions whose ancestor chain is
    shorter than ``level`` are omitted from the mapping.
    """
    logging.info(
        "Building business entity mapping from management group hierarchy level %d",
        level,
    )
    rows = run_graph_query(
        _MG_HIERARCHY_KQL,
        credential,
    )

    mapping: dict[str, str] = {}
    skipped = 0
    for row in rows:
        sub_id = row.get('SubscriptionId')
        chain = row.get('chain') or []
        if not sub_id or not isinstance(chain, list):
            skipped += 1
            continue
        if str(sub_id).lower() == _ZERO_SUBSCRIPTION_ID:
            skipped += 1
            continue
        # Reverse so index 0 = tenant root. Level 0 selects the tenant root;
        # level >=1 selects MGs below it (so we offset the chain accordingly).
        chain_top_down = list(reversed(chain))
        if level >= 1:
            chain_top_down = chain_top_down[1:]
        if len(chain_top_down) < max(level, 1):
            skipped += 1
            continue
        node = chain_top_down[max(level - 1, 0)]
        if not isinstance(node, dict):
            skipped += 1
            continue
        entity = node.get('displayName') or node.get('name')
        if not entity:
            skipped += 1
            continue
        mapping[str(sub_id).lower()] = str(entity)

    if skipped:
        logging.warning(
            "   %d subscription(s) had no management group at level %d; omitted from mapping.",
            skipped, level,
        )
    logging.info("   Built mapping with %d subscription(s).", len(mapping))
    return mapping


def _load_from_custom_file(
    path: str,
    subscription_id_column: str,
    business_entity_column: str,
) -> dict[str, str]:
    """Build a mapping from a user-provided CSV or Excel file."""
    logging.info("Building business entity mapping from custom file %s", path)
    ext = os.path.splitext(path)[1].lower()
    if ext == '.csv':
        df = pd.read_csv(path, dtype=str)
    elif ext in ('.xlsx', '.xls'):
        df = pd.read_excel(path, dtype=str)
    else:
        raise ValueError(
            f"Unsupported file extension {ext!r} for custom mapping file {path!r}. "
            f"Expected .csv, .xlsx or .xls."
        )

    missing = [
        c for c in (subscription_id_column, business_entity_column)
        if c not in df.columns
    ]
    if missing:
        raise ValueError(
            f"Custom mapping file {path!r} is missing required column(s): {missing}. "
            f"Available columns: {list(df.columns)}."
        )

    df = df[[subscription_id_column, business_entity_column]].dropna(
        subset=[subscription_id_column])
    df = df[df[subscription_id_column].astype(str).str.lower() != _ZERO_SUBSCRIPTION_ID]

    duplicates = df[subscription_id_column].str.lower().duplicated().sum()
    if duplicates:
        logging.warning(
            "   %d duplicate subscription id(s) in %s; later rows will overwrite earlier ones.",
            duplicates, path,
        )

    mapping = {
        str(sub).lower(): str(entity) if pd.notna(entity) else ''
        for sub, entity in zip(df[subscription_id_column], df[business_entity_column])
    }
    logging.info("   Built mapping with %d subscription(s).", len(mapping))
    return mapping


# ---------------------------------------------------------------------------
# Column injection
# ---------------------------------------------------------------------------
def add_business_entity_column(
    df: pd.DataFrame,
    mapping: dict[str, str],
    subscription_id_column: str = DEFAULT_SUBSCRIPTION_COL,
    business_entity_column: str = DEFAULT_BUSINESS_ENTITY_COL,
    unknown_value: str = DEFAULT_UNKNOWN_VALUE,
) -> pd.DataFrame:
    """Insert a BusinessEntity column into ``df`` immediately before the SubscriptionId column.

    The lookup is case-insensitive: values in ``df[subscription_id_column]`` are
    lower-cased before being looked up in ``mapping`` (whose keys are expected
    to already be lower-cased — that's how :func:`get_business_entity_mapping`
    builds them).

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame. Mutated in place; also returned for chaining.
    mapping : dict[str, str]
        Mapping ``subscription_id_lower → business_entity``.
    subscription_id_column : str, optional
        Name of the SubscriptionId column in ``df`` (default ``'SubscriptionId'``).
    business_entity_column : str, optional
        Name of the BusinessEntity column to add (default ``'BusinessEntity'``).
    unknown_value : str, optional
        Value to use when a subscription id is not found in ``mapping``
        (default ``'Unknown'``).

    Returns
    -------
    pd.DataFrame
        ``df`` with the new column inserted immediately before
        ``subscription_id_column``.

    Raises
    ------
    ValueError
        If ``subscription_id_column`` is not present in ``df``.
    """
    if subscription_id_column not in df.columns:
        raise ValueError(
            f"Column {subscription_id_column!r} not found in DataFrame. "
            f"Available columns: {list(df.columns)}."
        )

    entities = (
        df[subscription_id_column]
        .astype(str)
        .str.lower()
        .map(mapping)
        .fillna(unknown_value)
    )

    if business_entity_column in df.columns:
        logging.debug(
            "Column %r already present in DataFrame; replacing it.",
            business_entity_column,
        )
        df.drop(columns=[business_entity_column], inplace=True)

    insert_idx = df.columns.get_loc(subscription_id_column)
    df.insert(insert_idx, business_entity_column, entities.values)

    unknown_count = (df[business_entity_column] == unknown_value).sum()
    if unknown_count:
        logging.info(
            "   %d row(s) had no business entity mapping; set to %r.",
            unknown_count, unknown_value,
        )
    return df
