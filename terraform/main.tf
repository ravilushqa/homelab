provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token != "" ? var.proxmox_api_token : null
  username  = var.proxmox_api_token == "" ? var.proxmox_username : null
  password  = var.proxmox_api_token == "" ? var.proxmox_password : null
  insecure  = true

  ssh {
    agent = true
  }
}

provider "helm" {
  kubernetes = {
    host                   = local.k8s_cluster.host
    client_certificate     = local.k8s_client.client_certificate
    client_key             = local.k8s_client.client_key
    cluster_ca_certificate = local.k8s_cluster.cluster_ca_certificate
  }
}

provider "kubernetes" {
  host                   = local.k8s_cluster.host
  client_certificate     = local.k8s_client.client_certificate
  client_key             = local.k8s_client.client_key
  cluster_ca_certificate = local.k8s_cluster.cluster_ca_certificate
}

module "talos" {
  source = "./modules/talos"

  talos_cp_01_ip_addr       = var.talos_cp_01_ip_addr
  talos_worker_01_ip_addr   = var.talos_worker_01_ip_addr
  talos_cp_01_node_name     = var.talos_cp_01_node_name
  talos_worker_01_node_name = var.talos_worker_01_node_name
  talos_cp_hostname         = "talos-cp-01"
  talos_worker_hostname     = "talos-worker-01"
  vm_ids                    = module.proxmox.vm_ids

  cilium = {
    values  = file("${path.module}/../k8s/infra/network/cilium/values.yaml")
    install = file("${path.module}/modules/talos/inline-manifests/cilium-install.yaml")
  }
}

module "proxmox" {
  source                    = "./modules/proxmox"
  default_gateway           = var.default_gateway
  talos_cp_01_ip_addr       = var.talos_cp_01_ip_addr
  talos_worker_01_ip_addr   = var.talos_worker_01_ip_addr
  talos_cp_01_node_name     = var.talos_cp_01_node_name
  talos_worker_01_node_name = var.talos_worker_01_node_name
  datastore_id              = var.datastore_id
  talos_image_url           = module.talos.disk_image_url
  talos_version             = module.talos.talos_version
}

module "monitoring" {
  depends_on = [module.talos]
  source     = "./modules/monitoring"

  enabled                                        = var.monitoring_enabled
  cluster_name                                   = var.cluster_name
  namespace                                      = var.monitoring_namespace
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
  source     = "./modules/proxmox-csi-plugin"

  providers = {
    proxmox    = proxmox
    kubernetes = kubernetes
    helm       = helm
  }

  proxmox = {
    endpoint     = var.proxmox_endpoint
    insecure     = true
    cluster_name = var.cluster_name
  }

  proxmox_csi_plugin_helm_values = file("${path.module}/../k8s/infra/storage/proxmox-csi/values.yaml")
}

module "sealed-secrets" {
  depends_on = [module.talos]
  source     = "./modules/sealed-secrets"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }

  // openssl req -x509 -days 365 -nodes -newkey rsa:4096 -keyout sealed-secrets.key -out sealed-secrets.cert -subj "/CN=sealed-secret/O=sealed-secret"
  cert = {
    cert = file("${path.module}/modules/sealed-secrets/certs/sealed-secrets.cert")
    key  = file("${path.module}/modules/sealed-secrets/certs/sealed-secrets.key")
  }

  helm_values = file("${path.module}/../k8s/infra/security/sealed-secrets/values.yaml")
}

module "traefik" {
  depends_on = [module.talos]
  source     = "./modules/traefik"

  providers = {
    proxmox = proxmox
  }
}
