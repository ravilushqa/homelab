#!/bin/bash
# LAB ONLY — disposable-network-namespace regression for the dev-bridge port predicate used by
# `devvm-guard check` (adoption), `verify` (pre-start hook, post-start, watchdog) and `watchdog`.
#
#   unshare -rn --mount --propagation private bash tests/netns-bridge-ports-test.sh
#
# Real kernel objects inside a throwaway user+network namespace: bridges, veth, VLAN, the
# rendered nft table, and (when /dev/net/tun exists) real TAP devices held open by a stand-in
# "qemu" process whose pid sits in a temporary pidfile. Only the PVE CLI is a test double: a
# `qm` stub that serves a VM config/status from files (a namespace has no pmxcfs).
#
# Exit: 0 = every case ran and passed; 1 = a case failed; 77 = everything that ran passed but the
# TAP cases were SKIPPED (no /dev/net/tun here, e.g. inside an LXC). 77 is NOT a pass for the
# "expected guest tap accepted" gate: rerun on a host/VM that has /dev/net/tun.
#
# It does NOT prove real qemu-server/pve-bridge behaviour on pve01 (tap naming, fd ownership by
# the real kvm process); that remains a live-host probe in the runbook.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GUARD="$HERE/../devvm-guard"
WORK="$(mktemp -d)"
# HOLDERS, bg_spawn, tap_hold (sets TAP_PID), alive, kill_holders. Call them in THIS shell,
# never via $(...): see lib-holders.sh and tests/holders-bookkeeping-test.sh.
# shellcheck source=lib-holders.sh
. "$HERE/lib-holders.sh"
PASS=0; FAIL=0; SKIP=0
cleanup() { kill_holders; rm -rf "$WORK"; }
trap cleanup EXIT

ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf 'SKIP  %s\n' "$*"; }

HOST_NS="$(readlink /proc/1/ns/net 2>/dev/null || true)"
if [ -n "$HOST_NS" ] && [ "$HOST_NS" = "$(readlink /proc/self/ns/net)" ]; then
  echo "refusing to run in the host network namespace: use 'unshare -rn --mount --propagation private bash $0'" >&2
  exit 2
fi
nft list ruleset >/dev/null 2>&1 || { echo "no CAP_NET_ADMIN in this namespace" >&2; exit 2; }
# The guard reads /sys/class/net/<bridge>/brif. An inherited sysfs shows the PARENT namespace's
# devices (every lookup here would see "no such bridge" = "no ports", a silent false pass), so
# mount a sysfs for this namespace. Needs the private mount namespace from `unshare --mount`.
mount -t sysfs sysfs /sys 2>/dev/null || { echo "cannot mount a private sysfs: run under 'unshare -rn --mount --propagation private'" >&2; exit 2; }
[ "$(ls /sys/class/net | grep -v '^bonding_masters$' | sort | tr '\n' ' ')" = "$(ip -o link show | awk -F': ' '{sub(/@.*/,"",$2); print $2}' | sort | tr '\n' ' ')" ] \
  || { echo "sysfs does not reflect this network namespace" >&2; exit 2; }
# A fresh netns starts with br_netfilter call-ip(6)tables=1 when the module is loaded; the
# design gate requires 0 for FORWARD_DROP_SCOPE=blanket, so mirror the target host.
sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true

HAVE_TUN=0
[ -c /dev/net/tun ] && HAVE_TUN=1

# ---- config + qm stub ------------------------------------------------------------------------
GUEST_MAC=02:DE:AD:00:01:30
cat > "$WORK/test.conf" <<EOF
DEV_VMID=130
DEV_NAME=dev-vm-test
DEV_GUEST_MAC=$GUEST_MAC
DEV_BRIDGE=bdev0
DEV_HOST_IP=10.203.255.1
DEV_PREFIXLEN=30
DEV_GUEST_IP=10.203.255.2
WAN_IFACE=bwan0
WAN_PUBLIC_IP=8.8.4.4
DNS_V4="1.1.1.1, 9.9.9.9"
NTP_V4="162.159.200.1, 162.159.200.123"
CONTROL_ENABLED=0
DEADLINE_SECONDS=0
HEALTHCHECK_CMD=""
FORWARD_DROP_SCOPE=blanket
EOF

