"""Snapshot read/write helpers used by every source module.

Snapshots track the time the data was fetched from upstream so
``SourceMetadata.fetched_at`` is populated whether the data came from
upstream or from disk. Two storage strategies depending on file format:

- **JSON snapshots**: wrapped in an envelope
  ``{"fetched_at": "...", "source_url": "...", "payload": <raw>}``.
  Loaders unwrap transparently. Legacy bare list/dict files are
  tolerated — ``fetched_at`` falls back to file mtime and a one-time
  warning recommends ``refresh_snapshot()``.
- **Non-JSON snapshots** (HTML, CSV): stored raw, paired with a sidecar
  ``<name>.meta.json`` carrying
  ``{"fetched_at": "...", "source_url": "...", "etag": "...", "last_modified": "..."}``.
  Missing sidecar → fall back to file mtime.

All writes are atomic: write to ``*.tmp`` then ``os.replace()``.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ._base import SnapshotNotFoundError

logger = logging.getLogger(__name__)

# Mark snapshots stale (INFO log) when older than this many days.
STALE_AFTER_DAYS = 30

# Envelope keys used by JSON snapshots.
_ENVELOPE_KEYS = {"fetched_at", "source_url", "payload"}

# Track files we've already warned about for legacy/stale snapshots so the
# log doesn't repeat for every fetch in the same process.
_warned_legacy: set[Path] = set()
_warned_stale: set[Path] = set()


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _atomic_write(path: Path, write_callback) -> None:
    """Atomically write ``path`` by going through a ``*.tmp`` sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    write_callback(tmp)
    os.replace(tmp, path)


def _maybe_log_stale(path: Path, fetched_at: datetime | None, source: str) -> None:
    if fetched_at is None or path in _warned_stale:
        return
    age = datetime.now(tz=timezone.utc) - fetched_at
    if age.days >= STALE_AFTER_DAYS:
        _warned_stale.add(path)
        logger.info(
            "Snapshot for source %r is %d days old (%s); "
            "consider running refresh_snapshot() to update %s",
            source, age.days, fetched_at.isoformat(), path,
        )


# --------------------------------------------------------------------------
# JSON envelope I/O
# --------------------------------------------------------------------------

def write_json_snapshot(
    path: Path,
    payload,
    *,
    source_url: str | None,
    fetched_at: datetime | None = None,
) -> None:
    """Write ``payload`` wrapped in the standard envelope to ``path``."""
    envelope = {
        "fetched_at": (fetched_at or datetime.now(tz=timezone.utc)).isoformat(),
        "source_url": source_url,
        "payload": payload,
    }

    def _write(tmp: Path) -> None:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2, ensure_ascii=False)

    _atomic_write(path, _write)


def read_json_snapshot(path: Path, *, source: str) -> tuple[object, datetime | None]:
    """Return ``(payload, fetched_at)`` from ``path``.

    Tolerates legacy bare list/dict files: ``fetched_at`` becomes the file
    mtime and a one-time warning is logged.
    """
    if not path.exists():
        raise SnapshotNotFoundError(
            f"Snapshot for source {source!r} not found at {path}. "
            f"Run the source's refresh_snapshot() to create it."
        )

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict) and _ENVELOPE_KEYS.issubset(raw.keys()):
        fetched_at = _parse_iso(raw.get("fetched_at", "")) or _file_mtime_utc(path)
        _maybe_log_stale(path, fetched_at, source)
        return raw["payload"], fetched_at

    # Legacy bare payload — warn once.
    if path not in _warned_legacy:
        _warned_legacy.add(path)
        logger.warning(
            "Snapshot %s for source %r is in the legacy bare format; "
            "fetched_at will fall back to file mtime. "
            "Run refresh_snapshot() to upgrade it.",
            path, source,
        )
    fetched_at = _file_mtime_utc(path)
    _maybe_log_stale(path, fetched_at, source)
    return raw, fetched_at


# --------------------------------------------------------------------------
# Sidecar I/O for non-JSON snapshots
# --------------------------------------------------------------------------

def _sidecar_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(snapshot_path.suffix + ".meta.json")


def write_raw_snapshot(
    path: Path,
    data: bytes | str,
    *,
    source_url: str | None,
    fetched_at: datetime | None = None,
    extra_meta: dict | None = None,
) -> None:
    """Write raw ``data`` to ``path`` plus a sidecar ``<path>.meta.json``."""
    meta = {
        "fetched_at": (fetched_at or datetime.now(tz=timezone.utc)).isoformat(),
        "source_url": source_url,
    }
    if extra_meta:
        meta.update(extra_meta)

    if isinstance(data, str):
        encoded = data.encode("utf-8")
    else:
        encoded = data

    def _write_payload(tmp: Path) -> None:
        with tmp.open("wb") as f:
            f.write(encoded)

    def _write_meta(tmp: Path) -> None:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    _atomic_write(path, _write_payload)
    _atomic_write(_sidecar_path(path), _write_meta)


def read_raw_snapshot(
    path: Path,
    *,
    source: str,
    binary: bool = False,
) -> tuple[bytes | str, datetime | None]:
    """Return ``(data, fetched_at)`` from a raw snapshot file + sidecar."""
    if not path.exists():
        raise SnapshotNotFoundError(
            f"Snapshot for source {source!r} not found at {path}. "
            f"Run the source's refresh_snapshot() to create it."
        )

    if binary:
        data: bytes | str = path.read_bytes()
    else:
        data = path.read_text(encoding="utf-8")

    sidecar = _sidecar_path(path)
    fetched_at: datetime | None = None
    if sidecar.exists():
        try:
            with sidecar.open(encoding="utf-8") as f:
                meta = json.load(f)
            fetched_at = _parse_iso(meta.get("fetched_at", ""))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse sidecar %s: %r", sidecar, exc)
    if fetched_at is None:
        if path not in _warned_legacy:
            _warned_legacy.add(path)
            logger.warning(
                "Snapshot %s for source %r has no sidecar; "
                "fetched_at falls back to file mtime. "
                "Run refresh_snapshot() to create the sidecar.",
                path, source,
            )
        fetched_at = _file_mtime_utc(path)

    _maybe_log_stale(path, fetched_at, source)
    return data, fetched_at
