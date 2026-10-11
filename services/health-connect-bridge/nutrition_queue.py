"""
SQLite-backed nutrition queue for confirmed write-back to Health Connect.

Security / integrity properties:
- Separate DB file, typed by application_id; refuses to open health ingest DB.
- Permissions created 0600/0700 only for files we create; no chmod on foreign files.
- client_record_id: random token_urlsafe(32), immutable.
- client_record_version: monotonic positive; increments on every edit; starts at 1.
- All mutations use BEGIN IMMEDIATE before SELECT: atomic across separate processes.
- CAS WHERE clause on version ensures exactly-once mutation even under races.
- Edit resets state→draft, clears confirmation; corrections allowed after ack.
- delivery_possible=1 is set once fetched or acked; never reset; preserved across edits.
- Description is ALWAYS stored with server-controlled "[AI estimate]" prefix (never null).
- Only four required nutrients; optional nutrients (fiber/sugar/sodium) are omitted.
- Strict ISO8601 timestamp format with T, seconds, explicit tz; absolute date bounds.
- No PHI in log messages or error strings.

Required nutrients only: energy_kcal, protein_g, carbohydrate_g, fat_g.
Optional nutrients (fiber, sugar, sodium) are not accepted — omit unknown values entirely.

Validation upper bounds (documented realistic maxima per meal entry):
  energy_kcal:    [0, 5000]   5000 kcal extreme single-meal upper bound
  protein_g:      [0, 500]    500 g protein extreme athlete dose
  carbohydrate_g: [0, 1000]   1000 g carbs extreme
  fat_g:          [0, 500]    500 g fat extreme
  duration:       (0, 86400]  end strictly after start; max 24 h

Absolute timestamp bounds:
  Not before 2000-01-01T00:00:00Z.
  Not more than 7 days in the future at validation time.
"""

import logging
import math
import os
import re
import secrets
import sqlite3
import stat as _stat_mod
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_SCHEMA_VERSION = 2
_NUTRITION_APP_ID = 0x4E555452    # 'NUTR' in ASCII; identifies nutrition DB

AI_DESCRIPTION_PREFIX = "[AI estimate]"
CONFIRM_AFFIRM_PHRASE = "I confirm this meal for Health Connect"

MAX_KCAL = 5000.0
MAX_PROTEIN_G = 500.0
MAX_CARB_G = 1000.0
MAX_FAT_G = 500.0

MAX_NAME_LEN = 200
MAX_DESCRIPTION_BODY_LEN = 2000   # excludes the server-controlled prefix
MAX_DURATION_SECONDS = 86400      # 24 hours

# Absolute epoch bounds
_MIN_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
_MAX_FUTURE_OFFSET = 7 * 86400    # 7 days ahead

MAX_QUEUE_RETURN = 50
MAX_ACK_BODY = 1024

STATE_DRAFT = "draft"
STATE_CONFIRMED = "confirmed"
STATE_CANCELLED = "cancelled"
STATE_ACKED = "acked"

_VALID_STATES = frozenset([STATE_DRAFT, STATE_CONFIRMED, STATE_CANCELLED, STATE_ACKED])

# Strict RFC3339 / ISO8601 extended form:
#   YYYY-MM-DDTHH:MM:SS[.frac](Z|+HH:MM|-HH:MM)
_TS_STRICT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

# ── Exceptions ────────────────────────────────────────────────────────────────

class VersionConflict(Exception):
    """Supplied version does not match stored version (optimistic concurrency)."""

class NotFound(Exception):
    """No record with the given client_record_id."""

class InvalidState(Exception):
    """Operation not valid in the record's current state."""

class ValidationError(Exception):
    """Input data failed validation."""

class WrongDatabase(Exception):
    """Path does not point to a nutrition queue database."""


# ── Schema column sets for identity verification ──────────────────────────────

# All columns required in a correct v2 nutrition_queue table.
# Must be verified BEFORE any mutable pragma or migration.
_REQUIRED_V2_COLUMNS = frozenset({
    "id", "client_record_id", "client_record_version", "state",
    "name", "start_time", "end_time",
    "energy_kcal", "protein_g", "carbohydrate_g", "fat_g",
    "description", "delivery_possible", "created_at", "updated_at",
    # nullable structural columns present since v2
    "confirmed_by", "confirmed_at", "acked_at", "fetched_at",
})

# Minimum columns required in a v1 nutrition_queue table before migration.
# v1 lacks fetched_at and delivery_possible; everything else must be present.
_REQUIRED_V1_COLUMNS = frozenset({
    "id", "client_record_id", "client_record_version", "state",
    "name", "start_time", "end_time",
    "energy_kcal", "protein_g", "carbohydrate_g", "fat_g",
    "description", "created_at", "updated_at",
    "confirmed_by", "confirmed_at", "acked_at",
})

# ── Exact column specifications (type/notnull/dflt_value/pk match PRAGMA table_info) ──

