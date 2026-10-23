#!/usr/bin/env python3
"""
nutrition-writeback — skill executable for nutrition Health Connect write-back.

Calls the installed nutrition-queue CLI as a subprocess. Never imports
production modules or creates databases. Fails closed if system not deployed.

Subcommands:
  draft         Read JSON from stdin or --file, create draft, read back written record.
  confirm       Read back current record, verify name/version, verify explicit raw
                user consent text, confirm with technical affirm phrase, read back
                before AND after. Requires --consent-text or --consent-file and
                --confirmer and --meal-name.
  preview       Read back a draft, compute a content hash, and register a durable
                pending-preview context file binding the exact ID/version/content to
                the expected user/chat/thread and preview message. Required before
                short-confirm. Does not mutate the queue.
  short-confirm Consume a registered pending preview. Validates: short affirmative
                text (Да/Запиши), same user/chat/thread as bound, explicit reply to
                the bound preview message, message within the preview window, re-reads
                draft and checks content hash for drift. Then runs full mutation
                readiness and confirms via CLI. No long consent phrase required from
                the user; all binding is via the preview context.
  personal-preview  Personal-chat scope only (--scope LABEL --attest-personal-chat):
                store ONE active preview (draft snapshot + wrapper-built compact (display_format 2)
                text of saved name/macros/Berlin time + optional agent notes) BEFORE
                it is displayed; show exactly the returned display_text. Supersedes
                the scope's older preview. No queue mutation.
  personal-confirm  Confirm the scope's single active preview from the raw bare reply
                in --consent-file (Да/Запиши; file must be newer than the preview and never
                previously used). Agent-conversation-attested: no
                Telegram message ID/receipt/reply linkage (provenance_level
                "agent_conversation_attested"). Same snapshot CAS + privacy gates.
                No time-only expiry: an unchanged, still uniquely active preview
                may be confirmed after its stored expires_at (audited as late).
  recipe-preview / recipe-confirm / recipe-show
                Personal-scope known-recipe registry: a user-confirmed recipe
                (quantities + four label-based macros) is stored immutable
                (version + hash). Saving a recipe never logs a meal.
  recipe-register  Register an ALREADY user-confirmed recipe from private evidence of
                the user's real explicit save request (current message or history);
                no second yes, never logs a meal, does not touch the open question.
  personal-log-known  Personal-chat fast path for ONE fresh draft built from an
                active known recipe at an unambiguous user-supplied time: binds the
                user's raw direct request file (single-use) to the exact draft
                id/version/snapshot and confirms with the same gates/CAS.
  status        Show status of one or all entries (readonly), with user_status.
  day-report    Read-only Europe/Berlin day report: pending / done / excluded
                queue records with reasons; never sums queue and ingest.

Short-confirm trust boundary: the preview file and the --transport-file are
written by the trusted agent workflow, not independently authenticated by the
messaging server. They are private (0700/0600, no symlinks), bound to each other
and to the live draft, but a local file is workflow-attested evidence only
(provenance_level "workflow_attested"). Preview authorizations are single-use:
pending -> consuming (under flock) -> consumed; a crash leaves "consuming",
which fails closed and requires a new preview and reply.

Isolation mode (testing only):
  --isolation-root DIR   Private directory under allowed scratch; BOTH --cli and
                         --db must be under this same directory. Also required:
                         --privacy-source, --ssh-fixture, --token-path all under
                         this root. Receiver URL must be loopback http://.
  --cli PATH             nutrition-queue CLI binary (must be under --isolation-root)
  --db PATH              Nutrition SQLite database (must be under --isolation-root)

Production mode (no --cli/--db/--isolation-root):
  All prerequisites (CLI binary, DB, token) must exist at production paths.
  Never creates the database. Reports specific failure if not deployed.

Mutation readiness (draft/confirm only):
  --receiver-url URL     HTTP(S) receiver URL (https:// required for production;
                         http://127.x loopback required for isolation).
  --token-path PATH      Path to nutrition token file.
  --privacy-verifier P   Path to privacy_gate_check.py.
  --privacy-source PATH  Path to traefik compose.yaml for privacy source check.
  --live-host HOST       Traefik SSH host for live privacy check (production only).
  --ssh-user USER        SSH user for live check (default: root).
  --ssh-fixture PATH     SSH fixture for privacy live check (isolation only;
                         must be inside --isolation-root).

Exact consent grammar (English):
  I, {confirmer}, confirm the Health Connect entry for {meal_name}, ID {id},
  version {version}, with the shown time and macros.

Exact consent grammar (Russian):
  Я, {confirmer}, подтверждаю запись Health Connect для {meal_name}, ID {id},
  версия {version}, с показанным временем и макросами.

All fields must match exactly. Negation, vague phrases, mismatched confirmer,
wrong ID, wrong version, or wrong meal name are rejected. No fallback grammar.
"""

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROD_BASE = Path.home() / ".hermes/profiles/health/workspace/health-connect-bridge"
_PROD_CLI = _PROD_BASE / "bin" / "nutrition-queue"
_PROD_DB = _PROD_BASE / "data" / "nutrition.sqlite3"
_PROD_TOKEN = _PROD_BASE / "secrets" / "nutrition-token"

# Both scratch roots are allowed for isolation
_ALLOWED_SCRATCH = Path.home() / ".hermes/cache/scratch"
_ALLOWED_SCRATCH_ROOTS = [
    _ALLOWED_SCRATCH,
    Path.home() / ".hermes/profiles/homelab/cache/scratch",
    Path.home() / ".hermes/profiles/health/cache/scratch",
]

# Trusted production verifier paths (must be one of these two exact paths)
_PROD_VERIFIER_PRIMARY = Path(
    "/home/claw/.hermes/hermes-agent/homelab/services/health-connect-bridge/privacy_gate_check.py"
)
_PROD_VERIFIER_WORKTREE = Path(
    "/home/claw/.hermes/cache/scratch/nutrition-server-worktree/services/health-connect-bridge/privacy_gate_check.py"
)
# Source must come from the primary homelab repo only (worktree not trusted in production)
_PROD_VERIFIER_TO_SOURCE = {
    _PROD_VERIFIER_PRIMARY: Path(
        "/home/claw/.hermes/hermes-agent/homelab/komodo/stacks/traefik/compose.yaml"
    ),
}
_PROD_RECEIVER_URLS = {"https://health-connect.ravil.space", "https://health-connect.ravil.space/"}
_PROD_LIVE_HOST = "192.168.1.166"
_PROD_SSH_USER = "claw"

_TOKEN_MAX_BYTES = 4096

_CONFIRM_AFFIRM = "I confirm this meal for Health Connect"
_AI_DESCRIPTION_PREFIX = "[AI estimate]"
_MAX_JSON_INPUT = 65536

# Exact anchored consent grammar — both languages.
# Confirmer, meal, ID, and version are all named captures; all must match args exactly.
_CONSENT_EN = re.compile(
    r"^I, (?P<confirmer>.+?), confirm the Health Connect entry for "
    r"(?P<meal>.+?), ID (?P<id>\S+), version (?P<version>\d+), "
    r"with the shown time and macros\.$"
)
_CONSENT_RU = re.compile(
    r"^Я, (?P<confirmer>.+?), подтверждаю запись Health Connect для "
    r"(?P<meal>.+?), ID (?P<id>\S+), версия (?P<version>\d+), "
    r"с показанным временем и макросами\.$"
)
# Negation words — consent containing these is always rejected.
_NEGATION_RE = re.compile(r"\b(not|do not|don't)\b|(?<!\w)не(?!\w)", re.IGNORECASE)


def _emit_err(obj: dict) -> None:
    print(json.dumps(obj), file=sys.stderr)


def _check_prod_readiness(cli: Path, db: Path) -> list:
    """Return list of failure strings (empty = all prerequisites present)."""
    errs = []
    if not cli.exists():
        errs.append(f"CLI binary not found: {cli}")
    elif not os.access(str(cli), os.X_OK):
        errs.append(f"CLI binary not executable: {cli}")
    if not db.exists():
        errs.append(f"Nutrition DB not found: {db}")
    tok_errs = _check_regular_file_secure(_PROD_TOKEN, "production token", need_mode=0o600)
    if tok_errs:
        errs.extend(tok_errs)
    return errs


def _is_under_allowed_scratch(p: Path) -> bool:
    """True if p resolves to a path under any allowed scratch root."""
    resolved = p.resolve()
    for root in _ALLOWED_SCRATCH_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            pass
    return False


def _has_symlink_ancestor(p: Path) -> bool:
    """True if p itself or any ancestor directory is a symlink."""
    current = p.absolute()  # lexical absolute path, no symlink resolution
    while True:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _check_path_no_symlink_ancestor(p: Path, label: str) -> list:
    """Return error list if p or any ancestor is a symlink."""
    if _has_symlink_ancestor(p):
        return [f"{label} path must not have a symlink in any ancestor: {p}"]
    return []


def _check_regular_file_secure(p: Path, label: str, need_mode: int | None = None) -> list:
    """
    Verify p is a regular non-symlink file with no hardlinks, owned by us.
    Optionally check exact permission mode.
    Returns list of error strings.
    """
    errs = _check_path_no_symlink_ancestor(p, label)
    if errs:
        return errs
    if not p.exists():
        return [f"{label} not found: {p}"]
    if not p.is_file():
        return [f"{label} must be a regular file: {p}"]
    try:
        st = p.stat()
    except OSError as exc:
        return [f"cannot stat {label}: {exc}"]
    if st.st_nlink != 1:
        return [f"{label} must not have hard links (st_nlink={st.st_nlink}): {p}"]
    if st.st_uid != os.getuid():
        return [f"{label} must be owned by the current user: {p}"]
    if need_mode is not None:
        mode = st.st_mode & 0o777
        if mode != need_mode:
            return [f"{label} has insecure permissions {oct(mode)}, expected {oct(need_mode)}: {p}"]
    return []


