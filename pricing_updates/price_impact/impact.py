"""Usage enrichment, pricing rules, protection and impact summaries."""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Literal

import pandas as pd

from core.business_entity import add_business_entity_column
from core.region_translation import RegionVocabulary, translate_location_column
from core.units_of_measure import get_unit_price

logger = logging.getLogger(__name__)

SUMMARY_IMPACT_COLUMNS = [
    "Storage Regional Price Update",
    "Virtual Machines Regional Price Update",
    "v1-v2 Price Update",
]


RESERVATION_INVENTORY_COLUMNS = {
    "Reservation Id": "_ReservationIdKey",
    "Reservation order Id": "_ProductOrderIdKey",
    "Purchase date": "ReservationPurchaseDate",
    "Expiration date": "ReservationExpirationDate",
    "Product name": "_ReservationProductName",
}


def _impact_summary_input(
    storage_report: pd.DataFrame,
    vm_report: pd.DataFrame,
) -> pd.DataFrame:
    identity_columns = [
        column
        for column in (
            "BusinessEntity",
            "SubscriptionId",
            "SubscriptionName",
            "PricingModel",
        )
        if column in storage_report.columns or column in vm_report.columns
    ]

    storage = storage_report.reindex(
        columns=[*identity_columns, "CostImpact"],
    ).copy()
    storage["_ImpactType"] = SUMMARY_IMPACT_COLUMNS[0]

    vm = vm_report.reindex(
        columns=[*identity_columns, "CostImpact", "Impact"],
    ).copy()
    vm["_ImpactType"] = vm["Impact"].map(
        {
            "Region Price Update": SUMMARY_IMPACT_COLUMNS[1],
            "v1-v2 Price Update": SUMMARY_IMPACT_COLUMNS[2],
        }
    )

    combined = pd.concat([storage, vm], ignore_index=True)
    excluded_pricing_models = (
        combined["PricingModel"]
        .astype("string")
        .str.strip()
        .str.casefold()
        .eq("reservation")
    )
    combined["CostImpact"] = pd.to_numeric(
        combined["CostImpact"],
        errors="raise",
    ).astype("Float64")
    return combined.loc[
        ~excluded_pricing_models
        & combined["_ImpactType"].notna()
        & combined["CostImpact"].notna()
        & combined["CostImpact"].ne(0)
    ].copy()


def _impact_value_columns(summary: pd.DataFrame) -> pd.DataFrame:
    for column in SUMMARY_IMPACT_COLUMNS:
        if column not in summary.columns:
            summary[column] = 0.0
    summary = summary[SUMMARY_IMPACT_COLUMNS].astype("Float64")
    summary["Total Impact"] = summary.sum(axis=1)
    return summary


def _add_total_row(
    summary: pd.DataFrame,
    label_column: str,
) -> pd.DataFrame:
    total = {
        column: summary[column].sum()
        for column in [*SUMMARY_IMPACT_COLUMNS, "Total Impact"]
    }
    total[label_column] = "Total"
    for column in summary.columns:
        total.setdefault(column, pd.NA)
    return pd.concat(
        [summary, pd.DataFrame([total], columns=summary.columns)],
        ignore_index=True,
    )


def get_single_pricing_currency(
    storage_report: pd.DataFrame,
    vm_report: pd.DataFrame,
) -> str:
    for name, report in (
        ("Storage", storage_report),
        ("Virtual Machines", vm_report),
    ):
        if "PricingCurrency" not in report.columns:
            raise ValueError(
                f"{name} report is missing required column PricingCurrency."
            )

    currencies = pd.concat(
        [
            storage_report["PricingCurrency"],
            vm_report["PricingCurrency"],
        ],
        ignore_index=True,
    ).astype("string").str.strip()
    unique_currencies = sorted(
        currencies[currencies.notna() & currencies.ne("")]
        .unique()
        .tolist(),
        key=str.casefold,
    )
    if len(unique_currencies) != 1:
        values = ", ".join(unique_currencies) if unique_currencies else "none"
        raise ValueError(
            "Expected exactly one PricingCurrency across Storage and Virtual "
            f"Machines usage; found {values}."
        )
    return unique_currencies[0]