_V2_COLUMN_EXACT = {
    "id":                    {"type": "INTEGER", "notnull": 0, "dflt_value": None,      "pk": 1},
    "client_record_id":      {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "client_record_version": {"type": "INTEGER", "notnull": 1, "dflt_value": "1",       "pk": 0},
    "state":                 {"type": "TEXT",    "notnull": 1, "dflt_value": "'draft'", "pk": 0},
    "name":                  {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "start_time":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "end_time":              {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "energy_kcal":           {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "protein_g":             {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "carbohydrate_g":        {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "fat_g":                 {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "description":           {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "confirmed_by":          {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "confirmed_at":          {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "acked_at":              {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "fetched_at":            {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "delivery_possible":     {"type": "INTEGER", "notnull": 1, "dflt_value": "0",       "pk": 0},
    "created_at":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "updated_at":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
}

# v1 required columns: same spec as v2 for shared columns; no delivery_possible/fetched_at
_V1_COLUMN_EXACT = {
    "id":                    {"type": "INTEGER", "notnull": 0, "dflt_value": None,      "pk": 1},
    "client_record_id":      {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "client_record_version": {"type": "INTEGER", "notnull": 1, "dflt_value": "1",       "pk": 0},
    "state":                 {"type": "TEXT",    "notnull": 1, "dflt_value": "'draft'", "pk": 0},
    "name":                  {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "start_time":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "end_time":              {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "energy_kcal":           {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "protein_g":             {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "carbohydrate_g":        {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "fat_g":                 {"type": "REAL",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "description":           {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "confirmed_by":          {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "confirmed_at":          {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "acked_at":              {"type": "TEXT",    "notnull": 0, "dflt_value": None,      "pk": 0},
    "created_at":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
    "updated_at":            {"type": "TEXT",    "notnull": 1, "dflt_value": None,      "pk": 0},
}

# Legacy nutrient columns allowed in v1 (and migrated v2) DBs — REAL nullable
_V1_LEGACY_NUTRIENT_COLUMNS = frozenset({"fiber_g", "sugar_g", "sodium_mg"})

# Named indexes allowed per schema version
_ALLOWED_NAMED_INDEXES_V1 = frozenset({"idx_nq_state", "idx_nq_crid"})
_ALLOWED_NAMED_INDEXES_V2 = frozenset({"idx_nq_state", "idx_nq_crid", "idx_nq_conf"})

# Expected structural definition for each named index
_EXPECTED_INDEX_STRUCTURE = {
    "idx_nq_state": {"unique": 0, "partial": 0, "columns": ["state"]},
    "idx_nq_crid":  {"unique": 0, "partial": 0, "columns": ["client_record_id"]},
    "idx_nq_conf":  {
        "unique": 0, "partial": 1, "columns": ["state", "created_at"],
        # Anchored to end-of-string: reject any suffix like OR 1=1 or OR state='draft'
        "where_re": re.compile(r"WHERE\s+state\s*=\s*'confirmed'\s*$", re.IGNORECASE),
    },
}


# ── Schema (individual statements — never use executescript inside BEGIN IMMEDIATE) ──

_DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS nutrition_schema_version (
        version INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS nutrition_queue (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        client_record_id      TEXT    NOT NULL UNIQUE,
        client_record_version INTEGER NOT NULL DEFAULT 1,
        state                 TEXT    NOT NULL DEFAULT 'draft',
        name                  TEXT    NOT NULL,
        start_time            TEXT    NOT NULL,
        end_time              TEXT    NOT NULL,
        energy_kcal           REAL    NOT NULL,
        protein_g             REAL    NOT NULL,
        carbohydrate_g        REAL    NOT NULL,
        fat_g                 REAL    NOT NULL,
        description           TEXT    NOT NULL,
        confirmed_by          TEXT,
        confirmed_at          TEXT,
        acked_at              TEXT,
        fetched_at            TEXT,
        delivery_possible     INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT    NOT NULL,
        updated_at            TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nq_state ON nutrition_queue (state)",
    "CREATE INDEX IF NOT EXISTS idx_nq_crid  ON nutrition_queue (client_record_id)",
    "CREATE INDEX IF NOT EXISTS idx_nq_conf  ON nutrition_queue (state, created_at) WHERE state = 'confirmed'",
]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _check_path_security(db_path: Path) -> None:
    """
    Security pre-checks on db_path before any sqlite3.connect() or chmod.

    Verifies:
    - db_path is not a symlink
    - ALL ancestors of db_path are not symlinks (not just direct parent)
    - If db_path exists:
        - is a regular file (not device, FIFO, socket, etc.)
        - has hardlink count == 1 (no hardlinks to foreign inodes)
        - owned by current uid
        - mode is exactly 0600
    - Does NOT modify any existing file or directory permissions

    Raises RuntimeError for any security violation.
    Must be called BEFORE any sqlite3.connect() or pragma.
    """
    if db_path.is_symlink():
        raise RuntimeError("DB path must not be a symlink")

    # Check ALL ancestors for symlinks (not just the direct parent)
    ancestor = db_path.parent
    while True:
        if ancestor.is_symlink():
            raise RuntimeError("DB ancestor directory must not be a symlink")
        parent_of_ancestor = ancestor.parent
        if parent_of_ancestor == ancestor:
            break  # reached filesystem root
        ancestor = parent_of_ancestor

    if db_path.exists():
        # Use lstat (doesn't follow symlinks) even though we verified it's not a symlink;
        # this guards against TOCTOU between is_symlink() and stat().
        st = os.lstat(str(db_path))
        if not _stat_mod.S_ISREG(st.st_mode):
            raise RuntimeError("DB path must be a regular file")
        if st.st_nlink > 1:
            raise RuntimeError(
                "DB file has multiple hard links (nlink > 1) — refusing to open"
            )
        if st.st_uid != os.getuid():
            raise RuntimeError(
                "existing DB file not owned by process uid — refusing to open"
            )
        mode = _stat_mod.S_IMODE(st.st_mode)
        if mode != 0o600:
            raise RuntimeError(
                f"existing DB file has insecure permissions {oct(mode)} — "
                "expected 0600; refusing to open"
            )


def _create_db_file_if_absent(db_path: Path) -> None:
    """
    Create db_path and any missing parent directories with strict permissions.
    Only chmods directories and files that WE create here.
    Never touches existing files or existing directories.
    """
    parent = db_path.parent
    # Create missing parents one level at a time so we only chmod new ones
    if not parent.exists():
        # Build list of parts that need creating (bottom-up)
        to_create = []
        p = parent
        while not p.exists():
            to_create.append(p)
            p = p.parent
        # Create top-down
        for d in reversed(to_create):
            d.mkdir(mode=0o700, exist_ok=True)
            try:
                os.chmod(str(d), 0o700)
            except OSError:
                pass

    if not db_path.exists():
        fd = os.open(str(db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)


def _validate_column_specs_exact(
    actual_specs: dict, expected: dict, version_label: str
) -> None:
    """Raise WrongDatabase if any expected column has wrong type or constraint."""
    for col, exp in expected.items():
        if col not in actual_specs:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: nutrition_queue missing column {col!r}"
            )
        act = actual_specs[col]
        for field in ("type", "notnull", "dflt_value", "pk"):
            if act[field] != exp[field]:
                raise WrongDatabase(
                    f"nutrition DB v{version_label}: column {col!r} wrong {field} "
                    f"(expected {exp[field]!r}, got {act[field]!r})"
                )


def _validate_named_indexes_exact(
    conn: sqlite3.Connection,
    user_indexes: dict,
    allowed_names: frozenset,
    version_label: str,
) -> None:
    """Raise WrongDatabase for any unknown or structurally wrong named index."""
    if not user_indexes:
        return
    idx_list_rows = conn.execute("PRAGMA index_list('nutrition_queue')").fetchall()
    idx_props = {r[1]: r for r in idx_list_rows}
    for idx_name, idx_sql in user_indexes.items():
        if idx_name not in allowed_names:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: unknown index {idx_name!r} on nutrition_queue"
            )
        exp = _EXPECTED_INDEX_STRUCTURE[idx_name]
        row = idx_props.get(idx_name)
        if row is None:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: index {idx_name!r} in sqlite_master "
                "but absent from index_list"
            )
        if row[2] != exp["unique"]:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: index {idx_name!r} wrong unique "
                f"({row[2]!r} vs expected {exp['unique']!r})"
            )
        if row[4] != exp["partial"]:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: index {idx_name!r} wrong partial "
                f"({row[4]!r} vs expected {exp['partial']!r})"
            )
        cols = [r[2] for r in conn.execute(
            f"PRAGMA index_info('{idx_name}')"
        ).fetchall()]
        if cols != exp["columns"]:
            raise WrongDatabase(
                f"nutrition DB v{version_label}: index {idx_name!r} wrong columns "
                f"({cols!r} vs expected {exp['columns']!r})"
            )
        if exp.get("partial") and exp.get("where_re"):
            if not exp["where_re"].search(idx_sql or ""):
                raise WrongDatabase(
                    f"nutrition DB v{version_label}: index {idx_name!r} has wrong WHERE clause"
                )


def _validate_named_indexes_xinfo(
    conn: sqlite3.Connection,
    user_indexes: dict,
    version_label: str,
) -> None:
    """
    Reject named indexes with DESC ordering, expression columns, or non-BINARY collation.

    Uses PRAGMA index_xinfo which returns:
      (seqno, cid, name, desc, coll, key)
    - cid == -2: expression index column
    - desc != 0: descending order
    - coll != 'BINARY': non-default collation
    - key == 0: rowid sentinel row (skip)
    """
    for idx_name in user_indexes:
        xinfo_rows = conn.execute(f"PRAGMA index_xinfo('{idx_name}')").fetchall()
        for xrow in xinfo_rows:
            # (seqno, cid, name, desc, coll, key)
            if xrow[5] == 0:
                continue  # rowid sentinel, not a key column
            cid, name, desc, coll = xrow[1], xrow[2], xrow[3], xrow[4]
            if cid == -2:
                raise WrongDatabase(
                    f"nutrition DB v{version_label}: index {idx_name!r} "
                    "has expression column — not allowed"
                )
            if desc != 0:
                raise WrongDatabase(
                    f"nutrition DB v{version_label}: index {idx_name!r} "
                    f"column {name!r} has DESC ordering — not allowed"
                )
            if coll != "BINARY":
                raise WrongDatabase(
                    f"nutrition DB v{version_label}: index {idx_name!r} "
                    f"column {name!r} has non-BINARY collation {coll!r} — not allowed"
                )


def _check_db_identity_readonly(conn: sqlite3.Connection) -> None:
    """
    Read-only identity checks run BEFORE any mutable pragma or migration.

    Verifies:
    - application_id is 0 (fresh empty DB) or _NUTRITION_APP_ID (nutrition DB)
    - application_id == 0 is ONLY accepted when there are NO tables (truly fresh file)
    - application_id == _NUTRITION_APP_ID requires nutrition_schema_version present
      (the app_id and schema tables are always written atomically; if app_id is set
      without schema tables this indicates a forged or externally-manipulated file)
    - No views, triggers, or extra tables beyond {nutrition_schema_version, nutrition_queue}
    - Does not contain health_records table
    - nutrition_schema_version has exactly one positive integer row
    - Stored version does not exceed _SCHEMA_VERSION
    - nutrition_queue must be present for any known version (v1 or v2)
    - Exact column specs (type, notnull, dflt_value, pk) per version
    - No extra columns (v2: only legacy nutrient extras; v1: only legacy nutrient extras)
    - UNIQUE constraint present on client_record_id
    - No CHECK constraints on nutrition_queue
    - All named indexes have expected definitions; no unknown named indexes

    Uses only PRAGMA application_id, PRAGMA table_info, PRAGMA index_list,
    PRAGMA index_info, and SELECT from sqlite_master (all read-only).
    Raises WrongDatabase if any check fails.
    """
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]

    schema_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master"
    ).fetchall()
    tables   = {r[1] for r in schema_rows if r[0] == 'table'}
    views    = {r[1] for r in schema_rows if r[0] == 'view'}
    triggers = {r[1] for r in schema_rows if r[0] == 'trigger'}
    # User-created named indexes (SQLite auto-indexes start with 'sqlite_')
    user_indexes = {r[1]: r[2] for r in schema_rows
                    if r[0] == 'index' and not r[1].startswith('sqlite_')}

    if app_id == 0:
        # Accepted ONLY for a truly fresh/empty file with NO schema objects at all.
        if tables or views or triggers:
            raise WrongDatabase(
                "database has application_id=0 with existing schema objects — "
                "this appears to be an unrelated database, not a fresh nutrition queue"
            )
        return  # fresh empty file — safe to initialise

    if app_id != _NUTRITION_APP_ID:
        raise WrongDatabase(
            "database has unexpected application_id; refusing to open as nutrition queue DB"
        )

    # Known nutrition app_id — full schema checks.
    if views:
        raise WrongDatabase(
            f"database contains unexpected views: {sorted(views)}"
        )
    if triggers:
        raise WrongDatabase(
            f"database contains unexpected triggers: {sorted(triggers)}"
        )

    if "health_records" in tables:
        raise WrongDatabase(
            "database contains health_records table — "
            "this appears to be the health ingest DB, not the nutrition queue DB"
        )

    if "nutrition_schema_version" not in tables:
        raise WrongDatabase(
            "database has nutrition application_id but is missing "
            "nutrition_schema_version table — not a valid nutrition queue database; "
            "refusing to open"
        )

    # Validate nutrition_schema_version table structure: exactly one column
    # (version INTEGER NOT NULL, no default, no extras)
    sv_cols = conn.execute("PRAGMA table_info('nutrition_schema_version')").fetchall()
    if len(sv_cols) != 1:
        raise WrongDatabase(
            f"nutrition_schema_version must have exactly one column, "
            f"got {len(sv_cols)}: {[r[1] for r in sv_cols]}"
        )
    sv_col = sv_cols[0]  # (cid, name, type, notnull, dflt_value, pk)
    if sv_col[1] != "version" or sv_col[2] != "INTEGER" or sv_col[3] != 1 or sv_col[4] is not None:
        raise WrongDatabase(
            f"nutrition_schema_version column has unexpected definition: "
            f"name={sv_col[1]!r} type={sv_col[2]!r} notnull={sv_col[3]} dflt={sv_col[4]!r}"
        )

    # Detect hidden/generated columns in nutrition_schema_version via table_xinfo
    sv_xinfo_rows = conn.execute("PRAGMA table_xinfo('nutrition_schema_version')").fetchall()
    for xrow in sv_xinfo_rows:
        if xrow[6] != 0:
            raise WrongDatabase(
                f"nutrition_schema_version has hidden/generated column {xrow[1]!r} "
                f"(hidden={xrow[6]})"
            )

    # Structural SQL checks on nutrition_schema_version
    sv_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='nutrition_schema_version'"
    ).fetchone()
    if sv_sql_row and sv_sql_row[0]:
        sv_sql = sv_sql_row[0]
        if re.search(r'\bCOLLATE\b', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version has unexpected COLLATE on column"
            )
        if re.search(r'\bCHECK\b', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version has unexpected CHECK constraint"
            )
        if re.search(r'\bFOREIGN\s+KEY\b|\bREFERENCES\b', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version has unexpected FOREIGN KEY constraint"
            )
        if re.search(r'\bGENERATED\b|\bAS\s*\(', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version has unexpected generated column syntax"
            )
        if re.search(r'\bSTRICT\b', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version uses STRICT mode"
            )
        if re.search(r'\bWITHOUT\s+ROWID\b', sv_sql, re.IGNORECASE):
            raise WrongDatabase(
                "nutrition_schema_version uses WITHOUT ROWID"
            )

    # No indexes (including UNIQUE autoindexes) allowed on nutrition_schema_version
    sv_idx_list = conn.execute("PRAGMA index_list('nutrition_schema_version')").fetchall()
    if sv_idx_list:
        raise WrongDatabase(
            f"nutrition_schema_version has unexpected indexes: "
            f"{sorted(r[1] for r in sv_idx_list)}"
        )

    rows = conn.execute(
        "SELECT version FROM nutrition_schema_version"
    ).fetchall()
    if len(rows) == 0:
        raise WrongDatabase(
            "nutrition_schema_version has no rows — refusing to open"
        )
    if len(rows) > 1:
        raise WrongDatabase(
            "nutrition_schema_version has multiple rows — ambiguous schema version"
        )
    version = rows[0][0]
    if not isinstance(version, int) or version <= 0:
        raise WrongDatabase(
            f"nutrition schema version {version!r} is not a positive integer"
        )
    if version > _SCHEMA_VERSION:
        raise WrongDatabase(
            f"nutrition DB has schema version {version}, "
            f"but this code only understands up to version {_SCHEMA_VERSION}; "
            "upgrade the server before opening this database"
        )

    if "nutrition_queue" not in tables:
        raise WrongDatabase(
            f"nutrition DB claims schema version {version} but is missing "
            "nutrition_queue table — refusing to open"
        )

    # No extra tables beyond the two canonical ones (SQLite system tables like
    # sqlite_sequence are allowed — SQLite creates them for AUTOINCREMENT tracking)
    _ALLOWED_TABLES = frozenset({"nutrition_schema_version", "nutrition_queue"})
    extra_tables = {t for t in tables - _ALLOWED_TABLES if not t.startswith("sqlite_")}
    if extra_tables:
        raise WrongDatabase(
            f"database contains unexpected tables: {sorted(extra_tables)}"
        )

    # Detect hidden/generated columns via table_xinfo (table_info omits them).
    # hidden != 0 means generated (2=virtual, 3=stored) or hidden column.
    xinfo_rows = conn.execute("PRAGMA table_xinfo(nutrition_queue)").fetchall()
    for xrow in xinfo_rows:
        # (cid, name, type, notnull, dflt_value, pk, hidden)
        if xrow[6] != 0:
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue has hidden/generated "
                f"column {xrow[1]!r} (hidden={xrow[6]})"
            )

    # Full column spec validation (use table_info for standard column metadata)
    col_rows = conn.execute("PRAGMA table_info(nutrition_queue)").fetchall()
    actual_specs = {
        r[1]: {"type": r[2], "notnull": r[3], "dflt_value": r[4], "pk": r[5]}
        for r in col_rows
    }

    if version == _SCHEMA_VERSION:
        _validate_column_specs_exact(actual_specs, _V2_COLUMN_EXACT, "2")
        extra_cols = set(actual_specs) - frozenset(_V2_COLUMN_EXACT) - _V1_LEGACY_NUTRIENT_COLUMNS
        if extra_cols:
            raise WrongDatabase(
                f"nutrition DB v2: nutrition_queue has unexpected columns: {sorted(extra_cols)}"
            )
        allowed_idx = _ALLOWED_NAMED_INDEXES_V2
    elif version == 1:
        _validate_column_specs_exact(actual_specs, _V1_COLUMN_EXACT, "1")
        extra_cols = set(actual_specs) - frozenset(_V1_COLUMN_EXACT) - _V1_LEGACY_NUTRIENT_COLUMNS
        if extra_cols:
            raise WrongDatabase(
                f"nutrition DB v1: nutrition_queue has unexpected columns: {sorted(extra_cols)}"
            )
        allowed_idx = _ALLOWED_NAMED_INDEXES_V1
    else:
        raise WrongDatabase(f"unhandled schema version {version}")

    # Validate legacy nutrient columns: must be REAL and nullable (notnull=0)
    for col in _V1_LEGACY_NUTRIENT_COLUMNS:
        if col in actual_specs:
            act = actual_specs[col]
            if act["type"] != "REAL":
                raise WrongDatabase(
                    f"nutrition DB v{version}: legacy column {col!r} must be REAL, "
                    f"got {act['type']!r}"
                )
            if act["notnull"] != 0:
                raise WrongDatabase(
                    f"nutrition DB v{version}: legacy column {col!r} must be nullable (notnull=0)"
                )

    # No CHECK, FOREIGN KEY, COLLATE, STRICT, or WITHOUT ROWID on nutrition_queue
    tbl_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='nutrition_queue'"
    ).fetchone()
    if tbl_sql_row and tbl_sql_row[0]:
        tbl_sql = tbl_sql_row[0]
        if re.search(r'\bCHECK\b', tbl_sql, re.IGNORECASE):
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue has unexpected CHECK constraint"
            )
        if re.search(r'\bFOREIGN\s+KEY\b|\bREFERENCES\b', tbl_sql, re.IGNORECASE):
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue has unexpected FOREIGN KEY constraint"
            )
        if re.search(r'\bCOLLATE\b', tbl_sql, re.IGNORECASE):
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue has unexpected COLLATE in column definition"
            )
        if re.search(r'\bSTRICT\b', tbl_sql, re.IGNORECASE):
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue uses STRICT mode"
            )
        if re.search(r'\bWITHOUT\s+ROWID\b', tbl_sql, re.IGNORECASE):
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue uses WITHOUT ROWID"
            )

    # UNIQUE constraint on client_record_id must be present, and no other
    # UNIQUE auto-indexes (extra UNIQUE constraints) are allowed.
    idx_list_rows = conn.execute("PRAGMA index_list('nutrition_queue')").fetchall()
    unique_autoindexes = [r for r in idx_list_rows if r[2] == 1 and r[1].startswith('sqlite_')]
    unique_on_crid = False
    for row in unique_autoindexes:
        cols = [c[2] for c in conn.execute(f"PRAGMA index_info('{row[1]}')").fetchall()]
        if cols == ["client_record_id"]:
            unique_on_crid = True
        else:
            raise WrongDatabase(
                f"nutrition DB v{version}: nutrition_queue has unexpected UNIQUE "
                f"constraint on {cols} — only client_record_id may have UNIQUE"
            )
    if not unique_on_crid:
        raise WrongDatabase(
            f"nutrition DB v{version}: nutrition_queue missing UNIQUE constraint "
            "on client_record_id"
        )

    # Named index validation: no unknown indexes, exact structural match,
    # and no DESC ordering, expression columns, or non-BINARY collation.
    _validate_named_indexes_exact(conn, user_indexes, allowed_idx, str(version))
    _validate_named_indexes_xinfo(conn, user_indexes, str(version))


def open_nutrition_rw(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the nutrition queue database in read-write mode.

    Security order:
      1. Check path security (symlinks, ownership, mode) — before any file write
      2. Create file if absent (0600 mode, private parent dirs)
      3. Open sqlite3 with NO pragmas
      4. Check DB identity (read-only: app_id + table names + schema version)
      5. Only then set WAL mode and other mutable pragmas

    Uses isolation_level=None (autocommit) for explicit BEGIN IMMEDIATE transactions.
    """
    _check_path_security(db_path)
    _create_db_file_if_absent(db_path)

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=10.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    # Read-only identity check BEFORE any mutable pragma
    try:
        _check_db_identity_readonly(conn)
    except WrongDatabase:
        conn.close()
        raise

    # Now safe to set WAL mode and other pragmas
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _get_existing_columns(conn: sqlite3.Connection, table: str) -> set:
    """Return set of column names currently in the given table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def init_nutrition_schema(conn: sqlite3.Connection) -> None:
    """
    Create or migrate the nutrition queue schema.  Transactional and idempotent.

    Migration logic (all within BEGIN IMMEDIATE, rolled back on failure):
      - New DB (no version row): create all tables, set app_id, insert version 2.
      - Version 1 DB (missing fetched_at / delivery_possible): ADD COLUMN to
        existing nutrition_queue table preserving all rows, update version to 2.
      - Version 2 DB: verify columns exist, no-op if already current.
      - Version > 2: rejected by _check_db_identity_readonly (already called).

    Does NOT use executescript() — that auto-commits open transactions.
    """
    # Validate the DB state before any mutation (defense-in-depth).
    # open_nutrition_rw already calls this, but init_nutrition_schema may be
    # invoked independently; the read-only check is safe to repeat.
    _check_db_identity_readonly(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Determine current state
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        version_row = None
        if "nutrition_schema_version" in tables:
            version_row = conn.execute(
                "SELECT version FROM nutrition_schema_version LIMIT 1"
            ).fetchone()

        stored_version = version_row[0] if version_row else None

        if stored_version is None:
            # Fresh DB or pre-versioned DB: create full schema
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
            if "nutrition_schema_version" not in tables:
                conn.execute(
                    "INSERT INTO nutrition_schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            else:
                conn.execute(
                    "UPDATE nutrition_schema_version SET version = ?",
                    (_SCHEMA_VERSION,),
                )
        elif stored_version == 1:
            # Legacy v1: missing fetched_at and delivery_possible columns
            existing_cols = _get_existing_columns(conn, "nutrition_queue")
            if "fetched_at" not in existing_cols:
                conn.execute(
                    "ALTER TABLE nutrition_queue ADD COLUMN fetched_at TEXT"
                )
            if "delivery_possible" not in existing_cols:
                conn.execute(
                    "ALTER TABLE nutrition_queue "
                    "ADD COLUMN delivery_possible INTEGER NOT NULL DEFAULT 0"
                )
                # Conservative backfill: acked records were definitively delivered;
                # confirmed records may already have been fetched by the phone in v1
                # (no fetched_at tracking existed) — cannot safely declare not-delivered.
                conn.execute(
                    "UPDATE nutrition_queue SET delivery_possible = 1 "
                    "WHERE state IN ('acked', 'confirmed')"
                )
            # Add new indexes if missing
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nq_conf "
                "ON nutrition_queue (state, created_at) WHERE state = 'confirmed'"
            )
            conn.execute(
                "UPDATE nutrition_schema_version SET version = ?",
                (_SCHEMA_VERSION,),
            )
            # rows are preserved; only schema is extended and delivery_possible backfilled
        else:
            # Version 2 (current): just ensure indexes exist
            for stmt in _DDL_STATEMENTS[2:]:  # index statements only
                conn.execute(stmt)

        conn.execute(f"PRAGMA application_id = {_NUTRITION_APP_ID}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ── Timestamp validation ──────────────────────────────────────────────────────

def _parse_strict_ts(ts: str) -> datetime:
    """
    Parse a strict ISO8601/RFC3339 extended timestamp.

    Accepted forms:
      YYYY-MM-DDTHH:MM:SS[.frac]Z
      YYYY-MM-DDTHH:MM:SS[.frac]+HH:MM
      YYYY-MM-DDTHH:MM:SS[.frac]-HH:MM

    Rejected: space separator, basic format (no hyphens/colons), week dates,
    naive datetimes (no timezone designator), non-finite values.

    Returns an aware UTC datetime.
    Raises ValidationError on any violation.
    """
    if not isinstance(ts, str):
        raise ValidationError("timestamp must be a string")
    s = ts.strip()
    if not _TS_STRICT_RE.match(s):
        raise ValidationError(
            "timestamp must be strict ISO8601 extended with T separator, "
            "seconds, and explicit timezone (Z or ±HH:MM)"
        )
    # Normalise Z suffix for fromisoformat
    s_norm = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s_norm)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"invalid timestamp value: {e}") from e
    if dt.tzinfo is None:
        raise ValidationError("naive timestamp not allowed")
    return dt.astimezone(timezone.utc)


def _validate_ts_bounds(epoch: float, field: str) -> None:
    """Validate epoch is within absolute date bounds."""
    if epoch < _MIN_EPOCH:
        raise ValidationError(f"{field}: must not be before 2000-01-01")
    now = datetime.now(timezone.utc).timestamp()
    if epoch > now + _MAX_FUTURE_OFFSET:
        raise ValidationError(f"{field}: must not be more than 7 days in the future")


# ── Data validation ───────────────────────────────────────────────────────────

def _is_finite_nonneg(v) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(f) and f >= 0.0


def _has_control_chars(s: str) -> bool:
    """True if s contains ASCII control chars other than tab (0x09) or newline (0x0A)."""
    return bool(re.search(r"[\x00-\x08\x0b-\x1f]", s))


def _validate_text(value, field: str, max_len: int) -> list:
    errs = []
    if not isinstance(value, str):
        errs.append(f"{field}: must be a string")
        return errs
    if len(value) == 0:
        errs.append(f"{field}: must not be empty")
    elif len(value) > max_len:
        errs.append(f"{field}: exceeds maximum length {max_len}")
    if isinstance(value, str) and _has_control_chars(value):
        errs.append(f"{field}: contains disallowed control characters")
    return errs


def _validate_nonneg_bound(value, field: str, max_val: float) -> list:
    if isinstance(value, bool):
        return [f"{field}: must be a finite non-negative number, not bool"]
    if not isinstance(value, (int, float)):
        return [f"{field}: must be a finite non-negative number"]
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return [f"{field}: value out of range"]
    if not math.isfinite(f) or f < 0.0:
        return [f"{field}: must be finite and non-negative"]
    if f > max_val:
        return [f"{field}: exceeds maximum {max_val}"]
    return []


def validate_nutrition_data(data: dict) -> list:
    """
    Validate nutrition record data dict. Returns list of error strings.
    Empty list means valid.

    Required fields: name, start_time, end_time, energy_kcal, protein_g,
                     carbohydrate_g, fat_g.
    Optional field: description body (server prepends AI prefix).
    Unknown fields: rejected (strict schema — never echo field names).
    """
    errs = []
    known = {
        "name", "start_time", "end_time",
        "energy_kcal", "protein_g", "carbohydrate_g", "fat_g",
        "description",
    }
    if set(data.keys()) - known:
        errs.append("input contains unexpected fields; only required nutrients accepted")

    # name
    errs += _validate_text(data.get("name"), "name", MAX_NAME_LEN)

    # timestamps
    start_epoch = end_epoch = None
    for ts_field in ("start_time", "end_time"):
        v = data.get(ts_field)
        if v is None:
            errs.append(f"{ts_field}: required")
            continue
        try:
            dt = _parse_strict_ts(v)
            ep = dt.timestamp()
            _validate_ts_bounds(ep, ts_field)
            if ts_field == "start_time":
                start_epoch = ep
            else:
                end_epoch = ep
        except ValidationError as ve:
            errs.append(f"{ts_field}: {ve}")

    if start_epoch is not None and end_epoch is not None:
        if start_epoch >= end_epoch:
            errs.append("start_time must be strictly before end_time")
        elif (end_epoch - start_epoch) > MAX_DURATION_SECONDS:
            errs.append(f"duration exceeds maximum of {MAX_DURATION_SECONDS}s (24 h)")

    # required nutrients
    errs += _validate_nonneg_bound(data.get("energy_kcal"), "energy_kcal", MAX_KCAL)
    errs += _validate_nonneg_bound(data.get("protein_g"), "protein_g", MAX_PROTEIN_G)
    errs += _validate_nonneg_bound(data.get("carbohydrate_g"), "carbohydrate_g", MAX_CARB_G)
    errs += _validate_nonneg_bound(data.get("fat_g"), "fat_g", MAX_FAT_G)

    # optional description body
    body = data.get("description")
    if body is not None:
        errs += _validate_text(body, "description", MAX_DESCRIPTION_BODY_LEN)

    return errs


def _build_description(user_body) -> str:
    """
    Always return a non-null description with the server-controlled AI prefix.
    If the user provided a non-empty body, append it after the prefix.
    This ensures Health Connect entries always clearly identify as AI estimates.
    """
    if user_body and isinstance(user_body, str) and user_body.strip():
        return AI_DESCRIPTION_PREFIX + " " + user_body.strip()
    return AI_DESCRIPTION_PREFIX


def _safe_float(v) -> float:
    """Convert numeric to float; raises ValidationError on overflow/nan/inf."""
    try:
        f = float(v)
    except (TypeError, OverflowError, ValueError) as e:
        raise ValidationError(f"numeric value out of range: {e}") from e
    if not math.isfinite(f):
        raise ValidationError("non-finite numeric value")
    return f


# ── Row helper ────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Store ─────────────────────────────────────────────────────────────────────

class NutritionStore:
    """
    Thread-safe nutrition queue store.

    Uses BEGIN IMMEDIATE for all mutations to provide cross-process
    consistency (protects against races between CLI and server processes).
    The threading.Lock guards against in-process concurrent access;
    BEGIN IMMEDIATE guards against separate-process concurrent access.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = open_nutrition_rw(db_path)
        init_nutrition_schema(self._conn)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── Create ───────────────────────────────────────────────────────────────

    def create_entry(self, data: dict) -> dict:
        """
        Create a new draft nutrition entry.

        Required data keys: name, start_time, end_time, energy_kcal,
        protein_g, carbohydrate_g, fat_g.
        Optional: description body (server always prepends AI prefix).

        Raises ValidationError on invalid data.
        Returns the created record as a dict.
        """
        errs = validate_nutrition_data(data)
        if errs:
            raise ValidationError("; ".join(errs))

        # Prefix ensures IDs never start with '-', safe for argparse positional args.
        # Existing IDs in DB (from legacy entries without prefix) remain unchanged.
        client_record_id = "nutr_" + secrets.token_urlsafe(32)
        now = _now_utc()
        description = _build_description(data.get("description"))
        energy_kcal = _safe_float(data["energy_kcal"])
        protein_g = _safe_float(data["protein_g"])
        carbohydrate_g = _safe_float(data["carbohydrate_g"])
        fat_g = _safe_float(data["fat_g"])

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO nutrition_queue
                       (client_record_id, client_record_version, state,
                        name, start_time, end_time,
                        energy_kcal, protein_g, carbohydrate_g, fat_g,
                        description,
                        delivery_possible, created_at, updated_at)
                       VALUES (?,1,'draft',?,?,?,?,?,?,?,?,0,?,?)""",
                    (
                        client_record_id,
                        data["name"],
                        data["start_time"],
                        data["end_time"],
                        energy_kcal, protein_g, carbohydrate_g, fat_g,
                        description,
                        now, now,
                    ),
                )
                self._conn.execute("COMMIT")
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                return _row_to_dict(row)
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── Edit ─────────────────────────────────────────────────────────────────

    def edit_entry(self, client_record_id: str, expected_version: int, data: dict) -> dict:
        """
        Edit an existing entry.

        Always resets state→draft, clears confirmation fields, increments version.
        Allowed in states: draft, confirmed, acked (corrections after delivery).
        NOT allowed when: cancelled.

        When editing an acked record (correction after delivery), delivery_possible
        is durably retained (the old version may already be on the phone).

        Uses BEGIN IMMEDIATE to guard against concurrent CLI+server mutations.
        CAS WHERE clause ensures exactly-once even under races.

        Raises NotFound, VersionConflict, InvalidState, ValidationError.
        """
        errs = validate_nutrition_data(data)
        if errs:
            raise ValidationError("; ".join(errs))

        description = _build_description(data.get("description"))
        energy_kcal = _safe_float(data["energy_kcal"])
        protein_g = _safe_float(data["protein_g"])
        carbohydrate_g = _safe_float(data["carbohydrate_g"])
        fat_g = _safe_float(data["fat_g"])
        now = _now_utc()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                if row is None:
                    raise NotFound(client_record_id)
                stored_version = row["client_record_version"]
                if stored_version != expected_version:
                    raise VersionConflict(
                        f"expected version {expected_version}, stored version {stored_version}"
                    )
                state = row["state"]
                if state == STATE_CANCELLED:
                    raise InvalidState("Cannot edit a cancelled entry")

                new_version = stored_version + 1
                # Preserve delivery_possible: once set it is never cleared
                delivery_possible = row["delivery_possible"]

                result = self._conn.execute(
                    """UPDATE nutrition_queue SET
                       client_record_version = ?,
                       state = 'draft',
                       name = ?, start_time = ?, end_time = ?,
                       energy_kcal = ?, protein_g = ?, carbohydrate_g = ?, fat_g = ?,
                       description = ?,
                       confirmed_by = NULL, confirmed_at = NULL, acked_at = NULL,
                       delivery_possible = ?,
                       updated_at = ?
                       WHERE client_record_id = ? AND client_record_version = ?""",
                    (
                        new_version,
                        data["name"], data["start_time"], data["end_time"],
                        energy_kcal, protein_g, carbohydrate_g, fat_g,
                        description,
                        delivery_possible,
                        now, client_record_id, expected_version,
                    ),
                )
                if result.rowcount != 1:
                    # Concurrent mutation won; treat as VersionConflict
                    raise VersionConflict(
                        "concurrent update detected; retry with current version"
                    )
                self._conn.execute("COMMIT")
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                return _row_to_dict(row)
            except (NotFound, VersionConflict, InvalidState, ValidationError):
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── Confirm ──────────────────────────────────────────────────────────────

    def confirm_entry(
        self,
        client_record_id: str,
        expected_version: int,
        affirm: str,
        confirmer: str,
    ) -> dict:
        """
        Confirm a draft entry for delivery to Health Connect.

        affirm must be exactly CONFIRM_AFFIRM_PHRASE.
        confirmer must be a non-empty name.
        expected_version must match stored version exactly.

        Uses BEGIN IMMEDIATE + CAS to prevent concurrent confirmations.
        Raises NotFound, VersionConflict, InvalidState, ValidationError.
        """
        if affirm != CONFIRM_AFFIRM_PHRASE:
            raise ValidationError(
                f"affirm phrase must be exactly: {CONFIRM_AFFIRM_PHRASE!r}"
            )
        if not isinstance(confirmer, str) or not confirmer.strip():
            raise ValidationError("confirmer must be a non-empty string")
        confirmer = confirmer.strip()
        if len(confirmer) > 200:
            raise ValidationError("confirmer name exceeds maximum length of 200")

        now = _now_utc()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                if row is None:
                    raise NotFound(client_record_id)
                stored_version = row["client_record_version"]
                if stored_version != expected_version:
                    raise VersionConflict(
                        f"expected version {expected_version}, stored version {stored_version}"
                    )
                state = row["state"]
                if state == STATE_CONFIRMED:
                    raise InvalidState("entry is already confirmed at this version")
                if state == STATE_CANCELLED:
                    raise InvalidState("cannot confirm a cancelled entry")
                if state == STATE_ACKED:
                    raise InvalidState(
                        "cannot confirm an acked entry; edit first to create a new version"
                    )
                if state != STATE_DRAFT:
                    raise InvalidState(f"unexpected state: {state}")

                result = self._conn.execute(
                    """UPDATE nutrition_queue SET
                       state = 'confirmed',
                       confirmed_by = ?, confirmed_at = ?,
                       updated_at = ?
                       WHERE client_record_id = ? AND client_record_version = ?
                       AND state = 'draft'""",
                    (confirmer, now, now, client_record_id, expected_version),
                )
                if result.rowcount != 1:
                    raise VersionConflict(
                        "concurrent update detected; reload entry and retry"
                    )
                self._conn.execute("COMMIT")
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                return _row_to_dict(row)
            except (NotFound, VersionConflict, InvalidState, ValidationError):
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── Cancel ───────────────────────────────────────────────────────────────

    def cancel_entry(self, client_record_id: str, expected_version: int) -> dict:
        """
        Cancel an entry.

        Pre-fetch (draft, not yet returned from GET queue): clean cancel.
        Post-fetch or post-ack: delivery_possible was set when fetched/acked;
          cannot remove record from phone via write-back protocol (no tombstones).
          Status field 'delivery_possible' persists forever.

        Repeated cancel at same version: idempotent (returns note).
        Uses BEGIN IMMEDIATE + CAS for cross-process safety.
        """
        now = _now_utc()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                if row is None:
                    raise NotFound(client_record_id)
                stored_version = row["client_record_version"]
                if stored_version != expected_version:
                    raise VersionConflict(
                        f"expected version {expected_version}, stored version {stored_version}"
                    )
                state = row["state"]
                delivery_possible = row["delivery_possible"]

                if state == STATE_CANCELLED:
                    self._conn.execute("ROLLBACK")
                    d = _row_to_dict(row)
                    d["cancellation_note"] = "already cancelled; no change"
                    return d

                result = self._conn.execute(
                    """UPDATE nutrition_queue SET
                       state = 'cancelled',
                       updated_at = ?
                       WHERE client_record_id = ? AND client_record_version = ?""",
                    (now, client_record_id, expected_version),
                )
                if result.rowcount != 1:
                    raise VersionConflict(
                        "concurrent update detected; reload entry and retry"
                    )
                self._conn.execute("COMMIT")
                updated = _row_to_dict(
                    self._conn.execute(
                        "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                        (client_record_id,),
                    ).fetchone()
                )
                if delivery_possible:
                    updated["cancellation_note"] = (
                        "This record was returned in a GET /nutrition/queue response "
                        "before cancellation. The phone may have already inserted it "
                        "into Health Connect. The write-back protocol does not support "
                        "tombstone records, so the server cannot send a deletion "
                        "command. The record may remain on the device. "
                        "Status: delivery-possible-before-cancellation."
                    )
                return updated
            except (NotFound, VersionConflict, ValidationError):
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── Status ───────────────────────────────────────────────────────────────

    def get_entry(self, client_record_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
            (client_record_id,),
        ).fetchone()
        if row is None:
            raise NotFound(client_record_id)
        return _row_to_dict(row)

    def list_entries(self, state_filter: Optional[str] = None, limit: int = 200) -> list:
        if state_filter:
            rows = self._conn.execute(
                "SELECT * FROM nutrition_queue WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (state_filter, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM nutrition_queue ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def queue_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM nutrition_queue GROUP BY state"
        ).fetchall()
        stats = {s: 0 for s in _VALID_STATES}
        for row in rows:
            stats[row["state"]] = row["n"]
        return stats

    # ── Queue fetch (Android API) ─────────────────────────────────────────────

    def get_confirmed_queue(self) -> list:
        """
        Return up to MAX_QUEUE_RETURN confirmed+unacked records, oldest first.
        Atomically marks returned records with fetched_at and delivery_possible=1.

        This method has a necessary write side-effect: once a record is returned
        to a GET /queue caller, the phone may insert it, so delivery_possible is
        set durably. This persists across restarts and is never reset.

        Returns list of dicts matching the locked Android API shape:
          client_record_id, client_record_version, name, start_time, end_time,
          energy_kcal, protein_g, carbohydrate_g, fat_g, description
        """
        now = _now_utc()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """SELECT client_record_id, client_record_version,
                              name, start_time, end_time,
                              energy_kcal, protein_g, carbohydrate_g, fat_g,
                              description
                       FROM nutrition_queue
                       WHERE state = 'confirmed'
                       ORDER BY created_at ASC
                       LIMIT ?""",
                    (MAX_QUEUE_RETURN,),
                ).fetchall()

                if rows:
                    crids = [r["client_record_id"] for r in rows]
                    placeholders = ",".join("?" for _ in crids)
                    self._conn.execute(
                        f"""UPDATE nutrition_queue
                           SET delivery_possible = 1,
                               fetched_at = COALESCE(fetched_at, ?),
                               updated_at = ?
                           WHERE client_record_id IN ({placeholders})
                           AND state = 'confirmed'""",
                        [now, now] + crids,
                    )

                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        result = []
        for row in rows:
            result.append({
                "client_record_id": row["client_record_id"],
                "client_record_version": row["client_record_version"],
                "name": row["name"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "energy_kcal": row["energy_kcal"],
                "protein_g": row["protein_g"],
                "carbohydrate_g": row["carbohydrate_g"],
                "fat_g": row["fat_g"],
                "description": row["description"],  # always non-null
            })
        return result

    # ── Ack (Android API) ─────────────────────────────────────────────────────

    def ack_record(self, client_record_id: str, client_record_version: int) -> str:
        """
        Acknowledge a record as written by the phone.

        Returns:
          "acked"       — transitioned confirmed→acked
          "idempotent"  — already acked at same version

        Raises:
          NotFound, VersionConflict, InvalidState

        Uses BEGIN IMMEDIATE + CAS for cross-process safety.
        """
        now = _now_utc()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                    (client_record_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise NotFound(client_record_id)

                stored_version = row["client_record_version"]
                state = row["state"]

                if state == STATE_ACKED:
                    self._conn.execute("ROLLBACK")
                    if stored_version == client_record_version:
                        return "idempotent"
                    raise VersionConflict(
                        f"record acked at version {stored_version}, "
                        f"requested ack at version {client_record_version}"
                    )

                if state != STATE_CONFIRMED:
                    self._conn.execute("ROLLBACK")
                    raise InvalidState(
                        f"cannot ack record in state '{state}'; must be 'confirmed'"
                    )
                if stored_version != client_record_version:
                    self._conn.execute("ROLLBACK")
                    raise VersionConflict(
                        f"expected version {client_record_version}, "
                        f"stored version {stored_version}"
                    )

                result = self._conn.execute(
                    """UPDATE nutrition_queue SET
                       state = 'acked',
                       acked_at = ?,
                       delivery_possible = 1,
                       updated_at = ?
                       WHERE client_record_id = ? AND client_record_version = ?
                       AND state = 'confirmed'""",
                    (now, now, client_record_id, client_record_version),
                )
                if result.rowcount != 1:
                    raise VersionConflict(
                        "concurrent update detected; retry with current state"
                    )
                self._conn.execute("COMMIT")
                return "acked"
            except (NotFound, VersionConflict, InvalidState):
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
