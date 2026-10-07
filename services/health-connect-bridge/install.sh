#!/usr/bin/env bash
# Health Connect Bridge — idempotent installer
#
# Copies runtime code artifacts into ~/.hermes/profiles/health/workspace/health-connect-bridge/
# Sets up directory permissions (700 private, 600 secrets), generates a token
# atomically if not present, installs a systemd user unit, creates the CLI
# wrapper, and updates the health-profile skill.
#
# Does NOT start or enable the service — that is a manual step by the operator
# after reviewing configuration.
#
# Usage: ./install.sh [--source-dir DIR]
#   --source-dir: override source directory (default: directory of this script)

set -euo pipefail
umask 0077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SCRIPT_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir)
      SOURCE_DIR="$2"; shift 2;;
    *)
      echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

INSTALL_BASE="${HOME}/.hermes/profiles/health/workspace/health-connect-bridge"
DATA_DIR="${INSTALL_BASE}/data"
SECRETS_DIR="${INSTALL_BASE}/secrets"
CODE_PARENT="${INSTALL_BASE}/code"
CODE_DIR="${CODE_PARENT}/health_connect_bridge"
BIN_DIR="${INSTALL_BASE}/bin"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SKILL_DIR="${HOME}/.hermes/profiles/health/skills/health/health-connect"

TOKEN_FILE="${SECRETS_DIR}/ingest-token"
DB_FILE="${DATA_DIR}/health.sqlite3"
SERVICE_FILE="${SYSTEMD_DIR}/health-connect-bridge.service"

echo "==> Installing Health Connect Bridge"
echo "    Source:  ${SOURCE_DIR}"
echo "    Install: ${INSTALL_BASE}"

# ── Create directories with strict permissions ────────────────────────────────
install -d -m 0700 "${INSTALL_BASE}"
install -d -m 0700 "${DATA_DIR}"
install -d -m 0700 "${SECRETS_DIR}"
install -d -m 0700 "${CODE_PARENT}"
install -d -m 0700 "${CODE_DIR}"
install -d -m 0700 "${BIN_DIR}"
install -d -m 0755 "${SYSTEMD_DIR}"
install -d -m 0755 "${SKILL_DIR}"

# ── Copy only the needed runtime code files (explicit list, no extras) ────────
echo "==> Copying runtime code artifacts"
for f in cli.py identity.py __init__.py ratelimit.py receiver.py store.py validation.py; do
  if [[ ! -f "${SOURCE_DIR}/${f}" ]]; then
    echo "ERROR: required source file missing: ${SOURCE_DIR}/${f}" >&2
    exit 1
  fi
  install -m 0600 "${SOURCE_DIR}/${f}" "${CODE_DIR}/${f}"
done
echo "    Copied: cli.py identity.py __init__.py ratelimit.py receiver.py store.py validation.py"

# ── Generate token atomically if not present ─────────────────────────────────
if [[ ! -f "${TOKEN_FILE}" ]]; then
  echo "==> Generating ingest token"
  # Write to a temp file (mode 0600 via umask 0077), then rename atomically.
  TOKEN_TMP="$(mktemp "${SECRETS_DIR}/.token_tmp.XXXXXX")"
  /usr/bin/python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > "${TOKEN_TMP}"
  mv "${TOKEN_TMP}" "${TOKEN_FILE}"
  echo "    Token written to: ${TOKEN_FILE}"
  echo "    (value not displayed; read the file to configure the Android app)"
else
  echo "==> Token already exists: ${TOKEN_FILE}"
  chmod 0600 "${TOKEN_FILE}"
fi

# ── Create read-only CLI wrapper ──────────────────────────────────────────────
install -m 0755 /dev/stdin "${BIN_DIR}/health-connect-read" <<WRAPPER
#!/usr/bin/env bash
# Read-only CLI for Health Connect data. No server, no mutations.
export PYTHONPATH="${CODE_PARENT}:\${PYTHONPATH:-}"
exec /usr/bin/python3 -m health_connect_bridge.cli --db "${DB_FILE}" "\$@"
WRAPPER

# ── Install systemd user unit (idempotent: detect and update old unit) ─────────
# Detects old unit by absence of required new fields or presence of obsolete
# fields (PrivateTmp=true, python3 without full path, missing UMask/NoNewPrivileges).
# The ingest token lives in a separate file and is never touched here.
_unit_is_current() {
  local f="$1"
  [[ -f "${f}" ]]                          || return 1
  grep -q 'ExecStart=/usr/bin/python3' "${f}" || return 1
  grep -q 'UMask=0077'                 "${f}" || return 1
  grep -q 'NoNewPrivileges=true'       "${f}" || return 1
  grep -qF 'PrivateTmp=true'           "${f}" && return 1
  return 0
}

if ! _unit_is_current "${SERVICE_FILE}"; then
  install -m 0644 /dev/stdin "${SERVICE_FILE}" <<SERVICE
[Unit]
Description=Health Connect Webhook Bridge
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m health_connect_bridge.receiver \
  --host 192.168.1.65 \
  --port 9121 \
  --token-path ${TOKEN_FILE} \
  --db-path ${DB_FILE}
Environment=PYTHONPATH=${CODE_PARENT}
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
SERVICE
  echo "==> Systemd unit written: ${SERVICE_FILE}"
else
  echo "==> Systemd unit already up to date: ${SERVICE_FILE}"
fi

# ── Install health-profile skill from versioned repo template ─────────────────
SKILL_SRC="${SOURCE_DIR}/skill-template.md"
SKILL_DEST="${SKILL_DIR}/SKILL.md"
if [[ -f "${SKILL_SRC}" ]]; then
  install -m 0644 "${SKILL_SRC}" "${SKILL_DEST}"
  echo "==> Skill installed: ${SKILL_DEST}"
else
  echo "WARNING: skill-template.md not found at ${SKILL_SRC}; skipping skill install" >&2
fi

echo ""
echo "==> Installation complete."
echo ""
echo "    Next steps (run manually after review):"
echo "      1. Token file:        ${TOKEN_FILE}"
echo "         (read the file to retrieve the value for the Android app; do not output it in logs)"
echo "      2. Configure Android app X-Health-Token header with token value"
echo "      3. Enable service:    systemctl --user daemon-reload"
echo "      4.                    systemctl --user enable --now health-connect-bridge"
echo "      5. Check status:      systemctl --user status health-connect-bridge"
echo "      6. Test locally:      curl http://192.168.1.65:9121/healthz"
echo ""
echo "    Read-only CLI: ${BIN_DIR}/health-connect-read status"
echo ""
echo "    Production DB will be EMPTY until first phone sync."
echo "    HTTPS routing requires GitOps PR approval and Komodo deploy."
