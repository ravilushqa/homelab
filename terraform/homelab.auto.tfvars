# This homelab's own values (no secrets). Loaded automatically by terraform plan/apply.
# Keys are hostnames; see the lxc variable in lxc.tf for the schema.

# Talos sandbox cluster parked (2026-10-25). To bring it back: set true, apply, then
# `make kubeconfig k8s-apply` (docs/runbooks/bootstrap.md)
k8s_enabled = false

lxc = {
  proxmox-backup-server = {
    vm_id    = 103
    cores    = 2
    memory   = 2048
    disk     = { datastore = "nvme-thin", size = 50 }
    features = { nesting = true, keyctl = true }
    tags     = ["backup", "community-script"]
  }

  pihole = {
    vm_id    = 110
    cores    = 1
    memory   = 512
    disk     = { datastore = "local-lvm", size = 4 }
    ipv4     = "192.168.1.3/24"
    ipv6     = "fd2f:3771:abd2:0001::1/64"
    dns      = { servers = ["1.1.1.1"], domain = "google.com" }
    features = { nesting = true, keyctl = true }
    tags     = ["proxmox-helper-scripts"]
  }

  prometheus-pve-exporter = {
    vm_id    = 112
    cores    = 1
    memory   = 256
    swap     = 256
    disk     = { datastore = "local-lvm", size = 2 }
    features = { nesting = true, keyctl = true }
    tags     = ["community-script", "monitoring"]
  }

  fileserver = {
    vm_id    = 117
    cores    = 2
    memory   = 256
    swap     = 256
    disk     = { datastore = "local-lvm", size = 8 }
    ipv4     = "192.168.1.2/24"
    firewall = true
    features = { nesting = true }
    mount_points = [
      { volume = "raid1:subvol-117-disk-0", path = "/mnt/raid1", size = "1000G" },
    ]
  }

  komodo = {
    vm_id    = 122
    cores    = 4
    memory   = 16384
    swap     = 8192
    disk     = { datastore = "nvme-thin", size = 150 }
    features = { nesting = true, keyctl = true }
    mount_points = [
      { volume = "raid1:subvol-122-disk-1", path = "/mnt/raid1", size = "2000G", backup = true },
      # Bind mount: changing it needs root@pam, so CI (API token) can only keep it as is
      { volume = "/mnt/pve/smb/audiobooks", path = "/mnt/audiobooks", read_only = true },
    ]
    tags = ["community-script", "docker"]
  }
}
