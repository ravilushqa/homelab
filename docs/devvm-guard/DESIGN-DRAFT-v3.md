# One dev VM with external isolation — DRAFT v3 (S / G1 / G2 / G3 / M)

> **PRE-APPLY BLOCKER — UNIMPLEMENTED (section 5a):** `nftables.service` stop/reload/restart
> globally flushes the ruleset, and nothing in this bundle can stop the guest *before* that flush.
> No apply (host or Terraform enablement) until that gate, the real-TAP validation and the other
> feasibility gates in section 5 pass. The watchdog gap is **not** an accepted risk.

Status: **draft bundle, nothing applied.** Design approved by Ravil on 2026-11-20
("делаем минимальную dev-VM и тестовый PR"); that approval covers the design and this draft PR,
not apply, merge, host reboot or any credentialed runtime.

- Owner task: `t_85601c57` (parent `t_b2969405`, root card `t_2d311980`).
- Kernel under test: `7.0.14-25-pve` (Lab runs inside an unprivileged user+network namespace on
  the same PVE host, so the kernel semantics are the production kernel's).
- Bundle: `pve01/devvm-guard/` (policy template, `devvm-guard` control script, systemd units,
  PVE hook, namespace lab test). Runbook: `docs/devvm-guard/APPLY-RUNBOOK.md`.
- VM provisioning: `terraform/modules/dev-vm` (opt-in `dev_vm_enabled`, **off by default**); see 2a.

## 1. Threat model and scope

Inside the threat model: **guest root**. The developer inside the VM is root of the guest and may
change addresses, routes, MAC, its own firewall, add interfaces or kernel modules. None of that may
grant access to the LAN, PVE/host, metadata/link-local, other internal prefixes or IPv6.

Outside the threat model: root on `pve01` itself, and holders of `VM.Config`/`VM.Allocate` on the
PVE API (they are the trusted control plane). The dev guest never receives PVE credentials, host
mounts, `docker.sock`, an SSH agent or infrastructure secrets.

Consequences implemented here:

- enforcement is **host kernel state**, not guest firewall state;
- the guest is on a bridge with **no uplink**, so there is no L2 path to the LAN even with a
  spoofed MAC/VLAN tag;
- the only allowed egress is host-routed IPv4 to the public Internet (DNS to the pinned resolvers,
  **TCP 443 only**, NTP to the pinned servers, rate-limited ICMP), NATed out `vmbr0`; plain HTTP
  (TCP 80) is dropped, so APT/netinst use HTTPS mirrors (runbook step 8);
- management is **Proxmox console** by default; the optional narrow `M` control path can be added.

## 2. Components

| Label | What | Honest scope of the change |
|---|---|---|
| **S** | One VM: VMID 130 `dev-vm` on `pve01`, 2 vCPU / 2048 MiB / one empty 32 GiB `nvme-thin` disk, one virtio NIC `02:DE:AD:00:01:30` on the private bridge `vmbr1`, `firewall=0`, `onboot=0`, created stopped, no HA; installer = the pre-verified Debian 13.7 netinst ISO on `ide2`. **Created by Terraform** (`terraform/modules/dev-vm`) | New guest (Terraform) + runtime-only private bridge (guard) |
| **G1** | Per-interface IPv4 forwarding only: `net.ipv4.conf.vmbr1.forwarding=1`, `net.ipv4.conf.vmbr0.forwarding=1`; global `net.ipv4.ip_forward` stays **0**; IPv6 disabled on `vmbr1` | Enables forwarding for packets arriving on `vmbr0` too — **this changes the host's routed boundary**, which is why G2 ends with an explicit deny for every other forwarded path |
| **G2** | One owned nft table `inet devvm`: source pinning, deny LAN/PVE/link-local/metadata/CGNAT/special-use/public-hairpin, IPv6 drop, host-INPUT drop from the dev bridge, allow-list (DNS/TCP 443/NTP/ICMP; no plain HTTP), `masquerade` for the dev source out `vmbr0`, final drop for all non-dev forwarded traffic | Host-wide `forward` hook; the final drop restores exactly the pre-change "nothing is routed" boundary. Does not touch other tables, PVE firewall defaults, VLANs, Tailscale or existing NAT |
| **G3** | Fail-closed lifecycle: `devvm-guard verify` gate on every start (required PVE `pre-start` hookscript + `devvm-guard start`); `verify` accepts on `vmbr1` no port at all or only `tap130i0` proven to be VM 130's own qemu TAP with the reviewed net0; 5 s watchdog that quarantines the guest if the guard drifts/disappears, independent systemd deadline that rolls back even if the operator's session dies | Adds units/timer. **UNIMPLEMENTED / PRE-APPLY BLOCKER:** `nftables.service` stop/reload/restart (its `ExecStop`/`ExecReload` flush the whole ruleset) removes `inet devvm` before `verify`/watchdog can act, so the guest is briefly fail-open. Not accepted as residual risk; see 5a |
| **M** | Optional, default **off**: host DNAT `192.168.1.65:22220 -> guest:22`, source-limited, plus one matching ingress rule | New narrow DNAT/control exception. Packet-tested in the lab (below); enabling it in production is a separate, explicit decision |

## 2a. Ownership: Terraform provisions, the host guard enforces

| Object | Owner | Why |
|---|---|---|
| VM definition, empty disk, NIC, installer CD-ROM | Terraform, `bpg/proxmox` (pinned in `terraform/providers.tf`: 0.122.0 on this branch, 0.123.1 on `main`; module tests pass with both), existing API token, run by CI — never from the root console | Plain PVE API (`VM.Allocate`, `VM.Config.*`, `Datastore.AllocateSpace`, ISO read, `SDN.Use` on the bridge). Root is **not** needed to allocate a VM, a disk or a NIC |
| Power state (start/stop/quarantine) | `devvm-guard` | Start must be gated by a verified host policy. Terraform has `started = false` (creation only), `on_boot = false`, `reboot_after_update = false` and `ignore_changes = [started]`, so no plan starts, stops or reboots the guest |
| Hookscript (**required** before any start) | root console | PVE accepts `hookscript` only from a `root@pam` password session; API tokens are rejected. Terraform ignores `hook_script_file_id` so it never removes it |
| `vmbr1`, nft table, per-interface forwarding | `devvm-guard` | Runtime-only host kernel state (below) |
| VM/disk deletion | Terraform only | `prevent_destroy`; removal is a reviewed PR whose plan shows exactly one destroy. `devvm-guard destroy` refuses and never runs `qm destroy` |

Provisioning choice: an **empty managed disk plus the already verified netinst ISO** as CD-ROM.
Rejected alternatives (0.122.0; the 0.123.1 resource docs are unchanged here): `disk.file_id` imports need the provider's SSH connection to the
node (no SSH credential is acceptable); `disk.import_from` is API-native but needs an image already
stored with the `import` content type and import permissions on that storage, i.e. a new storage
content type and a downloaded (moving) cloud image. The netinst ISO does not consume cloud-init, so
the module has no `initialization` block; the OS is installed on the PVE console (runbook step 8) over HTTPS. The display stays the default
VGA on purpose (installer in the PVE noVNC console); the serial socket is kept for `qm terminal`.
The OS installation and the runtime end-to-end checks have **not** been performed yet.

Order (runbook): reviewed enablement PR, plan only → approved host bootstrap; `devvm-guard install`
creates the runtime bridge → separately approved Terraform apply (CI, API token) creates the stopped
VM → root attaches the fail-closed hookscript → `devvm-guard start` and probes. This does not rely
on PVE accepting a NIC on a bridge that does not exist. A failed apply may leave a partial stopped
VM/disk (several API calls); it is inspected (Terraform state + `qm config 130`) and resolved in a
reviewed change, never by automatic delete/recreate/import.

Terraform checks run offline with `terraform/tests/offline-test.sh`: a scratch copy without the
Terraform Cloud block, state, certs or secrets; `init -backend=false`, `validate`, `test` with
mocked providers. That proves wiring and schema only, not API calls, permissions or power state.
No `provisioner`, `local-exec`, `remote-exec`, `null_resource` or SSH block is used.

Bridge: the provider does offer `proxmox_network_linux_bridge` (0.122.0; the
`proxmox_virtual_environment_network_linux_bridge` name is deprecated). It manages a **persistent**
bridge in `/etc/network/interfaces` and, with the default `reload = true`, reloads the node network,
which applies **every** pending staged network change on the host, not just this bridge. That
conflicts with this design: `vmbr1` is **runtime-only** and guard-owned, created by
`devvm-guard install`, removed by `rollback`, and absent after a reboot so the VM cannot start
until the guard is re-installed. Having Terraform own it too would create a second owner that
re-creates the bridge without the nft policy. The bridge therefore stays in the guard; changing that
needs a separate design decision.

## 3. Live facts used (re-verified 2026-11-20 ~14:20–14:40 CET, read-only)

API `https://192.168.1.100:8006`, PVE `9.2.21`, single node `pve01`.

- QEMU guests: only `105 haos12.4`. `cluster/nextid=100`; **VMID 130 is free**.
- LXC: 103, 110 (`192.168.1.3`), 112, 114, 117 (`192.168.1.2`), 122, 125
  (`192.168.1.30`, plus `192.168.50.1/24` on a `tag=50` interface). So **192.168.50.0/24 is in use**
  (VLAN 50) — it is inside the denied RFC1918 space, no special case is needed.
- Node network as configured: `vmbr0` (`eno1`, `192.168.1.100/24`, gw `192.168.1.1`), `eno1`,
  `wlp1s0`, `wlp2s0`. No second bridge, no SDN, no `vmbr1`.
- Storage: `raid1` (images,rootdir), `nvme-thin` (rootdir,images), `smb` (images),
  `pbs-s3-blackbaze` (backup), `local-lvm` (rootdir,images). **No storage in the API list advertises
  the `snippets` content type**, and `local` is invisible to this token (permission-filtered, and
  earlier calls to `/storage/local/content|status` returned 403 rather than 404). Consequence:
  **the PVE hookscript path cannot be assumed** — and the hook is required before any guest start, so
  the start is blocked until an admin confirms an **existing** snippets storage
  (`pvesm status --content snippets`); enabling `snippets` or widening storage permissions for this
  is a separate decision.
- `net.ipv4.ip_forward=0`, `net.ipv6.conf.all.forwarding=0`; `nftables.service` inactive with an
  `ExecStop` that flushes the ruleset; both PVE firewall daemons active with unproven enforcement.
- API token is admin-level at `/`, but `POST /nodes/pve01/execute` = `403 (user != root@pam)`, and
  `root`/`claw` SSH to `pve01` is refused. **Host enforcement and its verification need a root
  console**, but that is **not** the only remaining blocker: the nftables-flush gate (5a), the
  real-TAP validation (gate 7) and the other feasibility gates in section 5 are open too. No
  additional root diagnostics are requested now.

### 10.203.255.0/30 occupancy — what could be verified

| Check | Result |
|---|---|
| Repository references (`grep -rn 10.203.255`) | none |
| PVE guest configs (all 8 guests) | no address in `10.203.255.0/30`; all guests are on `192.168.1.0/24` / VLAN 50 |
| Route on the Hermes host (`ip route get 10.203.255.2`) | only the default via `192.168.1.1`; no specific route |
| Router DHCP reservations / static routes / VPN pools | **not verifiable without admin** — confirm at the window: `ip route show | grep 10.203.255` on pve01 and the router's DHCP/route tables |

The `/30` is only used by the host side (`10.203.255.1`) and the guest (`10.203.255.2`); it is never
routed toward the LAN, so even a collision would be limited to the private segment — but it must
still be confirmed free before allocation.

## 4. Lab evidence (real, reproducible)

Command (unprivileged, disposable user+network namespace on the PVE host):

```bash
cd pve01/devvm-guard
for scope in blanket bridge; do
  unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh "$scope"
done
```

Latest run: **28 passed / 0 failed for `blanket` and 28 passed / 0 failed for `bridge`** (26 earlier
cases plus N4b and its counter). The lab
uses a production-shaped topology (one LAN/uplink bridge with ports, one portless dev bridge), so
the `bridge` fallback is validated, not just described.

| Case | Assertion | Result |
|---|---|---|
| G1 | per-interface forwarding works while global `ip_forward` stays 0 | PASS |
| P1 | dev -> Internet TCP/443 (listener present) | allowed, counter `accepted_internet` +1 |
| P2 | dev -> Internet ICMP echo | allowed |
| N1 | dev -> LAN `192.168.1.3:80` | dropped, `denied_internal` +2 |
| N2 | dev -> `169.254.169.254:80` (metadata) | dropped, `denied_internal` +2 |
| N3 | dev -> host `10.203.255.1:22` | dropped, `denied_host_input` +2 |
| N4 | dev -> Internet port 22 (listener present, silent drop, no RST) | dropped, `denied_egress_other` +2 |
| N4b | dev -> Internet TCP/80 (listener present; plain HTTP) | dropped silently, `denied_egress_other` +1 |
| N5 | dev -> homelab public IP (hairpin) | dropped, `denied_internal` +2 |
| N6 | dev with spoofed source `10.203.255.6` | dropped, `denied_spoof` +2 |
| N7 | LAN -> dev | dropped, `denied_other_forward` +2 |
| N8 | LAN -> Internet through the host | dropped, `denied_other_forward` +2 |
| N9 | dev -> IPv6 | dropped, `denied_ipv6` +2 |
| M-P1 | `192.168.1.65:22220` -> guest:22 via DNAT with `CONTROL_ENABLED=1` | connected, `accepted_control` +1 |
| M-N1 | another LAN host -> same port | blocked |
| M-N2 | dev -> LAN with M enabled | dropped silently (TimeoutError, no RST) |

Bridge-port predicate (`tests/netns-bridge-ports-test.sh`, same namespace method, private sysfs):
adoption refuses any port; `verify` passes with an empty bridge for every stopped/starting
transition; it rejects an external veth uplink, a VLAN sub-interface of the LAN uplink, a dummy
port, a `fwpr130p0` firewall-bridge port, an impostor veth named `tap130i0`, and a net0 with
`firewall=1`, `net1`, a different MAC, a VLAN tag, a second NIC or another bridge; the watchdog
quarantines on an external port and leaves a running guest alone on an empty bridge; `start` refuses
(no `qm start`) while VM 130 has no devvm-guard hookscript attached. Latest run in
this LXC: **22 passed, 0 failed, 9 skipped (exit 77)**. The 9 skipped cases need `/dev/net/tun`
(absent here): the expected `tap130i0` held by VM 130's qemu accepted (and not quarantined by the
watchdog), and an extra external port, a foreign `tap131i0`, `tap130i1`, a tap held by another
process or not by `qemu -id 130`, and a tap without the VM rejected. They are **not verified yet**
and must pass on pve01 (runbook step 2).

