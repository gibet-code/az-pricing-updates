"""Retrieve Azure compute (Virtual Machine) SKU availability for a subscription.

Queries ``Microsoft.Compute`` resource SKUs
(:meth:`azure.mgmt.compute.ComputeManagementClient.resource_skus.list`) and the
subscription ``/locations`` data, then reports per-region and per-zone
**availability** for every VM SKU — whether the SKU is in catalog and, when it
is, whether it is restricted (and why).

The result is keyed by location for O(1) regional lookup. Region-level metadata
(availability-zone support and the **logical -> physical** zone mapping) is
hoisted up once per location. Per-SKU records key their zones by **physical
zone** (e.g. ``"westeurope-az1"``) so availability is comparable across
subscriptions — logical zone numbers (``"1"/"2"/"3"``) are per-subscription and
map to different physical datacenters in different subscriptions. Callers that
need the logical number look it up in the location-level ``zone_mapping``.

Every status is a machine-friendly dict::

    {
        "in_catalog": bool,            # is the SKU/zone offered at all
        "restricted": bool,            # offered but blocked
        "restriction_reason": str | None,  # raw ARM reasonCode when restricted
        "display_name": str,           # "Available" | "Restricted" | "Not available"
    }

``usable`` is intentionally not stored; callers derive it as
``in_catalog and not restricted``.

Each SKU record also carries retirement metadata (``retirement_status`` /
``retirement_date`` / ``retirement_link``) sourced primarily from the SKUs API
``RetirementDateUtc`` capability, with Azure's published VM-size retirement
announcements as a curated fallback for the SKUs the API does not flag (promo /
constrained-core variants, and some GPU/FPGA/memory families). Retirement is a
lifecycle signal independent of availability: a size can be ``Available`` yet
announced for retirement, in which case new workloads should avoid it.
``retirement_status`` mirrors Azure's own vocabulary — ``"Announced"`` (still
GA, planned retirement in the future), ``"Retired"`` (planned date already
passed), or ``None`` (no announcement). See :data:`_RETIREMENT_BY_FAMILY` /
:data:`_RETIREMENT_BY_SIZE` and :func:`lookup_retirement`.

Each record also carries an ``availability_tier`` classifying how broadly its
**family** is offered across regions observed in the subscription's VM SKU catalog —
``"ubiquitous"`` (>= 80% of observed regions), ``"broad"`` (30% to < 80%), or
``"niche"`` (< 30%). It is computed once per scan and cascaded onto every SKU
record of the family, and is opt-in via ``include_availability_tier``. See
:func:`_classify_availability_tier`.

Each record also carries a ``category`` — the public-docs workload bucket
inferred from the **family** name (General purpose, Compute optimized, Memory
optimized, Storage optimized, Accelerated (GPU)/(FPGA), HPC, M Series,
Confidential, or Others) — and a ``processor`` (``"Intel"`` / ``"AMD"`` /
``"ARM"``) for the categories that follow Azure's size-naming convention,
``None`` otherwise. Both are pure functions of the family name (no API call or
scan). See :func:`classify_family`.

Inspired by an offline ``summarize_restrictions.py`` script, but fetches live
data instead of reading pre-saved JSON snapshots.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Iterable, Iterator, Optional

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, DefaultAzureCredential

from core.subscriptions import build_zone_mapping

logger = logging.getLogger(__name__)

_VM_RESOURCE_TYPE = "virtualmachines"


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _enum_str(value: Any) -> Optional[str]:
    """Return the plain string value of an Azure SDK enum (or ``None``)."""
    if value is None:
        return None
    return getattr(value, "value", None) or str(value)


def _status(in_catalog: bool, restricted: bool, reason: Optional[str]) -> dict[str, Any]:
    """Build a machine-friendly availability status dict with an Azure-vocabulary label."""
    if not in_catalog:
        display = "Not available"
    elif restricted:
        display = "Restricted"
    else:
        display = "Available"
    return {
        "in_catalog": in_catalog,
        "restricted": restricted,
        "restriction_reason": reason if restricted else None,
        "display_name": display,
    }


# ---------------------------------------------------------------------------
# VM-size retirement announcements
# ---------------------------------------------------------------------------
# Sourced from Azure's published retirement announcements, cross-checked against
# the consolidated Microsoft Learn "Retired Azure VM size series" list
# (https://learn.microsoft.com/azure/virtual-machines/sizes/retirement/retired-sizes-list).
# This map is now a *fallback* for :func:`lookup_retirement`, which prefers the
# SKUs API ``RetirementDateUtc`` capability when present. It still covers the
# SKUs the API leaves unflagged — promo / constrained-core variants and some
# GPU/FPGA/memory families. Keys are lowercased. The first tuple element is the
# planned retirement date (ISO).
#
# ``_RETIREMENT_BY_SIZE`` is checked before ``_RETIREMENT_BY_FAMILY`` so that
# retirements announced for specific sizes within an otherwise-current family
# take precedence over (or stand alone without) a family-wide entry.
#
# Values are ``(retirement_date_iso, announcement_url)``.

_RETIRED_SIZES_LIST_URL = (
    "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/retirement/"
    "retired-sizes-list"
)
_AZURE_UPDATE_URL = "https://azure.microsoft.com/en-us/updates/?id="

_RETIREMENT_BY_FAMILY: dict[str, tuple[str, str]] = {
    # ID 485569 — D, Ds, Dv2, Dsv2, Ls series retire 2028-05-01
    "standarddfamily": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),
    "standarddsfamily": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),
    "standarddv2family": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),
    "standarddsv2family": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),
    "standarddv2promofamily": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),  # Dv2 promo variant (not separately listed on Learn)
    "standarddsv2promofamily": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),  # Dsv2 promo variant (not separately listed on Learn)
    "standardlsfamily": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=485569"),
    # ID 572346 — Dv3, Dsv3, Ev3, and Esv3 series retire 2029-11-15
    "standarddv3family": ("2029-11-15", _AZURE_UPDATE_URL + "572346"),
    "standarddsv3family": ("2029-11-15", _AZURE_UPDATE_URL + "572346"),
    "standardev3family": ("2029-11-15", _AZURE_UPDATE_URL + "572346"),
    "standardesv3family": ("2029-11-15", _AZURE_UPDATE_URL + "572346"),
    # ID 500682 — F, Fs, Fsv2, Lsv2, G, Gs, Av2, Amv2, B series retire 2028-11-15
    "standardffamily": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardfsfamily": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardfsv2family": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardlsv2family": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardgfamily": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardgsfamily": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardav2family": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardamv2family": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),
    "standardbsfamily": ("2028-11-15", "https://azure.microsoft.com/en-us/updates/?id=500682"),  # original B/Bs V1 (Bsv2 is current-gen)
    # GPU — NVv3 (ID 500573) / NVv4 (ID 500578) series retire 2026-09-30
    "standardnvsv3family": ("2026-09-30", _AZURE_UPDATE_URL + "500573"),
    "standardnvsv4family": ("2026-09-30", _AZURE_UPDATE_URL + "500578"),
    # ID 548497 — FPGA NP series (NP10s/NP20s/NP40s) retire 2027-05-31
    "standardnpsfamily": ("2027-05-31", _AZURE_UPDATE_URL + "548497"),
    # ID 548543 — HPC HC series (HC44rs, HC44-16rs, HC44-32rs) retire 2027-05-31
    "standardhcsfamily": ("2027-05-31", _AZURE_UPDATE_URL + "548543"),
    # Confidential compute — DCsv2 series retires 2026-06-30 (ID 496104). The
    # SKUs API surfaces this via RetirementDateUtc; mirrored here as a fallback.
    "standarddcsv2family": ("2026-06-30", _AZURE_UPDATE_URL + "496104"),
}

# Per-size retirements. Azure announced these for specific sizes rather than a
# whole family.
_NCV3_LINK = (
    "https://azure.microsoft.com/en-us/updates/?id=standardnc6sv3-standardnc12sv3-"
    "and-standardnc24sv3-azure-virtual-machines-will-be-retired-on-september-30-2025"
)
_RETIREMENT_BY_SIZE: dict[str, tuple[str, str]] = {
    # NCv3 — retired 2025-09-30
    "standard_nc6s_v3": ("2025-09-30", _NCV3_LINK),
    "standard_nc12s_v3": ("2025-09-30", _NCV3_LINK),
    "standard_nc24s_v3": ("2025-09-30", _NCV3_LINK),
    "standard_nc24rs_v3": ("2025-09-30", _RETIRED_SIZES_LIST_URL),  # separate NCv3-NC24rs retirement
    # M192i*_v2 memory-optimized sizes — retire 2027-03-31 (one announcement per size)
    "standard_m192idms_v2": ("2027-03-31", _AZURE_UPDATE_URL + "support-for-standardm192idmsv2-will-be-retired-on-31-march-2027"),
    "standard_m192ids_v2": ("2027-03-31", _AZURE_UPDATE_URL + "community-support-for-standardm192idsv2-is-ending-on-31-march-2027"),
    "standard_m192ims_v2": ("2027-03-31", _AZURE_UPDATE_URL + "community-support-for-standardm192imsv2-is-ending-on-31-march-2027"),
    "standard_m192is_v2": ("2027-03-31", _AZURE_UPDATE_URL + "community-support-for-standardm192isv2-is-ending-on-31-march-2027"),
    # Standard_GS5 retires earlier than the rest of the Gs family (2028-05-01 vs
    # the family-wide 2028-11-15), per the SKUs API. Size entry overrides family.
    "standard_gs5": ("2028-05-01", "https://azure.microsoft.com/en-us/updates/?id=500682"),
}

# The SKUs API encodes "no planned retirement" as this sentinel date.
_RETIREMENT_NEVER = date(9999, 1, 1)


def lookup_retirement(
    name: Optional[str],
    family: Optional[str],
    api_retirement_date: Optional[date] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(retirement_status, retirement_date, retirement_link)`` for a SKU.

    API-first: when ``api_retirement_date`` is given (a real ``RetirementDateUtc``
    from the SKUs API, never the ``01/01/9999`` sentinel) it wins, reusing the
    curated announcement URL when one exists (else the generic retired-sizes
    list). Otherwise it falls back to the curated maps — size-specific first,
    then family (both matched case-insensitively) — which cover the SKUs the API
    leaves unflagged. ``retirement_status`` mirrors Azure's vocabulary and is
    derived from the planned date relative to today:

    * ``"Retired"``   — the planned retirement date has already passed.
    * ``"Announced"`` — a retirement is announced but the date is in the future.
    * ``None``        — no retirement announcement for this SKU.

    Returns ``(None, None, None)`` when neither the API nor either map matches.
    Cheap (two dict lookups) and reusable independently of a full availability
    scan.
    """
    curated = _RETIREMENT_BY_SIZE.get((name or "").lower())
    if curated is None:
        curated = _RETIREMENT_BY_FAMILY.get((family or "").lower())

    if api_retirement_date is not None:
        retirement_date = api_retirement_date.isoformat()
        retirement_link = curated[1] if curated else _RETIRED_SIZES_LIST_URL
    elif curated is not None:
        retirement_date, retirement_link = curated
    else:
        return None, None, None
    status = "Retired" if retirement_date <= date.today().isoformat() else "Announced"
    return status, retirement_date, retirement_link


