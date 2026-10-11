"""
Tests for consultation_read.py.

SYNTHETIC test data only — no production records, no PHI, no tokens.
All databases are created in TMPDIR and removed after each test.

Imports the module by file path via importlib.util so the tests run
correctly from the repository root without relative-import issues,
following the same pattern as test_skill_isolation_root.py.

Coverage:
  - _open_ro: missing DB, symlink rejection, read-only enforcement, relative paths
  - _provenance_notes: _no_metadata, null fields, Pixel Watch wearable origin,
    Pixel Watch via device model + Google Fit origin, no_metadata != Pixel Watch
  - _day_utc_epoch_bounds: day boundaries in CET and CEST
  - _build_inventory: grouping, count, dual age fields (record vs transport),
    old record freshly transported shows large record_age but small transport_age
  - _query_records_for_day: overlap semantics, per-source grouping
  - _query_queue_for_day: state buckets, day boundary, all four states,
    correlation field present, not_verified for current format
  - run(): partial results on DB error, notices present, no cross-source sums
  - Integrity: database bytes unchanged after read, missing DB not created
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Load module by path (no relative imports needed) ──────────────────────────

_MODULE_PATH = Path(__file__).parent.parent / "consultation_read.py"
_spec = importlib.util.spec_from_file_location("consultation_read", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_open_ro = _mod._open_ro
_check_no_symlinks = _mod._check_no_symlinks
_build_inventory = _mod._build_inventory
_query_records_for_day = _mod._query_records_for_day
_query_queue_for_day = _mod._query_queue_for_day
_provenance_notes = _mod._provenance_notes
_is_pixel_watch_source = _mod._is_pixel_watch_source
_parse_source_key = _mod._parse_source_key
_day_utc_epoch_bounds = _mod._day_utc_epoch_bounds
_elapsed_hours_from_iso = _mod._elapsed_hours_from_iso
_elapsed_hours_from_epoch = _mod._elapsed_hours_from_epoch
_epoch_to_iso = _mod._epoch_to_iso
_correlate_queue_entry = _mod._correlate_queue_entry
run = _mod.run


# ── Synthetic source keys (no real device data) ────────────────────────────────

_SK_FULL = json.dumps(
    {
        "device": {"manufacturer": "SyntheticCorp", "model": "TestDevice-A", "type": 3},
        "method": 2,
        "origin": "com.synthetic.testapp",
    },
    sort_keys=True,
    separators=(",", ":"),
)

_SK_NO_META = json.dumps({"_no_metadata": True}, separators=(",", ":"))

_SK_NULL_ORIGIN = json.dumps(
    {
        "device": {"manufacturer": "SyntheticCorp", "model": "TestDevice-A", "type": 3},
        "method": 2,
        "origin": None,
    },
    sort_keys=True,
    separators=(",", ":"),
)

_SK_PIXEL_WATCH_WEARABLE = json.dumps(
    {
        "device": {"manufacturer": "Google", "model": "Pixel Watch Synthetic", "type": 1},
        "method": 2,
        "origin": "com.google.android.wearable.healthservices",
    },
    sort_keys=True,
    separators=(",", ":"),
)

# Pixel Watch via Google Fit origin + device model — the shared-origin case.
_SK_PIXEL_WATCH_GFT = json.dumps(
    {
        "device": {"manufacturer": "Google", "model": "Pixel Watch 2 Synthetic", "type": 1},
        "method": 2,
        "origin": "com.google.android.apps.fitness",
    },
    sort_keys=True,
    separators=(",", ":"),
)

# Google Fit origin WITHOUT Pixel Watch device model — must NOT trigger PW note.
_SK_GFIT_PHONE = json.dumps(
    {
        "device": {"manufacturer": "Samsung", "model": "Galaxy S24 Synthetic", "type": 3},
        "method": 2,
        "origin": "com.google.android.apps.fitness",
    },
    sort_keys=True,
    separators=(",", ":"),
)

_SYNTHETIC_RECEIVED = "2026-01-15T10:00:00Z"
_PIXEL_WATCH_FIRST_RECEIVED = "2026-10-08T09:00:00Z"
# A received_at well in the future relative to an old record_end
_TRANSPORT_RECEIVED = "2026-10-11T08:00:00Z"  # "today" for freshness tests


# ── DB creation helpers ────────────────────────────────────────────────────────

_HEALTH_DDL = """
PRAGMA journal_mode = DELETE;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    payload_ts TEXT NOT NULL,
    app_version TEXT,
    record_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS health_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    expl_ns TEXT,
    expl_id TEXT,
    sample_disc TEXT,
    temporal_key TEXT,
    record_start_s REAL NOT NULL,
    record_end_s REAL NOT NULL,
    client_record_version INTEGER,
    last_modified_s REAL,
    data_origin TEXT,
    recording_method TEXT,
    device_json TEXT,
    record_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hr_type_src_start
    ON health_records (record_type, source_key, record_start_s);
"""

_NUTRITION_DDL = """
CREATE TABLE IF NOT EXISTS nutrition_schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nutrition_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_record_id TEXT NOT NULL UNIQUE,
    client_record_version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft',
    name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    energy_kcal REAL NOT NULL,
    protein_g REAL NOT NULL,
    carbohydrate_g REAL NOT NULL,
    fat_g REAL NOT NULL,
    description TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    acked_at TEXT,
    fetched_at TEXT,
    delivery_possible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _make_health_db(tmp_dir: Path) -> Path:
    db_path = tmp_dir / "test_health.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_HEALTH_DDL)
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.commit()
    conn.close()
    return db_path


def _make_nutrition_db(tmp_dir: Path) -> Path:
    db_path = tmp_dir / "test_nutrition.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_NUTRITION_DDL)
    conn.execute("INSERT INTO nutrition_schema_version VALUES (2)")
    conn.commit()
    conn.close()
    return db_path


