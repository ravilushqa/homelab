# Runbook: Bootstrap HomeLab Infrastructure

## Overview

This runbook describes the complete process to bootstrap the HomeLab infrastructure from scratch, including Proxmox VM provisioning, Talos cluster creation, and initial application deployment.

## Prerequisites

### Required Tools

- Terraform >= 1.0
- kubectl >= 1.28
- talosctl >= 1.7
- kubeseal >= 0.24
- git
- make

### Required Access

- Proxmox VE admin credentials
- Access to Proxmox host (SSH or web UI)
- Cloudflare API token (for DDNS)
- Grafana Cloud credentials (for monitoring)

### Environment Setup

```bash
# Clone repository
git clone https://github.com/your-username/homelab.git
cd homelab

# Set environment variables (or use terraform.tfvars)
export TF_VAR_proxmox_endpoint="https://proxmox.local:8006"
export TF_VAR_proxmox_username="root@pam"
export TF_VAR_proxmox_password="your-password"
```

## Bootstrap Process

### Step 1: Infrastructure Provisioning

**Command**:
```bash
make bootstrap
```

This single command orchestrates the entire bootstrap process:

1. Terraform initialization
2. Infrastructure planning
3. Infrastructure provisioning
4. Kubeconfig extraction
5. Kubernetes bootstrap

### Detailed Steps

#### 1.1: Terraform Init

```bash
make terraform-init
# Or manually:
terraform -chdir=./terraform init
```

**What it does**:
- Downloads provider plugins (Proxmox, Kubernetes, Helm, Talos)
- Initializes backend
- Downloads Terraform modules

**Expected output**:
```
Terraform has been successfully initialized!
```

#### 1.2: Terraform Plan

```bash
make terraform-plan
# Or manually:
terraform -chdir=./terraform plan
```

**What it does**:
- Validates Terraform configuration
- Shows planned changes
- Checks connectivity to Proxmox

**Expected output**:
```
Plan: X to add, 0 to change, 0 to destroy.
```

#### 1.3: Terraform Apply

```bash
make terraform-apply
# Or manually:
terraform -chdir=./terraform apply -auto-approve
```

**What it does**:
- Creates Talos VMs on Proxmox (control plane + worker)
- Bootstraps Talos Kubernetes cluster
- Deploys Cilium CNI via inline manifest
- Deploys Grafana Agent for monitoring
- Deploys Proxmox CSI plugin for storage
- Deploys Sealed Secrets controller
- Creates Traefik LXC container

**Duration**: ~10-15 minutes

**Expected output**:
```
Apply complete! Resources: X added, 0 changed, 0 destroyed.

Outputs:
kubeconfig_content = <sensitive>
```

**What gets created**:

| Resource | Type | Details |
|----------|------|---------|
| talos-cp-01 | VM | Control plane node (4GB RAM, 2 vCPU) |
| talos-worker-01 | VM | Worker node (8-16GB RAM, 4+ vCPU) |
| traefik | LXC | Edge reverse proxy |
| Kubernetes cluster | Cluster | v1.30.x |
| Cilium | Helm Release | CNI + Network Policies |
| Proxmox CSI | Helm Release | Storage provisioner |
| Sealed Secrets | Helm Release | Secret encryption |
| Grafana Agent | Helm Release | Observability |

#### 1.4: Extract Kubeconfig

```bash
make kubeconfig
# Or manually:
terraform -chdir=./terraform output -raw kubeconfig_content > ~/.kube/config
```

**What it does**:
- Extracts kubeconfig from Terraform state
- Saves to `~/.kube/config` (with confirmation)

**Verification**:
```bash
kubectl cluster-info
kubectl get nodes
```

**Expected output**:
```
NAME              STATUS   ROLES           AGE   VERSION
talos-cp-01       Ready    control-plane   5m    v1.30.x
talos-worker-01   Ready    <none>          5m    v1.30.x
```

### Step 2: Kubernetes Bootstrap

The `make k8s-apply` target (part of `make bootstrap`) deploys critical Kubernetes components:

