output "talos_cp_01_vm_id" {
  value       = proxmox_virtual_environment_vm.talos_cp_01.id
  description = "ID of the Talos control plane VM in Proxmox"
}

output "talos_worker_01_vm_id" {
  value       = proxmox_virtual_environment_vm.talos_worker_01.id
  description = "ID of the Talos worker VM in Proxmox"
}

