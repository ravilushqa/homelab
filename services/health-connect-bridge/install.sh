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
# Nutrition write-back is OPTIONAL and must be explicitly requested with --nutrition.
# Without --nutrition, the installer behaves exactly as before (ingest-only).
# With --nutrition:
#   - Generates a separate nutrition pairing token (never printed to stdout).
#   - Adds --nutrition-token-path and --nutrition-db-path to the systemd unit.
#   - Creates a nutrition-queue CLI wrapper.
#   - Token update detection: if the existing unit lacks nutrition args, it is updated.
#
# Usage: ./install.sh [--source-dir DIR] [--nutrition]
#   --source-dir: override source directory (default: directory of this script)
#   --nutrition:  enable nutrition write-back (opt-in; requires explicit activation)

set -euo pipefail
umask 0077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SCRIPT_DIR}"
ENABLE_NUTRITION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir)
      SOURCE_DIR="$2"; shift 2;;
    --nutrition)
      ENABLE_NUTRITION=1; shift;;
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
NUTRITION_TOKEN_FILE="${SECRETS_DIR}/nutrition-token"
NUTRITION_DB_FILE="${DATA_DIR}/nutrition.sqlite3"
SERVICE_FILE="${SYSTEMD_DIR}/health-connect-bridge.service"

echo "==> Installing Health Connect Bridge"
echo "    Source:      ${SOURCE_DIR}"
echo "    Install:     ${INSTALL_BASE}"
if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
  echo "    Nutrition:   enabled (--nutrition flag)"
else
  echo "    Nutrition:   disabled (ingest-only mode; pass --nutrition to enable)"
fi

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
for f in cli.py identity.py __init__.py ratelimit.py receiver.py store.py validation.py nutrition_queue.py nutrition_cli.py; do
  if [[ ! -f "${SOURCE_DIR}/${f}" ]]; then
    echo "ERROR: required source file missing: ${SOURCE_DIR}/${f}" >&2
    exit 1
  fi
  install -m 0600 "${SOURCE_DIR}/${f}" "${CODE_DIR}/${f}"
done
echo "    Copied: cli.py identity.py __init__.py ratelimit.py receiver.py store.py validation.py nutrition_queue.py nutrition_cli.py"

# ── Generate ingest token atomically if not present ──────────────────────────
if [[ ! -f "${TOKEN_FILE}" ]]; then
  echo "==> Generating ingest token"
  # Write to a temp file (mode 0600 via umask 0077), then rename atomically.
  TOKEN_TMP="$(mktemp "${SECRETS_DIR}/.token_tmp.XXXXXX")"
  /usr/bin/python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > "${TOKEN_TMP}"
  mv "${TOKEN_TMP}" "${TOKEN_FILE}"
  echo "    Token written to: ${TOKEN_FILE}"
  echo "    (value not displayed; read the file to configure the Android app)"
else
  echo "==> Ingest token already exists: ${TOKEN_FILE}"
  chmod 0600 "${TOKEN_FILE}"
fi

# ── Generate nutrition token atomically if nutrition is enabled ───────────────
if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
  if [[ ! -f "${NUTRITION_TOKEN_FILE}" ]]; then
    echo "==> Generating nutrition pairing token"
    NUTRITION_TOKEN_TMP="$(mktemp "${SECRETS_DIR}/.nutrition_token_tmp.XXXXXX")"
    /usr/bin/python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > "${NUTRITION_TOKEN_TMP}"
    mv "${NUTRITION_TOKEN_TMP}" "${NUTRITION_TOKEN_FILE}"
    echo "    Nutrition token written to: ${NUTRITION_TOKEN_FILE}"
    echo "    (value not displayed; read the file to configure the Android app pairing)"
  else
    echo "==> Nutrition token already exists: ${NUTRITION_TOKEN_FILE}"
    chmod 0600 "${NUTRITION_TOKEN_FILE}"
  fi
  # Verify tokens differ (must never be identical)
  INGEST_VAL="$(/usr/bin/python3 -c "p='${TOKEN_FILE}'; import pathlib; print(pathlib.Path(p).read_text().strip())")"
  NUTRITION_VAL="$(/usr/bin/python3 -c "p='${NUTRITION_TOKEN_FILE}'; import pathlib; print(pathlib.Path(p).read_text().strip())")"
  if [[ "${INGEST_VAL}" == "${NUTRITION_VAL}" ]]; then
    echo "ERROR: Ingest and nutrition tokens are identical — regenerate one of them." >&2
    exit 1
  fi
  unset INGEST_VAL NUTRITION_VAL
fi

# ── Create read-only CLI wrapper ──────────────────────────────────────────────
install -m 0755 /dev/stdin "${BIN_DIR}/health-connect-read" <<WRAPPER
#!/usr/bin/env bash
# Read-only CLI for Health Connect data. No server, no mutations.
export PYTHONPATH="${CODE_PARENT}:\${PYTHONPATH:-}"
exec /usr/bin/python3 -m health_connect_bridge.cli --db "${DB_FILE}" "\$@"
WRAPPER

