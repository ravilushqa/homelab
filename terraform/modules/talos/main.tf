resource "talos_machine_secrets" "machine_secrets" {}

locals {
  cp_nodes     = { for k, v in var.nodes : k => v if v.type == "controlplane" }
  worker_nodes = { for k, v in var.nodes : k => v if v.type == "worker" }
  first_cp_ip  = values(local.cp_nodes)[0].ip
}

data "talos_client_configuration" "talosconfig" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.machine_secrets.client_configuration
  endpoints            = [for k, v in local.cp_nodes : v.ip]
}

data "talos_machine_configuration" "machineconfig_cp" {
  cluster_name     = var.cluster_name
  cluster_endpoint = "https://${local.first_cp_ip}:6443"
  machine_type     = "controlplane"
  machine_secrets  = talos_machine_secrets.machine_secrets.machine_secrets
}

resource "talos_machine_configuration_apply" "cp_config_apply" {
  for_each = local.cp_nodes

  # Ensure VMs are created before applying configuration
  depends_on = [var.vm_ids]

  client_configuration        = talos_machine_secrets.machine_secrets.client_configuration
  machine_configuration_input = data.talos_machine_configuration.machineconfig_cp.machine_configuration
  node                        = each.value.ip
  config_patches = [
    templatefile("${path.module}/patches/control-plane.yaml.tmpl", {
      hostname       = each.key
      node_name      = each.value.host_node
      cluster_name   = var.cluster_name
      cilium_values  = var.cilium.values
      cilium_install = var.cilium.install
    }),
  ]
}

data "talos_machine_configuration" "machineconfig_worker" {
  cluster_name     = var.cluster_name
  cluster_endpoint = "https://${local.first_cp_ip}:6443"
  machine_type     = "worker"
  machine_secrets  = talos_machine_secrets.machine_secrets.machine_secrets
}

resource "talos_machine_configuration_apply" "worker_config_apply" {
  for_each = local.worker_nodes

  # Ensure VMs are created before applying configuration
  depends_on = [var.vm_ids]

  client_configuration        = talos_machine_secrets.machine_secrets.client_configuration
  machine_configuration_input = data.talos_machine_configuration.machineconfig_worker.machine_configuration
  node                        = each.value.ip
  config_patches = [
    templatefile("${path.module}/patches/worker.yaml.tmpl", {
      hostname     = each.key
      node_name    = each.value.host_node
      cluster_name = var.cluster_name
    }),
  ]
}

resource "talos_machine_bootstrap" "bootstrap" {
  depends_on           = [talos_machine_configuration_apply.cp_config_apply]
  client_configuration = talos_machine_secrets.machine_secrets.client_configuration
  node                 = local.first_cp_ip
}

data "talos_cluster_health" "health" {
  depends_on           = [talos_machine_configuration_apply.cp_config_apply, talos_machine_configuration_apply.worker_config_apply]
  client_configuration = data.talos_client_configuration.talosconfig.client_configuration
  control_plane_nodes  = [for k, v in local.cp_nodes : v.ip]
  worker_nodes         = [for k, v in local.worker_nodes : v.ip]
  endpoints            = data.talos_client_configuration.talosconfig.endpoints
  timeouts = {
    read = "10m"
  }
}

resource "talos_cluster_kubeconfig" "kubeconfig" {
  depends_on           = [talos_machine_bootstrap.bootstrap, data.talos_cluster_health.health]
  client_configuration = talos_machine_secrets.machine_secrets.client_configuration
  node                 = local.first_cp_ip
  timeouts = {
    read = "1m"
  }
}