def _load_token_secure(token_path: Path) -> tuple:
    """
    Validate token file security metadata BEFORE reading, then load.
    Returns (token_str, errors). Errors are intentionally generic.
    """
    # Ancestor symlink check
    if _has_symlink_ancestor(token_path):
        return None, ["token not available"]
    if not token_path.exists():
        return None, ["token not available"]
    if not token_path.is_file():
        return None, ["token not available"]
    try:
        st = token_path.stat()
    except OSError:
        return None, ["token not available"]
    # Must be 0600, owned, non-hardlinked
    if st.st_nlink != 1:
        return None, ["token not available"]
    if st.st_uid != os.getuid():
        return None, ["token not available"]
    mode = st.st_mode & 0o777
    if mode != 0o600:
        return None, ["token not available"]
    if st.st_size > _TOKEN_MAX_BYTES:
        return None, ["token not available"]
    # Read after all metadata checks pass
    try:
        raw = token_path.read_bytes()
    except OSError:
        return None, ["token not available"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["token not available"]
    stripped = text.strip()
    if not stripped:
        return None, ["token not available"]
    # Reject control characters (allow printable ASCII + common whitespace stripped above)
    import unicodedata as _uc
    for ch in stripped:
        cat = _uc.category(ch)
        if cat.startswith("C"):
            return None, ["token not available"]
    return stripped, []


def _is_production_path(p: Path) -> bool:
    """True if p resolves to anywhere under the production workspace."""
    try:
        p.resolve().relative_to(_PROD_BASE.resolve())
        return True
    except ValueError:
        return False


def _check_isolation_root(root: Path) -> list:
    """
    Validate the isolation root directory:
    - Must exist and be a real directory (not a symlink)
    - Must be under allowed scratch
    - Must have 0700 permissions
    - Must be owned by the current process uid
    Returns list of error strings (empty = valid).
    """
    errs = []
    if _has_symlink_ancestor(root):
        errs.append(f"isolation root must not have a symlink in any ancestor: {root}")
        return errs
    if not root.exists():
        errs.append(f"isolation root does not exist: {root}")
        return errs
    if not root.is_dir():
        errs.append(f"isolation root is not a directory: {root}")
        return errs
    if not _is_under_allowed_scratch(root):
        errs.append(
            f"isolation root must be under {_ALLOWED_SCRATCH}: {root}"
        )
    try:
        st = root.stat()
        mode = st.st_mode & 0o777
        if mode != 0o700:
            errs.append(
                f"isolation root must have 0700 permissions, got {oct(mode)}: {root}"
            )
        if st.st_uid != os.getuid():
            errs.append(f"isolation root must be owned by current user (uid {os.getuid()}): {root}")
    except OSError as exc:
        errs.append(f"cannot stat isolation root: {exc}")
    return errs


def _under_root(p: Path, root: Path) -> bool:
    """True if p resolves to a path under root."""
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve(args: argparse.Namespace) -> tuple:
    """
    Return (cli, db, isolation_root) or (None, None, None) on failure.

    Isolation mode (BOTH --cli AND --db provided):
      - --isolation-root required; must be under allowed scratch, private (0700), owned.
      - Both CLI and DB must resolve under the SAME isolation root.
      - CLI must not be a production path.
      - DB must not be a production path.
      - CLI must exist and be executable.
    Production mode (neither --cli nor --db):
      - --isolation-root must not be provided.
      - All production prerequisites must exist.
    Partial override (only one of --cli/--db): always rejected.
    """
    has_cli = bool(args.cli)
    has_db = bool(args.db)
    has_root = bool(getattr(args, "isolation_root", None))

    if has_cli != has_db:
        _emit_err({
            "error": "isolation mode requires BOTH --cli and --db together",
            "hint": "provide both --cli PATH --db PATH with --isolation-root DIR, or neither (production mode)",
        })
        return None, None, None

    if has_cli and has_db:
        if not has_root:
            _emit_err({
                "error": "isolation mode requires --isolation-root DIR",
                "hint": "provide --isolation-root with a private directory under allowed scratch",
            })
            return None, None, None

        root = Path(args.isolation_root)
        cli = Path(args.cli)
        db = Path(args.db)

        root_errs = _check_isolation_root(root)
        if root_errs:
            _emit_err({
                "error": "invalid --isolation-root",
                "details": root_errs,
            })
            return None, None, None

        if _is_production_path(cli):
            _emit_err({
                "error": "isolation CLI must not be a production path",
                "path": str(cli),
                "hint": f"use a CLI under {root}",
            })
            return None, None, None

        if _is_production_path(db):
            _emit_err({
                "error": "isolation DB must not be a production path",
                "path": str(db),
                "hint": f"use a DB under {root}",
            })
            return None, None, None

        if not _under_root(cli, root):
            _emit_err({
                "error": "isolation CLI must be under --isolation-root",
                "cli": str(cli),
                "isolation_root": str(root),
            })
            return None, None, None

        if not _under_root(db, root):
            _emit_err({
                "error": "isolation DB must be under --isolation-root",
                "db": str(db),
                "isolation_root": str(root),
            })
            return None, None, None

        # Full security check for CLI (regular, owned, non-hardlinked, no symlink ancestor)
        cli_sec_errs = _check_regular_file_secure(cli, "isolation CLI")
        if cli_sec_errs:
            _emit_err({"error": "isolation CLI path security violation", "details": cli_sec_errs})
            return None, None, None

        if not os.access(str(cli), os.X_OK):
            _emit_err({"error": "CLI not executable", "path": str(cli)})
            return None, None, None

        # Ancestor symlink check for DB path (DB need not exist yet)
        db_anc_errs = _check_path_no_symlink_ancestor(db, "isolation DB")
        if db_anc_errs:
            _emit_err({"error": "isolation DB path security violation", "details": db_anc_errs})
            return None, None, None

        # If DB already exists, apply full security checks
        if db.exists():
            db_sec_errs = _check_regular_file_secure(db, "isolation DB")
            if db_sec_errs:
                _emit_err({"error": "isolation DB path security violation", "details": db_sec_errs})
                return None, None, None

        return cli, db, root

    # Production mode
    if has_root:
        _emit_err({
            "error": "--isolation-root must not be provided in production mode",
            "hint": "use --isolation-root only with --cli and --db",
        })
        return None, None, None

    errs = _check_prod_readiness(_PROD_CLI, _PROD_DB)
    if errs:
        _emit_err({
            "error": "system not deployed",
            "details": errs,
            "hint": (
                "Run install.sh --nutrition and start the service before "
                "using nutrition write-back."
            ),
        })
        return None, None, None
    return _PROD_CLI, _PROD_DB, None


def _run_cli(cli: Path, db: Path, cli_args: list, stdin_bytes: bytes = None) -> tuple:
    """
    Invoke nutrition-queue CLI. Returns (returncode, stdout_str, stderr_str).
    Never raises; subprocess errors become rc=1 with error in stderr_str.
    """
    cmd = [str(cli), "--db", str(db)] + [str(a) for a in cli_args]
    try:
        r = subprocess.run(cmd, input=stdin_bytes, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return 1, "", json.dumps({"error": "CLI subprocess timeout"})
    except OSError as exc:
        return 1, "", json.dumps({"error": f"CLI subprocess failed: {exc}"})
    return (
        r.returncode,
        r.stdout.decode("utf-8", errors="replace"),
        r.stderr.decode("utf-8", errors="replace"),
    )


def _run_privacy_gate(
    verifier: Path,
    source: Path,
    live_host: str | None,
    ssh_user: str,
    ssh_fixture: Path | None,
) -> list:
    """
    Run privacy_gate_check.py as a subprocess.
    Returns list of error strings (empty = gate passed).
    """
    cmd = [sys.executable, str(verifier), "--source", str(source)]
    if ssh_fixture is not None:
        cmd += ["--ssh-fixture", str(ssh_fixture)]
    elif live_host:
        cmd += ["--live-host", live_host, "--ssh-user", ssh_user]
    else:
        return ["privacy gate: no --live-host or --ssh-fixture provided; cannot verify live state"]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ["privacy gate check timed out (60s)"]
    except OSError as exc:
        return [f"privacy gate check failed to start: {exc}"]

    if result.returncode != 0:
        return ["privacy gate check FAILED — mutation blocked until gate passes"]
    return []


def _probe_receiver_readiness(url: str, token: str, is_isolation: bool) -> list:
    """
    Probe the HTTP receiver before any mutation:
      1. POST /nutrition/ack with wrong token  → must get 401
      2. POST /nutrition/ack with correct token + {} body → must get 400
    No redirects permitted. Bounded timeout (10s per probe).
    Production: URL must be https://.
    Isolation: URL must be exact loopback http://localhost:PORT or http://127.x:PORT.
    URL must have no userinfo, query string, fragment, or non-root path.
    Returns list of error strings (empty = receiver ready).
    """
    import ipaddress as _ipaddress
    from urllib.parse import urlparse
    from http.client import HTTPConnection, HTTPSConnection
    import ssl as _ssl

    # Parse URL before any network activity
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        port = parsed.port  # raises ValueError for out-of-range ports
        url_path = parsed.path or ""
    except Exception:
        return ["receiver URL is malformed or has an invalid port"]

    # Reject userinfo (credentials in URL)
    if parsed.username or parsed.password:
        return ["receiver URL must not contain userinfo (credentials in URL rejected)"]
    # Reject query string and fragment
    if parsed.query:
        return ["receiver URL must not contain a query string"]
    if parsed.fragment:
        return ["receiver URL must not contain a fragment"]
    # Only root path permitted
    if url_path not in ("", "/"):
        return [f"receiver URL must use root path only, got path: {url_path!r}"]

    if is_isolation:
        if scheme != "http":
            return [f"isolation receiver URL must use http scheme, got: {scheme!r}"]
        if host is None:
            return ["isolation receiver URL has no host"]
        # Must be exact loopback: localhost or an IP address that is loopback
        _is_loopback = False
        if host == "localhost":
            _is_loopback = True
        else:
            try:
                _is_loopback = _ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if not _is_loopback:
            return [
                f"isolation receiver URL host must be loopback (localhost or 127.x/::1), "
                f"got: {host!r}"
            ]
        if port is None or not (1 <= port <= 65535):
            return ["isolation receiver URL must specify a valid port (1-65535)"]
    else:
        if scheme != "https":
            return [f"production receiver URL must be https://, got scheme: {scheme!r}"]
        if host is None:
            return ["production receiver URL has no host"]
        if port is not None and not (1 <= port <= 65535):
            return [f"production receiver URL has invalid port: {port}"]

    ack_path = "/nutrition/ack"

    def _do_probe(tok: str) -> tuple:
        """Returns (status_code, error_str). error_str is None on success."""
        body = b"{}"
        try:
            if scheme == "https":
                ctx = _ssl.create_default_context()
                conn = HTTPSConnection(host, port or 443, timeout=10, context=ctx)
            else:
                conn = HTTPConnection(host, port or 80, timeout=10)
            conn.request("POST", ack_path, body=body, headers={
                "Content-Type": "application/json",
                "X-Nutrition-Token": tok,
                "Content-Length": str(len(body)),
            })
            resp = conn.getresponse()
            st = resp.status
            resp.read(16384)  # bounded read
            conn.close()
            if st in (301, 302, 303, 307, 308):
                return None, f"receiver returned unexpected redirect ({st})"
            return st, None
        except Exception:
            return None, "connection failed"  # no credential echo in errors

    wrong_token = "probe-wrong-token-XXXXXXXXXXXXXXXXXXXXXXXXXXX"
    st1, err1 = _do_probe(wrong_token)
    if err1:
        return [f"receiver probe (wrong-token) connection failed: {err1}"]
    if st1 != 401:
        return [f"receiver probe (wrong-token): expected 401, got {st1}"]

    st2, err2 = _do_probe(token)
    if err2:
        return [f"receiver probe (invalid-body) connection failed: {err2}"]
    if st2 != 400:
        return [f"receiver probe (invalid-body): expected 400, got {st2}"]

    return []


def _check_mutation_readiness(
    args: argparse.Namespace,
    is_isolation: bool,
    isolation_root: Path | None,
) -> list:
    """
    Pre-mutation checks executed in order before any write:
      1. Privacy gate (privacy_gate_check.py subprocess) — before token load
      2. Token load from --token-path
      3. Receiver probe (two HTTP probes using loaded token)

    Returns list of error strings (empty = all checks passed).
    Privacy gate failure returns immediately — token is NOT loaded and no
    HTTP probe is attempted.
    """
    verifier_arg = getattr(args, "privacy_verifier", None)
    source_arg = getattr(args, "privacy_source", None)
    ssh_fixture_arg = getattr(args, "ssh_fixture", None)
    live_host = getattr(args, "live_host", None)
    ssh_user = getattr(args, "ssh_user", None) or "root"
    receiver_url = getattr(args, "receiver_url", None)
    token_path_arg = getattr(args, "token_path", None)

    # ── 1a. Verify verifier and source present ────────────────────────────────
    if not verifier_arg:
        return ["--privacy-verifier required for mutation operations"]
    verifier = Path(verifier_arg)

    if not source_arg:
        return ["--privacy-source required for mutation operations"]
    source = Path(source_arg)

    # ── 1b. Production: enforce exact fixed parameters BEFORE any token/HTTP ──
    if not is_isolation:
        # Verifier must be one of the two trusted paths (resolved)
        verifier_resolved = verifier.resolve()
        trusted_resolved = {p.resolve() for p in _PROD_VERIFIER_TO_SOURCE}
        if verifier_resolved not in trusted_resolved:
            return [
                "production privacy verifier must be the exact trusted verifier path; "
                "supplied verifier is not trusted"
            ]
        # Find matching trusted key
        matching_verifier = next(
            p for p in _PROD_VERIFIER_TO_SOURCE if p.resolve() == verifier_resolved
        )
        expected_source = _PROD_VERIFIER_TO_SOURCE[matching_verifier]
        if source.resolve() != expected_source.resolve():
            return [
                "production privacy source must come from the same repo as the verifier; "
                "supplied source path is not trusted"
            ]
        # SSH host and user
        if live_host != _PROD_LIVE_HOST:
            return [
                f"production --live-host must be {_PROD_LIVE_HOST!r}; "
                f"supplied value is not trusted"
            ]
        if ssh_user != _PROD_SSH_USER:
            return [
                f"production --ssh-user must be {_PROD_SSH_USER!r}; "
                f"supplied value is not trusted"
            ]
        # Receiver URL must be exact
        if not receiver_url:
            return ["--receiver-url required for mutation operations"]
        if receiver_url not in _PROD_RECEIVER_URLS:
            return [
                "production --receiver-url must be the exact trusted receiver URL; "
                "supplied URL is not trusted"
            ]
        # Token path must not be overridden in production
        if token_path_arg and Path(token_path_arg).resolve() != _PROD_TOKEN.resolve():
            return [
                "production --token-path must not override the production token path"
            ]

    # ── 1b2. Isolation: enforce trusted verifier paths ────────────────────────
    if is_isolation:
        _iso_verifier_resolved = verifier.resolve()
        _trusted_iso_verifiers = {
            _PROD_VERIFIER_PRIMARY.resolve(),
            _PROD_VERIFIER_WORKTREE.resolve(),
        }
        if _iso_verifier_resolved not in _trusted_iso_verifiers:
            return [
                "isolation privacy verifier must be one of the trusted verifier paths; "
                "supplied verifier is not trusted"
            ]

    # ── 1c. Validate verifier and source existence + security ─────────────────
    verifier_errs = _check_regular_file_secure(verifier, "privacy verifier")
    if verifier_errs:
        return verifier_errs
    source_errs = _check_regular_file_secure(source, "privacy source")
    if source_errs:
        return source_errs

    # ── 1d. Validate isolation vs production gate flags ───────────────────────
    if is_isolation:
        if not ssh_fixture_arg:
            return ["isolation mode: --ssh-fixture required for privacy gate"]
        ssh_fixture = Path(ssh_fixture_arg)
        if isolation_root and not _under_root(ssh_fixture, isolation_root):
            return [
                f"--ssh-fixture must be inside --isolation-root: {ssh_fixture}"
            ]
        fix_errs = _check_regular_file_secure(ssh_fixture, "ssh fixture")
        if fix_errs:
            return fix_errs
        # --privacy-source must be inside isolation root
        if isolation_root and not _under_root(source, isolation_root):
            return [
                f"--privacy-source must be inside --isolation-root in isolation mode: {source}"
            ]
        if live_host:
            return ["--live-host is not permitted in isolation mode; use --ssh-fixture"]
    else:
        # Production
        if ssh_fixture_arg:
            return ["--ssh-fixture is not permitted in production mode; use --live-host"]
        if not live_host:
            return ["production mode: --live-host required for privacy gate live check"]
        ssh_fixture = None

    # ── 1e. Run privacy gate (before token load) ──────────────────────────────
    gate_errs = _run_privacy_gate(
        verifier, source,
        live_host=live_host if not is_isolation else None,
        ssh_user=ssh_user,
        ssh_fixture=ssh_fixture if is_isolation else None,
    )
    if gate_errs:
        return gate_errs  # token NOT loaded, no HTTP probe

    # ── 1f. Validate receiver URL format before token load ────────────────────
    if not receiver_url:
        return ["--receiver-url required for mutation operations"]
    from urllib.parse import urlparse as _urlparse
    try:
        _pu = _urlparse(receiver_url)
        _sch = (_pu.scheme or "").lower()
        _pu_path = _pu.path or ""
        _ = _pu.port  # raises ValueError for out-of-range ports
    except Exception:
        return ["receiver URL is malformed or has an invalid port"]
    if _pu.username or _pu.password:
        return ["receiver URL must not contain userinfo (credentials in URL rejected)"]
    if _pu.query:
        return ["receiver URL must not contain a query string"]
    if _pu.fragment:
        return ["receiver URL must not contain a fragment"]
    if _pu_path not in ("", "/"):
        return [f"receiver URL must use root path only, got path: {_pu_path!r}"]
    if is_isolation:
        if _sch != "http":
            return [f"isolation receiver URL must use http scheme, got: {_sch!r}"]
    else:
        if _sch != "https":
            return [f"production receiver URL must be https://, got scheme: {_sch!r}"]

    # ── 2. Load token (only after gate passes) ────────────────────────────────
    if is_isolation:
        if not token_path_arg:
            return ["isolation mode: --token-path required"]
        token_path = Path(token_path_arg)
        if isolation_root and not _under_root(token_path, isolation_root):
            return [f"--token-path must be inside --isolation-root: {token_path}"]
    else:
        token_path = _PROD_TOKEN

    token, tok_errs = _load_token_secure(token_path)
    if tok_errs:
        return tok_errs

    # ── 3. Receiver probe (after token load) ─────────────────────────────────
    if not receiver_url:
        return ["--receiver-url required for mutation operations"]

    probe_errs = _probe_receiver_readiness(receiver_url, token, is_isolation)
    if probe_errs:
        return probe_errs

    return []


def _validate_consent(
    consent_text: str,
    record_id: str,
    version: int,
    meal_name: str,
    confirmer: str,
) -> list:
    """
    Validate raw user consent text against the exact anchored named grammar.

    English:
      I, {confirmer}, confirm the Health Connect entry for {meal_name},
      ID {id}, version {version}, with the shown time and macros.

    Russian:
      Я, {confirmer}, подтверждаю запись Health Connect для {meal_name},
      ID {id}, версия {version}, с показанным временем и макросами.

    Rejects:
    - Negation (not / do not / don't / не)
    - Internal technical affirm phrase
    - Text that does not match either grammar exactly
    - Confirmer mismatch
    - ID prefix/suffix (exact match required)
    - Version prefix/suffix (exact match required)
    - Meal name mismatch

    Returns list of error strings (empty = valid).
    """
    errs = []
    text = consent_text.strip()

    if not text:
        errs.append("consent text is empty")
        return errs

    # Reject negation before any other check
    if _NEGATION_RE.search(text):
        errs.append(
            "consent text contains negation — affirmative consent required"
        )
        return errs

    # Reject internal technical phrase
    if _CONFIRM_AFFIRM.lower() in text.lower():
        errs.append("consent text must not contain the internal technical affirm phrase")
        return errs

    # Require exact anchored grammar
    m = _CONSENT_EN.match(text) or _CONSENT_RU.match(text)
    if not m:
        errs.append(
            'consent text must match exact grammar: '
            '"I, {confirmer}, confirm the Health Connect entry for {meal_name}, '
            'ID {id}, version {version}, with the shown time and macros." '
            '(or the documented Russian equivalent)'
        )
        return errs

    # Exact confirmer match
    if m.group("confirmer") != confirmer:
        errs.append(
            f"consent confirmer {m.group('confirmer')!r} does not match "
            f"--confirmer {confirmer!r}"
        )

    # Exact ID match (grammar anchors it between ", ID " and ", version ")
    if m.group("id") != record_id:
        errs.append(
            f"consent ID {m.group('id')!r} does not match record ID {record_id!r}"
        )

    # Exact version match (grammar captures only digits, anchored)
    if int(m.group("version")) != version:
        errs.append(
            f"consent version {m.group('version')} does not match "
            f"record version {version}"
        )

    # Exact meal name match
    if m.group("meal") != meal_name:
        errs.append(
            f"consent meal name {m.group('meal')!r} does not match "
            f"--meal-name {meal_name!r}"
        )

    return errs


def cmd_draft(args: argparse.Namespace) -> int:
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1

    is_isolation = bool(args.cli)

    # Read JSON input from --file or stdin
    if getattr(args, "file", None):
        try:
            raw = Path(args.file).read_bytes()
        except OSError:
            _emit_err({"error": "cannot read input file"})
            return 1
    else:
        try:
            raw = sys.stdin.buffer.read(_MAX_JSON_INPUT + 1)
        except OSError:
            _emit_err({"error": "cannot read stdin"})
            return 1

    if len(raw) > _MAX_JSON_INPUT:
        _emit_err({"error": "input exceeds maximum size"})
        return 1

    try:
        submitted = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON input"})
        return 1

    # Validate submitted input structure
    import math as _math
    if not isinstance(submitted, dict):
        _emit_err({"error": "draft input must be a JSON object"})
        return 1
    _REQUIRED_FIELDS = ("name", "start_time", "end_time",
                        "energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
    _NUMERIC_FIELDS = ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
    input_errs = []
    for f in _REQUIRED_FIELDS:
        if f not in submitted:
            input_errs.append(f"missing required field: {f}")
    for f in _NUMERIC_FIELDS:
        if f in submitted:
            v = submitted[f]
            if isinstance(v, bool):
                input_errs.append(f"field {f!r} must be a number, not a boolean")
            elif not isinstance(v, (int, float)):
                input_errs.append(f"field {f!r} must be a number")
            elif _math.isnan(v):
                input_errs.append(f"field {f!r} must not be NaN")
    if input_errs:
        _emit_err({"error": "invalid draft input", "details": input_errs})
        return 1

    # Mutation readiness: privacy gate → token → receiver probe
    readiness_errs = _check_mutation_readiness(args, is_isolation, isolation_root)
    if readiness_errs:
        _emit_err({
            "error": "mutation readiness check failed",
            "details": readiness_errs,
        })
        return 1

    # Create draft entry via CLI
    rc, out, err = _run_cli(cli, db, ["create"], stdin_bytes=raw)
    if rc != 0:
        sys.stderr.write(err if err else json.dumps({"error": "create failed"}) + "\n")
        return 1

    try:
        create_out = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI create"})
        return 1
    if not isinstance(create_out, dict):
        _emit_err({"error": "invalid JSON from CLI create: expected object"})
        return 1

    crid = create_out.get("client_record_id")
    if not crid:
        _emit_err({"error": "no client_record_id in CLI create output"})
        return 1

    # Read back the exact written record
    rc2, out2, _ = _run_cli(cli, db, ["status", crid])
    if rc2 != 0:
        _emit_err({"error": "readback failed after create", "created": create_out})
        return 1

    try:
        readback = json.loads(out2)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI status readback"})
        return 1
    if not isinstance(readback, dict):
        _emit_err({"error": "invalid JSON from CLI status readback: expected object"})
        return 1

    # Verify identity fields
    if readback.get("client_record_id") != crid:
        _emit_err({
            "error": "readback id mismatch",
            "expected": crid,
            "got": readback.get("client_record_id"),
        })
        return 1
    if readback.get("client_record_version") != create_out.get("client_record_version"):
        _emit_err({
            "error": "readback version mismatch",
            "expected": create_out.get("client_record_version"),
            "got": readback.get("client_record_version"),
        })
        return 1
    if readback.get("state") != "draft":
        _emit_err({
            "error": "readback state is not draft",
            "got": readback.get("state"),
        })
        return 1

    # Verify exact nutrient and name fields against submitted data
    field_errs = []
    for field in ("name", "start_time", "end_time",
                  "energy_kcal", "protein_g", "carbohydrate_g", "fat_g"):
        rb_val = readback.get(field)
        if rb_val is None:
            field_errs.append(f"readback missing field: {field}")
            continue
        sub_val = submitted.get(field)
        if sub_val is None:
            continue
        if rb_val != sub_val:
            field_errs.append(
                f"readback {field} mismatch: submitted={sub_val!r}, got={rb_val!r}"
            )

    # Verify exact AI description content (prefix + submitted body)
    desc = readback.get("description")
    if desc is None:
        field_errs.append("readback missing field: description")
    else:
        sub_desc = submitted.get("description")
        if sub_desc:
            expected_desc = _AI_DESCRIPTION_PREFIX + " " + str(sub_desc).strip()
        else:
            expected_desc = _AI_DESCRIPTION_PREFIX
        if str(desc) != expected_desc:
            field_errs.append(
                f"readback description mismatch: expected {expected_desc!r}, "
                f"got: {str(desc)[:80]!r}"
            )

    if field_errs:
        _emit_err({"error": "readback field mismatch", "details": field_errs})
        return 1

    print(json.dumps({
        "ok": True,
        "action": "draft",
        "client_record_id": crid,
        "client_record_version": create_out.get("client_record_version"),
        "state": create_out.get("state"),
        "snapshot_hash": _record_snapshot_hash(readback),
        "readback": readback,
    }, indent=2))
    return 0


_STAGE = {"v": "pre"}


def _confirm_with_readback(cli, db, rid, version, meal_name, confirmer, pre_confirm=None,
                           extra_confirm_args=None) -> int:
    """
    Shared confirm path: read back before, verify draft/version/name, optionally
    run pre_confirm(before) -> error str | None (e.g. content-hash check and
    preview claim), confirm via CLI, read back after and verify immutability.
    Prints the success JSON and returns 0, or emits an error and returns 1.
    _STAGE records how far it got: "pre" / "confirm" / "post".
    """
    _STAGE["v"] = "pre"
    # ── Read back BEFORE confirming ───────────────────────────────────────────
    rc, out, _ = _run_cli(cli, db, ["status", rid])
    if rc != 0:
        _emit_err({"error": "cannot read record before confirm"})
        return 1

    try:
        before = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI status (before)"})
        return 1
    if not isinstance(before, dict):
        _emit_err({"error": "invalid JSON from CLI status (before): expected object"})
        return 1

    if before.get("client_record_id") != rid:
        _emit_err({
            "error": "readback id mismatch before confirm",
            "expected": rid,
            "got": before.get("client_record_id"),
        })
        return 1

    state = before.get("state")
    if state != "draft":
        _emit_err({
            "error": "record is not in draft state",
            "state": state,
            "hint": "only draft records can be confirmed",
        })
        return 1

    stored_version = before.get("client_record_version")
    if stored_version != version:
        _emit_err({
            "error": "version mismatch — stale or incorrect consent",
            "consent_version": version,
            "stored_version": stored_version,
            "hint": "read the current record and require new explicit consent",
        })
        return 1

    stored_name = before.get("name")
    if stored_name != meal_name:
        _emit_err({
            "error": "meal name mismatch — consent named a different meal",
            "hint": "re-read the record and require new explicit consent",
        })
        return 1

    if pre_confirm is not None:
        pre_err = pre_confirm(before)
        if pre_err:
            _emit_err({"error": pre_err})
            return 1

    # ── Confirm via CLI ───────────────────────────────────────────────────────
    _STAGE["v"] = "confirm"
    rc2, out2, err2 = _run_cli(cli, db, [
        "confirm", rid,
        "--version", str(version),
        "--affirm", _CONFIRM_AFFIRM,
        "--confirmer", confirmer,
    ] + list(extra_confirm_args or []))
    if rc2 != 0:
        sys.stderr.write(err2 if err2 else json.dumps({"error": "confirm CLI failed"}) + "\n")
        return 1

    try:
        confirm_out = json.loads(out2)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI confirm"})
        return 1
    if not isinstance(confirm_out, dict):
        _emit_err({"error": "invalid JSON from CLI confirm: expected object"})
        return 1

    # ── Read back AFTER confirming ────────────────────────────────────────────
    _STAGE["v"] = "post"
    rc3, out3, _ = _run_cli(cli, db, ["status", rid])
    if rc3 != 0:
        _emit_err({"error": "readback failed after confirm", "confirm_result": confirm_out})
        return 1

    try:
        after = json.loads(out3)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI status (after)"})
        return 1
    if not isinstance(after, dict):
        _emit_err({"error": "invalid JSON from CLI status (after): expected object"})
        return 1

    # Verify readback_after: id, version, state, name, confirmed_by all must match
    after_errs = []
    if after.get("client_record_id") != rid:
        after_errs.append(
            f"readback_after id mismatch: expected {rid!r}, "
            f"got {after.get('client_record_id')!r}"
        )
    if after.get("client_record_version") != version:
        after_errs.append(
            f"readback_after version mismatch: expected {version}, "
            f"got {after.get('client_record_version')!r}"
        )
    if after.get("state") != "confirmed":
        after_errs.append(
            f"readback_after state is not confirmed: got {after.get('state')!r}"
        )
    if after.get("name") != meal_name:
        after_errs.append(
            f"readback_after name mismatch: expected {meal_name!r}, "
            f"got {after.get('name')!r}"
        )
    if after.get("confirmed_by") != confirmer:
        after_errs.append(
            f"readback_after confirmed_by mismatch: expected {confirmer!r}, "
            f"got {after.get('confirmed_by')!r}"
        )
    # Verify immutable fields: full nutrient/time/name/description must be unchanged
    _IMMUTABLE = ("name", "start_time", "end_time", "energy_kcal",
                  "protein_g", "carbohydrate_g", "fat_g", "description")
    for field in _IMMUTABLE:
        before_val = before.get(field)
        after_val = after.get(field)
        if before_val != after_val:
            after_errs.append(
                f"immutable field {field!r} changed after confirm: "
                f"before={before_val!r}, after={after_val!r}"
            )
    if after_errs:
        _emit_err({"error": "readback_after verification failed", "details": after_errs})
        return 1

    # Output state comes from validated readback, not unchecked CLI confirm output
    print(json.dumps({
        "ok": True,
        "action": "confirm",
        "client_record_id": rid,
        "client_record_version": after.get("client_record_version"),
        "state": after.get("state"),
        "confirmed_by": after.get("confirmed_by"),
        "confirmed_at": after.get("confirmed_at"),
        "readback_before": before,
        "readback_after": after,
    }, indent=2))
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1

    is_isolation = bool(args.cli)

    # Require exactly one consent source
    if not getattr(args, "consent_text", None) and not getattr(args, "consent_file", None):
        _emit_err({
            "error": "explicit user consent is required",
            "hint": (
                "provide --consent-text TEXT or --consent-file PATH with the "
                "exact named consent phrase"
            ),
        })
        return 1

    # Load consent text
    if getattr(args, "consent_file", None):
        try:
            consent_text = Path(args.consent_file).read_text(encoding="utf-8").strip()
        except OSError:
            _emit_err({"error": "cannot read consent file"})
            return 1
    else:
        consent_text = args.consent_text or ""

    # Validate consent BEFORE any mutation or readiness check
    consent_errs = _validate_consent(
        consent_text, args.id, args.version, args.meal_name, args.confirmer
    )
    if consent_errs:
        _emit_err({
            "error": "explicit user consent text rejected",
            "reasons": consent_errs,
            "hint": (
                'Provide consent matching: "I, {confirmer}, confirm the Health '
                'Connect entry for {meal_name}, ID {id}, version {version}, '
                'with the shown time and macros."'
            ),
        })
        return 1

    # Mutation readiness: privacy gate → token → receiver probe
    readiness_errs = _check_mutation_readiness(args, is_isolation, isolation_root)
    if readiness_errs:
        _emit_err({
            "error": "mutation readiness check failed",
            "details": readiness_errs,
        })
        return 1

    return _confirm_with_readback(
        cli, db, args.id, args.version, args.meal_name, args.confirmer
    )


# ── Short final-preview consent ──────────────────────────────────────────────
#
# Trust boundary: the pending-preview file and the transport-reply file are both
# written by the trusted agent workflow (the same OS user). They are bound,
# private (0700/0600, no symlinks) and cross-checked, but a local JSON file is
# NOT proof of server-authenticated provenance: whoever can write it as this user
# can forge a reply. Results therefore carry provenance_level
# "workflow_attested". Independently authenticated provenance would need the
# messaging gateway to sign the reply record; that is not implemented here.

_PREVIEW_SCHEMA = 1
_PREVIEW_DIRNAME = "pending-previews"
_PREVIEW_DEFAULT_TTL = 900
_PREVIEW_MAX_TTL = 3600
_PREVIEW_CLOCK_SKEW = 60  # seconds a preview timestamp may be ahead of now
_PROVENANCE_LEVEL = "workflow_attested"
_SNAPSHOT_FIELDS = ("name", "start_time", "end_time",
                    "energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
_MACRO_FIELDS = ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
_SHORT_AFFIRMATIVES = frozenset({
    "да", "запиши", "да, запиши", "да запиши", "yes", "record it",
})
_SHORT_MAX_LEN = 40
# Conditional / hedging words: rejected with a clear reason even though the
# exact whitelist already excludes them.
_CONDITIONAL_RE = re.compile(r"\b(if|but|maybe|unless)\b|(?<!\w)(если|но|может|потом)(?!\w)",
                             re.IGNORECASE)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_ts(value, label: str):
    """Parse an ISO-8601 timestamp that carries a timezone. Returns (dt, err)."""
    from datetime import datetime, timezone
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be an ISO-8601 timestamp string"
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, f"{label} is not a valid ISO-8601 timestamp"
    if dt.tzinfo is None:
        return None, f"{label} must include a timezone offset"
    return dt.astimezone(timezone.utc), None


def _norm_id(value) -> str | None:
    """Transport identifiers: str or int (never bool); '' for absent thread."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip()


def _snapshot_of(record: dict) -> dict | None:
    snap = {}
    for f in _SNAPSHOT_FIELDS:
        v = record.get(f)
        if v is None:
            return None
        if f in _MACRO_FIELDS:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                return None
            v = float(v)
        snap[f] = v
    # The AI description is part of what gets written, so bind it too.
    snap["description"] = record.get("description")
    return snap


def _snapshot_hash(snap: dict) -> str:
    import hashlib
    canon = json.dumps(snap, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _record_snapshot_hash(record: dict) -> str | None:
    snap = _snapshot_of(record)
    return _snapshot_hash(snap) if snap is not None else None


def _check_private_dir(d: Path, label: str) -> list:
    errs = _check_path_no_symlink_ancestor(d, label)
    if errs:
        return errs
    if not d.is_dir():
        return [f"{label} must be a directory: {d}"]
    try:
        st = d.stat()
    except OSError as exc:
        return [f"cannot stat {label}: {exc}"]
    if st.st_uid != os.getuid():
        return [f"{label} must be owned by the current user: {d}"]
    if (st.st_mode & 0o777) != 0o700:
        return [f"{label} must have 0700 permissions: {d}"]
    return []


def _preview_dir(isolation_root: Path | None, create: bool) -> tuple:
    """Return (dir, errors). Parent must already be secure; dir is created 0700."""
    base = isolation_root if isolation_root is not None else _PROD_BASE
    d = base / _PREVIEW_DIRNAME
    if _has_symlink_ancestor(d):
        return None, [f"{_PREVIEW_DIRNAME} path must not have a symlink in any ancestor"]
    if not d.exists():
        if not create:
            return d, []
        if not base.is_dir():
            return None, [f"preview base directory missing: {base}"]
        try:
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
        except OSError as exc:
            return None, [f"cannot create preview directory: {exc}"]
    errs = _check_private_dir(d, "pending-preview directory")
    return (None, errs) if errs else (d, [])


class _PreviewLock:
    """Exclusive flock on <dir>/.lock serialising register/claim/release."""

    def __init__(self, d: Path):
        self._path = d / ".lock"
        self._fd = None

    def __enter__(self):
        import fcntl
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self._fd = fd
        return self

    def __exit__(self, *exc):
        import fcntl
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        return False


def _write_preview(path: Path, data: dict, exclusive: bool) -> None:
    """Atomic durable write: tmp (O_EXCL, 0600) + fsync + rename (or link if exclusive)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        if exclusive:
            os.link(str(tmp), str(path))  # fails if path exists
        else:
            os.replace(str(tmp), str(path))
    finally:
        try:
            os.unlink(str(tmp))
        except FileNotFoundError:
            pass
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _load_preview(path: Path) -> tuple:
    errs = _check_regular_file_secure(path, "pending preview", need_mode=0o600)
    if errs:
        return None, errs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ["pending preview unreadable"]
    if not isinstance(data, dict) or data.get("schema") != _PREVIEW_SCHEMA:
        return None, ["pending preview has unsupported schema"]
    return data, []


def _iter_previews(d: Path):
    """Yield (path, data) for every readable preview; unreadable ones are skipped
    but reported via data=None so callers can fail closed on ambiguity."""
    for p in sorted(d.glob("preview-*.json")):
        data, _ = _load_preview(p)
        yield p, data


def _unreadable_previews(d: Path) -> list:
    """Names of preview-like entries that cannot be securely loaded (fail closed)."""
    bad = []
    for p in sorted(d.iterdir()):
        n = p.name
        if n in (".lock",) or (n.startswith(".") and n.endswith(".tmp")):
            continue
        if not (n.startswith("preview-") and n.endswith(".json")):
            bad.append(n)
            continue
        data, _ = _load_preview(p)
        if data is None:
            bad.append(n)
    return bad


def _read_record(cli, db, rid) -> tuple:
    rc, out, _ = _run_cli(cli, db, ["status", rid])
    if rc != 0:
        return None, "cannot read record"
    try:
        rec = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None, "invalid JSON from CLI status"
    if not isinstance(rec, dict) or rec.get("client_record_id") != rid:
        return None, "readback id mismatch"
    return rec, None


def cmd_preview(args: argparse.Namespace) -> int:
    """Register a durable pending preview for an existing draft (no queue mutation)."""
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1

    fields = {
        "user_id": _norm_id(args.user_id),
        "chat_id": _norm_id(args.chat_id),
        "thread_id": _norm_id(args.thread_id),
        "preview_message_id": _norm_id(args.preview_message_id),
    }
    if not fields["user_id"] or not fields["chat_id"] or not fields["preview_message_id"]:
        _emit_err({"error": "--user-id, --chat-id and --preview-message-id are required"})
        return 1
    if fields["thread_id"] is None:
        _emit_err({"error": "invalid --thread-id"})
        return 1
    if not args.confirmer or not args.confirmer.strip():
        _emit_err({"error": "--confirmer is required"})
        return 1
    ttl = args.ttl_seconds
    if not (1 <= ttl <= _PREVIEW_MAX_TTL):
        _emit_err({"error": f"--ttl-seconds must be between 1 and {_PREVIEW_MAX_TTL}"})
        return 1
    sent_at, err = _parse_ts(args.preview_sent_at, "--preview-sent-at")
    if err:
        _emit_err({"error": err})
        return 1
    now = _now()
    from datetime import timedelta
    if (sent_at - now).total_seconds() > _PREVIEW_CLOCK_SKEW:
        _emit_err({"error": "preview timestamp is in the future"})
        return 1
    expires = sent_at + timedelta(seconds=ttl)
    if expires <= now:
        _emit_err({"error": "preview already expired"})
        return 1

    # The caller supplies the version and canonical snapshot hash of the EXACT
    # draft it displayed; the live record must still match both.
    exp_version = args.expected_version
    exp_hash = (args.expected_snapshot_hash or "").strip().lower()
    if isinstance(exp_version, bool) or not isinstance(exp_version, int) or exp_version < 1:
        _emit_err({"error": "--expected-version must be a positive integer"})
        return 1
    if len(exp_hash) != 64 or any(c not in "0123456789abcdef" for c in exp_hash):
        _emit_err({"error": "--expected-snapshot-hash must be a 64-char hex SHA-256"})
        return 1

    def _verify_live():
        rec, rerr = _read_record(cli, db, args.id)
        if rerr:
            return None, None, rerr
        if rec.get("state") != "draft":
            return None, None, "record is not in draft state"
        v = rec.get("client_record_version")
        if isinstance(v, bool) or not isinstance(v, int):
            return None, None, "record has no integer version"
        if v != exp_version:
            return None, None, "record version differs from the displayed preview version"
        sn = _snapshot_of(rec)
        if sn is None:
            return None, None, "record is missing name/time/macro fields"
        if _snapshot_hash(sn) != exp_hash:
            return None, None, "record content differs from the displayed preview content"
        return v, sn, None

    version, snap, verr = _verify_live()
    if verr:
        _emit_err({"error": verr})
        return 1

    d, derrs = _preview_dir(isolation_root, create=True)
    if derrs:
        _emit_err({"error": "pending-preview store insecure", "details": derrs})
        return 1

    import secrets
    preview_id = secrets.token_hex(16)
    data = {
        "schema": _PREVIEW_SCHEMA,
        "preview_id": preview_id,
        "status": "pending",
        "client_record_id": args.id,
        "client_record_version": version,
        "snapshot": snap,
        "snapshot_hash": _snapshot_hash(snap),
        "confirmer": args.confirmer.strip(),
        "created_at": now.isoformat(),
        "preview_sent_at": sent_at.isoformat(),
        "expires_at": expires.isoformat(),
        "provenance_level": _PROVENANCE_LEVEL,
        **fields,
    }
    with _PreviewLock(d):
        if _unreadable_previews(d):
            _emit_err({"error": "pending-preview store has unreadable/insecure entries"})
            return 1
        # Final check inside the lock, immediately before the preview is stored.
        _, _, verr = _verify_live()
        if verr:
            _emit_err({"error": verr})
            return 1
        for p, other in _iter_previews(d):
            if other is None:
                continue
            same_msg = (other.get("chat_id"), other.get("thread_id"),
                        other.get("preview_message_id")) == (
                fields["chat_id"], fields["thread_id"], fields["preview_message_id"])
            if same_msg:
                _emit_err({"error": "a preview is already registered for this message"})
                return 1
        # A new preview for the same record supersedes older pending ones.
        for p, other in _iter_previews(d):
            if (other and other.get("client_record_id") == args.id
                    and other.get("status") == "pending"):
                other["status"] = "superseded"
                other["superseded_by"] = preview_id
                _write_preview(p, other, exclusive=False)
        _write_preview(d / f"preview-{preview_id}.json", data, exclusive=True)

    print(json.dumps({
        "ok": True,
        "action": "preview",
        "preview_id": preview_id,
        "client_record_id": args.id,
        "client_record_version": version,
        "snapshot": snap,
        "snapshot_hash": data["snapshot_hash"],
        "expires_at": data["expires_at"],
    }, indent=2, ensure_ascii=False))
    return 0


def _load_transport(path_arg) -> tuple:
    """Load the raw user reply transport record. Returns (dict, errors)."""
    p = Path(path_arg)
    errs = _check_regular_file_secure(p, "transport reply file", need_mode=0o600)
    if errs:
        return None, errs
    try:
        if p.stat().st_size > _MAX_JSON_INPUT:
            return None, ["transport reply file too large"]
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ["transport reply file unreadable"]
    if not isinstance(raw, dict):
        return None, ["transport reply must be a JSON object"]
    out = {
        "sender_id": _norm_id(raw.get("sender_id")),
        "chat_id": _norm_id(raw.get("chat_id")),
        "thread_id": _norm_id(raw.get("thread_id")),
        "message_id": _norm_id(raw.get("message_id")),
        "reply_to_message_id": _norm_id(raw.get("reply_to_message_id")),
    }
    for k in ("sender_id", "chat_id", "message_id", "reply_to_message_id"):
        if not out[k]:
            return None, [f"transport reply missing {k}"]
    if out["thread_id"] is None:
        return None, ["transport reply has invalid thread_id"]
    text = raw.get("text")
    if not isinstance(text, str):
        return None, ["transport reply missing text"]
    out["text"] = text
    ts, terr = _parse_ts(raw.get("timestamp"), "transport timestamp")
    if terr:
        return None, [terr]
    out["timestamp"] = ts
    return out, []


def _validate_short_reply(text: str) -> list:
    if len(text) > _SHORT_MAX_LEN:
        return ["short reply too long; only a bare affirmative is accepted"]
    t = text.strip()
    if not t:
        return ["reply is empty"]
    if _NEGATION_RE.search(t):
        return ["reply contains negation — affirmative required"]
    if _CONDITIONAL_RE.search(t):
        return ["reply is conditional — unconditional affirmative required"]
    norm = " ".join(t.casefold().rstrip(".!").split())
    if norm not in _SHORT_AFFIRMATIVES:
        return ["reply is not a recognised short affirmative"]
    return []


def _claim_preview(d: Path, path: Path, transport: dict, expect: dict) -> str | None:
    """
    Atomically mark pending -> consuming under the lock. Returns error or None.

    Everything that made the selected preview valid is re-verified HERE, under
    the lock: store ambiguity, that the reply target still maps to exactly this
    one file, that the file is byte-for-byte (field-for-field) the record that was
    validated, and that this raw reply was never claimed before.
    """
    with _PreviewLock(d):
        if _unreadable_previews(d):
            return "pending-preview store has unreadable/insecure entries; failing closed"
        key = (transport["chat_id"], transport["thread_id"], transport["reply_to_message_id"])
        same_target = [
            p for p, data in _iter_previews(d)
            if data is not None and (data.get("chat_id"), data.get("thread_id"),
                                     data.get("preview_message_id")) == key
        ]
        if same_target != [path]:
            return "ambiguous or changed preview set for this reply target"
        cur, errs = _load_preview(path)
        if errs or cur is None:
            return "pending preview unreadable at claim"
        if cur.get("status") != "pending":
            return f"preview already {cur.get('status')} (replay rejected)"
        if cur != expect:
            return "preview changed during confirm"
        if transport["message_id"] in (cur.get("used_reply_ids") or []):
            return "this reply was already used (replay rejected)"
        # Expiry is judged under the lock too: the caller may have waited for flock.
        exp, eerr = _parse_ts(cur.get("expires_at"), "expires_at")
        if eerr or exp is None or _now() > exp:
            return "preview expired"
        cur["status"] = "consuming"
        cur["claimed_at"] = _now().isoformat()
        cur["reply_message_id"] = transport["message_id"]
        _write_preview(path, cur, exclusive=False)
    return None


def _revoke_if_pending(d: Path, path: Path, expect: dict) -> None:
    """Revoke a still-pending, unchanged preview (stale authorization)."""
    with _PreviewLock(d):
        cur, errs = _load_preview(path)
        if errs or cur is None or cur.get("status") != "pending" or cur != expect:
            return
        cur["status"] = "revoked"
        cur["finished_at"] = _now().isoformat()
        _write_preview(path, cur, exclusive=False)


def _finish_preview(d: Path, path: Path, status: str) -> None:
    with _PreviewLock(d):
        cur, errs = _load_preview(path)
        if errs or cur is None or cur.get("status") != "consuming":
            return
        if status == "pending" and cur.get("mode") == _PERSONAL_MODE:
            # Personal mode: remember the consumed consent FILE key (not a message id).
            used = list(cur.get("used_consent_file_keys") or [])
            if cur.get("consent_file_key") is not None:
                used.append(cur.pop("consent_file_key"))
            cur["used_consent_file_keys"] = used
            cur.pop("claimed_at", None)
        elif status == "pending":
            # Released after a failed confirm: remember the consumed raw reply so it
            # can never be replayed; a NEW actual reply is required.
            used = list(cur.get("used_reply_ids") or [])
            if cur.get("reply_message_id") is not None:
                used.append(cur["reply_message_id"])
            cur["used_reply_ids"] = used
            cur.pop("claimed_at", None)
        cur["status"] = status
        cur["finished_at"] = _now().isoformat()
        _write_preview(path, cur, exclusive=False)


def _confirm_claimed_preview(cli, db, d, path, pv, snap, rid, version, expires,
                             claim_fn) -> int:
    """Shared claim -> CAS confirm -> consume/release/revoke tail for pending previews."""
    claimed = {"v": False}

    def _pre_confirm(before: dict):
        live = _snapshot_of(before)
        if live is None or _snapshot_hash(live) != pv["snapshot_hash"]:
            _revoke_if_pending(d, path, pv)  # drift kills the consent for good
            return "record content differs from the previewed content — new preview required"
        # Expiry is re-checked right before the claim (expires=None: no time-only
        # expiry, used by personal-chat previews).
        if expires is not None and _now() > expires:
            return "preview expired"
        cerr = claim_fn()
        if cerr:
            return cerr
        claimed["v"] = True
        return None

    _STAGE["v"] = "pre"
    rc = _confirm_with_readback(
        cli, db, rid, version, snap["name"], pv["confirmer"], pre_confirm=_pre_confirm,
        # The queue verifies this INSIDE its confirm transaction (before any mutation).
        extra_confirm_args=["--expected-snapshot-hash", pv["snapshot_hash"]],
    )
    if not claimed["v"] and rc != 0 and _STAGE["v"] == "pre":
        # Rejected before the claim: if the draft drifted (state/version/content),
        # this preview is stale and must not revive if the content is restored.
        rec, _ = _read_record(cli, db, rid)
        live = _snapshot_of(rec) if rec else None
        if rec and (rec.get("state") != "draft"
                    or rec.get("client_record_version") != version
                    or live is None or _snapshot_hash(live) != pv["snapshot_hash"]):
            _revoke_if_pending(d, path, pv)
    if claimed["v"]:
        if rc == 0:
            _finish_preview(d, path, "consumed")
        elif _STAGE["v"] == "confirm":
            # CLI refused/failed: release only if the draft is verifiably untouched.
            rec, _ = _read_record(cli, db, rid)
            live = _snapshot_of(rec) if rec else None
            if (rec and rec.get("state") == "draft"
                    and rec.get("client_record_version") == version
                    and live is not None
                    and _snapshot_hash(live) == pv["snapshot_hash"]):
                # Verifiably untouched (same version AND same content): safe to release.
                _finish_preview(d, path, "pending")
            else:
                # Changed content/version/state: authorization is revoked for good.
                _finish_preview(d, path, "revoked")
        # Any other outcome (crash, post-readback failure) stays "consuming":
        # fail closed — a fresh preview and reply are required.
    return rc


def cmd_short_confirm(args: argparse.Namespace) -> int:
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    is_isolation = bool(args.cli)

    transport, terrs = _load_transport(args.transport_file)
    if terrs:
        _emit_err({"error": "transport reply rejected", "details": terrs})
        return 1

    reply_errs = _validate_short_reply(transport["text"])
    if reply_errs:
        _emit_err({"error": "short reply rejected", "reasons": reply_errs})
        return 1

    d, derrs = _preview_dir(isolation_root, create=False)
    if derrs:
        _emit_err({"error": "pending-preview store insecure", "details": derrs})
        return 1
    if not d.is_dir():
        _emit_err({"error": "no pending preview"})
        return 1

    # Any unreadable/insecure/corrupt/foreign entry makes the store ambiguous.
    if _unreadable_previews(d):
        _emit_err({"error": "pending-preview store has unreadable/insecure entries; "
                            "failing closed"})
        return 1

    # Locate the preview ONLY via the reply target; the caller never names a queue ID.
    matches = []
    for p, data in _iter_previews(d):
        if data is None:
            continue
        if (data.get("chat_id"), data.get("thread_id"),
                data.get("preview_message_id")) == (
                transport["chat_id"], transport["thread_id"],
                transport["reply_to_message_id"]):
            matches.append((p, data))
    if not matches:
        _emit_err({"error": "no pending preview matches this reply target"})
        return 1
    if len(matches) > 1:
        _emit_err({"error": "ambiguous preview: multiple previews match this reply target"})
        return 1
    path, pv = matches[0]

    errs = []
    if pv.get("status") != "pending":
        errs.append(f"preview is {pv.get('status')}, not pending (replay or superseded)")
    if transport["message_id"] in (pv.get("used_reply_ids") or []):
        errs.append("this reply was already used (replay rejected)")
    if pv.get("provenance_level") != _PROVENANCE_LEVEL:
        errs.append("preview has unexpected provenance level")
    if transport["sender_id"] != pv.get("user_id"):
        errs.append("reply sender is not the user the preview was addressed to")
    if transport["message_id"] == pv.get("preview_message_id"):
        errs.append("reply message id equals the preview message id")
    sent_at, e1 = _parse_ts(pv.get("preview_sent_at"), "preview_sent_at")
    expires, e2 = _parse_ts(pv.get("expires_at"), "expires_at")
    if e1 or e2:
        errs.append("preview timestamps invalid")
    else:
        if transport["timestamp"] <= sent_at:
            errs.append("reply does not postdate the preview")
        if transport["timestamp"] > expires or _now() > expires:
            errs.append("preview expired")
    snap = pv.get("snapshot")
    if (not isinstance(snap, dict) or _snapshot_hash(snap) != pv.get("snapshot_hash")):
        errs.append("preview snapshot does not match its hash (tampered)")
    rid = pv.get("client_record_id")
    version = pv.get("client_record_version")
    if (not isinstance(rid, str) or not rid or isinstance(version, bool)
            or not isinstance(version, int)):
        errs.append("preview has invalid record binding")
    if errs:
        _emit_err({"error": "short consent rejected", "reasons": errs})
        return 1

    # Same readiness gates as long consent (privacy → token → receiver).
    readiness_errs = _check_mutation_readiness(args, is_isolation, isolation_root)
    if readiness_errs:
        _emit_err({"error": "mutation readiness check failed", "details": readiness_errs})
        return 1

    return _confirm_claimed_preview(
        cli, db, d, path, pv, snap, rid, version, expires,
        lambda: _claim_preview(d, path, transport, pv))


# ── Personal-chat short consent (agent-conversation-attested) ────────────────
#
# Weaker provenance than receipt-bound short consent: there is NO Telegram
# message ID, receipt or server-side linkage. The agent stores ONE active preview
# per declared personal scope (an opaque label for a one-to-one chat), displays
# exactly the stored text, and later hands the wrapper the user's raw reply via a
# private consent file after deciding from the conversation that it is an
# unambiguous yes to that preview. The wrapper only (a) accepts a bare whitelisted
# affirmative, (b) selects the single active preview of the scope, and (c) enforces
# version + full-snapshot CAS in the queue transaction. It cannot prove who wrote
# the file; results carry provenance_level "agent_conversation_attested".

_PERSONAL_DIRNAME = "personal-previews"
_PERSONAL_MODE = "personal_chat_scope"
_PERSONAL_PROVENANCE = "agent_conversation_attested"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CONSENT_FILE_MAX = 256
_NOTES_MAX = 2048
_PERSONAL_PROMPT = "Записать или поправишь?"


def _personal_dir(isolation_root: Path | None, create: bool) -> tuple:
    base = isolation_root if isolation_root is not None else _PROD_BASE
    d = base / _PERSONAL_DIRNAME
    if _has_symlink_ancestor(d):
        return None, [f"{_PERSONAL_DIRNAME} path must not have a symlink in any ancestor"]
    if not d.exists():
        if not create:
            return d, []
        if not base.is_dir():
            return None, [f"preview base directory missing: {base}"]
        try:
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
        except OSError as exc:
            return None, [f"cannot create personal-preview directory: {exc}"]
    errs = _check_private_dir(d, "personal-preview directory")
    return (None, errs) if errs else (d, [])


def _personal_scope_args(args) -> tuple:
    scope = getattr(args, "scope", None)
    if not isinstance(scope, str) or _SCOPE_RE.fullmatch(scope) is None:
        return None, "--scope must be an opaque label of 1-64 chars [A-Za-z0-9._:-]"
    if not getattr(args, "attest_personal_chat", False):
        return None, ("--attest-personal-chat is required: declares this scope is a "
                      "private one-to-one chat (not a group/platform channel)")
    return scope, None


def _read_private_text(path_arg, label: str, max_bytes: int) -> tuple:
    """Read a 0600 regular file as exact UTF-8 text (unchanged). Returns (text, st, err)."""
    p = Path(path_arg)
    errs = _check_regular_file_secure(p, label, need_mode=0o600)
    if errs:
        return None, None, errs[0]
    try:
        st = p.stat()
        if st.st_size > max_bytes:
            return None, None, f"{label} too large"
        raw = p.read_bytes()
        return raw.decode("utf-8"), st, None
    except (OSError, UnicodeDecodeError):
        return None, None, f"{label} unreadable"


def _fmt_num(v: float) -> str:
    """Exact, round-trip numeric rendering (no rounding of stored values)."""
    return str(int(v)) if v == int(v) else repr(float(v))


def _canonical_summary(snap: dict) -> str:
    """Wrapper-produced summary of the ACTUAL saved name, macros and start/end times."""
    return (f"{snap['name']}: {_fmt_num(snap['energy_kcal'])} ккал, "
            f"белки {_fmt_num(snap['protein_g'])} г, "
            f"углеводы {_fmt_num(snap['carbohydrate_g'])} г, "
            f"жиры {_fmt_num(snap['fat_g'])} г; "
            f"время {snap['start_time']} → {snap['end_time']} (технический интервал очереди).")


def _compose_display_legacy(snap: dict, notes: str | None) -> str:
    """Format of previews saved WITHOUT a display_format field (already shown to users)."""
    parts = [_canonical_summary(snap)]
    if notes:
        parts.append(notes)
    parts.append(_PERSONAL_PROMPT)
    return "\n\n".join(parts)


_DISPLAY_FORMAT_COMPACT = 2
_NOTES_COMPACT_MAX_CHARS = 240
_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")
_DISPLAY_TZ = "Europe/Berlin"


def _berlin_range(start_iso: str, end_iso: str) -> str:
    """'14 октября, 09:30–09:45, Берлин' from the saved instants (raises ValueError)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(_DISPLAY_TZ)
    pts = []
    for label, v in (("start_time", start_iso), ("end_time", end_iso)):
        dt, err = _parse_ts(v, label)
        if err:
            raise ValueError(err)
        pts.append(dt.astimezone(tz))
    a, b = pts

    def hm(dt):
        txt = dt.strftime("%H:%M")
        # A wall time that occurs twice (autumn DST change) is disambiguated by offset.
        if dt.replace(fold=0).utcoffset() != dt.replace(fold=1).utcoffset():
            off = int(dt.utcoffset().total_seconds() // 3600)
            txt += f" (UTC{off:+d})"
        return txt

    def day(dt, with_year):
        return f"{dt.day} {_RU_MONTHS[dt.month - 1]}" + (f" {dt.year}" if with_year else "")

    if a.date() == b.date():
        return f"{day(a, False)}, {hm(a)}–{hm(b)}, Берлин"
    with_year = a.year != b.year
    return f"{day(a, with_year)}, {hm(a)} – {day(b, with_year)}, {hm(b)}, Берлин"


def _compose_display_compact(snap: dict, notes: str | None) -> str:
    lines = [
        snap["name"],
        f"≈{_fmt_num(snap['energy_kcal'])} ккал · Б {_fmt_num(snap['protein_g'])} · "
        f"Ж {_fmt_num(snap['fat_g'])} · У {_fmt_num(snap['carbohydrate_g'])} г",
    ]
    if notes:
        lines.append(notes)
    lines.append(_berlin_range(snap["start_time"], snap["end_time"]))
    lines.append("Записать?")
    return "\n".join(lines)


def _compose_display(snap: dict, notes: str | None, fmt=_DISPLAY_FORMAT_COMPACT) -> str:
    """Compose by saved display format; unknown formats raise (callers fail closed)."""
    if fmt is None:
        return _compose_display_legacy(snap, notes)
    if fmt == _DISPLAY_FORMAT_COMPACT and type(fmt) is int:
        return _compose_display_compact(snap, notes)
    raise ValueError(f"unknown display format {fmt!r}")


def _validate_notes_compact(notes: str) -> str | None:
    """Short assumptions only: one line, bounded, rejected (never truncated)."""
    if not notes.strip():
        return "notes file is empty (omit --notes-file instead)"
    if any(ord(c) < 32 for c in notes) or "\x7f" in notes:
        return "notes must be a single line without control characters"
    if len(notes) > _NOTES_COMPACT_MAX_CHARS:
        return f"notes too long ({len(notes)} > {_NOTES_COMPACT_MAX_CHARS} chars); keep assumptions short"
    return None


def _validate_notes(notes: str) -> str | None:
    if not notes.strip():
        return "notes file is empty (omit --notes-file instead)"
    if any(ord(c) < 32 and c not in "\n\t" for c in notes) or "\x7f" in notes:
        return "notes contain control characters"
    return None


def _hex64(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None


def _personal_record_errors(pv: dict, scope: str) -> list:
    """Structural + integrity validation of a stored personal preview. Never raises."""
    errs = []
    if pv.get("mode") != _PERSONAL_MODE or pv.get("personal_chat_attested") is not True:
        errs.append("preview is not a personal-chat preview")
    if pv.get("provenance_level") != _PERSONAL_PROVENANCE:
        errs.append("preview has unexpected provenance level")
    if pv.get("scope") != scope:
        errs.append("preview scope mismatch")
    pid = pv.get("preview_id")
    if not isinstance(pid, str) or re.fullmatch(r"[0-9a-f]{32}", pid) is None:
        errs.append("preview id malformed")
    snap = pv.get("snapshot")
    if not isinstance(snap, dict) or _snapshot_of(snap) != snap:
        errs.append("preview snapshot malformed")
    elif not _hex64(pv.get("snapshot_hash")) or _snapshot_hash(snap) != pv["snapshot_hash"]:
        errs.append("preview snapshot does not match its hash (tampered)")
    else:
        notes = pv.get("notes_text")
        has_fmt = "display_format" in pv  # absent = legacy preview, validated as before
        fmt = pv.get("display_format")
        check_notes = _validate_notes if not has_fmt else _validate_notes_compact
        try:
            if has_fmt and not (type(fmt) is int and fmt == _DISPLAY_FORMAT_COMPACT):
                raise ValueError("unknown display format")
            expected = (None if notes is not None and (
                not isinstance(notes, str) or check_notes(notes))
                else _compose_display(snap, notes, fmt if has_fmt else None))
        except ValueError:
            errs.append("preview display format unknown or snapshot times invalid")
            expected = False
        if expected is None:
            errs.append("preview notes malformed")
        elif expected is not False:
            disp = pv.get("display_text")
            if disp != expected:
                errs.append("preview display text does not match the saved snapshot (tampered)")
            elif (not _hex64(pv.get("display_text_sha256"))
                  or _sha256_text(disp) != pv["display_text_sha256"]):
                errs.append("preview display text hash mismatch (tampered)")
    if not isinstance(pv.get("confirmer"), str) or not pv["confirmer"]:
        errs.append("preview has no confirmer")
    rid, version = pv.get("client_record_id"), pv.get("client_record_version")
    if (not isinstance(rid, str) or not rid or isinstance(version, bool)
            or not isinstance(version, int) or version < 1):
        errs.append("preview has invalid record binding")
    created, e1 = _parse_ts(pv.get("created_at"), "created_at")
    expires, e2 = _parse_ts(pv.get("expires_at"), "expires_at")
    if e1 or e2:
        errs.append("preview timestamps invalid")
    else:
        ttl = (expires - created).total_seconds()
        if not (0 < ttl <= _PREVIEW_MAX_TTL):
            errs.append("preview lifetime is not within the finite maximum TTL")
        if (created - _now()).total_seconds() > _PREVIEW_CLOCK_SKEW:
            errs.append("preview creation time is in the future")
    wall = pv.get("created_wall_ns")
    if isinstance(wall, bool) or not isinstance(wall, int) or wall <= 0:
        errs.append("preview has no workflow freshness marker")
    return errs


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verify_draft_live(cli, db, rid, exp_version, exp_hash) -> tuple:
    rec, rerr = _read_record(cli, db, rid)
    if rerr:
        return None, None, rerr
    if rec.get("state") != "draft":
        return None, None, "record is not in draft state"
    v = rec.get("client_record_version")
    if isinstance(v, bool) or not isinstance(v, int):
        return None, None, "record has no integer version"
    if v != exp_version:
        return None, None, "record version differs from the displayed preview version"
    sn = _snapshot_of(rec)
    if sn is None:
        return None, None, "record is missing name/time/macro fields"
    if _snapshot_hash(sn) != exp_hash:
        return None, None, "record content differs from the displayed preview content"
    return v, sn, None


# Record kinds in personal-previews/. Meal previews carry NO "kind" field (all
# previews written before kinds existed are meal previews); the others always do.
_KIND_RECIPE = "recipe"
_KIND_KNOWN_LOG = "known_log"


def _scope_records(d: Path, scope: str, kind=None) -> list:
    """Records of one scope and kind (kind None = meal previews)."""
    return [(p, data) for p, data in _iter_previews(d)
            if data is not None and data.get("scope") == scope
            and data.get("mode") == _PERSONAL_MODE and data.get("kind") == kind]


def _scope_pending_any(d: Path, scope: str) -> list:
    """Every pending yes/no question of a scope (meal or recipe preview)."""
    return [(p, data) for p, data in _iter_previews(d)
            if data is not None and data.get("scope") == scope
            and data.get("mode") == _PERSONAL_MODE and data.get("status") == "pending"]


def _is_expired(data: dict) -> bool:
    exp, err = _parse_ts(data.get("expires_at"), "expires_at")
    return bool(err) or exp is None or _now() > exp


def _personal_store_errors(d: Path) -> list:
    """Fail closed on unreadable entries AND on loadable entries that are not
    well-formed personal-mode records (they would otherwise be invisible)."""
    if _unreadable_previews(d):
        return ["personal-preview store has unreadable/insecure entries; failing closed"]
    for _, data in _iter_previews(d):
        if (data is None or data.get("mode") != _PERSONAL_MODE
                or not isinstance(data.get("scope"), str)
                or not isinstance(data.get("status"), str)):
            return ["personal-preview store has foreign/malformed records; failing closed"]
    return []


def _used_consent_keys(d: Path) -> set:
    """Consent/request-file keys ever claimed, across EVERY record (any scope/status/kind)."""
    keys = set()
    for _, data in _iter_previews(d):
        if data is None:
            continue
        for k in ("consent_file_key", "request_file_key", "registration_evidence_key"):
            if isinstance(data.get(k), str):
                keys.add(data[k])
        used = data.get("used_consent_file_keys")
        if isinstance(used, list):
            keys.update(k for k in used if isinstance(k, str))
    return keys


def cmd_personal_preview(args: argparse.Namespace) -> int:
    """Store ONE active personal-chat preview (snapshot + canonical display). No queue mutation."""
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    confirmer = args.confirmer
    if not isinstance(confirmer, str) or not confirmer or confirmer != confirmer.strip():
        _emit_err({"error": "--confirmer must be a non-empty name without surrounding whitespace"})
        return 1
    ttl = args.ttl_seconds
    if not (1 <= ttl <= _PREVIEW_MAX_TTL):
        _emit_err({"error": f"--ttl-seconds must be between 1 and {_PREVIEW_MAX_TTL}"})
        return 1
    exp_version = args.expected_version
    exp_hash = args.expected_snapshot_hash
    if isinstance(exp_version, bool) or not isinstance(exp_version, int) or exp_version < 1:
        _emit_err({"error": "--expected-version must be a positive integer"})
        return 1
    if not _hex64(exp_hash):
        _emit_err({"error": "--expected-snapshot-hash must be a 64-char lowercase hex SHA-256"})
        return 1
    notes = None
    if getattr(args, "notes_file", None):
        notes, _, nerr = _read_private_text(args.notes_file, "notes file", _NOTES_MAX)
        if notes is not None and nerr is None:
            notes = notes.rstrip("\r\n")  # a trailing newline from the editor is not content
            nerr = _validate_notes_compact(notes)
        if nerr:
            _emit_err({"error": nerr})
            return 1

    version, snap, verr = _verify_draft_live(cli, db, args.id, exp_version, exp_hash)
    if verr:
        _emit_err({"error": verr})
        return 1

    d, derrs = _personal_dir(isolation_root, create=True)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1

    import secrets
    import time
    from datetime import timedelta
    now = _now()
    preview_id = secrets.token_hex(16)
    try:
        display = _compose_display(snap, notes, _DISPLAY_FORMAT_COMPACT)
    except ValueError as exc:
        _emit_err({"error": f"cannot render preview: {exc}"})
        return 1
    data = {
        "schema": _PREVIEW_SCHEMA,
        "mode": _PERSONAL_MODE,
        "preview_id": preview_id,
        "status": "pending",
        "scope": scope,
        "personal_chat_attested": True,
        "client_record_id": args.id,
        "client_record_version": version,
        "snapshot": snap,
        "snapshot_hash": _snapshot_hash(snap),
        "notes_text": notes,
        "display_format": _DISPLAY_FORMAT_COMPACT,
        "display_text": display,
        "display_text_sha256": _sha256_text(display),
        "confirmer": confirmer,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        # Local workflow freshness marker (real wall clock, ns). NOT a user timestamp.
        "created_wall_ns": time.time_ns(),
        "provenance_level": _PERSONAL_PROVENANCE,
    }
    with _PreviewLock(d):
        serrs = _personal_store_errors(d)
        if serrs:
            _emit_err({"error": serrs[0]})
            return 1
        _, _, verr = _verify_draft_live(cli, db, args.id, exp_version, exp_hash)
        if verr:
            _emit_err({"error": verr})
            return 1
        # No time-only expiry: every still-pending question of the scope (meal or
        # recipe preview, past its stored expires_at or not) is the active one.
        active = _scope_pending_any(d, scope)
        if len(active) > 1:
            _emit_err({"error": "ambiguous: several active previews exist for this scope"})
            return 1
        # Replacement: the older preview is superseded (its draft is NOT touched);
        # fresh real consent is required for the new one.
        for p, other in active:
            other["status"] = "superseded"
            other["superseded_by"] = preview_id
            other["finished_at"] = now.isoformat()
            _write_preview(p, other, exclusive=False)
        data["created_wall_ns"] = time.time_ns()
        _write_preview(d / f"preview-{preview_id}.json", data, exclusive=True)

    print(json.dumps({
        "ok": True,
        "action": "personal-preview",
        "preview_id": preview_id,
        "scope": scope,
        "client_record_id": args.id,
        "client_record_version": version,
        "snapshot_hash": data["snapshot_hash"],
        "expires_at": data["expires_at"],
        "provenance_level": _PERSONAL_PROVENANCE,
        "superseded_previews": len(active),
        "display_text": display,
    }, indent=2, ensure_ascii=False))
    return 0


def _claim_personal(d: Path, path: Path, scope: str, consent: dict, expect: dict):
    """pending -> consuming under the lock, re-verifying everything that selected it."""
    with _PreviewLock(d):
        serrs = _personal_store_errors(d)
        if serrs:
            return serrs[0]
        pending = [p for p, _ in _scope_pending_any(d, scope)]
        if pending != [path]:
            return "ambiguous or changed preview set for this scope"
        cur, errs = _load_preview(path)
        if errs or cur is None:
            return "pending preview unreadable at claim"
        if cur.get("status") != "pending":
            return f"preview already {cur.get('status')} (replay rejected)"
        if cur != expect:
            return "preview changed during confirm"
        if consent["key"] in _used_consent_keys(d):
            return "this consent file was already used (replay rejected)"
        # No time-only expiry for the unchanged, uniquely active personal preview:
        # the stored expires_at is kept unchanged and only audited.
        cur["late_consent"] = _is_expired(cur)
        cur["original_expires_at"] = cur.get("expires_at")
        cur["status"] = "consuming"
        cur["claimed_at"] = _now().isoformat()  # wrapper clock; NOT a user timestamp
        cur["consent_file_key"] = consent["key"]
        cur["consent_raw_text"] = consent["raw_text"]
        cur["consent_file_sha256"] = consent["sha256"]
        _write_preview(path, cur, exclusive=False)
    return None


def _personal_confirm_impl(args: argparse.Namespace) -> int:
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    is_isolation = bool(args.cli)
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1

    raw, st, cerr = _read_private_text(args.consent_file, "consent file", _CONSENT_FILE_MAX)
    if cerr:
        _emit_err({"error": "consent file rejected", "details": [cerr]})
        return 1
    reply_errs = _validate_short_reply(raw)
    if reply_errs:
        _emit_err({"error": "short reply rejected", "reasons": reply_errs})
        return 1
    sha = _sha256_text(raw)
    consent = {
        "raw_text": raw, "sha256": sha,
        # Internal claim key of this consent FILE (not a transport message id).
        "key": "consentfile:" + _sha256_text(
            f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}:{sha}"),
    }

    d, derrs = _personal_dir(isolation_root, create=False)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    if not d.is_dir():
        _emit_err({"error": "no active preview for this scope"})
        return 1
    serrs = _personal_store_errors(d)
    if serrs:
        _emit_err({"error": serrs[0]})
        return 1

    # A consent file that was ever claimed (any scope/status) can never be reused.
    if consent["key"] in _used_consent_keys(d):
        _emit_err({"error": "this consent file was already used (replay rejected)"})
        return 1

    records = _scope_records(d, scope)
    pending_any = _scope_pending_any(d, scope)
    if len(pending_any) > 1:
        _emit_err({"error": "ambiguous: several active previews exist for this scope"})
        return 1
    pending = [(p, x) for p, x in records if x.get("status") == "pending"]
    if not pending:
        if any(x.get("status") == "consumed" for _, x in records):
            _emit_err({"error": "latest preview already confirmed; no active preview "
                                "(replay rejected)"})
        else:
            _emit_err({"error": "no active preview for this scope"})
        return 1
    # No time-only expiry: the single active preview stays confirmable after its
    # stored expires_at as long as everything else below still matches.
    path, pv = pending[0]

    errs = _personal_record_errors(pv, scope)
    if not errs:
        # Local workflow freshness: a consent file older than this preview cannot be
        # a reply to it. Real file mtime vs the preview's creation marker; this is
        # NOT a user timestamp and is only as trustworthy as the local filesystem.
        if st.st_mtime_ns < pv["created_wall_ns"]:
            errs.append("consent file predates this preview (stale consent)")
    if errs:
        _emit_err({"error": "personal consent rejected", "reasons": errs})
        return 1
    snap, rid, version = pv["snapshot"], pv["client_record_id"], pv["client_record_version"]

    readiness_errs = _check_mutation_readiness(args, is_isolation, isolation_root)
    if readiness_errs:
        _emit_err({"error": "mutation readiness check failed", "details": readiness_errs})
        return 1

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _confirm_claimed_preview(
            cli, db, d, path, pv, snap, rid, version, None,
            lambda: _claim_personal(d, path, scope, consent, pv))
    out = buf.getvalue()
    if rc != 0:
        sys.stdout.write(out)
        return rc
    # rc == 0: the preview is already marked consumed (no replay possible). The helper
    # output must be a genuine confirmed readback; anything else is an uncertain
    # outcome that is reported, never papered over with a synthetic success.
    try:
        res = json.loads(out)
    except ValueError:
        res = None
    if (not isinstance(res, dict) or res.get("ok") is not True
            or res.get("state") != "confirmed" or res.get("client_record_id") != rid
            or res.get("client_record_version") != version):
        _emit_err({"error": "uncertain outcome: confirm helper returned an invalid success "
                            "result; read the record back (status) before any further action",
                   "preview_id": pv["preview_id"], "client_record_id": rid,
                   "preview_state": "consumed (no replay)"})
        return 1
    stored, _ = _load_preview(path)  # audit flag as written at claim time
    stored_late = stored.get("late_consent") if isinstance(stored, dict) else None
    res["action"] = "personal-confirm"
    res["provenance_level"] = _PERSONAL_PROVENANCE
    res["user_status"] = _user_status(res.get("readback_after"))
    res["personal_consent"] = {
        "scope": scope, "preview_id": pv["preview_id"],
        "raw_text": raw, "file_sha256": sha,
        "late_consent": stored_late, "original_expires_at": pv.get("expires_at"),
        "note": "agent-attested from conversation; no Telegram linkage; "
                "no author identity is claimed",
    }
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


# ── User-facing status (canonical, localized) ────────────────────────────────
#
# The ONLY wording the agent should use for a queue record's state. It never
# claims more than the queue knows: "confirmed" is server-side only, "acked" is
# phone-reported, and anything unexpected fails closed (no delivery claim).

_STATUS_TEXT = {
    "draft": "Черновик — ждёт подтверждения",
    "confirmed": "Принято — ожидает синхронизации",
    "acked": "Телефон подтвердил запись в Health Connect",
}
_NOT_WRITTEN_TEXT = "Не записано: {reason}"
_UNKNOWN_STATUS_TEXT = "Статус неизвестен — не считать записанным"
_REPLAY_STATUS_TEXT = "Повтор — ничего нового не записано; проверьте статус записи"
_CANCELLED_REASON = "отменено"
# A record cancelled after the phone may have fetched it cannot be called "not
# written": the protocol has no tombstones.
_CANCELLED_DELIVERY_POSSIBLE_TEXT = ("Отменено, но телефон уже мог получить запись — "
                                     "проверьте Health Connect на телефоне")
_EARLIER_VERSION_NOTE = ("Предыдущая версия уже могла попасть на телефон; "
                         "замена на телефоне не гарантируется.")


def _delivery_possible(rec: dict) -> bool:
    """False only for an explicit 0/False; absent or malformed counts as possible."""
    v = rec.get("delivery_possible")
    return not (v is False or (type(v) is int and v == 0))


def _user_status(rec) -> dict:
    """Canonical user-facing status of a queue readback. Unknown state fails closed."""
    state = rec.get("state") if isinstance(rec, dict) else None
    if isinstance(state, str) and state in _STATUS_TEXT:
        out = {"code": state, "text": _STATUS_TEXT[state], "known": True}
        if state == "draft" and _delivery_possible(rec):
            out["note"] = _EARLIER_VERSION_NOTE
        return out
    if state == "cancelled":
        if _delivery_possible(rec):
            return {"code": "cancelled_delivery_possible",
                    "text": _CANCELLED_DELIVERY_POSSIBLE_TEXT, "known": True}
        return {"code": "cancelled", "known": True,
                "text": _NOT_WRITTEN_TEXT.format(reason=_CANCELLED_REASON)}
    return {"code": "unknown", "text": _UNKNOWN_STATUS_TEXT, "known": False}


def _failed_status(reason: str) -> dict:
    return {"code": "failed", "text": _NOT_WRITTEN_TEXT.format(reason=reason), "known": True}


# Error texts after which the queue MAY already hold a confirmed record.
_UNCERTAIN_MARKERS = ("uncertain", "after confirm", "readback_after", "from cli confirm",
                      "timeout")
_REPLAY_MARKERS = ("replay", "already confirmed", "already used", "already bound")


def _failure_user_status(err_obj: dict) -> dict:
    msg = str(err_obj.get("error", "")).lower()
    if any(m in msg for m in _UNCERTAIN_MARKERS):
        return {"code": "unknown", "text": _UNKNOWN_STATUS_TEXT, "known": False}
    if any(m in msg for m in _REPLAY_MARKERS):
        return {"code": "replay", "text": _REPLAY_STATUS_TEXT, "known": True}
    return _failed_status(str(err_obj.get("error") or "ошибка"))


def _with_failure_status(impl):
    """Run a personal command; on failure attach the canonical user_status to its
    last JSON error line (or report an unknown status if none is parseable)."""
    def run(args: argparse.Namespace) -> int:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = impl(args)
        text = buf.getvalue()
        if rc != 0:
            lines = text.splitlines()
            for i in range(len(lines) - 1, -1, -1):
                # raw CLI stderr may precede our JSON on the same line (no newline)
                at = lines[i].find("{")
                try:
                    obj = json.loads(lines[i][at:]) if at >= 0 else None
                except ValueError:
                    continue
                if isinstance(obj, dict) and "error" in obj:
                    obj.setdefault("user_status", _failure_user_status(obj))
                    lines[i] = lines[i][:at] + json.dumps(obj, ensure_ascii=False)
                    break
            else:
                lines.append(json.dumps({"user_status": {
                    "code": "unknown", "text": _UNKNOWN_STATUS_TEXT, "known": False}},
                    ensure_ascii=False))
            text = "\n".join(lines) + "\n"
        sys.stderr.write(text)
        return rc
    run.__name__ = impl.__name__
    return run


cmd_personal_confirm = _with_failure_status(_personal_confirm_impl)


def cmd_status(args: argparse.Namespace) -> int:
    cli, db, _ = _resolve(args)
    if cli is None:
        return 1

    cli_args = ["status"]
    if getattr(args, "id", None):
        cli_args.append(args.id)
    if getattr(args, "state", None):
        cli_args.extend(["--state", args.state])

    rc, out, err = _run_cli(cli, db, cli_args)
    if rc != 0:
        sys.stderr.write(err if err else json.dumps({"error": "status failed"}) + "\n")
        return 1

    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        _emit_err({"error": "invalid JSON from CLI status"})
        return 1
    # For single-ID queries the result must be a JSON object (not a list/malformed)
    if getattr(args, "id", None):
        if not isinstance(parsed, dict):
            _emit_err({"error": "invalid JSON from CLI status: expected object for single-ID query"})
            return 1
        h = _record_snapshot_hash(parsed)
        if h:
            parsed["snapshot_hash"] = h
        parsed["user_status"] = _user_status(parsed)
        print(json.dumps(parsed, ensure_ascii=False))
        if not parsed["user_status"]["known"]:
            _emit_err({"error": "record has an unknown state; no delivery claim possible"})
            return 1
    else:
        entries = parsed.get("entries") if isinstance(parsed, dict) else None
        if not isinstance(entries, list):
            _emit_err({"error": "invalid JSON from CLI status: expected entries list"})
            return 1
        for e in entries:
            if isinstance(e, dict):
                e["user_status"] = _user_status(e)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    return 0


# ── Read-only day report (Europe/Berlin) ─────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_REPORT_NOTE = (
    "Queue records only. Never sum them with Health Connect ingest, and never add "
    "drafts (or anything not acked) to a day's intake. No totals are computed.")


def _berlin_day_bounds(date_str: str) -> tuple:
    """(start_utc, end_utc) of a Europe/Berlin calendar day, DST-aware. Raises ValueError."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    if not isinstance(date_str, str) or not _DATE_RE.match(date_str):
        raise ValueError("date must be YYYY-MM-DD")
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    tz = ZoneInfo(_DISPLAY_TZ)
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _day_report(entries: list, date_str: str) -> dict:
    """Classify queue rows whose start_time falls on date_str (Berlin) into
    pending / done / excluded, each with the canonical user status and a reason."""
    start, end = _berlin_day_bounds(date_str)
    pending, done, excluded = [], [], []
    for e in entries:
        if not isinstance(e, dict):
            excluded.append({"reason": "malformed queue row", "day_known": False})
            continue
        base = {"client_record_id": e.get("client_record_id"),
                "client_record_version": e.get("client_record_version"),
                "state": e.get("state"), "name": e.get("name")}
        st, serr = _parse_ts(e.get("start_time"), "start_time")
        if serr:
            excluded.append(dict(base, day_known=False, user_status=_user_status(e),
                                 reason=f"unusable start_time ({serr})"))
            continue
        if not (start <= st < end):
            continue
        item = dict(base, user_status=_user_status(e),
                    **{f: e.get(f) for f in _MACRO_FIELDS})
        try:
            item["local_time"] = _berlin_range(e.get("start_time"), e.get("end_time"))
        except ValueError as exc:
            excluded.append(dict(item, day_known=True, reason=f"unusable time ({exc})"))
            continue
        state = e.get("state")
        if state == "draft":
            pending.append(dict(item, reason="draft: not confirmed, not in any intake"))
        elif state == "confirmed":
            pending.append(dict(item, reason="confirmed: waiting for phone sync/ACK"))
        elif state == "acked":
            done.append(item)
        elif state == "cancelled":
            excluded.append(dict(item, day_known=True, reason="cancelled"
                                 + ("; phone may have fetched it before cancellation"
                                    if _delivery_possible(e) else "")))
        else:
            excluded.append(dict(item, day_known=True, reason="unknown state"))
    return {"date": date_str, "timezone": _DISPLAY_TZ,
            "counts": {"pending": len(pending), "done": len(done), "excluded": len(excluded)},
            "pending": pending, "done": done, "excluded": excluded,
            "totals": None, "note": _DAY_REPORT_NOTE}


_DAY_REPORT_LIMIT = 200
# Same selection as consultation_read._DAY_ROWS_SQL (a test keeps them identical).
_DAY_ROWS_SQL = """
SELECT * FROM nutrition_queue
WHERE (
    (start_time LIKE '%Z' OR start_time GLOB '*[+-][0-9][0-9]:[0-9][0-9]')
    AND CAST(strftime('%s', start_time) AS INTEGER) >= ?
    AND CAST(strftime('%s', start_time) AS INTEGER) < ?
) OR (
    NOT (start_time LIKE '%Z' OR start_time GLOB '*[+-][0-9][0-9]:[0-9][0-9]')
    AND substr(start_time, 1, 10) = ?
)
ORDER BY CAST(strftime('%s', start_time) AS INTEGER), id
LIMIT ?
"""


def _query_day_rows(db: Path, date_str: str) -> tuple:
    """Rows of one Berlin day via a read-only (mode=ro, query_only) parameterised
    query on the queue DB; the DB is never written or created. Returns
    (rows, possibly_incomplete, error)."""
    import sqlite3
    errs = _check_regular_file_secure(db, "nutrition DB")
    if errs:
        return None, None, errs[0]
    start, end = _berlin_day_bounds(date_str)
    try:
        conn = sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            rows = [dict(r) for r in conn.execute(
                _DAY_ROWS_SQL, (int(start.timestamp()), int(end.timestamp()), date_str,
                                _DAY_REPORT_LIMIT + 1)).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None, "cannot read the nutrition queue database read-only"
    return rows[:_DAY_REPORT_LIMIT], len(rows) > _DAY_REPORT_LIMIT, None


def cmd_day_report(args: argparse.Namespace) -> int:
    cli, db, _ = _resolve(args)
    if cli is None:
        return 1
    from zoneinfo import ZoneInfo
    date_str = args.date or _now().astimezone(ZoneInfo(_DISPLAY_TZ)).strftime("%Y-%m-%d")
    try:
        _berlin_day_bounds(date_str)
    except ValueError as exc:
        _emit_err({"error": str(exc)})
        return 1
    rows, incomplete, qerr = _query_day_rows(db, date_str)
    if qerr:
        _emit_err({"error": qerr})
        return 1
    report = _day_report(rows, date_str)
    report["limit"] = _DAY_REPORT_LIMIT
    report["returned"] = len(rows)
    report["possibly_incomplete"] = incomplete
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


# ── Known-recipe registry (personal scope) ───────────────────────────────────
#
# A recipe is a user-confirmed, immutable template: name, user-confirmed aliases
# (the words the user calls it by), ingredients WITH quantities and the four
# per-portion macros from labels. It is stored in personal-previews/ as
# kind "recipe": recipe-preview (pending) -> recipe-confirm with the user's bare
# yes (active). A new version of the same key retires the previous one. A recipe
# is never a meal: saving one writes nothing to the queue.

_RECIPE_MAX_BYTES = 8192
_RECIPE_MAX_INGREDIENTS = 20
_RECIPE_MAX_ALIASES = 10
_RECIPE_DESC_MAX = 1900
_ALIAS_RE = re.compile(r"^[0-9a-zа-я]+(?:[ -][0-9a-zа-я]+)*$")
_MACRO_MAX = {"energy_kcal": 5000.0, "protein_g": 500.0, "carbohydrate_g": 1000.0,
              "fat_g": 500.0}


def _norm_text(s: str) -> str:
    return " ".join(s.casefold().replace("ё", "е").split())


def _plain_text_error(v, label: str, max_len: int) -> str | None:
    if not isinstance(v, str) or not v.strip() or v != v.strip():
        return f"{label} must be a non-empty string without surrounding whitespace"
    if len(v) > max_len:
        return f"{label} too long (max {max_len})"
    if any(ord(c) < 32 or ord(c) == 0x7f for c in v):
        return f"{label} contains control characters"
    return None


def _canonical_recipe(raw) -> tuple:
    """Validate a submitted recipe; returns (canonical dict without version, error)."""
    if not isinstance(raw, dict):
        return None, "recipe must be a JSON object"
    allowed = {"key", "name", "aliases", "ingredients", "per_portion", "macro_basis"}
    if set(raw) != allowed:
        return None, f"recipe must have exactly the fields {sorted(allowed)}"
    key = raw["key"]
    if not isinstance(key, str) or _SCOPE_RE.fullmatch(key) is None:
        return None, "recipe key must be an opaque label of 1-64 chars [A-Za-z0-9._:-]"
    err = _plain_text_error(raw["name"], "recipe name", 100)
    if err:
        return None, err
    if raw["macro_basis"] != "label":
        return None, "macro_basis must be \"label\" (user-confirmed label values; no estimates)"
    aliases = raw["aliases"]
    if not isinstance(aliases, list) or len(aliases) > _RECIPE_MAX_ALIASES:
        return None, f"aliases must be a list of at most {_RECIPE_MAX_ALIASES} strings"
    norm_aliases = {_norm_text(raw["name"])}
    for a in aliases:
        if not isinstance(a, str):
            return None, "aliases must be strings"
        norm_aliases.add(_norm_text(a))
    for a in norm_aliases:
        if not (2 <= len(a) <= 60) or _ALIAS_RE.fullmatch(a) is None:
            return None, ("recipe name and aliases must be 2-60 letters/digits "
                          "(single spaces or hyphens) to be matchable in a request")
        if a in _REQ_FILLER or _LOG_VERB_RE.match(a) or _DATE_WORD_RE.fullmatch(a):
            return None, "recipe aliases must not be filler, date or log-command words"
    ings = raw["ingredients"]
    if not isinstance(ings, list) or not (1 <= len(ings) <= _RECIPE_MAX_INGREDIENTS):
        return None, f"ingredients must be a list of 1-{_RECIPE_MAX_INGREDIENTS} items"
    canon_ings = []
    for ing in ings:
        if not isinstance(ing, dict) or set(ing) != {"item", "quantity"}:
            return None, "each ingredient must be {\"item\": ..., \"quantity\": ...}"
        for f, n in (("item", 80), ("quantity", 30)):
            err = _plain_text_error(ing[f], f"ingredient {f}", n)
            if err:
                return None, err
        if not any(c.isdigit() for c in ing["quantity"]):
            return None, "every ingredient quantity must contain a number"
        canon_ings.append({"item": ing["item"], "quantity": ing["quantity"]})
    pp = raw["per_portion"]
    if not isinstance(pp, dict) or set(pp) != set(_MACRO_FIELDS):
        return None, f"per_portion must have exactly {list(_MACRO_FIELDS)}"
    macros = {}
    for f in _MACRO_FIELDS:
        v = pp[f]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v \
                or not (0 <= v <= _MACRO_MAX[f]):
            return None, f"per_portion.{f} must be a number within 0..{_MACRO_MAX[f]:g}"
        macros[f] = float(v)
    atwater = 4 * macros["protein_g"] + 4 * macros["carbohydrate_g"] + 9 * macros["fat_g"]
    if macros["energy_kcal"] <= 0 or abs(macros["energy_kcal"] - atwater) > max(
            30.0, 0.25 * max(macros["energy_kcal"], atwater)):
        return None, ("per_portion macros are not plausible label values "
                      "(energy vs 4/4/9 kcal per g protein/carbs/fat)")
    canon = {"key": key, "name": raw["name"], "aliases": sorted(norm_aliases),
             "ingredients": canon_ings, "per_portion": macros, "macro_basis": "label"}
    if len(_recipe_description_body(dict(canon, version=1))) > _RECIPE_DESC_MAX:
        return None, "ingredient list too long for the queue description"
    return canon, None


def _recipe_description_body(recipe: dict) -> str:
    """Description body of a meal logged from this recipe (provenance in the record)."""
    items = "; ".join(f"{i['item']} {i['quantity']}" for i in recipe["ingredients"])
    return f"Известный рецепт v{recipe['version']}: {items}"


def _recipe_display(recipe: dict) -> str:
    m = recipe["per_portion"]
    lines = [f"Рецепт: {recipe['name']}"]
    lines += [f"• {i['item']} — {i['quantity']}" for i in recipe["ingredients"]]
    lines.append(f"≈{_fmt_num(m['energy_kcal'])} ккал · Б {_fmt_num(m['protein_g'])} · "
                 f"Ж {_fmt_num(m['fat_g'])} · У {_fmt_num(m['carbohydrate_g'])} г "
                 f"на порцию (по этикеткам)")
    lines.append("Сохранить рецепт?")
    return "\n".join(lines)


def _recipe_record_errors(rec: dict, scope: str) -> list:
    """Integrity of a stored recipe record. Never raises."""
    errs = []
    if (rec.get("mode") != _PERSONAL_MODE or rec.get("kind") != _KIND_RECIPE
            or rec.get("personal_chat_attested") is not True
            or rec.get("provenance_level") != _PERSONAL_PROVENANCE):
        errs.append("record is not a personal recipe record")
    if rec.get("scope") != scope:
        errs.append("recipe scope mismatch")
    pid = rec.get("preview_id")
    if not isinstance(pid, str) or re.fullmatch(r"[0-9a-f]{32}", pid) is None:
        errs.append("recipe record id malformed")
    r = rec.get("recipe")
    if not isinstance(r, dict):
        return errs + ["recipe malformed"]
    version = r.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return errs + ["recipe version malformed"]
    body = {k: v for k, v in r.items() if k != "version"}
    canon, cerr = _canonical_recipe(dict(body))
    if cerr or canon != body:
        return errs + ["recipe content malformed (tampered)"]
    if not _hex64(rec.get("recipe_hash")) or _snapshot_hash(r) != rec["recipe_hash"]:
        errs.append("recipe does not match its hash (tampered)")
    disp = rec.get("display_text")
    if disp != _recipe_display(r) or not _hex64(rec.get("display_text_sha256")) \
            or _sha256_text(disp) != rec["display_text_sha256"]:
        errs.append("recipe display text does not match the saved recipe (tampered)")
    wall = rec.get("created_wall_ns")
    if isinstance(wall, bool) or not isinstance(wall, int) or wall <= 0:
        errs.append("recipe record has no workflow freshness marker")
    if rec.get("status") == "active":
        reg = rec.get("registration")
        if isinstance(reg, dict):
            if (set(reg) != _REGISTRATION_FIELDS or not isinstance(reg.get("user_text"), str)
                    or _registration_text_errors(
                        reg.get("source"), reg["user_text"],
                        r.get("aliases") if isinstance(r.get("aliases"), list) else [])
                    or not isinstance(rec.get("registration_evidence_key"), str)
                    or not _hex64(rec["registration_evidence_key"][-64:])):
                errs.append("recipe registration evidence malformed")
        elif not isinstance(rec.get("consent_file_key"), str):
            errs.append("active recipe has neither user consent nor registration evidence")
    return errs


def _load_private_json(path_arg, label: str, max_bytes: int) -> tuple:
    text, st, err = _read_private_text(path_arg, label, max_bytes)
    if err:
        return None, None, None, err

    def no_dup(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    def no_constant(c):
        raise ValueError(f"non-finite number {c}")
    try:
        data = json.loads(text, object_pairs_hook=no_dup, parse_constant=no_constant)
    except (ValueError, RecursionError):
        return None, None, None, f"{label} is not valid JSON (duplicate keys/NaN rejected)"
    return data, text, st, None


def _active_recipes(d: Path, scope: str, key: str) -> list:
    return [(p, x) for p, x in _scope_records(d, scope, _KIND_RECIPE)
            if x.get("status") == "active" and isinstance(x.get("recipe"), dict)
            and x["recipe"].get("key") == key]


def _consent_from_file(path_arg) -> tuple:
    """Bare-affirmative consent file -> (consent dict, st, error dict)."""
    raw, st, cerr = _read_private_text(path_arg, "consent file", _CONSENT_FILE_MAX)
    if cerr:
        return None, None, {"error": "consent file rejected", "details": [cerr]}
    reply_errs = _validate_short_reply(raw)
    if reply_errs:
        return None, None, {"error": "short reply rejected", "reasons": reply_errs}
    sha = _sha256_text(raw)
    key = "consentfile:" + _sha256_text(
        f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}:{sha}")
    return {"raw_text": raw, "sha256": sha, "key": key}, st, None


def cmd_recipe_preview(args: argparse.Namespace) -> int:
    """Store ONE pending recipe preview (no queue mutation); show its display_text."""
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    raw, _, _, jerr = _load_private_json(args.recipe_file, "recipe file", _RECIPE_MAX_BYTES)
    if jerr:
        _emit_err({"error": jerr})
        return 1
    canon, cerr = _canonical_recipe(raw)
    if cerr:
        _emit_err({"error": "recipe rejected", "reasons": [cerr]})
        return 1
    d, derrs = _personal_dir(isolation_root, create=True)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    import secrets
    import time
    now = _now()
    rec_id = secrets.token_hex(16)
    with _PreviewLock(d):
        serrs = _personal_store_errors(d)
        if serrs:
            _emit_err({"error": serrs[0]})
            return 1
        active = _scope_pending_any(d, scope)
        if len(active) > 1:
            _emit_err({"error": "ambiguous: several active previews exist for this scope"})
            return 1
        versions = [x["recipe"].get("version") for _, x in _scope_records(d, scope, _KIND_RECIPE)
                    if isinstance(x.get("recipe"), dict) and x["recipe"].get("key") == canon["key"]]
        versions = [v for v in versions if type(v) is int]
        recipe = dict(canon, version=max(versions, default=0) + 1)
        display = _recipe_display(recipe)
        data = {
            "schema": _PREVIEW_SCHEMA, "mode": _PERSONAL_MODE, "kind": _KIND_RECIPE,
            "preview_id": rec_id, "status": "pending", "scope": scope,
            "personal_chat_attested": True, "recipe": recipe,
            "recipe_hash": _snapshot_hash(recipe),
            "display_text": display, "display_text_sha256": _sha256_text(display),
            "created_at": now.isoformat(), "created_wall_ns": time.time_ns(),
            "provenance_level": _PERSONAL_PROVENANCE,
        }
        for p, other in active:  # one open yes/no question per scope
            other["status"] = "superseded"
            other["superseded_by"] = rec_id
            other["finished_at"] = now.isoformat()
            _write_preview(p, other, exclusive=False)
        _write_preview(d / f"preview-{rec_id}.json", data, exclusive=True)
    print(json.dumps({
        "ok": True, "action": "recipe-preview", "recipe_record_id": rec_id, "scope": scope,
        "recipe_key": recipe["key"], "recipe_version": recipe["version"],
        "recipe_hash": data["recipe_hash"], "superseded_previews": len(active),
        "display_text": display,
        "note": "Saving a recipe never logs a meal.",
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_recipe_confirm(args: argparse.Namespace) -> int:
    """Activate the scope's pending recipe from the user's raw bare yes (no queue mutation)."""
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    consent, st, cerr = _consent_from_file(args.consent_file)
    if cerr:
        _emit_err(cerr)
        return 1
    d, derrs = _personal_dir(isolation_root, create=False)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    if not d.is_dir():
        _emit_err({"error": "no pending recipe for this scope"})
        return 1
    serrs = _personal_store_errors(d)
    if serrs:
        _emit_err({"error": serrs[0]})
        return 1
    if consent["key"] in _used_consent_keys(d):
        _emit_err({"error": "this consent file was already used (replay rejected)"})
        return 1
    pending = _scope_pending_any(d, scope)
    if len(pending) > 1:
        _emit_err({"error": "ambiguous: several active previews exist for this scope"})
        return 1
    if not pending or pending[0][1].get("kind") != _KIND_RECIPE:
        _emit_err({"error": "no pending recipe for this scope"})
        return 1
    path, rv = pending[0]
    errs = _recipe_record_errors(rv, scope)
    if not errs and st.st_mtime_ns < rv["created_wall_ns"]:
        errs.append("consent file predates this recipe preview (stale consent)")
    if errs:
        _emit_err({"error": "recipe consent rejected", "reasons": errs})
        return 1
    now = _now()
    with _PreviewLock(d):
        serrs = _personal_store_errors(d)
        if serrs:
            _emit_err({"error": serrs[0]})
            return 1
        if [p for p, _ in _scope_pending_any(d, scope)] != [path]:
            _emit_err({"error": "ambiguous or changed preview set for this scope"})
            return 1
        cur, lerrs = _load_preview(path)
        if lerrs or cur != rv:
            _emit_err({"error": "recipe preview changed during confirm"})
            return 1
        if consent["key"] in _used_consent_keys(d):
            _emit_err({"error": "this consent file was already used (replay rejected)"})
            return 1
        retired = 0
        for p, other in _active_recipes(d, scope, rv["recipe"]["key"]):
            other["status"] = "retired"
            other["retired_by"] = rv["preview_id"]
            other["finished_at"] = now.isoformat()
            _write_preview(p, other, exclusive=False)
            retired += 1
        cur.update(status="active", activated_at=now.isoformat(),
                   consent_raw_text=consent["raw_text"],
                   consent_file_sha256=consent["sha256"], consent_file_key=consent["key"])
        _write_preview(path, cur, exclusive=False)
    r = rv["recipe"]
    print(json.dumps({
        "ok": True, "action": "recipe-confirm", "scope": scope,
        "recipe_key": r["key"], "recipe_version": r["version"],
        "recipe_hash": rv["recipe_hash"], "retired_versions": retired,
        "provenance_level": _PERSONAL_PROVENANCE,
        "note": "Recipe saved. Nothing was logged; no meal was sent to the queue.",
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_recipe_show(args: argparse.Namespace) -> int:
    """Read-only: the active recipe of a key, plus the exact draft fields to use."""
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    d, derrs = _personal_dir(isolation_root, create=False)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    if not d.is_dir():
        _emit_err({"error": "no active recipe with this key"})
        return 1
    serrs = _personal_store_errors(d)
    if serrs:
        _emit_err({"error": serrs[0]})
        return 1
    found = _active_recipes(d, scope, args.recipe_key)
    if len(found) != 1:
        _emit_err({"error": "no active recipe with this key" if not found
                   else "ambiguous: several active versions of this recipe"})
        return 1
    rv = found[0][1]
    errs = _recipe_record_errors(rv, scope)
    if errs:
        _emit_err({"error": "recipe record rejected", "reasons": errs})
        return 1
    r = rv["recipe"]
    print(json.dumps({
        "ok": True, "action": "recipe-show", "scope": scope, "recipe": r,
        "recipe_key": r["key"], "recipe_version": r["version"], "recipe_hash": rv["recipe_hash"],
        "draft_template": dict({"name": r["name"], "description": _recipe_description_body(r)},
                               **r["per_portion"]),
        "note": ("Add start_time and end_time = start_time + 60 s for the user's stated "
                 "time; create it with `draft`, then personal-log-known."),
    }, indent=2, ensure_ascii=False))
    return 0


# ── Known-recipe text log fast path (personal-log-known) ─────────────────────
#
# For ONE fresh draft (version 1) built exactly from an active known recipe at a
# time the user stated explicitly, in the user's own direct text request in a
# personal chat. The user's raw request (private 0600 file, single-use across the
# whole store) is the consent; no extra preview/yes is needed. Everything else
# (photos, estimates, unknown recipes, approximate/assumed times, portions,
# corrections, mixed meals, questions, negations, quotes, examples) is rejected
# here and must go through personal-preview + a fresh yes. The recognizer is a
# deliberately small whitelist, not NLP: when in doubt it refuses.

_REQUEST_MAX_BYTES = 2048
_REQUEST_MAX_CHARS = 300
_REQUEST_MAX_AGE_S = 1800
_REQUEST_FUTURE_SKEW_S = 60
_RECEIVED_TO_FILE_MAX_S = 600
_DRAFT_BEFORE_REQUEST_SKEW_S = 120
_MEAL_AFTER_REQUEST_MAX_S = 3600
_MEAL_PAST_MAX_DAYS = 7
_KNOWN_LOG_INTERVAL_S = 60
_TIME_EVIDENCE_MAX = 1024

_LW = r"0-9a-zа-я"  # word characters after _norm_text


def _words_re(words) -> re.Pattern:
    return re.compile(rf"(?<![{_LW}])(?:" + "|".join(words) + rf")(?![{_LW}])")


_LOG_VERB_RE = re.compile(
    rf"^(?:пожалуйста,?\s+)?(?:запиши(?:те)?|залогируй(?:те)?|"
    rf"(?:добавь|внеси|занеси)(?:те)?\s+в\s+дневник(?:\s+питания)?)(?![{_LW}])")
_REQ_BAD_CHARS = set("?\"'«»“”„‘’`>[]{}<")
_REQ_CHECKS = (
    (_words_re([r"не", r"нет", r"ни", r"никогда", r"отмен\w*", r"удал\w*", r"стоп",
                r"not", r"no", r"don't", r"dont"]), "request contains a negation"),
    (_words_re([r"если", r"бы", r"б", r"может\w*", r"можно", r"мож\w*", r"мог\w*",
                r"наверн\w*", r"допустим", r"представ\w*", r"вдруг", r"стоит", r"ли",
                r"как", r"сколько", r"зачем", r"почему", r"когда", r"что", r"чтобы",
                r"хоч\w*", r"хотел\w*", r"надо", r"нужно", r"давай\w*"]),
     "request is a question/hypothetical, not a direct instruction"),
    (_words_re([r"пример\w*", r"например", r"тест\w*", r"шаблон\w*", r"команд\w*",
                r"пользовател\w*", r"задани\w*", r"задач\w*", r"example\w*", r"test\w*",
                r"user\w*", r"assign\w*"]),
     "request looks like an example/assignment, not the user's own request"),
    (_words_re([r"фот\w*", r"снимк\w*", r"снимок", r"картинк\w*", r"изображени\w*",
                r"photo\w*", r"pic\w*"]), "photo meals need a preview"),
    (_words_re([r"оцен\w*", r"посчитай", r"прикинь", r"рассчита\w*", r"вес\w*",
                r"грамм\w*", r"г", r"гр", r"кг", r"мл", r"ккал", r"калори\w*"]),
     "estimates/quantities need a preview"),
    (re.compile(rf"(?<![{_LW}])(?:около|примерно|приблизительно|где-то|где то|ближе к|"
                rf"часов в|часа в|после|до|между|утром|днем|вечером|ночью|в обед|позже|"
                rf"раньше|недавно|только что|сейчас|полчаса|час назад)(?![{_LW}])|[~≈±]"),
     "time is approximate or not stated explicitly"),
    (_words_re([r"поправ\w*", r"исправ\w*", r"измени\w*", r"замени\w*", r"вместо", r"но",
                r"только", r"без", r"половин\w*", r"полпорци\w*", r"двойн\w*", r"порци\w*",
                r"больше", r"меньше", r"еще", r"плюс", r"минус", r"и", r"или", r"с", r"со",
                r"тоже", r"также"]),
     "corrections, portions or mixed meals need a preview"),
)
_HM_RE = re.compile(rf"(?<![{_LW}:.])(?:в\s+)?(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)"
                    rf"(?:\s+(?P<p>утра|дня|вечера|ночи))?(?![{_LW}:.])")
_HOUR_RE = re.compile(rf"(?<![{_LW}:.])в\s+(?P<h>[01]?\d|2[0-3])(?:\s*(?:часа|часов|час|ч))?"
                      rf"(?:\s+(?P<p>утра|дня|вечера|ночи))?(?![{_LW}:.])")
_DATE_WORD_RE = _words_re([r"сегодня", r"вчера", r"позавчера", r"завтра", r"послезавтра"])
_DATE_NUM_RE = re.compile(rf"(?<![{_LW}:.])(?P<d>[0-3]?\d)\.(?P<mo>[01]?\d)(?:\.(?P<y>\d{{4}}))?"
                          rf"(?![{_LW}:.])")
_DATE_TXT_RE = re.compile(rf"(?<![{_LW}])(?P<d>[0-3]?\d)\s+(?P<mon>{'|'.join(_RU_MONTHS)})"
                          rf"(?:\s+(?P<y>\d{{4}}))?(?![{_LW}])")
_REQ_FILLER = frozenset({
    "пожалуйста", "мой", "мою", "мое", "моя", "мои", "обычный", "обычную", "обычное",
    "обычная", "стандартный", "стандартную", "стандартное", "стандартная", "на", "в",
    "завтрак", "обед", "ужин", "перекус", "полдник", "дневник", "питания",
})
_REQ_FILLER_PHRASES = re.compile(rf"(?<![{_LW}])(?:по\s+рецепту|в\s+дневник(?:\s+питания)?)"
                                 rf"(?![{_LW}])")
_SPLIT_RE = re.compile(r"[\s,.!:;—–-]+")


def _resolve_hour(h: int, period, minute_given: bool) -> tuple:
    if period is None:
        if minute_given or h >= 13 or h == 0:
            return h, None
        return None, "hour without minutes or утра/дня/вечера/ночи is ambiguous"
    ok = {"утра": h if 1 <= h <= 11 else None,
          "дня": 12 if h == 12 else (h + 12 if 1 <= h <= 5 else (h if 12 < h <= 17 else None)),
          "вечера": h + 12 if 5 <= h <= 11 else (h if 17 <= h <= 23 else None),
          "ночи": 0 if h == 12 else (h if 0 <= h <= 4 else None)}[period]
    if ok is None:
        return None, "time and утра/дня/вечера/ночи contradict"
    return ok, None


def _parse_log_request(raw: str, aliases, request_dt) -> tuple:
    """Recognize a direct known-recipe log request. Pure; returns (info, error).

    request_dt: aware datetime when the request was received (its Berlin date is
    "сегодня"). info: time_span, date_span, local_date, local_time, start_utc.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    if not isinstance(raw, str):
        return None, "request is not text"
    text = raw.strip()
    if not text or len(text) > _REQUEST_MAX_CHARS:
        return None, f"request empty or longer than {_REQUEST_MAX_CHARS} chars"
    if "\n" in text or "\r" in text or any(ord(c) < 32 or ord(c) == 0x7f for c in text):
        return None, "request must be a single line"
    if any(c in _REQ_BAD_CHARS for c in text):
        return None, "request contains a question mark, quotes or brackets"
    norm = _norm_text(text).rstrip(".!")
    m = _LOG_VERB_RE.match(norm)
    if not m:
        return None, ("request does not start with a direct log instruction "
                      "(запиши / залогируй / добавь в дневник ...)")
    rest = " " + norm[m.end():] + " "
    # The known recipe, by its name or a user-confirmed alias (longest first).
    spans = []
    for a in sorted(aliases, key=len, reverse=True):
        for am in re.finditer(rf"(?<![{_LW}]){re.escape(a)}(?![{_LW}])", rest):
            if not any(am.start() < e and s < am.end() for s, e in spans):
                spans.append((am.start(), am.end()))
    if len(spans) != 1:
        return None, ("request does not name this known recipe exactly once"
                      if not spans else "request names the recipe more than once")
    s, e = spans[0]
    rest = rest[:s] + " " + rest[e:]
    for rx, reason in _REQ_CHECKS:
        if rx.search(rest):
            return None, reason
    times = [(tm, True) for tm in _HM_RE.finditer(rest)]
    times += [(tm, False) for tm in _HOUR_RE.finditer(rest)
              if not any(tm.start() < o.end() and o.start() < tm.end() for o, _ in times)]
    if len(times) != 1:
        return None, ("no explicit clock time in the request" if not times
                      else "request contains more than one time")
    tm, has_min = times[0]
    hour, herr = _resolve_hour(int(tm.group("h")), tm.group("p"), has_min)
    if herr:
        return None, herr
    minute = int(tm.group("m")) if has_min else 0
    time_span = tm.group(0).strip()
    rest = rest[:tm.start()] + " " + rest[tm.end():]

    tz = ZoneInfo(_DISPLAY_TZ)
    base = request_dt.astimezone(tz).date()
    dates = list(_DATE_WORD_RE.finditer(rest)) + list(_DATE_NUM_RE.finditer(rest)) \
        + list(_DATE_TXT_RE.finditer(rest))
    if len(dates) > 1:
        return None, "request contains more than one date"
    date_span = None
    day = base
    if dates:
        dm = dates[0]
        date_span = dm.group(0)
        try:
            if dm.re is _DATE_WORD_RE:
                off = {"сегодня": 0, "вчера": -1, "позавчера": -2}.get(date_span)
                if off is None:
                    return None, "future dates are not logged"
                day = base + timedelta(days=off)
            else:
                mon = (int(dm.group("mo")) if dm.re is _DATE_NUM_RE
                       else _RU_MONTHS.index(dm.group("mon")) + 1)
                year = int(dm.group("y")) if dm.group("y") else base.year
                day = datetime(year, mon, int(dm.group("d"))).date()
                if not dm.group("y") and day > base:
                    day = datetime(year - 1, mon, int(dm.group("d"))).date()
        except ValueError:
            return None, "invalid date in the request"
        rest = rest[:dm.start()] + " " + rest[dm.end():]
    rest = _REQ_FILLER_PHRASES.sub(" ", rest)
    extra = [t for t in _SPLIT_RE.split(rest) if t and t not in _REQ_FILLER]
    if extra:
        return None, "request contains extra content (possible correction or mixed meal)"
    if day > base or (base - day).days > _MEAL_PAST_MAX_DAYS:
        return None, f"date must be within the last {_MEAL_PAST_MAX_DAYS} days"
    naive = datetime(day.year, day.month, day.day, hour, minute)
    a = naive.replace(tzinfo=tz, fold=0)
    start = a.astimezone(timezone.utc)
    if start.astimezone(tz).replace(tzinfo=None) != naive:
        return None, "local time does not exist (DST change); use a preview"
    if a.utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset():
        return None, "local time is ambiguous (DST change); use a preview"
    if (start - request_dt).total_seconds() > _MEAL_AFTER_REQUEST_MAX_S:
        return None, "meal time is too far after the request"
    return {"time_span": time_span, "date_span": date_span,
            "local_date": day.isoformat(), "local_time": f"{hour:02d}:{minute:02d}",
            "start_utc": start}, None


# ── Recipe registration of an ALREADY user-confirmed recipe (recipe-register) ──
#
# When the user has already explicitly asked to save/remember a standard recipe
# (a clear «запомни ...» message now, or an earlier explicit confirmation found in
# the real conversation history), no second yes is requested. The agent hands the
# wrapper the recipe plus private evidence of that real user message; the wrapper
# validates the recipe (same canonical form as recipe-preview), checks the quoted
# text is a clear, non-negated, non-hypothetical, non-quoted save request, stores
# the recipe as a new immutable version and audits the evidence. This never logs a
# meal and never touches the scope's open yes/no question. The evidence is
# agent-attested (provenance agent_conversation_attested); the wrapper cannot
# verify that session/message ids or text are real.

_EVIDENCE_MAX_BYTES = 4096
_SAVE_TEXT_MAX_CHARS = 600
_EVIDENCE_SOURCES = ("direct_request", "history_import")
_EVIDENCE_FIELDS = frozenset({"source", "session_id", "message_id", "user_text",
                              "received_at", "basis"})
_REGISTRATION_FIELDS = frozenset({"source", "session_id", "message_id", "user_text",
                                  "received_at", "basis", "evidence_file_sha256"})
_SAVE_VERB_RE = re.compile(rf"(?<![{_LW}])(?:запомни\w*|запомн\w*|сохрани\w*|сохранить|"
                           rf"зафиксир\w*)(?![{_LW}])")
_SAVE_HYPO_RE = _words_re([r"если", r"бы", r"б", r"мож\w*", r"смож\w*", r"мог\w*", r"наверн\w*",
                           r"допустим", r"представ\w*", r"вдруг", r"ли", r"хоч\w*", r"хотел\w*",
                           r"давай\w*", r"возможно", r"потом", r"позже"])


def _save_request_errors(text) -> list:
    """Is this verbatim user text a clear, direct request to save/remember a recipe?
    Whitelist-style and conservative; returns reasons (empty = accepted)."""
    if not isinstance(text, str):
        return ["user_text is not text"]
    t = text.strip()
    if not t or len(t) > _SAVE_TEXT_MAX_CHARS:
        return [f"user_text empty or longer than {_SAVE_TEXT_MAX_CHARS} chars"]
    if "\n" in t or "\r" in t or any(ord(c) < 32 or ord(c) == 0x7f for c in t):
        return ["user_text must be a single line"]
    if any(c in _REQ_BAD_CHARS for c in t):
        return ["user_text contains a question mark, quotes or brackets"]
    norm = _norm_text(t)
    if not _SAVE_VERB_RE.search(norm):
        return ["user_text is not an explicit save request (запомни / сохрани ...)"]
    for rx, reason in ((_REQ_CHECKS[0][0], "user_text contains a negation"),
                       (_SAVE_HYPO_RE, "user_text is hypothetical, not a direct save request"),
                       (_REQ_CHECKS[2][0], "user_text looks like an example/assignment, "
                                           "not the user's own message")):
        if rx.search(" " + norm + " "):
            return [reason]
    return []


_STANDARD_MARKER_RE = _words_re([r"стандарт\w*", r"обычн\w*", r"дефолтн\w*", r"по умолчанию"])
_YES_START_RE = re.compile(rf"^да(?![{_LW}])")
_CHANGE_RE = _words_re([r"но", r"а", r"вместо", r"теперь", r"изменил\w*", r"поменял\w*", r"друг\w*",
                        r"нов\w*", r"или", r"примерно", r"около", r"приблизительно", r"без",
                        r"больше", r"меньше", r"еще", r"иногда", r"обычно"])


def _confirmed_standard_errors(text, aliases) -> list:
    """history_import ONLY: is this verbatim user text an explicit affirmative
    confirmation of THIS recipe as the user's standard («Да, стандартная <name> ...»)?
    Conservative; a bare yes, an unrelated standard, a changed/hedged assertion, a
    question, a quote or an example/assignment is refused. Returns reasons."""
    if not isinstance(text, str):
        return ["user_text is not text"]
    t = text.strip()
    if not t or len(t) > _SAVE_TEXT_MAX_CHARS:
        return [f"user_text empty or longer than {_SAVE_TEXT_MAX_CHARS} chars"]
    if "\n" in t or "\r" in t or any(ord(c) < 32 or ord(c) == 0x7f for c in t):
        return ["user_text must be a single line"]
    if any(c in _REQ_BAD_CHARS for c in t):
        return ["user_text contains a question mark, quotes or brackets"]
    norm = " " + _norm_text(t) + " "
    if not _YES_START_RE.match(norm.strip()):
        return ["user_text is not an explicit affirmative («Да, ...») confirmation"]
    for rx, reason in ((_REQ_CHECKS[0][0], "user_text contains a negation"),
                       (_SAVE_HYPO_RE, "user_text is hypothetical"),
                       (_CHANGE_RE, "user_text changes or hedges the recipe"),
                       (_REQ_CHECKS[2][0], "user_text looks like an example/assignment")):
        if rx.search(norm):
            return [reason]
    if not _STANDARD_MARKER_RE.search(norm):
        return ["user_text does not call the recipe the user's standard/default"]
    if not any(re.search(rf"(?<![{_LW}]){re.escape(a)}(?![{_LW}])", norm) for a in aliases):
        return ["user_text does not name this recipe"]
    return []


def _registration_text_errors(source, text, aliases) -> list:
    """direct_request: explicit save intent. history_import: save intent OR an
    explicit confirmed-standard reply for this recipe."""
    errs = _save_request_errors(text)
    if errs and source == "history_import":
        if not _confirmed_standard_errors(text, aliases):
            return []
    return errs


def cmd_recipe_register(args: argparse.Namespace) -> int:
    """Register an already user-confirmed recipe from audited evidence (no new yes)."""
    import hashlib
    import secrets
    import time
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    if not getattr(args, "attest_user_confirmed_recipe", False):
        _emit_err({"error": "--attest-user-confirmed-recipe is required: declares the evidence "
                            "is the user's real, explicit request to save THIS recipe (not an "
                            "assignment, example or quoted text) and that the recipe values "
                            "are the user's confirmed label values"})
        return 1
    raw, _, _, jerr = _load_private_json(args.recipe_file, "recipe file", _RECIPE_MAX_BYTES)
    if jerr:
        _emit_err({"error": jerr})
        return 1
    canon, cerr = _canonical_recipe(raw)
    if cerr:
        _emit_err({"error": "recipe rejected (estimated or unlabeled recipes cannot be "
                            "registered; log such meals through personal-preview)",
                   "reasons": [cerr]})
        return 1
    ev, ev_text, ev_st, everr = _load_private_json(args.evidence_file, "evidence file",
                                                   _EVIDENCE_MAX_BYTES)
    if everr:
        _emit_err({"error": everr})
        return 1
    if not isinstance(ev, dict) or set(ev) != _EVIDENCE_FIELDS:
        _emit_err({"error": f"evidence must have exactly the fields {sorted(_EVIDENCE_FIELDS)}"})
        return 1
    if ev["source"] not in _EVIDENCE_SOURCES:
        _emit_err({"error": f"evidence source must be one of {list(_EVIDENCE_SOURCES)}"})
        return 1
    for f, n in (("session_id", 128), ("message_id", 128), ("basis", 300)):
        e = _plain_text_error(ev[f], f"evidence {f}", n)
        if e:
            _emit_err({"error": e})
            return 1
    received, terr = _parse_ts(ev["received_at"], "evidence received_at")
    if terr:
        _emit_err({"error": terr})
        return 1
    now_s = time.time_ns() / 1e9
    if received.timestamp() > now_s + _REQUEST_FUTURE_SKEW_S:
        _emit_err({"error": "evidence received_at is in the future"})
        return 1
    if ev["source"] == "direct_request":
        if now_s - received.timestamp() > _REQUEST_MAX_AGE_S or \
                abs(now_s - ev_st.st_mtime_ns / 1e9) > _REQUEST_MAX_AGE_S:
            _emit_err({"error": "direct_request evidence is not fresh; a saved recipe from an "
                                "older message must use source history_import"})
            return 1
    reasons = _registration_text_errors(ev["source"], ev["user_text"], canon["aliases"])
    if reasons:
        _emit_err({"error": "evidence is not a clear user request to save the recipe; "
                            "use recipe-preview and ask the user", "reasons": reasons})
        return 1
    ev_key = "registrationevidence:" + _sha256_text(f"{ev['session_id']}\0{ev['message_id']}")
    d, derrs = _personal_dir(isolation_root, create=True)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    now = _now()
    rec_id = secrets.token_hex(16)
    with _PreviewLock(d):
        serrs = _personal_store_errors(d)
        if serrs:
            _emit_err({"error": serrs[0]})
            return 1
        if ev_key in _used_consent_keys(d):
            _emit_err({"error": "this evidence message was already used (replay rejected)"})
            return 1
        versions = [x["recipe"].get("version") for _, x in _scope_records(d, scope, _KIND_RECIPE)
                    if isinstance(x.get("recipe"), dict) and x["recipe"].get("key") == canon["key"]]
        recipe = dict(canon, version=max([v for v in versions if type(v) is int], default=0) + 1)
        display = _recipe_display(recipe)
        data = {
            "schema": _PREVIEW_SCHEMA, "mode": _PERSONAL_MODE, "kind": _KIND_RECIPE,
            "preview_id": rec_id, "status": "active", "scope": scope,
            "personal_chat_attested": True, "recipe": recipe,
            "recipe_hash": _snapshot_hash(recipe),
            "display_text": display, "display_text_sha256": _sha256_text(display),
            "created_at": now.isoformat(), "activated_at": now.isoformat(),
            "created_wall_ns": time.time_ns(), "provenance_level": _PERSONAL_PROVENANCE,
            "registration": {
                "source": ev["source"], "session_id": ev["session_id"],
                "message_id": ev["message_id"], "user_text": ev["user_text"],
                "received_at": received.isoformat(), "basis": ev["basis"],
                "evidence_file_sha256": hashlib.sha256(ev_text.encode("utf-8")).hexdigest(),
            },
            "registration_evidence_key": ev_key,
        }
        retired = 0
        for p, other in _active_recipes(d, scope, canon["key"]):
            other["status"] = "retired"
            other["retired_by"] = rec_id
            other["finished_at"] = now.isoformat()
            _write_preview(p, other, exclusive=False)
            retired += 1
        _write_preview(d / f"preview-{rec_id}.json", data, exclusive=True)
    print(json.dumps({
        "ok": True, "action": "recipe-register", "scope": scope,
        "recipe_key": recipe["key"], "recipe_version": recipe["version"],
        "recipe_hash": data["recipe_hash"], "retired_versions": retired,
        "evidence_source": ev["source"], "provenance_level": _PERSONAL_PROVENANCE,
        "note": "Recipe saved from the user's already explicit request. Nothing was logged; "
                "no meal was sent to the queue; no question was asked or closed.",
    }, indent=2, ensure_ascii=False))
    return 0


def _known_log_records(d: Path, scope: str | None = None) -> list:
    return [(p, x) for p, x in _iter_previews(d)
            if x is not None and x.get("kind") == _KIND_KNOWN_LOG
            and (scope is None or x.get("scope") == scope)]


def _known_log_conflict(d: Path, scope: str, rid: str, recipe_key: str, start_iso: str):
    """Error if this draft is already bound, or the same meal was already logged."""
    for _, x in _known_log_records(d):
        if x.get("client_record_id") == rid:
            return "this draft is already bound to a log request (replay rejected)"
        if (x.get("scope") == scope and x.get("recipe_key") == recipe_key
                and x.get("start_time") == start_iso and x.get("status") != "failed"):
            return "the same known meal at the same time was already logged (duplicate)"
    return None


def _finish_known_log(d: Path, path: Path, status: str, reason: str | None = None) -> None:
    with _PreviewLock(d):
        cur, errs = _load_preview(path)
        if errs or cur is None or cur.get("status") != "consuming":
            return
        cur["status"] = status
        cur["finished_at"] = _now().isoformat()
        if reason:
            cur["failure_reason"] = reason
        _write_preview(path, cur, exclusive=False)


def _personal_log_known_impl(args: argparse.Namespace) -> int:
    import secrets
    import time
    from datetime import datetime, timedelta, timezone
    cli, db, isolation_root = _resolve(args)
    if cli is None:
        return 1
    is_isolation = bool(args.cli)
    scope, serr = _personal_scope_args(args)
    if serr:
        _emit_err({"error": serr})
        return 1
    if not getattr(args, "attest_direct_user_request", False):
        _emit_err({"error": "--attest-direct-user-request is required: declares the request "
                            "file is the user's own direct text message (no attachment, not "
                            "an assignment, example or forwarded text)"})
        return 1
    if not getattr(args, "attest_user_time", False):
        _emit_err({"error": "--attest-user-time is required: declares the meal time was "
                            "stated by the user, not assumed"})
        return 1
    confirmer = args.confirmer
    if not isinstance(confirmer, str) or not confirmer or confirmer != confirmer.strip():
        _emit_err({"error": "--confirmer must be a non-empty name without surrounding whitespace"})
        return 1
    if (isinstance(args.expected_version, bool) or not isinstance(args.expected_version, int)
            or args.expected_version != 1):
        _emit_err({"error": "fast path only for a freshly created draft (version 1); "
                            "corrections need personal-preview and a fresh yes"})
        return 1
    if not _hex64(args.expected_snapshot_hash) or not _hex64(args.recipe_hash):
        _emit_err({"error": "--expected-snapshot-hash and --recipe-hash must be 64-char "
                            "lowercase hex SHA-256"})
        return 1
    if isinstance(args.recipe_version, bool) or not isinstance(args.recipe_version, int):
        _emit_err({"error": "--recipe-version must be an integer"})
        return 1

    # ── the user's raw request: private, fresh, single-use ──
    raw, st, rerr = _read_private_text(args.request_file, "request file", _REQUEST_MAX_BYTES)
    if rerr:
        _emit_err({"error": "request file rejected", "details": [rerr]})
        return 1
    now_ns = time.time_ns()  # real wall clock vs the file's real mtime (local workflow check)
    age_s = (now_ns - st.st_mtime_ns) / 1e9
    if age_s < -_REQUEST_FUTURE_SKEW_S or age_s > _REQUEST_MAX_AGE_S:
        _emit_err({"error": "request file is not fresh (must be written within the last "
                            f"{_REQUEST_MAX_AGE_S // 60} min)"})
        return 1
    req_sha = _sha256_text(raw)
    req_key = "requestfile:" + _sha256_text(
        f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}:{req_sha}")
    # The file's mtime is ONLY a freshness check. The day a relative date ("сегодня")
    # refers to comes from the received-at time the agent attests from the real
    # conversation metadata; it is never derived from when the file was written.
    request_dt, terr = _parse_ts(getattr(args, "request_received_at", None),
                                 "--request-received-at")
    if terr:
        _emit_err({"error": terr + " (the time the user's message was actually received, "
                                   "from conversation metadata; never invent it)"})
        return 1
    recv_age_s = now_ns / 1e9 - request_dt.timestamp()
    file_lag_s = (st.st_mtime_ns - request_dt.timestamp() * 1e9) / 1e9
    if recv_age_s < -_REQUEST_FUTURE_SKEW_S:
        _emit_err({"error": "--request-received-at is in the future"})
        return 1
    if recv_age_s > _REQUEST_MAX_AGE_S:
        _emit_err({"error": "--request-received-at is too old for the fast path "
                            f"(more than {_REQUEST_MAX_AGE_S // 60} min); use a preview"})
        return 1
    if file_lag_s < -_REQUEST_FUTURE_SKEW_S or file_lag_s > _RECEIVED_TO_FILE_MAX_S:
        _emit_err({"error": "--request-received-at is inconsistent with the request file "
                            "(file must be written shortly after the message was received)"})
        return 1

    d, derrs = _personal_dir(isolation_root, create=False)
    if derrs:
        _emit_err({"error": "personal-preview store insecure", "details": derrs})
        return 1
    if not d.is_dir():
        _emit_err({"error": "no active recipe with this key"})
        return 1
    serrs = _personal_store_errors(d)
    if serrs:
        _emit_err({"error": serrs[0]})
        return 1
    if req_key in _used_consent_keys(d):
        _emit_err({"error": "this request file was already used (replay rejected)"})
        return 1

    # ── the known recipe ──
    found = _active_recipes(d, scope, args.recipe_key)
    if len(found) != 1:
        _emit_err({"error": "no active recipe with this key" if not found
                   else "ambiguous: several active versions of this recipe"})
        return 1
    recipe_path, rv = found[0]
    errs = _recipe_record_errors(rv, scope)
    if errs:
        _emit_err({"error": "recipe record rejected", "reasons": errs})
        return 1
    recipe = rv["recipe"]
    if recipe["version"] != args.recipe_version or rv["recipe_hash"] != args.recipe_hash:
        _emit_err({"error": "recipe version/hash differ from the active known recipe"})
        return 1

    # ── the request itself ──
    info, perr = _parse_log_request(raw, recipe["aliases"], request_dt)
    if perr:
        _emit_err({"error": "not an explicit known-recipe log request; use personal-preview",
                   "reasons": [perr]})
        return 1
    ev, _, _, everr = _load_private_json(args.time_evidence_file, "time evidence file",
                                         _TIME_EVIDENCE_MAX)
    if everr:
        _emit_err({"error": everr})
        return 1
    if not isinstance(ev, dict) or set(ev) != {"time_phrase", "local_date", "local_time"} \
            or not all(isinstance(v, str) for v in ev.values()):
        _emit_err({"error": "time evidence must be {time_phrase, local_date, local_time} strings"})
        return 1
    phrase = _norm_text(ev["time_phrase"])
    if (not phrase or phrase not in _norm_text(raw) or info["time_span"] not in phrase
            or ev["local_date"] != info["local_date"] or ev["local_time"] != info["local_time"]):
        _emit_err({"error": "agent time attestation does not match the user's words"})
        return 1
    start_utc = info["start_utc"]

    # ── the exact saved draft ──
    rid = args.id
    version, snap, verr = _verify_draft_live(cli, db, rid, 1, args.expected_snapshot_hash)
    if verr:
        _emit_err({"error": verr})
        return 1
    rec, _ = _read_record(cli, db, rid)

    def _draft_errors(r, sn):
        e = []
        if sn["name"] != recipe["name"]:
            e.append("draft name differs from the known recipe")
        if any(sn[f] != recipe["per_portion"][f] for f in _MACRO_FIELDS):
            e.append("draft macros differ from the known recipe")
        if sn.get("description") != _AI_DESCRIPTION_PREFIX + " " + _recipe_description_body(recipe):
            e.append("draft description is not the known recipe description")
        st_dt, e1 = _parse_ts(sn["start_time"], "start_time")
        en_dt, e2 = _parse_ts(sn["end_time"], "end_time")
        if e1 or e2 or st_dt != start_utc:
            e.append("draft start time differs from the time the user stated")
        elif en_dt != st_dt + timedelta(seconds=_KNOWN_LOG_INTERVAL_S):
            e.append("draft end time must be start + 60 s (technical interval)")
        if not isinstance(r, dict) or _delivery_possible(r) or r.get("confirmed_by") is not None:
            e.append("draft is not a fresh, never-delivered draft")
        else:
            cr, e3 = _parse_ts(r.get("created_at"), "created_at")
            if e3 or (cr - request_dt).total_seconds() < -_DRAFT_BEFORE_REQUEST_SKEW_S:
                e.append("draft was not created for this request (older than the request)")
        return e
    derr = _draft_errors(rec, snap)
    if derr:
        _emit_err({"error": "draft does not match the known recipe request", "reasons": derr})
        return 1
    start_iso = start_utc.isoformat()

    # Duplicate guard against the queue (bounded list) and the store.
    rc, out, _ = _run_cli(cli, db, ["status"])
    try:
        listed = json.loads(out).get("entries") if rc == 0 else None
    except (ValueError, AttributeError):
        listed = None
    if not isinstance(listed, list):
        _emit_err({"error": "cannot list the queue for the duplicate check"})
        return 1
    for e in listed:
        if not isinstance(e, dict) or e.get("client_record_id") == rid:
            continue
        est, _ = _parse_ts(e.get("start_time"), "start_time")
        if e.get("state") in ("confirmed", "acked") and e.get("name") == recipe["name"] \
                and est == start_utc:
            _emit_err({"error": "the same known meal at the same time is already in the queue "
                                "(duplicate)"})
            return 1
    conflict = _known_log_conflict(d, scope, rid, recipe["key"], start_iso)
    if conflict:
        _emit_err({"error": conflict})
        return 1

    readiness_errs = _check_mutation_readiness(args, is_isolation, isolation_root)
    if readiness_errs:
        _emit_err({"error": "mutation readiness check failed", "details": readiness_errs})
        return 1

    log_id = secrets.token_hex(16)
    log_path = d / f"preview-{log_id}.json"
    record = {
        "schema": _PREVIEW_SCHEMA, "mode": _PERSONAL_MODE, "kind": _KIND_KNOWN_LOG,
        "preview_id": log_id, "status": "consuming", "scope": scope,
        "personal_chat_attested": True, "direct_user_request_attested": True,
        "user_time_attested": True,
        "client_record_id": rid, "client_record_version": version,
        "snapshot": snap, "snapshot_hash": _snapshot_hash(snap), "start_time": start_iso,
        "recipe_key": recipe["key"], "recipe_version": recipe["version"],
        "recipe_hash": rv["recipe_hash"],
        "request_raw_text": raw, "request_file_sha256": req_sha, "request_file_key": req_key,
        "request_received_at": request_dt.isoformat(),
        "time_evidence": {"time_phrase": ev["time_phrase"], "local_date": info["local_date"],
                          "local_time": info["local_time"]},
        "confirmer": confirmer, "provenance_level": _PERSONAL_PROVENANCE,
    }
    claimed = {"v": False}

    def _claim(before: dict):
        live = _snapshot_of(before)
        if live is None or _snapshot_hash(live) != record["snapshot_hash"] \
                or _delivery_possible(before):
            return "record changed before confirm — nothing logged"
        with _PreviewLock(d):
            serrs2 = _personal_store_errors(d)
            if serrs2:
                return serrs2[0]
            if req_key in _used_consent_keys(d):
                return "this request file was already used (replay rejected)"
            c2 = _known_log_conflict(d, scope, rid, recipe["key"], start_iso)
            if c2:
                return c2
            cur, lerrs = _load_preview(recipe_path)
            if lerrs or cur != rv:
                return "known recipe changed during log"
            now = _now()
            # The conversation moved on: no older yes/no question stays open.
            for p, other in _scope_pending_any(d, scope):
                other["status"] = "superseded"
                other["superseded_by"] = log_id
                other["finished_at"] = now.isoformat()
                _write_preview(p, other, exclusive=False)
            record["created_at"] = record["claimed_at"] = now.isoformat()
            _write_preview(log_path, record, exclusive=True)
        claimed["v"] = True
        return None

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _confirm_with_readback(
            cli, db, rid, version, recipe["name"], confirmer, pre_confirm=_claim,
            extra_confirm_args=["--expected-snapshot-hash", record["snapshot_hash"]])
    out = buf.getvalue()
    if rc != 0:
        sys.stdout.write(out)
        if claimed["v"] and _STAGE["v"] == "confirm":
            # Say "not logged" only if the readback proves the draft untouched.
            after, _ = _read_record(cli, db, rid)
            live = _snapshot_of(after) if after else None
            if (after and after.get("state") == "draft"
                    and after.get("client_record_version") == version
                    and live is not None and _snapshot_hash(live) == record["snapshot_hash"]):
                _finish_known_log(d, log_path, "failed", "queue confirm refused")
                _emit_err({"error": "queue confirm refused; nothing logged",
                           "client_record_id": rid})
            else:
                _emit_err({"error": "uncertain outcome: confirm failed and the record "
                                    "changed; read it back (status)", "client_record_id": rid})
        # post-readback failures stay "consuming": uncertain, fail closed.
        return rc
    try:
        res = json.loads(out)
    except ValueError:
        res = None
    if (not isinstance(res, dict) or res.get("ok") is not True
            or res.get("state") != "confirmed" or res.get("client_record_id") != rid
            or res.get("client_record_version") != version):
        _emit_err({"error": "uncertain outcome: confirm helper returned an invalid success "
                            "result; read the record back (status) before any further action",
                   "client_record_id": rid, "log_state": "consuming (no replay)"})
        return 1
    _finish_known_log(d, log_path, "consumed")
    after = res.get("readback_after") or {}
    status = _user_status(after)
    try:
        when = _berlin_range(after.get("start_time"), after.get("end_time"))
    except ValueError:
        when = info["local_date"] + " " + info["local_time"]
    print(json.dumps({
        "ok": True, "action": "personal-log-known",
        "client_record_id": rid, "client_record_version": version,
        "state": res["state"], "user_status": status,
        "ack_text": f"{recipe['name']} — {when}\n{status['text']}",
        "provenance_level": _PERSONAL_PROVENANCE,
        "known_log": {
            "log_id": log_id, "scope": scope, "recipe_key": recipe["key"],
            "recipe_version": recipe["version"], "recipe_hash": rv["recipe_hash"],
            "request_raw_text": raw, "request_file_sha256": req_sha,
            "request_received_at": request_dt.isoformat(),
            "time_phrase": ev["time_phrase"], "local_date": info["local_date"],
            "local_time": info["local_time"],
            "note": "agent-attested direct user request; no Telegram linkage; "
                    "no author identity is claimed",
        },
        "readback_after": after,
    }, indent=2, ensure_ascii=False))
    return 0


cmd_personal_log_known = _with_failure_status(_personal_log_known_impl)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nutrition-writeback",
        description=(
            "Nutrition Health Connect write-back skill executable.\n\n"
            "In production mode (no --cli/--db/--isolation-root), all prerequisites\n"
            "must exist. In isolation mode (--isolation-root + --cli + --db), both\n"
            "CLI and DB must be under the isolation root (private dir under scratch).\n"
            "Never creates production databases. Fails closed if not deployed.\n\n"
            "Mutation operations (draft/confirm/short-confirm) require --privacy-verifier,\n"
            "--privacy-source, --receiver-url, and --token-path. Production mode\n"
            "additionally requires --live-host. Isolation mode requires --ssh-fixture\n"
            "inside the isolation root."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Isolation mode flags
    parser.add_argument("--isolation-root", default=None, metavar="DIR",
                        help="Private isolation root directory (under allowed scratch)")
    parser.add_argument("--cli", default=None, metavar="PATH",
                        help="nutrition-queue CLI binary path (requires --isolation-root + --db)")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="Nutrition database path (requires --isolation-root + --cli)")

    # Mutation readiness flags (required for draft/confirm)
    parser.add_argument("--receiver-url", default=None, metavar="URL",
                        help="Nutrition HTTP receiver URL (https:// for production, "
                             "http://127.x for isolation)")
    parser.add_argument("--token-path", default=None, metavar="PATH",
                        help="Path to nutrition token file (isolation: required under isolation-root; "
                             "production: defaults to production token path)")
    parser.add_argument("--privacy-verifier", default=None, metavar="PATH",
                        help="Path to privacy_gate_check.py")
    parser.add_argument("--privacy-source", default=None, metavar="PATH",
                        help="Path to traefik compose.yaml for privacy source check")
    parser.add_argument("--live-host", default=None, metavar="HOST",
                        help="Traefik SSH host for live privacy check (production only)")
    parser.add_argument("--ssh-user", default="root", metavar="USER",
                        help="SSH user for live privacy check (default: root)")
    parser.add_argument("--ssh-fixture", default=None, metavar="PATH",
                        help="SSH fixture file for privacy check (isolation only; "
                             "must be inside --isolation-root)")

    sub = parser.add_subparsers(dest="command", required=True)

    # draft
    p_d = sub.add_parser("draft", help="Create a draft entry (JSON from stdin or --file)")
    p_d.add_argument("--file", default=None, metavar="PATH",
                     help="JSON input file (default: read from stdin)")

    # confirm
    p_c = sub.add_parser("confirm", help="Confirm draft after explicit user consent")
    p_c.add_argument("--id", required=True, metavar="ID",
                     help="client_record_id from user's consent")
    p_c.add_argument("--version", required=True, type=int, metavar="N",
                     help="Version from user's consent (must match stored version exactly)")
    p_c.add_argument("--confirmer", required=True, metavar="NAME",
                     help="Name of person giving explicit consent (must appear in consent text)")
    p_c.add_argument("--meal-name", required=True, metavar="NAME",
                     help="Meal name from consent (verified against stored record)")
    consent_grp = p_c.add_mutually_exclusive_group(required=False)
    consent_grp.add_argument("--consent-text", default=None, metavar="TEXT",
                              help="Raw explicit user consent phrase (exact grammar required)")
    consent_grp.add_argument("--consent-file", default=None, metavar="PATH",
                              help="Path to file containing the raw explicit user consent phrase")

    # preview
    p_p = sub.add_parser("preview", help="Register a pending final preview for a draft")
    p_p.add_argument("--id", required=True, metavar="ID", help="draft client_record_id")
    p_p.add_argument("--confirmer", required=True, metavar="NAME",
                     help="Name of the user the preview is addressed to")
    p_p.add_argument("--user-id", required=True, metavar="ID",
                     help="Transport sender ID of the intended user")
    p_p.add_argument("--chat-id", required=True, metavar="ID")
    p_p.add_argument("--thread-id", default="", metavar="ID")
    p_p.add_argument("--preview-message-id", required=True, metavar="ID",
                     help="Transport message ID of the final preview actually sent")
    p_p.add_argument("--preview-sent-at", required=True, metavar="ISO8601",
                     help="Transport timestamp (with offset) of that preview message")
    p_p.add_argument("--expected-version", required=True, type=int, metavar="N",
                     help="Version of the exact draft shown in the final preview")
    p_p.add_argument("--expected-snapshot-hash", required=True, metavar="SHA256",
                     help="snapshot_hash (from draft/status output) of the displayed draft")
    p_p.add_argument("--ttl-seconds", type=int, default=_PREVIEW_DEFAULT_TTL, metavar="N")

    # short-confirm
    p_sc = sub.add_parser("short-confirm",
                          help="Confirm via short reply bound to a pending preview")
    p_sc.add_argument("--transport-file", required=True, metavar="PATH",
                      help="0600 JSON of the raw user reply: sender_id, chat_id, "
                           "thread_id, message_id, timestamp, reply_to_message_id, text")

    # personal-preview / personal-confirm (agent-attested personal-chat scope)
    def _personal_flags(sp):
        sp.add_argument("--scope", required=True, metavar="LABEL",
                        help="Opaque stable label of ONE personal (one-to-one) chat")
        sp.add_argument("--attest-personal-chat", action="store_true",
                        help="Declare the scope is a private one-to-one chat")

    p_pp = sub.add_parser("personal-preview",
                          help="Store the one active personal-chat preview (before display)")
    _personal_flags(p_pp)
    p_pp.add_argument("--id", required=True, metavar="ID", help="draft client_record_id")
    p_pp.add_argument("--confirmer", required=True, metavar="NAME")
    p_pp.add_argument("--expected-version", required=True, type=int, metavar="N")
    p_pp.add_argument("--expected-snapshot-hash", required=True, metavar="SHA256")
    p_pp.add_argument("--notes-file", default=None, metavar="PATH",
                      help="Optional 0600 file with ONE short line of assumptions "
                           "(max 240 chars, rejected if longer); the wrapper composes "
                           "name/macros/notes/Berlin time and returns ONE display_text")
    p_pp.add_argument("--ttl-seconds", type=int, default=_PREVIEW_DEFAULT_TTL, metavar="N")

    p_pc = sub.add_parser("personal-confirm",
                          help="Confirm the scope's active preview from the raw bare reply")
    _personal_flags(p_pc)
    p_pc.add_argument("--consent-file", required=True, metavar="PATH",
                      help="0600 file with the user's raw reply text (e.g. 'Да')")

    # known-recipe registry (personal scope; never logs a meal)
    p_rp = sub.add_parser("recipe-preview",
                          help="Store the scope's one pending recipe preview (before display)")
    _personal_flags(p_rp)
    p_rp.add_argument("--recipe-file", required=True, metavar="PATH",
                      help="0600 JSON: key, name, aliases, ingredients[{item, quantity}], "
                           "per_portion{4 macros}, macro_basis \"label\"")
    p_rc = sub.add_parser("recipe-confirm",
                          help="Activate the pending recipe from the user's raw bare yes")
    _personal_flags(p_rc)
    p_rc.add_argument("--consent-file", required=True, metavar="PATH")
    p_rg = sub.add_parser("recipe-register",
                          help="Register an ALREADY user-confirmed recipe from audited "
                               "evidence (no new yes; never logs a meal)")
    _personal_flags(p_rg)
    p_rg.add_argument("--attest-user-confirmed-recipe", action="store_true",
                      help="Evidence is the user's real explicit request to save this recipe")
    p_rg.add_argument("--recipe-file", required=True, metavar="PATH")
    p_rg.add_argument("--evidence-file", required=True, metavar="PATH",
                      help="0600 JSON: source (direct_request|history_import), session_id, "
                           "message_id, user_text (verbatim), received_at (ISO with offset), "
                           "basis")
    p_rs = sub.add_parser("recipe-show", help="Show an active known recipe (readonly)")
    _personal_flags(p_rs)
    p_rs.add_argument("--recipe-key", required=True, metavar="KEY")

    # known-recipe text log fast path
    p_lk = sub.add_parser("personal-log-known",
                          help="Confirm ONE fresh known-recipe draft from the user's direct "
                               "text request with an explicit time")
    _personal_flags(p_lk)
    p_lk.add_argument("--attest-direct-user-request", action="store_true",
                      help="The request file is the user's own direct text message")
    p_lk.add_argument("--attest-user-time", action="store_true",
                      help="The meal time was stated by the user, not assumed")
    p_lk.add_argument("--request-file", required=True, metavar="PATH",
                      help="0600 file with the user's raw request text (single-use)")
    p_lk.add_argument("--request-received-at", required=True, metavar="ISO8601",
                      help="When the user's message was actually received (with UTC offset), "
                           "from real conversation metadata; NOT the file write time")
    p_lk.add_argument("--time-evidence-file", required=True, metavar="PATH",
                      help="0600 JSON {time_phrase, local_date, local_time} (Europe/Berlin)")
    p_lk.add_argument("--recipe-key", required=True, metavar="KEY")
    p_lk.add_argument("--recipe-version", required=True, type=int, metavar="N")
    p_lk.add_argument("--recipe-hash", required=True, metavar="SHA256")
    p_lk.add_argument("--id", required=True, metavar="ID", help="draft client_record_id")
    p_lk.add_argument("--expected-version", required=True, type=int, metavar="N")
    p_lk.add_argument("--expected-snapshot-hash", required=True, metavar="SHA256")
    p_lk.add_argument("--confirmer", required=True, metavar="NAME")

    # day-report
    p_dr = sub.add_parser("day-report",
                          help="Read-only Europe/Berlin day report of queue records")
    p_dr.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                      help="Berlin calendar day (default: today in Berlin)")

    # status
    p_s = sub.add_parser("status", help="Show entry status (readonly)")
    p_s.add_argument("id", nargs="?", default=None, metavar="ID",
                     help="client_record_id (omit to list all)")
    p_s.add_argument("--state", choices=["draft", "confirmed", "cancelled", "acked"],
                     default=None)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {"draft": cmd_draft, "confirm": cmd_confirm, "preview": cmd_preview,
                "short-confirm": cmd_short_confirm, "status": cmd_status,
                "personal-preview": cmd_personal_preview,
                "personal-confirm": cmd_personal_confirm,
                "recipe-preview": cmd_recipe_preview, "recipe-confirm": cmd_recipe_confirm,
                "recipe-show": cmd_recipe_show, "recipe-register": cmd_recipe_register,
                "personal-log-known": cmd_personal_log_known,
                "day-report": cmd_day_report}
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