What this **does not** prove: real qemu-server tap naming/fd ownership on the real `vmbr1`, the live
`br_netfilter` state, PVE firewall interaction, or that `qm` attaches exactly one NIC. Those are
checked by `devvm-guard check`/`verify` and the acceptance probes in the runbook, in the real
window.

## 5. Blocking gates before any production apply

1. **Root console on pve01**, opened first and kept open as an independent rollback channel.
2. `br_netfilter`: `net.bridge.bridge-nf-call-iptables` and `-ip6tables` must be `0`. The kernel
   reports `1` by default whenever `br_netfilter` is loaded (a fresh namespace on `pve01` shows `1`),
   so this is a **real, unresolved precondition**, not a formality. If either is `1`, bridged guest
   frames would traverse the `inet forward` hook and the blanket final drop would cut connectivity
   for **existing** guests (105, LXC traffic, VLAN 50). `devvm-guard check` and `verify` both refuse
   in that state. In that case, stop and choose one of:
   - **Variant G2-scoped** (`FORWARD_DROP_SCOPE=bridge`, **lab-validated 28/28**): the final rule is
     matched on the bridge devices only (`iifname { "vmbr0", "vmbr1" }`), so bridged frames are
     untouched because their `iif` is the bridge *port*. Operator directive (2026-11-20): if
     `bridge-nf-call-iptables=1` at the window, use this variant, not the blanket drop;
   - or disable `br_netfilter` host-wide — only with explicit approval, since it changes what
     `pve-firewall`/iptables see.
