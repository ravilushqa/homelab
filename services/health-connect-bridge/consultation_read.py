#!/usr/bin/env python3
"""
consultation_read.py — Read-only health profile consultation tool.

Security properties:
  - Opens databases in SQLite URI mode with mode=ro.
  - PRAGMA query_only=ON enforced on every connection.
  - Rejects any database path (or ancestor directory) that is a symlink.
    Symlink check runs on the original path BEFORE making it absolute;
    Path.absolute() is used (not resolve()) to avoid following symlinks.
  - Raises FileNotFoundError for missing databases; never creates files.
  - No authentication tokens, no network calls, no mutations of any kind.

Output is JSON to stdout.  All timestamps are ISO-8601 UTC strings.
Day boundaries use Europe/Berlin local time (DST-aware via zoneinfo).

Provenance and interpretation notes:
  - date_nutrition_ingest (from health_records) and queue_by_state
    (from nutrition_queue) are independent data sources; they must never
    be summed or merged.
  - Records with source_key {"_no_metadata":true} cannot be attributed
    to any specific device or application.
  - queue 'acked' state means the phone client reported delivery to
    Health Connect after its insert attempt.  This is client-reported
    and is NOT independently verified; phone UI readback is required.
  - No cross-source totals are produced.
  - Queue-to-ingest correlation is provided only when health_records
    contains an exact client_record_id + version match (expl_id).
    In the current JSON payload format this field is absent, so
    correlation status will be not_verified for all current records.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

_BERLIN_TZ_NAME = "Europe/Berlin"

DEFAULT_HEALTH_DB = Path(
    "/home/claw/.hermes/profiles/health/workspace"
    "/health-connect-bridge/data/health.sqlite3"
)
DEFAULT_NUTRITION_DB = Path(
    "/home/claw/.hermes/profiles/health/workspace"
    "/health-connect-bridge/data/nutrition.sqlite3"
)

# Origins exclusively used by Pixel Watch on-device health services.
_PIXEL_WATCH_WEARABLE_ORIGINS = frozenset([
    "com.google.android.wearable.healthservices",
])

# Origins shared with non-watch apps but paired with a Pixel Watch device model.
_PIXEL_WATCH_SHARED_ORIGINS = frozenset([
    "com.google.android.apps.fitness",
])

_NOTICES = [
    (
        "date_nutrition_ingest and queue_by_state are independent data sources. "
        "Never sum or merge them. Ingest reflects records received from Health Connect. "
        "Queue reflects meals staged for write-back to Health Connect."
    ),
    "No cross-source totals are provided. Each source is listed separately.",
    (
        "queue state 'acked' means the phone client reported delivery to Health Connect "
        "after its insert attempt. This is client-reported and NOT independently verified. "
        "Independent phone UI readback or a fresh Health Connect export is required."
    ),
    (
        "Records with source_key containing _no_metadata:true cannot be attributed "
        "to any specific device or application."
    ),
    (
        "queue-to-ingest correlation requires exact client_record_id and version in "
        "health_records (expl_id column). This field is absent in the current JSON "
        "payload format; correlation will be not_verified for all current records."
    ),
]


# ── Path security ──────────────────────────────────────────────────────────────

def _check_no_symlinks(db_path: Path) -> None:
    """
    Raise RuntimeError if db_path or any ancestor directory is a symlink.
    Must be called before any sqlite3.connect().
    """
    if db_path.is_symlink():
        raise RuntimeError(f"DB path must not be a symlink: {db_path}")
    ancestor = db_path.parent
    while True:
        if ancestor.is_symlink():
            raise RuntimeError(f"DB ancestor must not be a symlink: {ancestor}")
        parent = ancestor.parent
        if parent == ancestor:
            break
        ancestor = parent


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """
    Open a SQLite database read-only with PRAGMA query_only=ON.

    Raises:
        FileNotFoundError — if db_path does not exist.
        RuntimeError — if db_path or any ancestor is a symlink,
                       or if db_path is not a regular file.
    Never creates files.

    Security order: symlink check runs on the original path (before
    absolute()) so that a symlink is never silently resolved away.
    Path.absolute() is used (not resolve()) to get an absolute path
    for as_uri() without following symlinks.
    """
    # Symlink check FIRST, on original path, before any path manipulation.
    _check_no_symlinks(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if not db_path.is_file():
        raise RuntimeError(f"DB path is not a regular file: {db_path}")
    # as_uri() requires an absolute path; use absolute() not resolve()
    # to avoid following symlinks.
    abs_path = db_path.absolute()
    uri = abs_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ── Timezone helpers ───────────────────────────────────────────────────────────

def _berlin_tz() -> Any:
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError(
            "zoneinfo module unavailable. Python 3.9+ is required for "
            "Europe/Berlin DST-aware day boundaries."
        )
    return ZoneInfo(_BERLIN_TZ_NAME)


def _today_berlin() -> str:
    """Return today's date string (YYYY-MM-DD) in Europe/Berlin timezone."""
    return datetime.now(_berlin_tz()).strftime("%Y-%m-%d")


