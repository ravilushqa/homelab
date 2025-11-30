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