# ---------------------------------------------------------------------------
# Family availability tiers (breadth of regional presence)
# ---------------------------------------------------------------------------
# A family's ``availability_tier`` classifies how broadly it is offered across
# regions observed in the unfiltered subscription VM SKU catalog. Restrictions
# do not remove catalog presence. The fraction is ``(# regions where the family
# is in catalog) / (# regions observed across all VM SKUs)``:
#
#   * ``"ubiquitous"`` — present in >= 80% of observed regions.
#   * ``"broad"``      — present in 30% to < 80% of observed regions.
#   * ``"niche"``      — present in < 30% of observed regions.

_UBIQUITOUS_MIN_FRACTION = 0.80
_BROAD_MIN_FRACTION = 0.30


def _classify_availability_tier(fraction: float) -> str:
    """Map a family's regional-presence ``fraction`` (0..1) to an availability tier."""
    if fraction >= _UBIQUITOUS_MIN_FRACTION:
        return "ubiquitous"
    if fraction >= _BROAD_MIN_FRACTION:
        return "broad"
    return "niche"


def _sku_region_codes(sku: Any) -> set[str]:
    """Collect advertised regions, including records without location details."""
    locations = [
        *(getattr(sku, "locations", None) or []),
        *(info.location for info in sku.location_info or []),
    ]
    return {location.strip().lower() for location in locations if location and location.strip()}


