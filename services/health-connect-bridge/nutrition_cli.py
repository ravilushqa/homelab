"""
Nutrition queue CLI — create, edit, confirm, status, cancel.

PHI safety:
  - Meal data (name, times, nutrients) arrives via JSON file or stdin,
    never as command-line arguments (which appear in process listings).
  - Error messages never echo user-supplied values, field names, or paths.
  - No PHI in any log or stderr output.

JSON input hardening:
  - Max input size 65536 bytes.
  - Reject duplicate JSON keys.
  - Reject non-finite floats (NaN, Infinity, -Infinity).
  - Reject recursion depth > 20 levels.
  - Controlled errors; never propagate raw exceptions to output.

Commands:
  create     Create a new draft entry (JSON from --file or stdin).
  edit       Edit an entry, always resets confirmation (JSON from --file or stdin).
  confirm    Confirm a draft entry for Health Connect delivery.
  status     Show queue status or a single entry.
  cancel     Cancel an entry (pre-fetch: clean; post-fetch: documents limitation).
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

from .nutrition_queue import (
    NutritionStore,
    VersionConflict,
    NotFound,
    InvalidState,
    ValidationError,
    WrongDatabase,
    CONFIRM_AFFIRM_PHRASE,
    AI_DESCRIPTION_PREFIX,
    STATE_DRAFT,
    STATE_CONFIRMED,
    STATE_CANCELLED,
    STATE_ACKED,
)

_DEFAULT_DB = (
    Path.home()
    / ".hermes/profiles/health/workspace/health-connect-bridge/data/nutrition.sqlite3"
)

_MAX_JSON_INPUT = 65536    # 64 KiB
_MAX_JSON_DEPTH = 20


# ── JSON hardening ────────────────────────────────────────────────────────────

def _no_dup_keys(pairs: list) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key in JSON input")
        d[k] = v
    return d


def _check_depth(obj, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting depth exceeds limit")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _check_depth(item, depth + 1)


def _check_no_nan_inf(obj) -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("non-finite float in JSON input")
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_no_nan_inf(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_no_nan_inf(item)


def _load_json_input(args: argparse.Namespace) -> Optional[dict]:
    """
    Load and harden-parse JSON from --file or stdin.
    Returns parsed dict, or prints error to stderr and returns None.
    Never echoes user-supplied data in error messages.
    """
    if hasattr(args, "file") and args.file:
        try:
            raw = Path(args.file).read_bytes()
        except OSError:
            print(json.dumps({"error": "cannot read input file"}), file=sys.stderr)
            return None
    else:
        try:
            raw = sys.stdin.buffer.read(_MAX_JSON_INPUT + 1)
        except OSError:
            print(json.dumps({"error": "cannot read stdin"}), file=sys.stderr)
            return None

    if len(raw) > _MAX_JSON_INPUT:
        print(json.dumps({"error": "input exceeds maximum size"}), file=sys.stderr)
        return None

    try:
        data = json.loads(raw, object_pairs_hook=_no_dup_keys)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({"error": "invalid JSON in input"}), file=sys.stderr)
        return None
    except RecursionError:
        print(json.dumps({"error": "JSON input too deeply nested"}), file=sys.stderr)
        return None

    if not isinstance(data, dict):
        print(json.dumps({"error": "JSON input must be an object"}), file=sys.stderr)
        return None

    try:
        _check_depth(data)
    except ValueError:
        print(json.dumps({"error": "JSON input too deeply nested"}), file=sys.stderr)
        return None

    try:
        _check_no_nan_inf(data)
    except ValueError:
        print(json.dumps({"error": "non-finite number in JSON input"}), file=sys.stderr)
        return None

    return data


# ── Store opener ──────────────────────────────────────────────────────────────

def _open_store(args: argparse.Namespace) -> Optional[NutritionStore]:
    try:
        return NutritionStore(args.db)
    except WrongDatabase:
        print(
            json.dumps({"error": "path does not point to a nutrition queue database"}),
            file=sys.stderr,
        )
        return None
    except Exception:
        print(json.dumps({"error": "cannot open database"}), file=sys.stderr)
        return None


# ── Output helpers ────────────────────────────────────────────────────────────

def _safe_entry(row: dict) -> dict:
    """
    Prepare a row dict for display.
    Strips the server-controlled AI prefix from description for CLI display
    so the caller sees the body they entered.
    """
    out = dict(row)
    desc = out.get("description") or ""
    if desc == AI_DESCRIPTION_PREFIX:
        out["description_body"] = ""
        out["description_has_ai_prefix"] = True
    elif desc.startswith(AI_DESCRIPTION_PREFIX + " "):
        out["description_body"] = desc[len(AI_DESCRIPTION_PREFIX) + 1:]
        out["description_has_ai_prefix"] = True
    elif desc.startswith(AI_DESCRIPTION_PREFIX):
        out["description_body"] = desc[len(AI_DESCRIPTION_PREFIX):]
        out["description_has_ai_prefix"] = True
    return out


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create(args: argparse.Namespace) -> int:
    data = _load_json_input(args)
    if data is None:
        return 1

    store = _open_store(args)
    if store is None:
        return 1

    try:
        entry = store.create_entry(data)
    except ValidationError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "internal error"}), file=sys.stderr)
        return 1
    finally:
        store.close()

    print(json.dumps({
        "ok": True,
        "client_record_id": entry["client_record_id"],
        "client_record_version": entry["client_record_version"],
        "state": entry["state"],
    }, indent=2))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    data = _load_json_input(args)
    if data is None:
        return 1

    store = _open_store(args)
    if store is None:
        return 1

    try:
        entry = store.edit_entry(args.id, args.version, data)
    except NotFound:
        print(json.dumps({"error": "not found"}), file=sys.stderr)
        return 1
    except VersionConflict as e:
        print(json.dumps({"error": "version conflict", "detail": str(e)}), file=sys.stderr)
        return 1
    except InvalidState as e:
        print(json.dumps({"error": "invalid state", "detail": str(e)}), file=sys.stderr)
        return 1
    except ValidationError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "internal error"}), file=sys.stderr)
        return 1
    finally:
        store.close()

    out = {
        "ok": True,
        "client_record_id": entry["client_record_id"],
        "client_record_version": entry["client_record_version"],
        "state": entry["state"],
    }
    if entry.get("delivery_possible"):
        out["delivery_possible"] = True
        out["delivery_note"] = (
            "An earlier version of this record may already be on the phone. "
            "After reconfirm and redeliver, the phone should receive the higher "
            "version as a correction."
        )
    print(json.dumps(out, indent=2))
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if store is None:
        return 1

    try:
        entry = store.confirm_entry(args.id, args.version, args.affirm, args.confirmer)
    except NotFound:
        print(json.dumps({"error": "not found"}), file=sys.stderr)
        return 1
    except VersionConflict as e:
        print(json.dumps({"error": "version conflict", "detail": str(e)}), file=sys.stderr)
        return 1
    except InvalidState as e:
        print(json.dumps({"error": "invalid state", "detail": str(e)}), file=sys.stderr)
        return 1
    except ValidationError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "internal error"}), file=sys.stderr)
        return 1
    finally:
        store.close()

    print(json.dumps({
        "ok": True,
        "client_record_id": entry["client_record_id"],
        "client_record_version": entry["client_record_version"],
        "state": entry["state"],
        "confirmed_by": entry["confirmed_by"],
        "confirmed_at": entry["confirmed_at"],
    }, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if store is None:
        return 1

    try:
        if args.id:
            try:
                entry = store.get_entry(args.id)
            except NotFound:
                print(json.dumps({"error": "not found"}), file=sys.stderr)
                return 1
            print(json.dumps(_safe_entry(entry), indent=2))
        else:
            stats = store.queue_stats()
            entries = store.list_entries(
                state_filter=args.state if args.state else None
            )
            print(json.dumps({
                "stats": stats,
                "entries": [_safe_entry(e) for e in entries],
            }, indent=2))
    except Exception:
        print(json.dumps({"error": "internal error"}), file=sys.stderr)
        return 1
    finally:
        store.close()

    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if store is None:
        return 1

    try:
        result = store.cancel_entry(args.id, args.version)
    except NotFound:
        print(json.dumps({"error": "not found"}), file=sys.stderr)
        return 1
    except VersionConflict as e:
        print(json.dumps({"error": "version conflict", "detail": str(e)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "internal error"}), file=sys.stderr)
        return 1
    finally:
        store.close()

    out = {
        "ok": True,
        "client_record_id": result["client_record_id"],
        "state": result["state"],
    }
    if "cancellation_note" in result:
        out["cancellation_note"] = result["cancellation_note"]
    print(json.dumps(out, indent=2))
    return 0


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nutrition-queue",
        description=(
            "Nutrition queue CLI — create, edit, confirm, status, cancel.\n\n"
            "JSON input for create/edit is read from --file or stdin\n"
            "to avoid placing health information on the command line.\n\n"
            f"Confirm requires exactly:\n"
            f"  --version N\n"
            f"  --affirm '{CONFIRM_AFFIRM_PHRASE}'\n"
            f"  --confirmer NAME"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help="Path to nutrition SQLite database",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser(
        "create",
        help="Create a draft entry (JSON from --file or stdin)",
    )
    p_create.add_argument("--file", metavar="PATH",
                          help="JSON file path (default: read from stdin)")

    # edit
    p_edit = sub.add_parser(
        "edit",
        help="Edit an entry (resets confirmation; JSON from --file or stdin)",
    )
    p_edit.add_argument("id", help="client_record_id to edit")
    p_edit.add_argument("--version", type=int, required=True, metavar="N",
                        help="Expected current version (optimistic concurrency)")
    p_edit.add_argument("--file", metavar="PATH",
                        help="JSON file path (default: read from stdin)")

    # confirm
    p_confirm = sub.add_parser(
        "confirm",
        help="Confirm a draft entry for Health Connect delivery",
    )
    p_confirm.add_argument("id", help="client_record_id to confirm")
    p_confirm.add_argument("--version", type=int, required=True, metavar="N",
                           help="Must match stored version exactly")
    p_confirm.add_argument(
        "--affirm",
        required=True,
        metavar="PHRASE",
        help=f"Must be exactly: '{CONFIRM_AFFIRM_PHRASE}'",
    )
    p_confirm.add_argument("--confirmer", required=True, metavar="NAME",
                           help="Name of the person confirming")

    # status
    p_status = sub.add_parser("status", help="Show status of one or all entries")
    p_status.add_argument("id", nargs="?", default=None,
                          help="client_record_id (omit to list all)")
    p_status.add_argument("--state",
                          choices=["draft", "confirmed", "cancelled", "acked"],
                          default=None, help="Filter by state")

    # cancel
    p_cancel = sub.add_parser(
        "cancel",
        help="Cancel an entry (pre-fetch: clean; post-fetch: documents limitation)",
    )
    p_cancel.add_argument("id", help="client_record_id to cancel")
    p_cancel.add_argument("--version", type=int, required=True, metavar="N",
                          help="Expected current version")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "create": cmd_create,
        "edit": cmd_edit,
        "confirm": cmd_confirm,
        "status": cmd_status,
        "cancel": cmd_cancel,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
