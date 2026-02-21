# CLAUDE.md - Guidelines for HomeLab Repository

## Commands
- **Terraform**: `terraform -chdir=./terraform init/plan/apply`
- **Kubernetes**: `kubectl apply -k ./k8s/[path]`, `kubectl kustomize --enable-helm ./k8s/[path] | kubectl apply -f -`
- **Restart Services**: `make glance-restart`, `make isponsorblocktv-restart`
- **Full Deployment**: `make bootstrap`
- **Update DDNS**: `make cloudflare-ddns-gen`
- **ArgoCD**:
  - Get admin password: `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d`
  - Access UI: https://argocd.ravil.space
  - CLI Login: `argocd login argocd.ravil.space --username admin`

## Code Style Guidelines
- **YAML**: 2-space indentation, resource naming in kebab-case
- **Terraform**: 2-space indentation, resource naming in snake_case
- **Kubernetes Resources**: Follow standard K8s naming conventions (camelCase for spec fields)
- **Directory Structure**:
  - `/k8s/apps/external/` - External-facing applications
  - `/k8s/apps/internal/` - Internal services
  - `/k8s/infra/` - Infrastructure components
- **Resource Organization**: Group resources by functionality, use kustomization.yaml
- **Secrets Management**: Use sealed-secrets for encrypted credentials
- **Sealed Secrets Creation**: Always use Terraform certificate for idempotent sealed secrets:
  ```bash
  kubectl create secret generic <name> \
    --from-file=<data> \
    --dry-run=client -o yaml -n <namespace> | \
  kubeseal \
    --cert ../../../../terraform/modules/sealed-secrets/certs/sealed-secrets.cert \
    --scope strict \
    --namespace <namespace> \
    --name <name> \
    --format yaml > sealed-<name>.yaml
  ```
- **Networking**: Use Gateway API (HTTP/TLS routes) for ingress traffic

## Traefik TCP Routing (terraform/modules/traefik/configs/tcp_routers.yaml)
Traffic enters via a Traefik instance that does TLS passthrough to two backends:
- **Komodo host** (`192.168.1.166:443`) — Docker Compose stacks managed by Komodo
- **K8s cluster** (`192.168.1.222:443`) — Kubernetes-served services

**Routing logic**: `komodo-tcp-router` is the catch-all (`HostSNI("*")`, priority 50).
`k8s-tcp-router` has an explicit list of K8s domains (priority 100).

- **Adding a new Komodo service**: no change needed in `tcp_routers.yaml`
- **Adding a new K8s service**: add its hostname to `k8s-tcp-router` rule

K8s domains (as of last update): `argocd`, `glance`, `dozzle`, `it-tools`, `inbox-zero`,
`changedetection`, `grafana`, `ha`, `openwebui`, `pocketid`, `proxmox` (all `.ravil.space`)

## Best Practices
- Document major components in README.md
- Keep infrastructure as code (Terraform, Kubernetes manifests)
- Use descriptive comments for complex configurations
- Follow GitOps principles (git as source of truth)