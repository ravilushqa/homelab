# APPLY-RUNBOOK — one dev VM, separately authorized window

**This runbook is not authorization.** Steps 1–3 are read-only. Everything from step 4 on is a
production change on `pve01` and requires an explicitly agreed window, an open root console and
approval of this draft PR.

All root blocks below are meant for **bash in a root console on `pve01`** (not zsh, not the Hermes
host). Keep the console session open for the whole operation: it is the independent rollback
channel.

## 0. Preconditions to confirm before touching anything

Read-only:

```bash
hostname; date -Is; pveversion
qm list
ip -br a
ip -d link show vmbr0
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables
nft list ruleset | head -40
pvesm status
pvesm status --content snippets
ip route show table all | grep 10.203.255 || echo "no 10.203.255 routes"
ip neigh show | grep -i 10.203 || echo "no 10.203 neighbours"
```

Expected: `ip_forward=0`, `bridge-nf-call-iptables=0` (or the module unloaded), no `vmbr1`,
no route/neighbour for `10.203.255.0/30`, VMID 130 absent, `nvme-thin` free space >= 20 GiB.

If `bridge-nf-call-iptables=1` **stop**: bridged traffic would pass through the new forward hook.
This is a real possibility — the kernel default is `1` whenever `br_netfilter` is loaded, and a fresh
namespace on this host shows `1`. Do not continue; use the G2-scoped variant or get explicit approval
to disable `br_netfilter` (see design §5).
If `ip_forward=1` **stop**: the "restore the original boundary" argument in the design no longer
holds.

Confirm on the router as well (DHCP reservations, static routes, VPN/overlay pools): nothing uses
`10.203.255.0/30`.

## 1. Stage the reviewed bundle on pve01

From the reviewed branch (adjust the checkout path; do not use a moving branch name in production):

```bash
git clone --branch draft/devvm-guard-v3 --depth 1 https://github.com/ravilushqa/homelab.git /root/devvm-guard-src
cd /root/devvm-guard-src/pve01/devvm-guard
bash -n devvm-guard && bash -n pve-hook.sh && bash -n tests/netns-isolation-test.sh
unshare -rn --mount --propagation private bash tests/netns-isolation-test.sh
```

The last command must print `RESULT: 26 passed, 0 failed, 0 skipped` **on this host**. As root it
runs because `unshare -rn` gives it a separate network namespace (the test refuses to run in the
host netns); nothing outside that namespace is modified. A failure
means the kernel behaves differently from the lab assumptions — stop and report, do not apply.

## 2. Install files and pin the configuration

```bash
cd /root/devvm-guard-src/pve01/devvm-guard
./devvm-guard install-units
install -m 0600 devvm-guard.conf.example /etc/devvm-guard.conf
install -d -m 0755 /usr/local/share/doc/devvm-guard
install -m 0644 ../../docs/devvm-guard/DESIGN-DRAFT-v3.md ../../docs/devvm-guard/APPLY-RUNBOOK.md /usr/local/share/doc/devvm-guard/
```

Pin the two values that must not be guessed:

```bash
curl -4 -fsS https://ifconfig.me; echo
```

Take the address it prints (or the one the router reports), then:

```bash
sed -i 's|^WAN_PUBLIC_IP=.*|WAN_PUBLIC_IP=<that IPv4>|' /etc/devvm-guard.conf
grep -E '^(DEV_VMID|DEV_BRIDGE|DEV_GUEST_IP|DEV_HOST_IP|WAN_IFACE|WAN_PUBLIC_IP|CONTROL_ENABLED)=' /etc/devvm-guard.conf
```

Leave `CONTROL_ENABLED=0` for the first pass: console-only management. M is enabled later, in a
separate decision, once the acceptance probes pass.

Verify the MAC `02:DE:AD:00:01:30` is unused before creating the VM:

```bash
ip neigh show | grep -i '02:de:ad' || echo "MAC free on the LAN"
qm config 130 >/dev/null 2>&1 && echo "VMID 130 ALREADY EXISTS - stop" || echo "VMID 130 free"
```

## 3. Dry check (no change yet)

```bash
/usr/local/sbin/devvm-guard check
```

This renders the policy, validates it with `nft -c`, and refuses on any precondition mismatch
(`ip_forward` or any per-interface `forwarding=1`, `br_netfilter` iptables/ip6tables, any route
inside or covering the `/30` other than default, existing bridge with ports, autostart VM).
It also runs the configured `HEALTHCHECK_CMD` once. Record its output as evidence.

## 4. Apply the guard (guest still absent)

```bash
/usr/local/sbin/devvm-guard install
systemctl enable --now devvm-guard.service
systemctl enable --now devvm-guard-watchdog.timer
/usr/local/sbin/devvm-guard status
/usr/local/sbin/devvm-guard verify
```

