"""
SYNTHETIC test fixtures.

ALL data here is fabricated for testing purposes only.
This module MUST NEVER be imported by production code.
Production must NEVER receive synthetic health records.
"""

_SYNTHETIC = True  # sentinel

SYNTHETIC_TIMESTAMP = "2026-01-15T10:00:00.000Z"
SYNTHETIC_APP_VERSION = "SYNTHETIC-TEST-1.0"

SYNTHETIC_TOKEN = "synthetic-test-token-NEVER-USE-IN-PROD-abc123def456"

DEVICE_A = {"manufacturer": "SyntheticCorp", "model": "TestDevice-A", "type": 3}
DEVICE_B = {"manufacturer": "FakeMaker", "model": "MockDevice-B", "type": 1}

META_DEVICE_A = {
    "data_origin": "com.synthetic.testapp",
    "recording_method": 2,
    "device": DEVICE_A,
}
META_DEVICE_B = {
    "data_origin": "com.fakecorp.health",
    "recording_method": 1,
    "device": DEVICE_B,
}
META_NO_DEVICE = {
    "data_origin": "com.synthetic.testapp",
    "recording_method": 2,
}
# Alias used in some tests
META_SAME_ORIGIN_NO_DEVICE = META_NO_DEVICE


def minimal_payload(**extra) -> dict:
    p = {"timestamp": SYNTHETIC_TIMESTAMP, "app_version": SYNTHETIC_APP_VERSION}
    p.update(extra)
    return p


def payload_with_steps(
    count: int = 1000,
    start: str = "2026-01-15T06:00:00Z",
    end: str = "2026-01-15T07:00:00Z",
    meta: dict = None,
) -> dict:
    rec = {"count": count, "start_time": start, "end_time": end}
    if meta is not None:
        rec["metadata"] = meta
    return minimal_payload(steps=[rec])


def payload_with_hr_samples(meta: dict = None) -> dict:
    samples = [
        {"bpm": 65, "time": "2026-01-15T08:00:00Z"},
        {"bpm": 70, "time": "2026-01-15T08:01:00Z"},
        {"bpm": 68, "time": "2026-01-15T08:02:00Z"},
    ]
    if meta is not None:
        for s in samples:
            s["metadata"] = meta
    return minimal_payload(heart_rate=samples)


def payload_with_hr_aggregates(meta: dict = None) -> dict:
    buckets = [
        {"time": "2026-01-15T08:00:00Z", "avg": 68.0, "min": 62.0, "max": 75.0, "bpm": 68.0},
        {"time": "2026-01-15T08:05:00Z", "avg": 72.0, "min": 65.0, "max": 80.0, "bpm": 72.0},
    ]
    if meta is not None:
        for b in buckets:
            b["metadata"] = meta
    return minimal_payload(heart_rate=buckets)


def payload_with_sleep(
    end: str = "2026-01-15T07:30:00Z",
    duration: int = 28800,
    meta: dict = None,
) -> dict:
    rec = {
        "session_end_time": end,
        "duration_seconds": duration,
        "stages": [
            {
                "stage": "STAGE_AWAKE",
                "start_time": "2026-01-14T23:00:00Z",
                "end_time": "2026-01-14T23:10:00Z",
                "duration_seconds": 600,
            },
            {
                "stage": "STAGE_LIGHT_SLEEP",
                "start_time": "2026-01-14T23:10:00Z",
                "end_time": "2026-01-15T07:30:00Z",
                "duration_seconds": 30600,
            },
        ],
    }
    if meta is not None:
        rec["metadata"] = meta
    return minimal_payload(sleep=[rec])


def payload_with_exercise(
    ex_type: str = "EXERCISE_TYPE_RUNNING",
    start: str = "2026-01-15T09:00:00Z",
    end: str = "2026-01-15T10:00:00Z",
    duration: int = 3600,
    meta: dict = None,
) -> dict:
    rec = {
        "type": ex_type,
        "start_time": start,
        "end_time": end,
        "duration_seconds": duration,
        "distance_meters": 10000.0,
    }
    if meta is not None:
        rec["metadata"] = meta
    return minimal_payload(exercise=[rec])


def payload_with_weight(
    kg: float = 80.5,
    time: str = "2026-01-15T08:00:00Z",
    meta: dict = None,
) -> dict:
    rec = {"kilograms": kg, "time": time}
    if meta is not None:
        rec["metadata"] = meta
    return minimal_payload(weight=[rec])


def payload_multi_source() -> dict:
    return minimal_payload(
        steps=[
            {
                "count": 2000,
                "start_time": "2026-01-15T06:00:00Z",
                "end_time": "2026-01-15T07:00:00Z",
                "metadata": META_DEVICE_A,
            },
            {
                "count": 3000,
                "start_time": "2026-01-15T07:00:00Z",
                "end_time": "2026-01-15T08:00:00Z",
                "metadata": META_DEVICE_B,
            },
        ]
    )
