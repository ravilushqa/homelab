#!/bin/bash
# Offline, credential-free Terraform checks for terraform/ (root wiring + modules/dev-vm).
#
# Why a script: the root module carries a `cloud {}` block (Terraform Cloud ravilushqa/homelab).
# `terraform init -backend=false` in the real terraform/ directory is not enough on its own: it
# still writes .terraform/ and lock changes into the repo, picks up the operator's CLI config and
# TFC credentials from $HOME, and is one forgotten flag away from talking to the workspace.
#
# This script never touches terraform/ itself. It:
#   1. copies an allowlist of files (*.tf, *.tftest.hcl, .terraform.lock.hcl, homelab.auto.tfvars,
#      module templates, and the three k8s values files main.tf reads) into a fresh scratch dir;
#      no state, no .terraform/, no sealed-secrets certs/keys, no other tfvars;
#   2. runs `terraform fmt -check -recursive` on that pristine copy;
#   3. deletes every `cloud {}` / `backend "..." {}` block from the copy and refuses to continue
#      if one survives;
#   4. runs with an empty environment (`env -i`), HOME and TF_DATA_DIR inside the scratch dir and
#      TF_CLI_CONFIG_FILE=/dev/null, so no TFC token, PROXMOX_VE_* or cloud credentials exist;
#   5. `init -backend=false` with the pinned providers (registry download or --plugin-cache),
#      `validate`, `test` for the root (all providers mock_provider) and for modules/dev-vm
#      (pinned in the copy to the same bpg/proxmox version as the root).
#
# The tests are plan-only with mocked providers: they prove HCL wiring and the provider schema,
# NOT any Proxmox API behaviour, permission or VM power state.
#
# Usage:
#   terraform/tests/offline-test.sh [--terraform BIN] [--plugin-cache DIR]
#                                   [--proxmox-version X.Y.Z] [--workdir DIR] [--keep]
#   --proxmox-version rewrites the bpg/proxmox pin IN THE COPY ONLY (e.g. to try the version on
#   main before merging it); the repo's providers.tf and lock file are never modified.
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
TF_SRC="$REPO/terraform"
TF_BIN="${TERRAFORM:-terraform}"
PLUGIN_CACHE=""
PVE_VERSION=""
WORKDIR=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --terraform) TF_BIN="$2"; shift 2 ;;
    --plugin-cache) PLUGIN_CACHE="$2"; shift 2 ;;
    --proxmox-version) PVE_VERSION="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
