# shellcheck shell=bash
# Background-process bookkeeping for the devvm-guard lab tests. Sourced, not executed.
#
# Every helper here MUST be called directly in the parent shell, never via $(...):
#   - a command substitution is a subshell, so HOLDERS+=() / BG_PID would be lost there
#     (cleanup then kills nothing and every holder leaks), and
#   - the substitution waits for EOF on its pipe, which a background child inheriting stdout
#     keeps open for the child's whole lifetime (a 600 s sleep blocks the test for 600 s).
# Results come back in globals instead: BG_PID / TAP_PID. tests/holders-bookkeeping-test.sh
# checks this contract without /dev/net/tun.

HOLDERS=()
BG_PID=""
TAP_PID=""

# bg_spawn <outfile> <cmd...>: start cmd in the background with stdout+stderr redirected to
# <outfile> (never the caller's stdout), record its pid in HOLDERS and BG_PID.
bg_spawn() {
  local out="$1"; shift
  "$@" > "$out" 2>&1 < /dev/null &
  BG_PID=$!
  HOLDERS+=("$BG_PID")
}

# alive <pid>: the process exists and is not a zombie.
alive() {
  local st
  [ -n "${1:-}" ] && [ -r "/proc/$1/stat" ] || return 1
  st="$(sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f1)"
  [ -n "$st" ] && [ "$st" != Z ] && [ "$st" != X ]
}

# kill_holders: kill and reap every recorded holder, then forget them.
kill_holders() {
  local p
  for p in "${HOLDERS[@]}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  for p in "${HOLDERS[@]}"; do [ -n "$p" ] && wait "$p" 2>/dev/null; done
  HOLDERS=()
}

# tap_hold <holder.py> <workdir> <tap> <vmid>: start a stand-in qemu holding TAP <tap> open
# with "-id <vmid>" in argv; sets TAP_PID. Returns 1 (TAP_PID="") if the tap did not appear
# or the holder died.
tap_hold() {
  local py="$1" work="$2" name="$3" vmid="$4" out
  out="$work/holder.$name.out"
  TAP_PID=""
  bg_spawn "$out" python3 "$py" "$name" -id "$vmid"
  for _ in $(seq 1 50); do [ -s "$out" ] && break; sleep 0.05; done
  if ! ip link show dev "$name" >/dev/null 2>&1 || ! alive "$BG_PID"; then
    echo "tap $name not created/held: $(cat "$out" 2>/dev/null)" >&2; return 1
  fi
  TAP_PID="$BG_PID"
}
