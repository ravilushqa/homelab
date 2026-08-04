import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    Config,
    RateLimiter,
    access_window_open,
    parse_content_length,
    parse_iso_datetime,
    validate_config,
)


def config_with_window(start="", end=""):
    return Config(
        ha_url="https://ha.example",
        ha_token="ha-token",
        access_token="guest-token",
        pin="1234",
        access_start=parse_iso_datetime(start),
        access_end=parse_iso_datetime(end),
        rate_limit_seconds=10,
        building_entity="button.comelit_default_open_door",
        building_domain="button",
        building_service="press",
        apartment_entity="button.home_door_unlatch",
        apartment_domain="button",
        apartment_service="press",
    )


class ConfigTests(unittest.TestCase):
    def test_parse_iso_datetime_normalizes_to_utc(self):
        parsed = parse_iso_datetime("2026-08-04T12:30:00+02:00")
        self.assertEqual(parsed, datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc))

    def test_access_window_accepts_inside_bounds(self):
        config = config_with_window("2026-08-04T10:00:00Z", "2026-08-04T12:00:00Z")
        now = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
        self.assertTrue(access_window_open(config, now))

    def test_access_window_rejects_before_start(self):
        config = config_with_window("2026-08-04T10:00:00Z", "")
        now = datetime(2026, 8, 4, 9, 59, tzinfo=timezone.utc)
        self.assertFalse(access_window_open(config, now))

    def test_validate_config_rejects_empty_pin(self):
        config = config_with_window()
        config = Config(**{**config.__dict__, "pin": ""})
        with self.assertRaises(SystemExit) as raised:
            validate_config(config)
        self.assertIn("PIN", str(raised.exception))


class ContentLengthTests(unittest.TestCase):
    def test_parse_content_length_accepts_missing_and_valid(self):
        self.assertEqual(parse_content_length(None), 0)
        self.assertEqual(parse_content_length("12"), 12)

    def test_parse_content_length_rejects_invalid_negative_and_too_large(self):
        for value in ("abc", "-1", "8193"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_content_length(value, max_bytes=8192)


class RateLimiterTests(unittest.TestCase):
    def test_rate_limit_is_per_client_ip_and_action(self):
        limiter = RateLimiter(10)
        self.assertTrue(limiter.allow("192.0.2.1", "building", now=100))
        self.assertFalse(limiter.allow("192.0.2.1", "building", now=105))
        self.assertTrue(limiter.allow("192.0.2.1", "apartment", now=105))
        self.assertTrue(limiter.allow("192.0.2.2", "building", now=105))
        self.assertTrue(limiter.allow("192.0.2.1", "building", now=111))

    def test_rate_limiter_is_thread_safe(self):
        limiter = RateLimiter(10)
        results = []

        def attempt():
            results.append(limiter.allow("192.0.2.1", "building", now=100))

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count(True), 1)


class PageTests(unittest.TestCase):
    def test_root_page_reads_token_from_hash_not_path(self):
        from app import html_page

        page = html_page()
        self.assertIn("location.hash", page)
        self.assertNotIn("location.pathname.slice(1)", page)


class HandlerTests(unittest.TestCase):
    def test_token_path_no_longer_serves_app(self):
        from app import GuestDoorHandler

        class FakeHandler(GuestDoorHandler):
            def __init__(self):
                self.path = "/guest-token"
                self.client_address = ("192.0.2.1", 12345)
                self.responses = []
                self.server = type("Server", (), {"config": config_with_window()})()

            def send_json(self, status, payload):
                self.responses.append((status, payload))

        handler = FakeHandler()
        handler.do_GET()

        self.assertEqual(handler.responses[0][0].value, 404)


if __name__ == "__main__":
    unittest.main()
