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
  count  = var.k8s_enabled ? 1 : 0
  source = "./modules/talos"

  nodes  = var.nodes
  vm_ids = module.proxmox[0].vm_ids

  cilium = {
    values  = file("${path.module}/../k8s/infra/network/cilium/values.yaml")
    install = file("${path.module}/modules/talos/inline-manifests/cilium-install.yaml")
  }
}

# Talos VMs
module "proxmox" {
  count           = var.k8s_enabled ? 1 : 0
  source          = "./modules/proxmox"
  default_gateway = var.default_gateway
  nodes           = var.nodes
  datastore_id    = var.datastore_id
  talos_image_url = module.talos[0].disk_image_url
  talos_version   = module.talos[0].talos_version
}

module "proxmox_csi_plugin" {
  count      = var.k8s_enabled ? 1 : 0
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
  count      = var.k8s_enabled ? 1 : 0
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
  source = "./modules/traefik"

  providers = {
    proxmox = proxmox
  }
}

# The K8s modules got count = 1 for k8s_enabled; keep their existing state
moved {
  from = module.talos
  to   = module.talos[0]
}

moved {
  from = module.proxmox
  to   = module.proxmox[0]
}

moved {
  from = module.proxmox_csi_plugin
  to   = module.proxmox_csi_plugin[0]
}

moved {
  from = module.sealed-secrets
  to   = module.sealed-secrets[0]
}
