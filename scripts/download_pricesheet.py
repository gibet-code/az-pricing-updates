"""Download the latest EA or current-month MCA price sheet using ``az login``.

Examples (``--output`` is a file, not a directory)::

    python -m scripts.download_pricesheet --ea-billing-account-id <id> --output pricesheet.zip
    python -m scripts.download_pricesheet --mca-billing-account-id <id> --mca-billing-profile-id <id> --output pricesheet.zip
    python -m scripts.download_pricesheet --output pricesheet.zip   # pick from a list

With no account parameters (interactive terminal only), the accessible EA billing
accounts and MCA/MPA billing profiles are listed (Microsoft.Billing
2022-10-01-privatepreview billingAccounts with $expand; profiles fall back to the
2024-04-01 billingProfiles list when the expansion is empty) and one is selected
by number. The timeout starts after the selection.

EA discovers the latest available YYYYMM period, including pagination, using
Microsoft.Billing's 2022-10-01-privatepreview billingPeriods endpoint (the version
advertised by ARM and verified against an EA account). MCA's download endpoint
selects the current month itself and is requested with format=csv (its default is
JSON; EA already returns CSV). Both downloads use Cost Management 2026-06-01.

Generation can return HTTP 200 while still running, or HTTP 202. Polling follows
Location (or Azure-AsyncOperation), honors Retry-After, and continues until a
download URL is returned. Without a polling URL, a running HTTP 200 is polled by
repeating the original POST. --timeout bounds discovery and generation;
individual HTTP requests also have timeouts.

The report is saved as returned by Azure (a ZIP of CSV files), without
extraction. Parent directories are created; an existing file is replaced only
after a successful, nonempty download. Signed URLs and access tokens are not
logged. Requires Azure CLI login and billing price-sheet read permissions.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any, BinaryIO
from urllib.parse import quote, urlencode, urljoin, urlsplit
from uuid import uuid4

import requests
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential


_ARM = "https://management.azure.com"
_API_VERSION = "2026-06-01"
_PERIODS_API_VERSION = "2022-10-01-privatepreview"
_ACCOUNTS_API_VERSION = "2022-10-01-privatepreview"
_ACCOUNTS_EXPAND = "billingProfiles/invoiceSections,billingProfiles,billingProfiles/customers,subscriptions,soldTo"
_PROFILES_API_VERSION = "2024-04-01"
_EA_AGREEMENTS = {"EnterpriseAgreement"}
_MCA_AGREEMENTS = {"MicrosoftCustomerAgreement", "MicrosoftPartnerAgreement"}
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _arm_url(url: str) -> str:
    absolute = urljoin(_ARM, url)
    parsed = urlsplit(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "management.azure.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Azure returned a polling/page URL outside the ARM endpoint")
    return absolute


def _payload(response: requests.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object from Azure")
    return body


def _wait(response: requests.Response, interval: float, deadline: float) -> None:
    waits = [interval]
    for name in ("Retry-After", "x-ms-ratelimit-microsoft.consumption-retry-after"):
        raw = response.headers.get(name)
        if raw is None:
            continue
        try:
            delay = float(raw)
        except ValueError:
            delay = (
                parsedate_to_datetime(raw) - datetime.now(timezone.utc)
            ).total_seconds()
        if not math.isfinite(delay):
            raise ValueError(f"Invalid {name} delay from Azure")
        waits.append(delay)
    delay = max(waits)
    if monotonic() + delay >= deadline:
        raise TimeoutError("Timed out waiting for the price sheet")
    logging.info("Waiting %.1fs before checking Azure again", delay)
    sleep(delay)


def _request(
    session: requests.Session,
    credential: TokenCredential,
    method: str,
    url: str,
    interval: float,
    deadline: float,
) -> requests.Response:
    url = _arm_url(url)
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the price sheet")
        token = credential.get_token(f"{_ARM}/.default").token
        try:
            response = session.request(
                method, url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=min(120, remaining),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Azure price-sheet request failed (connection/timeout)") from exc
        if response.status_code in _RETRY_STATUSES:
            logging.warning("Azure returned HTTP %d; retrying", response.status_code)
            _wait(response, interval, deadline)
            continue
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"Azure price-sheet request failed (HTTP {response.status_code}): "
                f"{response.text}"
            )
        return response


def _get_all(
    session: requests.Session,
    credential: TokenCredential,
    url: str,
    interval: float,
    deadline: float,
) -> list[dict[str, Any]]:
    """Return every item of an ARM list response, following nextLink."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    while url:
        if url in seen:
            raise RuntimeError("Azure returned a repeated page")
        seen.add(url)
        body = _payload(_request(session, credential, "GET", url, interval, deadline))
        items.extend(body.get("value") or [])
        url = body.get("nextLink")
    return items


