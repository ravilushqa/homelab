terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">=2.38.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.87.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.1.1"
    }
  }
}
