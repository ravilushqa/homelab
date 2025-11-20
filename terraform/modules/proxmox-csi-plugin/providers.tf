terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
      version = ">=2.33.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.68.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "2.16.1"
    }
  }
}
