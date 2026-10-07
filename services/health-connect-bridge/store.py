"""
SQLite persistence for Health Connect webhook bridge.

Schema design:
  - identity_key TEXT UNIQUE: collision-free dedup key (JSON list encoding).
  - record_start_s / record_end_s REAL: epoch seconds for range queries.
    This avoids broken lexical ISO-string comparisons in SQL.
  - client_record_version / last_modified_s: stale-replay rejection for
    future JSON payloads that include version metadata.
  - journal_mode = DELETE: stable for mode=ro read-only queries (no WAL sidecars).
  - PRAGMA query_only = ON: enforced on every read-only connection.
  - DB 0600, data directory 0700: created atomically before first sqlite3.connect.
"""

import json
import logging
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .identity import (
    build_source_key,
    build_temporal_key,
    build_identity_key,
    extract_explicit_id,
    extract_version_info,
    extract_record_times,
    parse_epoch,
    SERIES_TYPES,
    normalise_ts_to_epoch_us,
)
from .validation import KNOWN_RECORD_TYPES

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2

_SCHEMA = f"""
PRAGMA journal_mode = DELETE;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   TEXT    NOT NULL,
    payload_ts    TEXT    NOT NULL,
    app_version   TEXT,
    record_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_records (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key         TEXT    NOT NULL UNIQUE,
    record_type          TEXT    NOT NULL,
    source_key           TEXT    NOT NULL,
    expl_ns              TEXT,
    expl_id              TEXT,
    sample_disc          TEXT,
    temporal_key         TEXT,
    record_start_s       REAL    NOT NULL,
    record_end_s         REAL    NOT NULL,
    client_record_version INTEGER,
    last_modified_s      REAL,
    data_origin          TEXT,
    recording_method     TEXT,
    device_json          TEXT,
    record_json          TEXT    NOT NULL,
    received_at          TEXT    NOT NULL,
    payload_ts           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hr_identity
    ON health_records (identity_key);
CREATE INDEX IF NOT EXISTS idx_hr_type_src_start
    ON health_records (record_type, source_key, record_start_s);
CREATE INDEX IF NOT EXISTS idx_hr_type_src_end
    ON health_records (record_type, source_key, record_end_s);
CREATE INDEX IF NOT EXISTS idx_hr_received
    ON health_records (received_at);
CREATE INDEX IF NOT EXISTS idx_hr_expl
    ON health_records (record_type, expl_ns, expl_id)
    WHERE expl_ns IS NOT NULL;
"""


# ── DB file creation with strict permissions ───────────────────────────────────

