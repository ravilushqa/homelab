output "kubeconfig_content" {
  value     = one(module.talos[*].kubeconfig)
  sensitive = true
}

output "talosconfig" {
  value     = one(module.talos[*].talosconfig)
  sensitive = true
}
