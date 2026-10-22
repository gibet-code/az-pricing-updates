"""Collect and validate pricing-report inputs from files, caches and Azure."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from core._sources_common import ColumnFilter
from core.business_entity import get_business_entity_mapping
from core.compute_skus import get_compute_sku_availability
from core.pricesheet import PricesheetMetadata, load_pricesheet
from core.subscriptions import get_subscriptions
from core.usage_data import (
    UsageMetadata,
    get_contract_type as get_usage_contract_type,
    load_usage_details,
    parse_additional_info,
)
from pricing_updates.price_impact import impact, ri_evolution

logger = logging.getLogger(__name__)

REPORT_DIMENSIONS = [
    "SubscriptionId",
    "SubscriptionName",
    "PricingCurrency",
    "MeterCategory",
    "MeterSubCategory",
    "MeterName",
    "UnitOfMeasure",
    "MeterRegion",
    "ResourceLocation",
    "PricingModel",
    "ChargeType",
    "ReservationId",
    "ProductOrderId",
    "MeterId",
    "Term"
]

VM_REPORT_DIMENSIONS = [*REPORT_DIMENSIONS, "Product"]


def load_reservation_inventory(path: Path) -> pd.DataFrame:
    inventory = pd.read_csv(path, dtype="string")
    missing_columns = set(impact.RESERVATION_INVENTORY_COLUMNS).difference(
        inventory.columns
    )
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(
            f"Reservation inventory is missing required columns: {missing}"
        )

    for source, target in impact.RESERVATION_INVENTORY_COLUMNS.items():
        inventory[target] = inventory[source]
    for key in ("_ReservationIdKey", "_ProductOrderIdKey"):
        inventory[key] = impact.normalize_identifier(inventory[key])
        if inventory[key].isna().any():
            raise ValueError(
                f"Reservation inventory contains an empty {key[1:-3]} value."
            )

    duplicate_keys = inventory.duplicated(
        subset=["_ReservationIdKey", "_ProductOrderIdKey"],
        keep=False,
    )
    if duplicate_keys.any():
        raise ValueError(
            "Reservation inventory contains duplicate reservation and order "
            "identifier pairs."
        )
    return inventory


def load_storage_usage(
    usage_path: str | Path,
    *,
    price_protection_contract_type: Literal["ea", "mca"] | None = None,
    cache_dir: Path | None = None,
) -> tuple[pd.DataFrame, UsageMetadata]:
    """Collect aggregated Storage usage and metadata without pricing enrichment."""
    protection_dimensions = {
        "ea": ["OfferId"],
        "mca": ["ProductOrderName"],
        None: [],
    }
    report_dimensions = [
        *REPORT_DIMENSIONS,
        *protection_dimensions[price_protection_contract_type],
    ]
    aggregate_columns = [
        "CostInPricingCurrency",
        *(["Quantity"] if price_protection_contract_type else []),
    ]
    loaded = load_usage_details(
        path=str(usage_path),
        columns=report_dimensions,
        aggregate=aggregate_columns,
        filters={
            "MeterCategory": ColumnFilter(("Storage","Azure NetApp Files"), op="in"),
        },
        return_metadata=True,
        cache_path_directory=str(cache_dir) if cache_dir is not None else None,
    )
    return loaded


def load_vm_usage(
    usage_path: str | Path,
    *,
    include_offer_id: bool = False,
    include_product_order_name: bool = False,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Collect VM usage, derive VMSize and aggregate without AdditionalInfo."""
    report_dimensions = [
        *VM_REPORT_DIMENSIONS,
        *(["OfferId"] if include_offer_id else []),
        *(["ProductOrderName"] if include_product_order_name else []),
    ]
    report = load_usage_details(
        path=str(usage_path),
        columns=report_dimensions + ["AdditionalInfo"],
        aggregate=["CostInPricingCurrency", "Quantity"],
        filters={
            "MeterCategory": ColumnFilter(("Virtual Machines",), op="in"),
        },
        cache_path_directory=str(cache_dir) if cache_dir is not None else None,
    )
    report["VMSize"] = (
        report["AdditionalInfo"]
        .map(
            lambda value: parse_additional_info(value, "ServiceType")
            if isinstance(value, str)
            else None
        )
        .astype("string")
    )
    report = (
        report.groupby(
            report_dimensions + ["VMSize"],
            dropna=False,
            observed=True,
            as_index=False,
        )[["CostInPricingCurrency", "Quantity"]]
        .sum()
    )
    return report


def _pricesheet_meter_ids(usage_report: pd.DataFrame) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                meter_id
                for meter_id in (
                    usage_report["MeterId"]
                    .astype("string")
                    .str.strip()
                    .dropna()
                )
                if meter_id
            },
            key=str.casefold,
        )
    )