@dataclass(frozen=True)
class _Target:
    """An EA billing account (no profile) or one MCA/MPA billing profile."""

    account_id: str
    account_name: str
    profile_id: str | None = None
    profile_name: str | None = None

    @property
    def label(self) -> str:
        if self.profile_id is None:
            return f"[EA]  {self.account_name} (account {self.account_id})"
        return (
            f"[MCA] {self.account_name} / {self.profile_name} "
            f"(account {self.account_id}, profile {self.profile_id})"
        )


def _list_targets(
    session: requests.Session,
    credential: TokenCredential,
    interval: float,
    deadline: float,
) -> list[_Target]:
    query = urlencode({
        "$expand": _ACCOUNTS_EXPAND,
        "includeCAID": "true",
        "includeAllOrgs": "false",
        "api-version": _ACCOUNTS_API_VERSION,
    })
    accounts = _get_all(
        session, credential,
        f"{_ARM}/providers/Microsoft.Billing/billingAccounts?{query}", interval, deadline,
    )
    targets: list[_Target] = []
    for account in accounts:
        properties = account.get("properties") or {}
        agreement = properties.get("agreementType")
        account_id = account["name"]
        account_name = properties.get("displayName") or account_id
        if agreement in _EA_AGREEMENTS:
            targets.append(_Target(account_id, account_name))
        elif agreement in _MCA_AGREEMENTS:
            profiles = properties.get("billingProfiles")
            if isinstance(profiles, dict):
                profiles = profiles.get("value")
            if not profiles:
                # The expansion is not guaranteed to be populated; ask the dedicated endpoint.
                profiles = _get_all(
                    session, credential,
                    f"{_ARM}/providers/Microsoft.Billing/billingAccounts/"
                    f"{quote(account_id, safe='')}/billingProfiles"
                    f"?api-version={_PROFILES_API_VERSION}",
                    interval, deadline,
                )
            for profile in profiles:
                profile_properties = profile.get("properties") or {}
                # `name` can be an internal GUID; systemId is the short billing-profile ID.
                profile_id = profile_properties.get("systemId") or profile["name"]
                targets.append(_Target(
                    account_id, account_name, profile_id,
                    profile_properties.get("displayName") or profile_id,
                ))
    return targets


def _choose_target(targets: list[_Target]) -> _Target:
    if not targets:
        raise RuntimeError("No EA billing account or MCA billing profile is accessible to this identity")
    print("Available billing targets:")
    for number, target in enumerate(targets, start=1):
        print(f"  {number:>2}) {target.label}")
    while True:
        try:
            answer = input(f"Select a target (1-{len(targets)}, q to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise RuntimeError("Selection cancelled") from None
        if answer in {"q", "quit"}:
            raise RuntimeError("Selection cancelled")
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(targets):
            return targets[int(answer) - 1]
        print(f"Invalid selection: enter a number from 1 to {len(targets)}, or q.")


def _latest_ea_period(
    session: requests.Session,
    credential: TokenCredential,
    scope: str,
    interval: float,
    deadline: float,
) -> str:
    url = f"{_ARM}{scope}/billingPeriods?api-version={_PERIODS_API_VERSION}"
    periods: list[str] = []
    for period in _get_all(session, credential, url, interval, deadline):
        name = period["name"]
        if not isinstance(name, str) or len(name) != 6 or not name.isascii():
            raise ValueError("Azure returned an invalid EA billing period")
        datetime.strptime(name, "%Y%m")
        periods.append(name)
    if not periods:
        raise RuntimeError("No billing periods are available for this EA account")
    return max(periods)


def _report_url(
    session: requests.Session,
    credential: TokenCredential,
    url: str,
    interval: float,
    deadline: float,
) -> str:
    method = "POST"
    while True:
        response = _request(session, credential, method, url, interval, deadline)
        body = _payload(response)
        status = str(body.get("status", "")).lower()
        properties = body.get("properties") or {}
        # Location polling (MCA) completes with HTTP 200 and the report under publishedEntity.
        published = (body.get("publishedEntity") or {}).get("properties") or {}
        if body.get("errorMessage"):
            raise RuntimeError(f"Price-sheet generation failed: {body['errorMessage']}")
        if status in {"failed", "canceled", "cancelled"}:
            raise RuntimeError(f"Price-sheet generation {status}: {body.get('error') or properties}")
        report = (
            body.get("downloadUrl")
            or properties.get("downloadUrl")
            or properties.get("reportUrl")
            or published.get("downloadUrl")
        )
        if report:
            if not isinstance(report, str) or urlsplit(report).scheme != "https":
                raise ValueError("Azure returned an invalid HTTPS download URL")
            return report
        if status in {"completed", "succeeded"}:
            raise RuntimeError("Azure completed the price sheet without a download URL")
        if method == "GET" and response.status_code == 200 and not status:
            # On a polling URL, 200 means finished; looping would never end.
            raise RuntimeError(
                "Azure completed the price sheet in an unrecognized format "
                f"(response fields: {sorted(body)})"
            )
        if status not in {"", "running", "inprogress", "notstarted", "accepted"}:
            raise RuntimeError(f"Unexpected price-sheet status: {status}")
        location = response.headers.get("Location") or response.headers.get("Azure-AsyncOperation")
        if location:
            url = _arm_url(location)
            method = "GET"
        logging.info("Price-sheet generation: %s (HTTP %d)", status or "pending", response.status_code)
        _wait(response, interval, deadline)


def _create_temp(output: Path) -> tuple[Path, BinaryIO]:
    """Exclusively create a sibling temp file with a single attempt.

    tempfile.NamedTemporaryFile retries PermissionError ~10,000 times on Windows,
    which looks like a hang in unwritable folders such as C:\\.
    """
    path = output.with_name(f".{output.name}.{uuid4().hex}.part")
    return path, open(path, "xb")


def _check_output_writable(output: Path) -> None:
    """Fail before the (slow) generation if the output location can't be written."""
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        path, handle = _create_temp(output)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot write to output folder '{output.parent}': {exc.strerror or exc}"
        ) from exc
    handle.close()
    path.unlink(missing_ok=True)


