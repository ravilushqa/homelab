#!/usr/bin/env python3
"""
Privacy gate verification for health-connect-bridge deployment.

Usage:
  python3 privacy_gate_check.py --source komodo/stacks/traefik/compose.yaml
  python3 privacy_gate_check.py --source FILE --ssh-user USER --live-host HOST
  python3 privacy_gate_check.py --source FILE --ssh-fixture FILE  (tests only)

Both source and live checks are required; either failing blocks deployment.

Source check:
  - Parses compose YAML using PyYAML safe_load with duplicate-key rejection
  - Targets ONLY services.traefik.labels and services.traefik.command
  - Applies Docker Compose $$ → $ escape normalization to label values
  - Verifies each required ignoreURLs[N] label value with exact regex match
    that also passes positive and negative sample URL tests
  - Verifies accesslog query-parameter and header drop flags by exact match
  - Verifies no conflicting defaultmode=keep override is present
  - Invalid YAML, duplicate keys, or missing traefik service fail

Live check:
  - SSHes to the Traefik host (user@host)
  - Extracts ONLY: container running state, three required ignoreURLs labels,
    two accesslog defaultmode flags, and per-field override presence
    via narrow allowlisted docker inspect --format commands
    (never raw inspect, env, or full command dump)
  - Verifies the container is running (not stopped/stale)
  - Verifies the running container has the required exclusions active
  - Verifies no per-field accesslog overrides contradict defaultmode=drop
  - If SSH or extraction fails: FAIL CLOSED (deployment blocked)
  - For testing: --ssh-fixture provides pre-canned SSH output

Requires: PyYAML (pip install pyyaml)

Exit:
  0  All checks passed
  1  One or more checks failed (block deployment)
  2  Usage error

Privacy note: this script never outputs any label values, command args, or
container config data to stdout/stderr beyond pass/fail verdicts.
"""

import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# ── Duplicate-key-rejecting YAML loader ───────────────────────────────────────

class _NoDupKeyLoader(_yaml.SafeLoader):
    pass

def _no_dup_mapping_constructor(loader, node):
    loader.flatten_mapping(node)
    pairs = loader.construct_pairs(node)
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise _yaml.YAMLError(f"Duplicate key in YAML mapping: {k!r}")
        seen[k] = v
    return seen

_NoDupKeyLoader.add_constructor(
    _yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_dup_mapping_constructor,
)


# ── Expected patterns ─────────────────────────────────────────────────────────

# Each entry: (label_key_suffix, expected_regex_after_compose_normalization,
#              positive_samples, negative_samples)
_REQUIRED_IGNORE_URLS = [
    (
        "ignoreURLs[0]",
        r"^/ingest/health-connect(\?.*)?$",
        ["/ingest/health-connect", "/ingest/health-connect?foo=bar"],
        ["/ingest/health-connect/extra", "/other", "/ingest/health-connectx"],
    ),
    (
        "ignoreURLs[1]",
        r"^/nutrition/queue(\?.*)?$",
        ["/nutrition/queue", "/nutrition/queue?page=1"],
        ["/nutrition/queue/extra", "/nutrition/queuex", "/other"],
    ),
    (
        "ignoreURLs[2]",
        r"^/nutrition/ack(\?.*)?$",
        ["/nutrition/ack", "/nutrition/ack?retry=1"],
        ["/nutrition/ackx", "/nutrition/ack/extra", "/other"],
    ),
]

_UMAMI_LABEL_PREFIX = (
    "traefik.http.middlewares.umami.plugin.umami-feeder."
)

_REQUIRED_ACCESSLOG_ARGS = [
    "--accesslog.fields.queryparameters.defaultmode=drop",
    "--accesslog.fields.headers.defaultmode=drop",
]

# Prefixes for defaultmode args — any value other than =drop is a conflict
_DEFAULTMODE_PREFIXES = (
    "--accesslog.fields.queryparameters.defaultmode=",
    "--accesslog.fields.headers.defaultmode=",
)


# ── Source YAML parsing (PyYAML with duplicate-key rejection) ─────────────────

