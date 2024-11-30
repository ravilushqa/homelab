output "kubeconfig_content" {
  value     = module.talos.kubeconfig
  sensitive = true
}

output "talosconfig" {
  value     = module.talos.talosconfig
  sensitive = true
}

output "traefik_container_password" {
  value     = module.traefik.ubuntu_container_password
  sensitive = true
}

output "traefik_container_private_key" {
  value     = module.traefik.ubuntu_container_private_key
  sensitive = true
}

output "traefik_container_public_key" {
  value = module.traefik.ubuntu_container_public_key
}