variable "cluster_name" {
  type        = string
  description = "Name of the Kubernetes cluster"
  default     = "cluster01"
}

variable "talos_cp_01_ip_addr" {
  type        = string
  description = "IP address for the Talos control plane node"
}

variable "talos_worker_01_ip_addr" {
  type        = string
  description = "IP address for the Talos worker node"
}

variable "talos_cp_hostname" {
  type        = string
  description = "Hostname for the Talos control plane node"
  default     = "talos-cp-01"
}

variable "talos_worker_hostname" {
  type        = string
  description = "Hostname for the Talos worker node"
  default     = "talos-worker-01"
}

variable "proxmox_node_name" {
  type        = string
  description = "Name of the Proxmox node where VMs are created"
  default     = "pve01"
}

variable "cilium" {
  description = "Cilium configuration"
  type = object({
    values  = string
    install = string
  })
}

variable "talos_version" {
  type        = string
  description = "Talos Linux version"
  default     = "v1.11.5"
}

variable "system_extensions" {
  type        = list(string)
  description = "List of Talos system extensions to include in the image"
  default = [
    "siderolabs/i915-ucode",
    "siderolabs/intel-ucode",
    "siderolabs/iscsi-tools",
    "siderolabs/qemu-guest-agent",
    "siderolabs/util-linux-tools",
  ]
}

variable "vm_ids" {
  type        = map(string)
  description = "Map of VM names to IDs (for dependency tracking)"
  default     = {}
}