def _parse_traefik_service_block(yaml_text: str) -> dict:
    """
    Parse compose YAML and extract labels and command from services.traefik ONLY.

    Uses PyYAML safe_load with a custom loader that rejects duplicate keys.
    Returns {'labels': [str, ...], 'command': [str, ...]} on success.
    Returns {'labels': [], 'command': [], 'parse_error': str} on failure.

    Labels are returned as raw strings (e.g. "key=value") with YAML unescaping
    already applied by PyYAML (e.g. \\\\ in YAML source → \\ in the string).
    Docker Compose $$ → $ normalization is applied separately by the caller.
    """
    try:
        doc = _yaml.load(yaml_text, Loader=_NoDupKeyLoader)
    except _yaml.YAMLError as e:
        return {"labels": [], "command": [], "parse_error": f"YAML parse error: {e}"}

    if not isinstance(doc, dict):
        return {"labels": [], "command": [], "parse_error": "YAML root is not a mapping"}

    services = doc.get("services")
    if not isinstance(services, dict):
        return {"labels": [], "command": []}

    traefik = services.get("traefik")
    if not isinstance(traefik, dict):
        return {"labels": [], "command": []}

    # Labels: compose supports list-of-"key=value" or mapping format
    raw_labels = traefik.get("labels", [])
    labels: list = []
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, str):
                labels.append(item)
    elif isinstance(raw_labels, dict):
        for k, v in raw_labels.items():
            labels.append(f"{k}={v}")

    # Command: list of strings
    raw_cmd = traefik.get("command", [])
    command: list = []
    if isinstance(raw_cmd, list):
        for item in raw_cmd:
            if isinstance(item, str):
                command.append(item)

    return {"labels": labels, "command": command}


def _normalize_compose_value(s: str) -> str:
    """
    Apply Docker Compose variable substitution: $$ → $

    Note: YAML unescaping (e.g. \\\\ → \\) is already performed by PyYAML
    before values reach this function.  Only the Compose-level $$ → $ step
    is needed here.
    """
    return s.replace("$$", "$")


def _validate_ignore_pattern(raw_value: str, suffix: str, expected: str,
                              pos: list, neg: list) -> tuple:
    """
    Validate one ignoreURLs label value.

    Returns (ok: bool, message: str).
    raw_value: the label value string (after stripping key=)
    """
    normalized = _normalize_compose_value(raw_value)
    if normalized != expected:
        return False, (
            f"  FAIL {suffix}: pattern does not match expected "
            f"(got different value)"
        )
    try:
        pattern = re.compile(normalized)
    except re.error as e:
        return False, f"  FAIL {suffix}: invalid regex: {e}"

    for url in pos:
        if not pattern.match(url):
            return False, f"  FAIL {suffix}: pattern does not match expected URL {url!r}"
    for url in neg:
        if pattern.match(url):
            return False, f"  FAIL {suffix}: pattern incorrectly matches {url!r}"

    return True, f"  OK  {suffix}: pattern correct and validated"


