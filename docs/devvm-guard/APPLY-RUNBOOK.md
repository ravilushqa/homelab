# APPLY-RUNBOOK — one dev VM, separately authorized window

**This runbook is not authorization.** Merging PR #913 (this draft) authorizes nothing: no
enablement, no Terraform apply, no host change. Steps 0, 1 and 4 are read-only. Every other step
writes to production (host files, systemd units and `daemon-reload`, runtime network state, the
Terraform-managed VM, the VM config) and may run **only inside an explicitly agreed and approved
window**; the host steps also need an open root console. Step 6 (Terraform apply) needs its own
approval on top of the window.

> **PRE-APPLY BLOCKER — UNIMPLEMENTED (`DESIGN-DRAFT-v3.md` 5a). Do not run steps 2–11 yet.**
> The host's `nftables.service` `ExecStop`/reload flush the **whole** ruleset; `verify` and the
> periodic watchdog cannot stop the guest before that flush, so the guest would be fail-open until
> the next watchdog tick. A mechanism that confirms the guest is **stopped or disconnected before**
> any `nftables.service` start/stop/reload/restart or other global flush, and **refuses** the flush
> when it cannot isolate the guest, must be designed, implemented and tested first. That gap is
> **not** an accepted risk. No apply until this gate **and** the real-TAP validation
> (`tests/netns-bridge-ports-test.sh` `exit=0` on pve01; `77` is not a pass) **and** the other
> feasibility gates (design section 5) pass. An open root console is **not** the only remaining
> blocker, and no additional root diagnostics are requested now.

Ownership split (details in `DESIGN-DRAFT-v3.md`, section 2a):

- **Terraform** (`terraform/modules/dev-vm`, gated by `dev_vm_enabled`, off by default) owns the VM
  definition, its one empty 32 GiB disk, its one NIC and the installer CD-ROM. It never starts,
  stops or reboots the guest. It runs from the CI runner / Terraform Cloud with the existing API
  token, never from the root console; there is no manual VM-creation or disk-import path on the host.
- **`devvm-guard` (root on pve01)** owns the runtime-only bridge `vmbr1`, the nft table, the
  per-interface forwarding, start/stop/quarantine/rollback and the pre-start hookscript.
  It never creates, modifies or deletes the VM or its disk.

Order, and why: the guard creates the runtime bridge **before** Terraform creates a VM whose NIC
points at it (this does not rely on PVE accepting a NIC on a bridge that does not exist), and the
fail-closed hookscript is attached **before** anything starts the guest.

| Step | What | Kind |
|------|------|------|
| 0 | Preconditions, offline Terraform checks | read-only |
| 1 | Review the enablement PR — plan only, not merged | read-only |
| 2 | Stage + lab-test the bundle on pve01 | host write (window) |
| 3 | Install files, units (`daemon-reload`), pin config | production change (window) |
| 4 | `devvm-guard check` | read-only |
| 5 | `devvm-guard install`: runtime bridge + nft table, enable units | production change (window) |
| 6 | Terraform apply creates the stopped VM (CI) | production change (window + separate approval) |
| 7 | Attach the pre-start hookscript (root-only setter) | production change (window) |
| 8 | `devvm-guard start`, OS install over HTTPS | production change (window) |
| 9–10 | Acceptance probes, commit readiness | read-only, then disarm |

All root blocks below are meant for **bash in a root console on `pve01`** (not zsh, not the Hermes
host). Keep the console session open for the whole host operation: it is the independent rollback
channel.

## 0. Preconditions (read-only)

On the root console, read-only:

```bash
hostname; date -Is; pveversion
qm list
ip -br a
ip -d link show vmbr0
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables
nft list ruleset | head -40
pvesm status
pvesm status --content snippets
pvesm list local --content iso | grep -F 'dev-t_379eee20-debian-13.7.0-amd64-netinst.iso'
ip route show table all | grep 10.203.255 || echo "no 10.203.255 routes"
ip neigh show | grep -i 10.203 || echo "no 10.203 neighbours"
ip neigh show | grep -i '02:de:ad' || echo "MAC free on the LAN"
qm config 130 >/dev/null 2>&1 && echo "VMID 130 ALREADY EXISTS - stop" || echo "VMID 130 free"
ls -l /dev/net/tun
```

