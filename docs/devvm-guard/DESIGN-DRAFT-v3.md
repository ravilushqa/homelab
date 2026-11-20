# One dev VM with external isolation — DRAFT v3 (S / G1 / G2 / G3 / M)

Status: **draft bundle, nothing applied.** Design approved by Ravil on 2026-11-20
("делаем минимальную dev-VM и тестовый PR"); that approval covers the design and this draft PR,
not apply, merge, host reboot or any credentialed runtime.

- Owner task: `t_85601c57` (parent `t_b2969405`, root card `t_2d311980`).
- Kernel under test: `7.0.14-25-pve` (Lab runs inside an unprivileged user+network namespace on
  the same PVE host, so the kernel semantics are the production kernel's).
- Bundle: `pve01/devvm-guard/` (policy template, `devvm-guard` control script, systemd units,
  PVE hook, namespace lab test). Runbook: `docs/devvm-guard/APPLY-RUNBOOK.md`.

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
- the only allowed egress is host-routed IPv4 to the public Internet (DNS/TCP 80,443/NTP/ICMP),
  NATed out `vmbr0`;
- management is **Proxmox console** by default; the optional narrow `M` control path can be added.

## 2. Components

| Label | What | Honest scope of the change |
|---|---|---|
| **S** | One VM: VMID 130 `dev-vm`, 2 vCPU / 2048 MiB / 20 GiB `nvme-thin`, one virtio NIC on the private bridge, `onboot=0`, no HA, no hotplug, `firewall=0` | New guest + runtime-only private bridge |
| **G1** | Per-interface IPv4 forwarding only: `net.ipv4.conf.vmbr1.forwarding=1`, `net.ipv4.conf.vmbr0.forwarding=1`; global `net.ipv4.ip_forward` stays **0**; IPv6 disabled on `vmbr1` | Enables forwarding for packets arriving on `vmbr0` too — **this changes the host's routed boundary**, which is why G2 ends with an explicit deny for every other forwarded path |
| **G2** | One owned nft table `inet devvm`: source pinning, deny LAN/PVE/link-local/metadata/CGNAT/special-use/public-hairpin, IPv6 drop, host-INPUT drop from the dev bridge, allow-list (DNS/TCP 80,443/NTP/ICMP), `masquerade` for the dev source out `vmbr0`, final drop for all non-dev forwarded traffic | Host-wide `forward` hook; the final drop restores exactly the pre-change "nothing is routed" boundary. Does not touch other tables, PVE firewall defaults, VLANs, Tailscale or existing NAT |
| **G3** | Fail-closed lifecycle: `devvm-guard verify` gate on every sanctioned start, 5 s watchdog that quarantines the guest if the guard drifts/disappears, independent systemd deadline that rolls back even if the operator's session dies | Adds units/timer; a `nftables.service` stop is an unplanned transition covered only by the watchdog interval (documented residual risk) |
| **M** | Optional, default **off**: host DNAT `192.168.1.65:22220 -> guest:22`, source-limited, plus one matching ingress rule | New narrow DNAT/control exception. Packet-tested in the lab (below); enabling it in production is a separate, explicit decision |

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
  **the PVE hookscript path cannot be assumed**; the bundle therefore installs the hook only if an
  admin confirms `pvesm status --content snippets`, and G3 works without it (`devvm-guard start` +
  watchdog).
- `net.ipv4.ip_forward=0`, `net.ipv6.conf.all.forwarding=0`; `nftables.service` inactive with an
  `ExecStop` that flushes the ruleset; both PVE firewall daemons active with unproven enforcement.
- API token is admin-level at `/`, but `POST /nodes/pve01/execute` = `403 (user != root@pam)`, and
  `root`/`claw` SSH to `pve01` is refused. **Host enforcement and its verification need a root
  console** — this is the one remaining external action.

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
unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh
```

Latest run: **26 passed, 0 failed, 0 skipped.**

| Case | Assertion | Result |
|---|---|---|
| G1 | per-interface forwarding works while global `ip_forward` stays 0 | PASS |
| P1 | dev -> Internet TCP/443 (listener present) | allowed, counter `accepted_internet` +1 |
| P2 | dev -> Internet ICMP echo | allowed |
| N1 | dev -> LAN `192.168.1.3:80` | dropped, `denied_internal` +2 |
| N2 | dev -> `169.254.169.254:80` (metadata) | dropped, `denied_internal` +2 |
| N3 | dev -> host `10.203.255.1:22` | dropped, `denied_host_input` +2 |
| N4 | dev -> Internet port 22 (listener present, silent drop, no RST) | dropped, `denied_egress_other` +2 |
| N5 | dev -> homelab public IP (hairpin) | dropped, `denied_internal` +2 |
| N6 | dev with spoofed source `10.203.255.6` | dropped, `denied_spoof` +2 |
| N7 | LAN -> dev | dropped, `denied_other_forward` +2 |
| N8 | LAN -> Internet through the host | dropped, `denied_other_forward` +2 |
| N9 | dev -> IPv6 | dropped, `denied_ipv6` +2 |
| M-P1 | `192.168.1.65:22220` -> guest:22 via DNAT with `CONTROL_ENABLED=1` | connected, `accepted_control` +1 |
| M-N1 | another LAN host -> same port | blocked |
| M-N2 | dev -> LAN with M enabled | dropped silently (TimeoutError, no RST) |

What this **does not** prove: qemu tap/bridge behaviour on the real `vmbr1`, the live
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
   - **Variant G2-scoped** (pre-planned fallback, *not yet lab-tested*): replace the blanket
     `counter name denied_other_forward drop` with a drop matched on the bridge devices only
     (`iifname { "vmbr0", "vmbr1" }`), which leaves bridged frames untouched because their `iif` is
     the bridge *port*. Requires re-running the lab with a bridge-based LAN segment (the current
     lab uses veths, whose `iif` is the veth, so the fallback cannot be validated with it);
   - or disable `br_netfilter` host-wide — only with explicit approval, since it changes what
     `pve-firewall`/iptables see.
3. `ip_forward=0` and **no per-interface `forwarding=1`** anywhere except the dev bridge. If the host
   already routes, this design's "restore the original boundary" argument does not hold; re-plan.
4. Subnet `/30` confirmed free (host + router), VMID 130 free, `nvme-thin` capacity, MAC free.
5. Snippets storage confirmed (only needed for the optional hook and for `--sshkeys`).
6. Independent review of this bundle; `nft -c` on the rendered file plus the lab suite on the target
   host.

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
- **`nftables.service` stop/reload** flushes the ruleset and cannot be intercepted with an
  `ExecStopPre`. Mitigations: the service is currently inactive, the guest has `onboot=0`, every
  sanctioned start is verified, and the 5 s watchdog quarantines the guest if the table disappears.
  Worst case exposure is the watchdog interval; eliminating it entirely needs a host-wide change to
  the nftables unit, which is out of scope here.
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