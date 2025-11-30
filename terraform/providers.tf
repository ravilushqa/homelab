terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.68.0"
    }
    talos = {
      source  = "siderolabs/talos"
      version = "0.6.1"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "2.16.1"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.38.0"
    }
  }
}
