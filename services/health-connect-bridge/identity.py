"""
Record identity, source-key, and temporal-key logic.

JSON metadata limitation (as of SyncManager.kt putRecordMetadata):
  The upstream app serializes ONLY data_origin, recording_method, and
  device{manufacturer, model, type}.  Record IDs (id, client_record_id,
  client_record_version, last_modified_time) appear in the Protobuf schema
  but are NOT present in JSON payloads today.

  If a future JSON payload includes metadata.id / metadata.client_record_id,
  this module extracts them and uses them as the primary identity.  When absent
  (current behaviour), a temporal key derived from record timestamps is used.

Source key encoding:
  The source key is a JSON-encoded dict of ALL metadata fields including
  recording_method and device.type.  This is collision-free: no field value
  can create ambiguity regardless of embedded punctuation, because JSON
  serialisation escapes all such characters.

  Limitation: Health Connect stores no hardware serial number in JSON metadata,
  so two devices with identical make/model/app/method cannot be distinguished.
  When source_key contains any null or 'unknown' markers, that ambiguity is
  explicitly flagged.

Temporal key normalisation:
  Timestamps are normalised to UTC epoch-microseconds (integer string) so that
  "2026-01-15T10:00:00Z" and "2026-01-15T10:00:00+00:00" produce the same key.

Identity key encoding (for the database UNIQUE constraint):
  JSON list encoding is used so that no component value (regardless of content)
  can create collisions across columns:

  Temporal fallback:  ["t", record_type, source_key, temporal_key_norm]
  Explicit ordinary:  ["e", record_type, ns, id]           ns = "server" or "client"
  Explicit series:    ["es", record_type, ns, id, disc]    disc = epoch-µs of sample

  "Series" types (heart_rate, heart_rate_variability, oxygen_saturation,
  respiratory_rate, skin_temperature) may carry a parent explicit ID with
  multiple sample-level rows; earlier samples are never deleted when new
  ones arrive.

Known deduplication/update limitations:
  - Deletions: deleted on-device records persist in the journal; incremental
    sync never sends tombstones.
  - Resegmentation: if an interval is split on the device side, both old and
    new temporal keys coexist.
  - Time edit on a record without explicit ID: old temporal key persists
    alongside new one.
  - Indistinguishable devices: two phones with identical metadata share a
    source key; temporal-fallback records from either cannot be attributed.
"""

import json
import math
from datetime import datetime, timezone
from typing import Optional


# ── Time utilities ────────────────────────────────────────────────────────────

def parse_aware_dt(ts: str) -> datetime:
    """
    Parse an ISO-8601 timestamp string and return an aware UTC datetime.
    Raises ValueError for naive datetimes, NaN/Inf, or unparseable strings.
    """
    s = ts.strip()
    # Normalise Z suffix
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"Naive datetime not allowed: {ts!r}")
    return dt.astimezone(timezone.utc)


def parse_epoch(ts: str) -> float:
    """Return epoch seconds (float) for an ISO-8601 aware timestamp."""
    return parse_aware_dt(ts).timestamp()


def normalise_ts_to_epoch_us(ts: str) -> str:
    """
    Normalise an ISO-8601 aware timestamp to an integer epoch-nanoseconds string.
    Uses integer arithmetic to avoid float precision loss — Kotlin Instant.toString()
    can emit 9 fractional digits; Python datetime only supports 6.
    Z and +00:00 variants produce identical keys.
    Distinct nanosecond values remain distinct.
    """
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    # Separate fractional seconds from the rest before calling fromisoformat.
    dot_idx = s.find(".")
    if dot_idx != -1:
        # Timezone offset starts at the first '+'/'-' after the fractional part.
        tz_start = len(s)
        for i in range(dot_idx + 1, len(s)):
            if s[i] in ("+", "-"):
                tz_start = i
                break
        frac_str = s[dot_idx + 1 : tz_start]
        # Pad/truncate to exactly 9 digits for nanosecond resolution.
        frac_ns = int(frac_str.ljust(9, "0")[:9])
        s_whole = s[:dot_idx] + s[tz_start:]
    else:
        frac_ns = 0
        s_whole = s

    dt = datetime.fromisoformat(s_whole)
    if dt.tzinfo is None:
        raise ValueError(f"Naive datetime not allowed: {ts!r}")
    dt_utc = dt.astimezone(timezone.utc)

    # Integer arithmetic only — no float epoch multiplication.
    delta = dt_utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    epoch_s = delta.days * 86400 + delta.seconds

    return str(epoch_s * 1_000_000_000 + frac_ns)


