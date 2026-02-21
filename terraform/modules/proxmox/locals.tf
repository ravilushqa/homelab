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
    talos-cp-01 = {
      ip_addr   = var.talos_cp_01_ip_addr
      node_name = var.talos_cp_01_node_name
      cpu = {
        cores = 2
        type  = "x86-64-v2-AES"
      }
      memory = {
        dedicated = 8192
      }
    }

    talos-worker-01 = {
      ip_addr   = var.talos_worker_01_ip_addr
      node_name = var.talos_worker_01_node_name
      cpu = {
        cores = 4
        type  = "x86-64-v2-AES"
      }
      memory = {
        dedicated = 8192
      }
      depends_on_cp = true
    }
  }
}