def _tiers_from_presence(
    family_region_presence: dict[str, set[str]],
    observed_region_count: int,
) -> dict[str, str]:
    """Classify each family by its fraction of observed VM catalog regions."""
    if observed_region_count <= 0:
        return {family: "niche" for family in family_region_presence}
    return {
        family: _classify_availability_tier(len(regions) / observed_region_count)
        for family, regions in family_region_presence.items()
    }


# ---------------------------------------------------------------------------
# Family category & processor classification
# ---------------------------------------------------------------------------
# Two independent dimensions inferred from the **family** name alone (so the
# result is consistent for every size within a family), aligned with the public
# VM-sizes taxonomy (https://learn.microsoft.com/azure/virtual-machines/sizes/overview):
#
#   * ``category`` — the workload bucket: General purpose, Compute optimized,
#     Memory optimized, Storage optimized, Accelerated (GPU), Accelerated
#     (FPGA), HPC, M Series, Confidential, or Others. M Series and Confidential
#     are intentionally kept as their own buckets rather than folded into Memory
#     optimized / General purpose.
#   * ``processor`` — Intel / AMD / ARM, derived from Azure's size-naming
#     convention (an ``a`` modifier marks AMD, a ``p`` modifier marks ARM, else
#     Intel). Only the five categories that follow that convention get a
#     processor; for Accelerated / HPC / M Series / Others the naming letters are
#     not a reliable silicon signal (e.g. the "P" in "RTX **P**RO"), so
#     ``processor`` is ``None``.

# (normalized prefix, category) ordered longest-prefix-first; first match wins.
_CATEGORY_RULES: list[tuple[str, str]] = [
    ("NC", "Accelerated (GPU)"), ("ND", "Accelerated (GPU)"),
    ("NG", "Accelerated (GPU)"), ("NV", "Accelerated (GPU)"),
    ("NM", "Accelerated (FPGA)"), ("NP", "Accelerated (FPGA)"),
    ("PB", "Accelerated (FPGA)"),
    ("HB", "HPC"), ("HC", "HPC"), ("HX", "HPC"),
    ("DC", "Confidential"), ("EC", "Confidential"),
    ("M", "M Series"),
    ("FX", "Compute optimized"), ("F", "Compute optimized"),
    ("E", "Memory optimized"), ("G", "Memory optimized"),
    ("L", "Storage optimized"),
    ("A", "General purpose"), ("B", "General purpose"), ("D", "General purpose"),
]

# Categories whose naming follows the a/p = silicon convention; the rest get
# ``processor = None``.
_PROC_CATEGORIES = frozenset({
    "General purpose", "Compute optimized", "Memory optimized",
    "Storage optimized", "Confidential",
})

# Strips separators from a normalized family token.
_FAMILY_NONALNUM = re.compile(r"[^A-Z0-9]")
# Cuts a token at its version marker (``V2``/``V5``…) so the processor scan only
# inspects the base family letters, not the version or trailing accelerator tags.
_FAMILY_VERSION_CUT = re.compile(r"V\d")


def _norm_family(family: str) -> str:
    """Normalize a SKU family name to an uppercase alphanumeric token.

    Strips the ``standard`` prefix / ``Family`` suffix and the ``Promo`` marker,
    uppercases and drops separators, e.g. ``"standardDSv5Family"`` -> ``"DSV5"``.
    """
    token = family.strip()
    token = re.sub(r"(?i)^standard\s*", "", token)
    token = re.sub(r"(?i)family$", "", token)
    token = re.sub(r"(?i)promo", "", token)
    return _FAMILY_NONALNUM.sub("", token.upper())


def _processor_of(remainder: str) -> str:
    """Infer ``"Intel"`` / ``"AMD"`` / ``"ARM"`` from a family token's base letters.

    ``remainder`` is the normalized family token with its category prefix already
    stripped. An ``A`` in the pre-version chunk marks AMD, a ``P`` marks ARM,
    otherwise Intel.
    """
    first_chunk = _FAMILY_VERSION_CUT.split(remainder, maxsplit=1)[0]
    if "A" in first_chunk:
        return "AMD"
    if "P" in first_chunk:
        return "ARM"
    return "Intel"


