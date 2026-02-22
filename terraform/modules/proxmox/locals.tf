locals {
  # Common VM configuration
  common_vm_config = {
    description = "Managed by Terraform"
    tags        = ["terraform"]
    on_boot     = true

    network_device = {
      bridge = "vmbr0"
    }

    agent = {
      enabled = true
    }

    operating_system = {
      type = "l26" # Linux Kernel 2.6 - 5.X.
    }

    disk = {
      file_format  = "raw"
      interface    = "virtio0"
      size         = 40
      datastore_id = var.datastore_id
    }
  }

  # VM-specific configurations
  vms = {
    for k, v in var.nodes : k => {
      ip_addr   = v.ip
      node_name = v.host_node
      type      = v.type
      cpu = {
        cores = v.type == "controlplane" ? 2 : 4
        type  = "x86-64-v2-AES"
      }
      memory = {
        dedicated = 8192
      }
    }
  }
}