mkdir -p "$WORK/bin" "$WORK/qm" "$WORK/pid" "$WORK/state" "$WORK/run"
cat > "$WORK/bin/qm" <<'EOF'
#!/bin/sh
# Test double for the PVE CLI: config/status come from files under $QM_DIR.
echo "qm $*" >> "$QM_DIR/calls.log"
case "$1" in
  config) [ -f "$QM_DIR/config" ] || { echo "Configuration file 'nodes/x/qemu-server/$2.conf' does not exist" >&2; exit 2; }
          cat "$QM_DIR/config" ;;
  status) [ -f "$QM_DIR/config" ] || exit 2; echo "status: $(cat "$QM_DIR/status")" ;;
  stop)   echo stopped > "$QM_DIR/status" ;;
  start)  echo running > "$QM_DIR/status" ;;
  *)      exit 0 ;;
esac
EOF
chmod +x "$WORK/bin/qm"
export PATH="$WORK/bin:$PATH" QM_DIR="$WORK/qm"
export DEVVM_CONF="$WORK/test.conf" DEVVM_STATE_DIR="$WORK/state" DEVVM_RENDER_DIR="$WORK/run" \
       DEVVM_TMPL="$HERE/../devvm-guard.nft.tmpl" DEVVM_QEMU_PID_DIR="$WORK/pid"

GOOD_NET0="net0: virtio=$GUEST_MAC,bridge=bdev0,firewall=0"
vm_absent()  { rm -f "$QM_DIR/config" "$QM_DIR/status" "$WORK/pid/130.pid"; }
vm_config()  { printf 'boot: order=scsi0;ide2\nname: dev-vm\nonboot: 0\n%s\n' "$@" > "$QM_DIR/config"; }
vm_status()  { echo "$1" > "$QM_DIR/status"; }

# A stand-in qemu: holds /dev/net/tun open on a TAP named $1 and carries "-id <vmid>" in its
# argv like the real kvm process. Prints its pid once the device exists.
cat > "$WORK/holder.py" <<'PY'
import fcntl, os, struct, sys, time
name = sys.argv[1]
fd = os.open("/dev/net/tun", os.O_RDWR)
fcntl.ioctl(fd, 0x400454ca, struct.pack("16sH", name.encode(), 0x0002 | 0x1000))  # TUNSETIFF, IFF_TAP|IFF_NO_PI
print(os.getpid(), flush=True)
time.sleep(600)
PY
hold() { tap_hold "$WORK/holder.py" "$WORK" "$1" "$2"; }   # name vmid -> sets TAP_PID
untap() { kill_holders; sleep 0.1; }
clear_ports() {
  untap
  local p; for p in /sys/class/net/bdev0/brif/*; do [ -e "$p" ] && ip link del "${p##*/}" 2>/dev/null; done
  ip link del tap130i0 2>/dev/null; ip link del tap131i0 2>/dev/null; ip link del tap130i1 2>/dev/null
  rm -f "$WORK/pid/130.pid"; true
}
port() { ip link set "$1" master bdev0 && ip link set "$1" up; }

# expect_verify <0|1> <label> [regex that every FAIL line must match]
expect_verify() {
  local want="$1" label="$2" failre="${3:-}" out rc extra
  out="$("$GUARD" verify 2>&1)"; rc=$?
  if [ "$want" = 0 ]; then
    if [ "$rc" = 0 ] && ! grep -q '^FAIL' <<<"$out"; then ok "$label"
    else bad "$label (rc=$rc)"; grep '^FAIL' <<<"$out" | sed 's/^/      /'; fi
  else
    extra="$(grep '^FAIL' <<<"$out" | grep -Ev -- "$failre" || true)"
    if [ "$rc" != 0 ] && grep -Eq -- "^FAIL  .*($failre)" <<<"$out" && [ -z "$extra" ]; then ok "$label"
    else bad "$label (rc=$rc; want only FAIL lines matching /$failre/)"; grep '^FAIL' <<<"$out" | sed 's/^/      /'; fi
  fi
}

