"""
Persistence tests: SQLite restart survival.

SYNTHETIC fixtures, SEPARATE isolated DB (not shared with other tests).

Tests:
- Records survive store close/reopen
- Record count correct after restart
- Ingest log survives restart
- Deduplication survives restart
- Read-only connection cannot INSERT/DROP
- DB file permissions 0600
- DB directory not a symlink rejected
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .fixtures import META_DEVICE_A, payload_with_steps, payload_with_sleep, payload_with_hr_samples
from .helpers import make_temp_db
from ..store import HealthStore, open_ro


def _ingest(store: HealthStore, payload: dict) -> None:
    received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.ingest_payload(payload, received_at)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.db_path = make_temp_db()

    def tearDown(self):
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_records_survive_close_reopen(self):
        store = HealthStore(self.db_path)
        _ingest(store, payload_with_steps(meta=META_DEVICE_A))
        store.close()
        conn = open_ro(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_multiple_types_survive_restart(self):
        store = HealthStore(self.db_path)
        _ingest(store, payload_with_steps(meta=META_DEVICE_A))
        _ingest(store, payload_with_sleep(meta=META_DEVICE_A))
        _ingest(store, payload_with_hr_samples(meta=META_DEVICE_A))
        store.close()
        conn = open_ro(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM health_records").fetchone()["n"]
            step_count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
            hr_count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='heart_rate'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(step_count, 1)
        self.assertEqual(hr_count, 3)
        self.assertEqual(total, 5)  # 1 step + 1 sleep + 3 HR

    def test_ingest_log_survives_restart(self):
        store = HealthStore(self.db_path)
        _ingest(store, payload_with_steps(meta=META_DEVICE_A))
        _ingest(store, payload_with_steps(meta=META_DEVICE_A))  # duplicate
        store.close()
        conn = open_ro(self.db_path)
        try:
            log_count = conn.execute(
                "SELECT COUNT(*) AS n FROM ingest_log"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(log_count, 2)

    def test_deduplication_survives_restart(self):
        store = HealthStore(self.db_path)
        _ingest(store, payload_with_steps(count=1000, meta=META_DEVICE_A))
        store.close()
        store2 = HealthStore(self.db_path)
        _ingest(store2, payload_with_steps(count=1000, meta=META_DEVICE_A))
        store2.close()
        conn = open_ro(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE record_type='steps'"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_readonly_connection_cannot_insert(self):
        store = HealthStore(self.db_path)
        store.close()
        conn = open_ro(self.db_path)
        try:
            with self.assertRaises((sqlite3.OperationalError, sqlite3.DatabaseError)):
                conn.execute(
                    "INSERT INTO ingest_log (received_at, payload_ts, app_version, record_count) "
                    "VALUES ('2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','test',0)"
                )
        finally:
            conn.close()

    def test_readonly_connection_cannot_drop(self):
        store = HealthStore(self.db_path)
        store.close()
        conn = open_ro(self.db_path)
        try:
            with self.assertRaises((sqlite3.OperationalError, sqlite3.DatabaseError)):
                conn.execute("DROP TABLE health_records")
        finally:
            conn.close()

    def test_db_file_permissions_600(self):
        store = HealthStore(self.db_path)
        store.close()
        mode = self.db_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"Expected 0600, got {oct(mode)}")

    def test_db_symlink_rejected(self):
        """HealthStore must refuse to open a symlink path."""
        import tempfile
        link = Path(tempfile.mktemp(suffix=".sqlite3.link"))
        target = make_temp_db()
        link.symlink_to(target)
        try:
            with self.assertRaises(RuntimeError):
                HealthStore(link)
        finally:
            link.unlink()
            try:
                target.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
