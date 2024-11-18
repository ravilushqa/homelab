resource "kubernetes_namespace" "sealed-secrets" {
    metadata {
        name = "sealed-secrets"
    }
}

resource "kubernetes_secret" "sealed-secrets-key" {
    depends_on = [ kubernetes_namespace.sealed-secrets ]
    type = "kubernetes.io/tls"

    metadata {
        name = "sealed-secrets-bootstrap-key"
        namespace = "sealed-secrets"
        labels = {
            "sealedsecrets.bitnami.com/sealed-secrets-key" = "active"
        }
    }

    data = {
        "tls.crt" = var.cert.cert
        "tls.key" = var.cert.key
    }
}

resource "helm_release" "sealed_secrets" {
    name       = "sealed-secrets-controller"
    repository = "oci://registry-1.docker.io/bitnamicharts"
    chart      = "sealed-secrets"
    namespace  = "sealed-secrets"
    version    = "2.4.9"

    values = [var.helm_values]

    create_namespace = true
}