"""Subscription helpers.

Exposes :func:`get_subscriptions`, which queries Azure Resource Graph for every
subscription in the tenant and returns a flat ``list[dict]``; plus
:func:`list_subscription_locations` and :func:`build_zone_mapping`, thin wrappers
over the ARM ``subscriptions/locations`` endpoint (availability-zone metadata);
and :func:`get_compute_usages`, which returns ``Microsoft.Compute`` quota usage
and limits per location.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

from core.azure_resource_graph import run_graph_query

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


# KQL: enumerate every subscription in the tenant. ``resourcecontainers``
# exposes subscriptions as ``microsoft.resources/subscriptions``. The immediate
# parent management group is the first element of
# ``properties.managementGroupAncestorsChain`` (ordered from immediate parent up
# to the tenant root group). ``order by id asc`` gives deterministic ordering
# for Resource Graph pagination.
_SUBSCRIPTIONS_KQL = """
resourcecontainers
| where type =~ 'microsoft.resources/subscriptions'
| extend chain = properties.managementGroupAncestorsChain
| project
    id = subscriptionId,
    name,
    parentManagementGroupId = tostring(chain[0].name),
    state = tostring(properties.state)
| order by id asc
""".strip()


def get_subscriptions(credential) -> list[dict]:
    """Return a flat list of every subscription in the tenant.

    Uses Azure Resource Graph (the same mechanism as
    :func:`core.management_groups.get_management_groups`) to enumerate
    subscription containers.

    Parameters
    ----------
    credential : TokenCredential
        Azure credential forwarded to
        :func:`core.azure_resource_graph.run_graph_query`.

    Returns
    -------
    list[dict]
        One dict per subscription with keys ``id``, ``name``,
        ``parentManagementGroupId`` and ``state``.
        ``parentManagementGroupId`` is ``None`` when the subscription has no
        management-group ancestor.
    """
    logging.info("Enumerating subscriptions from Azure Resource Graph")
    rows = run_graph_query(_SUBSCRIPTIONS_KQL, credential)

    # Normalise empty parentManagementGroupId to None.
    for row in rows:
        if not row.get('parentManagementGroupId'):
            row['parentManagementGroupId'] = None

    logging.info("   Found %d subscription(s).", len(rows))
    return rows


def list_subscription_locations(
    subscription_id: str,
    credential: "TokenCredential",
    *,
    physical_only: bool = True,
) -> list[Any]:
    """Return the ARM ``Location`` objects available to ``subscription_id``.

    Uses :meth:`azure.mgmt.resource.subscriptions.SubscriptionClient.subscriptions.list_locations`,
    whose ``Location`` model surfaces ``availability_zone_mappings`` (the
    ``azure-mgmt-subscription`` package's model does not). When ``physical_only``
    is ``True`` (default) logical/non-physical regions are filtered out via
    ``metadata.region_type``.
    """
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    client = SubscriptionClient(credential)
    locations = list(client.subscriptions.list_locations(subscription_id))
    if physical_only:
        locations = [
            loc for loc in locations
            if loc.metadata and (loc.metadata.region_type or "").lower() == "physical"
        ]
    return locations


def build_zone_mapping(
    subscription_id: str,
    credential: "TokenCredential",
) -> dict[str, dict[str, str]]:
    """Return ``{location: {logical_zone: physical_zone}}`` for a subscription.

    Built on :func:`list_subscription_locations`. A location absent from the
    result (or mapped to an empty dict) does not support availability zones.
    Logical zone numbers (``"1"/"2"/"3"``) are per-subscription and map to
    different physical datacenters in different subscriptions.
    """
    mapping: dict[str, dict[str, str]] = {}
    for loc in list_subscription_locations(subscription_id, credential, physical_only=False):
        code = (loc.name or "").lower()
        if not code:
            continue
        zone_map: dict[str, str] = {}
        for az in loc.availability_zone_mappings or []:
            logical = az.logical_zone
            physical = az.physical_zone
            if logical is not None and physical is not None:
                zone_map[str(logical)] = str(physical)
        mapping[code] = zone_map
    return mapping


def get_compute_usages(
    credential: "TokenCredential",
    subscription_id: str,
    locations: "str | Iterable[str]",
) -> dict[str, dict[str, dict]]:
    """Return ``Microsoft.Compute`` quota usage and limits per location.

    Wraps :meth:`azure.mgmt.compute.ComputeManagementClient.usage.list`, the
    same data the portal surfaces via
    ``/subscriptions/{id}/providers/Microsoft.Compute/locations/{loc}/usages``.
    The Compute usage API is per-location, so one call is made per requested
    region.

    Parameters
    ----------
    credential : TokenCredential
        Azure credential forwarded to ``ComputeManagementClient``.
    subscription_id : str
        The subscription whose compute quotas are queried.
    locations : str | Iterable[str]
        A single region code (e.g. ``"francecentral"``) or an iterable of them.

    Returns
    -------
    dict[str, dict[str, dict]]
        Nested mapping keyed by **lowercased location**, then by **lowercased
        usage name** (``name.value``), for O(1) quota lookup. Each leaf holds
        ``name`` (machine name, original case), ``localized_name``,
        ``current_value``, ``limit`` and ``unit``.
    """
    from azure.mgmt.compute import ComputeManagementClient

    if isinstance(locations, str):
        locations = [locations]

    client = ComputeManagementClient(credential, subscription_id)
    result: dict[str, dict[str, dict]] = {}
    for location in locations:
        loc_key = location.lower()
        logging.info("Fetching Microsoft.Compute usages for %s", loc_key)
        per_name: dict[str, dict] = {}
        for usage in client.usage.list(location):
            name = usage.name.value or ""
            per_name[name.lower()] = {
                "name": name,
                "localized_name": usage.name.localized_value,
                "current_value": usage.current_value,
                "limit": usage.limit,
                "unit": usage.unit,
            }
        logging.info("   Found %d quota entr(ies) for %s.", len(per_name), loc_key)
        result[loc_key] = per_name
    return result