def build_impact_summaries(
    storage_report: pd.DataFrame,
    vm_report: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame]:
    impact = _impact_summary_input(storage_report, vm_report)

    overall_values = _impact_value_columns(
        impact.pivot_table(
            index="PricingModel",
            columns="_ImpactType",
            values="CostImpact",
            aggfunc="sum",
            fill_value=0,
            observed=True,
        ).rename_axis(columns=None)
    )
    overall = (
        overall_values.sort_values(
            "Total Impact",
            ascending=False,
            kind="stable",
        )
        .reset_index()
        .rename(columns={"PricingModel": "Pricing Model"})
    )
    overall = _add_total_row(overall, "Pricing Model")

    business_summary: pd.DataFrame | None = None
    if "BusinessEntity" in impact.columns:
        business_values = _impact_value_columns(
            impact.pivot_table(
                index="BusinessEntity",
                columns="_ImpactType",
                values="CostImpact",
                aggfunc="sum",
                fill_value=0,
                observed=True,
            ).rename_axis(columns=None)
        )
        business_summary = (
            business_values.sort_values(
                "Total Impact",
                ascending=False,
                kind="stable",
            )
            .reset_index()
        )
        business_summary = _add_total_row(
            business_summary,
            "BusinessEntity",
        )

    subscription_values = _impact_value_columns(
        impact.pivot_table(
            index="SubscriptionId",
            columns="_ImpactType",
            values="CostImpact",
            aggfunc="sum",
            fill_value=0,
            observed=True,
        ).rename_axis(columns=None)
    )
    attributes = impact.drop_duplicates(
        subset=["SubscriptionId"],
        keep="last",
    ).set_index("SubscriptionId")
    subscription_summary = subscription_values.copy()
    subscription_summary.insert(
        0,
        "SubscriptionName",
        attributes["SubscriptionName"].reindex(
            subscription_summary.index
        ),
    )
    if "BusinessEntity" in attributes.columns:
        subscription_summary.insert(
            0,
            "BusinessEntity",
            attributes["BusinessEntity"].reindex(
                subscription_summary.index
            ),
        )
    subscription_summary = (
        subscription_summary.sort_values(
            "Total Impact",
            ascending=False,
            kind="stable",
        )
        .reset_index()
    )
    subscription_summary = _add_total_row(
        subscription_summary,
        "SubscriptionId",
    )
    return overall, business_summary, subscription_summary


PRICE_INCREASE_BY_REGION = {
    "EU North": 17,
    "EU West": 9,
    "FR Central": 11,
    "FR South": 11,
    "NO East": 9,
    "NO West": 9,
    "AP Southeast": 8,
}


# MeterSubCategory values (matched case-insensitively) of announced Storage
# exclusions. The trailing comment records where each value was observed in
# usage details and pricesheets: MCA only, EA only, or both. EA and MCA name
# some sub-categories differently, so both spellings are listed. Assumption:
# "Inter-Region Bandwidth" (EA) / "Storage - Inter-Region Bandwidth" (MCA) are
# not announced as excluded, so they stay impacted.
EXCLUDED_STORAGE_METER_SUBCATEGORIES = (
    "Bandwidth",  # EA only (MCA: "Storage - Bandwidth")
    "Storage - Bandwidth",  # MCA only (EA: "Bandwidth")
    "Blob Features",  # both
    "Change Feed",  # both
    "Queues",  # both
    "Queues v2",  # both
    "Standard Page Blob",  # both
    "General Block Blob",  # both
    "Tables",  # both
    # Assumed to be the announced "Transaction Optimized SKU": no meter carries
    # that name, and Files usage has almost no MeterRegion. Both.
    "Files",
    "Azure Container Storage",  # MCA only (EA: "Azure Container")
    "Azure Container",  # EA only (MCA: "Azure Container Storage")
    "Azure Managed Lustre",  # both
    "Import/Export",  # both
    "Storage Actions",  # MCA only (EA: "Actions")
    "Actions",  # EA only (MCA: "Storage Actions")
    "Storage Discovery",  # MCA only (EA: "Discovery")
    "Discovery",  # EA only (MCA: "Storage Discovery")
)


EXCLUDED_AZURE_NETAPP_FILES_METER_NAME_SUBSTRING = (
    "Cross Region Replication"
)


RESERVABLE_AZURE_NETAPP_FILES_METER_NAMES = (
    "Premium Capacity",
    "Standard Capacity",
    "Ultra Capacity",
)


RESERVABLE_STORAGE_TERMS = ("P1Y", "P3Y")


LEGACY_PRICE_UPDATE_VM_PATTERN = re.compile(
    r"^(?:virtual machines )?"
    r"(?:bs(?: vdi)?|d(?:/ds)?|ds|f(?:/fs)?|fs|g(?:/gs)?|gs|"
    r"ls|np|hcs|av2|amv2|dv2(?:/dsv2)?|dsv2|fsv2|lsv2)"
    r"(?: promo)? series(?:\b.*)?$",
    re.IGNORECASE,
)


EXCLUDED_REGION_PRICE_UPDATE_VM_PATTERN = re.compile(
    r"^(?:virtual machines )?"
    r"(?:dsv7|ddsv7|dlsv7|dldsv7|esv7|edsv7)"
    r"(?:-| )series(?:\b.*)?$",
    re.IGNORECASE,
)


IMPACTED_VM_RESULTS = (
    "Region Price Update",
    "v1-v2 Price Update",
)


