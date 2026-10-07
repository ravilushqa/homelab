"""
Validation module unit tests — all-or-nothing schema validation.

SYNTHETIC.
"""

import unittest
from ..validation import validate_payload


def _p(**kwargs) -> dict:
    base = {"timestamp": "2026-01-15T10:00:00Z", "app_version": "TEST"}
    base.update(kwargs)
    return base


class TestValidateTopLevel(unittest.TestCase):
    def test_valid_minimal(self):
        self.assertEqual(validate_payload(_p()), [])

    def test_missing_timestamp(self):
        errs = validate_payload({"app_version": "1.0"})
        self.assertTrue(any("timestamp" in e for e in errs))

    def test_naive_timestamp_invalid(self):
        errs = validate_payload({"timestamp": "2026-01-15T10:00:00"})
        self.assertTrue(errs)

    def test_non_string_timestamp(self):
        errs = validate_payload({"timestamp": 12345})
        self.assertTrue(errs)

    def test_known_array_must_be_list(self):
        errs = validate_payload(_p(steps={"count": 100}))
        self.assertTrue(errs)

    def test_unknown_key_silently_ignored(self):
        """Unknown array keys must not cause validation errors."""
        errs = validate_payload(_p(unknown_future_key=[{"foo": "bar"}]))
        self.assertEqual(errs, [])


class TestValidateSteps(unittest.TestCase):
    _valid = {"count": 500, "start_time": "2026-01-15T06:00:00Z",
              "end_time": "2026-01-15T07:00:00Z"}

    def test_valid(self):
        self.assertEqual(validate_payload(_p(steps=[self._valid])), [])

    def test_negative_count(self):
        rec = {**self._valid, "count": -1}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)

    def test_bool_count_rejected(self):
        rec = {**self._valid, "count": True}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)

    def test_float_count_rejected(self):
        rec = {**self._valid, "count": 100.5}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)

    def test_start_equals_end_rejected(self):
        rec = {"count": 100, "start_time": "2026-01-15T06:00:00Z",
               "end_time": "2026-01-15T06:00:00Z"}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)

    def test_start_after_end_rejected(self):
        rec = {"count": 100, "start_time": "2026-01-15T07:00:00Z",
               "end_time": "2026-01-15T06:00:00Z"}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)

    def test_naive_start_time_rejected(self):
        rec = {**self._valid, "start_time": "2026-01-15T06:00:00"}
        errs = validate_payload(_p(steps=[rec]))
        self.assertTrue(errs)


class TestValidateSleep(unittest.TestCase):
    _valid = {
        "session_end_time": "2026-01-15T07:30:00Z",
        "duration_seconds": 28800,
        "stages": [
            {"stage": "AWAKE", "start_time": "2026-01-15T07:00:00Z",
             "end_time": "2026-01-15T07:30:00Z", "duration_seconds": 1800},
        ],
    }

    def test_valid(self):
        self.assertEqual(validate_payload(_p(sleep=[self._valid])), [])

    def test_negative_duration_rejected(self):
        rec = {**self._valid, "duration_seconds": -1}
        errs = validate_payload(_p(sleep=[rec]))
        self.assertTrue(errs)

    def test_bool_duration_rejected(self):
        rec = {**self._valid, "duration_seconds": True}
        errs = validate_payload(_p(sleep=[rec]))
        self.assertTrue(errs)

    def test_invalid_session_end_time(self):
        rec = {**self._valid, "session_end_time": "not-a-date"}
        errs = validate_payload(_p(sleep=[rec]))
        self.assertTrue(errs)

    def test_stage_start_after_end_rejected(self):
        bad_stage = {"stage": "AWAKE", "start_time": "2026-01-15T08:00:00Z",
                     "end_time": "2026-01-15T07:00:00Z"}
        rec = {**self._valid, "stages": [bad_stage]}
        errs = validate_payload(_p(sleep=[rec]))
        self.assertTrue(errs)


