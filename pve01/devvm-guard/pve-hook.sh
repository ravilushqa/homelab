#!/bin/bash
# PVE hookscript pre-start gate for the dev VM (phase: pre-start).
# Install as a snippets volume (needs a storage with content type `snippets`) or point
# qm's hookscript at it; until that storage is confirmed, `devvm-guard start` is the only
# sanctioned start path and the watchdog enforces the same invariant.
set -u
PHASE="${1:-}"; VMID="${2:-}"
[ "$PHASE" = "pre-start" ] || exit 0
CONF="${DEVVM_CONF:-/etc/devvm-guard.conf}"
# Only gate our VM.
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"
[ "${DEV_VMID:-}" = "$VMID" ] || exit 0
/usr/local/sbin/devvm-guard verify --quiet >/dev/null 2>&1 || {
  echo "devvm-guard: refusing to start VM $VMID: host isolation guard is not verified" >&2
  exit 1
}
exit 0