#### 2.1: Patch Default Storage Class

```bash
kubectl patch storageclass proxmox-csi -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

#### 2.2: Apply Gateway API CRDs

```bash
kubectl apply -k ./k8s/infra/crds
```

**What it does**:
- Installs Gateway API custom resource definitions
- Required before deploying Gateway controllers

#### 2.3: Deploy Cilium

```bash
kubectl kustomize --enable-helm ./k8s/infra/network/cilium | kubectl apply -f -
```

**Note**: Cilium is typically already running (deployed via Talos inline manifest). This step ensures the full Helm chart is applied with all configurations.

#### 2.4: Deploy Cert-Manager

```bash
kubectl kustomize --enable-helm ./k8s/infra/security/cert-manager | kubectl apply -f -
```

**What it does**:
- Deploys Cert-Manager for TLS certificate management
- Configures Let's Encrypt issuer
- Enables automatic certificate renewal

**Verification**:
```bash
kubectl get pods -n cert-manager
kubectl get clusterissuers
```

#### 2.5: Deploy ArgoCD

```bash
kubectl kustomize --enable-helm ./k8s/infra/argocd | kubectl apply -f -
```

**What it does**:
- Deploys ArgoCD for GitOps
- Creates ArgoCD Applications for all services
- Configures auto-sync and self-healing

**Verification**:
```bash
kubectl get pods -n argocd
kubectl get applications -n argocd
```

#### 2.6: ArgoCD Syncs Applications

ArgoCD automatically:
- Detects applications defined in Git
- Syncs all applications to cluster
- Maintains desired state

**Monitor sync progress**:
```bash
kubectl get applications -n argocd -w
```

### Step 3: Post-Bootstrap Verification

#### 3.1: Check Node Status

```bash
kubectl get nodes
```

Expected: All nodes `Ready`

#### 3.2: Check Core Components

```bash
kubectl get pods -n kube-system
kubectl get pods -n cilium
kubectl get pods -n cert-manager
kubectl get pods -n argocd
```

Expected: All pods `Running`

#### 3.3: Check Storage

```bash
kubectl get storageclass
kubectl get csidrivers
```

Expected:
- `proxmox-csi` storage class marked as default
- `csi.proxmox.sinextra.dev` CSI driver present

#### 3.4: Check Gateway API

```bash
kubectl get gateways -A
kubectl get httproutes -A
kubectl get tlsroutes -A
```

#### 3.5: Check ArgoCD Applications

```bash
kubectl get applications -n argocd
```

Expected: All applications `Synced` and `Healthy`

#### 3.6: Access ArgoCD UI

Get admin password:
```bash
make argocd-password
# Or:
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

Access UI: https://argocd.ravil.space
- Username: `admin`
- Password: (from above command)

### Step 4: Deploy Applications

Applications are automatically deployed by ArgoCD. Manual deployment (if needed):

```bash
# Deploy a specific application
kubectl kustomize ./k8s/apps/external/immich | kubectl apply -f -

# Or use ArgoCD CLI
argocd app sync immich
```

## Bootstrap Timings

Typical bootstrap timings on moderate hardware:

| Phase | Duration |
|-------|----------|
| Terraform init | 1-2 min |
| Terraform apply (VM creation) | 5-7 min |
| Talos cluster bootstrap | 2-3 min |
| Cilium deployment | 1-2 min |
| Cert-Manager deployment | 1-2 min |
| ArgoCD deployment | 1-2 min |
| Application sync (ArgoCD) | 3-5 min |
| **Total** | **15-25 min** |

## Troubleshooting

### Terraform Errors

**Issue**: Proxmox connection timeout
```
Error: error creating Proxmox client: dial tcp: i/o timeout
```

**Solution**:
- Check Proxmox endpoint URL
- Verify network connectivity
- Check Proxmox firewall rules

**Issue**: Insufficient resources
```
Error: VM creation failed: not enough space
```

**Solution**:
- Check Proxmox storage capacity
- Reduce VM sizes in `terraform/variables.tf`

### Talos Bootstrap Errors

