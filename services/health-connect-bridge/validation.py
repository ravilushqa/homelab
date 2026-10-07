"""
Strict schema validation for Health Connect JSON payloads.

Validation is ALL-or-nothing for known array types: if any record in a known
array fails validation, the entire payload is rejected (400) and no records
are written.

Unknown array keys are silently ignored — never logged (user-controlled key).

Generic known types (interval or instant shape) are validated for time fields
but not rejected on unknown extra fields, so normal upstream additions do not
break ingest.
"""

import math
from typing import Optional

from .identity import (
    parse_aware_dt,
    INSTANT_TYPES,
    INTERVAL_TYPES,
    SERIES_TYPES,
)

# Types where we apply strict field-level validation
_STRICT_TYPES = frozenset([
    "steps", "sleep", "heart_rate", "weight", "exercise",
])

# All known types (strict + generic)
KNOWN_RECORD_TYPES = frozenset([
    "steps", "sleep", "heart_rate", "heart_rate_variability", "distance",
    "active_calories", "total_calories", "weight", "height", "blood_pressure",
    "blood_glucose", "oxygen_saturation", "body_temperature", "skin_temperature",
    "respiratory_rate", "resting_heart_rate", "exercise", "hydration", "nutrition",
    "basal_metabolic_rate", "body_fat", "lean_body_mass", "body_water_mass",
    "vo2_max", "bone_mass", "menstruation_flow", "menstruation_period",
    "intermenstrual_bleeding", "ovulation_test", "cervical_mucus",
    "sexual_activity", "basal_body_temperature",
    "bmi",
])


def _is_finite_positive(v) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v)) and float(v) > 0


def _is_finite_nonneg(v) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v)) and float(v) >= 0


def _validate_aware_ts(v, field: str) -> list[str]:
    if not isinstance(v, str):
        return [f"{field}: must be a string"]
    try:
        parse_aware_dt(v)
        return []
    except (ValueError, TypeError):
        return [f"{field}: invalid or naive timestamp"]


def _validate_steps(rec: dict) -> list[str]:
    errs = []
    count = rec.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        errs.append("count: must be non-negative integer (not bool)")
    errs += _validate_aware_ts(rec.get("start_time", ""), "start_time")
    errs += _validate_aware_ts(rec.get("end_time", ""), "end_time")
    if not errs:
        try:
            from .identity import parse_epoch
            if parse_epoch(rec["start_time"]) >= parse_epoch(rec["end_time"]):
                errs.append("start_time must be strictly before end_time")
        except Exception:
            pass
    return errs


def _validate_sleep_stage(stage: dict, idx: int) -> list[str]:
    errs = []
    prefix = f"stages[{idx}]"
    if not isinstance(stage.get("stage"), str):
        errs.append(f"{prefix}.stage: must be string")
    errs += _validate_aware_ts(stage.get("start_time", ""), f"{prefix}.start_time")
    errs += _validate_aware_ts(stage.get("end_time", ""), f"{prefix}.end_time")
    if not errs:
        try:
            from .identity import parse_epoch
            if parse_epoch(stage["start_time"]) >= parse_epoch(stage["end_time"]):
                errs.append(f"{prefix}: start_time must be before end_time")
        except Exception:
            pass
    dur = stage.get("duration_seconds")
    if dur is not None and (
        isinstance(dur, bool) or not isinstance(dur, (int, float))
        or not math.isfinite(float(dur)) or dur < 0
    ):
        errs.append(f"{prefix}.duration_seconds: must be finite non-negative")
    return errs


def _validate_sleep(rec: dict) -> list[str]:
    errs = []
    errs += _validate_aware_ts(rec.get("session_end_time", ""), "session_end_time")
    dur = rec.get("duration_seconds")
    if (isinstance(dur, bool) or not isinstance(dur, (int, float))
            or not math.isfinite(float(dur)) or dur < 0):
        errs.append("duration_seconds: must be finite non-negative number")
    stages = rec.get("stages")
    if stages is not None and not isinstance(stages, list):
        # Explicit null (Python None) passes through; non-list non-null is an error.
        # query_sleep normalizes stored null to [] at read time.
        errs.append("stages: must be list if present")
    elif isinstance(stages, list):
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                errs.append(f"stages[{i}]: must be dict")
            else:
                errs += _validate_sleep_stage(stage, i)
    return errs


def _validate_heart_rate(rec: dict) -> list[str]:
    errs = []
    errs += _validate_aware_ts(rec.get("time", ""), "time")
    is_agg = "avg" in rec
    if is_agg:
        for field in ("avg", "min", "max"):
            v = rec.get(field)
            if v is None or not _is_finite_nonneg(v):
                errs.append(f"{field}: must be non-negative finite number")
        if not errs:
            avg = rec.get("avg", 0)
            mn = rec.get("min", 0)
            mx = rec.get("max", 0)
            if mn > avg or avg > mx:
                errs.append("min <= avg <= max required")
    else:
        bpm = rec.get("bpm")
        if bpm is None or not _is_finite_positive(bpm):
            errs.append("bpm: must be positive finite number")
    return errs