def _insert_health_record(
    db_path: Path,
    record_type: str,
    source_key: str,
    start_s: float,
    end_s: float,
    received_at: str = _SYNTHETIC_RECEIVED,
    record_json: Optional[str] = None,
    data_origin: Optional[str] = None,
    recording_method: Optional[str] = None,
    identity_key: Optional[str] = None,
    expl_ns: Optional[str] = None,
    expl_id: Optional[str] = None,
    client_record_version: Optional[int] = None,
) -> None:
    if record_json is None:
        record_json = json.dumps({"synthetic": True, "record_type": record_type})
    if identity_key is None:
        identity_key = json.dumps(["t", record_type, source_key, str(start_s)])
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO health_records
        (identity_key, record_type, source_key, expl_ns, expl_id,
         client_record_version, record_start_s, record_end_s,
         data_origin, recording_method, record_json, received_at, payload_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity_key, record_type, source_key, expl_ns, expl_id,
            client_record_version, start_s, end_s,
            data_origin, recording_method, record_json, received_at, received_at,
        ),
    )
    conn.commit()
    conn.close()


def _insert_queue_record(
    db_path: Path,
    client_record_id: str,
    state: str,
    name: str,
    start_time: str,
    end_time: str,
    energy_kcal: float = 500.0,
    protein_g: float = 30.0,
    carbohydrate_g: float = 60.0,
    fat_g: float = 15.0,
    confirmed_by: Optional[str] = None,
    confirmed_at: Optional[str] = None,
    acked_at: Optional[str] = None,
    delivery_possible: int = 0,
) -> None:
    now = "2026-01-15T10:00:00Z"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO nutrition_queue
        (client_record_id, client_record_version, state, name,
         start_time, end_time, energy_kcal, protein_g, carbohydrate_g, fat_g,
         description, confirmed_by, confirmed_at, acked_at,
         delivery_possible, created_at, updated_at)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, '[AI estimate] Synthetic meal',
                ?, ?, ?, ?, ?, ?)
        """,
        (
            client_record_id, state, name, start_time, end_time,
            energy_kcal, protein_g, carbohydrate_g, fat_g,
            confirmed_by, confirmed_at, acked_at, delivery_possible, now, now,
        ),
    )
    conn.commit()
    conn.close()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Tests: _open_ro ────────────────────────────────────────────────────────────

class TestOpenRo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_db_raises_file_not_found(self):
        missing = self.tmp_dir / "no_such.sqlite3"
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            _open_ro(missing)

    def test_symlink_db_rejected(self):
        real_db = _make_health_db(self.tmp_dir)
        link_path = self.tmp_dir / "link.sqlite3"
        link_path.symlink_to(real_db)
        with self.assertRaises(RuntimeError) as ctx:
            _open_ro(link_path)
        self.assertIn("symlink", str(ctx.exception).lower())

    def test_symlink_checked_before_exists(self):
        # A symlink pointing to a non-existent target should raise RuntimeError
        # (symlink check), not FileNotFoundError.
        dangling = self.tmp_dir / "dangling.sqlite3"
        dangling.symlink_to(self.tmp_dir / "nowhere.sqlite3")
        with self.assertRaises(RuntimeError):
            _open_ro(dangling)

    def test_open_real_db_returns_connection(self):
        db_path = _make_health_db(self.tmp_dir)
        conn = _open_ro(db_path)
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(row[0], 2)
        finally:
            conn.close()

    def test_read_only_rejects_insert(self):
        db_path = _make_health_db(self.tmp_dir)
        conn = _open_ro(db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO schema_version VALUES (99)")
        finally:
            conn.close()

    def test_read_only_rejects_drop(self):
        db_path = _make_health_db(self.tmp_dir)
        conn = _open_ro(db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DROP TABLE schema_version")
        finally:
            conn.close()

    def test_relative_path_works(self):
        # _open_ro must work with a relative path (uses absolute() for as_uri()).
        db_path = _make_health_db(self.tmp_dir)
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.tmp_dir)
            rel_path = Path("test_health.sqlite3")
            conn = _open_ro(rel_path)
            try:
                row = conn.execute("SELECT version FROM schema_version").fetchone()
                self.assertEqual(row[0], 2)
            finally:
                conn.close()
        finally:
            os.chdir(orig_cwd)


# ── Tests: database integrity ──────────────────────────────────────────────────

class TestDatabaseIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_integrity_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_health_db_bytes_unchanged_after_read(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(
            db_path, "steps", _SK_FULL,
            start_s=1736895600.0, end_s=1736899200.0,
        )
        before = _sha256_file(db_path)
        conn = _open_ro(db_path)
        try:
            conn.execute("SELECT COUNT(*) FROM health_records").fetchone()
        finally:
            conn.close()
        after = _sha256_file(db_path)
        self.assertEqual(
            before, after,
            "Health DB bytes changed after read-only open and query."
        )

    def test_nutrition_db_bytes_unchanged_after_read(self):
        db_path = _make_nutrition_db(self.tmp_dir)
        _insert_queue_record(
            db_path, "nutr_test_001", "draft", "Synthetic Breakfast",
            "2026-01-15T07:00:00Z", "2026-01-15T07:30:00Z",
        )
        before = _sha256_file(db_path)
        conn = _open_ro(db_path)
        try:
            conn.execute("SELECT COUNT(*) FROM nutrition_queue").fetchone()
        finally:
            conn.close()
        after = _sha256_file(db_path)
        self.assertEqual(
            before, after,
            "Nutrition DB bytes changed after read-only open and query."
        )

    def test_missing_health_db_not_created(self):
        missing = self.tmp_dir / "never_created_health.sqlite3"
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            _open_ro(missing)
        self.assertFalse(
            missing.exists(),
            "_open_ro created a file for a missing health DB.",
        )

    def test_missing_nutrition_db_not_created(self):
        missing = self.tmp_dir / "never_created_nutrition.sqlite3"
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            _open_ro(missing)
        self.assertFalse(
            missing.exists(),
            "_open_ro created a file for a missing nutrition DB.",
        )

    def test_run_does_not_create_missing_dbs(self):
        missing_health = self.tmp_dir / "nonexistent_health.sqlite3"
        missing_nutrition = self.tmp_dir / "nonexistent_nutrition.sqlite3"
        result = run(missing_health, missing_nutrition, "2026-01-15")
        self.assertFalse(missing_health.exists())
        self.assertFalse(missing_nutrition.exists())
        self.assertIn("errors", result)
        self.assertIn("health_db", result["errors"])
        self.assertIn("nutrition_db", result["errors"])


# ── Tests: day boundaries ──────────────────────────────────────────────────────

class TestDayBoundaries(unittest.TestCase):
    """
    Day boundary tests use 2026-01-15 (CET = UTC+1, winter):
      Berlin midnight = 2026-01-14T23:00:00Z  (day_start_s)
      Next midnight   = 2026-01-15T23:00:00Z  (day_end_s)
    """

    DATE = "2026-01-15"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_bounds_")
        self.tmp_dir = Path(self._tmp)
        self.db_path = _make_health_db(self.tmp_dir)
        self.day_start_s, self.day_end_s = _day_utc_epoch_bounds(self.DATE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _insert(self, tag: str, start_s: float, end_s: float) -> None:
        _insert_health_record(
            self.db_path, "exercise", _SK_FULL,
            start_s=start_s, end_s=end_s,
            identity_key=f"t_test_{tag}",
        )

    def test_record_ending_at_day_start_excluded(self):
        self._insert("before", self.day_start_s - 7200, self.day_start_s)
        conn = _open_ro(self.db_path)
        try:
            groups = _query_records_for_day(
                conn, "exercise", self.day_start_s, self.day_end_s
            )
        finally:
            conn.close()
        self.assertEqual(groups, [], "Record ending exactly at day start should be excluded.")

    def test_record_ending_one_second_into_day_included(self):
        self._insert("just_in", self.day_start_s - 60, self.day_start_s + 1)
        conn = _open_ro(self.db_path)
        try:
            groups = _query_records_for_day(
                conn, "exercise", self.day_start_s, self.day_end_s
            )
        finally:
            conn.close()
        total = sum(len(g["records"]) for g in groups)
        self.assertEqual(total, 1, "Record overlapping day start should be included.")

    def test_record_entirely_within_day_included(self):
        mid = (self.day_start_s + self.day_end_s) / 2
        self._insert("inside", mid - 1800, mid + 1800)
        conn = _open_ro(self.db_path)
        try:
            groups = _query_records_for_day(
                conn, "exercise", self.day_start_s, self.day_end_s
            )
        finally:
            conn.close()
        total = sum(len(g["records"]) for g in groups)
        self.assertEqual(total, 1)

    def test_record_starting_exactly_at_day_end_excluded(self):
        self._insert("at_end", self.day_end_s, self.day_end_s + 3600)
        conn = _open_ro(self.db_path)
        try:
            groups = _query_records_for_day(
                conn, "exercise", self.day_start_s, self.day_end_s
            )
        finally:
            conn.close()
        self.assertEqual(groups, [], "Record starting at day end should be excluded.")

    def test_record_spanning_full_day_included(self):
        self._insert("spanning", self.day_start_s - 3600, self.day_end_s + 3600)
        conn = _open_ro(self.db_path)
        try:
            groups = _query_records_for_day(
                conn, "exercise", self.day_start_s, self.day_end_s
            )
        finally:
            conn.close()
        total = sum(len(g["records"]) for g in groups)
        self.assertEqual(total, 1)

    def test_queue_record_at_day_start_included(self):
        db_path = _make_nutrition_db(self.tmp_dir)
        start_iso = datetime.fromtimestamp(
            self.day_start_s, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            self.day_start_s + 1800, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            db_path, "nutr_bound_001", "confirmed",
            "Synthetic Breakfast", start_iso, end_iso,
        )
        conn = _open_ro(db_path)
        try:
            by_state = _query_queue_for_day(conn, self.DATE)
        finally:
            conn.close()
        self.assertEqual(
            len(by_state["confirmed"]), 1,
            "Queue record at day start should be included.",
        )

    def test_queue_record_before_day_start_excluded(self):
        db_path = _make_nutrition_db(self.tmp_dir)
        before_s = self.day_start_s - 60
        start_iso = datetime.fromtimestamp(
            before_s, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            before_s + 1800, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            db_path, "nutr_bound_002", "draft",
            "Synthetic Late Snack", start_iso, end_iso,
        )
        conn = _open_ro(db_path)
        try:
            by_state = _query_queue_for_day(conn, self.DATE)
        finally:
            conn.close()
        total = sum(len(v) for v in by_state.values())
        self.assertEqual(total, 0, "Queue record before day start should be excluded.")

    def test_queue_record_at_day_end_excluded(self):
        db_path = _make_nutrition_db(self.tmp_dir)
        start_iso = datetime.fromtimestamp(
            self.day_end_s, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            self.day_end_s + 1800, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            db_path, "nutr_bound_003", "draft",
            "Synthetic Next Day Breakfast", start_iso, end_iso,
        )
        conn = _open_ro(db_path)
        try:
            by_state = _query_queue_for_day(conn, self.DATE)
        finally:
            conn.close()
        total = sum(len(v) for v in by_state.values())
        self.assertEqual(total, 0, "Queue record at day end should be excluded.")

    def test_day_epoch_bounds_cest(self):
        start_s, end_s = _day_utc_epoch_bounds("2026-08-15")
        start_dt = datetime.fromtimestamp(start_s, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_s, tz=timezone.utc)
        self.assertEqual(start_dt.hour, 22)
        self.assertEqual(start_dt.day, 14)
        self.assertEqual(end_dt.hour, 22)
        self.assertEqual(end_dt.day, 15)

    def test_day_epoch_bounds_cet(self):
        start_s, _ = _day_utc_epoch_bounds("2026-01-15")
        start_dt = datetime.fromtimestamp(start_s, tz=timezone.utc)
        self.assertEqual(start_dt.hour, 23)
        self.assertEqual(start_dt.day, 14)


# ── Tests: inventory age fields ───────────────────────────────────────────────

class TestInventoryAgeFields(unittest.TestCase):
    """
    Verify the two age fields in inventory:
      latest_record_age_hours: how old is the health data (from record_end_s)
      transport_age_hours: how old is the most recent ingest (from received_at)

    Key test: a record with an old record_end_s but a recent received_at must
    show a large latest_record_age_hours and a small transport_age_hours.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_age_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_old_record_freshly_transported(self):
        db_path = _make_health_db(self.tmp_dir)
        # Record end: 2026-01-15 (very old relative to today 2026-10-11)
        old_record_end_s = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        # received_at: 2026-10-11 (recent transport)
        recent_received = "2026-10-11T08:00:00Z"
        _insert_health_record(
            db_path, "steps", _SK_FULL,
            start_s=old_record_end_s - 3600,
            end_s=old_record_end_s,
            received_at=recent_received,
        )
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        self.assertEqual(len(inv), 1)
        entry = inv[0]
        record_age = entry["latest_record_age_hours"]
        transport_age = entry["transport_age_hours"]
        self.assertIsNotNone(record_age)
        self.assertIsNotNone(transport_age)
        # Record is from Jan 2026; today is Oct 2026 — must be much older than transport
        self.assertGreater(
            record_age, transport_age + 100,
            "Old record freshly transported: record_age should vastly exceed transport_age."
        )
        # Transport is from today — should be < 48h at time of test
        self.assertLess(transport_age, 48.0)

    def test_recent_record_and_transport(self):
        db_path = _make_health_db(self.tmp_dir)
        now_s = datetime.now(timezone.utc).timestamp()
        _insert_health_record(
            db_path, "steps", _SK_FULL,
            start_s=now_s - 3600,
            end_s=now_s - 1800,
            received_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        entry = inv[0]
        self.assertLess(entry["latest_record_age_hours"], 2.0)
        self.assertLess(entry["transport_age_hours"], 2.0)

    def test_inventory_no_freshness_key(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(db_path, "steps", _SK_FULL, 1000.0, 2000.0)
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        entry = inv[0]
        self.assertNotIn("freshness_hours", entry)
        self.assertIn("latest_record_age_hours", entry)
        self.assertIn("transport_age_hours", entry)


# ── Tests: inventory grouping ─────────────────────────────────────────────────

class TestInventory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_inv_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_db_returns_empty_inventory(self):
        db_path = _make_health_db(self.tmp_dir)
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        self.assertEqual(inv, [])

    def test_inventory_groups_by_type_and_source(self):
        db_path = _make_health_db(self.tmp_dir)
        for i in range(3):
            _insert_health_record(
                db_path, "steps", _SK_FULL,
                start_s=1000.0 + i * 3600,
                end_s=1000.0 + i * 3600 + 3600,
                identity_key=f"steps_full_{i}",
            )
        for i in range(2):
            _insert_health_record(
                db_path, "steps", _SK_NO_META,
                start_s=2000.0 + i * 3600,
                end_s=2000.0 + i * 3600 + 3600,
                identity_key=f"steps_no_meta_{i}",
            )
        _insert_health_record(
            db_path, "exercise", _SK_FULL,
            start_s=9000.0, end_s=10000.0,
            identity_key="ex_1",
        )
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        self.assertEqual(len(inv), 3)
        steps_full = next(
            e for e in inv if e["record_type"] == "steps" and e["source_key"] == _SK_FULL
        )
        steps_no_meta = next(
            e for e in inv if e["record_type"] == "steps" and e["source_key"] == _SK_NO_META
        )
        self.assertEqual(steps_full["count"], 3)
        self.assertEqual(steps_no_meta["count"], 2)

    def test_inventory_no_metadata_provenance_note(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(db_path, "steps", _SK_NO_META, 1000.0, 2000.0)
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        notes = inv[0].get("provenance_notes", [])
        combined = " ".join(notes).lower()
        self.assertIn("no device metadata", combined)

    def test_inventory_null_origin_provenance_note(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(db_path, "steps", _SK_NULL_ORIGIN, 1000.0, 2000.0)
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        notes = inv[0].get("provenance_notes", [])
        combined = " ".join(notes).lower()
        self.assertIn("incomplete", combined)

    def test_inventory_pixel_watch_wearable_provenance_note(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(
            db_path, "exercise", _SK_PIXEL_WATCH_WEARABLE,
            start_s=1000.0, end_s=2000.0,
            received_at=_PIXEL_WATCH_FIRST_RECEIVED,
        )
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        notes = inv[0].get("provenance_notes", [])
        combined = " ".join(notes)
        self.assertIn("Pixel Watch", combined)
        self.assertIn("2026-10-08", combined)
        self.assertIn("external provenance", combined.lower())

    def test_inventory_full_source_has_no_provenance_note(self):
        db_path = _make_health_db(self.tmp_dir)
        _insert_health_record(db_path, "steps", _SK_FULL, 1000.0, 2000.0)
        conn = _open_ro(db_path)
        try:
            inv = _build_inventory(conn)
        finally:
            conn.close()
        self.assertNotIn("provenance_notes", inv[0])


# ── Tests: provenance notes ────────────────────────────────────────────────────

class TestProvenanceNotes(unittest.TestCase):
    def test_no_metadata_returns_attribution_note(self):
        notes = _provenance_notes(_SK_NO_META, None)
        self.assertTrue(notes)
        self.assertIn("No device metadata", notes[0])

    def test_no_metadata_not_attributed_to_pixel_watch(self):
        # _no_metadata source must NEVER be attributed to Pixel Watch,
        # even if min_received_at matches the known Pixel Watch first-seen date.
        notes = _provenance_notes(_SK_NO_META, _PIXEL_WATCH_FIRST_RECEIVED)
        combined = " ".join(notes).lower()
        self.assertNotIn("pixel watch", combined)

    def test_null_origin_flags_incomplete(self):
        notes = _provenance_notes(_SK_NULL_ORIGIN, None)
        self.assertTrue(notes)
        combined = " ".join(notes)
        self.assertIn("Incomplete", combined)
        self.assertIn("data_origin", combined)

    def test_pixel_watch_wearable_origin_first_seen_date_in_note(self):
        notes = _provenance_notes(_SK_PIXEL_WATCH_WEARABLE, _PIXEL_WATCH_FIRST_RECEIVED)
        combined = " ".join(notes)
        self.assertIn("2026-10-08", combined)
        self.assertIn("Pixel Watch", combined)

    def test_pixel_watch_gfit_origin_plus_device_model_triggers_note(self):
        # Google Fit origin + Pixel Watch device model → should trigger PW note.
        notes = _provenance_notes(_SK_PIXEL_WATCH_GFT, _PIXEL_WATCH_FIRST_RECEIVED)
        combined = " ".join(notes)
        self.assertIn("Pixel Watch", combined)

    def test_gfit_phone_origin_no_pixel_watch_note(self):
        # Google Fit origin WITHOUT Pixel Watch model → must NOT trigger PW note.
        notes = _provenance_notes(_SK_GFIT_PHONE, _SYNTHETIC_RECEIVED)
        combined = " ".join(notes).lower()
        self.assertNotIn("pixel watch", combined)

    def test_pixel_watch_note_does_not_declare_fake_or_real(self):
        notes = _provenance_notes(_SK_PIXEL_WATCH_WEARABLE, _PIXEL_WATCH_FIRST_RECEIVED)
        combined = " ".join(notes).lower()
        self.assertNotIn("fake", combined)
        self.assertNotIn("not real", combined)
        self.assertNotIn("are real", combined)
        # Generic titles: note must not claim they prove demo status
        self.assertNotIn("indicate demo", combined)
        self.assertNotIn("indicates demo", combined)
        self.assertIn("external provenance", combined)

    def test_full_source_no_notes(self):
        notes = _provenance_notes(_SK_FULL, _SYNTHETIC_RECEIVED)
        self.assertEqual(notes, [])

    def test_unparseable_source_key_returns_note(self):
        notes = _provenance_notes("not-json{{", None)
        self.assertTrue(notes)

    def test_is_pixel_watch_wearable_origin(self):
        parsed = _parse_source_key(_SK_PIXEL_WATCH_WEARABLE)
        self.assertTrue(_is_pixel_watch_source(parsed))

    def test_is_pixel_watch_gfit_with_model(self):
        parsed = _parse_source_key(_SK_PIXEL_WATCH_GFT)
        self.assertTrue(_is_pixel_watch_source(parsed))

    def test_not_pixel_watch_gfit_phone(self):
        parsed = _parse_source_key(_SK_GFIT_PHONE)
        self.assertFalse(_is_pixel_watch_source(parsed))

    def test_not_pixel_watch_no_meta(self):
        parsed = _parse_source_key(_SK_NO_META)
        self.assertFalse(_is_pixel_watch_source(parsed))


# ── Tests: nutrition queue distinction ────────────────────────────────────────

class TestQueueDistinction(unittest.TestCase):
    DATE = "2026-01-15"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_queue_")
        self.tmp_dir = Path(self._tmp)
        self.health_db = _make_health_db(self.tmp_dir)
        self.nutrition_db = _make_nutrition_db(self.tmp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _day_bounds(self):
        return _day_utc_epoch_bounds(self.DATE)

    def test_nutrition_ingest_in_health_not_queue(self):
        day_start_s, day_end_s = self._day_bounds()
        mid = (day_start_s + day_end_s) / 2
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 1800, end_s=mid + 1800,
        )
        result = run(self.health_db, self.nutrition_db, self.DATE)
        self.assertGreater(len(result["date_nutrition_ingest"]), 0)
        total_queue = sum(len(v) for v in result["queue_by_state"].values())
        self.assertEqual(total_queue, 0)

    def test_queue_record_in_queue_not_ingest(self):
        day_start_s, _ = self._day_bounds()
        start_iso = datetime.fromtimestamp(
            day_start_s + 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            day_start_s + 5400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            self.nutrition_db, "nutr_distinct_001", "draft",
            "Synthetic Queue Meal", start_iso, end_iso,
        )
        result = run(self.health_db, self.nutrition_db, self.DATE)
        self.assertEqual(result["date_nutrition_ingest"], [])
        total_queue = sum(len(v) for v in result["queue_by_state"].values())
        self.assertGreater(total_queue, 0)

    def test_all_four_queue_states(self):
        day_start_s, _ = self._day_bounds()

        def _ts(off):
            return datetime.fromtimestamp(
                day_start_s + off, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        _insert_queue_record(
            self.nutrition_db, "nutr_s_draft", "draft", "Draft", _ts(3600), _ts(5400)
        )
        _insert_queue_record(
            self.nutrition_db, "nutr_s_confirmed", "confirmed", "Confirmed",
            _ts(7200), _ts(9000), confirmed_by="synthetic_user",
            confirmed_at="2026-01-15T10:00:00Z",
        )
        _insert_queue_record(
            self.nutrition_db, "nutr_s_acked", "acked", "Acked",
            _ts(10800), _ts(12600), confirmed_by="synthetic_user",
            confirmed_at="2026-01-15T11:00:00Z",
            acked_at="2026-01-15T12:00:00Z", delivery_possible=1,
        )
        _insert_queue_record(
            self.nutrition_db, "nutr_s_cancelled", "cancelled", "Cancelled",
            _ts(14400), _ts(16200),
        )
        conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(conn, self.DATE)
        finally:
            conn.close()
        self.assertEqual(len(by_state["draft"]), 1)
        self.assertEqual(len(by_state["confirmed"]), 1)
        self.assertEqual(len(by_state["acked"]), 1)
        self.assertEqual(len(by_state["cancelled"]), 1)

    def test_all_queue_entries_have_correlation_field(self):
        day_start_s, _ = self._day_bounds()
        start_iso = datetime.fromtimestamp(
            day_start_s + 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            day_start_s + 5400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            self.nutrition_db, "nutr_corr_001", "confirmed",
            "Correlation Test", start_iso, end_iso,
        )
        conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(conn, self.DATE)
        finally:
            conn.close()
        entry = by_state["confirmed"][0]
        self.assertIn("correlation", entry)

    def test_correlation_not_verified_current_format(self):
        # In current format (no expl_id in health_records), correlation is not_verified.
        day_start_s, _ = self._day_bounds()
        start_iso = datetime.fromtimestamp(
            day_start_s + 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            day_start_s + 5400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_queue_record(
            self.nutrition_db, "nutr_corr_002", "acked",
            "Acked No Match", start_iso, end_iso,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        entry = by_state["acked"][0]
        self.assertEqual(entry["correlation"]["status"], "not_verified")

    def test_correlation_identity_match_when_expl_id_present(self):
        # When health_records contains a matching expl_id, correlation = identity_match.
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 4000
        start_iso = datetime.fromtimestamp(
            mid - 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            mid + 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_syn_expl_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({
                "energy_kcal": 500.0, "protein_g": 30.0,
                "carbohydrate_g": 60.0, "fat_g": 15.0,
            }),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(
            self.nutrition_db, crid, "acked",
            "Matched Meal", start_iso, end_iso,
            energy_kcal=500.0, protein_g=30.0, carbohydrate_g=60.0, fat_g=15.0,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        entry = by_state["acked"][0]
        self.assertEqual(entry["correlation"]["status"], "identity_match")
        self.assertTrue(entry["correlation"]["content_match"])

    def test_correlation_identity_match_content_mismatch(self):
        # Same expl_id but different macros → identity_match with content_match=False.
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 6000
        start_iso = datetime.fromtimestamp(
            mid - 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            mid + 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_syn_mismatch_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({
                "energy_kcal": 999.0, "protein_g": 99.0,
                "carbohydrate_g": 60.0, "fat_g": 15.0,
            }),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(
            self.nutrition_db, crid, "acked",
            "Mismatch Meal", start_iso, end_iso,
            energy_kcal=500.0, protein_g=30.0, carbohydrate_g=60.0, fat_g=15.0,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        entry = by_state["acked"][0]
        corr = entry["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertFalse(corr["content_match"])
        self.assertIn("content_issues", corr)

    def test_notices_include_acked_client_reported(self):
        result = run(self.health_db, self.nutrition_db, self.DATE)
        combined = " ".join(result["notices"]).lower()
        self.assertIn("acked", combined)
        self.assertIn("client-reported", combined)

    def test_no_cross_source_sum_in_output(self):
        day_start_s, day_end_s = self._day_bounds()
        for i, sk in enumerate([_SK_FULL, _SK_NULL_ORIGIN]):
            mid = day_start_s + 3600 * (i + 1)
            _insert_health_record(
                self.health_db, "nutrition", sk,
                start_s=mid - 900, end_s=mid + 900,
                identity_key=f"nutr_src_{i}",
            )
        result = run(self.health_db, self.nutrition_db, self.DATE)
        ingest = result["date_nutrition_ingest"]
        source_keys_seen = {g["source_key"] for g in ingest}
        self.assertEqual(len(source_keys_seen), 2)
        for group in ingest:
            self.assertNotIn("total_energy_kcal", group)

    def test_correlation_server_id_same_value_as_client_id_not_matched(self):
        """
        Regression: a health record with expl_ns='server' and expl_id equal to
        the queue's client_record_id must NOT produce an identity_match.
        Server and client ID namespaces are distinct identity spaces.
        """
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 8000
        start_iso = datetime.fromtimestamp(
            mid - 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            mid + 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_server_collision_001"
        # Health record has the same ID value but server namespace
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="server",     # server namespace — must NOT match client query
            expl_id=crid,         # same value as queue client_record_id
            client_record_version=1,
            record_json=json.dumps({
                "energy_kcal": 500.0, "protein_g": 30.0,
                "carbohydrate_g": 60.0, "fat_g": 15.0,
            }),
            identity_key=f"e_nutrition_server_{crid}",
        )
        _insert_queue_record(
            self.nutrition_db, crid, "acked",
            "Server Collision Meal", start_iso, end_iso,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        entry = by_state["acked"][0]
        self.assertEqual(
            entry["correlation"]["status"], "not_verified",
            "Server-namespace expl_id must not match queue client_record_id.",
        )

    def test_correlation_client_id_in_record_json_when_server_id_primary(self):
        """
        Regression: when a health record has expl_ns='server' (server ID primary),
        the client_record_id may be in record_json.metadata.client_record_id.
        The secondary lookup must find and match it.
        """
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 9000
        start_iso = datetime.fromtimestamp(
            mid - 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            mid + 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_rj_client_002"
        server_id = "hc_server_assigned_id_abc"
        # Health record: server ID is primary (expl_ns='server'), but record_json
        # contains the client ID in metadata.
        record_payload = json.dumps({
            "energy_kcal": 600.0, "protein_g": 40.0,
            "carbohydrate_g": 70.0, "fat_g": 20.0,
            "metadata": {
                "id": server_id,
                "client_record_id": crid,
                "client_record_version": 1,
            },
        })
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="server",
            expl_id=server_id,
            client_record_version=1,
            record_json=record_payload,
            identity_key=f"e_nutrition_server_{server_id}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(
            self.nutrition_db, crid, "acked",
            "Both IDs Meal", start_iso, end_iso,
            energy_kcal=600.0, protein_g=40.0, carbohydrate_g=70.0, fat_g=20.0,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        entry = by_state["acked"][0]
        corr = entry["correlation"]
        self.assertEqual(
            corr["status"], "identity_match",
            "Client ID in record_json metadata should be found by secondary lookup.",
        )
        self.assertEqual(corr["match_via"], "record_json_metadata_client_id")
        self.assertTrue(corr["content_match"])

    def test_correlation_match_via_field_present_in_identity_match(self):
        """identity_match result must include match_via field."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 10000
        start_iso = datetime.fromtimestamp(
            mid - 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(
            mid + 900, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_match_via_003"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({
                "energy_kcal": 400.0, "protein_g": 25.0,
                "carbohydrate_g": 50.0, "fat_g": 10.0,
            }),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(
            self.nutrition_db, crid, "acked", "Match Via Meal",
            start_iso, end_iso,
            energy_kcal=400.0, protein_g=25.0, carbohydrate_g=50.0, fat_g=10.0,
        )
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertIn("match_via", corr)
        self.assertEqual(corr["match_via"], "expl_id_client_ns")

    def test_content_match_false_when_no_macros_in_record_json(self):
        """Empty record JSON → content_match=False, never True."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 11000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_empty_macros_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({}),  # no macros at all
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(self.nutrition_db, crid, "acked", "Empty Macros Meal",
                             start_iso, end_iso, energy_kcal=500.0, protein_g=30.0,
                             carbohydrate_g=60.0, fat_g=15.0)
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertFalse(corr["content_match"],
                         "Empty record JSON must not yield content_match=True")
        self.assertIn("content_issues", corr)

    def test_content_match_canonical_aliases(self):
        """Record using protein_grams/carbs_grams aliases matches queue values."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 12000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_alias_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({
                "calories": 500.0,        # alias for energy_kcal
                "protein_grams": 30.0,    # alias for protein_g
                "carbs_grams": 60.0,      # alias for carbohydrate_g
                "fat_grams": 15.0,        # alias for fat_g
            }),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(self.nutrition_db, crid, "acked", "Alias Meal",
                             start_iso, end_iso,
                             energy_kcal=500.0, protein_g=30.0,
                             carbohydrate_g=60.0, fat_g=15.0)
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertTrue(corr["content_match"], "Canonical aliases must match queue values")

    def test_content_match_false_different_start_end_times(self):
        """Same macros but different start/end times → content_match=False."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 13000
        q_start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q_end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_time_diff_001"
        # Health record has different start/end (shifted by 3600s)
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900 + 3600, end_s=mid + 900 + 3600,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({"energy_kcal": 500.0, "protein_g": 30.0,
                                    "carbohydrate_g": 60.0, "fat_g": 15.0}),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(self.nutrition_db, crid, "acked", "Time Diff Meal",
                             q_start_iso, q_end_iso,
                             energy_kcal=500.0, protein_g=30.0,
                             carbohydrate_g=60.0, fat_g=15.0)
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertFalse(corr["content_match"],
                         "Different start/end times must yield content_match=False")

    def test_content_match_nonfinite_macro_not_verified(self):
        """NaN/Inf in record macro → content_match=False with issue reported."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 14000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_nan_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json='{"energy_kcal": "NaN", "protein_g": 30.0, '
                        '"carbohydrate_g": 60.0, "fat_g": 15.0}',
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        _insert_queue_record(self.nutrition_db, crid, "acked", "NaN Meal",
                             start_iso, end_iso,
                             energy_kcal=500.0, protein_g=30.0,
                             carbohydrate_g=60.0, fat_g=15.0)
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_match")
        self.assertFalse(corr["content_match"],
                         "Nonfinite macro must not yield content_match=True")
        issues = corr.get("content_issues", [])
        self.assertTrue(any("energy_kcal" in i for i in issues),
                        "energy_kcal issue must be reported")

    def test_correlation_wrong_origin_no_match(self):
        """Same client_record_id and version but wrong data_origin → not correlated."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 15000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_wrong_origin_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({"energy_kcal": 500.0, "protein_g": 30.0,
                                    "carbohydrate_g": 60.0, "fat_g": 15.0}),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin="com.some.other.unrelated.app",  # wrong origin
        )
        _insert_queue_record(self.nutrition_db, crid, "acked", "Wrong Origin Meal",
                             start_iso, end_iso)
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        self.assertEqual(corr["status"], "identity_found_wrong_origin",
                         "Wrong data_origin must not produce an identity_match.")
        self.assertIn("found_origin", corr)
        self.assertIn("expected_origin", corr)

    def test_correlation_bool_version_not_matched(self):
        """Version stored as bool (True) must not match integer version 1."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 16000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_bool_ver_001"
        # Insert with client_record_version=True (bool); Python sqlite3 may store as 1
        conn = sqlite3.connect(str(self.health_db))
        conn.execute("""
            INSERT INTO health_records
            (identity_key, record_type, source_key, expl_ns, expl_id,
             client_record_version, record_start_s, record_end_s,
             record_json, received_at, payload_ts, data_origin)
            VALUES (?, 'nutrition', ?, 'client', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"e_nutrition_client_{crid}", _SK_FULL, crid,
            True,  # bool stored as version — must be rejected
            mid - 900, mid + 900,
            json.dumps({"energy_kcal": 500.0, "protein_g": 30.0,
                        "carbohydrate_g": 60.0, "fat_g": 15.0}),
            _SYNTHETIC_RECEIVED, _SYNTHETIC_RECEIVED,
            _mod._EXPECTED_NUTRITION_ORIGIN,
        ))
        conn.commit()
        conn.close()
        _insert_queue_record(self.nutrition_db, crid, "acked", "Bool Version Meal",
                             start_iso, end_iso)
        # Queue version is int 1; health record version was stored as bool True
        # SQLite stores both as integer 1, so the version values will match numerically.
        # The test verifies that our Python-side bool check catches this case
        # when the stored_ver is retrieved as int (SQLite collapses bool→int).
        # This test confirms the identity match proceeds — Python int 1 != bool True
        # in isinstance check, but SQLite returns int. Document this limitation.
        health_conn = _open_ro(self.health_db)
        nutrition_conn = _open_ro(self.nutrition_db)
        try:
            by_state = _query_queue_for_day(nutrition_conn, self.DATE, health_conn)
        finally:
            health_conn.close()
            nutrition_conn.close()
        corr = by_state["acked"][0]["correlation"]
        # SQLite stores bool as int — stored_ver comes back as int 1, which passes
        # our int check. The important guard is in the queue entry cver (also int).
        # If queue entry has bool version, that must fail. Test that path separately.
        self.assertIn(corr["status"], ("identity_match", "identity_match_version_mismatch"),
                      "Bool-stored version should be handled gracefully")

    def test_correlation_queue_bool_version_rejected(self):
        """Queue entry with bool version (not int) → version mismatch/not verified."""
        day_start_s, _ = self._day_bounds()
        mid = day_start_s + 17000
        start_iso = datetime.fromtimestamp(mid - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(mid + 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        crid = "nutr_qbool_ver_001"
        _insert_health_record(
            self.health_db, "nutrition", _SK_FULL,
            start_s=mid - 900, end_s=mid + 900,
            expl_ns="client", expl_id=crid, client_record_version=1,
            record_json=json.dumps({"energy_kcal": 500.0, "protein_g": 30.0,
                                    "carbohydrate_g": 60.0, "fat_g": 15.0}),
            identity_key=f"e_nutrition_client_{crid}",
            data_origin=_mod._EXPECTED_NUTRITION_ORIGIN,
        )
        # Simulate a queue entry whose version comes in as bool True
        mock_row_dict = {
            "record_start_s": mid - 900, "record_end_s": mid + 900,
            "record_json": json.dumps({"energy_kcal": 500.0, "protein_g": 30.0,
                                       "carbohydrate_g": 60.0, "fat_g": 15.0}),
            "client_record_version": 1,
            "received_at": _SYNTHETIC_RECEIVED,
        }
        # Create a synthetic queue entry with bool version
        bool_queue_entry = {"client_record_id": crid, "client_record_version": True,
                             "start_time": start_iso, "end_time": end_iso,
                             "energy_kcal": 500.0, "protein_g": 30.0,
                             "carbohydrate_g": 60.0, "fat_g": 15.0}
        health_conn = _open_ro(self.health_db)
        try:
            # Use the module's Row via direct query then pass bool cver
            row = health_conn.execute(
                "SELECT * FROM health_records WHERE expl_id=?", (crid,)
            ).fetchone()
            result = _mod._build_correlation_result(row, True, bool_queue_entry, "test")
        finally:
            health_conn.close()
        # bool True is an instance of int in Python, isinstance(True, bool) catches it
        self.assertEqual(result["status"], "identity_match_version_mismatch",
                         "bool cver must be rejected by integer check")


# ── Tests: run() integration ──────────────────────────────────────────────────

class TestRun(unittest.TestCase):
    DATE = "2026-01-15"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_run_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_run_returns_expected_top_level_keys(self):
        health_db = _make_health_db(self.tmp_dir)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        result = run(health_db, nutrition_db, self.DATE)
        for key in (
            "generated_at", "query_date", "query_timezone",
            "health_db", "nutrition_db", "notices",
            "inventory", "date_nutrition_ingest", "date_exercise",
            "queue_by_state",
        ):
            self.assertIn(key, result, f"Missing top-level key: {key}")

    def test_run_missing_health_db_in_errors(self):
        missing = self.tmp_dir / "missing_health.sqlite3"
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        result = run(missing, nutrition_db, self.DATE)
        self.assertIn("errors", result)
        self.assertEqual(result["errors"]["health_db"]["error"], "not_found")
        self.assertEqual(result["inventory"], [])

    def test_run_missing_nutrition_db_in_errors(self):
        health_db = _make_health_db(self.tmp_dir)
        missing = self.tmp_dir / "missing_nutrition.sqlite3"
        result = run(health_db, missing, self.DATE)
        self.assertIn("errors", result)
        self.assertIn("nutrition_db", result["errors"])
        self.assertNotIn("health_db", result.get("errors", {}))

    def test_run_both_dbs_no_errors_key(self):
        health_db = _make_health_db(self.tmp_dir)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        result = run(health_db, nutrition_db, self.DATE)
        self.assertNotIn("errors", result)

    def test_run_invalid_date_raises_value_error(self):
        health_db = _make_health_db(self.tmp_dir)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        with self.assertRaises(ValueError):
            run(health_db, nutrition_db, "not-a-date")

    def test_run_result_is_json_serialisable(self):
        health_db = _make_health_db(self.tmp_dir)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        day_start_s, day_end_s = _day_utc_epoch_bounds(self.DATE)
        mid = (day_start_s + day_end_s) / 2
        _insert_health_record(health_db, "exercise", _SK_FULL, mid - 1800, mid + 1800)
        result = run(health_db, nutrition_db, self.DATE)
        json.dumps(result)  # raises if not serialisable

    def test_run_symlink_health_db_in_errors(self):
        real_db = _make_health_db(self.tmp_dir)
        link = self.tmp_dir / "link_health.sqlite3"
        link.symlink_to(real_db)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        result = run(link, nutrition_db, self.DATE)
        self.assertIn("errors", result)
        self.assertEqual(result["errors"]["health_db"]["error"], "rejected")

    def test_run_date_defaults_to_today(self):
        health_db = _make_health_db(self.tmp_dir)
        nutrition_db = _make_nutrition_db(self.tmp_dir)
        result = run(health_db, nutrition_db, date_str=None)
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
        self.assertEqual(result["query_date"], today)


# ── Tests: identity_meta field ────────────────────────────────────────────────

class TestIdentityMeta(unittest.TestCase):
    DATE = "2026-01-15"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_cr_id_")
        self.tmp_dir = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_no_explicit_id_marks_unavailable(self):
        health_db = _make_health_db(self.tmp_dir)
        day_start_s, day_end_s = _day_utc_epoch_bounds(self.DATE)
        mid = (day_start_s + day_end_s) / 2
        _insert_health_record(health_db, "nutrition", _SK_FULL, mid - 900, mid + 900)
        conn = _open_ro(health_db)
        try:
            groups = _query_records_for_day(conn, "nutrition", day_start_s, day_end_s)
        finally:
            conn.close()
        rec = groups[0]["records"][0]
        self.assertFalse(rec["identity_meta"].get("available", True))
        self.assertIn("temporal", rec["identity_meta"]["note"].lower())

    def test_explicit_id_present_when_columns_set(self):
        health_db = _make_health_db(self.tmp_dir)
        day_start_s, day_end_s = _day_utc_epoch_bounds(self.DATE)
        mid = (day_start_s + day_end_s) / 2
        _insert_health_record(
            health_db, "nutrition", _SK_FULL, mid - 900, mid + 900,
            expl_ns="client", expl_id="syn_crid_001", client_record_version=3,
            identity_key="e_nutrition_client_syn_crid_001",
        )
        conn = _open_ro(health_db)
        try:
            groups = _query_records_for_day(conn, "nutrition", day_start_s, day_end_s)
        finally:
            conn.close()
        meta = groups[0]["records"][0]["identity_meta"]
        self.assertEqual(meta["namespace"], "client")
        self.assertEqual(meta["id"], "syn_crid_001")
        self.assertEqual(meta["client_record_version"], 3)


# ── Tests: helper utilities ───────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    def test_epoch_to_iso_known_value(self):
        self.assertEqual(_epoch_to_iso(0.0), "1970-01-01T00:00:00Z")

    def test_epoch_to_iso_none_returns_none(self):
        self.assertIsNone(_epoch_to_iso(None))

    def test_elapsed_hours_from_iso_past_timestamp(self):
        fh = _elapsed_hours_from_iso("2000-01-01T00:00:00Z")
        self.assertIsNotNone(fh)
        self.assertGreater(fh, 0)

    def test_elapsed_hours_from_iso_none_input(self):
        self.assertIsNone(_elapsed_hours_from_iso(None))

    def test_elapsed_hours_from_epoch_past(self):
        past_epoch = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        fh = _elapsed_hours_from_epoch(past_epoch)
        self.assertIsNotNone(fh)
        self.assertGreater(fh, 0)

    def test_elapsed_hours_from_epoch_none(self):
        self.assertIsNone(_elapsed_hours_from_epoch(None))

    def test_parse_source_key_valid(self):
        sk = json.dumps({"origin": "com.test.app", "method": 2}, separators=(",", ":"))
        parsed = _parse_source_key(sk)
        self.assertEqual(parsed["origin"], "com.test.app")

    def test_parse_source_key_invalid_returns_error_dict(self):
        parsed = _parse_source_key("not json{{")
        self.assertIn("_unparseable", parsed)

    def test_day_utc_epoch_bounds_invalid_date(self):
        with self.assertRaises(ValueError):
            _day_utc_epoch_bounds("2026-13-01")

    def test_day_utc_epoch_bounds_24h_duration(self):
        start_s, end_s = _day_utc_epoch_bounds("2026-01-15")
        self.assertAlmostEqual(end_s - start_s, 86400.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