# ---- Phase A: adoption (`check`) on a pre-existing bridge, before any guard state ------------
ip link add bwan0 type bridge; ip link set bwan0 up
ip addr add 203.0.113.7/24 dev bwan0
ip link add bdev0 type bridge
ip link add hwan0 type veth peer name xwan0; ip link set hwan0 master bwan0; ip link set hwan0 up

vm_absent
"$GUARD" check >"$WORK/out" 2>&1 && ok "A1 check adopts an existing EMPTY bridge" \
  || { bad "A1 check on empty bridge"; sed 's/^/      /' "$WORK/out"; }

ip link add uplink0 type veth peer name uplink0p; port uplink0
"$GUARD" check >"$WORK/out" 2>&1; rc=$?
[ "$rc" != 0 ] && grep -q 'refusing to adopt' "$WORK/out" && ok "A2 check refuses to adopt a bridge with an external port" \
  || { bad "A2 check adopted a bridge with a port (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }
clear_ports

ip link add tap130i0 type veth peer name tap130p; port tap130i0
"$GUARD" check >"$WORK/out" 2>&1; rc=$?
[ "$rc" != 0 ] && grep -q 'refusing to adopt' "$WORK/out" && ok "A3 check refuses to adopt even with the dev tap name (guest must be stopped before install)" \
  || { bad "A3 check adopted a bridge with a port named tap130i0 (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }
clear_ports

# ---- Phase B: installed guard state (what `install` leaves behind), then verify --------------
ip link set bdev0 up
ip addr add 10.203.255.1/30 dev bdev0
sysctl -qw net.ipv6.conf.bdev0.disable_ipv6=1
sysctl -qw net.ipv4.conf.bdev0.forwarding=1
sysctl -qw net.ipv4.conf.bwan0.forwarding=1
"$GUARD" render >/dev/null 2>&1 || { echo "render failed" >&2; exit 1; }
nft -f "$WORK/run/devvm-guard.nft" || { echo "load failed" >&2; exit 1; }

vm_absent
expect_verify 0 "B1 verify: guard installed, VM not created yet, bridge empty"

vm_config "$GOOD_NET0"; vm_status stopped
expect_verify 0 "B2 verify: VM stopped, bridge empty (pre-start hook / installer transition)"

vm_status running
expect_verify 0 "B3 verify: VM starting, tap not plugged yet (bridge empty)"

# --- rejected ports (real kernel devices, no tun needed) ---
ip link add uplink0 type veth peer name uplink0p; port uplink0
expect_verify 1 "R1 external uplink port (veth) on the dev bridge is rejected" "bdev0 ports"
clear_ports

ip link add tap130i0 type veth peer name tap130p; port tap130i0
expect_verify 1 "R2 impostor veth named tap130i0 (not a TAP) is rejected" "bdev0 ports"
clear_ports

ip link add link hwan0 name hwan0.10 type vlan id 10; port hwan0.10
expect_verify 1 "R3 VLAN sub-interface of the LAN uplink on the dev bridge is rejected" "bdev0 ports"
clear_ports

ip link add fwpr130p0 type veth peer name fwln130i0; port fwpr130p0
expect_verify 1 "R4 PVE firewall-bridge port fwpr130p0 (firewall=1 layout) is rejected" "bdev0 ports"
clear_ports

ip link add dummy0 type dummy; port dummy0
expect_verify 1 "R5 dummy (non-tap) port is rejected" "bdev0 ports"
clear_ports

# --- net0 config mapping (VM 130, net0 only, our bridge/MAC, firewall off, no VLAN) ---
vm_status stopped
vm_config "net0: virtio=$GUEST_MAC,bridge=bdev0,firewall=1"
expect_verify 1 "C1 net0 with firewall=1 (would insert fwbr/fwpr ports) is rejected" "net0"
vm_config "net1: virtio=$GUEST_MAC,bridge=bdev0,firewall=0"
expect_verify 1 "C2 NIC on net1 instead of net0 (tap130i1 mapping) is rejected" "net0|NIC"
vm_config "net0: virtio=02:DE:AD:00:01:31,bridge=bdev0,firewall=0"
expect_verify 1 "C3 net0 MAC differs from DEV_GUEST_MAC is rejected" "net0"
vm_config "net0: virtio=$GUEST_MAC,bridge=bdev0,firewall=0,tag=10"
expect_verify 1 "C4 net0 with a VLAN tag is rejected" "net0"
vm_config "net0: virtio=$GUEST_MAC,bridge=bdev0,firewall=0" "net1: virtio=02:DE:AD:00:01:31,bridge=bdev0"
expect_verify 1 "C5 a second NIC is rejected" "net0|NIC"
vm_config "net0: virtio=$GUEST_MAC,bridge=vmbr0,firewall=0"
expect_verify 1 "C6 net0 on another bridge is rejected" "net0|NIC"
vm_config "net0: virtio=$GUEST_MAC,bridge=bdev0"
expect_verify 0 "C7 net0 with firewall omitted (PVE default off) is accepted"
vm_config "$GOOD_NET0"

# --- watchdog paths that need no tun ---
vm_status running
: > "$QM_DIR/calls.log"
"$GUARD" watchdog >"$WORK/out" 2>&1; rc=$?
[ "$rc" = 0 ] && ! nft list table inet devvm_quarantine >/dev/null 2>&1 && ! grep -q 'qm stop' "$QM_DIR/calls.log" \
  && ok "W1 watchdog leaves a running VM alone while the bridge is empty" \
  || { bad "W1 watchdog (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }

ip link add uplink0 type veth peer name uplink0p; port uplink0
: > "$QM_DIR/calls.log"
"$GUARD" watchdog >"$WORK/out" 2>&1; rc=$?
[ "$rc" = 1 ] && nft list table inet devvm_quarantine >/dev/null 2>&1 && grep -q 'qm stop 130' "$QM_DIR/calls.log" \
  && ok "W2 watchdog quarantines + stops the VM when an external port appears" \
  || { bad "W2 watchdog did not quarantine (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }
nft delete table inet devvm_quarantine 2>/dev/null
clear_ports

# --- start gate: no guest start without the fail-closed pre-start hookscript ---
vm_config "$GOOD_NET0"; vm_status stopped
: > "$QM_DIR/calls.log"
"$GUARD" start >"$WORK/out" 2>&1; rc=$?
[ "$rc" != 0 ] && ! grep -q 'qm start' "$QM_DIR/calls.log" && grep -q 'hookscript' "$WORK/out" \
  && ok "S1 start refuses (no qm start) while VM 130 has no hookscript" \
  || { bad "S1 start without hookscript (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }
vm_config "$GOOD_NET0" "hookscript: local:snippets/devvm-guard-pve-hook.sh"; vm_status stopped
: > "$QM_DIR/calls.log"
"$GUARD" start >"$WORK/out" 2>&1; rc=$?
[ "$rc" = 0 ] && grep -q 'qm start 130' "$QM_DIR/calls.log" \
  && ok "S2 start proceeds when the hookscript is attached and the guard verifies" \
  || { bad "S2 start with hookscript (rc=$rc)"; sed 's/^/      /' "$WORK/out"; }
vm_config "$GOOD_NET0"; vm_status stopped

# ---- Phase T: real TAP devices (need /dev/net/tun) ------------------------------------------
if [ "$HAVE_TUN" = 1 ]; then
  vm_config "$GOOD_NET0"; vm_status running

  hold tap130i0 130 && echo "$TAP_PID" > "$WORK/pid/130.pid" && port tap130i0
  expect_verify 0 "T1 expected guest tap tap130i0 held by VM 130's qemu pid is ACCEPTED"
  : > "$QM_DIR/calls.log"
  "$GUARD" watchdog >"$WORK/out" 2>&1; rc=$?
  [ "$rc" = 0 ] && ! nft list table inet devvm_quarantine >/dev/null 2>&1 && ! grep -q 'qm stop' "$QM_DIR/calls.log" \
    && ok "T2 watchdog does NOT quarantine the valid running guest" \
    || { bad "T2 watchdog quarantined a valid guest (rc=$rc)"; sed 's/^/      /' "$WORK/out"; nft delete table inet devvm_quarantine 2>/dev/null; vm_status running; }

  ip link add uplink0 type veth peer name uplink0p; port uplink0
  expect_verify 1 "T3 expected tap PLUS an external port is rejected" "bdev0 ports"
  ip link del uplink0

  hold tap131i0 131 && port tap131i0
  expect_verify 1 "T4 expected tap PLUS a foreign VM's tap (tap131i0) is rejected" "bdev0 ports: unexpected port"
  hold tap130i1 130 && ip link set tap131i0 nomaster && port tap130i1
  expect_verify 1 "T5 expected tap PLUS an additional tap130i1 is rejected" "bdev0 ports: unexpected port"
  [ "${#HOLDERS[@]}" = 3 ] && ok "T5b parent tracks all 3 TAP holders for cleanup" \
    || bad "T5b parent tracks ${#HOLDERS[@]} TAP holders, want 3 (holder leak)"
  clear_ports

  hold tap131i0 131 && port tap131i0
  expect_verify 1 "T6 a foreign VM's tap alone (tap131i0) is rejected" "bdev0 ports: unexpected port"
  clear_ports

  # Name matches, but it is held by a different process than VM 130's qemu pidfile names.
  # The decoy is a LIVE 'python3 ... -id 130' that does not hold the fd: the rejection must be
  # "not held open", not "no live qemu pid" (a dead/absent decoy would prove nothing).
  hold tap130i0 131 && real="$TAP_PID" && port tap130i0
  bg_spawn "$WORK/decoy.out" python3 -c 'import time; time.sleep(600)' -id 130; decoy="$BG_PID"
  echo "$decoy" > "$WORK/pid/130.pid"
  alive "$decoy" && ok "T7a decoy pid $decoy is live before verify" || bad "T7a decoy pid $decoy not live before verify"
  expect_verify 1 "T7 tap130i0 held by a live process other than VM 130's qemu is rejected (fd not held)" \
    "bdev0 ports: tap130i0 is not held open by VM 130's qemu"
  alive "$decoy" && ok "T7b decoy pid $decoy is still live after verify" || bad "T7b decoy pid $decoy died during verify"
  echo "$real" > "$WORK/pid/130.pid"   # pid holds the fd but its argv says -id 131
  expect_verify 1 "T8 tap130i0 whose holder is not 'qemu -id 130' is rejected" "bdev0 ports: pid [0-9]+ from the VM 130 pidfile is not"
  clear_ports

  vm_config "net0: virtio=$GUEST_MAC,bridge=bdev0,firewall=0"
  hold tap130i0 130 && echo "$TAP_PID" > "$WORK/pid/130.pid" && port tap130i0
  vm_absent
  expect_verify 1 "T9 a tap130i0 while VM 130 does not exist is rejected" "bdev0 ports"
  clear_ports
else
  for c in "T1 expected guest tap accepted" "T2 watchdog keeps valid guest" "T3 tap+external rejected" \
           "T4 tap+foreign tap rejected" "T5 tap+tap130i1 rejected" "T6 foreign tap alone rejected" \
           "T7 tap held by other (live) pid rejected" "T8 tap holder not qemu -id 130 rejected" \
           "T9 tap without VM rejected"; do
    skip "$c (no /dev/net/tun in this environment — NOT verified)"
  done
fi

echo "---"
printf 'RESULT[bridge-ports]: %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] || exit 1
[ "$SKIP" -eq 0 ] || exit 77
exit 0
