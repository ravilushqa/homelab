resource "proxmox_virtual_environment_role" "csi" {
  role_id = "CSI"
  privileges = [
    "VM.Audit",
    "VM.Config.Disk",
    "Datastore.Allocate",
    "Datastore.AllocateSpace",
    "Datastore.Audit"
  ]
}

resource "proxmox_virtual_environment_user" "kubernetes-csi" {
  user_id = "kubernetes-csi@pve"
  comment = "User for Proxmox CSI Plugin"
  acl {
    path      = "/"
    propagate = true
    role_id   = proxmox_virtual_environment_role.csi.role_id
  }
}

resource "proxmox_virtual_environment_user_token" "kubernetes-csi-token" {
  comment               = "Token for Proxmox CSI Plugin"
  token_name            = "csi"
  user_id               = proxmox_virtual_environment_user.kubernetes-csi.user_id
  privileges_separation = false
}

resource "kubernetes_namespace" "csi-proxmox" {
  metadata {
    name = "csi-proxmox"
    labels = {
      "pod-security.kubernetes.io/enforce" = "privileged"
      "pod-security.kubernetes.io/audit"   = "baseline"
      "pod-security.kubernetes.io/warn"    = "baseline"
    }
  }
}

resource "kubernetes_secret" "proxmox-csi-plugin" {
  metadata {
    name      = "proxmox-csi-plugin"
    namespace = kubernetes_namespace.csi-proxmox.id
  }

  data = {
    "config.yaml" = <<EOF
clusters:
- url: "${var.proxmox.endpoint}/api2/json"
  insecure: ${var.proxmox.insecure}
  token_id: "${proxmox_virtual_environment_user_token.kubernetes-csi-token.id}"
  token_secret: "${element(split("=", proxmox_virtual_environment_user_token.kubernetes-csi-token.value), length(split("=", proxmox_virtual_environment_user_token.kubernetes-csi-token.value)) - 1)}"
  region: ${var.proxmox.cluster_name}
EOF
  }
}

resource "helm_release" "proxmox_csi_plugin" {
  name       = "proxmox-csi-plugin"
  repository = "oci://ghcr.io/sergelogvinov/charts"
  chart      = "proxmox-csi-plugin"
  namespace  = "csi-proxmox"
  version    = "0.2.13"

  values = [var.proxmox_csi_plugin_helm_values]

  create_namespace = true
}