# ── Create nutrition queue CLI wrapper (only when nutrition is enabled) ────────
if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
  install -m 0755 /dev/stdin "${BIN_DIR}/nutrition-queue" <<WRAPPER
#!/usr/bin/env bash
# Nutrition queue CLI — create, edit, confirm, status, cancel.
# JSON input for create/edit is read from --file or stdin (not command args).
export PYTHONPATH="${CODE_PARENT}:\${PYTHONPATH:-}"
exec /usr/bin/python3 -m health_connect_bridge.nutrition_cli --db "${NUTRITION_DB_FILE}" "\$@"
WRAPPER
  echo "==> Nutrition CLI wrapper written: ${BIN_DIR}/nutrition-queue"
fi

# ── Install systemd user unit (idempotent: detect and update old unit) ─────────
# Unit update detection:
#   - Old unit: missing /usr/bin/python3, missing UMask, has PrivateTmp, or
#     missing/present nutrition args vs. current --nutrition flag state.
# The ingest token lives in a separate file and is never touched here.
_unit_is_current() {
  local f="$1"
  [[ -f "${f}" ]]                          || return 1
  grep -q 'ExecStart=/usr/bin/python3' "${f}" || return 1
  grep -q 'UMask=0077'                 "${f}" || return 1
  grep -q 'NoNewPrivileges=true'       "${f}" || return 1
  grep -qF 'PrivateTmp=true'           "${f}" && return 1
  # Check nutrition arg alignment with current --nutrition flag
  if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
    grep -q 'nutrition-token-path'    "${f}" || return 1
  else
    grep -q 'nutrition-token-path'    "${f}" && return 1
  fi
  return 0
}

if ! _unit_is_current "${SERVICE_FILE}"; then
  if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
    install -m 0644 /dev/stdin "${SERVICE_FILE}" <<SERVICE
[Unit]
Description=Health Connect Webhook Bridge (with Nutrition Write-Back)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m health_connect_bridge.receiver \
  --host 192.168.1.65 \
  --port 9121 \
  --token-path ${TOKEN_FILE} \
  --db-path ${DB_FILE} \
  --nutrition-token-path ${NUTRITION_TOKEN_FILE} \
  --nutrition-db-path ${NUTRITION_DB_FILE}
Environment=PYTHONPATH=${CODE_PARENT}
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
SERVICE
  else
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
  fi
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
echo "      1. Ingest token file: ${TOKEN_FILE}"
echo "         (read the file to retrieve the value for the Android app; do not output it in logs)"
echo "      2. Configure Android app X-Health-Token header with ingest token value"
echo "      3. Enable service:    systemctl --user daemon-reload"
echo "      4.                    systemctl --user enable --now health-connect-bridge"
echo "      5. Check status:      systemctl --user status health-connect-bridge"
echo "      6. Test locally:      curl http://192.168.1.65:9121/healthz"
echo ""
echo "    Read-only CLI: ${BIN_DIR}/health-connect-read status"
echo ""
if [[ "${ENABLE_NUTRITION}" -eq 1 ]]; then
  echo "    Nutrition write-back:"
  echo "      - Nutrition token:  ${NUTRITION_TOKEN_FILE}"
  echo "        (read the file to retrieve the value for Android app X-Nutrition-Token header)"
  echo "      - Nutrition CLI:    ${BIN_DIR}/nutrition-queue --help"
  echo "      - Nutrition DB:     ${NUTRITION_DB_FILE}"
  echo ""
  echo "    Safe nutrition DB backup (sqlite backup API, not naive WAL file copy):"
  echo "      python3 -c \\"
  echo "        \"import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()\" \\"
  echo "        ${NUTRITION_DB_FILE} /path/to/backup/nutrition.sqlite3.bak"
  echo ""
  echo "    Rollback/upgrade path:"
  echo "      - Both DBs (health + nutrition) are independent; rollback health does not affect nutrition."
  echo "      - To disable nutrition: re-run install.sh WITHOUT --nutrition; unit will be updated."
  echo "      - To re-enable: re-run install.sh WITH --nutrition."
  echo "      - Nutrition DB is NOT deleted by rollback; data is preserved."
  echo ""
  echo "    Android cancellation limitation:"
  echo "      - Once a nutrition record is ACKed by the phone, it is stored in Health Connect."
  echo "      - Android Health Connect lacks deletion tombstones for nutrition records."
  echo "      - Cancelling an already-acked record on the server side stops future delivery,"
  echo "        but CANNOT remove the record from the phone. The server honestly reports this."
fi
echo ""
echo "    Production DB will be EMPTY until first phone sync."
echo "    HTTPS routing requires GitOps PR approval and Komodo deploy."
echo "    Privacy gate: traefik Umami exclusions for /nutrition/queue and /nutrition/ack"
echo "    must be deployed and confirmed BEFORE the health-connect-bridge Komodo stack."
