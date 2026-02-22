output "vm_ids" {
  value = merge(
    { for name, vm in proxmox_virtual_environment_vm.talos_cp : name => vm.id },
    { for name, vm in proxmox_virtual_environment_vm.talos_workers : name => vm.id }
  )
  description = "Map of all VM names to their IDs"
}

output "node_names" {
  value       = { for k, v in local.vms : k => v.node_name }
  description = "Map of VM names to their Proxmox node names"
}
