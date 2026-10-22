"""Report regional and legacy VM price-change impacts from Azure usage data.

Usage:
    python -m pricing_updates.impact_report <usage-file> \
        --output <report.xlsx> \
        [--reservation-inventory <reservedInstances.csv>] \
        [--pricesheet <pricesheet-path>] \
        [--price-protection] \
        [--ri-price-evolution] \
        [--ri-price-evolution-horizon-months <months>] \
        [--ri-price-evolution-fiscal-year-start-month <month>] \
        [--cache-dir <directory>] \
        [--business-entity-method <method>]

The usage input may be a CSV file, Parquet file, directory, or glob accepted
by :func:`core.usage_data.load_usage_details`.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Literal

from azure.identity import AzureCliCredential

from pricing_updates.price_impact import data, excel, impact, ri_evolution


logger = logging.getLogger(__name__)

BUSINESS_ENTITY_METHODS = {
    "ea_department_name": "use_ea_department_name_from_billing_api",
    "management_group_hierarchy_level_0": (
        "use_management_group_hierarchy_level_0"
    ),
    "management_group_hierarchy_level_1": (
        "use_management_group_hierarchy_level_1"
    ),
    "management_group_hierarchy_level_2": (
        "use_management_group_hierarchy_level_2"
    ),
    "management_group_hierarchy_level_3": (
        "use_management_group_hierarchy_level_3"
    ),
    "management_group_hierarchy_level_4": (
        "use_management_group_hierarchy_level_4"
    ),
    "management_group_hierarchy_level_5": (
        "use_management_group_hierarchy_level_5"
    ),
    "management_group_hierarchy_level_6": (
        "use_management_group_hierarchy_level_6"
    ),
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "usage_file",
        type=Path,
        help="Usage CSV, Parquet file, directory, or glob to load.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path of the Excel workbook to create.",
    )
    parser.add_argument(
        "--reservation-inventory",
        type=Path,
        help=(
            "Optional Azure reservation inventory CSV used to add purchase "
            "and expiration dates and fill missing reservation VM sizes. "
            "Adds a Reservations tab with original inventory columns "
            "except Deep link to reservation. Includes all "
            "reservation types and statuses. "
            "Only amortized usage without Purchase or Refund rows is supported."
        ),
    )
    parser.add_argument(
        "--pricesheet",
        type=Path,
        help=(
            "Optional Azure pricesheet file, directory, or glob. Matching "
            "Storage and Virtual Machines MeterId rows are written to "
            "dedicated pricesheet tabs."
        ),
    )
    parser.add_argument(
        "--price-protection",
        action="store_true",
        help=(
            "Apply EA or MCA PAYG BasePrice protection to Storage and Virtual "
            "Machines impact calculations. Requires --pricesheet."
        ),
    )
    parser.add_argument(
        "--ri-price-evolution",
        dest="price_evolution",
        action="store_true",
        help=(
            "Add monthly price evolution for existing VM RI usage and "
            "unused commitment only, with RI/SP/PAYG expiration fallbacks. "
            "Requires --pricesheet and --reservation-inventory."
        ),
    )
    parser.add_argument(
        "--ri-price-evolution-horizon-months",
        dest="price_evolution_horizon_months",
        type=int,
        default=18,
        help=(
            "Number of monthly periods in the RI price-evolution forecast "
            "(default: 18)."
        ),
    )
    parser.add_argument(
        "--ri-price-evolution-fiscal-year-start-month",
        dest="fiscal_year_start_month",
        type=int,
        choices=range(1, 13),
        default=1,
        metavar="MONTH",
        help=(
            "First month of the RI forecast fiscal year, from 1 (January) to 12 "
            "(December). Default: 1."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help=(
            "Optional shared cache directory for usage, pricesheets and VM "
            "lifecycle data. Caching is disabled by default. Lifecycle data "
            "is stored in vm-lifecycle.json and reused indefinitely; cached "
            "forecasts need no Azure credential unless business-entity "
            "grouping is requested."
        ),
    )
    parser.add_argument(
        "--business-entity-method",
        choices=tuple(BUSINESS_ENTITY_METHODS),
        help=(
            "Optionally add BusinessEntity to the Storage and Virtual "
            "Machines sheets. Use ea_department_name for the current EA "
            "department, or management_group_hierarchy_level_0 through "
            "management_group_hierarchy_level_6 for the selected management "
            "group level. Subscriptions without the selected level are "
            "reported as Unknown."
        ),
    )
    parser.add_argument(
        "--keep-no-impact-details",
        action="store_true",
        help=(
            "Keep non-impacting rows in the Storage and Virtual Machines "
            "worksheets for troubleshooting. By default, only impacted rows "
            "are exported."
        ),
    )
    args = parser.parse_args(argv)
    if args.price_protection and args.pricesheet is None:
        parser.error("--price-protection requires --pricesheet.")
    if args.price_evolution and args.pricesheet is None:
        parser.error("--ri-price-evolution requires --pricesheet.")
    if args.price_evolution and args.reservation_inventory is None:
        parser.error("--ri-price-evolution requires --reservation-inventory.")
    if args.price_evolution_horizon_months < 1:
        parser.error("--ri-price-evolution-horizon-months must be at least 1.")
    if (
        args.cache_dir is not None
        and args.cache_dir.exists()
        and not args.cache_dir.is_dir()
    ):
        parser.error("--cache-dir must be a directory.")
    return args


@contextmanager
def _log_phase(phase: str, operation: str) -> Generator[None, None, None]:
    started = perf_counter()
    logger.info("[%s] Starting: %s", phase, operation)
    yield
    logger.info(
        "[%s] Completed: %s (%.2fs)", phase, operation, perf_counter() - started,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)

    started = perf_counter()
    with _log_phase("validate", "Validating requested options"):
        args = _parse_args(argv)
        cache_dir: Path | None = args.cache_dir
        usage_cache_dir = cache_dir / "usage" if cache_dir is not None else None
        logger.info(
            "[validate] Cache: %s",
            cache_dir.resolve() if cache_dir is not None else "disabled",
        )
        price_evolution_enabled = bool(
            getattr(args, "price_evolution", False)
        )
        logger.info(
            "[validate] Optional work: %s",
            "; ".join(
                f"{name}={'enabled' if enabled else 'skipped'}"
                for name, enabled in (
                    ("reservation inventory", args.reservation_inventory is not None),
                    ("pricesheet", args.pricesheet is not None),
                    ("price protection", args.price_protection),
                    ("RI forecast", price_evolution_enabled),
                    ("business entities", args.business_entity_method is not None),
                )
            ),
        )
    reservation_inventory = None
    if args.reservation_inventory is not None:
        with _log_phase(
            "validate",
            f"Loading reservation inventory from {args.reservation_inventory}",
        ):
            reservation_inventory = data.load_reservation_inventory(
                args.reservation_inventory
            )
            logger.info(
                "[validate] Loaded %d reservation inventory rows",
                len(reservation_inventory),
            )
    credential: AzureCliCredential | None = None

    def get_credential() -> AzureCliCredential:
        nonlocal credential
        if credential is None:
            credential = AzureCliCredential()
        return credential

    price_protection_contract_type: Literal["ea", "mca"] | None = None
    pricing_contract_type: Literal["ea", "mca"] | None = None
    if args.price_protection or price_evolution_enabled:
        with _log_phase("validate", "Detecting the usage contract"):
            detected_contract_type = data.get_usage_contract_type(
                str(args.usage_file)
            )
            if detected_contract_type not in {"ea", "mca"}:
                raise ValueError(
                    "Cannot enable contract-aware pricing because the usage "
                    "contract type could not be detected: "
                    f"{detected_contract_type!r}."
                )
            pricing_contract_type = detected_contract_type
            if args.price_protection:
                price_protection_contract_type = pricing_contract_type
            logger.info("[validate] Detected %s usage contract", pricing_contract_type)
    with _log_phase("usage", f"Loading Storage usage from {args.usage_file}"):
        storage_usage, usage_metadata = data.load_storage_usage(
            args.usage_file,
            price_protection_contract_type=price_protection_contract_type,
            cache_dir=usage_cache_dir,
        )
        logger.info("[usage] Loaded %d aggregated Storage rows", len(storage_usage))
    with _log_phase("impact", "Preparing Storage impacts"):
        storage_report = impact.prepare_storage_report(
            storage_usage,
            reservation_inventory=reservation_inventory,
        )
    ri_evolution.warn_if_usage_period_is_not_single_complete_month(usage_metadata)
    if args.price_protection or price_evolution_enabled:
        with _log_phase("validate", "Checking loaded usage contract metadata"):
            metadata_contract_type = data.validate_price_protection_contract(
                usage_metadata
            )
            if metadata_contract_type != pricing_contract_type:
                raise ValueError(
                    "Detected usage contract type changed during loading: "
                    f"{pricing_contract_type} -> "
                    f"{metadata_contract_type}."
                )
    with _log_phase("usage", f"Loading VM usage from {args.usage_file}"):
        vm_usage = data.load_vm_usage(
            args.usage_file,
            include_offer_id=pricing_contract_type == "ea",
            include_product_order_name=(
                pricing_contract_type == "mca"
            ),
            cache_dir=usage_cache_dir,
        )
        logger.info("[usage] Loaded %d aggregated VM rows", len(vm_usage))
    with _log_phase("impact", "Preparing VM impacts"):
        vm_report = impact.prepare_vm_report(
            vm_usage,
            reservation_inventory=reservation_inventory,
        )
    price_evolution_vm_report = (
        vm_report.copy() if price_evolution_enabled else None
    )
    ri_report = None
    with _log_phase("validate", "Validating currency and analysis scope"):
        pricing_currency = impact.get_single_pricing_currency(
            storage_report,
            vm_report,
        )
        if price_evolution_vm_report is not None:
            ri_report = ri_evolution.select_ri_price_evolution_usage(
                price_evolution_vm_report
            )
            if usage_metadata.usage_end_date is None:
                raise ValueError(
                    "RI price evolution requires the usage period end date."
                )
            logger.info("[validate] Selected %d VM RI analysis rows", len(ri_report))
        logger.info("[validate] Pricing currency: %s", pricing_currency)
    if not args.keep_no_impact_details:
        storage_row_count = len(storage_report)
        vm_row_count = len(vm_report)
        storage_report, vm_report = impact.remove_no_impact_details(
            storage_report,
            vm_report,
        )
        logger.info(
            "[impact] Removed %d non-impacting Storage rows and %d non-impacting "
            "Virtual Machines rows",
            storage_row_count - len(storage_report),
            vm_row_count - len(vm_report),
        )
    storage_pricesheet_report = None
    vm_pricesheet_report = None
    pricesheet_metadata = None
    if args.pricesheet is not None:
        with _log_phase(
            "pricesheet", f"Loading Storage and VM prices from {args.pricesheet}",
        ):
            (
                storage_pricesheet_report,
                vm_pricesheet_report,
                pricesheet_metadata,
            ) = data.load_report_pricesheets(
                storage_report,
                (
                    price_evolution_vm_report
                    if price_evolution_vm_report is not None
                    else vm_report
                ),
                args.pricesheet,
                cache_dir=cache_dir / "pricesheet" if cache_dir is not None else None,
            )
            data.validate_matching_contract_types(
                usage_metadata,
                pricesheet_metadata,
            )
            storage_report = impact.add_reservable_status(
                storage_report,
                storage_pricesheet_report,
            )
            logger.info(
                "[pricesheet] Selected %d Storage and %d VM price rows",
                len(storage_pricesheet_report), len(vm_pricesheet_report),
            )
    if args.price_protection:
        with _log_phase("impact", "Applying current-usage price protection"):
            if (
                storage_pricesheet_report is None
                or vm_pricesheet_report is None
            ):
                raise ValueError(
                    "Price protection requires a loaded pricesheet."
                )
            storage_report = impact.add_storage_base_prices(
                storage_report,
                storage_pricesheet_report,
                price_protection_contract_type,
            )
            vm_report = impact.add_vm_base_prices(
                vm_report,
                vm_pricesheet_report,
                price_protection_contract_type,
            )
    lifecycle_data = None
    if ri_report is not None:
        with _log_phase("validate", "Validating RI forecast pricing inputs"):
            if vm_pricesheet_report is None or pricing_contract_type is None:
                raise ValueError(
                    "RI price evolution requires a VM pricesheet and a supported "
                    "usage contract."
                )
            ri_evolution.validate_price_evolution_price_columns(
                ri_report, vm_pricesheet_report,
            )
            if args.price_protection:
                impact.validate_base_price_columns(
                    ri_report,
                    vm_pricesheet_report,
                    pricing_contract_type,
                    policy=impact.BasePricePolicy.RI_PAYG_FALLBACK,
                    context="RI price evolution",
                )
        with _log_phase("lifecycle", "Collecting VM lifecycle data"):
            lifecycle_data = data.load_vm_lifecycle(
                ri_report,
                cache_dir / "vm-lifecycle.json" if cache_dir is not None else None,
                credential_factory=get_credential,
            )
            logger.info(
                "[lifecycle] Loaded %d SKU records (snapshot %s)",
                len(lifecycle_data[0]), lifecycle_data[2],
            )
    if args.business_entity_method is not None:
        with _log_phase(
            "usage",
            f"Collecting business entities using {args.business_entity_method}",
        ):
            business_entity_mapping = data.get_business_entity_mapping(
                BUSINESS_ENTITY_METHODS[args.business_entity_method],
                credential=get_credential(),
            )
            for prepared_report in (storage_report, vm_report, ri_report):
                if prepared_report is not None:
                    impact.add_business_entity_column(
                        prepared_report, business_entity_mapping,
                    )
            logger.info(
                "[usage] Received %d subscription business-entity mappings",
                len(business_entity_mapping),
            )
    price_evolution_result = None
    if ri_report is not None:
        with _log_phase("forecast", "Calculating RI price evolution"):
            if (
                vm_pricesheet_report is None
                or pricing_contract_type is None
                or lifecycle_data is None
            ):
                raise ValueError(
                    "RI price evolution requires collected prices, a supported "
                    "usage contract, and lifecycle data."
                )
            lifecycle, lifecycle_source, snapshot_date = lifecycle_data
            price_evolution_result = ri_evolution.build_price_evolution(
                ri_report,
                vm_pricesheet_report,
                usage_metadata,
                lifecycle=lifecycle,
                lifecycle_source=lifecycle_source,
                lifecycle_snapshot_date=snapshot_date,
                horizon_months=getattr(
                    args, "price_evolution_horizon_months", 18,
                ),
                fiscal_year_start_month=getattr(
                    args, "fiscal_year_start_month", 1,
                ),
                contract_type=pricing_contract_type,
                price_protection_enabled=args.price_protection,
            )
            logger.info(
                "[forecast] Produced %d projection rows and %d chart-data rows "
                "over %d months",
                len(price_evolution_result.projection),
                len(price_evolution_result.chart_data),
                price_evolution_result.horizon_months,
            )
    with _log_phase("workbook", f"Writing report to {args.output}"):
        excel.write_report(
            storage_report,
            vm_report,
            args.output,
            vm_pricesheet_report,
            storage_pricesheet_report=storage_pricesheet_report,
            usage_metadata=usage_metadata,
            pricing_currency=pricing_currency,
            price_protection_enabled=args.price_protection,
            price_evolution=price_evolution_result,
            reservation_inventory=reservation_inventory,
            pricesheet_metadata=pricesheet_metadata,
        )
        logger.info(
            "[workbook] Wrote %d Storage rows, %d Virtual Machines rows, %d Storage "
            "pricesheet rows, and %d VM pricesheet rows",
            len(storage_report),
            len(vm_report),
            (
                len(storage_pricesheet_report)
                if storage_pricesheet_report is not None
                else 0
            ),
            len(vm_pricesheet_report) if vm_pricesheet_report is not None else 0,
        )
    logger.info(
        "[complete] Report written to %s (%.2fs total)",
        args.output.resolve(), perf_counter() - started,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
