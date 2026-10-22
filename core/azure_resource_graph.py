
import logging
import re
import time
from pathlib import Path
from typing import Optional, Union

from azure.core.exceptions import HttpResponseError
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

# ── Azure data collection (Resource Graph) ────────────────────────────────────


_KQL_DIR = Path(__file__).parent / "kql"


def _retry_on_throttle(
    func,
    *args,
    max_retries: int = 6,
    base_delay: float = 10.0,
    **kwargs,
):
    """Call func(*args, **kwargs), retrying on HTTP 429 with exponential back-off."""
    delay = base_delay
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except HttpResponseError as exc:
            if exc.status_code != 429 or attempt == max_retries:
                raise
            retry_after: Optional[float] = None
            if exc.response is not None:
                raw = exc.response.headers.get("Retry-After")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        pass
            wait = retry_after if retry_after is not None else delay
            logging.warning(
                "HTTP 429 Too Many Requests — retrying in %.0fs (attempt %d/%d)",
                wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            delay = min(delay * 2, 120.0)


def _resolve_query(
    query_or_ref: Union[str, Path],
    bundled_kql_query_path: Optional[Path] = None,
) -> str:
    """Resolve a Resource Graph query reference into a KQL string.

    Accepts:
    - a :class:`pathlib.Path`: read as a KQL file (must exist).
    - a ``str`` containing a newline or a ``|`` pipe: treated as a raw KQL query.
    - a single-line ``str`` that points to an existing file on disk: read as a KQL file.
    - a single-line ``str`` matching the name of a bundled query (with or without the
      ``.kql`` extension). When ``bundled_kql_query_path`` is provided, that folder is
      searched first; the module's ``kql/`` folder is always searched as a fallback.
    """
    if isinstance(query_or_ref, Path):
        if not query_or_ref.is_file():
            raise FileNotFoundError(f"KQL query file not found: {query_or_ref!s}")
        return query_or_ref.read_text(encoding="utf-8")

    if not isinstance(query_or_ref, str):
        raise TypeError(
            f"query must be a str or pathlib.Path, got {type(query_or_ref).__name__}"
        )

    # Multi-line content or KQL pipes => treat as a raw query.
    if "\n" in query_or_ref or "|" in query_or_ref:
        return query_or_ref

    # Single-line string: try filesystem path first, then bundled name lookup.
    candidate_path = Path(query_or_ref)
    if candidate_path.is_file():
        return candidate_path.read_text(encoding="utf-8")

    bundled_name = query_or_ref if query_or_ref.endswith(".kql") else f"{query_or_ref}.kql"
    search_dirs: list[Path] = []
    if bundled_kql_query_path is not None:
        search_dirs.append(bundled_kql_query_path)
    search_dirs.append(_KQL_DIR)

    for directory in search_dirs:
        bundled_path = directory / bundled_name
        if bundled_path.is_file():
            return bundled_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"Could not resolve KQL query reference {query_or_ref!r}: not an existing file "
        f"and no matching bundled query found in {[str(d) for d in search_dirs]}."
    )


def run_graph_query(
    query: Union[str, Path],
    credential,
    subscription_ids: list[str] = None,
    silence_missing_order_by: bool = False,
    bundled_kql_query_path: Optional[Path] = None,
) -> list[dict]:
    """Execute a Resource Graph KQL query with pagination and throttle retry.

    ``query`` may be a raw KQL string, a :class:`pathlib.Path` to a ``.kql`` file,
    a string path to a ``.kql`` file, or the name (with or without ``.kql``) of a
    bundled query. When ``bundled_kql_query_path`` is provided, that folder is searched
    first for bundled-name lookups, falling back to the module's ``kql/`` folder.
    See :func:`_resolve_query`.

    When ``silence_missing_order_by`` is False (default), a warning is logged if the
    last non-comment KQL statement is not an ``order by`` / ``sort by`` clause. ARG
    pagination via ``skip_token`` requires deterministic ordering for stable results;
    queries that may exceed 500 rows should end with an ``order by``.
    """
    resolved_query = _resolve_query(query, bundled_kql_query_path=bundled_kql_query_path)

    if not silence_missing_order_by and not _ends_with_order_by(resolved_query):
        logging.warning(
            "ARG query does not end with an 'order by' statement; results may be "
            "duplicated or missing across pages. Pass silence_missing_order_by=True "
            "to suppress this warning. Query starts with: %s",
            _query_preview(resolved_query),
        )

    client = ResourceGraphClient(credential)
    results: list[dict] = []
    skip_token: Optional[str] = None

    # Scope the query at the Resource Graph service level via the native
    # ``subscriptions`` parameter on QueryRequest. This is independent of what
    # the KQL projects/filters, so it works for any query shape (incl. ones
    # that don't expose a SubscriptionId column).
    request_subscriptions = list(subscription_ids) if subscription_ids else None

    while True:
        options = QueryRequest(
            subscriptions=request_subscriptions,
            query=resolved_query,
            options=QueryRequestOptions(skip_token=skip_token, top=500),
        )
        response = _retry_on_throttle(client.resources, options)
        if response.data:
            results.extend(response.data)
        skip_token = response.skip_token
        if not skip_token:
            break

    return results


_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_ORDER_BY_RE = re.compile(r"^\|?\s*(?:order|sort)\s+by\b", re.IGNORECASE)


def _ends_with_order_by(query: str) -> bool:
    """Return True if the last non-comment, non-blank KQL statement is order/sort by."""
    cleaned = _LINE_COMMENT_RE.sub("", query)
    statements = [s.strip() for s in cleaned.split("|") if s.strip()]
    if not statements:
        return False
    return bool(_ORDER_BY_RE.match(statements[-1]) or _ORDER_BY_RE.match("|" + statements[-1]))


def _query_preview(query: str, limit: int = 80) -> str:
    """Return a short single-line preview of a KQL query for log messages."""
    flat = " ".join(query.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