def _download(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # A separate, unauthenticated request keeps the ARM bearer token off storage.
        with requests.get(url, stream=True, timeout=(30, 120)) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Report download failed (HTTP {response.status_code})")
            temporary, handle = _create_temp(output)
            with handle:
                size = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    handle.write(chunk)
                    size += len(chunk)
            if size == 0:
                raise RuntimeError("Azure returned an empty price-sheet download")
        os.replace(temporary, output)
        logging.info("Saved %s (%d bytes)", output, size)
    except requests.RequestException as exc:
        raise RuntimeError("Report download failed (connection/timeout)") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Must be a finite number greater than zero")
    return seconds


def _identifier(value: str) -> str:
    if not value.strip() or "/" in value or "\\" in value:
        raise argparse.ArgumentTypeError("Provide an ID, not a display name or ARM resource path")
    return value.strip()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    accounts = parser.add_mutually_exclusive_group()
    accounts.add_argument("--ea-billing-account-id", "--ea_billing_account_id", type=_identifier)
    accounts.add_argument("--mca-billing-account-id", "--mca_billing_account_id", type=_identifier)
    parser.add_argument("--mca-billing-profile-id", "--mca_billing_profile_id", type=_identifier,
                        help="Billing profile ID (the API calls this billingProfileName).")
    parser.add_argument("--output", type=Path, required=True, help="Destination file (usually .zip).")
    parser.add_argument("--poll-interval", type=_positive_seconds, default=10,
                        help="Minimum poll/retry interval in seconds (default: 10).")
    parser.add_argument("--timeout", type=_positive_seconds, default=1800,
                        help="Discovery/generation timeout in seconds (default: 1800).")
    args = parser.parse_args(argv)
    if bool(args.mca_billing_account_id) != bool(args.mca_billing_profile_id):
        parser.error("--mca-billing-account-id and --mca-billing-profile-id must be used together, without EA")
    if args.output.is_dir():
        parser.error("--output must be a file path, not an existing directory")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    interactive = not (args.ea_billing_account_id or args.mca_billing_account_id)
    if interactive and not sys.stdin.isatty():
        logging.error(
            "No billing target given and stdin is not interactive; pass "
            "--ea-billing-account-id or --mca-billing-account-id/--mca-billing-profile-id"
        )
        return 1
    try:
        _check_output_writable(args.output)
        with AzureCliCredential(process_timeout=60) as credential, requests.Session() as session:
            if interactive:
                target = _choose_target(_list_targets(
                    session, credential, args.poll_interval, monotonic() + args.timeout,
                ))
            else:
                target = _Target(
                    args.ea_billing_account_id or args.mca_billing_account_id,
                    "", args.mca_billing_profile_id,
                )
            # Started after selection so time spent at the prompt never counts.
            deadline = monotonic() + args.timeout
            scope = f"/providers/Microsoft.Billing/billingAccounts/{quote(target.account_id, safe='')}"
            if target.profile_id is None:
                period = _latest_ea_period(session, credential, scope, args.poll_interval, deadline)
                logging.info("Latest available EA billing period: %s", period)
                scope += f"/billingPeriods/{period}"
            else:
                scope += f"/billingProfiles/{quote(target.profile_id, safe='')}"
                logging.info("Requesting the current month's MCA price sheet")
            url = f"{_ARM}{scope}/providers/Microsoft.CostManagement/pricesheets/default/download?api-version={_API_VERSION}"
            if target.profile_id is not None:
                # MCA returns JSON files unless CSV is requested; EA already returns CSV.
                url += "&format=csv"
            report = _report_url(session, credential, url, args.poll_interval, deadline)
            _download(report, args.output)
    except (AzureError, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logging.error("Interrupted")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
