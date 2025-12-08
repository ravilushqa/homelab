output "talosconfig" {
  value     = data.talos_client_configuration.talosconfig.talos_config
  sensitive = true
}

output "kubeconfig" {
  value     = trimspace(talos_cluster_kubeconfig.kubeconfig.kubeconfig_raw)
  sensitive = true
}

output "image_factory_urls" {
  value       = data.talos_image_factory_urls.this.urls
  description = "Generated Talos image factory URLs for all platforms"
}

output "disk_image_url" {
  value       = data.talos_image_factory_urls.this.urls.disk_image
  description = "URL for the Talos disk image"
}

output "talos_version" {
  value       = var.talos_version
  description = "Talos Linux version being used"
}