3. `ip_forward=0` and **no per-interface `forwarding=1`** anywhere except the dev bridge. If the host
   already routes, this design's "restore the original boundary" argument does not hold; re-plan.
4. Subnet `/30` confirmed free (host + router), VMID 130 free, `nvme-thin` capacity (32 GiB), MAC
   free, before the `dev_vm_enabled = true` PR is merged; the Terraform token has the privileges in
   2a; the CI plan shows exactly one VM to add.
5. An **existing** snippets storage confirmed: the pre-start hookscript is required before any
   guest start (enabling `snippets` or widening storage permissions for this is out of scope).
6. Independent review of this bundle; `nft -c` on the rendered file plus the lab suite on the target
   host.
7. `tests/netns-bridge-ports-test.sh` passes on pve01 with `exit=0` (real TAP cases run).
   `exit=77` (TAP cases skipped, e.g. no `/dev/net/tun`) is **not** a pass.
8. **The nftables-flush gate in 5a is designed, implemented and tested.** It is not today.

### 5a. PRE-APPLY BLOCKER — global ruleset flush (UNIMPLEMENTED)

The host's `nftables.service` has an `ExecStop` (and reload) that flushes the **whole** ruleset.
`verify` runs only at sanctioned starts and the watchdog is periodic, so neither can stop the guest
*before* such a flush: between the flush and the next watchdog tick the guest is routed without
`inet devvm` (fail-open). This draft does **not** solve it and does **not** accept it:

