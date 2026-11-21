# pve01/devvm-guard

Scoped, host-enforced isolation for exactly one dev VM on `pve01`. Design:
`docs/devvm-guard/DESIGN-DRAFT-v3.md`. Runbook: `docs/devvm-guard/APPLY-RUNBOOK.md`.

**Nothing here is applied.** This is a reviewed draft artifact for a separately authorized window.

## Files

| File | Purpose |
|---|---|
| `devvm-guard.nft.tmpl` | Policy template. Rendered values only; the table is `inet devvm`. |
| `devvm-guard` | Control script: `check`, `render`, `install`, `install-units`, `verify`, `status`, `start`, `quarantine`, `arm-deadline`, `disarm-deadline`, `rollback`, `watchdog`. `destroy` refuses: the VM is Terraform-owned. |
| `devvm-guard.conf.example` | Pinned parameters (no secrets). Copy to `/etc/devvm-guard.conf`. |
| `pve-hook.sh` | PVE `pre-start` hook, **required** before any guest start; needs an existing storage with `snippets`. |
| `systemd/` | Boot unit that only *verifies* the guard (it reloads nothing), and a 5 s watchdog that quarantines on drift. |
| `tests/netns-isolation-test.sh` | Lab test of the nft policy in a disposable user+network namespace. Must pass on the target kernel. |
| `tests/netns-bridge-ports-test.sh` | Lab test of the `vmbr1` port predicate (adoption/verify/watchdog): only VM 130's own `tap130i0` may be attached. Real TAP cases need `/dev/net/tun`; exit 77 = skipped, not passed. |
| `tests/lib-holders.sh` | Sourced by the bridge-port test: background TAP-holder bookkeeping (`bg_spawn`, `tap_hold` → `TAP_PID`, `kill_holders`). Call in the parent shell, never via `$(...)`. |
| `tests/holders-bookkeeping-test.sh` | Runs anywhere (no root/tun): holder pids are tracked, live, non-blocking and cleaned up; the bridge test never spawns holders in a subshell. |
| `tests/test_terraform_ownership.py` | Guard/Terraform ownership split: `destroy` refuses, module values match this config, docs. |

The VM itself (VMID 130, 2 vCPU, 2 GiB, one empty 32 GiB disk, one NIC on `vmbr1`, created stopped)
is created by Terraform: `terraform/modules/dev-vm`, enabled only via `dev_vm_enabled` (off by
default). This bundle never creates, changes or deletes it; it only gates its start.

## Safety properties

- **Open pre-apply blocker (UNIMPLEMENTED, `docs/devvm-guard/DESIGN-DRAFT-v3.md` 5a):** a host
  `nftables.service` stop/reload/restart flushes the whole ruleset, including `inet devvm`, and
  neither `verify` nor the periodic watchdog can stop the guest before that. Nothing here may be
  applied until a guest-stop/disconnect-before-flush mechanism (refusing the flush when it cannot
  isolate the guest) is designed, implemented and tested.
- Never flushes the global ruleset, never deletes a foreign nft table.
- Never changes `net.ipv4.ip_forward`, PVE firewall defaults, VLANs, existing routes or Tailscale.
- The private bridge is runtime-only and absent from `/etc/network/interfaces`, so host networking
  is never reloaded.
- Rollback always quarantines the guest before removing protection, and leaves the VM and its disk
  intact (Terraform owns them; `destroy` refuses instead of running `qm destroy`).
- An independent systemd deadline rolls back even if the operator's session dies.
- After a host reboot the bridge, table and sysctls are gone (runtime-only by design): verify
  fails, `start` refuses. Recover with `devvm-guard rollback` then `devvm-guard install`.

## Test

```bash
for scope in blanket bridge; do
  unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh "$scope"
done
unshare -rn --mount --propagation private bash tests/netns-bridge-ports-test.sh; echo "exit=$?"
```

Current result on `7.0.14-25-pve` (in an LXC on the PVE host kernel): 28/28 for `blanket` and 28/28
for `bridge` (egress is TCP 443 only; plain HTTP is dropped); bridge ports 22 passed, 0 failed,
9 skipped (`exit=77`: no `/dev/net/tun` in the LXC, so the real-TAP acceptance cases are **not yet
verified**; they must pass on pve01).

```bash
python3 -m unittest discover -s pve01/devvm-guard/tests -p 'test_*.py' -v   # from the repo root
terraform/tests/offline-test.sh [--terraform BIN] [--plugin-cache DIR] [--proxmox-version X.Y.Z]
```

The Terraform checks never run in `terraform/` itself (it has a Terraform Cloud block): the script
uses a scratch copy without the cloud block, state, certs or secrets, mocked providers, plan only.