**Issue**: Cluster bootstrap timeout
```
Error: timeout waiting for cluster to be ready
```

**Solution**:
```bash
# Check Talos VM status in Proxmox
# SSH to Proxmox and check VM console

# Check Talos logs
talosctl logs -n <control-plane-ip> controller-runtime
talosctl logs -n <control-plane-ip> kubelet
```

### Kubernetes Errors

**Issue**: Nodes not ready
```bash
kubectl get nodes
# Shows NotReady
```

**Solution**:
```bash
# Check kubelet logs
talosctl logs -n <node-ip> kubelet

# Check CNI (Cilium)
kubectl get pods -n kube-system
kubectl logs -n kube-system -l k8s-app=cilium
```

**Issue**: Pods stuck in Pending
```bash
kubectl get pods -A | grep Pending
```

**Solution**:
```bash
# Describe pod to see reason
kubectl describe pod <pod-name> -n <namespace>

# Common causes:
# - Insufficient resources
# - Storage not available
# - Image pull errors
```

### ArgoCD Sync Errors

**Issue**: Application not syncing

**Solution**:
```bash
# Check ArgoCD application status
kubectl describe application <app-name> -n argocd

# Force sync
argocd app sync <app-name>

# Check ArgoCD logs
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-application-controller
```

## Rollback Procedures

### Rollback Terraform Changes

```bash
# Destroy specific resource
terraform -chdir=./terraform destroy -target=module.monitoring

# Rollback to previous state
terraform -chdir=./terraform apply -auto-approve
```

### Rollback Kubernetes Changes

```bash
# ArgoCD handles rollback automatically via Git
# Just revert the Git commit:
git revert <commit-hash>
git push

# ArgoCD will sync to reverted state
```

### Complete Cluster Rebuild

If the cluster is in an unrecoverable state:

```bash
# Destroy everything
terraform -chdir=./terraform destroy -auto-approve

# Wait for VMs to be deleted (check Proxmox)

# Rebuild from scratch
make bootstrap
```

## Recovery from Backup

### Restore etcd Snapshot (Talos)

```bash
# List snapshots
talosctl -n <control-plane-ip> etcd snapshot ls

# Restore from snapshot (use with caution!)
# This is destructive - only use for disaster recovery
talosctl -n <control-plane-ip> etcd snapshot <snapshot-name>
```

### Restore Application Data

Application data restoration depends on the specific application. See application-specific runbooks.

## Post-Bootstrap Tasks

### 1. Update DNS Records

If public DNS changes are needed:
```bash
make cloudflare-ddns-gen
```

### 2. Configure Monitoring

Verify Grafana Cloud is receiving metrics:
- Log in to Grafana Cloud
- Check dashboards for cluster metrics
- Verify log ingestion

### 3. Test External Access

```bash
curl https://argocd.ravil.space
# Should return ArgoCD login page
```

### 4. Create Initial Sealed Secrets

For new applications requiring secrets:
```bash
kubectl create secret generic <name> \
  --from-literal=key=value \
  --dry-run=client -o yaml -n <namespace> | \
kubeseal \
  --cert ./terraform/modules/sealed-secrets/certs/sealed-secrets.cert \
  --scope strict \
  --namespace <namespace> \
  --name <name> \
  --format yaml > sealed-<name>.yaml
```

## Regular Maintenance

### Weekly
- Review ArgoCD for sync errors
- Check Renovate PRs for updates

### Monthly
- Review Grafana dashboards
- Check resource utilization
- Review logs for errors

### Quarterly
- Test disaster recovery procedures
- Review and update documentation
- Security review

## Related Runbooks

- [Disaster Recovery](disaster-recovery.md)
- [Troubleshooting Guide](troubleshooting.md)
- [Upgrade Procedures](upgrades.md) (future)

## References

- [Makefile](../../Makefile)
- [Terraform Main](../../terraform/main.tf)
- [ArgoCD Configuration](../../k8s/infra/argocd/)
- [CLAUDE.md](../../CLAUDE.md) - Command reference