def _day_utc_epoch_bounds(date_str: str) -> tuple:
    """
    Return (start_epoch_s, end_epoch_s) for date_str in Europe/Berlin.

    The range is half-open: [start_epoch_s, end_epoch_s).
    Midnight-to-midnight, DST-aware.
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"Invalid date: {date_str!r}. Expected YYYY-MM-DD."
        )
    tz = _berlin_tz()
    day_start = datetime(d.year, d.month, d.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    return day_start.timestamp(), day_end.timestamp()


def _epoch_to_iso(epoch_s: Any) -> Optional[str]:
    """Convert epoch seconds to ISO-8601 UTC string. Returns None on error."""
    if epoch_s is None:
        return None
    try:
        return (
            datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def _elapsed_hours_from_iso(ts_iso: Optional[str]) -> Optional[float]:
    """Hours elapsed since an ISO-8601 timestamp. Returns None if unparseable."""
    if not ts_iso:
        return None
    try:
        s = ts_iso.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return round(delta.total_seconds() / 3600, 2)
    except (ValueError, OSError, AttributeError):
        return None


def _elapsed_hours_from_epoch(epoch_s: Any) -> Optional[float]:
    """Hours elapsed since an epoch-seconds value. Returns None on error."""
    if epoch_s is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return round(delta.total_seconds() / 3600, 2)
    except (OSError, OverflowError, ValueError, TypeError):
        return None


# ── Source key helpers ─────────────────────────────────────────────────────────

def _parse_source_key(source_key: str) -> Any:
    """Parse source_key JSON. Returns dict on success, error dict on failure."""
    try:
        return json.loads(source_key)
    except (ValueError, TypeError):
        return {"_unparseable": source_key}


def _is_pixel_watch_source(parsed: dict) -> bool:
    """
    Return True if the source is identifiably Pixel Watch.

    Two cases:
    1. Origin is com.google.android.wearable.healthservices (exclusive to Pixel Watch).
    2. Origin is com.google.android.apps.fitness (shared) AND device model
       contains 'pixel watch' (case-insensitive).
    """
    origin = parsed.get("origin") or ""
    device = parsed.get("device")
    model = (device.get("model") or "").lower() if isinstance(device, dict) else ""

    if any(m in origin for m in _PIXEL_WATCH_WEARABLE_ORIGINS):
        return True
    if any(m in origin for m in _PIXEL_WATCH_SHARED_ORIGINS) and "pixel watch" in model:
        return True
    return False


def _provenance_notes(source_key: str, min_received_at: Optional[str]) -> list:
    """
    Return human-readable provenance notes for a (record_type, source_key) group.

    Notes are additive and non-exclusive; multiple may apply.
    Does not declare records real or fake; flags need for external validation.
    """
    notes: list = []
    parsed = _parse_source_key(source_key)

    if not isinstance(parsed, dict):
        notes.append("Source key could not be parsed as a JSON object.")
        return notes

    if parsed.get("_no_metadata"):
        notes.append(
            "No device metadata present for this source. "
            "Origin and device are unknown. "
            "These records cannot be attributed to any specific device or application."
        )
        return notes

    origin = parsed.get("origin")
    method = parsed.get("method")
    device = parsed.get("device")

    missing: list = []
    if origin is None:
        missing.append("data_origin")
    if method is None:
        missing.append("recording_method")
    if (
        not isinstance(device, dict)
        or device.get("manufacturer") is None
        or device.get("model") is None
    ):
        missing.append("device manufacturer/model")

    if missing:
        notes.append(
            "Incomplete source metadata: "
            + ", ".join(missing)
            + " null or absent. Device attribution may be ambiguous."
        )

    if _is_pixel_watch_source(parsed):
        first_date: Optional[str] = None
        if min_received_at:
            try:
                s = min_received_at.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                first_date = (
                    datetime.fromisoformat(s)
                    .astimezone(timezone.utc)
                    .strftime("%Y-%m-%d")
                )
            except (ValueError, AttributeError):
                pass
        notes.append(
            f"Source origin '{origin}' is associated with Pixel Watch. "
            f"Records from this source were first received "
            f"{first_date or '(date unknown)'}. "
            "Generic English exercise or record titles are not sufficient "
            "evidence to determine whether records represent real activity "
            "or data loaded during device setup. "
            "External provenance verification is required; "
            "this tool cannot make that determination without independent "
            "phone readback or a verified Health Connect export."
        )

    return notes


# ── Health DB queries ──────────────────────────────────────────────────────────

def _build_inventory(conn: sqlite3.Connection) -> list:
    """
    Return per-(record_type, source_key) inventory.

    Each entry includes count, latest record end, latest received, freshness
    in hours, and any provenance notes.  No numeric totals across sources.
    """
    rows = conn.execute(
        """
        SELECT
            record_type,
            source_key,
            COUNT(*)        AS cnt,
            MAX(record_end_s) AS max_end_s,
            MAX(received_at)  AS max_received,
            MIN(received_at)  AS min_received
        FROM health_records
        GROUP BY record_type, source_key
        ORDER BY record_type, source_key
        """
    ).fetchall()

    result: list = []
    for row in rows:
        sk = row["source_key"]
        notes = _provenance_notes(sk, row["min_received"])
        entry: dict = {
            "record_type": row["record_type"],
            "source_key": sk,
            "source_parsed": _parse_source_key(sk),
            "count": row["cnt"],
            "latest_record_end": _epoch_to_iso(row["max_end_s"]),
            # Age of the most recent health data (from record_end_s).
            # A large value with a small transport_age_hours means old data was
            # recently transported; a large value for both means stale data.
            "latest_record_age_hours": _elapsed_hours_from_epoch(row["max_end_s"]),
            "latest_received": row["max_received"],
            # Age since the bridge last received data for this source.
            "transport_age_hours": _elapsed_hours_from_iso(row["max_received"]),
        }
        if notes:
            entry["provenance_notes"] = notes
        result.append(entry)

    return result


def _format_health_record(row: sqlite3.Row) -> dict:
    """
    Format a health_records row for consultation output.

    Explicit identity metadata (client_record_id / version) is included only
    when present in the record (expl_ns / expl_id non-null).  Otherwise an
    explicit note marks this information as unavailable in the current payload
    format, so the caller is not misled about deduplication certainty.
    """
    try:
        record = json.loads(row["record_json"])
    except (ValueError, TypeError):
        record = {"_error": "record_json unparseable"}

    if row["expl_ns"] is not None and row["expl_id"] is not None:
        identity_meta: dict = {
            "namespace": row["expl_ns"],
            "id": row["expl_id"],
            "client_record_version": row["client_record_version"],
        }
    else:
        identity_meta = {
            "available": False,
            "note": (
                "client_record_id and version are not present in the current "
                "payload format. Deduplication uses temporal key only."
            ),
        }

    return {
        "record_type": row["record_type"],
        "record_start": _epoch_to_iso(row["record_start_s"]),
        "record_end": _epoch_to_iso(row["record_end_s"]),
        "data_origin": row["data_origin"],
        "recording_method": row["recording_method"],
        "received_at": row["received_at"],
        "identity_meta": identity_meta,
        "record": record,
    }


def _query_records_for_day(
    conn: sqlite3.Connection,
    record_type: str,
    start_s: float,
    end_s: float,
) -> list:
    """
    Return records of record_type whose time range overlaps [start_s, end_s).

    Grouped by source_key; no cross-source aggregation.
    Overlap condition: record_end_s > start_s AND record_start_s < end_s.
    """
    rows = conn.execute(
        """
        SELECT *
        FROM health_records
        WHERE record_type = ?
          AND record_end_s  > ?
          AND record_start_s < ?
        ORDER BY source_key, record_start_s
        """,
        (record_type, start_s, end_s),
    ).fetchall()

    by_source: dict = {}
    for row in rows:
        sk = row["source_key"]
        if sk not in by_source:
            by_source[sk] = {
                "source_key": sk,
                "source_parsed": _parse_source_key(sk),
                "records": [],
            }
        by_source[sk]["records"].append(_format_health_record(row))

    return list(by_source.values())


# ── Queue-to-ingest correlation ────────────────────────────────────────────────

def _build_correlation_result(
    row: sqlite3.Row,
    cver: Optional[int],
    queue_entry: dict,
    match_via: str,
) -> dict:
    """Build correlation result dict after an identity match is found."""
    stored_ver = row["client_record_version"]
    if stored_ver != cver:
        return {
            "status": "identity_match_version_mismatch",
            "match_via": match_via,
            "health_record_version": stored_ver,
            "queue_version": cver,
            "health_record_received_at": row["received_at"],
            "note": (
                "Same client_record_id found but version differs; "
                "may be a stale or edited entry."
            ),
        }

    content_issues: list = []
    try:
        rec = json.loads(row["record_json"])
        for field in ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g"):
            q_val = queue_entry.get(field)
            r_val = rec.get(field)
            if q_val is not None and r_val is not None:
                if abs(float(q_val) - float(r_val)) > 0.01:
                    content_issues.append(
                        f"{field}: queue={q_val} health_record={r_val}"
                    )
    except (ValueError, TypeError, AttributeError):
        content_issues.append("record_json unparseable; macros not compared")

    result: dict = {
        "status": "identity_match",
        "match_via": match_via,
        "health_record_version": stored_ver,
        "health_record_received_at": row["received_at"],
        "content_match": len(content_issues) == 0,
    }
    if content_issues:
        result["content_discrepancies"] = content_issues
    return result


def _correlate_queue_entry(
    queue_entry: dict,
    health_conn: Optional[sqlite3.Connection],
) -> dict:
    """
    Attempt to correlate a queue entry with a health_records ingest record.

    Correlation requires an exact match on client_record_id AND
    client_record_version.  Two lookup paths are tried:

    1. Primary (expl_ns='client'): health_records.expl_id = client_record_id
       with expl_ns = 'client'.  This is the direct path when the JSON payload
       includes metadata.client_record_id as the primary explicit ID.

    2. Secondary (record_json metadata): when a server-assigned HC ID has
       precedence in expl_id (expl_ns='server'), the client ID may only appear
       in record_json.metadata.client_record_id.  A small scan of server-ID
       records is performed to find it.

    A record whose expl_ns='server' is NEVER matched against the queue
    client_record_id via expl_id, even if the values happen to be equal.
    Server-namespace and client-namespace IDs are distinct identity spaces.

    In the current JSON payload format neither ID field is present, so status
    will be not_verified for all current records.
    """
    if health_conn is None:
        return {
            "status": "not_verified",
            "reason": "health_db unavailable",
        }

    crid = queue_entry.get("client_record_id")
    cver = queue_entry.get("client_record_version")

    if not crid:
        return {
            "status": "not_verified",
            "reason": "queue entry has no client_record_id",
        }

    try:
        # ── Primary: expl_ns='client' only ────────────────────────────────
        row = health_conn.execute(
            """
            SELECT record_start_s, record_end_s, record_json,
                   client_record_version, received_at
            FROM health_records
            WHERE record_type = 'nutrition'
              AND expl_ns = 'client'
              AND expl_id = ?
            LIMIT 1
            """,
            (crid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {
            "status": "not_verified",
            "reason": "health_records query failed (schema incompatibility?)",
        }

    if row is not None:
        return _build_correlation_result(row, cver, queue_entry, "expl_id_client_ns")

    try:
        # ── Secondary: server ID primary, client ID in record_json ─────────
        # Handles future payloads where both server and client IDs exist and
        # the server ID takes precedence in expl_id.  Scans a bounded set of
        # server-ID records and parses record_json for the client ID.
        server_rows = health_conn.execute(
            """
            SELECT record_start_s, record_end_s, record_json,
                   client_record_version, received_at
            FROM health_records
            WHERE record_type = 'nutrition'
              AND expl_ns = 'server'
            LIMIT 200
            """,
        ).fetchall()
    except sqlite3.OperationalError:
        server_rows = []

    for sr in server_rows:
        try:
            rec_data = json.loads(sr["record_json"])
            meta = rec_data.get("metadata") or {}
            if meta.get("client_record_id") == crid:
                return _build_correlation_result(
                    sr, cver, queue_entry, "record_json_metadata_client_id"
                )
        except (ValueError, TypeError):
            continue

    return {
        "status": "not_verified",
        "reason": (
            "no health_records entry with matching client_record_id "
            "(checked expl_ns='client' expl_id and record_json metadata); "
            "client_record_id is absent in the current JSON payload format"
        ),
    }


# ── Nutrition queue queries ────────────────────────────────────────────────────

def _format_queue_row(row: sqlite3.Row) -> dict:
    """Format a nutrition_queue row for consultation output."""
    return {
        "client_record_id": row["client_record_id"],
        "client_record_version": row["client_record_version"],
        "state": row["state"],
        "name": row["name"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "energy_kcal": row["energy_kcal"],
        "protein_g": row["protein_g"],
        "carbohydrate_g": row["carbohydrate_g"],
        "fat_g": row["fat_g"],
        "description": row["description"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "acked_at": row["acked_at"],
        "delivery_possible": bool(row["delivery_possible"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _query_queue_for_day(
    conn: sqlite3.Connection,
    date_str: str,
    health_conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """
    Return queue records whose start_time falls within date_str (Europe/Berlin).

    Grouped by state: draft, confirmed, acked, cancelled.
    Records are compared using epoch conversion for DST-correct boundaries.

    Each entry includes a correlation field: identity match against health_records
    using exact client_record_id + version (expl_id). Status is not_verified when
    health_records lacks explicit IDs (the current JSON payload format).

    Note: 'acked' state is client-reported delivery to HC; not independently verified.
    """
    tz = _berlin_tz()
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date: {date_str!r}")
    day_start = datetime(d.year, d.month, d.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    day_start_s = day_start.timestamp()
    day_end_s = day_end.timestamp()

    rows = conn.execute(
        "SELECT * FROM nutrition_queue ORDER BY created_at"
    ).fetchall()

    by_state: dict = {"draft": [], "confirmed": [], "acked": [], "cancelled": []}

    for row in rows:
        try:
            s = row["start_time"].strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            epoch = datetime.fromisoformat(s).timestamp()
        except (ValueError, AttributeError, TypeError):
            continue

        if day_start_s <= epoch < day_end_s:
            state = row["state"]
            if state in by_state:
                entry = _format_queue_row(row)
                entry["correlation"] = _correlate_queue_entry(entry, health_conn)
                by_state[state].append(entry)

    return by_state


# ── Run ────────────────────────────────────────────────────────────────────────

def run(
    health_db: Path,
    nutrition_db: Path,
    date_str: Optional[str] = None,
) -> dict:
    """
    Execute consultation read. Returns a JSON-serialisable dict.

    DB errors are captured per-database in the 'errors' key rather than
    raised, so a partial result is returned when one DB is unavailable.
    """
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if date_str is None:
        date_str = _today_berlin()
    else:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Invalid date: {date_str!r}. Expected YYYY-MM-DD."
            )

    day_start_s, day_end_s = _day_utc_epoch_bounds(date_str)

    errors: dict = {}
    inventory: list = []
    date_nutrition_ingest: list = []
    date_exercise: list = []
    queue_by_state: dict = {}

    # Keep health_conn open through the nutrition query so the correlation
    # function can search health_records by expl_id.  Both connections are
    # closed in the outer finally block.
    health_conn = None
    nutrition_conn = None
    try:
        try:
            health_conn = _open_ro(health_db)
            inventory = _build_inventory(health_conn)
            date_nutrition_ingest = _query_records_for_day(
                health_conn, "nutrition", day_start_s, day_end_s
            )
            date_exercise = _query_records_for_day(
                health_conn, "exercise", day_start_s, day_end_s
            )
        except FileNotFoundError as exc:
            errors["health_db"] = {"error": "not_found", "detail": str(exc)}
            health_conn = None
        except RuntimeError as exc:
            errors["health_db"] = {"error": "rejected", "detail": str(exc)}
            health_conn = None
        except sqlite3.OperationalError as exc:
            errors["health_db"] = {"error": "sqlite_error", "detail": str(exc)}
            health_conn = None

        try:
            nutrition_conn = _open_ro(nutrition_db)
            queue_by_state = _query_queue_for_day(
                nutrition_conn, date_str, health_conn
            )
        except FileNotFoundError as exc:
            errors["nutrition_db"] = {"error": "not_found", "detail": str(exc)}
        except RuntimeError as exc:
            errors["nutrition_db"] = {"error": "rejected", "detail": str(exc)}
        except sqlite3.OperationalError as exc:
            errors["nutrition_db"] = {"error": "sqlite_error", "detail": str(exc)}
    finally:
        for _c in (health_conn, nutrition_conn):
            if _c is not None:
                try:
                    _c.close()
                except Exception:
                    pass

    result: dict = {
        "generated_at": now_iso,
        "query_date": date_str,
        "query_timezone": _BERLIN_TZ_NAME,
        "health_db": str(health_db),
        "nutrition_db": str(nutrition_db),
        "notices": _NOTICES,
        "inventory": inventory,
        "date_nutrition_ingest": date_nutrition_ingest,
        "date_exercise": date_exercise,
        "queue_by_state": queue_by_state,
    }

    if errors:
        result["errors"] = errors

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consultation_read",
        description=(
            "Read-only health profile consultation tool. "
            "Outputs JSON to stdout. Never creates databases or network connections."
        ),
    )
    parser.add_argument(
        "--health-db",
        type=Path,
        default=DEFAULT_HEALTH_DB,
        metavar="PATH",
        help="Path to health SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--nutrition-db",
        type=Path,
        default=DEFAULT_NUTRITION_DB,
        metavar="PATH",
        help="Path to nutrition queue SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Date for record lookup in Europe/Berlin timezone "
            "(default: today in Europe/Berlin)"
        ),
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(
            health_db=args.health_db,
            nutrition_db=args.nutrition_db,
            date_str=args.date,
        )
    except ValueError as exc:
        print(json.dumps({"error": "invalid_argument", "detail": str(exc)}, indent=2))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "detail": str(exc)}, indent=2
            )
        )
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
