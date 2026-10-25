locals {
  # Parse kubeconfig once and extract values
  # null when k8s_enabled = false: the helm/kubernetes providers then get an
  # empty config, which is fine because no resource uses them
  kubeconfig_parsed = var.k8s_enabled ? yamldecode(module.talos[0].kubeconfig) : null

  # Kubernetes cluster configuration
  k8s_cluster = {
    host                   = try(local.kubeconfig_parsed["clusters"][0]["cluster"]["server"], null)
    cluster_ca_certificate = try(base64decode(local.kubeconfig_parsed["clusters"][0]["cluster"]["certificate-authority-data"]), null)
  }

  # Kubernetes client configuration
  k8s_client = {
    client_certificate = try(base64decode(local.kubeconfig_parsed["users"][0]["user"]["client-certificate-data"]), null)
    client_key         = try(base64decode(local.kubeconfig_parsed["users"][0]["user"]["client-key-data"]), null)
  }
}