TF_BIN="$(command -v "$TF_BIN" || true)"
[ -x "$TF_BIN" ] || { echo "terraform binary not found (use --terraform or TERRAFORM=)" >&2; exit 2; }
[ -z "$PVE_VERSION" ] || [[ "$PVE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad --proxmox-version" >&2; exit 2; }

if [ -n "$WORKDIR" ]; then
  mkdir -p "$WORKDIR"; S="$(mktemp -d "$WORKDIR/tfoffline.XXXXXX")"
else
  S="$(mktemp -d)"
fi
case "$S" in "$REPO"|"$REPO"/*) echo "scratch dir must be outside the repo" >&2; exit 2 ;; esac
[ "$KEEP" = 1 ] || trap 'rm -rf "$S"' EXIT
W="$S/repo"; mkdir -p "$W/terraform" "$S/home"

# ---- 1. allowlisted copy --------------------------------------------------------------------
( cd "$TF_SRC" && find . -path ./.terraform -prune -o -path '*/.terraform' -prune \
    -o -path ./modules/sealed-secrets/certs -prune -o -type f \
    \( -name '*.tf' -o -name '*.tftest.hcl' -o -name '.terraform.lock.hcl' -o -name 'homelab.auto.tfvars' \
       -o -path './modules/*/patches/*.tmpl' -o -path './modules/*/inline-manifests/*.yaml' \) -print0 ) \
  | ( cd "$TF_SRC" && xargs -0 -r cp --parents -t "$W/terraform" )
# Only the module lock files we generate below may exist in modules; the root lock is the repo's.
find "$W/terraform/modules" -name .terraform.lock.hcl -delete
for f in network/cilium/values.yaml storage/proxmox-csi/values.yaml security/sealed-secrets/values.yaml; do
  mkdir -p "$W/k8s/infra/$(dirname "$f")"; cp "$REPO/k8s/infra/$f" "$W/k8s/infra/$f"
done
bad="$(find "$W" \( -name '*.tfstate*' -o -name '*.key' -o -name '*.cert' -o -name '*.pem' -o -name '*.tfvars.json' \
        -o \( -name '*.tfvars' ! -name homelab.auto.tfvars \) -o -name .terraform -o -name '*.tfrc*' \) -print)"
[ -z "$bad" ] || { echo "refusing: sensitive/stateful files in the copy: $bad" >&2; exit 1; }

RC=0
step() { echo "+ $*"; if "$@"; then echo "  rc=0"; else local r=$?; echo "  rc=$r"; RC=1; fi; }
tf() { # <dir-relative-to-copy> <args...>
  local d="$1"; shift
  env -i PATH="/usr/bin:/bin" HOME="$S/home" TF_CLI_CONFIG_FILE=/dev/null TF_IN_AUTOMATION=1 \
      CHECKPOINT_DISABLE=1 TF_INPUT=0 TF_DATA_DIR="$S/data/$(tr / _ <<<"$d")" \
      ${PLUGIN_CACHE:+TF_PLUGIN_CACHE_DIR="$PLUGIN_CACHE"} \
      "$TF_BIN" -chdir="$W/$d" "$@" -no-color
}

# ---- 2. fmt on the pristine copy --------------------------------------------------------------
step tf terraform fmt -check -recursive -diff

# ---- 3. drop cloud/backend, 3b. optional version rewrite (copy only) -------------------------
python3 -I - "$W/terraform" "$PVE_VERSION" <<'PY'
import pathlib, re, sys
root, pver = pathlib.Path(sys.argv[1]), sys.argv[2]
def drop_blocks(text, head):
    out, i = [], 0
    for m in re.finditer(head, text):
        if m.start() < i:
            continue
        j = text.index("{", m.end() - 1); depth = 0
        for k in range(j, len(text)):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            if depth == 0:
                break
        out.append(text[i:m.start()]); i = k + 1
    return "".join(out) + text[i:]
for p in root.rglob("*.tf"):
    t = p.read_text()
    n = drop_blocks(t, r'(?m)^[ \t]*cloud[ \t]*\{')
    n = drop_blocks(n, r'(?m)^[ \t]*backend[ \t]+"[^"]*"[ \t]*\{')
    if pver:
        n = re.sub(r'(source\s*=\s*"bpg/proxmox"\s*\n\s*version\s*=\s*")[^"]+(")', r"\g<1>%s\g<2>" % pver, n)
    if n != t:
        p.write_text(n)
PY
if grep -rEn '^[[:space:]]*(cloud[[:space:]]*\{|backend[[:space:]]+")' "$W/terraform" --include='*.tf'; then
  echo "refusing: a cloud/backend block survived in the copy" >&2; exit 1
fi
ROOT_PVE="$(sed -n '/source *= *"bpg\/proxmox"/{n;s/.*version *= *"\([^"]*\)".*/\1/p;q}' "$W/terraform/providers.tf")"
[ -n "$ROOT_PVE" ] || { echo "could not read the bpg/proxmox pin" >&2; exit 1; }
echo "--- scratch: $S   bpg/proxmox under test: $ROOT_PVE${PVE_VERSION:+ (overridden in the copy)}"

# The module has no own pin (the root pins it); pin the copy so its test uses the same schema.
cat > "$W/terraform/modules/dev-vm/offline_pin_override.tf" <<EOF
terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "$ROOT_PVE"
    }
  }
}
EOF

# ---- 4/5. init (no backend), validate, test ---------------------------------------------------
UPGRADE=""; [ -n "$PVE_VERSION" ] && UPGRADE="-upgrade"   # the copied lock pins the repo's version
step tf terraform init -backend=false -input=false $UPGRADE
step tf terraform validate
step tf terraform test
step tf terraform/modules/dev-vm init -backend=false -input=false
step tf terraform/modules/dev-vm validate
step tf terraform/modules/dev-vm test
# Prove which provider binary actually ran
echo "--- providers resolved:"
grep -h -A1 'provider "registry.terraform.io/bpg/proxmox"' "$W/terraform/.terraform.lock.hcl" \
  "$W/terraform/modules/dev-vm/.terraform.lock.hcl" | grep version || true

echo "---"
[ "$RC" = 0 ] && echo "RESULT: offline terraform checks PASSED (bpg/proxmox $ROOT_PVE, mocked, plan-only)" \
              || echo "RESULT: offline terraform checks FAILED"
exit "$RC"