Expected: `ip_forward=0`, `bridge-nf-call-iptables=0` (or the module unloaded), no `vmbr1`,
no route/neighbour for `10.203.255.0/30`, VMID 130 absent, MAC `02:DE:AD:00:01:30` unused,
`nvme-thin` free space >= 32 GiB, the already verified installer ISO
`local:iso/dev-t_379eee20-debian-13.7.0-amd64-netinst.iso` present, and at least one storage that
**already** has the `snippets` content type (step 7 requires it; if there is none, stop here: the
start in step 8 cannot happen, and enabling `snippets` or changing storage permissions for this is
a separate decision). Nothing is downloaded in this runbook.

If `bridge-nf-call-iptables=1` **stop**: bridged traffic would pass through the new forward hook.
This is a real possibility — the kernel default is `1` whenever `br_netfilter` is loaded, and a fresh
namespace on this host shows `1`. If it is `1`, set `FORWARD_DROP_SCOPE=bridge` in
`/etc/devvm-guard.conf` (lab-validated) instead of the blanket drop, per the operator
directive of 2026-11-20; do NOT proceed with `blanket`. `devvm-guard check` enforces this.
If `ip_forward=1` **stop**: the "restore the original boundary" argument in the design no longer
holds.

Confirm on the router as well (DHCP reservations, static routes, VPN/overlay pools): nothing uses
`10.203.255.0/30`.

Terraform token privileges (checked by an admin in the PVE UI, *Permissions*; the token is never
printed or copied): `VM.Allocate` and `VM.Config.*` for VMID 130, `Datastore.AllocateSpace` on
`nvme-thin`, `Datastore.Audit` (or `AllocateSpace`) on `local` for the ISO, and `SDN.Use` on
`/sdn/zones/localnetwork/vmbr1` (a grant on `/` covers it). No `root@pam`, no SSH and no
`Sys.Modify` are needed. A missing privilege is **not** guaranteed to fail cleanly up front: the
provider creates the VM through several API calls, so a 403 or timeout halfway can leave a
partial, stopped VM 130 (with or without its disk) — see step 6, "If the apply fails".

Offline Terraform checks (any workstation, no host, no credentials). The root module has a
Terraform Cloud `cloud {}` block, so do **not** run `terraform init` in `terraform/` itself; the
script works on a scratch copy without that block, state, certs or secrets:

```bash
terraform/tests/offline-test.sh                              # root pin
terraform/tests/offline-test.sh --proxmox-version 0.123.1    # e.g. the pin on main, copy only
```

They are plan-only with mocked providers: wiring and schema, not API behaviour or power state.

## 1. Review the enablement PR — plan only, do not merge

A separate PR sets, in `terraform/homelab.auto.tfvars`:

```hcl
dev_vm_enabled = true
```

Its CI plan (the `Terraform` workflow comment) must show **exactly** `1 to add, 0 to change,
0 to destroy`, the add being `module.dev_vm[0].proxmox_virtual_environment_vm.this` with
`vm_id = 130`, `node_name = "pve01"`, `started = false`, `on_boot = false`, one `disk`
(`scsi0`, `nvme-thin`, `size = 32`, no `import_from`/`file_id`), one `network_device`
(`vmbr1`, `02:DE:AD:00:01:30`, `firewall = false`) and the netinst ISO on `ide2`.
Any other change in that plan: stop. **Do not merge it yet**: merging to `main` is the apply
(CI `apply` job), which happens only in step 6, after the guard owns `vmbr1`.

## 2. Stage the reviewed bundle on pve01 (host write, inside the window)

From the reviewed commit (adjust the path; pin a commit, not a moving branch name):

```bash
git clone https://github.com/ravilushqa/homelab.git /root/devvm-guard-src
git -C /root/devvm-guard-src checkout --detach <reviewed-commit-sha>
cd /root/devvm-guard-src/pve01/devvm-guard
bash -n devvm-guard && bash -n pve-hook.sh && bash -n tests/netns-isolation-test.sh && bash -n tests/netns-bridge-ports-test.sh
bash tests/holders-bookkeeping-test.sh   # harness self-check, needs no tun/root
for scope in blanket bridge; do
  unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh "$scope"
done
unshare -rn --mount --propagation private bash tests/netns-bridge-ports-test.sh; echo "exit=$?"
```

