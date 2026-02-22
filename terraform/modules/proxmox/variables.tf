variable "nodes" {
  description = "Configuration for cluster nodes"
  type = map(object({
    host_node = string
    ip        = string
    type      = string
  }))
}

variable "default_gateway" {
  type        = string
  description = "Default gateway for network configuration"
}

variable "datastore_id" {
  type        = string
  default     = "local-lvm"
  description = "Datastore ID for Proxmox storage"
}

variable "talos_image_url" {
  type        = string
  description = "URL to download the Talos disk image"
}

variable "talos_version" {
  type        = string
  description = "Talos version for image naming"
}
