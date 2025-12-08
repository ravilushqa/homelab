output "talos_cp_01_vm_id" {
  value       = proxmox_virtual_environment_vm.talos_cp["talos-cp-01"].id
  description = "ID of the Talos control plane VM in Proxmox"
}

output "talos_worker_01_vm_id" {
  value       = proxmox_virtual_environment_vm.talos_workers["talos-worker-01"].id
  description = "ID of the Talos worker VM in Proxmox"
}

output "vm_ids" {
  value = merge(
    { for name, vm in proxmox_virtual_environment_vm.talos_cp : name => vm.id },
    { for name, vm in proxmox_virtual_environment_vm.talos_workers : name => vm.id }
  )
  description = "Map of all VM names to their IDs"
}

output "node_name" {
  value       = var.node_name
  description = "Proxmox node name where VMs are created"
}

output "vm_hostnames" {
  value = {
    control_plane = keys({ for k, v in local.vms : k => v if !lookup(v, "depends_on_cp", false) })[0]
    worker        = keys({ for k, v in local.vms : k => v if lookup(v, "depends_on_cp", false) })[0]
  }
  description = "Hostnames for control plane and worker VMs"
}

output "talos_image_urls" {
  value       = data.talos_image_factory_urls.this.urls
  description = "Generated Talos image factory URLs"
}