- Required before any apply: a mechanism, implemented and tested, that **confirms the guest is
  stopped or its `tap130i0` is disconnected from `vmbr1` BEFORE** any `nftables.service`
  start/stop/reload/restart or any other global flush (`nft flush ruleset`, `nft -f` of a file
  that begins with a flush), and that **refuses the flush** if it cannot isolate the guest first.
- Not in this bundle: no change to `nftables.service`, no drop-in, no wrapper. Designing that
  lifecycle is follow-up work with its own review.
- No approval exists to treat the watchdog interval as acceptable exposure.

Root scope, so the blocker is not mistaken for "just needs a console": host root is needed only
for the guard's files and units, the runtime private bridge, the nft table, the per-interface
`sysctl` deltas, the root-only hookscript setter, and rollback. Creating the VM, disk and NIC
(PVE API token, Terraform, off by default) and installing the guest OS through `VM.Console` are
**not** inherently host-root operations. An open root console is therefore **not** the only
remaining blocker, and no further root diagnostics are requested at this stage.

### Emergency if `br_netfilter` flips to `1` while the guard is loaded

The dev VM is quarantined by the watchdog automatically, but that does not restore other guests.
On the root console, remove **only** the final drop rule (never the whole table, never a flush):

```bash
nft -a list chain inet devvm forward
nft delete rule inet devvm forward handle <handle-of-the-final-drop>
```

