#!/bin/bash
# Regression for tests/lib-holders.sh, runnable anywhere (no root, no netns, no /dev/net/tun):
#
#   bash tests/holders-bookkeeping-test.sh
#
# Guards against the harness bug where tap_hold / the T7 decoy were called via $(...): the
# subshell dropped HOLDERS (cleanup leaked every holder) and the substitution blocked until the
# inheriting 600 s child exited. The TAP cases in netns-bridge-ports-test.sh SKIP without tun,
# so this check must not depend on them.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# shellcheck source=lib-holders.sh
. "$HERE/lib-holders.sh"
WORK="$(mktemp -d)"
trap 'kill_holders; rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

# H1: bg_spawn in the parent records the pid and returns at once although the child lives on.
t0=$SECONDS
bg_spawn "$WORK/a.out" python3 -c 'import time; print("x", flush=True); time.sleep(600)' -id 130
a="$BG_PID"
bg_spawn "$WORK/b.out" sleep 600
b="$BG_PID"
[ $((SECONDS - t0)) -lt 5 ] && ok "H1 bg_spawn returns immediately for a long-lived child" \
  || bad "H1 bg_spawn blocked for $((SECONDS - t0))s"

# H2: both pids are tracked in the parent's HOLDERS and are live.
[ "${#HOLDERS[@]}" = 2 ] && [ "${HOLDERS[0]}" = "$a" ] && [ "${HOLDERS[1]}" = "$b" ] \
  && ok "H2 parent HOLDERS tracks every spawned pid" || bad "H2 HOLDERS=(${HOLDERS[*]}) want ($a $b)"
alive "$a" && alive "$b" && ok "H3 spawned holders are live" || bad "H3 holder not live ($a $b)"

# H4: the child's stdout goes to its file, not the caller (the decoy-blocking root cause).
for _ in $(seq 1 50); do [ -s "$WORK/a.out" ] && break; sleep 0.05; done
[ "$(cat "$WORK/a.out")" = x ] && ok "H4 child output is redirected to its own file" || bad "H4 child output missing"
grep -qF -- "-id 130" <(tr '\0' ' ' < "/proc/$a/cmdline") \
  && ok "H5 holder argv carries '-id 130'" || bad "H5 argv: $(tr '\0' ' ' < "/proc/$a/cmdline")"

# H6: kill_holders kills and reaps all of them and empties HOLDERS.
kill_holders
! alive "$a" && ! alive "$b" && [ "${#HOLDERS[@]}" = 0 ] \
  && ok "H6 kill_holders kills every tracked holder and clears HOLDERS" || bad "H6 leaked holders"

# H7: the old bug, demonstrated: a helper called via $(...) loses its HOLDERS update.
leak_probe() { bg_spawn /dev/null sleep 30; echo "$BG_PID"; }
p="$(leak_probe)"
[ "${#HOLDERS[@]}" = 0 ] && ok "H7 \$(...) loses HOLDERS (why helpers must run in the parent)" \
  || bad "H7 expected the subshell update to be lost"
kill "$p" 2>/dev/null

# H8: tap_hold without a tun device fails cleanly, leaves TAP_PID empty, still tracks+kills its pid.
tap_hold "$WORK/missing-holder.py" "$WORK" tapnone0 130 2>/dev/null; rc=$?
n="${#HOLDERS[@]}"
kill_holders
[ "$rc" != 0 ] && [ -z "$TAP_PID" ] && [ "$n" = 1 ] && [ "${#HOLDERS[@]}" = 0 ] \
  && ok "H8 tap_hold failure: rc!=0, TAP_PID empty, holder still tracked and cleaned" \
  || bad "H8 tap_hold failure path (rc=$rc TAP_PID=$TAP_PID tracked=$n)"

# H9: the bridge-ports harness never calls the helpers through a command substitution again.
T="$HERE/netns-bridge-ports-test.sh"
if grep -nE '\$\(\s*(tap_hold|bg_spawn)\b|&\s*echo\s+\$!\s*\)' "$T"; then
  bad "H9 $T calls a holder helper via \$(...) or captures a background pid in a substitution"
else
  ok "H9 netns-bridge-ports-test.sh spawns holders only in the parent shell"
fi
grep -q 'lib-holders.sh' "$T" && ok "H10 netns-bridge-ports-test.sh uses lib-holders.sh" || bad "H10 lib not sourced"

echo "---"
printf 'RESULT[holders]: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
