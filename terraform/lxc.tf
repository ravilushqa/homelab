# LXC containers on Proxmox, one entry per container in var.lxc (see homelab.auto.tfvars).
# No entry, no container: an empty map manages nothing.

variable "proxmox_node_name" {
  type        = string
  default     = "pve01"
  description = "Proxmox node the containers run on"
}

variable "lxc" {
  description = "LXC containers keyed by hostname"
  type = map(object({
    vm_id  = number
    cores  = number
    memory = number
    swap   = optional(number, 512)
    disk = object({
      datastore = string
      size      = number # GB
    })
    ipv4     = optional(string, "dhcp") # "dhcp" or CIDR, e.g. "192.168.1.3/24"
    ipv6     = optional(string)
    firewall = optional(bool, false)
    dns = optional(object({
      servers = optional(list(string))
      domain  = optional(string)
    }))
    features = optional(object({
      nesting = optional(bool, false)
      keyctl  = optional(bool, false)
    }), {})
    mount_points = optional(list(object({
      volume    = string # storage volume ("raid1:subvol-117-disk-0") or host path (bind mount, needs root@pam)
      path      = string
      size      = optional(string)
      backup    = optional(bool, false)
      read_only = optional(bool, false)
    })), [])
    os_type       = optional(string, "debian")
    template      = optional(string, "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst")
    tags          = optional(list(string), [])
    start_on_boot = optional(bool, true)
  }))
  default = {}
}

resource "proxmox_virtual_environment_container" "lxc" {
  for_each = var.lxc

  node_name     = var.proxmox_node_name
  vm_id         = each.value.vm_id
  unprivileged  = true
  start_on_boot = each.value.start_on_boot
  tags          = each.value.tags

  # Proxmox doesn't store the default cores = 1, so only set it when it differs
  dynamic "cpu" {
    for_each = each.value.cores == 1 ? [] : [each.value.cores]
    content {
      cores = cpu.value
    }
  }

  console {
    enabled   = true
    type      = "tty"
    tty_count = 2
  }

  memory {
    dedicated = each.value.memory
    swap      = each.value.swap
  }

  disk {
    datastore_id = each.value.disk.datastore
    size         = each.value.disk.size
  }

  features {
    nesting = each.value.features.nesting
    keyctl  = each.value.features.keyctl
  }

  dynamic "mount_point" {
    for_each = each.value.mount_points
    content {
      volume    = mount_point.value.volume
      path      = mount_point.value.path
      size      = mount_point.value.size
      backup    = mount_point.value.backup
      read_only = mount_point.value.read_only
    }
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = each.value.firewall
  }

  initialization {
    hostname = each.key

    ip_config {
      ipv4 {
        address = each.value.ipv4
        gateway = each.value.ipv4 == "dhcp" ? null : var.default_gateway
      }
      dynamic "ipv6" {
        for_each = each.value.ipv6 == null ? [] : [each.value.ipv6]
        content {
          address = ipv6.value
        }
      }
    }

    dynamic "dns" {
      for_each = each.value.dns == null ? [] : [each.value.dns]
      content {
        servers = dns.value.servers
        domain  = dns.value.domain
      }
    }
  }

  operating_system {
    template_file_id = each.value.template
    type             = each.value.os_type
  }

  lifecycle {
    # Creation-only inputs: existing containers were imported, so these must
    # never force a replacement. Descriptions are left to the Proxmox UI.
    ignore_changes = [
      vm_id, # not populated by import; still used on create
      timeout_start,
      operating_system[0].template_file_id,
      initialization[0].user_account,
      network_interface[0].mac_address,
      description,
    ]
  }
}