def load_report_pricesheets(
    storage_report: pd.DataFrame,
    vm_report: pd.DataFrame,
    pricesheet_path: str | Path,
    *,
    cache_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, PricesheetMetadata]:
    """Load prices once and split by each service's categories and usage meters."""
    storage_categories = ("Storage", "Azure NetApp Files")
    vm_categories = ("Virtual Machines", "VirtualMachines")
    storage_meter_ids = _pricesheet_meter_ids(storage_report)
    vm_meter_ids = _pricesheet_meter_ids(vm_report)
    meter_ids = tuple(
        sorted(set(storage_meter_ids).union(vm_meter_ids), key=str.casefold)
    )
    pricesheet, metadata = load_pricesheet(
        path=str(pricesheet_path),
        filters={
            "MeterCategory": ColumnFilter(
                (*storage_categories, *vm_categories),
                op="in",
            ),
            # Empty Parquet IN predicates have no value type; filter in memory.
            "MeterId": ColumnFilter(meter_ids, op="in" if meter_ids else "iin"),
        },
        return_metadata=True,
        cache_path_directory=str(cache_dir) if cache_dir is not None else None,
    )
    storage_pricesheet = pricesheet.loc[
        pricesheet["MeterCategory"].isin(storage_categories)
        & pricesheet["MeterId"].isin(storage_meter_ids)
    ].reset_index(drop=True)
    vm_pricesheet = pricesheet.loc[
        pricesheet["MeterCategory"].isin(vm_categories)
        & pricesheet["MeterId"].isin(vm_meter_ids)
    ].reset_index(drop=True)
    return storage_pricesheet, vm_pricesheet, metadata


def validate_matching_contract_types(
    usage_metadata: UsageMetadata,
    pricesheet_metadata: PricesheetMetadata,
) -> None:
    """Require usage details and pricesheet to use the same contract type."""
    usage_contract_type = usage_metadata.contract_type
    pricesheet_contract_type = pricesheet_metadata.contract_type
    if usage_contract_type is None:
        raise ValueError(
            "Cannot validate contract types because the usage details "
            "contract type is missing."
        )
    if pricesheet_contract_type is None:
        raise ValueError(
            "Cannot validate contract types because the pricesheet contract "
            "type is missing."
        )

    normalized_usage_type = usage_contract_type.strip().casefold()
    normalized_pricesheet_type = pricesheet_contract_type.strip().casefold()
    if normalized_usage_type != normalized_pricesheet_type:
        raise ValueError(
            "Contract type mismatch: usage details use "
            f"{usage_contract_type.upper()}, but the pricesheet uses "
            f"{pricesheet_contract_type.upper()}."
        )


def validate_price_protection_contract(
    usage_metadata: UsageMetadata,
) -> Literal["ea", "mca"]:
    """Return the supported usage contract for price protection."""
    contract_type = usage_metadata.contract_type
    if contract_type is None:
        raise ValueError(
            "Cannot enable price protection because the usage details "
            "contract type is missing."
        )
    normalized = contract_type.strip().casefold()
    if normalized not in {"ea", "mca"}:
        raise ValueError(
            "Price protection supports only EA and MCA contracts; "
            f"{contract_type!r} is unsupported."
        )
    return normalized


def load_vm_lifecycle(
    vm_report: pd.DataFrame,
    cache_path: Path | None,
    *,
    credential_factory: Callable[[], Any],
) -> tuple[dict[str, dict[str, str | None]], str, str]:
    """Reuse the cache before requesting a credential for a live lookup."""
    vm_sizes = sorted(
        {
            str(value).strip()
            for value in vm_report["VMSize"].dropna()
            if str(value).strip()
        },
        key=str.casefold,
    )
    if cache_path is not None and cache_path.exists():
        logger.info(
            "[lifecycle] Source: cache %s (%d requested VM sizes)",
            cache_path.resolve(), len(vm_sizes),
        )
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            lifecycle = payload["skus"]
            snapshot_date = str(payload["created_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(
                f"Invalid VM lifecycle cache {cache_path}: {exc}"
            ) from exc
        if not isinstance(lifecycle, dict):
            raise ValueError(
                f"Invalid VM lifecycle cache {cache_path}: skus must be an "
                "object."
            )
        return lifecycle, f"Cache: {cache_path.resolve()}", snapshot_date

    logger.info(
        "[lifecycle] Source: live compute-SKU API (%d requested VM sizes)",
        len(vm_sizes),
    )
    credential = credential_factory()
    subscriptions = get_subscriptions(credential)
    subscription_ids = [
        str(subscription.get("id") or "").strip()
        for subscription in subscriptions
        if str(subscription.get("id") or "").strip()
    ]
    if not subscription_ids:
        raise ValueError(
            "Price evolution requires at least one subscription visible to "
            "the connected identity for the lifecycle lookup."
        )
    subscription_id = subscription_ids[0]
    logger.debug(
        "[lifecycle] Using subscription %s",
        subscription_id,
    )
    try:
        # TODO: Migrate to get_compute_skus(include_retirement=True).
        availability = get_compute_sku_availability(
            subscription_id,
            credential,
            fill_missing=False,
            include_availability_tier=False,
        )
    except Exception as exc:
        raise RuntimeError(
            "VM lifecycle lookup failed; price evolution cannot be "
            "generated."
        ) from exc

    lifecycle = ri_evolution.build_lifecycle_index(availability)
    if vm_sizes and not lifecycle:
        raise RuntimeError(
            "VM lifecycle lookup returned no SKUs for "
            f"{len(vm_sizes)} requested VM size(s)."
        )
    snapshot_date = datetime.now(timezone.utc).isoformat()
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": snapshot_date,
            "subscription_id": subscription_id,
            "skus": lifecycle,
        }
        cache_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        logger.info(
            "[lifecycle] Wrote cache %s (%d SKU records)",
            cache_path.resolve(), len(lifecycle),
        )
    return lifecycle, "Live compute-SKU API", snapshot_date
