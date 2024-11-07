variable "cluster_name" {
  type    = string
  default = "homelab"
}

# Proxmox
variable "proxmox_endpoint" {
  type        = string
  description = "Proxmox API endpoint"
}

variable "default_gateway" {
  type        = string
  description = "IP address of your default gateway"
}

variable "talos_cp_01_ip_addr" {
  type        = string
  description = "IP address for control plane"
}

variable "talos_worker_01_ip_addr" {
  type        = string
  description = "IP address for worker node"
}

# Monitoring
variable "montoring_namespace" {
  type    = string
  default = "monitoring"
}

variable "externalservices_prometheus_host" {
  type    = string
  description = "Prometheus host"
}

variable "externalservices_prometheus_basicauth_username" {
  type    = number
}

variable "externalservices_prometheus_basicauth_password" {
  type    = string
}

variable "externalservices_loki_host" {
  type    = string
}

variable "externalservices_loki_basicauth_username" {
  type    = number
}

variable "externalservices_loki_basicauth_password" {
  type    = string
}

variable "externalservices_tempo_host" {
  type    = string
}

variable "externalservices_tempo_basicauth_username" {
  type    = number
}

variable "externalservices_tempo_basicauth_password" {
  type    = string
}
