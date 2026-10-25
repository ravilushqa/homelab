# CLAUDE.md - Guidelines for HomeLab Repository

## Architecture Overview
- **Proxmox** hosts a Talos-managed K8s cluster and a separate Komodo Docker host
- **ArgoCD** (deployed at bootstrap) handles all subsequent K8s app deployments via GitOps
- **Komodo** auto-syncs Docker stacks from `komodo/stacks.toml` in this repo (see `komodo/sync.toml`)
  - Never set `deploy = true` in `stacks.toml`: the sync deploys all such stacks in parallel and saturates the host CPU.
    Deploys are done sequentially by `.github/workflows/komodo-deploy.yaml` (runs the sync, then deploys changed stacks one by one)
- New K8s apps: add to `k8s/apps/internal/` (picked up by the `internal-apps` ApplicationSet)
- **Terraform** (state in Terraform Cloud `ravilushqa/homelab`): the `Terraform` workflow plans on PRs and applies on `main`.
  LXCs are entries in `var.lxc` (`terraform/homelab.auto.tfvars`); `var.k8s_enabled` gates the Talos/K8s modules
- **Docs**: `docs/architecture/00-homelab-architecture.md` is the architecture source of truth; update it (and README.md)
  in the same PR as infra changes. Mermaid labels use `<br/>` for line breaks, never `\n`

## Commands
- **Terraform**: `terraform -chdir=./terraform init/plan/apply`
- **Kubernetes**: `kubectl apply -k ./k8s/[path]`, `kubectl kustomize --enable-helm ./k8s/[path] | kubectl apply -f -`
- **Restart Services**: `make glance-restart`, `make isponsorblocktv-restart`
- **Full Deployment**: `make bootstrap`
- **Update DDNS config** (re-seal `config.json`): `make -C k8s/infra/network/cloudflare-ddns gen`
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

## Ingress (komodo/stacks/traefik/dynamic/external.yml)
The router forwards `443/tcp` to the Komodo Traefik (`192.168.1.166:443`), the only public entry point:
- **K8s hostnames**: the `k8s` TCP router matches them by `HostSNI` and passes TLS through to the
  Cilium gateway (`192.168.1.222:443`), which terminates TLS itself. TCP routers win over HTTP routers.
- **Everything else**: HTTP routes from Docker labels (Komodo stacks) or from `dynamic/external.yml`
  (LXC/VM backends such as Home Assistant, Proxmox, PBS).

K8s domains (as of last update): `argocd`, `glance`, `dozzle.k8s`, `it-tools`, `inbox-zero` (all `.ravil.space`)

## Adding a New K8s Service

1. Create `k8s/apps/internal/{name}/` with a kustomization (the `internal-apps` ApplicationSet picks it up)
2. Add an HTTPRoute on the `external` gateway and add the hostname to the `k8s` TCP router in
   `komodo/stacks/traefik/dynamic/external.yml`

Services running outside Docker/K8s (LXC, VM) are routed by Komodo's Traefik, not by K8s.

## Adding a New Service (Docker/Komodo)

1. Create `komodo/stacks/{name}/compose.yaml` with Traefik labels
   - Hardcode domain as `ravil.space` in labels (not via env var)
   - Pin images to a version tag (or `latest@sha256:...` if the image has none) so Renovate can bump them; never bare `:latest`
2. Add secrets to Komodo Variables/Secrets (never commit secrets to repo)
3. Declare the stack in `komodo/stacks.toml` (never `deploy = true`); merging to `main` deploys it
4. Configure OIDC (Traefik middleware or native) — **mandatory for public services**

