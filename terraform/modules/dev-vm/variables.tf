# Defaults are the reviewed values; they must match pve01/devvm-guard/devvm-guard.conf.example
# (DEV_VMID, DEV_NAME, DEV_BRIDGE, DEV_GUEST_MAC). pve01/devvm-guard/tests checks that they do.

variable "node_name" {
  type    = string
  default = "pve01"
}

variable "vm_id" {
  type    = number
  default = 130
}

variable "name" {
  type    = string
  default = "dev-vm"
}

variable "cores" {
  type    = number
  default = 2
}

variable "memory_mib" {
  type    = number
  default = 2048
}

variable "disk_datastore" {
  type    = string
  default = "nvme-thin"
}

variable "disk_size_gib" {
  type    = number
  default = 32
}

variable "bridge" {
  type        = string
  default     = "vmbr1"
  description = "Guard-owned, runtime-only private bridge (no ports). Never the LAN bridge."

  validation {
    condition     = var.bridge != "vmbr0"
    error_message = "The dev VM must never sit on the LAN bridge vmbr0."
  }
}

variable "mac_address" {
  type    = string
  default = "02:DE:AD:00:01:30"

  validation {
    # Locally administered unicast (second hex digit 2/6/A/E), so it cannot collide with a vendor MAC
    condition     = can(regex("^[0-9A-Fa-f][26AEae](:[0-9A-Fa-f]{2}){5}$", var.mac_address))
    error_message = "mac_address must be a locally administered unicast MAC (e.g. 02:DE:AD:00:01:30)."
  }
}

variable "installer_iso_file_id" {
  type        = string
  default     = "local:iso/dev-t_379eee20-debian-13.7.0-amd64-netinst.iso"
  description = "Pre-verified installer ISO already on the node. Attached as a CD-ROM only; never imported."

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+:iso/[^,=[:space:]]+\\.iso$", var.installer_iso_file_id))
    error_message = "installer_iso_file_id must be an existing <storage>:iso/<name>.iso volume."
  }
}

variable "tags" {
  type    = list(string)
  default = ["dev-vm"]
}