Expected **on this host**: `RESULT[blanket]` and `RESULT[bridge]` `28 passed, 0 failed, 0 skipped`,
and `RESULT[bridge-ports]: 31 passed, 0 failed, 0 skipped` with `exit=0`. pve01 has
`/dev/net/tun`, so the real-TAP cases (expected guest tap accepted; foreign/extra/unowned taps
rejected) must run here; `exit=77` (skipped) is **not** acceptable on pve01. The tests refuse to run
in the host network namespace; nothing outside the throwaway namespace is modified. A failure means
the kernel behaves differently from the lab assumptions — stop and report, do not apply.

## 3. Install files and pin the configuration (production change, inside the window)

`install-units` writes `/usr/local/sbin`, `/usr/local/share/devvm-guard` and
`/etc/systemd/system` and runs `systemctl daemon-reload`; it does not enable anything.

```bash
cd /root/devvm-guard-src/pve01/devvm-guard
./devvm-guard install-units
install -m 0600 devvm-guard.conf.example /etc/devvm-guard.conf
install -d -m 0755 /usr/local/share/doc/devvm-guard
install -m 0644 ../../docs/devvm-guard/DESIGN-DRAFT-v3.md ../../docs/devvm-guard/APPLY-RUNBOOK.md /usr/local/share/doc/devvm-guard/
```

Pin the value that must not be guessed:

```bash
curl -4 -fsS https://ifconfig.me; echo
```

Take the address it prints (or the one the router reports), then:

```bash
sed -i 's|^WAN_PUBLIC_IP=.*|WAN_PUBLIC_IP=<that IPv4>|' /etc/devvm-guard.conf
grep -E '^(DEV_VMID|DEV_BRIDGE|DEV_GUEST_IP|DEV_GUEST_MAC|DEV_HOST_IP|WAN_IFACE|WAN_PUBLIC_IP|CONTROL_ENABLED|FORWARD_DROP_SCOPE)=' /etc/devvm-guard.conf
```

Leave `CONTROL_ENABLED=0` for the first pass: console-only management. M is enabled later, in a
separate decision, once the acceptance probes pass.

## 4. Dry check (read-only)

```bash
/usr/local/sbin/devvm-guard check
```

This renders the policy, validates it with `nft -c`, and refuses on any precondition mismatch
(`ip_forward` or any per-interface `forwarding=1`, `br_netfilter` iptables/ip6tables, any route
inside or covering the `/30` other than default, an existing `vmbr1` with **any** port). VM 130
does not exist yet, so the NIC checks are skipped here and run from step 6 on. It also runs the
configured `HEALTHCHECK_CMD` once. Record its output as evidence.

## 5. Install the guard: runtime bridge and nft table (production change, inside the window)

```bash
/usr/local/sbin/devvm-guard install
systemctl enable --now devvm-guard.service
systemctl enable --now devvm-guard-watchdog.timer
/usr/local/sbin/devvm-guard status
/usr/local/sbin/devvm-guard verify
```

`install` arms an independent 30-minute rollback deadline **before** the first host change, creates
the runtime-only bridge `vmbr1` (no ports, not in `/etc/network/interfaces`), sets per-interface
forwarding, loads `inet devvm` and verifies it. Expected: every line `PASS`, including
`vmbr1 ports: none (no L2 uplink)` and `VM 130 not created yet`; `ip_forward` still 0.

If the window is long, re-arm the deadline (`devvm-guard arm-deadline 1800`) so step 6 has time;
the deadline only ever rolls back the guard and stops the guest, it never touches Terraform state.

## 6. Terraform apply creates the stopped VM (inside the window, separate approval)

With the guard verified, merge the PR from step 1 (only with its approval). The CI `apply` job
creates the VM through the PVE API with the existing token; nothing is run on the root console
for this, and there is no manual VM-creation or disk-import fallback.

Then, read-only on the console:

```bash
qm status 130          # status: stopped
qm config 130          # exactly net0: virtio=02:DE:AD:00:01:30,bridge=vmbr1[,firewall=0]; no onboot: 1; scsi0 32G; ide2 = the ISO
/usr/local/sbin/devvm-guard verify
```

`verify` must be all `PASS`, now including `VM 130 net0 is the reviewed NIC` and
`vmbr1 ports: none`. Do **not** start the VM with the UI or `qm`; from now on it is started only
by `devvm-guard start` (step 8), and only after step 7.

