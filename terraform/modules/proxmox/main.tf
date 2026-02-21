# Control plane node
resource "proxmox_virtual_environment_vm" "talos_cp" {
  for_each = { for k, v in local.vms : k => v if !lookup(v, "depends_on_cp", false) }

  name        = each.key
  description = local.common_vm_config.description
  tags        = local.common_vm_config.tags
  node_name   = each.value.node_name
  on_boot     = local.common_vm_config.on_boot

  cpu {
    cores = each.value.cpu.cores
    type  = each.value.cpu.type
  }

  memory {
    dedicated = each.value.memory.dedicated
  }

  agent {
    enabled = local.common_vm_config.agent.enabled
  }

  network_device {
    bridge = local.common_vm_config.network_device.bridge
  }

  disk {
    datastore_id = local.common_vm_config.disk.datastore_id
    file_id      = proxmox_virtual_environment_download_file.talos_nocloud_image[each.value.node_name].id
    file_format  = local.common_vm_config.disk.file_format
    interface    = local.common_vm_config.disk.interface
    size         = local.common_vm_config.disk.size
  }

  operating_system {
    type = local.common_vm_config.operating_system.type
  }

  initialization {
    datastore_id = local.common_vm_config.disk.datastore_id
    ip_config {
      ipv4 {
        address = "${each.value.ip_addr}/24"
        gateway = var.default_gateway
      }
      ipv6 {
        address = "dhcp"
      }
    }
  }
}

# Worker nodes (depend on control plane)
resource "proxmox_virtual_environment_vm" "talos_workers" {
  for_each = { for k, v in local.vms : k => v if lookup(v, "depends_on_cp", false) }

  depends_on = [proxmox_virtual_environment_vm.talos_cp]

  name        = each.key
  description = local.common_vm_config.description
  tags        = local.common_vm_config.tags
  node_name   = each.value.node_name
  on_boot     = local.common_vm_config.on_boot

  cpu {
    cores = each.value.cpu.cores
    type  = each.value.cpu.type
  }

  memory {
    dedicated = each.value.memory.dedicated
  }

  agent {
    enabled = local.common_vm_config.agent.enabled
  }

  network_device {
    bridge = local.common_vm_config.network_device.bridge
  }

  disk {
    datastore_id = local.common_vm_config.disk.datastore_id
    file_id      = proxmox_virtual_environment_download_file.talos_nocloud_image[each.value.node_name].id
    file_format  = local.common_vm_config.disk.file_format
    interface    = local.common_vm_config.disk.interface
    size         = local.common_vm_config.disk.size
  }

  operating_system {
    type = local.common_vm_config.operating_system.type
  }

  initialization {
    datastore_id = local.common_vm_config.disk.datastore_id
    ip_config {
      ipv4 {
        address = "${each.value.ip_addr}/24"
        gateway = var.default_gateway
      }
      ipv6 {
        address = "dhcp"
      }
    }
  }
}
