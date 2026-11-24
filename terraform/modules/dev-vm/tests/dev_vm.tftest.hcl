# Plan-only tests: the real bpg/proxmox resource schema with mock_provider, so no Proxmox API call
# and no credentials. They check the planned values only, NOT API behaviour, token permissions or
# VM power state. Run them with terraform/tests/offline-test.sh (scratch copy, no TFC backend),
# which pins this module to the root's bpg/proxmox version; passed with 0.122.0 (root pin on this
# branch) and 0.123.1 (--proxmox-version 0.123.1, the pin on main).

mock_provider "proxmox" {}

run "pinned_identity_matches_guard" {
  command = plan

  assert {
    condition     = proxmox_virtual_environment_vm.this.vm_id == 130 && proxmox_virtual_environment_vm.this.node_name == "pve01"
    error_message = "dev VM must be VMID 130 on pve01 (DEV_VMID in devvm-guard.conf)"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.name == "dev-vm"
    error_message = "name must be dev-vm"
  }
  assert {
    condition     = length(proxmox_virtual_environment_vm.this.network_device) == 1
    error_message = "exactly one NIC"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.network_device[0].bridge == "vmbr1"
    error_message = "the only NIC must be on the guard-owned private bridge vmbr1"
  }
  assert {
    condition     = upper(proxmox_virtual_environment_vm.this.network_device[0].mac_address) == "02:DE:AD:00:01:30"
    error_message = "MAC must be the pinned 02:DE:AD:00:01:30"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.network_device[0].firewall == false && proxmox_virtual_environment_vm.this.network_device[0].model == "virtio"
    error_message = "virtio NIC with PVE firewall=0 (enforcement is the host nft table)"
  }
}

run "sizing_matches_design" {
  command = plan

  assert {
    condition     = proxmox_virtual_environment_vm.this.cpu[0].cores == 2
    error_message = "2 vCPU"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.memory[0].dedicated == 2048
    error_message = "2048 MiB"
  }
  assert {
    condition     = length(proxmox_virtual_environment_vm.this.disk) == 1
    error_message = "exactly one managed disk"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.disk[0].size == 32 && proxmox_virtual_environment_vm.this.disk[0].datastore_id == "nvme-thin" && proxmox_virtual_environment_vm.this.disk[0].interface == "scsi0"
    error_message = "one empty 32 GiB scsi0 disk on nvme-thin"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.disk[0].import_from == null && proxmox_virtual_environment_vm.this.disk[0].file_id == null
    error_message = "the disk is allocated empty: no import, no file_id (both would need SSH or import storage)"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.cdrom[0].file_id == "local:iso/dev-t_379eee20-debian-13.7.0-amd64-netinst.iso"
    error_message = "installer is the pre-verified netinst ISO"
  }
}

run "terraform_never_starts_it" {
  command = plan

  assert {
    condition     = proxmox_virtual_environment_vm.this.started == false && proxmox_virtual_environment_vm.this.on_boot == false
    error_message = "started=false and on_boot=false: only devvm-guard starts the VM"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.reboot == false && proxmox_virtual_environment_vm.this.reboot_after_update == false
    error_message = "Terraform must never power-cycle the guest"
  }
  assert {
    condition     = proxmox_virtual_environment_vm.this.hook_script_file_id == null
    error_message = "the hookscript is a root@pam-only setter and stays in the host bootstrap"
  }
  assert {
    condition     = length(proxmox_virtual_environment_vm.this.initialization) == 0
    error_message = "netinst does not consume cloud-init: no initialization block"
  }
}

run "rejects_lan_bridge" {
  command = plan
  variables {
    bridge = "vmbr0"
  }
  expect_failures = [var.bridge]
}

run "rejects_non_iso_installer" {
  command = plan
  variables {
    installer_iso_file_id = "local:import/debian-13-genericcloud-amd64.qcow2"
  }
  expect_failures = [var.installer_iso_file_id]
}

run "rejects_non_local_mac" {
  command = plan
  variables {
    mac_address = "BC:24:11:00:01:30"
  }
  expect_failures = [var.mac_address]
}
