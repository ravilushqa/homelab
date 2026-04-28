variable "cluster_name" {
  type        = string
  description = "Name of the Kubernetes cluster"
  default     = "cluster01"
}

variable "nodes" {
  description = "Configuration for cluster nodes"
  type = map(object({
    host_node = string
    ip        = string
    type      = string
    cpu       = number
    memory    = number
  }))
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

variable "kubernetes_version" {
  type        = string
  description = "Kubernetes version"
  default     = "1.30.0"
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