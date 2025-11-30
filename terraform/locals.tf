locals {
  # Parse kubeconfig once and extract values
  kubeconfig_parsed = yamldecode(module.talos.kubeconfig)

  # Kubernetes cluster configuration
  k8s_cluster = {
    host                   = local.kubeconfig_parsed["clusters"][0]["cluster"]["server"]
    cluster_ca_certificate = base64decode(local.kubeconfig_parsed["clusters"][0]["cluster"]["certificate-authority-data"])
  }

  # Kubernetes client configuration
  k8s_client = {
    client_certificate = base64decode(local.kubeconfig_parsed["users"][0]["user"]["client-certificate-data"])
    client_key         = base64decode(local.kubeconfig_parsed["users"][0]["user"]["client-key-data"])
  }
}
