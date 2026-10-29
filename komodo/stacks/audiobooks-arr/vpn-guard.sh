#!/bin/sh
# Entrypoint of the qbittorrent container: start qBittorrent only if the container has no way out
# but xray. Docker gives a container a default route only when it is on a non-internal network
# (or the host network), so a default route means the vpn kill switch is gone.
# Checked by .github/scripts/vpn_gate_check.py.
set -eu

if awk 'NR > 1 && $2 == "00000000" { found = 1 } END { exit !found }' /proc/net/route; then
  echo "vpn-guard: IPv4 default route found, refusing to start qBittorrent" >&2
  exit 1
fi

if [ -r /proc/net/ipv6_route ] &&
  awk '$1 == "00000000000000000000000000000000" && $2 == "00" && $10 != "lo" { found = 1 } END { exit !found }' /proc/net/ipv6_route; then
  echo "vpn-guard: IPv6 default route found, refusing to start qBittorrent" >&2
  exit 1
fi

# Belt and braces for anything the route check misses: a direct connection must fail
if curl -s -o /dev/null -m 5 https://1.1.1.1; then
  echo "vpn-guard: direct internet access works, refusing to start qBittorrent" >&2
  exit 1
fi

echo "vpn-guard: no direct route out, starting qBittorrent"
exec /init
