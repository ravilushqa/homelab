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
  status        Show status of one or all entries (readonly).

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
        if status == "pending":
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

    claimed = {"v": False}

    def _pre_confirm(before: dict):
        live = _snapshot_of(before)
        if live is None or _snapshot_hash(live) != pv["snapshot_hash"]:
            _revoke_if_pending(d, path, pv)  # drift kills the consent for good
            return "record content differs from the previewed content — new preview required"
        # Expiry is re-checked right before the claim.
        if _now() > expires:
            return "preview expired"
        cerr = _claim_preview(d, path, transport, pv)
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

    # For single-ID queries the result must be a JSON object (not a list/malformed)
    if getattr(args, "id", None):
        try:
            parsed = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            _emit_err({"error": "invalid JSON from CLI status"})
            return 1
        if not isinstance(parsed, dict):
            _emit_err({"error": "invalid JSON from CLI status: expected object for single-ID query"})
            return 1
        h = _record_snapshot_hash(parsed)
        if h:
            parsed["snapshot_hash"] = h
        print(json.dumps(parsed))
    else:
        print(out, end="")
    return 0


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
                "short-confirm": cmd_short_confirm, "status": cmd_status}
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
