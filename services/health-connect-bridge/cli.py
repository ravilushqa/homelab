"""
Read-only CLI for Health Connect data.

All connections use PRAGMA query_only=ON and mode=ro URI.
No server, no mutations, no arbitrary SQL commands.

Commands:
  status              Sync freshness and per-source record counts.
  day [DATE]          Daily summary per source (steps, HR, weight).
  sleep [DATE]        Sleep sessions whose END falls on DATE (Berlin time).
  workouts [DATE]     Exercise sessions whose START falls on DATE (Berlin time).

DATE defaults to today in Europe/Berlin if omitted.
Format: YYYY-MM-DD.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_DEFAULT_DB = (
    Path.home()
    / ".hermes/profiles/health/workspace/health-connect-bridge/data/health.sqlite3"
)
_TZ = "Europe/Berlin"


def _today_berlin() -> str:
    return datetime.now(ZoneInfo(_TZ)).strftime("%Y-%m-%d")


def _open_ro(db_path: Path):
    from .store import open_ro
    return open_ro(db_path)


def cmd_status(args: argparse.Namespace) -> int:
    from .store import query_status
    try:
        conn = _open_ro(args.db)
    except FileNotFoundError as e:
        print(json.dumps({"state": "not_synced", "error": str(e)}))
        return 1
    except RuntimeError as e:
        print(json.dumps({"state": "error", "error": str(e)}))
        return 1
    try:
        result = query_status(conn)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0 if result.get("state") in ("ok",) else 1


def cmd_day(args: argparse.Namespace) -> int:
    from .store import query_day, _day_epoch_bounds
    date = getattr(args, "date", None) or _today_berlin()
    try:
        _day_epoch_bounds(date, _TZ)  # validate date format early
    except ValueError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        conn = _open_ro(args.db)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        result = query_day(conn, date, _TZ)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


def cmd_sleep(args: argparse.Namespace) -> int:
    from .store import query_sleep, _day_epoch_bounds
    date = getattr(args, "date", None) or _today_berlin()
    try:
        _day_epoch_bounds(date, _TZ)
    except ValueError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        conn = _open_ro(args.db)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        result = query_sleep(conn, date, _TZ)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


def cmd_workouts(args: argparse.Namespace) -> int:
    from .store import query_workouts, _day_epoch_bounds
    date = getattr(args, "date", None) or _today_berlin()
    try:
        _day_epoch_bounds(date, _TZ)
    except ValueError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        conn = _open_ro(args.db)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "date": date}))
        return 1
    try:
        result = query_workouts(conn, date, _TZ)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health-connect-read",
        description="Read-only Health Connect data CLI (no mutations, no server).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help="Path to the SQLite database (opened read-only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show sync freshness and record counts per source")

    p_day = sub.add_parser("day", help="Daily summary per source")
    p_day.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today in Europe/Berlin)")

    p_sleep = sub.add_parser("sleep", help="Sleep sessions ending on a date")
    p_sleep.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")

    p_work = sub.add_parser("workouts", help="Exercise sessions starting on a date")
    p_work.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "status": cmd_status,
        "day": cmd_day,
        "sleep": cmd_sleep,
        "workouts": cmd_workouts,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