class TestValidateHeartRate(unittest.TestCase):
    def test_valid_sample(self):
        rec = {"bpm": 72, "time": "2026-01-15T08:00:00Z"}
        self.assertEqual(validate_payload(_p(heart_rate=[rec])), [])

    def test_valid_aggregate(self):
        rec = {"time": "2026-01-15T08:00:00Z", "avg": 68.0, "min": 60.0, "max": 80.0, "bpm": 68.0}
        self.assertEqual(validate_payload(_p(heart_rate=[rec])), [])

    def test_bool_bpm_rejected(self):
        rec = {"bpm": True, "time": "2026-01-15T08:00:00Z"}
        errs = validate_payload(_p(heart_rate=[rec]))
        self.assertTrue(errs)

    def test_zero_bpm_rejected(self):
        rec = {"bpm": 0, "time": "2026-01-15T08:00:00Z"}
        errs = validate_payload(_p(heart_rate=[rec]))
        self.assertTrue(errs)

    def test_agg_min_gt_avg_rejected(self):
        rec = {"time": "2026-01-15T08:00:00Z", "avg": 60.0, "min": 70.0, "max": 80.0, "bpm": 60.0}
        errs = validate_payload(_p(heart_rate=[rec]))
        self.assertTrue(errs)


class TestValidateWeight(unittest.TestCase):
    def test_valid(self):
        rec = {"kilograms": 80.5, "time": "2026-01-15T08:00:00Z"}
        self.assertEqual(validate_payload(_p(weight=[rec])), [])

    def test_zero_weight_rejected(self):
        rec = {"kilograms": 0.0, "time": "2026-01-15T08:00:00Z"}
        errs = validate_payload(_p(weight=[rec]))
        self.assertTrue(errs)

    def test_negative_weight_rejected(self):
        rec = {"kilograms": -5.0, "time": "2026-01-15T08:00:00Z"}
        errs = validate_payload(_p(weight=[rec]))
        self.assertTrue(errs)


class TestValidateExercise(unittest.TestCase):
    _valid = {
        "type": "RUNNING",
        "start_time": "2026-01-15T09:00:00Z",
        "end_time": "2026-01-15T10:00:00Z",
        "duration_seconds": 3600,
    }

    def test_valid(self):
        self.assertEqual(validate_payload(_p(exercise=[self._valid])), [])

    def test_missing_type_rejected(self):
        rec = {k: v for k, v in self._valid.items() if k != "type"}
        errs = validate_payload(_p(exercise=[rec]))
        self.assertTrue(errs)

    def test_zero_duration_accepted(self):
        """Zero duration is valid: Kotlin Duration.toSeconds() returns 0 for sub-second intervals."""
        rec = {**self._valid, "duration_seconds": 0}
        errs = validate_payload(_p(exercise=[rec]))
        self.assertEqual(errs, [], "duration_seconds=0 must be accepted (sub-second Kotlin interval)")

    def test_negative_duration_rejected(self):
        rec = {**self._valid, "duration_seconds": -1}
        errs = validate_payload(_p(exercise=[rec]))
        self.assertTrue(errs)

    def test_inf_duration_rejected(self):
        rec = {**self._valid, "duration_seconds": float("inf")}
        errs = validate_payload(_p(exercise=[rec]))
        self.assertTrue(errs)

    def test_bool_steps_rejected(self):
        rec = {**self._valid, "steps": True}
        errs = validate_payload(_p(exercise=[rec]))
        self.assertTrue(errs)


class TestAllOrNothing(unittest.TestCase):
    def test_one_bad_record_fails_whole_payload(self):
        """If steps[0] is invalid, payload is rejected even if weight is valid."""
        payload = _p(
            steps=[{"count": -1, "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T07:00:00Z"}],
            weight=[{"kilograms": 80.0, "time": "2026-01-15T08:00:00Z"}],
        )
        errs = validate_payload(payload)
        self.assertTrue(errs)

    def test_all_valid_no_errors(self):
        payload = _p(
            steps=[{"count": 500, "start_time": "2026-01-15T06:00:00Z",
                    "end_time": "2026-01-15T07:00:00Z"}],
            weight=[{"kilograms": 80.0, "time": "2026-01-15T08:00:00Z"}],
        )
        self.assertEqual(validate_payload(payload), [])


if __name__ == "__main__":
    unittest.main()
