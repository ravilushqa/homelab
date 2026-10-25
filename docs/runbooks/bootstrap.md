# Runbook: Bootstrap the Kubernetes Cluster

Rebuilds the Talos cluster on Proxmox and hands it to ArgoCD. The Komodo host, LXCs and Home Assistant are not
touched by the K8s part; the LXCs in `var.lxc` are managed by the same Terraform root.

## Prerequisites

- Tools: `terraform`, `kubectl`, `talosctl`, `kubeseal`, `make`
- Terraform Cloud access: state lives in organization `ravilushqa`, workspace `homelab` (`terraform login`)
- Proxmox API token (`TF_VAR_proxmox_api_token="<id>=<secret>"`) and `TF_VAR_proxmox_endpoint="https://192.168.1.100:8006"`;
  the provider also uses SSH via your agent (`root@pve01`)
- Sealed-secrets key pair in `terraform/modules/sealed-secrets/certs/sealed-secrets.{cert,key}` (gitignored; also stored as
  GitHub secrets `SEALED_SECRETS_CERT`/`SEALED_SECRETS_KEY`). The **same** key must be reused, or every committed
  `sealed-*.yaml` becomes undecryptable.
- `var.k8s_enabled = true` (default)

Node layout comes from `var.nodes` in `terraform/variables.tf` (`talos-cp-01` 192.168.1.210, `talos-worker-01` 192.168.1.211).

## Steps

```bash
make bootstrap
```

which runs, in order:

1. `terraform-init`, `terraform-plan`, `terraform-apply` (`-auto-approve`): Talos image, VMs 101/102, Talos config and
   bootstrap with Cilium as an inline manifest, Proxmox CSI and sealed-secrets via Helm, the LXCs
2. `kubeconfig`: asks before overwriting `~/.kube/config` with `terraform output -raw kubeconfig_content`
3. `k8s-apply`: marks `proxmox-csi` as the default StorageClass, applies `k8s/infra/crds`, Cilium, cert-manager and ArgoCD,
   then `k8s/infra/argocd/applications`

From there ArgoCD syncs `k8s/infra/*` (`infrastructure.yaml`) and every directory under `k8s/apps/internal/`
(`internal-apps` ApplicationSet).

Talos access: `terraform -chdir=terraform output -raw talosconfig > ~/.talos/config`.

## Verify

```bash
kubectl get nodes
kubectl -n argocd get applications          # all Synced/Healthy
kubectl get gateway,httproute -A            # external gateway on 192.168.1.222
kubectl get certificates -A                 # Ready=True
make argocd-password                        # https://argocd.ravil.space
```

Then open one K8s hostname from outside the LAN; it goes through the `k8s` TCP router in
`komodo/stacks/traefik/dynamic/external.yml`.

## Turning the cluster off

Set `k8s_enabled = false` only after a targeted destroy of the K8s modules (`module.talos`, `module.proxmox`,
`module.proxmox_csi_plugin`, `module.sealed-secrets`); otherwise the helm/kubernetes providers lose their cluster
mid-plan.