### If the apply fails

A failed apply can be **partial**: the provider may have created VM 130 (stopped) and possibly its
disk before the failing call. Do not start anything, do not re-run the apply, and do not "fix" it
by hand. Inspect both sides read-only:

```bash
# Terraform side (operator workstation or the TFC workspace UI; read-only)
terraform state list
terraform state show 'module.dev_vm[0].proxmox_virtual_environment_vm.this'
```

```bash
# PVE side (root console, read-only)
qm status 130; qm config 130
pvesm list nvme-thin | grep -E 'vm-130-' || echo "no vm-130 volumes"
/usr/local/sbin/devvm-guard verify
```

Record both and decide in review. No automatic delete, recreate, `terraform import`, `-replace`,
`qm destroy` or removal of `prevent_destroy`: each of those is a separate, reviewed change. A
partial VM is stopped and on `onboot: 0`; the guard (and the watchdog, which treats a stopped VM
as safe) stays as is, or `devvm-guard rollback` returns the host to the baseline without touching
the VM or its disk.

## 7. Attach the fail-closed pre-start hookscript (required, root-only; inside the window)

The hookscript is **required** before any guest start, installer boot or credentialed readiness
step: it makes every start path (UI, API, `qm start`, a later `onboot` mistake) run
`devvm-guard verify` first and refuse if the guard is not verified.

It needs a storage that **already** has the `snippets` content type (step 0). Use it as is: do not
add `snippets` to another storage and do not change storage permissions/ACLs for this; if there is
no such storage, stop — step 8 does not happen. Setting a hookscript is accepted by PVE only from a
`root@pam` password session (`only root can set 'hookscript' config`; API tokens, including root's,
are rejected), so it is done here on the root console, not in Terraform. Terraform ignores the
attribute (`lifecycle.ignore_changes`), so later applies do not remove it.

```bash
SNIP=<storage-with-snippets>                       # from 'pvesm status --content snippets'
DIR="$(pvesm path "$SNIP:snippets/devvm-guard-pve-hook.sh" | xargs dirname)"
install -m 0755 /usr/local/sbin/devvm-guard-pve-hook "$DIR/devvm-guard-pve-hook.sh"
qm set 130 --hookscript "$SNIP:snippets/devvm-guard-pve-hook.sh"
qm config 130 | grep '^hookscript'
"$DIR/devvm-guard-pve-hook.sh" 130 pre-start; echo "verified guard -> exit=$? (expect 0)"
DEVVM_CONF=/nonexistent "$DIR/devvm-guard-pve-hook.sh" 130 pre-start; echo "no config -> exit=$? (expect 1)"
```

The last two lines only run the hook's check (no start). Both expectations must hold; otherwise
stop.

## 8. Start behind the gate and install the OS over HTTPS (inside the window)

```bash
/usr/local/sbin/devvm-guard start
```

