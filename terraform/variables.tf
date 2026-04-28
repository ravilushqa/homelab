variable "cluster_name" {
  type    = string
  default = "cluster01"
}

variable "kubernetes_version" {
  type        = string
  description = "Kubernetes version"
  default     = "1.30.0"
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

# Monitoring
variable "monitoring_enabled" {
  type    = bool
  default = false
}

variable "monitoring_namespace" {
  type    = string
  default = "monitoring"
}

variable "externalservices_prometheus_host" {
  type    = string
  default = ""
}

variable "externalservices_prometheus_basicauth_username" {
  type    = number
  default = 0
}

variable "externalservices_prometheus_basicauth_password" {
  type    = string
  default = ""
}

variable "externalservices_loki_host" {
  type    = string
  default = ""
}

variable "externalservices_loki_basicauth_username" {
  type    = number
  default = 0
}

variable "externalservices_loki_basicauth_password" {
  type    = string
  default = ""
}

variable "externalservices_tempo_host" {
  type    = string
  default = ""
}

variable "externalservices_tempo_basicauth_username" {
  type    = number
  default = 0
}

variable "externalservices_tempo_basicauth_password" {
  type    = string
  default = ""
}
