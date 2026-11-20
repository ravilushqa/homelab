#!/bin/bash
# LAB ONLY — disposable-network-namespace test of the devvm-guard nft policy.
#
# It does NOT touch the real host: run it as an unprivileged user, it re-enters a user+network
# namespace first (unshare -rn). Nothing outside the namespace is modified.
#
#   unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh
#
# What it proves (and what it does not):
#   * the rendered ruleset parses and its per-chain semantics behave as intended on THIS kernel;
#   * per-interface forwarding (ip_forward=0) actually forwards on this kernel;
#   * allowed egress (TCP 443, ICMP) works and denied destinations, source spoofing, IPv6,
#     host-INPUT and non-dev forwarded paths are dropped with the expected counters.
# It does NOT prove PVE/qemu tap behaviour, br_netfilter state, or that the guest cannot escape
# by other means. Those are separate gates in DESIGN-DRAFT-v3.md.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GUARD="$HERE/../devvm-guard"
WORK="$(mktemp -d)"
CHILDREN=()
PASS=0; FAIL=0; SKIP=0
cleanup() { for p in "${CHILDREN[@]:-}" ${NS_DEV:-} ${NS_WAN:-} ${NS_LAN:-}; do kill "$p" 2>/dev/null; done; rm -rf "$WORK"; }
trap cleanup EXIT

ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf 'SKIP  %s\n' "$*"; }

# What matters is a network namespace separate from the host's (PID 1). The uid_map check
# alone refused real root running 'unshare -rn' (map is 0->0), i.e. the runbook's command.
in_userns() { read -r _uid _lower _len < /proc/self/uid_map 2>/dev/null; [ "${_lower:-0}" != "0" ]; }
HOST_NS="$(readlink /proc/1/ns/net 2>/dev/null || true)"
SELF_NS="$(readlink /proc/self/ns/net)"
if [ -n "$HOST_NS" ] && [ "$HOST_NS" = "$SELF_NS" ]; then
  echo "refusing to run in the host network namespace: use 'unshare -rn --mount --propagation private bash $0'" >&2
  exit 2
fi
if [ -z "$HOST_NS" ] && [ "$(id -u)" -eq 0 ] && ! in_userns && [ "${DEVVM_TEST_ALLOW_ROOT:-0}" != "1" ]; then
  echo "refusing to run as real root: use 'unshare -rn --mount --propagation private bash $0'" >&2
  exit 2
fi
nft list ruleset >/dev/null 2>&1 || { echo "no CAP_NET_ADMIN in this namespace" >&2; exit 2; }

cat > "$WORK/test.conf" <<'EOF'
DEV_VMID=130
DEV_NAME=dev-vm-test
DEV_GUEST_MAC=02:DE:AD:00:01:30
DEV_BRIDGE=hdev0
DEV_HOST_IP=10.203.255.1
DEV_PREFIXLEN=30
DEV_GUEST_IP=10.203.255.2
WAN_IFACE=hwan0
WAN_PUBLIC_IP=8.8.4.4
DNS_V4="1.1.1.1, 9.9.9.9"
NTP_V4="162.159.200.1, 162.159.200.123"
CONTROL_ENABLED=0
DEADLINE_SECONDS=0
HEALTHCHECK_CMD=""
EOF

DEVVM_CONF="$WORK/test.conf" DEVVM_RENDER_DIR="$WORK" DEVVM_TMPL="$HERE/../devvm-guard.nft.tmpl" \
  "$GUARD" render >/dev/null 2>&1 || { echo "render failed" >&2; exit 1; }
RENDERED="$WORK/devvm-guard.nft"
echo "--- rendered policy ($(wc -l < "$RENDERED") lines), syntax checked by render"