def _validate_weight(rec: dict) -> list[str]:
    errs = []
    kg = rec.get("kilograms")
    if not _is_finite_positive(kg):
        errs.append("kilograms: must be positive finite number")
    errs += _validate_aware_ts(rec.get("time", ""), "time")
    return errs


def _validate_exercise(rec: dict) -> list[str]:
    errs = []
    if not isinstance(rec.get("type"), str) or not rec["type"]:
        errs.append("type: must be non-empty string")
    errs += _validate_aware_ts(rec.get("start_time", ""), "start_time")
    errs += _validate_aware_ts(rec.get("end_time", ""), "end_time")
    if not errs:
        try:
            from .identity import parse_epoch
            if parse_epoch(rec["start_time"]) >= parse_epoch(rec["end_time"]):
                errs.append("start_time must be strictly before end_time")
        except Exception:
            pass
    dur = rec.get("duration_seconds")
    if (isinstance(dur, bool) or not isinstance(dur, (int, float))
            or not math.isfinite(float(dur)) or dur < 0):
        errs.append("duration_seconds: must be finite non-negative number")
    steps = rec.get("steps")
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 0):
        errs.append("steps: must be non-negative integer if present")
    dist = rec.get("distance_meters")
    if dist is not None and not _is_finite_nonneg(dist):
        errs.append("distance_meters: must be non-negative finite if present")
    return errs


def _validate_metadata(meta) -> list[str]:
    if meta is None:
        return []
    if not isinstance(meta, dict):
        return ["metadata: must be dict"]
    errs = []
    origin = meta.get("data_origin")
    if origin is not None and not isinstance(origin, str):
        errs.append("metadata.data_origin: must be string if present")
    dev = meta.get("device")
    if dev is not None and not isinstance(dev, dict):
        errs.append("metadata.device: must be dict if present")
    return errs


def _validate_generic_interval(rec: dict) -> list[str]:
    errs = []
    errs += _validate_aware_ts(rec.get("start_time", ""), "start_time")
    errs += _validate_aware_ts(rec.get("end_time", ""), "end_time")
    if not errs:
        try:
            from .identity import parse_epoch
            if parse_epoch(rec["start_time"]) >= parse_epoch(rec["end_time"]):
                errs.append("start_time must be strictly before end_time")
        except Exception:
            pass
    return errs


def _validate_generic_instant(rec: dict) -> list[str]:
    return _validate_aware_ts(rec.get("time", ""), "time")


_STRICT_VALIDATORS = {
    "steps": _validate_steps,
    "sleep": _validate_sleep,
    "heart_rate": _validate_heart_rate,
    "weight": _validate_weight,
    "exercise": _validate_exercise,
}


def _validate_record(record_type: str, rec: dict, index: int) -> list[str]:
    prefix = f"{record_type}[{index}]"
    errs = []

    # Metadata validation (optional field, applicable to all types)
    errs += [f"{prefix}.{e}" for e in _validate_metadata(rec.get("metadata"))]

    # Type-specific validation
    if record_type in _STRICT_VALIDATORS:
        type_errs = _STRICT_VALIDATORS[record_type](rec)
        errs += [f"{prefix}: {e}" for e in type_errs]
    elif record_type in INTERVAL_TYPES:
        type_errs = _validate_generic_interval(rec)
        errs += [f"{prefix}: {e}" for e in type_errs]
    elif record_type in INSTANT_TYPES:
        type_errs = _validate_generic_instant(rec)
        errs += [f"{prefix}: {e}" for e in type_errs]
    # Unknown types: no validation (handled at payload level)

    return errs


def validate_payload(payload: dict) -> list[str]:
    """
    Validate a decoded payload dict.  Returns a list of error messages.
    Empty list means the payload is valid and may be ingested.

    Known arrays with invalid records cause ALL-or-nothing 400.
    Unknown array keys are silently ignored (never logged).
    """
    errs = []

    # Required timestamp
    ts = payload.get("timestamp")
    if ts is None:
        errs.append("missing required field: timestamp")
        return errs
    if not isinstance(ts, str):
        errs.append("timestamp: must be string")
        return errs
    try:
        parse_aware_dt(ts)
    except (ValueError, TypeError):
        errs.append("timestamp: invalid or naive datetime")
        return errs

    # Per-type validation
    for key, value in payload.items():
        if key in ("timestamp", "app_version"):
            continue
        if key not in KNOWN_RECORD_TYPES:
            # Unknown key: silently skip — NEVER log the key name (user-controlled)
            continue
        if not isinstance(value, list):
            errs.append(f"{key}: must be an array")
            continue
        for i, rec in enumerate(value):
            if not isinstance(rec, dict):
                errs.append(f"{key}[{i}]: record must be an object")
                continue
            errs += _validate_record(key, rec, i)

    return errs