`install` arms an independent 30-minute rollback deadline **before** the first host change, creates
the runtime-only bridge, sets per-interface forwarding, loads `inet devvm` and verifies it.
Expected: every line `PASS`, tables `devvm` loaded, `vmbr1` with no ports, `ip_forward` still 0.

## 5. Create the VM, stopped, without credentials

```bash
qm create 130 --name dev-vm --memory 2048 --cores 2 --cpu host --ostype l26 \
  --scsihw virtio-scsi-single --serial0 socket --vga serial0 \
  --net0 virtio=02:DE:AD:00:01:30,bridge=vmbr1,firewall=0 \
  --agent enabled=1 --onboot 0 --tags dev-vm
```

Fetch and verify the image (checksum must match the published `SHA512SUMS`):

```bash
cd /var/lib/vz/template/iso
curl -fSLO https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
curl -fSLO https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
grep 'debian-13-genericcloud-amd64.qcow2$' SHA512SUMS | sha512sum -c -
qm importdisk 130 /var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2 nvme-thin
qm set 130 --scsi0 nvme-thin:vm-130-disk-0,discard=on,ssd=1
qm disk resize 130 scsi0 20G   # the cloud image is ~3 GiB; S specifies 20 GiB
qm set 130 --ide2 nvme-thin:cloudinit --boot order=scsi0
qm set 130 --ciuser dev --ipconfig0 ip=10.203.255.2/30,gw=10.203.255.1 --nameserver 1.1.1.1
qm config 130
```

No host mount, no `docker.sock`, no SSH agent, no infrastructure credential, no copied root auth.
If a guest login is needed for the first boot, set a one-time guest password locally with
`qm set 130 --cipassword` (typed on the console, never committed and never sent through chat).

Image: `debian-13-genericcloud-amd64.qcow2` from `https://cloud.debian.org/images/cloud/trixie/latest/`.
Verify before import: `sha512sum` against the published `SHA512SUMS`.

## 6. Start behind the gate

```bash
/usr/local/sbin/devvm-guard start
```

`start` re-verifies the guard, starts the VM and re-verifies again; on any failure it quarantines.
Then open the console: `qm terminal 130` (exit with `Ctrl-O`) or the PVE UI console.

## 7. Acceptance probes (inside the guest, no credentials)

Positive:

```bash
ip -br a
ip -4 route
curl -sS -o /dev/null -w '%{http_code}\n' https://deb.debian.org/
getent hosts deb.debian.org
ping -c 2 1.1.1.1
curl -sS -o /dev/null -w '%{http_code}\n' http://deb.debian.org/
```

Negative — every one of these must fail, from a **root** shell in the guest:

```bash
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
/usr/local/sbin/devvm-guard verify
journalctl -u devvm-guard-watchdog --since -10min --no-pager
```

Every `denied_*` counter that corresponds to a probe must have moved; `accepted_internet` must be
non-zero. A timeout alone is not proof.

## 8. Commit readiness

Only after all probes pass, existing guests and the PVE API are healthy, and management is still
reachable:

```bash
/usr/local/sbin/devvm-guard disarm-deadline
```

That removes the automatic rollback. Until then the host rolls back on its own if the session dies.

After any host reboot the runtime bridge/table/sysctls are gone: `devvm-guard.service` (verify only)
fails and `start` refuses. Recover in a window with `devvm-guard rollback` then `devvm-guard install`.

## 9. Rollback (any time, always in this order)

```bash
/usr/local/sbin/devvm-guard rollback
```

Rollback stops/quarantines the guest first, removes **only** the owned tables `devvm` and
`devvm_quarantine`, restores only the recorded per-interface forwarding delta, deletes `vmbr1` only
if this bundle created it, and leaves the VM disk in place. It never flushes the global ruleset and
never touches another table.

Full removal, only when explicitly asked:

```bash
/usr/local/sbin/devvm-guard destroy --yes
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

## 10. Optional control path M (separate decision)

Only after step 7 passes and only if console access is considered insufficient:

1. confirm the guest-only public key is installed (`qm set 130 --sshkeys ...` requires snippets
   storage; without snippets stay console-only);
2. set `CONTROL_ENABLED=1` in `/etc/devvm-guard.conf`;
3. `devvm-guard check && devvm-guard install` is **not** idempotent — rollback first, then install,
   in a fresh window;
4. re-run step 7 plus: from `192.168.1.65` connect to `192.168.1.100:22220` (works), from another
   LAN host (must fail), from the guest to any LAN address (must fail).

## 11. What still needs a human

Only this: **a root console on pve01 in an agreed window** to execute steps 0–9. Everything else
(draft PR, policy, scripts, lab evidence) exists in this bundle. The Hermes API token cannot run host
commands (`403 user != root@pam`), and no workaround that copies root auth into the guest is
acceptable.