# ---- namespaces (children keep their own netns; links are moved in by PID) ----
ns_spawn() { unshare -n sleep 300 >/dev/null 2>&1 & local p=$!; CHILDREN+=("$p"); echo "$p"; }
NS_DEV="$(ns_spawn)"; NS_WAN="$(ns_spawn)"; NS_LAN="$(ns_spawn)"
sleep 0.4
for p in "$NS_DEV" "$NS_WAN" "$NS_LAN"; do
  [ "$(readlink "/proc/$p/ns/net" 2>/dev/null)" != "$(readlink /proc/self/ns/net)" ] \
    || { echo "namespace child $p not in its own netns (race/unshare failure)" >&2; exit 1; }
done
ni() { local pid="$1"; shift; nsenter -t "$pid" -n "$@"; }

ip link add dev0 type veth peer name hdev0 >/dev/null
ip link add wan0 type veth peer name hwan0 >/dev/null
ip link add lan0 type veth peer name hlan0 >/dev/null
ip link set dev0 netns "$NS_DEV"; ip link set wan0 netns "$NS_WAN"; ip link set lan0 netns "$NS_LAN"

# guest / "internet" / "LAN" ends
ni "$NS_DEV" ip link set lo up
ni "$NS_DEV" ip link set dev0 up
ni "$NS_DEV" ip addr add 10.203.255.2/30 dev dev0
ni "$NS_DEV" ip route add default via 10.203.255.1
ni "$NS_DEV" ip addr add fd00:203::2/64 dev dev0 nodad
ni "$NS_DEV" ip -6 route add default via fd00:203::1

ni "$NS_WAN" ip link set lo up
ni "$NS_WAN" ip link set wan0 up
ni "$NS_WAN" ip addr add 203.0.113.8/24 dev wan0
ni "$NS_WAN" ip addr add 8.8.8.8/32 dev wan0
ni "$NS_WAN" ip addr add 8.8.4.4/32 dev wan0
ni "$NS_WAN" ip route add default via 203.0.113.7
ni "$NS_WAN" ip addr add 2001:db8::8/128 dev wan0 nodad

ni "$NS_LAN" ip link set lo up
ni "$NS_LAN" ip link set lan0 up
ni "$NS_LAN" ip addr add 192.168.1.3/24 dev lan0
ni "$NS_LAN" ip route add default via 192.168.1.100

# router (this namespace)
ip link set hdev0 up; ip link set hwan0 up; ip link set hlan0 up
ip addr add 10.203.255.1/30 dev hdev0
ip addr add fd00:203::1/64 dev hdev0 nodad
ip addr add 203.0.113.7/24 dev hwan0
ip route add 8.8.8.8/32 via 203.0.113.8 dev hwan0
ip route add default via 203.0.113.8 dev hwan0
ip addr add 2001:db8::7/128 dev hwan0 nodad
ip route add 2001:db8::8/128 dev hwan0
ip addr add 192.168.1.100/24 dev hlan0

sysctl -qw net.ipv4.conf.hdev0.forwarding=1
sysctl -qw net.ipv4.conf.hwan0.forwarding=1
sysctl -qw net.ipv4.conf.hlan0.forwarding=1
sysctl -qw net.ipv6.conf.hdev0.forwarding=0
sysctl -qw net.ipv4.conf.all.rp_filter=0
sysctl -qw net.ipv4.conf.hdev0.rp_filter=0
[ "$(sysctl -n net.ipv4.ip_forward)" = "0" ] && ok "global ip_forward stays 0 in this test" \
  || bad "global ip_forward was not 0 before the test"

# a listener on the "internet" host, port 443 (allowed) and port 22 (not allowed)
cat > "$WORK/listen.py" <<'PY'
import socket, sys, threading
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((host, port)); s.listen(4); s.settimeout(60)
def serve():
    while True:
        try:
            c, _ = s.accept(); c.close()
        except Exception:
            return
serve()
PY
cat > "$WORK/connect.py" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
src = sys.argv[3] if len(sys.argv) > 3 else None
s = socket.socket(); s.settimeout(2)
try:
    if src: s.bind((src, 0))
    s.connect((host, port)); print("connected"); sys.exit(0)
except Exception as e:
    print("blocked:%s" % type(e).__name__); sys.exit(1)
PY

