# The isolated dev VM (docs/devvm-guard/DESIGN-DRAFT-v3.md, component S).
#
# Ownership split:
#   * Terraform (this module, API token): VM definition, one empty managed disk, one NIC,
#     installer CD-ROM. All plain PVE API calls (VM.Allocate/VM.Config.*, Datastore.AllocateSpace,
#     SDN.Use on the bridge, read access to the ISO storage); no SSH, no provisioner, no root.
#   * devvm-guard (root on pve01): the runtime-only bridge, nft table, per-interface forwarding,
#     start/stop/quarantine, and the required pre-start hookscript (root@pam only). Terraform never starts, stops or reboots
#     the guest, and never removes the root-installed hookscript.
#
# OS installation is a separate step on the PVE console from the netinst ISO; nothing here
# configures the guest OS.

resource "proxmox_virtual_environment_vm" "this" {
  node_name   = var.node_name
  vm_id       = var.vm_id
  name        = var.name
  description = "Isolated dev VM. Start/stop only via devvm-guard on pve01 (docs/devvm-guard)."
  tags        = sort(var.tags)

  # Only devvm-guard starts the guest, after verifying the host guard
  started             = false
  on_boot             = false
  reboot              = false
  reboot_after_update = false # an update that needs a power cycle fails instead of stopping it
  stop_on_destroy     = true  # an authorized destroy does not hang on a guest without ACPI
  migrate             = false

  operating_system {
    type = "l26"
  }

  cpu {
    cores = var.cores
    type  = "host"
  }

  memory {
    dedicated = var.memory_mib
  }

  # The guest agent is optional inside the guest; never block refresh/apply waiting for it
  agent {
    enabled = true
    wait_for_ip {
      disabled = true
    }
  }

  scsi_hardware = "virtio-scsi-single"

  # Empty managed disk, allocated by the API on the target storage (no import, no SSH)
  disk {
    datastore_id = var.disk_datastore
    interface    = "scsi0"
    size         = var.disk_size_gib
    file_format  = "raw"
    discard      = "on"
    ssd          = true
  }

  cdrom {
    file_id   = var.installer_iso_file_id
    interface = "ide2"
  }

  # The empty disk is not bootable, so the first boot falls through to the installer;
  # after installation the guest boots from scsi0 without another Terraform change
  boot_order = ["scsi0", "ide2"]

  # Exactly one NIC, on the guard-owned private bridge
  network_device {
    bridge      = var.bridge
    mac_address = var.mac_address
    model       = "virtio"
    firewall    = false
  }

  serial_device {
    device = "socket"
  }

  lifecycle {
    # A plan that would destroy or replace the VM (and its disk) fails instead. Removing it is a
    # separate, reviewed change: see docs/devvm-guard/APPLY-RUNBOOK.md, "Full removal".
    prevent_destroy = true

    ignore_changes = [
      # Runtime power state belongs to devvm-guard: a running guest must not make a later plan
      # stop it (started = false only applies at creation).
      started,
      # Set by root@pam on the host only (API tokens are rejected); never remove it from here.
      hook_script_file_id,
    ]
  }
}
