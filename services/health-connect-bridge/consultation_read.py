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
  - queue 'acked' state means the phone client reported a successful
    Health Connect insert.  This is client-reported and NOT independently
    verified; phone UI readback is required.
  - No cross-source totals are produced.
  - Queue-to-ingest correlation is provided only when health_records
    contains an exact client_record_id + version match (expl_id).
    In the current JSON payload format this field is absent, so
    correlation status will be not_verified for all current records.
"""

import argparse
import json
import math
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

# Origins that are heuristic markers for Pixel Watch — NOT verified product
# guarantees.  Two or more devices/apps may share these origins.
_PIXEL_WATCH_WEARABLE_ORIGINS = frozenset([
    "com.google.android.wearable.healthservices",
])

# Expected data_origin for nutrition records written by this app.
_EXPECTED_NUTRITION_ORIGIN = "com.ravil.healthconnect.nutrition"

# Canonical field aliases used in HC nutrition record JSON exports.
# Keys are queue column names; values are ordered lists of aliases to try.
_MACRO_ALIASES: dict = {
    "energy_kcal":    ["energy_kcal", "calories", "total_calories"],
    "protein_g":      ["protein_g", "protein_grams"],
    "carbohydrate_g": ["carbohydrate_g", "carbohydrate_grams", "carbs_grams", "carbs"],
    "fat_g":          ["fat_g", "fat_grams"],
}

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
        "queue state 'acked' means the phone client reported a successful Health Connect "
        "insert. This is client-reported and NOT independently verified. "
        "Independent phone UI readback or a fresh Health Connect export is required. "
        "A record cancelled after fetch cannot guarantee removal from Health Connect."
    ),
    (
        "queue state 'draft' is unapproved and is NOT pending automatic delivery. "
        "Only 'confirmed' entries are queued for phone delivery."
    ),
    (
        "queue_day_report lists pending (draft, confirmed), done (acked) and excluded "
        "(cancelled, unknown state, unusable time) queue records with reasons. It has no "
        "totals; drafts are never part of a day's intake."
    ),
    (
        "Records with source_key containing _no_metadata:true cannot be attributed "
        "to any specific device or application."
    ),
    (
        "queue-to-ingest correlation requires exact client_record_id and expected "
        "data_origin in health_records (expl_id with expl_ns='client'). "
        "In payload formats without explicit IDs, status will be not_verified. "
        "Check actual record expl_ns/expl_id columns; this tool cannot determine "
        "payload format at runtime."
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
    Return True if the source matches heuristic Pixel Watch markers.

    These are heuristic patterns, NOT verified product guarantees.  Other
    devices or apps may share the same origins.

    Two cases (both heuristic):
    1. Origin is com.google.android.wearable.healthservices (often associated
       with Pixel Watch wearable services, but not exclusively guaranteed).
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
            f"Source origin '{origin}' is a heuristic marker associated with "
            "Pixel Watch — this is NOT a verified product guarantee; other "
            "devices or apps may share the same origin. "
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

def _extract_macro_value(rec: dict, field: str) -> tuple:
    """
    Extract a macro value from a health record dict using canonical aliases.

    Returns (float_value, None) on success or (None, reason_str) on error.
    Never raises.

    Validation rules:
    - Bool values are rejected (isinstance check before float()).
    - Non-numeric types (strings, lists, etc.) are rejected.
    - Nonfinite (NaN, Inf) values are rejected.
    - Negative values are rejected.
    - Multiple aliases with conflicting values → error (avoids silently picking
      one when the data is inconsistent).
    """
    if not isinstance(rec, dict):
        return None, "record is not a JSON object"

    aliases = _MACRO_ALIASES.get(field, [field])
    found: dict = {}
    for alias in aliases:
        raw = rec.get(alias)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return None, f"{alias!r}: boolean value rejected"
        if not isinstance(raw, (int, float)):
            return None, f"{alias!r}: non-numeric value {raw!r}"
        val = float(raw)
        if not math.isfinite(val):
            return None, f"{alias!r}: nonfinite ({val})"
        if val < 0:
            return None, f"{alias!r}: negative ({val})"
        found[alias] = val

    if not found:
        return None, f"not found under aliases {aliases}"

    values = list(found.values())
    if len(values) > 1:
        if max(values) - min(values) > 0.01:
            return None, f"conflicting alias values: {found}"

    return values[0], None


def _iso_to_epoch(ts: str) -> Optional[float]:
    """Convert ISO-8601 string to epoch seconds for queue date filtering.
    Returns None on failure. Does not reject naive timestamps."""
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _iso_to_epoch_strict(ts: str) -> tuple:
    """
    Convert ISO-8601 aware timestamp to epoch seconds for correlation.

    Returns (epoch_s, None) on success, (None, reason_str) on failure.
    Rejects naive timestamps (no explicit timezone).
    """
    if not ts:
        return None, "empty timestamp"
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None, f"naive timestamp rejected (no timezone): {ts!r}"
        return dt.timestamp(), None
    except (ValueError, AttributeError, TypeError) as exc:
        return None, f"unparseable: {exc}"


def _build_correlation_result(
    row: sqlite3.Row,
    cver: Optional[int],
    queue_entry: dict,
    match_via: str,
) -> dict:
    """
    Build correlation result after identity (client_record_id) match.

    Version check:
      - Both cver and stored_ver must be plain integers (not bool, not None).
      - If record_json contains metadata.client_record_version, that raw value
        is also checked for bool/non-integer type.
      - Any mismatch or invalid type → identity_match_version_mismatch.

    Content check (all four required macros + start/end instants):
      - Queue macro values are validated: must be finite non-negative numeric
        non-bool.  Malformed queue values → content_match=False, never True.
      - Record macros extracted via canonical aliases; bool, string, nonfinite,
        negative, alias conflict → content_match=False.
      - Start/end instants compared with zero tolerance (exact epoch equality).
        Nonfinite epoch values → content_match=False.
      - Never returns content_match=True when any field is absent or malformed.
    """
    stored_ver = row["client_record_version"]

    # Parse record_json once; used for both metadata version check and content.
    rec: Any = None
    rec_parse_err: Optional[str] = None
    try:
        rec = json.loads(row["record_json"])
    except (ValueError, TypeError) as exc:
        rec_parse_err = str(exc)

    # ── Version check ─────────────────────────────────────────────────────────
    cver_ok = isinstance(cver, int) and not isinstance(cver, bool)
    stored_ok = isinstance(stored_ver, int) and not isinstance(stored_ver, bool)

    # Inspect raw metadata version in record_json if the key is explicitly present.
    # Key presence (even with null value) requires a valid integer matching cver.
    # Absence of the key allows falling through to the stored_ver comparison.
    if rec is not None and isinstance(rec, dict):
        raw_meta = rec.get("metadata")
        if isinstance(raw_meta, dict) and "client_record_version" in raw_meta:
            raw_ver = raw_meta["client_record_version"]
            # null, bool, non-integer, or mismatch with queue cver → reject.
            if raw_ver is None:
                note = "record_json metadata.client_record_version is null"
            elif isinstance(raw_ver, bool):
                note = (
                    f"record_json metadata.client_record_version is boolean {raw_ver!r}"
                )
            elif not isinstance(raw_ver, int):
                note = (
                    f"record_json metadata.client_record_version is not a plain integer "
                    f"(got {type(raw_ver).__name__} {raw_ver!r})"
                )
            elif raw_ver != cver:
                note = (
                    f"record_json metadata.client_record_version {raw_ver!r} "
                    f"does not match queue version {cver!r}"
                )
            else:
                note = None  # raw_ver is a valid integer equal to cver

            if note is not None:
                return {
                    "status": "identity_match_version_mismatch",
                    "match_via": match_via,
                    "health_record_version": stored_ver,
                    "queue_version": cver,
                    "health_record_received_at": row["received_at"],
                    "note": note,
                }

    if not cver_ok or not stored_ok or stored_ver != cver:
        return {
            "status": "identity_match_version_mismatch",
            "match_via": match_via,
            "health_record_version": stored_ver,
            "queue_version": cver,
            "health_record_received_at": row["received_at"],
            "note": (
                "Version mismatch or non-integer version; "
                "may be a stale, edited, or malformed entry."
            ),
        }

    content_issues: list = []

    if rec_parse_err:
        return {
            "status": "identity_match",
            "match_via": match_via,
            "health_record_version": stored_ver,
            "health_record_received_at": row["received_at"],
            "content_match": False,
            "content_issues": [f"record_json unparseable: {rec_parse_err}"],
        }

    if not isinstance(rec, dict):
        return {
            "status": "identity_match",
            "match_via": match_via,
            "health_record_version": stored_ver,
            "health_record_received_at": row["received_at"],
            "content_match": False,
            "content_issues": ["record_json is not a JSON object"],
        }

    # ── Macro comparison ──────────────────────────────────────────────────────
    for field in ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g"):
        q_val = queue_entry.get(field)

        # Validate queue value: must be finite non-negative non-bool numeric.
        if q_val is None:
            content_issues.append(f"{field}: absent from queue entry")
            continue
        if isinstance(q_val, bool):
            content_issues.append(f"{field}: queue value is boolean")
            continue
        if not isinstance(q_val, (int, float)):
            content_issues.append(f"{field}: queue value is not numeric ({q_val!r})")
            continue
        q_float = float(q_val)
        if not math.isfinite(q_float):
            content_issues.append(f"{field}: queue value is nonfinite ({q_float})")
            continue
        if q_float < 0:
            content_issues.append(f"{field}: queue value is negative ({q_float})")
            continue

        # Extract and validate record value.
        r_val, r_err = _extract_macro_value(rec, field)
        if r_err:
            content_issues.append(f"{field}: health record — {r_err}")
            continue

        if abs(q_float - r_val) > 0.01:
            content_issues.append(f"{field}: queue={q_float} health_record={r_val}")

    # ── Time comparison (exact instants, zero tolerance) ──────────────────────
    for time_field, r_epoch_field in (
        ("start_time", "record_start_s"),
        ("end_time", "record_end_s"),
    ):
        q_ts, q_err = _iso_to_epoch_strict(queue_entry.get(time_field) or "")
        if q_err:
            content_issues.append(f"{time_field}: {q_err}")
            continue

        r_raw = row[r_epoch_field]
        if r_raw is None:
            content_issues.append(f"{r_epoch_field}: absent from health record")
            continue
        if isinstance(r_raw, bool) or not isinstance(r_raw, (int, float)):
            content_issues.append(
                f"{r_epoch_field}: not numeric ({r_raw!r})"
            )
            continue
        r_epoch = float(r_raw)
        if not math.isfinite(r_epoch):
            content_issues.append(f"{r_epoch_field}: nonfinite ({r_epoch})")
            continue
        if q_ts != r_epoch:
            content_issues.append(
                f"{time_field} mismatch: queue={q_ts} health_record={r_epoch}"
            )

    result: dict = {
        "status": "identity_match",
        "match_via": match_via,
        "health_record_version": stored_ver,
        "health_record_received_at": row["received_at"],
        "content_match": len(content_issues) == 0,
    }
    if content_issues:
        result["content_issues"] = content_issues
    return result


def _correlate_queue_entry(
    queue_entry: dict,
    health_conn: Optional[sqlite3.Connection],
) -> dict:
    """
    Attempt to correlate a queue entry with a health_records ingest record.

    Correlation requires:
      - Exact client_record_id match (client namespace only)
      - data_origin = _EXPECTED_NUTRITION_ORIGIN (prevents cross-app collision)
      - Integer (non-bool) client_record_version match
      - Content check on all four macros + start/end instants

    Two lookup paths:
    1. Primary: expl_ns='client' AND expl_id=client_record_id
    2. Secondary: server ID primary, client ID in record_json.metadata

    A server-namespace expl_id is NEVER matched against client_record_id.
    Unknown/mismatched origin → identity_found_wrong_origin, not success.
    Missing/nonfinite/malformed macros → content_match=False, never True.
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
        # ── Primary: expl_ns='client', data_origin in SQL WHERE ────────────
        # Origin is enforced in SQL so a correct-origin record is found even
        # if a wrong-origin record with the same expl_id exists.
        row = health_conn.execute(
            """
            SELECT record_start_s, record_end_s, record_json,
                   client_record_version, received_at, data_origin
            FROM health_records
            WHERE record_type = 'nutrition'
              AND expl_ns = 'client'
              AND expl_id = ?
              AND data_origin = ?
            LIMIT 1
            """,
            (crid, _EXPECTED_NUTRITION_ORIGIN),
        ).fetchone()
    except sqlite3.OperationalError:
        return {
            "status": "not_verified",
            "reason": "health_records query failed (schema incompatibility?)",
        }

    if row is not None:
        return _build_correlation_result(row, cver, queue_entry, "expl_id_client_ns")

    # Diagnostic: check if a wrong-origin record exists with this client_id.
    try:
        diag = health_conn.execute(
            """
            SELECT data_origin FROM health_records
            WHERE record_type = 'nutrition'
              AND expl_ns = 'client'
              AND expl_id = ?
            LIMIT 1
            """,
            (crid,),
        ).fetchone()
        if diag is not None:
            return {
                "status": "identity_found_wrong_origin",
                "match_via": "expl_id_client_ns",
                "found_origin": diag["data_origin"],
                "expected_origin": _EXPECTED_NUTRITION_ORIGIN,
                "note": (
                    "client_record_id matched but data_origin differs; "
                    "may be from a different app. Not correlated."
                ),
            }
    except sqlite3.OperationalError:
        pass

    try:
        # ── Secondary: server ID primary, client ID in record_json ─────────
        # Bounded scan of server-ID records with expected origin.
        # This scan is limited to 200 rows; results may be incomplete for
        # large tables with many server-ID nutrition records.
        server_rows = health_conn.execute(
            """
            SELECT record_start_s, record_end_s, record_json,
                   client_record_version, received_at, data_origin
            FROM health_records
            WHERE record_type = 'nutrition'
              AND expl_ns = 'server'
              AND data_origin = ?
            LIMIT 200
            """,
            (_EXPECTED_NUTRITION_ORIGIN,),
        ).fetchall()
    except sqlite3.OperationalError:
        server_rows = []

    for sr in server_rows:
        try:
            rec_data = json.loads(sr["record_json"])
            meta = rec_data.get("metadata") or {}
            if meta.get("client_record_id") != crid:
                continue
            return _build_correlation_result(
                sr, cver, queue_entry, "record_json_metadata_client_id"
            )
        except (ValueError, TypeError):
            continue

    return {
        "status": "not_verified",
        "reason": (
            "no health_records entry with matching client_record_id "
            f"and expected origin '{_EXPECTED_NUTRITION_ORIGIN}' "
            "(checked expl_ns='client' expl_id and bounded server-ID scan "
            "of record_json metadata; scan limited to 200 rows)"
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


# ── Day report: pending / done / excluded with reasons ────────────────────────

# Canonical user-facing status texts. MUST stay identical to the agent wrapper
# (agent-workflow/nutrition_writeback.py _STATUS_TEXT etc.); a test checks this.
_STATUS_TEXT = {
    "draft": "Черновик — ждёт подтверждения",
    "confirmed": "Принято — ожидает синхронизации",
    "acked": "Телефон подтвердил запись в Health Connect",
}
_NOT_WRITTEN_TEXT = "Не записано: {reason}"
_UNKNOWN_STATUS_TEXT = "Статус неизвестен — не считать записанным"
_CANCELLED_REASON = "отменено"
_CANCELLED_DELIVERY_POSSIBLE_TEXT = ("Отменено, но телефон уже мог получить запись — "
                                     "проверьте Health Connect на телефоне")
_QUEUE_REPORT_SCAN_LIMIT = 1000


def _user_status_text(state: Any, delivery_possible: Any) -> dict:
    """Canonical status; unknown state fails closed (no delivery claim)."""
    dp = not (delivery_possible is False
              or (type(delivery_possible) is int and delivery_possible == 0))
    if isinstance(state, str) and state in _STATUS_TEXT:
        return {"code": state, "text": _STATUS_TEXT[state], "known": True}
    if state == "cancelled":
        if dp:
            return {"code": "cancelled_delivery_possible",
                    "text": _CANCELLED_DELIVERY_POSSIBLE_TEXT, "known": True}
        return {"code": "cancelled", "known": True,
                "text": _NOT_WRITTEN_TEXT.format(reason=_CANCELLED_REASON)}
    return {"code": "unknown", "text": _UNKNOWN_STATUS_TEXT, "known": False}


def _queue_day_report(conn: sqlite3.Connection, date_str: str) -> dict:
    """
    Bounded, read-only day report of nutrition_queue rows (Europe/Berlin day).

    pending  = draft (unapproved) + confirmed (waiting for phone sync/ACK)
    done     = acked (phone-reported)
    excluded = cancelled, unknown state, or unusable/naive start_time (day unknown),
               each with a reason.
    Only timezone-aware start_time values are placed on a day (naive values are
    excluded, never interpreted in the host timezone). No totals are produced:
    drafts are never intake, and queue rows are never summed with ingest.
    """
    start_s, end_s = _day_utc_epoch_bounds(date_str)
    rows = conn.execute(
        "SELECT * FROM nutrition_queue ORDER BY id DESC LIMIT ?",
        (_QUEUE_REPORT_SCAN_LIMIT + 1,),
    ).fetchall()
    truncated = len(rows) > _QUEUE_REPORT_SCAN_LIMIT
    rows = rows[:_QUEUE_REPORT_SCAN_LIMIT]
    pending: list = []
    done: list = []
    excluded: list = []
    for row in rows:
        status = _user_status_text(row["state"], row["delivery_possible"])
        base = {
            "client_record_id": row["client_record_id"],
            "client_record_version": row["client_record_version"],
            "state": row["state"],
            "name": row["name"],
            "start_time": row["start_time"],
            "user_status": status,
        }
        raw_start = row["start_time"]
        epoch, err = (_iso_to_epoch_strict(raw_start) if isinstance(raw_start, str)
                      else (None, "start_time is not text"))
        if err:
            excluded.append(dict(base, day_known=False,
                                 reason=f"unusable start_time: {err}"))
            continue
        if not (start_s <= epoch < end_s):
            continue
        item = dict(base, **{f: row[f] for f in
                             ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")})
        state = row["state"]
        if state == "draft":
            pending.append(dict(item, reason="draft: unapproved, not in any intake"))
        elif state == "confirmed":
            pending.append(dict(item, reason="confirmed: waiting for phone sync/ACK"))
        elif state == "acked":
            done.append(item)
        elif state == "cancelled":
            excluded.append(dict(item, day_known=True, reason="cancelled" + (
                "; phone may have fetched it before cancellation"
                if status["code"] == "cancelled_delivery_possible" else "")))
        else:
            excluded.append(dict(item, day_known=True, reason="unknown state"))
    return {
        "date": date_str,
        "timezone": _BERLIN_TZ_NAME,
        "scan_limit": _QUEUE_REPORT_SCAN_LIMIT,
        "scanned": len(rows),
        "possibly_incomplete": truncated,
        "counts": {"pending": len(pending), "done": len(done), "excluded": len(excluded)},
        "pending": pending,
        "done": done,
        "excluded": excluded,
        "totals": None,
        "note": (
            "Queue records only; never sum with date_nutrition_ingest. Drafts and "
            "other non-acked records are not part of any daily intake."
        ),
    }


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
    queue_day_report: Optional[dict] = None

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
            queue_day_report = _queue_day_report(nutrition_conn, date_str)
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
        "queue_day_report": queue_day_report,
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