def check_source(compose_path: str) -> tuple:
    """
    Check source compose file for required privacy labels in the traefik service.
    Returns (passed: bool, messages: list[str]).
    """
    try:
        text = Path(compose_path).read_text(encoding="utf-8")
    except OSError as e:
        return False, [f"Cannot read compose file: {e}"]

    messages = []
    passed = True

    block = _parse_traefik_service_block(text)

    if "parse_error" in block:
        return False, [f"  FAIL source: {block['parse_error']}"]

    labels = block["labels"]
    command_args = block["command"]

    if not labels and not command_args:
        return False, [
            "  FAIL cannot locate 'traefik' service labels/command in compose file"
        ]

    # Build a lookup: label key suffix → value (from traefik service only)
    label_map = {}
    for raw in labels:
        if "=" in raw:
            key, _, value = raw.partition("=")
            key = key.strip()
            if key.startswith(_UMAMI_LABEL_PREFIX):
                suffix = key[len(_UMAMI_LABEL_PREFIX):]
                label_map[suffix] = value.strip()

    # Verify each required ignoreURLs entry
    for suffix, expected, pos, neg in _REQUIRED_IGNORE_URLS:
        if suffix not in label_map:
            passed = False
            messages.append(f"  FAIL missing label: {_UMAMI_LABEL_PREFIX}{suffix}")
        else:
            ok, msg = _validate_ignore_pattern(
                label_map[suffix], suffix, expected, pos, neg
            )
            if not ok:
                passed = False
            messages.append(msg)

    # Verify required accesslog drop args by exact match (not substring).
    # Also reject duplicate occurrences of the same drop flag (ambiguous ordering).
    arg_counts: dict = {}
    for a in command_args:
        arg_counts[a] = arg_counts.get(a, 0) + 1
    for required_arg in _REQUIRED_ACCESSLOG_ARGS:
        if arg_counts.get(required_arg, 0) == 1:
            messages.append(f"  OK  command arg present: {required_arg}")
        elif arg_counts.get(required_arg, 0) > 1:
            passed = False
            messages.append(
                f"  FAIL duplicate command arg (ambiguous): {required_arg}"
            )
        else:
            passed = False
            messages.append(f"  FAIL missing command arg: {required_arg}")

    # Reject any --accesslog.fields.*.defaultmode=X where X is not 'drop'
    # (case-insensitive). This catches keep, redact, unknown, and any future values.
    for arg in command_args:
        arg_lower = arg.lower()
        for prefix in _DEFAULTMODE_PREFIXES:
            if arg_lower.startswith(prefix):
                val = arg_lower[len(prefix):]
                if val != "drop":
                    passed = False
                    messages.append(
                        f"  FAIL conflicting/invalid defaultmode value "
                        f"(expected 'drop', got {val!r})"
                    )

    # Verify no per-field overrides in queryparameters or headers.
    # Any non-drop per-field setting (=keep, =redact, =unknown, etc.) is unsafe
    # and contradicts the defaultmode=drop intent.
    _PF_PREFIXES = (
        "--accesslog.fields.queryparameters.names.",
        "--accesslog.fields.headers.names.",
    )
    for arg in command_args:
        arg_lower = arg.lower()
        if any(arg_lower.startswith(p) for p in _PF_PREFIXES):
            if not arg_lower.endswith("=drop"):
                passed = False
                messages.append(
                    "  FAIL per-field accesslog override found in traefik command "
                    "(non-drop per-field setting contradicts defaultmode=drop)"
                )

    return passed, messages


# ── Live SSH check ────────────────────────────────────────────────────────────

# SSH extraction script: extracts ONLY container running state, three allowlisted
# label values, two specific accesslog flag checks, and per-field override detection.
# Never outputs full inspect, env, raw command array, or secrets.
# Output format: KEY=VALUE lines (one per item).
_SSH_EXTRACT_SCRIPT = r"""
set -e
TRAEFIK_CONTAINER=traefik
_label() {
    docker inspect --format "{{index .Config.Labels \"$1\"}}" "$TRAEFIK_CONTAINER" 2>/dev/null
}
_cmd_has_exact_flag() {
    local flag="$1"
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    print('true' if any(a == '$1' for a in args) else 'false')
except Exception:
    print('error')
"
}
_cmd_has_qp_field_override() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    found=[a for a in args if 'accesslog.fields.queryparameters.names.' in a.lower()
           and not a.lower().endswith('=drop')]
    print('true' if found else 'false')
except Exception:
    print('error')
"
}
_cmd_has_hd_field_override() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    found=[a for a in args if 'accesslog.fields.headers.names.' in a.lower()
           and not a.lower().endswith('=drop')]
    print('true' if found else 'false')
except Exception:
    print('error')
"
}
_cmd_has_qp_conflict_mode() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    found=[a for a in args if a.lower().startswith('--accesslog.fields.queryparameters.defaultmode=')
           and not a.lower().endswith('=drop')]
    print('true' if found else 'false')
except Exception:
    print('error')
"
}
_cmd_has_hd_conflict_mode() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    found=[a for a in args if a.lower().startswith('--accesslog.fields.headers.defaultmode=')
           and not a.lower().endswith('=drop')]
    print('true' if found else 'false')
except Exception:
    print('error')
"
}
_cmd_has_qp_dup_drop() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    count=sum(1 for a in args if a=='--accesslog.fields.queryparameters.defaultmode=drop')
    print('true' if count > 1 else 'false')
except Exception:
    print('error')
"
}
_cmd_has_hd_dup_drop() {
    docker inspect --format '{{json .Config.Cmd}}' "$TRAEFIK_CONTAINER" 2>/dev/null \
      | python3 -c "
import sys,json
try:
    args=json.load(sys.stdin)
    count=sum(1 for a in args if a=='--accesslog.fields.headers.defaultmode=drop')
    print('true' if count > 1 else 'false')
except Exception:
    print('error')
"
}
printf 'running=%s\n' "$(docker inspect --format '{{.State.Running}}' "$TRAEFIK_CONTAINER" 2>/dev/null || echo false)"
printf 'ignoreURLs[0]=%s\n' "$(_label 'traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[0]')"
printf 'ignoreURLs[1]=%s\n' "$(_label 'traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[1]')"
printf 'ignoreURLs[2]=%s\n' "$(_label 'traefik.http.middlewares.umami.plugin.umami-feeder.ignoreURLs[2]')"
printf 'qp_drop=%s\n' "$(_cmd_has_exact_flag '--accesslog.fields.queryparameters.defaultmode=drop')"
printf 'hd_drop=%s\n' "$(_cmd_has_exact_flag '--accesslog.fields.headers.defaultmode=drop')"
printf 'qp_override=%s\n' "$(_cmd_has_qp_field_override)"
printf 'hd_override=%s\n' "$(_cmd_has_hd_field_override)"
printf 'qp_conflict=%s\n' "$(_cmd_has_qp_conflict_mode)"
printf 'hd_conflict=%s\n' "$(_cmd_has_hd_conflict_mode)"
printf 'qp_dup_drop=%s\n' "$(_cmd_has_qp_dup_drop)"
printf 'hd_dup_drop=%s\n' "$(_cmd_has_hd_dup_drop)"
"""


