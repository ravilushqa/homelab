variable "cluster_name" {
  type    = string
  default = "cluster01"
}

# Proxmox
variable "proxmox_endpoint" {
  type        = string
  description = "Proxmox API endpoint"
}

variable "proxmox_username" {
  type        = string
  description = "Proxmox username (used for local runs)"
  default     = ""
}

variable "proxmox_password" {
  type        = string
  description = "Proxmox password (used for local runs)"
  default     = ""
  sensitive   = true
}

variable "proxmox_api_token" {
  type        = string
  description = "Proxmox API token in format user@realm!tokenid=secret (used for CI)"
  default     = ""
  sensitive   = true
}

variable "default_gateway" {
  type        = string
  default     = "192.168.1.1"
  description = "IP address of your default gateway"
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
  default = {
    "talos-cp-01" = {
      host_node = "pve01"
      ip        = "192.168.1.210"
      type      = "controlplane"
      cpu       = 2
      memory    = 8192
    }
    "talos-worker-01" = {
      host_node = "pve01"
      ip        = "192.168.1.211"
      type      = "worker"
      cpu       = 4
      memory    = 8192
    }
  }
}

variable "datastore_id" {
  type        = string
  default     = "local-lvm"
  description = "Datastore ID for Proxmox storage"
}

variable "k8s_enabled" {
  type        = bool
  default     = true
  description = "Create the Talos Kubernetes cluster (VMs, Talos, sealed-secrets)"
  # To turn off an existing cluster, first destroy the in-cluster resources while it's
  # still on (terraform destroy -target=module.sealed-secrets),
  # then set false: with the cluster gone the kubernetes provider can't refresh them.
}