tcp_probe() { # host port expected(yes|no) label [src] [expect_err]
  local host="$1" port="$2" expect="$3" label="$4" src="${5:-}" want="${6:-}"
  local out
  [ "${DEVVM_TEST_DEBUG:-0}" = "1" ] && echo "      [dbg] $label src='$src' counters: $(cget_line)"
  out="$(ni "$NS_DEV" timeout 5 python3 "$WORK/connect.py" "$host" "$port" $src 2>&1)"
  if [ "$expect" = yes ]; then
    [[ "$out" == connected* ]] && ok "$label" || bad "$label (got: $out)"
  elif [ -n "$want" ]; then
    [[ "$out" == *"$want"* ]] && ok "$label" || bad "$label (expected $want, got: $out)"
  else
    [[ "$out" == blocked:* ]] && ok "$label" || bad "$label (got: $out)"
  fi
}

cget_line() { nft list counters table inet devvm 2>/dev/null | tr -d '\n\t' | sed 's/counter /\n/g' | awk '{printf "%s ", $1}' ; }
nft -f "$RENDERED" || { echo "loading rendered policy failed" >&2; exit 1; }
echo "--- policy loaded; counters:"; nft list counters table inet devvm | sed 's/^/    /'

# ---- listeners on the "internet" side keep drop vs refusal distinguishable ----
LISTENERS=()
for target in 8.8.8.8:443 8.8.4.4:443 8.8.8.8:22; do
  ni "$NS_WAN" python3 "$WORK/listen.py" "${target%:*}" "${target##*:}" >/dev/null 2>&1 &
  LISTENERS+=($!)
done
sleep 0.6

cval() { nft list counter inet devvm "$1" 2>/dev/null | sed -n 's/.*packets \([0-9]*\).*/\1/p'; }
assert_delta() { # counter before [min]
  local now; now="$(cval "$1")"; now="${now:-0}"
  [ "$now" -ge $(( $2 + ${3:-1} )) ] && ok "C:$1 +$((now-$2))" || bad "C:$1 did not increase (before=$2 now=$now)"
}

b=""
# ---- positive cases ----
b="$(cval accepted_internet)"
tcp_probe 8.8.8.8 443 yes "P1 dev -> internet TCP/443 allowed"
assert_delta accepted_internet "$b"

if command -v ping >/dev/null; then
  ni "$NS_DEV" ping -c1 -W2 -q 8.8.8.8 >/dev/null 2>&1 && ok "P2 dev -> internet ICMP echo allowed" \
    || bad "P2 dev -> internet ICMP echo should be allowed"
else
  skip "P2 ICMP (no ping binary)"
fi

# ---- negative cases (each also asserts the exact counter moved) ----
b="$(cval denied_internal)"
tcp_probe 192.168.1.3 80 no "N1 dev -> LAN 192.168.1.3:80 blocked"
assert_delta denied_internal "$b"

b="$(cval denied_internal)"
tcp_probe 169.254.169.254 80 no "N2 dev -> link-local metadata :80 blocked"
assert_delta denied_internal "$b"

b="$(cval denied_host_input)"
tcp_probe 10.203.255.1 22 no "N3 dev -> host 10.203.255.1:22 blocked (host INPUT)"
assert_delta denied_host_input "$b"

b="$(cval denied_egress_other)"
tcp_probe 8.8.8.8 22 no "N4 dev -> internet port outside allow-list dropped (silent, no RST)" "" TimeoutError
assert_delta denied_egress_other "$b"

b="$(cval denied_internal)"
tcp_probe 8.8.4.4 443 no "N5 dev -> homelab public IP (hairpin) blocked"
assert_delta denied_internal "$b"

ni "$NS_DEV" ip addr add 10.203.255.6/32 dev dev0 nodad 2>/dev/null
b="$(cval denied_spoof)"
tcp_probe 8.8.8.8 443 no "N6 dev with spoofed source 10.203.255.6 blocked" 10.203.255.6
assert_delta denied_spoof "$b"
ni "$NS_DEV" ip addr del 10.203.255.6/32 dev dev0 2>/dev/null

