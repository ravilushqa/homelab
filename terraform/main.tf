provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = var.proxmox_username
  password = var.proxmox_password
  insecure = true
}

provider "helm" {
  kubernetes {
    host                   = yamldecode(module.talos.kubeconfig)["clusters"][0]["cluster"]["server"]
    client_certificate     = base64decode(yamldecode(module.talos.kubeconfig)["users"][0]["user"]["client-certificate-data"])
    client_key             = base64decode(yamldecode(module.talos.kubeconfig)["users"][0]["user"]["client-key-data"])
    cluster_ca_certificate = base64decode(yamldecode(module.talos.kubeconfig)["clusters"][0]["cluster"]["certificate-authority-data"])
  }
}

provider "kubernetes" {
  host                   = yamldecode(module.talos.kubeconfig)["clusters"][0]["cluster"]["server"]
  client_certificate     = base64decode(yamldecode(module.talos.kubeconfig)["users"][0]["user"]["client-certificate-data"])
  client_key             = base64decode(yamldecode(module.talos.kubeconfig)["users"][0]["user"]["client-key-data"])
  cluster_ca_certificate = base64decode(yamldecode(module.talos.kubeconfig)["clusters"][0]["cluster"]["certificate-authority-data"])
}

module "proxmox" {
  source                  = "./modules/proxmox"
  default_gateway         = var.default_gateway
  talos_cp_01_ip_addr     = var.talos_cp_01_ip_addr
  talos_worker_01_ip_addr = var.talos_worker_01_ip_addr
}

module "talos" {
  depends_on = [module.proxmox]
  source = "./modules/talos"

  talos_cp_01_ip_addr     = var.talos_cp_01_ip_addr
  talos_worker_01_ip_addr = var.talos_worker_01_ip_addr

  cilium = {
    values = file("${path.module}/../k8s/infra/network/cilium/values.yaml")
    install = file("${path.module}/modules/talos/inline-manifests/cilium-install.yaml")
  }
}

module "monitoring" {
  depends_on = [module.talos]
  source = "./modules/monitoring"

  cluster_name                                   = var.cluster_name
  namespace                                      = var.montoring_namespace
  externalservices_prometheus_host               = var.externalservices_prometheus_host
  externalservices_prometheus_basicauth_username = var.externalservices_prometheus_basicauth_username
  externalservices_prometheus_basicauth_password = var.externalservices_prometheus_basicauth_password
  externalservices_loki_host                     = var.externalservices_loki_host
  externalservices_loki_basicauth_username       = var.externalservices_loki_basicauth_username
  externalservices_loki_basicauth_password       = var.externalservices_loki_basicauth_password
  externalservices_tempo_host                    = var.externalservices_tempo_host
  externalservices_tempo_basicauth_username      = var.externalservices_tempo_basicauth_username
  externalservices_tempo_basicauth_password      = var.externalservices_tempo_basicauth_password
}

module "proxmox_csi_plugin" {
  depends_on = [module.talos]
  source = "./modules/proxmox-csi-plugin"

  providers = {
    proxmox    = proxmox
    kubernetes = kubernetes
  }

  proxmox = {
    endpoint = var.proxmox_endpoint
    insecure = true
    cluster_name = "cluster01"
  }
}