output "talosconfig" {
  value     = data.talos_client_configuration.talosconfig.talos_config
  sensitive = true
}

output "kubeconfig" {
  value     = trimspace(talos_cluster_kubeconfig.kubeconfig.kubeconfig_raw)
  sensitive = true
}