# ── Source key ────────────────────────────────────────────────────────────────

def build_source_key(meta: Optional[dict]) -> str:
    """
    Build a collision-free canonical source key from record metadata.

    The key is a JSON-serialised dict of ALL provenance fields, including
    recording_method and device.type.  Missing components are represented as
    JSON null, making absent-vs-present distinctions explicit.

    JSON encoding is used (not a string with delimiters) so that no field value
    can create a collision regardless of embedded characters.
    """
    if not meta or not isinstance(meta, dict):
        return json.dumps({"_no_metadata": True}, separators=(",", ":"))

    raw_device = meta.get("device")
    if isinstance(raw_device, dict):
        device_data: Optional[dict] = {
            "manufacturer": raw_device.get("manufacturer"),
            "model": raw_device.get("model"),
            "type": raw_device.get("type"),
        }
    else:
        device_data = None

    key_obj = {
        "origin": meta.get("data_origin"),
        "method": meta.get("recording_method"),
        "device": device_data,
    }
    return json.dumps(key_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def source_key_flags(meta: Optional[dict]) -> dict:
    """Return diagnostic flags about metadata completeness."""
    if not meta or not isinstance(meta, dict):
        return {"no_metadata": True, "unknown_origin": True, "unknown_device": True}
    flags: dict = {}
    if not meta.get("data_origin"):
        flags["unknown_origin"] = True
    dev = meta.get("device")
    if not isinstance(dev, dict) or (
        not dev.get("manufacturer") and not dev.get("model") and dev.get("type") is None
    ):
        flags["unknown_device"] = True
    return flags


# ── Explicit ID extraction ─────────────────────────────────────────────────────

def extract_explicit_id(meta: Optional[dict]) -> Optional[tuple[str, str]]:
    """
    Return (namespace, id_value) if an explicit record ID is present in metadata,
    otherwise None.

    namespace is "server" for metadata.id, "client" for metadata.client_record_id.
    These fields are NOT present in current JSON payloads; this function always
    returns None today.  It is ready for a future JSON format that adds them.
    """
    if not meta or not isinstance(meta, dict):
        return None
    server_id = meta.get("id")
    if server_id is not None:
        return ("server", str(server_id))
    client_id = meta.get("client_record_id")
    if client_id is not None:
        return ("client", str(client_id))
    return None


def extract_version_info(meta: Optional[dict]) -> tuple[Optional[int], Optional[float]]:
    """
    Return (client_record_version, last_modified_epoch_s) from metadata.
    Either or both may be None if not present (current JSON has neither).
    """
    if not meta or not isinstance(meta, dict):
        return None, None
    ver = meta.get("client_record_version")
    if ver is not None and not isinstance(ver, (int,)):
        ver = None
    lmt = meta.get("last_modified_time")
    lmt_s: Optional[float] = None
    if isinstance(lmt, str):
        try:
            lmt_s = parse_epoch(lmt)
        except ValueError:
            pass
    return ver, lmt_s


# ── Series types (multi-sample, may share parent explicit ID) ─────────────────

SERIES_TYPES = frozenset([
    "heart_rate",
    "heart_rate_variability",
    "oxygen_saturation",
    "respiratory_rate",
    "skin_temperature",
])


# ── Temporal keys ─────────────────────────────────────────────────────────────

INSTANT_TYPES = frozenset([
    "heart_rate", "heart_rate_variability", "oxygen_saturation",
    "respiratory_rate", "skin_temperature", "resting_heart_rate",
    "weight", "height", "blood_pressure", "blood_glucose",
    "body_temperature", "basal_metabolic_rate", "body_fat",
    "lean_body_mass", "body_water_mass", "vo2_max", "bone_mass",
    "menstruation_flow", "intermenstrual_bleeding", "ovulation_test",
    "cervical_mucus", "sexual_activity", "basal_body_temperature",
    "bmi",
])

INTERVAL_TYPES = frozenset([
    "steps", "distance", "active_calories", "total_calories",
    "hydration", "nutrition", "exercise", "menstruation_period",
])


def build_temporal_key(record_type: str, record: dict) -> Optional[str]:
    """
    Derive a normalised temporal identity key for a record.

    Returns None if required time fields are missing or unparseable.
    Timestamps are normalised to epoch-microseconds so that equivalent
    Z and +00:00 representations produce the same key.
    """
    try:
        if record_type in INSTANT_TYPES:
            t = record.get("time")
            if not t:
                return None
            epoch_key = normalise_ts_to_epoch_us(str(t))
            # Discriminate raw samples from aggregate buckets at the same timestamp
            # so a 150bpm sample and a 70-avg bucket at T are stored as separate records.
            if record_type == "heart_rate":
                shape = "b" if "avg" in record else "s"
                return f"i:{shape}:{epoch_key}"
            return f"i:{epoch_key}"

        if record_type == "sleep":
            t = record.get("session_end_time")
            if not t:
                return None
            return f"i:{normalise_ts_to_epoch_us(str(t))}"

        if record_type in INTERVAL_TYPES:
            s = record.get("start_time")
            e = record.get("end_time")
            if not s or not e:
                return None
            return f"iv:{normalise_ts_to_epoch_us(str(s))}:{normalise_ts_to_epoch_us(str(e))}"

        # Unknown type: use a deterministic but non-sensitive hash
        import hashlib
        data_for_hash = {k: v for k, v in record.items() if k != "metadata"}
        blob = json.dumps(data_for_hash, sort_keys=True).encode()
        h = hashlib.sha256(blob).hexdigest()[:24]
        return f"h:{h}"

    except (ValueError, TypeError):
        return None


def build_identity_key(
    record_type: str,
    source_key: str,
    temporal_key: Optional[str],
    explicit_id: Optional[tuple[str, str]],
    sample_disc: Optional[str],
    origin: Optional[str] = None,
) -> Optional[str]:
    """
    Build the database identity key for deduplication.

    Returns None if no identity can be constructed (missing temporal key and
    no explicit ID, or explicit ID present but no usable origin).

    Encoding uses JSON list serialisation which is collision-free regardless
    of component values.

    Explicit keys include origin so that two apps with the same client_record_id
    but different data_origin values remain distinct records.  Device metadata
    (manufacturer/model) is NOT part of identity — a device metadata update to
    the same app+ID stays as one record.

    When explicit_id is present but origin is None (no usable origin), the
    explicit ID is ignored and the temporal+source fallback is used instead,
    rather than pretending the ID is globally unique.
    """
    if explicit_id is not None and origin is not None:
        ns, id_val = explicit_id
        if record_type in SERIES_TYPES and sample_disc is not None:
            # Series type with parent ID: identity includes origin + sample time
            parts = ["es", record_type, ns, origin, id_val, sample_disc]
        else:
            # Ordinary record with explicit ID: namespaced by origin only
            parts = ["e", record_type, ns, origin, id_val]
        return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)

    # Temporal fallback (also used when explicit_id has no usable origin)
    if temporal_key is None:
        return None
    parts = ["t", record_type, source_key, temporal_key]
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


def extract_record_times(record_type: str, record: dict) -> Optional[tuple[float, float]]:
    """
    Extract (start_epoch_s, end_epoch_s) from a record for indexed storage.
    Returns None if times are missing or unparseable.
    For instant records, start == end.
    """
    try:
        if record_type in INSTANT_TYPES:
            t = record.get("time")
            if not t:
                return None
            e = parse_epoch(str(t))
            return e, e

        if record_type == "sleep":
            end_t = record.get("session_end_time")
            if not end_t:
                return None
            end_e = parse_epoch(str(end_t))
            dur = record.get("duration_seconds", 0)
            start_e = end_e - float(dur)
            return start_e, end_e

        if record_type in INTERVAL_TYPES:
            s = record.get("start_time")
            e = record.get("end_time")
            if not s or not e:
                return None
            return parse_epoch(str(s)), parse_epoch(str(e))

        return None

    except (ValueError, TypeError):
        return None