def _run_ssh_extract(ssh_user: str, host: str) -> tuple:
    """
    SSH to host and run the narrow extraction script.
    Returns (success: bool, output_lines: list[str]).
    Never logs or prints the raw SSH output.
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                f"{ssh_user}@{host}",
                "bash -s",
            ],
            input=_SSH_EXTRACT_SCRIPT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, []
        lines = result.stdout.splitlines()
        return True, lines
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, []


def _parse_ssh_output(lines: list) -> tuple:
    """
    Parse KEY=VALUE lines from SSH output.
    Returns (result_dict, duplicate_keys_set).
    Duplicate keys indicate a possible extraction bug or injection attempt.
    """
    result = {}
    duplicates: set = set()
    for line in lines:
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            if k in result:
                duplicates.add(k)
            result[k] = v.strip()
    return result, duplicates


def check_live(
    host: str,
    ssh_user: str = "root",
    fixture_path: str = None,
) -> tuple:
    """
    Verify the live running Traefik has the required privacy exclusions.

    Uses SSH to extract only allowlisted values (running state, label values,
    two accesslog flags, override presence).
    If SSH is unavailable: FAIL CLOSED.

    fixture_path (tests only): file containing simulated SSH KEY=VALUE output.
    Returns (passed: bool, messages: list[str]).
    """
    messages = []
    passed = True

    if fixture_path:
        try:
            lines = Path(fixture_path).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return False, [f"FAIL CLOSED: cannot read SSH fixture: {e}"]
    else:
        ok, lines = _run_ssh_extract(ssh_user, host)
        if not ok:
            return False, [
                f"FAIL CLOSED: SSH extraction failed for {ssh_user}@{host}; "
                "cannot verify live Traefik state"
            ]

    data, duplicates = _parse_ssh_output(lines)

    if not data:
        return False, ["FAIL CLOSED: SSH output empty or unparseable"]

    if duplicates:
        return False, [
            f"FAIL CLOSED: duplicate keys in SSH output: {sorted(duplicates)} — "
            "possible extraction bug or injection; cannot verify live state"
        ]

    # Verify container is actually running (not stopped or stale)
    running = data.get("running", "")
    if running != "true":
        return False, [
            "  FAIL CLOSED: Traefik container is not running "
            f"(running={running!r}); cannot verify live state"
        ]
    messages.append("  OK  container running: true")

    # Verify each ignoreURLs value against expected patterns
    for suffix, expected, pos, neg in _REQUIRED_IGNORE_URLS:
        raw = data.get(suffix, "")
        if not raw:
            passed = False
            messages.append(f"  FAIL live {suffix}: label is empty or missing")
            continue
        ok_pat, msg = _validate_ignore_pattern(raw, suffix, expected, pos, neg)
        if not ok_pat:
            passed = False
        messages.append(msg.replace("FAIL ", "FAIL live ").replace("  OK  ", "  OK  live "))

    # Verify accesslog drop flags (exact)
    for flag_key, label_text in [
        ("qp_drop", "--accesslog.fields.queryparameters.defaultmode=drop"),
        ("hd_drop", "--accesslog.fields.headers.defaultmode=drop"),
    ]:
        val = data.get(flag_key, "false")
        if val == "true":
            messages.append(f"  OK  live flag active: {label_text}")
        else:
            passed = False
            messages.append(f"  FAIL live flag not active: {label_text}")

    # Fail closed for each evidence key: absent or extraction error means we
    # cannot confirm safety.  'error' values come from except-blocks in the
    # SSH extraction script and must be treated as unknown (fail closed).
    _EVIDENCE_CHECKS = [
        ("qp_override",    "queryparameters field-level non-drop override",        "true"),
        ("hd_override",    "headers field-level non-drop override",                "true"),
        ("qp_conflict",    "queryparameters conflicting defaultmode (non-drop)",   "true"),
        ("hd_conflict",    "headers conflicting defaultmode (non-drop)",           "true"),
        ("qp_dup_drop",    "queryparameters duplicate defaultmode=drop flag",      "true"),
        ("hd_dup_drop",    "headers duplicate defaultmode=drop flag",              "true"),
    ]
    for ev_key, desc, bad_val in _EVIDENCE_CHECKS:
        if ev_key not in data:
            passed = False
            messages.append(
                f"  FAIL CLOSED live: {desc} evidence missing from SSH output — "
                "cannot confirm absence"
            )
        elif data[ev_key] == bad_val:
            passed = False
            messages.append(f"  FAIL live: {desc} found — blocks deployment")
        elif data[ev_key] not in ("true", "false"):
            passed = False
            messages.append(
                f"  FAIL CLOSED live: {desc} extraction returned unexpected value "
                f"{data[ev_key]!r} — cannot confirm absence"
            )
        else:
            messages.append(f"  OK  live no {desc}")

    return passed, messages


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", required=True,
                   help="Path to traefik compose.yaml to verify")
    p.add_argument("--live-host", default=None,
                   help="Traefik host for live SSH check (required for deployment gate)")
    p.add_argument("--ssh-user", default="root",
                   help="SSH user for live check (default: root)")
    p.add_argument("--ssh-fixture", default=None,
                   help="For tests only: file with simulated SSH KEY=VALUE output")
    args = p.parse_args(argv)

    all_passed = True

    print("=== Source check (compose file) ===")
    src_ok, src_msgs = check_source(args.source)
    for m in src_msgs:
        print(m)
    print("Source check:", "PASSED" if src_ok else "FAILED")
    if not src_ok:
        all_passed = False

    print()
    print("=== Live check (running Traefik) ===")
    if args.ssh_fixture:
        live_ok, live_msgs = check_live(
            host="(fixture)", fixture_path=args.ssh_fixture
        )
        for m in live_msgs:
            print(m)
        print("Live check (fixture):", "PASSED" if live_ok else "FAILED")
        if not live_ok:
            all_passed = False
    elif args.live_host:
        live_ok, live_msgs = check_live(
            host=args.live_host, ssh_user=args.ssh_user
        )
        for m in live_msgs:
            print(m)
        print("Live check:", "PASSED" if live_ok else "FAILED")
        if not live_ok:
            all_passed = False
    else:
        print("  FAIL CLOSED: no --live-host or --ssh-fixture provided")
        print("  Live Traefik state cannot be verified; deployment blocked.")
        all_passed = False

    print()
    if all_passed:
        print("=== GATE PASSED — proceed to deployment ===")
        return 0
    else:
        print("=== GATE FAILED — deployment BLOCKED ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