`start` re-verifies the guard, starts the VM (the hookscript verifies again at `pre-start`) and
re-verifies after start. After start, `vmbr1` carries exactly one port, `tap130i0`, which `verify`
accepts only because it is a real TAP device held open by VM 130's own qemu process and net0 is
the reviewed NIC; any other port (uplink, VLAN, veth, `fwpr…`, another VM's tap, `tap130i1`) fails
`verify`, and the 5 s watchdog then quarantines and stops the guest.

Console: the VM uses the default VGA display on purpose so the installer runs in the PVE UI
console (noVNC); the serial socket is kept for later `qm terminal 130`.

Egress is **HTTPS only** (TCP 443), plus DNS to the pinned resolvers, NTP to the pinned servers
and rate-limited ICMP. Plain HTTP (TCP 80) is dropped and counted (`denied_egress_other`); this is
deliberate and is **not** widened for the installer. The netinst therefore has to use an HTTPS
mirror:

1. At the installer boot menu choose *Advanced options → Expert install* (or press `e`/`Tab` on
   the *Install* entry) and append to the kernel line:
   `mirror/protocol=https mirror/country=manual mirror/https/hostname=deb.debian.org mirror/https/directory=/debian`.
2. Network: manual, `10.203.255.2/30`, gateway `10.203.255.1`, nameserver `1.1.1.1`
   (no DHCP on `vmbr1`).
3. Mirror: protocol `https`, host `deb.debian.org`, directory `/debian/`, no proxy. The installer's
   NTP step (Debian pool) is not on the allow-list and is expected to fail quietly; set the time
   zone and continue.
4. Install `qemu-guest-agent` if wanted. Set the guest passwords on the console only: never
   committed, never sent through chat. Nothing is configured by cloud-init.
5. Before the first reboot, or right after, check that **every** APT source uses `https://`
   (including security):

   ```bash
   grep -rhE '^(URIs:|deb )' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null
   ```

   Any `http://` entry (e.g. `security.debian.org`) is changed to its HTTPS form, e.g.
   `https://deb.debian.org/debian-security`, then `apt update` must succeed over 443. If a needed
   endpoint offers only HTTP, that is a separate policy decision, not a runbook step.

After the installer reboots, the guest boots from `scsi0` (boot order `scsi0, ide2`); the ISO stays
attached read-only until a later Terraform change removes it. Later shell access:
`qm terminal 130` (exit with `Ctrl-O`) once a serial getty is enabled in the guest, or the UI
console.

Status of this draft: the OS installation and the runtime end-to-end checks (steps 8–9 on pve01)
have **not** been performed; only the namespace lab tests and mocked Terraform tests have run.

## 9. Acceptance probes (inside the guest, no credentials)

Positive:

```bash
ip -br a
ip -4 route
curl -sS -o /dev/null -w '%{http_code}\n' https://deb.debian.org/
getent hosts deb.debian.org
ping -c 2 1.1.1.1
apt-get update
```

Negative — every one of these must fail, from a **root** shell in the guest:

```bash
curl -m 3 -sS http://deb.debian.org/ ; echo "exit=$?"        # plain HTTP: must fail (dropped)
timeout 3 bash -c 'cat < /dev/tcp/192.168.1.100/8006' ; echo "exit=$?"
curl -m 3 -sS http://169.254.169.254/ ; echo "exit=$?"
timeout 3 bash -c 'cat < /dev/tcp/192.168.1.1/443' ; echo "exit=$?"
timeout 3 bash -c 'cat < /dev/tcp/10.203.255.1/22' ; echo "exit=$?"
ping -c 1 -W 2 192.168.1.1 ; ping -c 1 -W 2 10.203.255.1
ping -6 -c 1 -W 2 2606:4700:4700::1111
```

Guest-root bypass attempts — add the route/address yourself and confirm it still fails:

```bash
IF="$(ip -o -4 route show default | awk '{print $5; exit}')"   # e.g. ens18; do not assume a name
ip route add 192.168.1.0/24 via 10.203.255.1
ip addr add 192.168.1.99/24 dev "$IF" 2>/dev/null || true
ip route get 192.168.1.100
ping -c 1 -W 2 192.168.1.100
ip link set dev "$IF" address 02:11:22:33:44:55
ping -c 1 -W 2 192.168.1.100
nft list ruleset
```

The last one must be empty or guest-local only: the filter is on the host, so nothing in the guest
changes the outcome.

Host-side evidence (root console):

```bash
nft list counters table inet devvm
nft list table inet devvm
ls /sys/class/net/vmbr1/brif                     # exactly: tap130i0
/usr/local/sbin/devvm-guard verify
journalctl -u devvm-guard-watchdog --since -10min --no-pager
```

Every `denied_*` counter that corresponds to a probe must have moved; `accepted_internet` must be
non-zero; the watchdog journal must show no quarantine of the running guest. A timeout alone is
not proof.

## 10. Commit readiness

Only after all probes pass, existing guests and the PVE API are healthy, and management is still
reachable:

```bash
/usr/local/sbin/devvm-guard disarm-deadline
```

That removes the automatic rollback. Until then the host rolls back on its own if the session dies.

After any host reboot the runtime bridge/table/sysctls are gone: `devvm-guard.service` (verify only)
fails, `start` and the hookscript refuse, and the VM cannot start anyway because `vmbr1` is missing.
Recover in a window with `devvm-guard rollback` then `devvm-guard install`.

### Terraform maintenance while the guard is live

- `started` and `hook_script_file_id` are ignored by Terraform: a running guest does not make a
  later plan stop it, and the root-installed hookscript is not removed.
- `reboot_after_update = false`: a change that needs a power cycle fails instead of stopping or
  restarting the guest.
- Hot-pluggable changes (e.g. `network_device`) *would* apply to a running guest. Any plan that
  touches `module.dev_vm` other than as reviewed must be applied only with the guest stopped
  (`devvm-guard quarantine`), followed by `devvm-guard verify` before the next `devvm-guard start`.
  The watchdog quarantines the guest if net0 or the bridge ports drift while it runs.
- Unrelated `main` applies must show no change for `module.dev_vm`; a diff there is a stop signal.

## 11. Rollback (any time, always in this order)

```bash
/usr/local/sbin/devvm-guard rollback
```

Rollback stops/quarantines the guest first, removes **only** the owned tables `devvm` and
`devvm_quarantine`, restores only the recorded per-interface forwarding delta, deletes `vmbr1` only
if this bundle created it, and leaves the VM definition and disk in place. It does **not** destroy
or change any Terraform resource: VM 130, its disk and the Terraform state stay as they are. With
`vmbr1` gone, the VM cannot be started again until the next `devvm-guard install`.

Full removal, only when explicitly asked, in this order:

1. `devvm-guard rollback` (above) on the root console; the guest is stopped.
2. `qm set 130 --delete hookscript` and remove the snippet file (root-only).
3. Delete the VM and its disk **through Terraform**, in a separate reviewed PR that removes the
   `module "dev_vm"` block from `terraform/dev_vm.tf` (the module has `prevent_destroy`, so just
   setting `dev_vm_enabled = false` fails the plan on purpose). Its plan must show exactly
   `0 to add, 0 to change, 1 to destroy` for `module.dev_vm[0].proxmox_virtual_environment_vm.this`.
   `devvm-guard destroy` refuses for this reason; never delete the VM by hand, which would
   leave Terraform state pointing at a missing VM.
4. Host files:

   ```bash
   systemctl disable --now devvm-guard-watchdog.timer devvm-guard.service
   rm -f /etc/devvm-guard.conf
   ```

Emergency, if the guard is gone and the guest is still running:

```bash
qm stop 130 --timeout 15 --skiplock
nft list ruleset | grep -A5 devvm
```

Emergency, if `bridge-nf-call-iptables` flipped to `1` and other guests lost connectivity — remove
only the final drop rule, never the table and never a flush:

```bash
nft -a list chain inet devvm forward
nft delete rule inet devvm forward handle <handle-of-the-final-drop>
```

Never `nft flush ruleset` and never delete `vmbr0` state.

## 12. Optional control path M (separate decision)

Only after step 9 passes and only if console access is considered insufficient:

1. install the guest-only public key **inside the guest** from the console
   (`~/.ssh/authorized_keys` of the dev user); there is no cloud-init, so no snippets storage and no
   `qm set` is needed for this;
2. set `CONTROL_ENABLED=1` in `/etc/devvm-guard.conf`;
3. `devvm-guard check && devvm-guard install` is **not** idempotent — rollback first, then install,
   in a fresh window;
4. re-run step 9 plus: from `192.168.1.65` connect to `192.168.1.100:22220` (works), from another
   LAN host (must fail), from the guest to any LAN address (must fail).

## 13. What still needs a human

- **The 5a nftables-flush gate** (design `DESIGN-DRAFT-v3.md` 5a): unimplemented, blocks every
  apply. Not something a root console or an approval can waive.

- **Approval** of the window and, separately, of merging the `dev_vm_enabled = true` PR (step 6),
  after reviewing its plan (step 1). Merging PR #913 does not imply either.
- **A root console on pve01 inside that window** for steps 0, 2–5 and 7–11: stage/lab-test the
  bundle (including the real-TAP port test, which only runs where `/dev/net/tun` exists),
  `install-units` (+ `daemon-reload`), pin `/etc/devvm-guard.conf`, `check`/`install`, enable the
  two units, the hookscript setter, `start`, the host-side probes, and rollback/disarm. Host root
  is needed only for the guard's files/units, the private bridge, the nft table, the `sysctl`
  deltas, the root-only hookscript setter and rollback. Creating the VM, its disk and NIC (step 6)
  runs in CI with the API token, and installing the guest OS (over HTTPS) through `VM.Console` is
  not inherently a host-root operation.

The Hermes API token cannot run host commands (`403 user != root@pam`), and no workaround that
copies root auth into the guest, or root credentials into Terraform, is acceptable.
