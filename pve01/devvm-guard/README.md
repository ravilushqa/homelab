# pve01/devvm-guard

Scoped, host-enforced isolation for exactly one dev VM on `pve01`. Design:
`docs/devvm-guard/DESIGN-DRAFT-v3.md`. Runbook: `docs/devvm-guard/APPLY-RUNBOOK.md`.

**Nothing here is applied.** This is a reviewed draft artifact for a separately authorized window.

## Files

| File | Purpose |
|---|---|
| `devvm-guard.nft.tmpl` | Policy template. Rendered values only; the table is `inet devvm`. |
| `devvm-guard` | Control script: `check`, `render`, `install`, `install-units`, `verify`, `status`, `start`, `quarantine`, `arm-deadline`, `disarm-deadline`, `rollback`, `destroy`, `watchdog`. |
| `devvm-guard.conf.example` | Pinned parameters (no secrets). Copy to `/etc/devvm-guard.conf`. |
| `pve-hook.sh` | Optional PVE `pre-start` hook. Only usable if a storage with `snippets` exists. |
| `systemd/` | Boot unit loading/verifying the guard, and a 5 s watchdog that quarantines on drift. |
| `tests/netns-isolation-test.sh` | Lab test in a disposable user+network namespace. Must pass on the target kernel. |

## Safety properties

- Never flushes the global ruleset, never deletes a foreign nft table.
- Never changes `net.ipv4.ip_forward`, PVE firewall defaults, VLANs, existing routes or Tailscale.
- The private bridge is runtime-only and absent from `/etc/network/interfaces`, so host networking
  is never reloaded.
- Rollback always quarantines the guest before removing protection, and leaves the VM disk intact.
- An independent systemd deadline rolls back even if the operator's session dies.

## Test

```bash
unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh
```

Current result on `7.0.14-25-pve` (the PVE host kernel): `RESULT: 26 passed, 0 failed, 0 skipped`.