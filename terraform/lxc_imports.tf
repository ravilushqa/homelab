# One-time adoption of containers created before they were in Terraform.
# Delete this file once the import has been applied.
import {
  for_each = toset(["proxmox-backup-server", "pihole", "prometheus-pve-exporter", "fileserver", "komodo"])
  to       = proxmox_virtual_environment_container.lxc[each.key]
  id       = "${var.proxmox_node_name}/${var.lxc[each.key].vm_id}"
}
