variable "talos_cp_01_ip_addr" {
  type        = string
  description = "IP address for the Talos control plane node"
}

variable "talos_worker_01_ip_addr" {
  type        = string
  description = "IP address for the Talos worker node"
}

variable "talos_cp_01_node_name" {
  type        = string
  default     = "pve01"
  description = "Proxmox node name for control plane"
}

variable "talos_worker_01_node_name" {
  type        = string
  default     = "pve01"
  description = "Proxmox node name for worker node"
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
