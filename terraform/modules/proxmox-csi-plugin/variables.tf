variable "proxmox" {
  type = object({
    cluster_name = string
    endpoint = string
    insecure = bool
  })
}

variable "proxmox_csi_plugin_helm_values" {
  description = "Values for the Proxmox CSI Plugin Helm chart"
  type = string
}