def _atomic_create_db(db_path: Path) -> None:
    """
    Create the DB file atomically with mode 0600 before sqlite3 opens it.
    Refuses to proceed if path is a symlink.
    Raises if the directory cannot be made private.
    """
    if db_path.is_symlink():
        raise RuntimeError(f"DB path must not be a symlink: {db_path}")
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Enforce parent directory permissions
    os.chmod(db_path.parent, 0o700)
    if not db_path.exists():
        fd = os.open(str(db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    else:
        # Enforce permissions on existing file
        if db_path.is_symlink():
            raise RuntimeError(f"DB path must not be a symlink: {db_path}")
        os.chmod(db_path, 0o600)


def open_rw(db_path: Path) -> sqlite3.Connection:
    _atomic_create_db(db_path)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def open_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if db_path.is_symlink():
        raise RuntimeError(f"DB path must not be a symlink: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Upsert schema version
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
    conn.commit()


# ── Write store ────────────────────────────────────────────────────────────────

class HealthStore:
    """Thread-safe write store for the HTTP receiver."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = open_rw(db_path)
        init_schema(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ingest_payload(self, payload: dict, received_at: str) -> dict:
        """
        Ingest a pre-validated payload dict.  Returns a summary dict of
        record-type → inserted/updated count (no health data in return value).

        Identity logic:
          - Explicit ID present (future JSON): use (record_type, ns, id) or
            (record_type, ns, id, sample_disc) for series types.
            Source_key is stored for provenance but is NOT part of identity.
            Stale replay is rejected if client_record_version/last_modified_s
            is present and older than the stored version.
          - No explicit ID (current JSON): use
            (record_type, source_key, temporal_key) as fallback.

        Within one transaction, records with the same identity_key are processed
        sequentially; the last version wins (idempotent for same data, update for
        value corrections).
        """
        payload_ts = payload.get("timestamp", received_at)
        app_version = payload.get("app_version")

        summary: dict[str, int] = {}
        rows: list[dict] = []

        for record_type, records in payload.items():
            if record_type in ("timestamp", "app_version"):
                continue
            if not isinstance(records, list):
                continue
            if record_type not in KNOWN_RECORD_TYPES:
                # Unknown type: skip silently; NEVER log the key name
                continue

            count = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue

                meta = rec.get("metadata")
                source_key = build_source_key(meta)
                temporal_key = build_temporal_key(record_type, rec)
                expl = extract_explicit_id(meta)
                expl_ns, expl_id = (expl if expl else (None, None))
                client_ver, last_mod_s = extract_version_info(meta)

                # Extract origin before building identity key (required for origin-namespaced keys)
                data_origin = meta.get("data_origin") if meta else None
                rec_method = meta.get("recording_method") if meta else None
                raw_device = meta.get("device") if meta else None
                device_json = json.dumps(raw_device) if isinstance(raw_device, dict) else None

                # For series types with parent ID: sample discriminator is normalised time.
                # heart_rate includes shape prefix (s/b) so a raw sample and an aggregate
                # bucket at the same explicit_id+origin+time remain two distinct records.
                sample_disc: Optional[str] = None
                if expl and record_type in SERIES_TYPES:
                    t = rec.get("time")
                    if t:
                        try:
                            disc_time = normalise_ts_to_epoch_us(str(t))
                            if record_type == "heart_rate":
                                shape = "b" if "avg" in rec else "s"
                                sample_disc = f"{shape}:{disc_time}"
                            else:
                                sample_disc = disc_time
                        except (ValueError, TypeError):
                            pass

                identity_key = build_identity_key(
                    record_type, source_key, temporal_key, expl, sample_disc,
                    origin=data_origin,
                )
                if identity_key is None:
                    # No temporal key and no explicit ID: skip
                    continue

                times = extract_record_times(record_type, rec)
                if times is None:
                    # Cannot extract times; skip
                    continue
                start_s, end_s = times

                rows.append({
                    "identity_key": identity_key,
                    "record_type": record_type,
                    "source_key": source_key,
                    "expl_ns": expl_ns,
                    "expl_id": expl_id,
                    "sample_disc": sample_disc,
                    "temporal_key": temporal_key,
                    "record_start_s": start_s,
                    "record_end_s": end_s,
                    "client_record_version": client_ver,
                    "last_modified_s": last_mod_s,
                    "data_origin": data_origin,
                    "recording_method": str(rec_method) if rec_method is not None else None,
                    "device_json": device_json,
                    "record_json": json.dumps(rec),
                    "received_at": received_at,
                    "payload_ts": payload_ts,
                })
                count += 1

            if count:
                summary[record_type] = count

        total = sum(summary.values())

        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO ingest_log "
                    "(received_at, payload_ts, app_version, record_count) VALUES (?,?,?,?)",
                    (received_at, payload_ts, app_version, total),
                )

                for row in rows:
                    # Stale replay check: if existing record has a newer version, skip
                    existing = cur.execute(
                        "SELECT client_record_version, last_modified_s "
                        "FROM health_records WHERE identity_key = ?",
                        (row["identity_key"],),
                    ).fetchone()

                    if existing:
                        ev = existing["client_record_version"]
                        iv = row["client_record_version"]
                        # Versionless update to a versioned record: reject to preserve watermark
                        if ev is not None and iv is None:
                            continue
                        if ev is not None and iv is not None and iv < ev:
                            continue  # stale replay
                        el = existing["last_modified_s"]
                        il = row["last_modified_s"]
                        # lmt-less update to an lmt-tracked record: reject
                        if el is not None and il is None:
                            continue
                        if el is not None and il is not None and il < el:
                            continue  # stale replay

                    cur.execute(
                        """INSERT OR REPLACE INTO health_records
                           (identity_key, record_type, source_key,
                            expl_ns, expl_id, sample_disc, temporal_key,
                            record_start_s, record_end_s,
                            client_record_version, last_modified_s,
                            data_origin, recording_method, device_json,
                            record_json, received_at, payload_ts)
                           VALUES
                           (:identity_key, :record_type, :source_key,
                            :expl_ns, :expl_id, :sample_disc, :temporal_key,
                            :record_start_s, :record_end_s,
                            :client_record_version, :last_modified_s,
                            :data_origin, :recording_method, :device_json,
                            :record_json, :received_at, :payload_ts)""",
                        row,
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return summary

    def record_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as n FROM health_records"
            ).fetchone()
        return row["n"] if row else 0


# ── Read-only query helpers ────────────────────────────────────────────────────

def _day_epoch_bounds(date_str: str, tz_name: str) -> tuple[float, float]:
    """
    Return (day_start_epoch_s, day_end_epoch_s) as a half-open interval
    [midnight, next_midnight) in the given timezone.

    Handles DST transitions correctly: uses calendar arithmetic (next day
    at midnight) rather than adding 86400 seconds.

    Raises ValueError for invalid date_str.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)

    # Strict YYYY-MM-DD validation
    parts = date_str.strip().split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date_str!r}")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])

    day_start = datetime(year, month, day, 0, 0, 0, tzinfo=tz)
    # Next day midnight — calendar arithmetic handles 23h/25h DST days
    import calendar
    if month == 12 and day == 31:
        next_year, next_month, next_day = year + 1, 1, 1
    elif day == calendar.monthrange(year, month)[1]:
        next_year, next_month, next_day = year, month + 1, 1
    else:
        next_year, next_month, next_day = year, month, day + 1
    day_end = datetime(next_year, next_month, next_day, 0, 0, 0, tzinfo=tz)

    return day_start.timestamp(), day_end.timestamp()


def query_status(conn: sqlite3.Connection) -> dict:
    """
    Return sync status with per-source freshness.

    Distinguishes:
      not_synced  — no ingest events at all
      empty       — ingest events exist but no health records
      ok          — records exist, latest record < 48h ago
      stale       — records exist but latest record >= 48h ago

    latest_record_time is the actual maximum record_end_s across all records,
    not the payload timestamp.  This accurately reflects how fresh the health
    data is, separate from when we received the HTTP POST.
    """
    log_row = conn.execute(
        "SELECT received_at, payload_ts, app_version, record_count "
        "FROM ingest_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM health_records"
    ).fetchone()["n"]

    if not log_row:
        return {"state": "not_synced", "total_records": 0}

    now_utc = datetime.now(timezone.utc)
    transport_age_h = (
        now_utc - datetime.fromisoformat(
            log_row["received_at"].replace("Z", "+00:00")
        )
    ).total_seconds() / 3600

    if total == 0:
        return {
            "state": "empty",
            "transport": {
                "last_received_at": log_row["received_at"],
                "transport_age_hours": round(transport_age_h, 1),
            },
            "total_records": 0,
        }

    # Per-source latest record time
    src_rows = conn.execute(
        """SELECT source_key,
                  MAX(record_end_s) AS latest_s,
                  COUNT(*) AS cnt
           FROM health_records
           GROUP BY source_key"""
    ).fetchall()

    per_source: dict[str, Any] = {}
    latest_overall_s: float = 0.0
    for row in src_rows:
        ls = row["latest_s"]
        if ls is not None:
            latest_overall_s = max(latest_overall_s, ls)
            age_h = (now_utc.timestamp() - ls) / 3600
            latest_utc = datetime.fromtimestamp(ls, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            per_source[row["source_key"]] = {
                "latest_record_utc": latest_utc,
                "record_age_hours": round(age_h, 1),
                "record_count": row["cnt"],
            }

    data_age_h = (now_utc.timestamp() - latest_overall_s) / 3600 if latest_overall_s else None
    state = "stale" if (data_age_h is None or data_age_h > 48) else "ok"

    return {
        "state": state,
        "transport": {
            "last_received_at": log_row["received_at"],
            "transport_age_hours": round(transport_age_h, 1),
        },
        "data": {
            "total_records": total,
            "latest_record_age_hours": round(data_age_h, 1) if data_age_h is not None else None,
            "per_source": per_source,
        },
    }


def _source_key_provenance_flags(source_key: str) -> dict:
    """
    Derive provenance completeness flags from a stored source_key string.
    Mirrors identity.source_key_flags() but operates on the serialised key.
    Returns non-empty dict when origin or device provenance is absent/ambiguous.
    """
    try:
        parsed = json.loads(source_key)
    except (json.JSONDecodeError, TypeError):
        return {"no_metadata": True, "unknown_origin": True, "unknown_device": True}
    if not isinstance(parsed, dict):
        return {"no_metadata": True, "unknown_origin": True, "unknown_device": True}
    if parsed.get("_no_metadata"):
        return {"no_metadata": True, "unknown_origin": True, "unknown_device": True}
    flags: dict = {}
    if not parsed.get("origin"):
        flags["unknown_origin"] = True
    dev = parsed.get("device")
    if not isinstance(dev, dict) or (
        not dev.get("manufacturer") and not dev.get("model") and dev.get("type") is None
    ):
        flags["unknown_device"] = True
    return flags


def _detect_step_overlaps(
    intervals: list[tuple[float, float, int]],  # (start_s, end_s, count)
    day_start_s: float,
    day_end_s: float,
    provenance_flags: Optional[dict] = None,
) -> dict:
    """
    Analyse step intervals for a single source on a single day.

    Fetched intervals include those that merely intersect the day, so some may
    start before day_start or end after day_end (cross-boundary).

    Overlap detection is performed across ALL intersecting intervals, not just
    wholly-contained ones — a prior-day interval can overlap with a within-day
    interval and must not be silently excluded from the overlap check.

    Touching disjoint boundaries (end_a == start_b) are NOT overlaps.

    Returns:
      - total: authoritative count sum, or null if any overlap, cross-boundary,
        or provenance ambiguity (unknown origin/device)
      - ambiguity_warning: present when total is null
      - cross_boundary: intervals not wholly within the day
      - raw_intervals: number of intervals examined
      - contained_disjoint_subtotal: sum of wholly-contained disjoint intervals
        when cross-boundary is the sole reason for null (NOT a full-day total)
      - raw_unallocated_subtotal: arithmetic sum when provenance is ambiguous and
        intervals are otherwise disjoint+contained (NOT authoritative)
    """
    cross_boundary = []
    contained = []

    for start_s, end_s, count in intervals:
        if start_s < day_start_s or end_s > day_end_s:
            cross_boundary.append({
                "count": count,
                "note": "interval crosses day boundary; count not allocated",
            })
        else:
            contained.append((start_s, end_s, count))

    # Detect overlaps across ALL intersecting intervals (not just contained).
    all_sorted = sorted(intervals, key=lambda x: x[0])
    has_overlap = False
    for i in range(len(all_sorted) - 1):
        # Touching (end_i == start_{i+1}) is not overlap
        if all_sorted[i][1] > all_sorted[i + 1][0]:
            has_overlap = True
            break

    result: dict[str, Any] = {
        "raw_intervals": len(intervals),
        "cross_boundary": cross_boundary,
    }

    if has_overlap:
        result["total"] = None
        result["ambiguity_warning"] = (
            "Overlapping step intervals detected for this source. "
            "Summing would double-count; total set to null."
        )
    elif cross_boundary:
        result["total"] = None
        result["ambiguity_warning"] = (
            "Cross-boundary intervals present; full-day total uncomputable."
        )
        contained_sum = sum(c for _, _, c in contained)
        result["contained_disjoint_subtotal"] = contained_sum
        result["contained_disjoint_note"] = (
            "Sum of wholly-contained disjoint intervals only — NOT a full-day total."
        )
    elif provenance_flags:
        # Intervals are disjoint and wholly contained, but source provenance is
        # ambiguous. Multiple unknown devices may share this source key;
        # reporting an authoritative total would misattribute combined counts.
        disjoint_sum = sum(c for _, _, c in contained)
        result["total"] = None
        result["ambiguity_warning"] = (
            "Source provenance is ambiguous (unknown origin or device). "
            "Multiple indistinguishable devices may share this source key; "
            "total set to null to avoid misattribution."
        )
        result["raw_unallocated_subtotal"] = disjoint_sum
        result["raw_unallocated_note"] = (
            "Arithmetic sum of intervals for this source key — NOT authoritative; "
            "may aggregate data from multiple indistinguishable devices."
        )
    else:
        result["total"] = sum(c for _, _, c in contained)

    return result


def query_day(
    conn: sqlite3.Connection, date_str: str, tz_name: str = "Europe/Berlin"
) -> dict:
    """
    Return a day summary for the given date (YYYY-MM-DD) in the given timezone.

    Uses epoch second bounds [day_start_s, day_end_s) — half-open interval —
    so DST transitions (23h/25h days) are handled correctly.  No lexical
    ISO-string comparison in SQL.

    Per-source only.  No cross-source totals.
    Steps: overlap detection — total is null if overlapping intervals found.
    """
    day_start_s, day_end_s = _day_epoch_bounds(date_str, tz_name)
    result: dict[str, Any] = {"date": date_str, "timezone": tz_name, "sources": {}}

    # Steps — include all intervals that INTERSECT the day (not just those starting in it).
    # A record at 23:00-01:00 local time must appear on both boundary days for overlap
    # detection; cross-boundary intervals are flagged and excluded from clean totals.
    rows = conn.execute(
        """SELECT source_key, record_json, record_start_s, record_end_s
           FROM health_records
           WHERE record_type = 'steps'
             AND record_start_s < ? AND record_end_s > ?""",
        (day_end_s, day_start_s),
    ).fetchall()
    steps_by_src: dict[str, list] = {}
    for row in rows:
        rec = json.loads(row["record_json"])
        steps_by_src.setdefault(row["source_key"], []).append(
            (row["record_start_s"], row["record_end_s"], int(rec.get("count", 0)))
        )
    for src, intervals in steps_by_src.items():
        prov_flags = _source_key_provenance_flags(src)
        src_entry = result["sources"].setdefault(src, {})
        if prov_flags:
            src_entry["provenance_flags"] = prov_flags
        src_entry["steps"] = _detect_step_overlaps(
            intervals, day_start_s, day_end_s, prov_flags or None
        )

    # Heart rate — instant records (start_s == end_s)
    rows = conn.execute(
        """SELECT source_key, record_json
           FROM health_records
           WHERE record_type = 'heart_rate'
             AND record_start_s >= ? AND record_start_s < ?""",
        (day_start_s, day_end_s),
    ).fetchall()
    hr_by_src: dict[str, list] = {}
    for row in rows:
        hr_by_src.setdefault(row["source_key"], []).append(
            json.loads(row["record_json"])
        )
    for src, recs in hr_by_src.items():
        # Separate raw samples (bpm only) from aggregate buckets (has "avg").
        # Mixed payloads must not drop raw samples or average bucket means as true samples.
        samples = [r for r in recs if "avg" not in r and "bpm" in r]
        buckets = [r for r in recs if "avg" in r]
        hr_data: dict[str, Any] = {}
        if samples:
            bpms = [float(r["bpm"]) for r in samples if math.isfinite(float(r["bpm"]))]
            hr_data["sample_summary"] = {
                "shape": "samples",
                "samples": len(samples),
                "avg_bpm": round(sum(bpms) / len(bpms), 1) if bpms else None,
                "min_bpm": min(bpms) if bpms else None,
                "max_bpm": max(bpms) if bpms else None,
            }
        if buckets:
            avgs = [float(r["avg"]) for r in buckets if math.isfinite(float(r["avg"]))]
            mins = [float(r["min"]) for r in buckets if "min" in r and math.isfinite(float(r["min"]))]
            maxs = [float(r["max"]) for r in buckets if "max" in r and math.isfinite(float(r["max"]))]
            hr_data["bucket_summary"] = {
                "shape": "aggregate",
                "buckets": len(buckets),
                "avg_of_bucket_avgs": round(sum(avgs) / len(avgs), 1) if avgs else None,
                "min_bucket_min": min(mins) if mins else None,
                "max_bucket_max": max(maxs) if maxs else None,
                "note": "unweighted bucket mean — avg_of_bucket_avgs is not sample-weighted",
            }
        if hr_data:
            result["sources"].setdefault(src, {})["heart_rate"] = hr_data

    # Weight — instant records
    rows = conn.execute(
        """SELECT source_key, record_json
           FROM health_records
           WHERE record_type = 'weight'
             AND record_start_s >= ? AND record_start_s < ?""",
        (day_start_s, day_end_s),
    ).fetchall()
    for row in rows:
        rec = json.loads(row["record_json"])
        result["sources"].setdefault(row["source_key"], {}).setdefault(
            "weight_kg", []
        ).append({"kg": rec.get("kilograms"), "time": rec.get("time")})

    return result


def query_sleep(
    conn: sqlite3.Connection, date_str: str, tz_name: str = "Europe/Berlin"
) -> dict:
    """
    Return sleep sessions whose END time (record_end_s) falls on the given
    date in tz.  Uses epoch bounds [day_start_s, day_end_s) — half-open.
    """
    day_start_s, day_end_s = _day_epoch_bounds(date_str, tz_name)

    rows = conn.execute(
        """SELECT source_key, record_json, record_end_s
           FROM health_records
           WHERE record_type = 'sleep'
             AND record_end_s >= ? AND record_end_s < ?
           ORDER BY record_end_s""",
        (day_start_s, day_end_s),
    ).fetchall()

    sessions: list[dict] = []
    for row in rows:
        rec = json.loads(row["record_json"])
        dur_s = rec.get("duration_seconds", 0)
        stages = rec.get("stages") or []  # normalize explicit null to []
        sessions.append({
            "source_key": row["source_key"],
            "session_end_time": rec.get("session_end_time"),
            "duration_hours": round(dur_s / 3600, 2),
            "stage_count": len(stages),
            "stages": [
                {
                    "stage": s.get("stage"),
                    "duration_minutes": round(s.get("duration_seconds", 0) / 60, 1),
                }
                for s in stages
            ],
        })

    return {"date": date_str, "timezone": tz_name, "sessions": sessions}


def query_workouts(
    conn: sqlite3.Connection, date_str: str, tz_name: str = "Europe/Berlin"
) -> dict:
    """
    Return exercise sessions whose START time (record_start_s) falls on the
    given date in tz.  Uses epoch bounds [day_start_s, day_end_s) — half-open.
    """
    day_start_s, day_end_s = _day_epoch_bounds(date_str, tz_name)

    rows = conn.execute(
        """SELECT source_key, record_json, record_start_s
           FROM health_records
           WHERE record_type = 'exercise'
             AND record_start_s >= ? AND record_start_s < ?
           ORDER BY record_start_s""",
        (day_start_s, day_end_s),
    ).fetchall()

    workouts: list[dict] = []
    for row in rows:
        rec = json.loads(row["record_json"])
        dur_s = rec.get("duration_seconds", 0)
        w: dict = {
            "source_key": row["source_key"],
            "type": rec.get("type"),
            "start_time": rec.get("start_time"),
            "end_time": rec.get("end_time"),
            "duration_minutes": round(dur_s / 60, 1),
        }
        if "distance_meters" in rec:
            w["distance_km"] = round(rec["distance_meters"] / 1000, 3)
        if "steps" in rec:
            w["steps"] = rec["steps"]
        workouts.append(w)

    return {"date": date_str, "timezone": tz_name, "workouts": workouts}
