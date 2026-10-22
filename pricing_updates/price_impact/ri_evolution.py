"""RI price-evolution calculations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from core.units_of_measure import get_unit_price
from core.usage_data import UsageMetadata
from pricing_updates.price_impact import impact

logger = logging.getLogger(__name__)

PRICE_EVOLUTION_FALLBACKS = ("RI 3Y", "SP 3Y", "PAYG")


@dataclass(frozen=True)
class PriceEvolutionResult:
    projection: pd.DataFrame
    chart_data: pd.DataFrame
    forecast_start: pd.Timestamp
    forecast_end: pd.Timestamp
    horizon_months: int
    fiscal_year_start_month: int
    price_protection_enabled: bool
    # The dashboard hides the Business entity filter when no mapping was added.
    business_entity_provided: bool = True


PRICE_EVOLUTION_EFFECTIVE_MONTH = pd.Timestamp("2027-02-01")


PRICE_EVOLUTION_DIMENSIONS = [
    "ForecastMonth",
    "FallbackPolicy",
    "ImpactClassification",
    "BusinessEntity",
    "SubscriptionId",
    "SubscriptionName",
    "Region",
    "VMFamily",
    "VMSize",
    "FiscalYear",
    "ImpactDriver",
]


PRICE_EVOLUTION_MEASURES = [
    "ReferenceMonthlyCost",
    "ProjectedMonthlyCost",
    "UnpricedCohortCount",
]


EXCEL_MAX_DATA_ROWS = 1_048_575


def warn_if_usage_period_is_not_single_complete_month(
    metadata: UsageMetadata,
) -> None:
    if (
        metadata.usage_start_date is None
        or metadata.usage_end_date is None
    ):
        return

    start = pd.Timestamp(metadata.usage_start_date).normalize()
    end = pd.Timestamp(metadata.usage_end_date).normalize()
    expected_end = start + pd.offsets.MonthEnd(0)
    if start.day != 1 or end != expected_end:
        logger.warning(
            "[validate] Usage period %s through %s is not one complete calendar month; "
            "price evolution will treat the aggregated period as the "
            "reference-month usage without normalization.",
            start.date().isoformat(),
            end.date().isoformat(),
        )


def _price_evolution_month(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value).to_period("M").to_timestamp()


def _price_evolution_fiscal_period(
    month: pd.Timestamp,
    fiscal_year_start_month: int,
) -> tuple[str, str, int]:
    if fiscal_year_start_month == 1:
        fiscal_year = month.year
    else:
        fiscal_year = month.year + int(
            month.month >= fiscal_year_start_month
        )
    fiscal_month = (
        (month.month - fiscal_year_start_month) % 12
    ) + 1
    fiscal_quarter = ((fiscal_month - 1) // 3) + 1
    return f"FY{fiscal_year}", f"Q{fiscal_quarter}", fiscal_month


def _warn_missing_price_evolution_price_types(
    vm_pricesheet: pd.DataFrame,
) -> None:
    price_types = (
        vm_pricesheet.get(
            "PriceType",
            pd.Series(pd.NA, index=vm_pricesheet.index, dtype="string"),
        )
        .astype("string")
        .str.strip()
        .str.casefold()
    )
    if not price_types.eq("reservedinstance").any():
        logger.warning(
            "[forecast] The VM pricesheet contains no ReservedInstance prices; the "
            "RI 3Y scenario will fall back to SP 3Y or PAYG."
        )
    if not price_types.eq("savingsplan").any():
        logger.warning(
            "[forecast] The VM pricesheet contains no SavingsPlan prices; affected "
            "price-evolution scenarios will fall back to PAYG."
        )


def _price_evolution_price_candidates(
    prices: pd.DataFrame,
    price_type: str,
    term: str | None,
    key_columns: list[str],
) -> dict[tuple[str, ...], set[float]]:
    price_types = prices["_PriceTypeKey"]
    terms = prices["_TermKey"]
    mask = price_types.eq(price_type)
    mask &= terms.isna() if term is None else terms.eq(term)
    candidates: dict[tuple[str, ...], set[float]] = {}
    for row in prices.loc[
        mask,
        [*key_columns, "HourlyUnitPrice"],
    ].itertuples(index=False, name=None):
        *raw_key, price = row
        if any(pd.isna(value) for value in raw_key) or pd.isna(price):
            continue
        key = tuple(str(value) for value in raw_key)
        candidates.setdefault(key, set()).add(float(price))
    return candidates


def _select_price_evolution_price(
    candidates: dict[tuple[str, ...], set[float]],
    key: tuple[str, ...] | None,
    description: str,
    *,
    choose_highest: bool | None = None,
) -> float | None:
    if key is None:
        return None
    values = candidates.get(key, set())
    if not values:
        return None
    if len(values) > 1:
        if choose_highest is None:
            raise ValueError(
                f"Conflicting {description} values for key "
                f"{key!r}: {sorted(values)}."
            )
        return max(values) if choose_highest else min(values)
    return next(iter(values))


def validate_price_evolution_price_columns(
    vm_report: pd.DataFrame,
    vm_pricesheet: pd.DataFrame,
) -> None:
    """Validate forecast pricing fields before collecting external lifecycle data."""
    required_usage_columns = {"MeterId", "UnitOfMeasure"}
    missing_usage = required_usage_columns.difference(vm_report.columns)
    if missing_usage:
        raise ValueError(
            "VM report is missing price-evolution columns: "
            + ", ".join(sorted(missing_usage))
        )
    required_price_columns = {
        "MeterId",
        "PriceType",
        "Term",
        "UnitPrice",
        "UnitOfMeasure",
    }
    missing_prices = required_price_columns.difference(vm_pricesheet.columns)
    if missing_prices:
        raise ValueError(
            "VM pricesheet is missing price-evolution columns: "
            + ", ".join(sorted(missing_prices))
        )


def _add_price_evolution_prices(
    vm_report: pd.DataFrame,
    vm_pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
) -> pd.DataFrame:
    validate_price_evolution_price_columns(vm_report, vm_pricesheet)
    _warn_missing_price_evolution_price_types(vm_pricesheet)
    prices = impact.add_hourly_prices(vm_pricesheet)
    prices["_MeterIdKey"] = impact.normalize_identifier(prices["MeterId"])
    prices["_PriceTypeKey"] = impact.normalize_identifier(prices["PriceType"])
    prices["_TermKey"] = impact.normalize_identifier(prices["Term"])
    result = vm_report.copy()
    meter_keys = impact.normalize_identifier(result["MeterId"])
    use_offer = (
        contract_type == "ea"
        and "OfferId" in result.columns
        and "OfferId" in prices.columns
    )
    usage_offer_keys = (
        impact.normalize_identifier(result["OfferId"])
        if use_offer
        else pd.Series(pd.NA, index=result.index, dtype="string")
    )
    if use_offer:
        prices["_OfferIdKey"] = impact.normalize_identifier(prices["OfferId"])

    for option, description in (
        ("PAYG", "PAYG hourly unit price"),
        ("RI 3Y", "RI 3-year hourly unit price"),
        ("SP 3Y", "Savings Plan 3-year hourly unit price"),
    ):
        hourly_column = {
            "PAYG": "HourlyUnitPricePAYG",
            "RI 3Y": "HourlyUnitPriceRI3Yrs",
            "SP 3Y": "HourlyUnitPriceSP3Yrs",
        }[option]
        usage_column = {
            "PAYG": "UnitPricePAYG",
            "RI 3Y": "UnitPriceRI3Yrs",
            "SP 3Y": "UnitPriceSP3Yrs",
        }[option]
        price_type, term = {
            "PAYG": ("consumption", None),
            "RI 3Y": ("reservedinstance", "p3y"),
            "SP 3Y": ("savingsplan", "p3y"),
        }[option]
        meter_candidates = _price_evolution_price_candidates(
            prices,
            price_type,
            term,
            ["_MeterIdKey"],
        )
        exact_candidates: dict[tuple[str, ...], set[float]] = {}
        wildcard_candidates: dict[tuple[str, ...], set[float]] = {}
        if use_offer:
            exact_candidates = _price_evolution_price_candidates(
                prices.loc[prices["_OfferIdKey"].notna()],
                price_type,
                term,
                ["_MeterIdKey", "_OfferIdKey"],
            )
            wildcard_candidates = _price_evolution_price_candidates(
                prices.loc[prices["_OfferIdKey"].isna()],
                price_type,
                term,
                ["_MeterIdKey"],
            )

        hourly_values: list[float | None] = []
        choose_highest = False if option == "PAYG" else None
        for meter_id, offer_id in zip(
            meter_keys,
            usage_offer_keys,
        ):
            meter_key = (
                (str(meter_id),)
                if pd.notna(meter_id)
                else None
            )
            value: float | None = None
            if use_offer and meter_key is not None and pd.notna(offer_id):
                value = _select_price_evolution_price(
                    exact_candidates,
                    (str(meter_id), str(offer_id)),
                    description,
                    choose_highest=choose_highest,
                )
            if value is None and use_offer:
                value = _select_price_evolution_price(
                    wildcard_candidates,
                    meter_key,
                    f"{description} with blank OfferId",
                    choose_highest=choose_highest,
                )
            if value is None:
                value = _select_price_evolution_price(
                    meter_candidates,
                    meter_key,
                    description,
                    choose_highest=choose_highest,
                )
            hourly_values.append(value)
        result[hourly_column] = pd.Series(
            pd.array(hourly_values, dtype="Float64"),
            index=result.index,
        )
        converted: list[float | None] = []
        for hourly_price, usage_unit, raw_meter_id in zip(
            result[hourly_column],
            result["UnitOfMeasure"],
            result["MeterId"],
        ):
            if pd.isna(hourly_price) or pd.isna(usage_unit):
                converted.append(None)
                continue
            try:
                converted.append(
                    get_unit_price(
                        "1 Hour",
                        float(hourly_price),
                        str(usage_unit),
                    )
                )
            except ValueError as exc:
                raise ValueError(
                    f"Cannot convert {description} for MeterId "
                    f"{raw_meter_id!r} into usage unit {usage_unit!r}."
                ) from exc
        result[usage_column] = pd.Series(
            pd.array(converted, dtype="Float64"),
            index=result.index,
        )
    return result


def _add_price_evolution_base_prices(
    vm_report: pd.DataFrame,
    vm_pricesheet: pd.DataFrame,
    contract_type: Literal["ea", "mca"],
) -> pd.DataFrame:
    return impact.add_base_prices_in_usage_unit(
        vm_report,
        vm_pricesheet,
        contract_type,
        policy=impact.BasePricePolicy.RI_PAYG_FALLBACK,
        context="VM RI price evolution",
    )


def _apply_price_evolution_protection(
    candidate_unit_price: float,
    base_price: Any,
    selection_status: Any,
    *,
    enabled: bool,
    applicable: bool,
) -> tuple[float, str, str | None]:
    if not enabled:
        return candidate_unit_price, "Disabled", None
    if not applicable:
        return (
            candidate_unit_price,
            "Not Applicable - No Impact",
            None,
        )
    status = str(selection_status) if pd.notna(selection_status) else (
        "BasePrice Unavailable"
    )
    if status != "Available" or pd.isna(base_price):
        return candidate_unit_price, status, None
    numeric_base_price = float(base_price)
    if candidate_unit_price > numeric_base_price:
        return numeric_base_price, "Enforced", "Yes"
    return candidate_unit_price, "Not Enforced", "No"


def _price_evolution_option_available(
    option: str,
    start_month: pd.Timestamp,
    retirement_month: pd.Timestamp | None,
    prices: dict[str, float | None],
) -> bool:
    if prices[option] is None or pd.isna(prices[option]):
        return False
    if retirement_month is None or option == "PAYG":
        return True
    if option == "RI 3Y":
        cutoff = _price_evolution_month(
            retirement_month - pd.DateOffset(years=3)
        )
        return start_month < cutoff
    if option == "SP 3Y":
        return start_month < retirement_month
    raise ValueError(f"Unsupported price-evolution option: {option}")


def _resolve_price_evolution_fallback(
    selected_fallback: str,
    start_month: pd.Timestamp,
    retirement_month: pd.Timestamp | None,
    prices: dict[str, float | None],
) -> tuple[str | None, str]:
    chains = {
        "RI 3Y": ("RI 3Y", "SP 3Y", "PAYG"),
        "SP 3Y": ("SP 3Y", "PAYG"),
        "PAYG": ("PAYG",),
    }
    for option in chains[selected_fallback]:
        if _price_evolution_option_available(
            option,
            start_month,
            retirement_month,
            prices,
        ):
            if option == selected_fallback:
                return option, "Selected option available"
            return option, f"{selected_fallback} unavailable"
    return None, "PAYG price unavailable"


def _price_evolution_lifecycle_status(
    month: pd.Timestamp,
    retirement_month: pd.Timestamp | None,
) -> str:
    if retirement_month is None:
        return "Unknown"
    if month < retirement_month:
        return "Retirement announced"
    return "Retired"


def _price_evolution_effective_commitment(
    selected_fallback: str,
    fallback_start: pd.Timestamp,
    month: pd.Timestamp,
    retirement_month: pd.Timestamp | None,
    prices: dict[str, float | None],
) -> tuple[str | None, str, pd.Timestamp, pd.Timestamp | None]:
    commitment_start = fallback_start
    while True:
        option, reason = _resolve_price_evolution_fallback(
            selected_fallback,
            commitment_start,
            retirement_month,
            prices,
        )
        if option not in {"RI 3Y", "SP 3Y"}:
            return option, reason, commitment_start, None
        commitment_end = commitment_start + pd.DateOffset(years=3)
        if month < commitment_end:
            return option, reason, commitment_start, commitment_end
        commitment_start = commitment_end


def select_ri_price_evolution_usage(vm_report: pd.DataFrame) -> pd.DataFrame:
    reservation_rows = (
        vm_report["PricingModel"]
        .astype("string")
        .str.strip()
        .str.casefold()
        .eq("reservation")
    )
    result = vm_report.loc[reservation_rows].copy()
    if result.empty:
        raise ValueError("RI price evolution requires VM reservation usage.")
    impact.validate_amortized_reservation_usage(result)
    if (
        ~impact.normalize_identifier(result["ChargeType"]).isin(
            ("usage", "unusedreservation")
        )
    ).any():
        raise ValueError(
            "RI price evolution requires Usage or UnusedReservation charges."
        )
    return result


@dataclass(frozen=True)
class _PriceEvolutionCohort:
    baseline_cost: float
    quantity: float
    expiration_month: pd.Timestamp | None
    retirement_month: pd.Timestamp | None
    increase_factor: float
    prices: dict[str, float | None]
    base_price: object
    base_price_selection_status: object


@dataclass(frozen=True)
class _PriceEvolutionMonthResult:
    projected_cost: float | None
    impact_driver: str
    crosses_retirement: bool
    unavailable_protection_status: str | None


def _prepare_price_evolution_cohort(row: pd.Series) -> _PriceEvolutionCohort:
    expiration = pd.to_datetime(
        row.get("ReservationExpirationDate"),
        errors="coerce",
    )
    retirement = pd.to_datetime(
        row.get("RetirementDate"),
        errors="coerce",
    )
    increase_percent = pd.to_numeric(
        pd.Series([row.get("PriceIncreasePercent")]),
        errors="coerce",
    ).iloc[0]
    return _PriceEvolutionCohort(
        baseline_cost=float(row["CostInPricingCurrency"]),
        quantity=float(row["Quantity"]),
        expiration_month=(
            _price_evolution_month(expiration) if pd.notna(expiration) else None
        ),
        retirement_month=(
            _price_evolution_month(retirement) if pd.notna(retirement) else None
        ),
        increase_factor=(
            1 + float(increase_percent) / 100
            if pd.notna(increase_percent)
            else 1.0
        ),
        prices={
            option: float(row[column]) if pd.notna(row.get(column)) else None
            for option, column in (
                ("RI 3Y", "UnitPriceRI3Yrs"),
                ("SP 3Y", "UnitPriceSP3Yrs"),
                ("PAYG", "UnitPricePAYG"),
            )
        },
        base_price=row.get("BasePriceInUsageUnitPAYG", pd.NA),
        base_price_selection_status=row.get(
            "_BasePriceSelectionStatus",
            "BasePrice Unavailable",
        ),
    )


def _calculate_price_evolution_month(
    cohort: _PriceEvolutionCohort,
    selected_fallback: str,
    month: pd.Timestamp,
    *,
    price_protection_enabled: bool,
) -> _PriceEvolutionMonthResult:
    projected_cost: float | None
    impact_driver = "No impact"
    crosses_retirement = False
    unavailable_protection_status = None
    if cohort.expiration_month is None:
        projected_cost = None
    elif month < cohort.expiration_month:
        projected_cost = cohort.baseline_cost
    else:
        resolved, _, commitment_start, commitment_end = (
            _price_evolution_effective_commitment(
                selected_fallback,
                cohort.expiration_month,
                month,
                cohort.retirement_month,
                cohort.prices,
            )
        )
        if resolved is None:
            projected_cost = None
        else:
            applicable_month = month if resolved == "PAYG" else commitment_start
            if applicable_month is None:
                raise AssertionError(
                    "Resolved fallback has no applicable month."
                )
            price_factor = (
                cohort.increase_factor
                if applicable_month >= PRICE_EVOLUTION_EFFECTIVE_MONTH
                else 1.0
            )
            resolved_price = cohort.prices[resolved]
            if resolved_price is None:
                raise AssertionError("Resolved fallback has no price.")
            projected_unit_price = resolved_price * price_factor
            if resolved == "PAYG":
                projected_unit_price, protection_status, protection_enforced = (
                    _apply_price_evolution_protection(
                        projected_unit_price,
                        cohort.base_price,
                        cohort.base_price_selection_status,
                        enabled=price_protection_enabled,
                        applicable=price_factor != 1.0,
                    )
                )
                if (
                    price_protection_enabled
                    and price_factor != 1.0
                    and protection_enforced is None
                ):
                    unavailable_protection_status = protection_status
            projected_cost = cohort.quantity * projected_unit_price
            impact_driver = f"RI expiration to {resolved}"
            crosses_retirement = (
                resolved == "RI 3Y"
                and cohort.retirement_month is not None
                and commitment_end is not None
                and commitment_end > cohort.retirement_month
            )
    if projected_cost is None:
        impact_driver = "Unpriced"
    elif _price_evolution_lifecycle_status(
        month, cohort.retirement_month,
    ) == "Retired":
        impact_driver = "Retired VM - pricing assumed to continue"
    return _PriceEvolutionMonthResult(
        projected_cost=projected_cost,
        impact_driver=impact_driver,
        crosses_retirement=crosses_retirement,
        unavailable_protection_status=unavailable_protection_status,
    )


def _aggregate_price_evolution_projection(
    projection_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    projection = pd.DataFrame(projection_rows)
    for column in PRICE_EVOLUTION_DIMENSIONS:
        if column != "ForecastMonth":
            projection[column] = (
                projection[column].astype("string").fillna("Unknown")
            )
    projection = projection.groupby(
        PRICE_EVOLUTION_DIMENSIONS,
        dropna=False,
        observed=True,
        as_index=False,
    )[PRICE_EVOLUTION_MEASURES].sum(min_count=1)
    unpriced_count = int(projection["UnpricedCohortCount"].sum())
    if unpriced_count:
        logger.warning(
            "[forecast] RI price evolution contains %d unpriced cohort-month-policy "
            "results. Check reservation expiration dates and fallback prices.",
            unpriced_count,
        )
    if len(projection) > EXCEL_MAX_DATA_ROWS:
        raise ValueError(
            "RI price-evolution projection contains "
            f"{len(projection):,} rows, exceeding the Excel worksheet limit."
        )
    return projection


def _build_price_evolution_projection(
    vm_report: pd.DataFrame,
    usage_metadata: UsageMetadata,
    horizon_months: int,
    fiscal_year_start_month: int,
    price_protection_enabled: bool = False,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    if usage_metadata.usage_end_date is None:
        raise ValueError(
            "RI price evolution requires the usage period end date."
        )
    forecast_start = (
        _price_evolution_month(usage_metadata.usage_end_date)
        + pd.DateOffset(months=1)
    )
    months = pd.date_range(
        forecast_start,
        periods=horizon_months,
        freq="MS",
    )
    forecast_end = months[-1]
    fiscal_years = {
        month: _price_evolution_fiscal_period(
            month, fiscal_year_start_month,
        )[0]
        for month in months
    }
    projection_rows: list[dict[str, Any]] = []
    crossing_commitments: set[tuple[str, str, str]] = set()
    unprotected_payg_cohorts: set[int] = set()
    unavailable_protection_statuses: set[str] = set()

    ri_report = select_ri_price_evolution_usage(vm_report)
    for source_number, (_, row) in enumerate(ri_report.iterrows()):
        cohort = _prepare_price_evolution_cohort(row)
        for selected_fallback in PRICE_EVOLUTION_FALLBACKS:
            for month in months:
                calculation = _calculate_price_evolution_month(
                    cohort,
                    selected_fallback,
                    month,
                    price_protection_enabled=price_protection_enabled,
                )
                if calculation.crosses_retirement:
                    crossing_commitments.add(
                        (
                            str(row.get("MeterId") or ""),
                            str(row.get("VMSize") or ""),
                            "RI 3Y",
                        )
                    )
                if calculation.unavailable_protection_status is not None:
                    unprotected_payg_cohorts.add(source_number)
                    unavailable_protection_statuses.add(
                        calculation.unavailable_protection_status
                    )
                projection_rows.append(
                    {
                        "ForecastMonth": month,
                        "FiscalYear": fiscal_years[month],
                        "FallbackPolicy": selected_fallback,
                        "BusinessEntity": row.get(
                            "BusinessEntity",
                            "Unknown",
                        ),
                        "SubscriptionId": row.get("SubscriptionId"),
                        "SubscriptionName": row.get("SubscriptionName"),
                        "Region": row.get("UniformizedMeterRegion"),
                        "VMFamily": row.get("VMFamily"),
                        "VMSize": row.get("VMSize"),
                        "ImpactClassification": row.get(
                            "Impact",
                            "No Impact",
                        ),
                        "ReferenceMonthlyCost": cohort.baseline_cost,
                        "ProjectedMonthlyCost": calculation.projected_cost,
                        "ImpactDriver": calculation.impact_driver,
                        "UnpricedCohortCount": int(
                            calculation.projected_cost is None
                        ),
                    }
                )

    if crossing_commitments:
        logger.warning(
            "[forecast] %d VM RI price-evolution cohort(s) contain a new RI "
            "commitment that extends beyond VM retirement.",
            len(crossing_commitments),
        )
    if unprotected_payg_cohorts:
        logger.warning(
            "[forecast] PAYG price protection is unavailable for %d RI cohort(s) "
            "(%s); their PAYG fallbacks use unprotected prices.",
            len(unprotected_payg_cohorts),
            ", ".join(sorted(unavailable_protection_statuses)),
        )
    projection = _aggregate_price_evolution_projection(projection_rows)
    return projection, forecast_start, forecast_end


def _build_price_evolution_chart_data(
    projection: pd.DataFrame,
) -> pd.DataFrame:
    selector_columns = [
        column for column in PRICE_EVOLUTION_DIMENSIONS if column != "ImpactDriver"
    ]
    source = projection.loc[
        projection["UnpricedCohortCount"].eq(0)
    ].copy()
    source["MonthlyImpact"] = (
        source["ProjectedMonthlyCost"] - source["ReferenceMonthlyCost"]
    )
    for column in selector_columns:
        if column == "ForecastMonth":
            continue
        source[column] = (
            source[column].astype("string").fillna("Unknown")
        )

    impact_only = source.loc[
        source["MonthlyImpact"].notna()
        & source["MonthlyImpact"].ne(0),
        selector_columns,
    ].copy()
    retired_impact = (
        source.loc[impact_only.index, "ImpactDriver"].eq(
            "Retired VM - pricing assumed to continue"
        )
    )
    impact_only["DisplayMode"] = "Impact only"
    impact_only["DisplayCategory"] = source.loc[
        impact_only.index,
        "ImpactDriver",
    ].astype("string")
    impact_only.loc[
        retired_impact,
        "DisplayCategory",
    ] = "Impact - retired VM pricing assumption"
    impact_only["DisplayAmount"] = source.loc[
        impact_only.index,
        "MonthlyImpact",
    ].astype("Float64")

    active = source["ImpactDriver"].ne(
        "Retired VM - pricing assumed to continue"
    )
    baseline = source.loc[
        active & source["ReferenceMonthlyCost"].notna(),
        selector_columns,
    ].copy()
    baseline["DisplayMode"] = "Impact + baseline"
    baseline["DisplayCategory"] = "Baseline"
    baseline["DisplayAmount"] = source.loc[
        baseline.index,
        "ReferenceMonthlyCost",
    ].astype("Float64")

    active_impact = source.loc[
        active
        & source["MonthlyImpact"].notna()
        & source["MonthlyImpact"].ne(0),
        selector_columns,
    ].copy()
    active_impact["DisplayMode"] = "Impact + baseline"
    active_impact["DisplayCategory"] = source.loc[
        active_impact.index,
        "ImpactDriver",
    ].astype("string")
    active_impact["DisplayAmount"] = source.loc[
        active_impact.index,
        "MonthlyImpact",
    ].astype("Float64")

    retired = source.loc[
        ~active
        & source["ProjectedMonthlyCost"].notna(),
        selector_columns,
    ].copy()
    retired["DisplayMode"] = "Impact + baseline"
    retired[
        "DisplayCategory"
    ] = "Retired VM - pricing assumed to continue"
    retired["DisplayAmount"] = source.loc[
        retired.index,
        "ProjectedMonthlyCost",
    ].astype("Float64")

    chart_data = pd.concat(
        [impact_only, baseline, active_impact, retired],
        ignore_index=True,
    )
    group_columns = [
        *selector_columns,
        "DisplayMode",
        "DisplayCategory",
    ]
    chart_data = (
        chart_data.groupby(
            group_columns,
            dropna=False,
            observed=True,
            as_index=False,
        )["DisplayAmount"]
        .sum()
    )
    if len(chart_data) > EXCEL_MAX_DATA_ROWS:
        raise ValueError(
            "RI price-evolution chart data contains "
            f"{len(chart_data):,} rows, exceeding the Excel worksheet limit."
        )
    return chart_data


def build_price_evolution(
    ri_report: pd.DataFrame,
    vm_pricesheet: pd.DataFrame,
    usage_metadata: UsageMetadata,
    *,
    horizon_months: int,
    fiscal_year_start_month: int,
    contract_type: Literal["ea", "mca"],
    price_protection_enabled: bool,
) -> PriceEvolutionResult:
    """Build a forecast from selected RI usage.

    Retirement dates come from the ``RetirementDate`` column that
    ``impact.add_vm_impact_result`` derives from the VM series.
    """
    enriched = _add_price_evolution_prices(
        ri_report,
        vm_pricesheet,
        contract_type,
    )
    if price_protection_enabled:
        enriched = _add_price_evolution_base_prices(
            enriched,
            vm_pricesheet,
            contract_type,
        )
    projection, forecast_start, forecast_end = (
        _build_price_evolution_projection(
            enriched,
            usage_metadata,
            horizon_months,
            fiscal_year_start_month,
            price_protection_enabled,
        )
    )
    chart_data = _build_price_evolution_chart_data(projection)
    return PriceEvolutionResult(
        projection=projection,
        chart_data=chart_data,
        forecast_start=forecast_start,
        forecast_end=forecast_end,
        horizon_months=horizon_months,
        fiscal_year_start_month=fiscal_year_start_month,
        price_protection_enabled=price_protection_enabled,
        business_entity_provided="BusinessEntity" in ri_report.columns,
    )