b="$(cval denied_other_forward)"
out="$(ni "$NS_LAN" timeout 4 python3 "$WORK/connect.py" 10.203.255.2 22 2>&1)"
[[ "$out" == blocked:* ]] && ok "N7 LAN -> dev is not routed (original boundary kept)" \
  || bad "N7 LAN -> dev should be blocked (got: $out)"
assert_delta denied_other_forward "$b"

b="$(cval denied_other_forward)"
out="$(ni "$NS_LAN" timeout 4 python3 "$WORK/connect.py" 8.8.8.8 443 2>&1)"
[[ "$out" == blocked:* ]] && ok "N8 LAN -> internet routed path stays closed" \
  || bad "N8 LAN -> internet must stay closed (got: $out)"
assert_delta denied_other_forward "$b"

if command -v ping >/dev/null; then
  b="$(cval denied_ipv6)"
  ni "$NS_DEV" ping -c1 -W2 -q 2001:db8::8 >/dev/null 2>&1 && bad "N9 dev -> IPv6 should be blocked" \
    || ok "N9 dev -> IPv6 blocked"
  assert_delta denied_ipv6 "$b"
else
  skip "N9 IPv6 (no ping binary)"
fi


# ---- Phase 2: optional control path M (CONTROL_ENABLED=1) ----
# Proves the narrow DNAT exception works for exactly one source and that another LAN host
# cannot use it, while the guest still cannot start flows toward the host.
cp "$WORK/test.conf" "$WORK/test-m.conf"
cat >> "$WORK/test-m.conf" <<'EOF'
CONTROL_ENABLED=1
CONTROL_SRC=192.168.1.65
CONTROL_PORT=22220
GUEST_SSH_PORT=22
EOF
mkdir -p "$WORK/m"
DEVVM_CONF="$WORK/test-m.conf" DEVVM_RENDER_DIR="$WORK/m" DEVVM_TMPL="$HERE/../devvm-guard.nft.tmpl" \
  "$GUARD" render >/dev/null 2>&1 || { echo "M render failed" >&2; FAIL=$((FAIL+1)); }

ni "$NS_LAN" ip addr add 192.168.1.65/32 dev lan0
ni "$NS_LAN" ip addr add 192.168.1.66/32 dev lan0
ni "$NS_DEV" python3 "$WORK/listen.py" 10.203.255.2 22 >/dev/null 2>&1 &
GUEST_LISTENER=$!
sleep 0.4

nft delete table inet devvm
nft -f "$WORK/m/devvm-guard.nft" || { echo "M policy load failed" >&2; FAIL=$((FAIL+1)); }

out="$(ni "$NS_LAN" timeout 5 python3 "$WORK/connect.py" 192.168.1.100 22220 192.168.1.65 2>&1)"
[[ "$out" == connected* ]] && ok "M-P1 control source 192.168.1.65 -> dev:22 via DNAT works" \
  || bad "M-P1 control path failed (got: $out)"
c="$(cval accepted_control)"; c="${c:-0}"
[ "$c" -gt 0 ] && ok "C:accepted_control counted $c packets" || bad "C:accepted_control did not move"

out="$(ni "$NS_LAN" timeout 4 python3 "$WORK/connect.py" 192.168.1.100 22220 192.168.1.66 2>&1)"
[[ "$out" == blocked:* ]] && ok "M-N1 other LAN host cannot use the control port" \
  || bad "M-N1 unexpected access from 192.168.1.66 (got: $out)"

# Must be a silent drop (TimeoutError): a refused connection would mean the SYN was forwarded.
tcp_probe 192.168.1.65 22 no "M-N2 dev still cannot open flows to the LAN (dropped, no RST)" "" TimeoutError

kill "$GUEST_LISTENER" 2>/dev/null

for p in "${LISTENERS[@]:-}"; do kill "$p" 2>/dev/null; done

echo "---"
printf 'RESULT: %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]