def normalize_identifier(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.casefold()
    return normalized.mask(normalized.eq(""))


def validate_amortized_reservation_usage(report: pd.DataFrame) -> None:
    charge_types = report["ChargeType"].astype("string").str.casefold()
    unsupported = charge_types.isin(("purchase", "refund"))
    if unsupported.any():
        values = sorted(
            report.loc[unsupported, "ChargeType"]
            .astype("string")
            .dropna()
            .unique()
        )
        raise ValueError(
            "Reservation inventory enrichment requires amortized usage data; "
            f"found unsupported ChargeType values: {', '.join(values)}."
        )


def enrich_reservations(
    report: pd.DataFrame,
    reservation_inventory: pd.DataFrame,
    *,
    fill_vm_size: bool = False,
) -> pd.DataFrame:
    validate_amortized_reservation_usage(report)
    enriched = report.assign(
        _ReservationIdKey=normalize_identifier(report["ReservationId"]),
        _ProductOrderIdKey=normalize_identifier(report["ProductOrderId"]),
    ).merge(
        reservation_inventory[list(RESERVATION_INVENTORY_COLUMNS.values())],
        how="left",
        on=["_ReservationIdKey", "_ProductOrderIdKey"],
        validate="many_to_one",
    )

    reservation_rows = (
        enriched["PricingModel"]
        .astype("string")
        .str.casefold()
        .eq("reservation")
        & enriched["_ReservationIdKey"].notna()
    )
    unmatched = (
        reservation_rows
        & enriched["ReservationExpirationDate"].isna()
    )
    if unmatched.any():
        logger.warning(
            "[impact] %d reservation rows did not match the reservation inventory.",
            int(unmatched.sum()),
        )

    if fill_vm_size:
        missing_vm_size = (
            reservation_rows
            & enriched["VMSize"].isna()
            & enriched["_ReservationProductName"].notna()
        )
        enriched.loc[missing_vm_size, "VMSize"] = enriched.loc[
            missing_vm_size,
            "_ReservationProductName",
        ]

    return enriched.drop(
        columns=[
            "_ReservationIdKey",
            "_ProductOrderIdKey",
            "_ReservationProductName",
        ]
    )


def add_region_controls(report: pd.DataFrame) -> pd.DataFrame:
    report = translate_location_column(
        report,
        source_column="MeterRegion",
        target_column="UniformizedMeterRegion",
        target=RegionVocabulary.METER_REGION,
        strict=False,
    )
    report["RegionImpacted"] = report["UniformizedMeterRegion"].isin(
        PRICE_INCREASE_BY_REGION
    ).map({True: "Yes", False: "No"})
    report["MeterRegionPresent"] = (
        report["MeterRegion"]
        .astype("string")
        .str.strip()
        .ne("")
        .fillna(False)
        .map({True: "Yes", False: "No"})
    )
    return report


def _add_impact_result(
    report: pd.DataFrame,
    control_columns: list[str],
) -> pd.DataFrame:
    report["Impacted"] = (
        report[control_columns]
        .eq("Yes")
        .all(axis=1)
        .map({True: "Yes", False: "No"})
    )
    report["PriceIncreasePercent"] = (
        report["UniformizedMeterRegion"]
        .map(PRICE_INCREASE_BY_REGION)
        .astype("Int64")
        .where(report["Impacted"].eq("Yes"))
    )
    return _add_storage_cost_impact(report)


def _add_storage_cost_impact(report: pd.DataFrame) -> pd.DataFrame:
    """Calculate Storage impact, applying converted BasePrice when present."""
    cost = pd.to_numeric(
        report["CostInPricingCurrency"],
        errors="raise",
    ).astype("Float64")
    increase_percent = pd.to_numeric(
        report["PriceIncreasePercent"],
        errors="raise",
    ).astype("Float64")
    base_price_column = "BasePriceInUsageUnitPAYG"
    if base_price_column not in report.columns:
        report["CostImpact"] = (
            cost * increase_percent / 100
        ).astype("Float64")
        return report.drop(
            columns=[
                "PriceProtectionEnforced",
                "ActualPriceIncreasePercent",
            ],
            errors="ignore",
        )

    if "Quantity" not in report.columns:
        raise ValueError(
            "Storage report is missing Quantity for price protection."
        )
    return _apply_price_protection(report)


def _apply_price_protection(report: pd.DataFrame) -> pd.DataFrame:
    """Apply a selected BasePrice only to impacted OnDemand usage."""
    required_columns = {
        "BasePriceInUsageUnitPAYG",
        "CostInPricingCurrency",
        "PriceIncreasePercent",
        "PricingModel",
        "Quantity",
        "_BasePriceSelectionStatus",
    }
    missing_columns = required_columns.difference(report.columns)
    if missing_columns:
        raise ValueError(
            "Report is missing required price-protection columns: "
            + ", ".join(sorted(missing_columns))
        )

    cost = pd.to_numeric(
        report["CostInPricingCurrency"],
        errors="raise",
    ).astype("Float64")
    increase_percent = pd.to_numeric(
        report["PriceIncreasePercent"],
        errors="raise",
    ).astype("Float64")
    quantity = pd.to_numeric(
        report["Quantity"],
        errors="raise",
    ).astype("Float64")
    base_price = pd.to_numeric(
        report["BasePriceInUsageUnitPAYG"],
        errors="raise",
    ).astype("Float64")
    impact_applicable = increase_percent.notna()
    valid_quantity = quantity.gt(0)
    is_on_demand = (
        report["PricingModel"]
        .astype("string")
        .str.strip()
        .str.casefold()
        .eq("ondemand")
        .fillna(False)
    )
    selection_status = report["_BasePriceSelectionStatus"].astype("string")
    protection_evaluated = (
        impact_applicable
        & valid_quantity
        & is_on_demand
        & selection_status.eq("Available")
        & base_price.notna()
    )
    current_unit_price = cost / quantity.where(quantity.gt(0))
    unprotected_unit_price = current_unit_price * (
        1 + increase_percent / 100
    )
    protection_enforced = (
        protection_evaluated
        & unprotected_unit_price.gt(base_price)
    )
    final_unit_price = unprotected_unit_price.where(
        ~protection_enforced,
        base_price,
    )
    nominal_impact = (
        cost * increase_percent / 100
    ).where(impact_applicable & valid_quantity)
    protected_impact = (
        quantity * final_unit_price - cost
    ).where(protection_evaluated)
    report["CostImpact"] = nominal_impact.where(
        ~protection_evaluated,
        protected_impact,
    ).astype("Float64")

    report["PriceProtectionStatus"] = pd.Series(
        pd.NA,
        index=report.index,
        dtype="string",
    )
    report.loc[
        ~impact_applicable,
        "PriceProtectionStatus",
    ] = "Not Applicable - No Impact"
    report.loc[
        impact_applicable & ~valid_quantity,
        "PriceProtectionStatus",
    ] = "Not Applicable - Invalid Quantity"
    report.loc[
        impact_applicable & valid_quantity & ~is_on_demand,
        "PriceProtectionStatus",
    ] = "Not Applicable - Non-Consumption"
    unresolved = impact_applicable & valid_quantity & is_on_demand
    report.loc[
        unresolved & selection_status.eq("Unsupported Plan"),
        "PriceProtectionStatus",
    ] = "Unsupported Plan"
    report.loc[
        unresolved & selection_status.eq("BasePrice Unavailable"),
        "PriceProtectionStatus",
    ] = "BasePrice Unavailable"
    report.loc[
        protection_evaluated & protection_enforced,
        "PriceProtectionStatus",
    ] = "Enforced"
    report.loc[
        protection_evaluated & ~protection_enforced,
        "PriceProtectionStatus",
    ] = "Not Enforced"

    report["PriceProtectionEnforced"] = pd.Series(
        pd.NA,
        index=report.index,
        dtype="string",
    )
    report.loc[protection_evaluated, "PriceProtectionEnforced"] = (
        protection_enforced.loc[protection_evaluated]
        .map({True: "Yes", False: "No"})
        .astype("string")
    )
    report["ActualPriceIncreasePercent"] = (
        report["CostImpact"] / cost.where(cost.ne(0)) * 100
    ).where(impact_applicable & valid_quantity).astype("Float64")
    return report.drop(columns="_BasePriceSelectionStatus")


def add_vm_impact_result(report: pd.DataFrame) -> pd.DataFrame:
    required_columns = {"MeterSubCategory", "Product"}
    missing_columns = required_columns.difference(report.columns)
    if missing_columns:
        raise ValueError(
            "VM report is missing required classification columns: "
            + ", ".join(sorted(missing_columns))
        )

    meter_subcategory = (
        report["MeterSubCategory"].astype("string").str.strip()
    )
    product = report["Product"].astype("string").str.strip()
    classification_value = meter_subcategory.mask(
        meter_subcategory.eq("")
    ).fillna(product.mask(product.eq("")))
    has_classification = classification_value.notna()
    is_legacy = classification_value.str.fullmatch(
        LEGACY_PRICE_UPDATE_VM_PATTERN
    ).fillna(False)
    is_excluded_region = classification_value.str.fullmatch(
        EXCLUDED_REGION_PRICE_UPDATE_VM_PATTERN
    ).fillna(False)

    report["NotExcludedRegionPriceUpdate"] = (
        has_classification & ~is_excluded_region
    ).map({True: "Yes", False: "No"})
    is_region_update = (
        report["RegionImpacted"].eq("Yes")
        & report["MeterRegionPresent"].eq("Yes")
        & has_classification
        & ~is_legacy
        & ~is_excluded_region
    )

    report["Impact"] = "No Impact"
    report.loc[is_region_update, "Impact"] = "Region Price Update"
    report.loc[is_legacy, "Impact"] = "v1-v2 Price Update"

    report["PriceIncreasePercent"] = pd.Series(
        pd.NA,
        index=report.index,
        dtype="Int64",
    )
    report.loc[is_region_update, "PriceIncreasePercent"] = (
        report.loc[is_region_update, "UniformizedMeterRegion"]
        .map(PRICE_INCREASE_BY_REGION)
        .astype("Int64")
    )
    report.loc[is_legacy, "PriceIncreasePercent"] = 25
    return _add_vm_cost_impact(report)


def _add_vm_cost_impact(report: pd.DataFrame) -> pd.DataFrame:
    """Calculate VM cost impact, applying PAYG BasePrice when available."""
    required_columns = {
        "CostInPricingCurrency",
        "Quantity",
        "PriceIncreasePercent",
    }
    missing_columns = required_columns.difference(report.columns)
    if missing_columns:
        raise ValueError(
            "VM report is missing required cost-impact columns: "
            + ", ".join(sorted(missing_columns))
        )

    cost = pd.to_numeric(
        report["CostInPricingCurrency"],
        errors="raise",
    ).astype("Float64")
    quantity = pd.to_numeric(
        report["Quantity"],
        errors="raise",
    ).astype("Float64")
    increase_percent = pd.to_numeric(
        report["PriceIncreasePercent"],
        errors="raise",
    ).astype("Float64")
    applicable = increase_percent.notna() & quantity.gt(0)
    base_price_column = "BasePriceInUsageUnitPAYG"
    if base_price_column not in report.columns:
        report["CostImpact"] = (
            cost * increase_percent / 100
        ).where(applicable).astype("Float64")
        return report.drop(
            columns=[
                "PriceProtectionEnforced",
                "ActualPriceIncreasePercent",
            ],
            errors="ignore",
        )

    return _apply_price_protection(report)


def remove_no_impact_details(
    storage_report: pd.DataFrame,
    vm_report: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return report details containing only rows with a pricing impact."""
    if "Impacted" not in storage_report.columns:
        raise ValueError("Storage report is missing the Impacted column.")
    if "Impact" not in vm_report.columns:
        raise ValueError("VM report is missing the Impact column.")

    impacted_storage = storage_report.loc[
        storage_report["Impacted"].eq("Yes")
    ].copy()
    impacted_vms = vm_report.loc[
        vm_report["Impact"].isin(IMPACTED_VM_RESULTS)
    ].copy()
    return impacted_storage, impacted_vms


def add_reservable_status(
    storage_report: pd.DataFrame,
    storage_pricesheet: pd.DataFrame,
) -> pd.DataFrame:
    """Classify Storage and ANF usage using exact reservation MeterIds."""
    required_report_columns = {
        "MeterCategory",
        "MeterName",
        "MeterId",
    }
    missing_report_columns = required_report_columns.difference(
        storage_report.columns
    )
    if missing_report_columns:
        raise ValueError(
            "Storage report is missing required reservability columns: "
            + ", ".join(sorted(missing_report_columns))
        )

    required_pricesheet_columns = {"MeterId", "PriceType", "Term"}
    missing_pricesheet_columns = required_pricesheet_columns.difference(
        storage_pricesheet.columns
    )
    if missing_pricesheet_columns:
        raise ValueError(
            "Storage pricesheet is missing required reservability columns: "
            + ", ".join(sorted(missing_pricesheet_columns))
        )

    price_types = normalize_identifier(storage_pricesheet["PriceType"])
    terms = normalize_identifier(storage_pricesheet["Term"])
    reservation_meter_ids = set(
        normalize_identifier(
            storage_pricesheet.loc[
                price_types.eq("reservedinstance")
                & terms.isin(
                    {
                        value.casefold()
                        for value in RESERVABLE_STORAGE_TERMS
                    }
                ),
                "MeterId",
            ]
        ).dropna()
    )

    meter_categories = normalize_identifier(
        storage_report["MeterCategory"]
    )
    anf_meter_names = normalize_identifier(storage_report["MeterName"])
    capacity_candidates = (
        meter_categories.eq("azure netapp files")
        & anf_meter_names.isin(
            {
                value.casefold()
                for value in RESERVABLE_AZURE_NETAPP_FILES_METER_NAMES
            }
        )
    )
    exact_match = normalize_identifier(
        storage_report["MeterId"]
    ).isin(reservation_meter_ids)

    result = storage_report.copy()
    result["Reservable"] = "No"
    result.loc[capacity_candidates, "Reservable"] = "Not identified"
    result.loc[exact_match, "Reservable"] = "Yes"
    return result


BasePriceCandidate = tuple[str, float]


BasePriceCandidatesByKey = dict[
    tuple[str, ...],
    list[BasePriceCandidate],
]


class BasePricePolicy(Enum):
    CURRENT_USAGE_PROTECTION = "current_usage_protection"
    RI_PAYG_FALLBACK = "ri_payg_fallback"


def validate_base_price_columns(
    usage_report: pd.DataFrame,
    pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
    *,
    policy: BasePricePolicy,
    context: str,
) -> None:
    """Validate required pricing fields without selecting or converting prices."""
    required_usage_columns = {
        "MeterId",
        "PricingModel",
        "UnitOfMeasure",
        "Quantity",
    }
    if contract_type == "ea":
        required_usage_columns.add("OfferId")
    elif policy == BasePricePolicy.CURRENT_USAGE_PROTECTION:
        required_usage_columns.add("ProductOrderName")
    missing_usage_columns = required_usage_columns.difference(
        usage_report.columns
    )
    if missing_usage_columns:
        raise ValueError(
            f"{context} report is missing required pricing columns: "
            + ", ".join(sorted(missing_usage_columns))
        )

    required_pricesheet_columns = {
        "MeterId",
        "PriceType",
        "UnitOfMeasure",
        "BasePrice",
    }
    if contract_type == "ea":
        required_pricesheet_columns.add("OfferId")
    missing_pricesheet_columns = required_pricesheet_columns.difference(
        pricesheet.columns
    )
    if missing_pricesheet_columns:
        raise ValueError(
            f"{context} pricesheet is missing required pricing columns: "
            + ", ".join(sorted(missing_pricesheet_columns))
        )


def _build_base_price_candidates(
    pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
) -> tuple[
    BasePriceCandidatesByKey,
    dict[str, list[BasePriceCandidate]],
]:
    prices = pricesheet.copy()
    prices["_MeterIdKey"] = normalize_identifier(prices["MeterId"])
    prices["_PriceTypeKey"] = normalize_identifier(prices["PriceType"])
    if contract_type == "ea":
        prices["_OfferIdKey"] = normalize_identifier(prices["OfferId"])

    candidate_columns = ["_MeterIdKey"]
    if contract_type == "ea":
        candidate_columns.append("_OfferIdKey")
    candidate_columns.extend(["UnitOfMeasure", "BasePrice"])
    consumption = prices.loc[
        prices["_PriceTypeKey"].eq("consumption"),
        candidate_columns,
    ]

    candidates_by_key: BasePriceCandidatesByKey = {}
    for row in consumption.itertuples(index=False, name=None):
        *raw_key, unit, base_price = row
        if (
            any(pd.isna(value) for value in raw_key)
            or pd.isna(unit)
            or pd.isna(base_price)
        ):
            continue
        key = tuple(str(value) for value in raw_key)
        candidates_by_key.setdefault(key, []).append(
            (str(unit), float(base_price))
        )

    candidates_by_meter: dict[str, list[BasePriceCandidate]] = {}
    for key, candidates in candidates_by_key.items():
        candidates_by_meter.setdefault(key[0], []).extend(candidates)
    return candidates_by_key, candidates_by_meter


def _select_base_price_candidates(
    contract_type: Literal["ea", "mca"],
    meter_id: str,
    offer_id: object,
    product_order_name: object,
    candidates_by_key: BasePriceCandidatesByKey,
    candidates_by_meter: dict[str, list[BasePriceCandidate]],
    *,
    policy: BasePricePolicy,
) -> tuple[list[BasePriceCandidate], bool, str | None]:
    if contract_type == "ea":
        candidates = (
            candidates_by_meter.get(meter_id, [])
            if pd.isna(offer_id)
            else candidates_by_key.get(
                (meter_id, str(offer_id)),
                [],
            )
        )
        choose_highest = policy == BasePricePolicy.CURRENT_USAGE_PROTECTION
        return candidates, choose_highest, None

    if policy == BasePricePolicy.RI_PAYG_FALLBACK:
        return candidates_by_meter.get(meter_id, []), False, None

    choose_highest, unavailable_status = _select_mca_plan_price_strategy(
        product_order_name
    )
    if unavailable_status is not None:
        return [], False, "Unsupported Plan"
    assert choose_highest is not None
    return candidates_by_meter.get(meter_id, []), choose_highest, None


def _select_mca_plan_price_strategy(
    product_order_name: object,
) -> tuple[bool | None, str | None]:
    if pd.isna(product_order_name) or not str(product_order_name).strip():
        return None, "Unsupported Plan"
    normalized_plan = str(product_order_name).strip().casefold()
    return (
        normalized_plan
        in {
            "azure plan",
            "microsoft azure plan",
        },
        None,
    )


def _convert_base_price_candidates(
    candidates: list[BasePriceCandidate],
    usage_unit: str,
    meter_id: str,
    *,
    choose_highest: bool,
    context: str,
) -> float | None:
    converted: list[float] = []
    for pricesheet_unit, base_price in candidates:
        try:
            converted.append(
                get_unit_price(
                    pricesheet_unit,
                    base_price,
                    usage_unit,
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"Cannot convert {context} pricesheet UnitOfMeasure "
                f"{pricesheet_unit!r} to usage UnitOfMeasure "
                f"{usage_unit!r} for MeterId {meter_id!r}."
            ) from exc
    if not converted:
        return None
    return max(converted) if choose_highest else min(converted)


def add_base_prices_in_usage_unit(
    usage_report: pd.DataFrame,
    pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
    *,
    policy: BasePricePolicy,
    context: str,
) -> pd.DataFrame:
    """Select and convert PAYG BasePrice into each usage row's unit."""
    validate_base_price_columns(
        usage_report,
        pricesheet,
        contract_type,
        policy=policy,
        context=context,
    )
    candidates_by_key, candidates_by_meter = _build_base_price_candidates(
        pricesheet,
        contract_type,
    )

    result = usage_report.copy()
    meter_ids = normalize_identifier(result["MeterId"])
    usage_units = result["UnitOfMeasure"].astype("string").str.strip()
    pricing_models = (
        result["PricingModel"].astype("string").str.strip().str.casefold()
    )
    requires_on_demand = policy == BasePricePolicy.CURRENT_USAGE_PROTECTION
    offer_ids = (
        normalize_identifier(result["OfferId"])
        if contract_type == "ea"
        else pd.Series(pd.NA, index=result.index, dtype="string")
    )
    product_order_names = (
        result["ProductOrderName"].astype("string").str.strip()
        if contract_type == "mca" and requires_on_demand
        else pd.Series(pd.NA, index=result.index, dtype="string")
    )
    converted_values: list[object] = []
    selection_statuses: list[str] = []
    for meter_id, offer_id, product_order_name, usage_unit, pricing_model in zip(
        meter_ids,
        offer_ids,
        product_order_names,
        usage_units,
        pricing_models,
    ):
        if requires_on_demand and pricing_model != "ondemand":
            converted_values.append(pd.NA)
            selection_statuses.append("Not Applicable - Non-Consumption")
            continue
        if pd.isna(meter_id) or pd.isna(usage_unit):
            converted_values.append(pd.NA)
            selection_statuses.append("BasePrice Unavailable")
            continue
        meter_id_value = str(meter_id)
        candidates, choose_highest, unavailable_status = (
            _select_base_price_candidates(
                contract_type,
                meter_id_value,
                offer_id,
                product_order_name,
                candidates_by_key,
                candidates_by_meter,
                policy=policy,
            )
        )
        if unavailable_status is not None:
            converted_values.append(pd.NA)
            selection_statuses.append(unavailable_status)
            continue
        converted = _convert_base_price_candidates(
            candidates,
            str(usage_unit),
            meter_id_value,
            choose_highest=choose_highest,
            context=context,
        )
        if converted is None:
            converted_values.append(pd.NA)
            selection_statuses.append("BasePrice Unavailable")
            continue
        converted_values.append(converted)
        selection_statuses.append("Available")

    result["BasePriceInUsageUnitPAYG"] = pd.Series(
        pd.array(converted_values, dtype="Float64"),
        index=result.index,
    )
    result["_BasePriceSelectionStatus"] = pd.Series(
        selection_statuses,
        index=result.index,
        dtype="string",
    )
    return result


def add_storage_base_prices(
    storage_report: pd.DataFrame,
    storage_pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
) -> pd.DataFrame:
    """Add converted EA or MCA PAYG BasePrice and recalculate Storage impact."""
    result = add_base_prices_in_usage_unit(
        storage_report,
        storage_pricesheet,
        contract_type,
        policy=BasePricePolicy.CURRENT_USAGE_PROTECTION,
        context="Storage",
    )
    return _add_storage_cost_impact(result)


def add_hourly_prices(pricesheet: pd.DataFrame) -> pd.DataFrame:
    """Add hourly prices and the discount from the PAYG market price."""
    if "UnitOfMeasure" not in pricesheet.columns:
        raise ValueError(
            "VM pricesheet is missing required column UnitOfMeasure."
        )
    if "UnitPrice" not in pricesheet.columns:
        raise ValueError("VM pricesheet is missing required column UnitPrice.")

    result = pricesheet.copy()
    units = result["UnitOfMeasure"].astype("string").str.strip()
    price_types = result.get(
        "PriceType",
        pd.Series(pd.NA, index=result.index, dtype="string"),
    ).astype("string")
    is_reserved_instance = price_types.str.casefold().eq(
        "reservedinstance"
    ).fillna(False)
    non_reservation_priced_rows = (
        result["UnitPrice"].notna() & ~is_reserved_instance
    )
    if (non_reservation_priced_rows & units.isna()).any():
        raise ValueError(
            "VM pricesheet contains UnitPrice values without UnitOfMeasure."
        )

    conversion_factors: dict[str, float] = {}
    non_reservation_units = units[~is_reserved_instance]
    for unit in sorted(
        non_reservation_units.dropna().unique(),
        key=str.casefold,
    ):
        try:
            conversion_factors[unit] = get_unit_price(
                unit,
                1.0,
                "1 Hour",
            )
        except ValueError as exc:
            raise ValueError(
                f"Cannot convert VM pricesheet UnitOfMeasure {unit!r} "
                "to an hourly price."
            ) from exc

    factors = units.map(conversion_factors).astype("Float64")
    if is_reserved_instance.any():
        if "Term" not in result.columns:
            raise ValueError(
                "VM pricesheet is missing Term for ReservedInstance prices."
            )
        reservation_terms = (
            result.loc[is_reserved_instance, "Term"]
            .astype("string")
            .str.strip()
        )
        invalid_terms = reservation_terms[
            ~reservation_terms.str.fullmatch(r"P[1-9]\d*Y", na=False)
        ]
        if not invalid_terms.empty:
            invalid_values = sorted(
                invalid_terms.fillna("<blank>").unique(),
                key=str.casefold,
            )
            raise ValueError(
                "Unsupported ReservedInstance Term value(s): "
                + ", ".join(invalid_values)
            )
        reservation_years = reservation_terms.str.extract(
            r"P([1-9]\d*)Y",
            expand=False,
        ).astype("Int64")
        factors.loc[is_reserved_instance] = (
            1 / (reservation_years * 365 * 24)
        ).astype("Float64")

    result["HourlyUnitPrice"] = (
        pd.to_numeric(result["UnitPrice"], errors="raise") * factors
    ).astype("Float64")
    result["Discount"] = pd.Series(
        pd.NA,
        index=result.index,
        dtype="Float64",
    )
    if "MarketPrice" in result.columns:
        unit_price = pd.to_numeric(
            result["UnitPrice"],
            errors="raise",
        ).astype("Float64")
        market_price = pd.to_numeric(
            result["MarketPrice"],
            errors="raise",
        ).astype("Float64")
        valid_market_price = market_price.notna() & market_price.ne(0)
        result.loc[valid_market_price, "Discount"] = (
            (
                unit_price.loc[valid_market_price]
                - market_price.loc[valid_market_price]
            )
            / market_price.loc[valid_market_price]
        ).astype("Float64")
    if "BasePrice" in result.columns:
        base_price = pd.to_numeric(
            result["BasePrice"],
            errors="raise",
        ).astype("Float64")
        result["HourlyBasePrice"] = (
            base_price * factors
        ).astype("Float64")
    return result


def add_vm_base_prices(
    vm_report: pd.DataFrame,
    vm_pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
) -> pd.DataFrame:
    """Add converted EA or MCA PAYG BasePrice and recalculate VM impact."""
    result = add_base_prices_in_usage_unit(
        vm_report,
        vm_pricesheet,
        contract_type,
        policy=BasePricePolicy.CURRENT_USAGE_PROTECTION,
        context="VM",
    )
    return _add_vm_cost_impact(result)


def add_storage_impact_result(report: pd.DataFrame) -> pd.DataFrame:
    report = add_region_controls(report)
    excluded_subcategories = {
        value.casefold() for value in EXCLUDED_STORAGE_METER_SUBCATEGORIES
    }
    is_excluded_subcategory = (
        report["MeterSubCategory"]
        .astype("string")
        .str.casefold()
        .isin(excluded_subcategories)
    )
    is_cross_region_replication = (
        report["MeterCategory"]
        .astype("string")
        .str.casefold()
        .eq("azure netapp files")
        & report["MeterName"]
        .astype("string")
        .str.contains(
            EXCLUDED_AZURE_NETAPP_FILES_METER_NAME_SUBSTRING,
            case=False,
            regex=False,
            na=False,
        )
    )
    report["NotExcludedStorageService"] = (
        ~(is_excluded_subcategory | is_cross_region_replication)
    ).map({True: "Yes", False: "No"})
    result = _add_impact_result(
        report,
        [
            "RegionImpacted",
            "MeterRegionPresent",
            "NotExcludedStorageService",
        ],
    )
    return result


def prepare_storage_report(
    usage: pd.DataFrame,
    reservation_inventory: pd.DataFrame | None = None,
    business_entity_mapping: dict[str, str] | None = None,
) -> pd.DataFrame:
    if reservation_inventory is not None:
        usage = enrich_reservations(usage, reservation_inventory)
    result = add_storage_impact_result(usage)
    if business_entity_mapping is not None:
        add_business_entity_column(result, business_entity_mapping)
    return result


def prepare_vm_report(
    usage: pd.DataFrame,
    reservation_inventory: pd.DataFrame | None = None,
    business_entity_mapping: dict[str, str] | None = None,
) -> pd.DataFrame:
    if reservation_inventory is not None:
        usage = enrich_reservations(
            usage, reservation_inventory, fill_vm_size=True,
        )
    result = add_vm_impact_result(add_region_controls(usage))
    if business_entity_mapping is not None:
        add_business_entity_column(result, business_entity_mapping)
    return result