Then `devvm-guard rollback`. Restoring the rule requires the G2-scoped variant above.

## 6. Residual risks (stated, not hidden)

- **Global forward hook.** The final `drop` in `chain forward` denies all forwarded traffic that is
  not the dev allow-list. That is intentional (it recreates "nothing routed") but it is a host-wide
  policy object: if a future feature needs host routing, it must be added to this table explicitly.
- **`nftables.service` start/stop/reload/restart — NOT a residual risk, an open PRE-APPLY
  BLOCKER (5a, UNIMPLEMENTED).** Its `ExecStop`/reload flush the whole ruleset and cannot be
  intercepted with an `ExecStopPre`; `verify` and the periodic watchdog cannot stop the guest
  before the flush, so the guest is fail-open until the next watchdog tick. `onboot=0`, the
  inactive service and the 5 s watchdog shrink but do not close that window, and nobody has
  approved accepting it. Apply stays blocked until a guest-stop/disconnect-before-flush mechanism
  that refuses the flush when it cannot isolate the guest is designed, implemented and tested.
- **Unrestricted Internet** allows the guest to reach external relays, so this design does not claim
  that homelab services are unreachable through third parties.
- **M** is a DNAT exception. It is off by default and only source `192.168.1.65` can use it. It gets
  enabled only after the production probes pass and only if console access is considered
  insufficient.
- Per-interface forwarding on `vmbr0` is a kernel feature verified in the lab on the same kernel,
  but the production effect on the existing guests is re-checked with a health probe and a rollback
  if it fails.

## 7. Not in scope

No Honcho broker, no GitHub adapter, no additional Mattermost bot, no second VM, no changes to
existing profiles/guests/LXC, no PVE datacenter firewall enable, no global ruleset flush, no reboot.