output "kubeconfig_content" {
  value     = module.talos.kubeconfig
  sensitive = true
}