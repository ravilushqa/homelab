# Isolated dev VM (VMID 130), see docs/devvm-guard/. Off until the reviewed apply window:
# enabling it is a separate PR that sets dev_vm_enabled = true in homelab.auto.tfvars and whose
# plan shows exactly one VM to add. The host guard (pve01/devvm-guard) owns the runtime bridge,
# nft policy and start/stop; this only creates the stopped VM, its empty disk and one NIC.

variable "dev_vm_enabled" {
  type        = bool
  default     = false
  description = "Create the isolated dev VM (stopped, never started by Terraform)"
}

module "dev_vm" {
  count  = var.dev_vm_enabled ? 1 : 0
  source = "./modules/dev-vm"
}
