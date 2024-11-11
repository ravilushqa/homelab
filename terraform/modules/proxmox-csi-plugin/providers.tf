terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
      version = ">=2.33.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = ">=0.66.3"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "2.16.1"
    }
  }
}
