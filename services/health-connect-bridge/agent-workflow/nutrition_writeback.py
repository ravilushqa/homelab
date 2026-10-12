#!/usr/bin/env python3
"""
nutrition-writeback — skill executable for nutrition Health Connect write-back.

Calls the installed nutrition-queue CLI as a subprocess. Never imports
production modules or creates databases. Fails closed if system not deployed.

Subcommands:
  draft     Read JSON from stdin or --file, create draft, read back written record.
  confirm   Read back current record, verify name/version, verify explicit raw
            user consent text, confirm with technical affirm phrase, read back
            before AND after. Requires --consent-text or --consent-file and
            --confirmer and --meal-name.
  status    Show status of one or all entries (readonly).

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
        "readback": readback,
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

    # ── Read back BEFORE confirming ───────────────────────────────────────────
    rc, out, _ = _run_cli(cli, db, ["status", args.id])
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

    if before.get("client_record_id") != args.id:
        _emit_err({
            "error": "readback id mismatch before confirm",
            "expected": args.id,
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
    if stored_version != args.version:
        _emit_err({
            "error": "version mismatch — stale or incorrect consent",
            "consent_version": args.version,
            "stored_version": stored_version,
            "hint": "read the current record and require new explicit consent",
        })
        return 1

    stored_name = before.get("name")
    if stored_name != args.meal_name:
        _emit_err({
            "error": "meal name mismatch — consent named a different meal",
            "hint": "re-read the record and require new explicit consent",
        })
        return 1

    # ── Confirm via CLI ───────────────────────────────────────────────────────
    rc2, out2, err2 = _run_cli(cli, db, [
        "confirm", args.id,
        "--version", str(args.version),
        "--affirm", _CONFIRM_AFFIRM,
        "--confirmer", args.confirmer,
    ])
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
    rc3, out3, _ = _run_cli(cli, db, ["status", args.id])
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
    if after.get("client_record_id") != args.id:
        after_errs.append(
            f"readback_after id mismatch: expected {args.id!r}, "
            f"got {after.get('client_record_id')!r}"
        )
    if after.get("client_record_version") != args.version:
        after_errs.append(
            f"readback_after version mismatch: expected {args.version}, "
            f"got {after.get('client_record_version')!r}"
        )
    if after.get("state") != "confirmed":
        after_errs.append(
            f"readback_after state is not confirmed: got {after.get('state')!r}"
        )
    if after.get("name") != args.meal_name:
        after_errs.append(
            f"readback_after name mismatch: expected {args.meal_name!r}, "
            f"got {after.get('name')!r}"
        )
    if after.get("confirmed_by") != args.confirmer:
        after_errs.append(
            f"readback_after confirmed_by mismatch: expected {args.confirmer!r}, "
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
        "client_record_id": args.id,
        "client_record_version": after.get("client_record_version"),
        "state": after.get("state"),
        "confirmed_by": after.get("confirmed_by"),
        "confirmed_at": after.get("confirmed_at"),
        "readback_before": before,
        "readback_after": after,
    }, indent=2))
    return 0


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
            "Mutation operations (draft/confirm) require --privacy-verifier,\n"
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
    dispatch = {"draft": cmd_draft, "confirm": cmd_confirm, "status": cmd_status}
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
