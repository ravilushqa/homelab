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