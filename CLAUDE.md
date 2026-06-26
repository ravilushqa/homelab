# CLAUDE.md - Guidelines for HomeLab Repository

## Architecture Overview
- **Proxmox** hosts a Talos-managed K8s cluster and a separate Komodo Docker host
- **ArgoCD** (deployed at bootstrap) handles all subsequent K8s app deployments via GitOps
- **Komodo** auto-syncs Docker stacks from `komodo/stacks.toml` in this repo (see `komodo/sync.toml`)
- New K8s apps: add to `k8s/apps/external/` or `k8s/apps/internal/` + register in `k8s/infra/argocd/applications`

## Commands
- **Terraform**: `terraform -chdir=./terraform init/plan/apply`
- **Kubernetes**: `kubectl apply -k ./k8s/[path]`, `kubectl kustomize --enable-helm ./k8s/[path] | kubectl apply -f -`
- **Restart Services**: `make glance-restart`, `make isponsorblocktv-restart`
- **Full Deployment**: `make bootstrap`
- **Update DDNS**: `make cloudflare-ddns-gen`
- **Scaffold K8s external service**: `make create-external-service SERVICE=name PORT=port IP=ip`
- **Scaffold K8s TLS service**: `make create-tls-service SERVICE=name PORT=port IP=ip`
- **Fetch kubeconfig from Terraform**: `make kubeconfig`
- **Restart ArgoCD**: `make argocd-restart`
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

K8s domains (as of last update): `argocd`, `glance`, `dozzle.k8s`, `it-tools`, `inbox-zero`,
`changedetection`, `grafana`, `ha`, `openwebui`, `pocketid`, `proxmox` (all `.ravil.space`)

## Adding a New K8s Service

1. Use `make create-external-service SERVICE=name PORT=port IP=ip` (or `create-tls-service` for TLS passthrough)
   — generates `k8s/apps/external/{name}/` with kustomization
2. Add the hostname to `k8s-tcp-router` in `terraform/modules/traefik/configs/tcp_routers.yaml`
3. Apply: `kubectl apply -k ./k8s/apps/external/{name}`
4. Register under `k8s/infra/argocd/applications` for ArgoCD to manage it going forward

## Adding a New Service (Docker/Komodo)

1. Create `komodo/stacks/{name}/compose.yaml` with Traefik labels
   - Hardcode domain as `ravil.space` in labels (not via env var)
2. Add secrets to Komodo Variables/Secrets (never commit secrets to repo)
3. Create Komodo stack pointing to this git repo
4. Configure OIDC (Traefik middleware or native) — **mandatory for public services**

