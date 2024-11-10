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

variable "cilium" {
  description = "Cilium configuration"
  type = object({
    values  = string
    install = string
  })
}