def classify_family(family: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(category, processor)`` inferred from a SKU ``family`` name.

    ``category`` is the public-docs workload bucket (see :data:`_CATEGORY_RULES`);
    ``processor`` is ``"Intel"`` / ``"AMD"`` / ``"ARM"`` for the
    convention-following categories (:data:`_PROC_CATEGORIES`) and ``None``
    elsewhere. An empty family yields ``(None, None)``; an unrecognized family
    yields ``("Others", None)``. Pure and cheap — no API or scan required.
    """
    if not family:
        return None, None
    token = _norm_family(family)
    for prefix, category in _CATEGORY_RULES:
        if token.startswith(prefix):
            processor = (
                _processor_of(token[len(prefix):])
                if category in _PROC_CATEGORIES
                else None
            )
            return category, processor
    return "Others", None


# ---------------------------------------------------------------------------
# VM SKU enumeration
# ---------------------------------------------------------------------------

def _list_vm_skus(
    subscription_id: str,
    credential: TokenCredential,
    *,
    location: Optional[str] = None,
) -> Iterator[Any]:
    """Yield ``ComputeManagementClient`` VM SKU objects.

    When ``location`` is given, the filter is pushed down to the API
    (``$filter=location eq '<location>'``). The Compute resource-skus API only
    supports a single ``location eq`` filter, so multi-location / size / family
    filtering is done client-side by the caller.
    """
    from azure.mgmt.compute import ComputeManagementClient

    client = ComputeManagementClient(credential, subscription_id)
    filter_expr = f"location eq '{location}'" if location else None
    for sku in client.resource_skus.list(filter=filter_expr):
        if _enum_str(sku.resource_type).lower() == _VM_RESOURCE_TYPE:
            yield sku


# Matches a base-10 integer (optionally signed). Checked before the float
# pattern so ``"16"`` becomes ``int`` rather than ``float``.
_INT_RE = re.compile(r"[+-]?\d+")
# Matches a decimal number with a fractional part (``"1.5"``, ``".5"``,
# ``"1."``). Capability values are rarely floats today, but covered defensively.
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+)")

# Capability names (lower-cased) whose value is a comma-separated list. They are
# split into a list with each element passed back through the scalar caster
# (e.g. ``DiskControllerTypes`` -> ``["NVMe", "SCSI"]``,
# ``vCPUsConstraintsAllowed`` -> ``[1, 2, 4, ...]``).
_LIST_CAPABILITIES = {
    "diskcontrollertypes",
    "hypervgenerations",
    "supportedcapacityreservationtypes",
    "supportedephemeralosdiskplacements",
    "vmdeploymenttypes",
    "vcpusconstraintsallowed",
}
# Capability names (lower-cased) whose value is a date, parsed to ``datetime.date``.
_DATE_CAPABILITIES = {"retirementdateutc"}
# Date formats seen in the catalog (e.g. ``"11/16/2028"``, ``"01/01/9999"``).
_DATE_FORMATS = ("%m/%d/%Y",)


def _cast_scalar(value: Optional[str]) -> Any:
    """Coerce a single capability string to ``bool``/``int``/``float``/``str``.

    Booleans come as ``"True"``/``"False"``, scalars as plain digits (``"16"``,
    ``"429496729600"``). Anything that does not cleanly parse (and empty
    strings) is left as the original string; ``None`` passes through as ``None``.
    """
    if value is None:
        return None
    text = value.strip()
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.fullmatch(text):
        try:
            return int(text)
        except ValueError:
            return value
    if _FLOAT_RE.fullmatch(text):
        try:
            return float(text)
        except ValueError:
            return value
    return value


def _parse_capability_date(text: str) -> Optional[date]:
    """Parse a capability date string to ``date`` (or ``None`` if unparseable)."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cast_capability_value(name: str, value: Optional[str]) -> Any:
    """Coerce a raw SKU capability value based on its capability ``name``.

    Known comma-separated capabilities (:data:`_LIST_CAPABILITIES`) become lists
    of scalar-cast elements; known date capabilities
    (:data:`_DATE_CAPABILITIES`) become :class:`datetime.date`. Everything else
    falls back to :func:`_cast_scalar` (``bool``/``int``/``float``/``str``).

    Null / empty handling:
      * ``value is None`` -> ``None`` in every case.
      * empty string -> ``""`` for scalars, ``[]`` for list capabilities,
        ``None`` for date capabilities.
      * an unparseable date falls back to the original string (data is never
        dropped).
    """
    if value is None:
        return None
    key = name.lower()

    if key in _DATE_CAPABILITIES:
        text = value.strip()
        if not text:
            return None
        return _parse_capability_date(text) or value

    if key in _LIST_CAPABILITIES:
        text = value.strip()
        if not text:
            return []
        return [_cast_scalar(part.strip()) for part in text.split(",")]

    return _cast_scalar(value)


def _api_retirement_date(capabilities: Optional[Iterable[Any]]) -> Optional[date]:
    """Extract a real ``RetirementDateUtc`` date from a SKU's capabilities.

    Returns ``None`` when the capability is absent or carries the
    ``01/01/9999`` "never retires" sentinel.
    """
    for capability in capabilities or []:
        if capability.name == "RetirementDateUtc":
            value = _cast_capability_value("RetirementDateUtc", capability.value)
            if isinstance(value, date) and value != _RETIREMENT_NEVER:
                return value
            return None
    return None


def _vcpus_from_capabilities(capabilities: Optional[Iterable[Any]]) -> Optional[int]:
    """Extract the integer ``vCPUs`` count from a SKU's capabilities.

    Returns ``None`` when the capability is absent or does not parse as an int.
    Needed for quota-impact math (Compute quota is expressed in vCPUs/cores).
    """
    for capability in capabilities or []:
        if capability.name == "vCPUs":
            value = _cast_capability_value("vCPUs", capability.value)
            return value if isinstance(value, int) else None
    return None


def list_vm_skus(
    subscription_id: str,
    credential: TokenCredential,
    *,
    locations: Optional[Iterable[str]] = None,
    include_capabilities: bool = False,
    capabilities: Optional[Iterable[str]] = None,
    cast_capabilities: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield flat per-(location, SKU) VM SKU records, expanded by location.

    The Compute resource-skus API returns a SKU only for the regions where it is
    offered (and only supports a *single* ``location eq`` filter). This helper
    therefore lists the catalog **once** and expands each SKU across the
    locations it advertises in ``location_info``, emitting one record per
    ``(location, sku)`` pair — ready to join on ``(location, size)``.

    Parameters
    ----------
    locations:
        Restrict output to these regions (case-insensitive). ``None`` returns
        every region the SKU is offered in. When exactly one location is
        requested the filter is pushed down to the API; otherwise a single
        unfiltered catalog pass is made and locations are filtered client-side.
    include_capabilities:
        When ``True`` each record carries a ``capabilities`` dict of the SKU's
        ``name -> value`` capability pairs. Capabilities are SKU-global, not
        per-location.
    capabilities:
        Optional subset of capability names to include (matched
        case-insensitively). ``None`` includes all capabilities. Ignored when
        ``include_capabilities`` is ``False``.
    cast_capabilities:
        When ``True`` (default) each capability value is coerced to a native
        type: scalars become ``bool``/``int``/``float`` where they cleanly parse
        (e.g. ``vCPUs`` -> ``16``, ``CapacityReservationSupported`` -> ``True``);
        known comma-separated capabilities become lists (e.g.
        ``DiskControllerTypes`` -> ``["NVMe", "SCSI"]``) and known date
        capabilities become ``datetime.date`` (e.g. ``RetirementDateUtc``).
        Composite/unknown values stay strings. Set ``False`` to keep the raw
        API strings.

    Yields
    ------
    dict
        ``{"location", "name", "family", "tier", "size", "category",
        "processor"}`` and, when requested, ``"capabilities"``. ``category`` and
        ``processor`` are inferred from the family name (see
        :func:`classify_family`).
    """
    wanted_locations: Optional[set[str]] = (
        {loc.lower() for loc in locations} if locations is not None else None
    )
    wanted_caps: Optional[set[str]] = (
        {name.lower() for name in capabilities} if capabilities is not None else None
    )

    # Push the filter down only when a single location is requested; otherwise a
    # single unfiltered pass is cheaper than one round trip per region.
    push_location: Optional[str] = None
    if wanted_locations is not None and len(wanted_locations) == 1:
        push_location = next(iter(wanted_locations))

    for sku in _list_vm_skus(subscription_id, credential, location=push_location):
        cap_dict: Optional[dict[str, Any]] = None
        if include_capabilities:
            cap_dict = {}
            for capability in sku.capabilities or []:
                name = capability.name
                if name is None:
                    continue
                if wanted_caps is not None and name.lower() not in wanted_caps:
                    continue
                cap_dict[name] = (
                    _cast_capability_value(name, capability.value)
                    if cast_capabilities
                    else capability.value
                )

        for loc_info in sku.location_info or []:
            location = (loc_info.location or "").lower()
            if not location:
                continue
            if wanted_locations is not None and location not in wanted_locations:
                continue

            category, processor = classify_family(sku.family)
            record: dict[str, Any] = {
                "location": location,
                "name": sku.name,
                "family": sku.family,
                "tier": sku.tier,
                "size": sku.size,
                "category": category,
                "processor": processor,
            }
            if include_capabilities:
                record["capabilities"] = dict(cap_dict or {})
            yield record


def _region_restriction(sku: Any, location: str) -> tuple[bool, Optional[str]]:
    """Return ``(restricted, reason)`` for a SKU in ``location`` (region scope)."""
    location = location.lower()
    for restriction in sku.restrictions or []:
        if _enum_str(restriction.type).lower() != "location":
            continue
        info = restriction.restriction_info
        restricted_locations = [l.lower() for l in (info.locations or [])] if info else []
        if location in restricted_locations:
            return True, _enum_str(restriction.reason_code)
    return False, None


def _zone_restriction(
    sku: Any, location: str, logical_zone: str
) -> tuple[bool, Optional[str]]:
    """Return ``(restricted, reason)`` for a SKU's logical zone in ``location``."""
    location = location.lower()
    for restriction in sku.restrictions or []:
        if _enum_str(restriction.type).lower() != "zone":
            continue
        info = restriction.restriction_info
        if not info:
            continue
        restricted_locations = [l.lower() for l in (info.locations or [])]
        restricted_zones = [str(z) for z in (info.zones or [])]
        if location in restricted_locations and logical_zone in restricted_zones:
            return True, _enum_str(restriction.reason_code)
    return False, None


# ---------------------------------------------------------------------------
# Cross-region exhaustivity (synthesize "Not available" records)
# ---------------------------------------------------------------------------

def _fill_missing_skus(
    result: dict[str, dict[str, Any]],
    present: dict[str, set[str]],
    universe: dict[str, dict[str, Any]],
    zone_mapping: dict[str, dict[str, str]],
    locations_filter: Optional[set[str]],
) -> None:
    """Synthesize ``Not available`` records so every location covers the size universe.

    The Compute resource-skus API omits a VM size **entirely** from a region
    that does not offer it (there is no "not in catalog" record). For
    cross-region exhaustivity this fills those gaps so each location exposes the
    same set of sizes (the global ``universe`` observed across all regions) —
    synthesized records carry an ``in_catalog=False`` ("Not available") status at
    both region and zone scope.
    """
    targets = set(locations_filter) if locations_filter is not None else set(result.keys())
    not_available = _status(False, False, None)
    for location in targets:
        bucket = result.get(location)
        if bucket is None:
            loc_zone_map = zone_mapping.get(location, {})
            bucket = {
                "supports_availability_zones": bool(loc_zone_map),
                "zone_mapping": dict(loc_zone_map),
                "skus": [],
            }
            result[location] = bucket
        loc_present = present.get(location, set())
        physical_zones = list(bucket["zone_mapping"].values())
        for name, identity in universe.items():
            if name in loc_present:
                continue
            bucket["skus"].append({
                **identity,
                "region": dict(not_available),
                "zones": {pz: dict(not_available) for pz in physical_zones},
            })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _project_capabilities(
    capabilities: Optional[Iterable[Any]],
    wanted: Optional[set[str]],
) -> dict[str, Any]:
    return {
        capability.name: _cast_capability_value(capability.name, capability.value)
        for capability in capabilities or []
        if capability.name is not None
        and (wanted is None or capability.name.lower() in wanted)
    }


def get_compute_skus(
    subscription_id: str,
    credential: Optional[TokenCredential] = None,
    *,
    locations: Optional[Iterable[str]] = None,
    include_capabilities: bool = False,
    capabilities: Optional[Iterable[str]] = None,
    include_retirement: bool = False,
    include_availability: bool = False,
    include_availability_tier: bool = False,
    fill_missing: bool = False,
) -> dict[str, dict[str, Any]]:
    """Collect configurable per-region VM SKU records in one catalog traversal.

    The result is ``{location: {"skus": [record, ...]}}``. Each record contains
    ``location``, ``name``, ``size``, ``family``, ``tier``, ``category``,
    ``processor`` and ``vcpus``. Unrequested enrichments are omitted.

    ``include_capabilities`` adds a typed ``capabilities`` dict, optionally
    limited to the supplied capability names. This is an output projection,
    not an Azure API field filter. Matching regional zone-detail capabilities
    are also projected as ``zone_capabilities`` groups containing
    logical ``zones`` and typed ``capabilities``. These preserve the API's
    subscription-relative labels without fetching a zone mapping. Callers
    needing physical IDs translate them using the location-level
    ``zone_mapping`` returned with ``include_availability=True``. Zonal
    capabilities are not promoted to global SKU capabilities.
    ``include_retirement`` adds
    ``retirement_status``, ISO-string ``retirement_date`` and ``retirement_link``,
    using API retirement dates with the published-announcement fallbacks.

    ``include_availability`` adds both ``region`` and physical-zone-keyed
    ``zones`` status dictionaries, plus location-level
    ``supports_availability_zones`` and ``zone_mapping`` metadata. Only this
    option performs the subscription zone-mapping lookup.
    ``include_availability_tier`` adds the family's breadth across all regions
    advertised by the unfiltered VM SKU catalog. It uses the same full SKU
    footprint scan, with no separate region lookup. Restricted regions still
    count as catalog presence.

    ``fill_missing`` requires availability and synthesizes unavailable records
    for absent region/size pairs using the global size universe.

    A single ``locations`` entry is pushed to Azure unless ``fill_missing`` or
    availability-tier calculation requires the full catalog. Multiple regions
    are filtered locally after one full traversal; an empty iterable returns
    an empty result without querying Azure. Sizes and families should always
    be filtered locally from the returned snapshot, not by repeated API calls.
    SDK pagination can still require multiple HTTP requests per traversal.
    """
    if fill_missing and not include_availability:
        raise ValueError("fill_missing requires include_availability=True")
    if capabilities is not None and not include_capabilities:
        raise ValueError("capabilities requires include_capabilities=True")
    locations_filter = (
        {location.lower() for location in locations} if locations is not None else None
    )
    if locations_filter == set():
        return {}
    wanted_caps = (
        {name.lower() for name in capabilities} if capabilities is not None else None
    )
    credential = credential or DefaultAzureCredential()
    zone_mapping = (
        build_zone_mapping(subscription_id, credential) if include_availability else {}
    )
    observed_regions: set[str] = set()
    push_location = None
    if (
        locations_filter is not None
        and len(locations_filter) == 1
        and not fill_missing
        and not include_availability_tier
    ):
        push_location = next(iter(locations_filter))

    result: dict[str, dict[str, Any]] = {}
    present: dict[str, set[str]] = {}
    universe: dict[str, dict[str, Any]] = {}
    family_region_presence: dict[str, set[str]] = {}
    for sku in _list_vm_skus(subscription_id, credential, location=push_location):
        if include_availability_tier:
            sku_regions = _sku_region_codes(sku)
            observed_regions.update(sku_regions)
            family_region_presence.setdefault(
                (sku.family or "").lower(), set()
            ).update(sku_regions)
        category, processor = classify_family(sku.family)
        identity: dict[str, Any] = {
            "name": sku.name,
            "size": sku.size,
            "family": sku.family,
            "tier": sku.tier,
            "category": category,
            "processor": processor,
            "vcpus": _vcpus_from_capabilities(sku.capabilities),
        }
        if include_capabilities:
            identity["capabilities"] = _project_capabilities(sku.capabilities, wanted_caps)
        if include_retirement:
            status, retirement_date, link = lookup_retirement(
                sku.name, sku.family, _api_retirement_date(sku.capabilities)
            )
            identity.update(
                retirement_status=status,
                retirement_date=retirement_date,
                retirement_link=link,
            )
        if fill_missing:
            universe[sku.name] = identity

        for loc_info in sku.location_info or []:
            location = (loc_info.location or "").lower()
            if not location:
                continue
            if locations_filter is not None and location not in locations_filter:
                continue
            bucket = result.setdefault(location, {"skus": []})
            record = dict(identity)
            if include_capabilities:
                zone_capabilities = []
                for detail in getattr(loc_info, "zone_details", None) or []:
                    projected = _project_capabilities(detail.capabilities, wanted_caps)
                    if not projected:
                        continue
                    names = detail.name
                    if names is None:
                        # Some API responses use "Name", which the SDK leaves unmapped.
                        names = detail.as_dict().get("Name", [])
                    if not names:
                        raise ValueError(
                            f"SKU {sku.name!r} has zonal capabilities without zone "
                            f"identifiers in {location!r}"
                        )
                    zone_capabilities.append({
                        "zones": [str(zone) for zone in names],
                        "capabilities": projected,
                    })
                if zone_capabilities:
                    record["zone_capabilities"] = zone_capabilities
            if include_availability:
                mapping = zone_mapping.get(location, {})
                bucket["supports_availability_zones"] = bool(mapping)
                bucket["zone_mapping"] = dict(mapping)
                restricted, reason = _region_restriction(sku, location)
                record["region"] = _status(True, restricted, reason)
                sku_zones = {str(zone) for zone in loc_info.zones or []}
                zones = {}
                for logical, physical in mapping.items():
                    in_catalog = logical in sku_zones
                    restricted, reason = (
                        _zone_restriction(sku, location, logical)
                        if in_catalog else (False, None)
                    )
                    zones[physical] = _status(in_catalog, restricted, reason)
                record["zones"] = zones
            bucket["skus"].append(record)
            if fill_missing:
                present.setdefault(location, set()).add(sku.name)

    if fill_missing:
        _fill_missing_skus(result, present, universe, zone_mapping, locations_filter)
    tiers = (
        _tiers_from_presence(family_region_presence, len(observed_regions))
        if include_availability_tier else {}
    )
    for location, bucket in result.items():
        for record in bucket["skus"]:
            record["location"] = location
            if include_capabilities:
                record["capabilities"] = dict(record["capabilities"])
            if include_availability_tier:
                record["availability_tier"] = tiers.get(
                    (record["family"] or "").lower()
                )
    return result


def get_compute_sku_availability(
    subscription_id: str,
    credential: Optional[TokenCredential] = None,
    *,
    locations: Optional[Iterable[str]] = None,
    sizes: Optional[Iterable[str]] = None,
    families: Optional[Iterable[str]] = None,
    fill_missing: bool = True,
    include_availability_tier: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return VM SKU availability for ``subscription_id``, keyed by location.

    For every VM SKU in catalog, reports its per-region and per-zone
    availability — whether it is offered and, if so, whether it is restricted
    (with the reason).

    Parameters
    ----------
    subscription_id:
        Target subscription GUID. Mandatory.

        .. todo::
            Future enhancement — make this optional and auto-resolve to the
            last-created non-dev/test subscription. The helper would live in
            :mod:`core.subscriptions` and use the documented
            ``Microsoft.Billing`` ``billingSubscriptions`` operation
            (``systemData.createdAt`` for recency — works for EA and MCA) plus
            ``subscriptionPolicies.quotaId`` for dev/test exclusion. Do **not**
            use the undocumented ``service.bmx.azure.com`` portal endpoints.
    credential:
        Optional Azure credential. Defaults to :class:`DefaultAzureCredential`.
    locations:
        Optional location codes to include (case-insensitive). A single value is
        pushed to the Compute API as a server-side ``location eq`` filter; for
        multiple values all SKUs are listed and filtered client-side. ``None``
        returns every location.
    sizes:
        Optional SKU sizes to include (e.g. ``"D4s_v5"``). Client-side only.
    families:
        Optional SKU families to include (e.g. ``"standardDSv5Family"``).
        Client-side only.
    fill_missing:
        When ``True`` (default) synthesize ``Not available`` records so every
        returned location exposes the same **global** size universe (the union
        of all VM sizes seen across the queried regions), making the result a
        rectangular size x region matrix. A size that a region does not offer is
        omitted by the API; this fills the gap with an ``in_catalog=False``
        status. Because this needs the full catalog, the single-location
        server-side filter is bypassed when enabled. When ``False`` each
        location lists only the sizes the API associates with it.
    include_availability_tier:
        When ``True`` (default) compute each family's ``availability_tier`` and
        stamp it on every SKU record. The tier reflects how broadly the family
        is offered across all regions observed in the subscription's VM SKU
        catalog, so requesting it forces a full
        footprint scan: the single-location server-side filter is bypassed (as
        with ``fill_missing``) to keep the tier accurate. When ``False`` the
        ``availability_tier`` key is omitted and a single-location query may use
        the faster server-side filter — use this when the caller only needs raw availability and not
        the breadth signal.

    Returns
    -------
    dict[str, dict]
        ``{location: {"supports_availability_zones": bool,
                      "zone_mapping": {logical: physical},
                      "skus": [sku_record, ...]}}``.
        Each ``sku_record`` is ``{"name", "tier", "size", "family",
        "category", "processor", "vcpus", "retirement_status",
        "retirement_date", "retirement_link", "availability_tier", "region",
        "zones"}`` where ``vcpus`` is the SKU's integer vCPU count (from the
        ``vCPUs`` capability, ``None`` when unavailable) used for quota math,
        ``category`` is the public-docs workload bucket and ``processor`` is
        ``"Intel"`` / ``"AMD"`` / ``"ARM"`` (or ``None``), both inferred from the
        family name (see :func:`classify_family`); ``region`` is an
        availability status dict, ``zones`` maps each physical zone to an
        availability status dict (empty for regions without AZ support), the
        ``retirement_status`` / ``retirement_date`` / ``retirement_link`` fields
        come from :func:`lookup_retirement`, and ``availability_tier``
        (``"ubiquitous"`` / ``"broad"`` / ``"niche"``) reflects the breadth of
        the SKU's **family** across observed catalog regions (see
        :func:`_classify_availability_tier`). ``availability_tier`` is present
        only when ``include_availability_tier`` is ``True``; ``sizes`` /
        ``families`` filters only narrow output, not the observed-region
        denominator or the family's regional footprint. Regions advertised by
        restricted SKUs count as catalog presence. No separate region lookup
        is performed for this classification.
    """
    credential = credential or DefaultAzureCredential()

    locations_filter = {l.lower() for l in locations} if locations else None
    sizes_filter = {s.lower() for s in sizes} if sizes else None
    families_filter = {f.lower() for f in families} if families else None

    zone_mapping = build_zone_mapping(subscription_id, credential)
    observed_regions: set[str] = set()

    # When ``fill_missing`` is on we need the global size universe (every VM size
    # the subscription can see, across all regions) to detect what is absent
    # from a given region, so the single-location server-side filter must be
    # bypassed. The availability tier likewise needs every region's footprint,
    # so requesting it also forces a full scan. Otherwise push the location
    # filter down when exactly one is requested.
    push_location: Optional[str] = None
    if (
        not fill_missing
        and not include_availability_tier
        and locations_filter is not None
        and len(locations_filter) == 1
    ):
        push_location = next(iter(locations_filter))

    result: dict[str, dict[str, Any]] = {}
    present: dict[str, set[str]] = {}
    universe: dict[str, dict[str, Any]] = {}
    family_region_presence: dict[str, set[str]] = {}

    for sku in _list_vm_skus(subscription_id, credential, location=push_location):
        size = (sku.size or "").lower()
        family = (sku.family or "").lower()
        if include_availability_tier:
            sku_regions = _sku_region_codes(sku)
            observed_regions.update(sku_regions)
            family_region_presence.setdefault(family, set()).update(sku_regions)
        if sizes_filter is not None and size not in sizes_filter:
            continue
        if families_filter is not None and family not in families_filter:
            continue

        retirement_status, retirement_date, retirement_link = lookup_retirement(
            sku.name, sku.family, _api_retirement_date(sku.capabilities)
        )
        category, processor = classify_family(sku.family)
        identity = {
            "name": sku.name,
            "tier": sku.tier,
            "size": sku.size,
            "family": sku.family,
            "category": category,
            "processor": processor,
            "vcpus": _vcpus_from_capabilities(sku.capabilities),
            "retirement_status": retirement_status,
            "retirement_date": retirement_date,
            "retirement_link": retirement_link,
        }
        if include_availability_tier:
            identity["availability_tier"] = None  # stamped once the full scan completes
        if fill_missing:
            universe[sku.name] = identity

        for loc_info in sku.location_info or []:
            location = (loc_info.location or "").lower()
            if not location:
                continue
            if locations_filter is not None and location not in locations_filter:
                continue

            loc_zone_map = zone_mapping.get(location, {})
            bucket = result.get(location)
            if bucket is None:
                bucket = {
                    "supports_availability_zones": bool(loc_zone_map),
                    "zone_mapping": dict(loc_zone_map),
                    "skus": [],
                }
                result[location] = bucket

            region_restricted, region_reason = _region_restriction(sku, location)
            sku_zones = {str(z) for z in (loc_info.zones or [])}

            zones: dict[str, dict[str, Any]] = {}
            for logical_zone, physical_zone in loc_zone_map.items():
                in_catalog = logical_zone in sku_zones
                if in_catalog:
                    restricted, reason = _zone_restriction(sku, location, logical_zone)
                else:
                    restricted, reason = False, None
                zones[physical_zone] = _status(in_catalog, restricted, reason)

            present.setdefault(location, set()).add(sku.name)
            bucket["skus"].append({
                **identity,
                "region": _status(True, region_restricted, region_reason),
                "zones": zones,
            })

    if fill_missing:
        _fill_missing_skus(result, present, universe, zone_mapping, locations_filter)

    # Cascade the per-family availability tier onto every SKU record now that the
    # full regional footprint of each family is known.
    if include_availability_tier:
        tiers = _tiers_from_presence(family_region_presence, len(observed_regions))
        for bucket in result.values():
            for record in bucket["skus"]:
                record["availability_tier"] = tiers.get((record["family"] or "").lower())

    return result


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subscription_id")
    parser.add_argument(
        "--location", action="append", dest="locations",
        help="Location code to include (repeatable).",
    )
    parser.add_argument(
        "--size", action="append", dest="sizes",
        help="SKU size to include, e.g. D4s_v5 (repeatable).",
    )
    parser.add_argument(
        "--family", action="append", dest="families",
        help="SKU family to include (repeatable).",
    )
    parser.add_argument(
        "--fill-missing", action=argparse.BooleanOptionalAction, default=True,
        help="Synthesize 'Not available' records so every location covers the "
             "full size universe (default: on).",
    )
    parser.add_argument(
        "--availability-tier", dest="include_availability_tier",
        action=argparse.BooleanOptionalAction, default=True,
        help="Compute and stamp each family's availability_tier; forces a full "
             "footprint scan (default: on).",
    )
    parser.add_argument("--out", default=None, help="Optional JSON output path.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure").setLevel(logging.WARNING)

    # TODO: Migrate this CLI to get_compute_skus with explicit enrichment options.
    data = get_compute_sku_availability(
        args.subscription_id,
        credential=AzureCliCredential(),
        locations=args.locations,
        sizes=args.sizes,
        families=args.families,
        fill_missing=args.fill_missing,
        include_availability_tier=args.include_availability_tier,
    )
    logger.info(
        "Retrieved availability for %d location(s): %s",
        len(data), ", ".join(sorted(data)),
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        logger.info("Wrote %s", args.out)
    else:
        print(json.dumps(data, indent=2, default=str))
