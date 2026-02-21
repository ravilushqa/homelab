# Runbook: Troubleshooting Guide

## Overview

This runbook provides troubleshooting procedures for common issues in the HomeLab infrastructure.

## Quick Diagnostic Commands

```bash
# Cluster health
kubectl get nodes
kubectl get pods -A | grep -v Running
kubectl top nodes
kubectl top pods -A

# ArgoCD status
kubectl get applications -n argocd
kubectl get applications -n argocd -o json | jq '.items[] | select(.status.sync.status != "Synced")'

# Certificate status
kubectl get certificates -A
kubectl get certificaterequests -A

# Network connectivity
kubectl run debug --rm -it --image=nicolaka/netshoot -- /bin/bash
```

## Issue Categories

- [Node Issues](#node-issues)
- [Pod Issues](#pod-issues)
- [Networking Issues](#networking-issues)
- [Storage Issues](#storage-issues)
- [ArgoCD Issues](#argocd-issues)
- [Certificate Issues](#certificate-issues)
- [Application Issues](#application-issues)

---

## Node Issues

### Symptom: Node Not Ready

```bash
kubectl get nodes
# Shows NotReady status
```

**Diagnosis**:
```bash
# Describe node
kubectl describe node <node-name>

# Check node conditions
kubectl get node <node-name> -o json | jq '.status.conditions'

# Check kubelet logs (via talosctl)
talosctl logs -n <node-ip> kubelet
```

**Common Causes & Solutions**:

1. **Network plugin not running**
   ```bash
   # Check Cilium pods
   kubectl get pods -n kube-system -l k8s-app=cilium

   # If not running, redeploy
   kubectl kustomize --enable-helm ./k8s/infra/network/cilium | kubectl apply -f -
   ```

2. **Disk pressure**
   ```bash
   # Check disk usage on node
   talosctl -n <node-ip> df

   # Clean up unused images
   talosctl -n <node-ip> service kubelet restart
   ```

3. **Memory pressure**
   ```bash
   # Check memory on node
   talosctl -n <node-ip> free

   # Identify memory-hungry pods
   kubectl top pods -A --sort-by=memory

   # Scale down or delete pods if necessary
   ```

### Symptom: High CPU/Memory Usage

**Diagnosis**:
```bash
# Node resource usage
kubectl top nodes

# Pod resource usage
kubectl top pods -A --sort-by=cpu
kubectl top pods -A --sort-by=memory
```

**Solutions**:
```bash
# Identify top consumers
kubectl top pods -A --sort-by=memory | head -20

# Check resource limits
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 Limits

# Scale down if needed
kubectl scale deployment <deployment> -n <namespace> --replicas=1
```

---

## Pod Issues

### Symptom: Pod Stuck in Pending

**Diagnosis**:
```bash
kubectl describe pod <pod-name> -n <namespace>
```

**Common Causes & Solutions**:

1. **Insufficient resources**
   ```
   Warning  FailedScheduling  pod failed to fit in any node
   ```

   **Solution**:
   ```bash
   # Check node capacity
   kubectl describe nodes | grep -A 5 "Allocated resources"

   # Reduce resource requests or add nodes
   ```

2. **PVC not bound**
   ```
   Warning  FailedMount  Unable to attach or mount volumes
   ```

   **Solution**:
   ```bash
   # Check PVC status
   kubectl get pvc -n <namespace>

   # Check PV provisioning
   kubectl get pv

   # Check CSI driver
   kubectl get pods -n kube-system | grep csi
   ```

3. **Image pull error**
   ```
   Warning  Failed  Failed to pull image
   ```

   **Solution**:
   ```bash
   # Check image pull secrets
   kubectl get secrets -n <namespace>

   # Manually pull to verify
   talosctl -n <worker-ip> run --rm -it docker.io/library/busybox -- echo test
   ```

### Symptom: Pod CrashLoopBackOff

**Diagnosis**:
```bash
# Check pod logs
kubectl logs <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace> --previous

# Check events
kubectl describe pod <pod-name> -n <namespace> | tail -20
```

**Common Causes & Solutions**:

1. **Application error**
   - Check logs for error messages
   - Verify configuration (ConfigMaps, Secrets)
   - Check liveness/readiness probes

2. **Missing dependencies**
   - Database not ready
   - Required service unavailable
   - Missing environment variables

3. **Resource limits too low**
   ```bash
   # Check OOMKilled
   kubectl describe pod <pod-name> -n <namespace> | grep -i oom

   # Increase memory limits
   ```

### Symptom: Pod Evicted

```bash
kubectl get pods -A | grep Evicted
```

**Causes**:
- Node disk pressure
- Node memory pressure
- Node PID pressure

**Solution**:
```bash
# Clean up evicted pods
kubectl get pods -A | grep Evicted | awk '{print $2 " -n " $1}' | xargs -L1 kubectl delete pod

# Address root cause (disk/memory pressure)
kubectl top nodes
```

---

## Networking Issues

### Symptom: Pod Cannot Reach External Network

**Diagnosis**:
```bash
# Create debug pod
kubectl run debug --rm -it --image=nicolaka/netshoot -- /bin/bash

# Inside debug pod:
ping 8.8.8.8
nslookup google.com
curl https://www.google.com
```

**Solutions**:

1. **DNS not working**
   ```bash
   # Check CoreDNS
   kubectl get pods -n kube-system -l k8s-app=kube-dns
   kubectl logs -n kube-system -l k8s-app=kube-dns

   # Test DNS resolution
   kubectl run debug --rm -it --image=nicolaka/netshoot -- nslookup kubernetes.default
   ```

2. **Network policy blocking**
   ```bash
   # Check network policies
   kubectl get networkpolicies -A
   kubectl describe networkpolicy <policy-name> -n <namespace>

   # Temporarily remove policy for testing
   kubectl delete networkpolicy <policy-name> -n <namespace>
   ```

### Symptom: Service Not Reachable

**Diagnosis**:
```bash
# Check service
kubectl get svc <service-name> -n <namespace>
kubectl describe svc <service-name> -n <namespace>

# Check endpoints
kubectl get endpoints <service-name> -n <namespace>

# Test from another pod
kubectl run debug --rm -it --image=nicolaka/netshoot -- curl http://<service-name>.<namespace>.svc.cluster.local
```

**Solutions**:

1. **No endpoints (no ready pods)**
   ```bash
   # Check pod selector
   kubectl get svc <service-name> -n <namespace> -o yaml | grep -A 5 selector
   kubectl get pods -n <namespace> --show-labels

   # Ensure pods are running and ready
   ```

2. **Wrong port**
   ```bash
   # Verify service ports match container ports
   kubectl get svc <service-name> -n <namespace> -o yaml
   kubectl get pod <pod-name> -n <namespace> -o yaml | grep -A 10 ports
   ```

### Symptom: Ingress/Gateway Not Working

**Diagnosis**:
```bash
# Check Gateway
kubectl get gateways -A
kubectl describe gateway <gateway-name> -n <namespace>

# Check HTTPRoutes
kubectl get httproutes -A
kubectl describe httproute <route-name> -n <namespace>

# Check from outside
curl -v https://<domain>
```

**Solutions**:

1. **Gateway not ready**
   ```bash
   # Check Gateway API CRDs
   kubectl get crds | grep gateway

   # Check Cilium gateway controller
   kubectl logs -n kube-system -l app.kubernetes.io/name=cilium-gateway
   ```

2. **Certificate issue**
   ```bash
   # Check certificate
   kubectl get certificates -A
   kubectl describe certificate <cert-name> -n <namespace>

   # Check cert-manager logs
   kubectl logs -n cert-manager -l app=cert-manager
   ```

3. **DNS not pointing to correct IP**
   ```bash
   # Check DNS resolution
   nslookup <domain>

   # Update Cloudflare DNS if needed
   make cloudflare-ddns-gen
   ```

---

## Storage Issues

### Symptom: PVC Stuck in Pending

**Diagnosis**:
```bash
kubectl describe pvc <pvc-name> -n <namespace>
kubectl get pv
kubectl get storageclass
```

**Solutions**:

1. **No storage class**
   ```bash
   # Check default storage class
   kubectl get storageclass

   # Set proxmox-csi as default
   kubectl patch storageclass proxmox-csi -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
   ```

2. **CSI driver not running**
   ```bash
   # Check CSI pods
   kubectl get pods -n kube-system | grep csi

   # Check CSI driver
   kubectl get csidrivers

   # Restart CSI if needed
   kubectl rollout restart deployment proxmox-csi-controller -n kube-system
   ```

3. **Proxmox storage full**
   - Check Proxmox storage capacity
   - Clean up unused volumes

### Symptom: PV Mount Errors

**Diagnosis**:
```bash
kubectl describe pod <pod-name> -n <namespace> | grep -A 10 Events
talosctl -n <worker-ip> logs kubelet
```

**Solutions**:
- Check volume exists in Proxmox
- Verify node has access to Proxmox storage
- Check CSI node driver logs

---

## ArgoCD Issues

### Symptom: Application Not Syncing

**Diagnosis**:
```bash
kubectl describe application <app-name> -n argocd
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-application-controller
```

**Solutions**:

1. **Git repository unreachable**
   ```bash
   # Check repository secret
   kubectl get secrets -n argocd | grep repo

   # Test Git access from ArgoCD pod
   kubectl exec -it -n argocd deployment/argocd-server -- git ls-remote <repo-url>
   ```

2. **Invalid manifests**
   ```bash
   # Validate manifests locally
   kubectl kustomize ./k8s/apps/<app>

   # Check for YAML syntax errors
   ```

3. **Resource conflicts**
   ```bash
   # Check for existing resources
   kubectl get <resource> -n <namespace>

   # Delete conflicting resources if needed
   ```

### Symptom: ArgoCD UI Not Accessible

**Diagnosis**:
```bash
# Check ArgoCD pods
kubectl get pods -n argocd

# Check Gateway route
kubectl get httproute -n argocd
kubectl describe httproute argocd-httproute -n argocd

# Check certificate
kubectl get certificate -n argocd
```

**Solutions**:
```bash
# Restart ArgoCD
make argocd-restart

# Port-forward as temporary access
kubectl port-forward -n argocd svc/argocd-server 8080:443
# Access at https://localhost:8080
```

### Symptom: Application Stuck in Progressing

**Solutions**:
```bash
# Force hard refresh
argocd app sync <app-name> --force

# Delete and recreate
kubectl delete application <app-name> -n argocd
kubectl apply -f k8s/infra/argocd/applications/<app>.yaml
```

---

## Certificate Issues

### Symptom: Certificate Not Issued

**Diagnosis**:
```bash
kubectl get certificates -A
kubectl describe certificate <cert-name> -n <namespace>
kubectl get certificaterequest -A
kubectl describe certificaterequest <request-name> -n <namespace>
```

**Solutions**:

1. **ACME challenge failing**
   ```bash
   # Check cert-manager logs
   kubectl logs -n cert-manager -l app=cert-manager

   # Check challenge
   kubectl get challenges -A
   kubectl describe challenge <challenge-name> -n <namespace>

   # Common issues:
   # - DNS not pointing to correct IP
   # - Firewall blocking port 80/443
   # - Rate limiting from Let's Encrypt
   ```

2. **ClusterIssuer not ready**
   ```bash
   # Check issuer
   kubectl get clusterissuer
   kubectl describe clusterissuer letsencrypt-prod

   # Recreate if needed
   kubectl delete clusterissuer letsencrypt-prod
   kubectl kustomize --enable-helm ./k8s/infra/security/cert-manager | kubectl apply -f -
   ```

### Symptom: Certificate Expired

**Solutions**:
```bash
# Check expiry
kubectl get certificate <cert-name> -n <namespace> -o yaml | grep -A 5 notAfter

# Force renewal
kubectl delete secret <tls-secret-name> -n <namespace>
kubectl delete certificaterequest -n <namespace> --all

# cert-manager will automatically recreate
```

---

## Application Issues

### Application-Specific Debugging

For each application:

```bash
# Check application pods
kubectl get pods -n <namespace>

# Check logs
kubectl logs -n <namespace> -l app=<app-name>

# Describe deployment
kubectl describe deployment <deployment-name> -n <namespace>

# Check service
kubectl get svc -n <namespace>

# Check ingress/routes
kubectl get httproute -n <namespace>
```

### Common Application Patterns

#### Database Connection Issues

```bash
# Check database pod
kubectl get pods -n <namespace> -l app=postgres

# Check connection from app pod
kubectl exec -it -n <namespace> <pod-name> -- env | grep DB
kubectl exec -it -n <namespace> <pod-name> -- nc -zv <db-service> 5432
```

#### Secret/ConfigMap Issues

```bash
# Check secret exists
kubectl get secret <secret-name> -n <namespace>

# Verify sealed secret was decrypted
kubectl get sealedsecret <sealed-secret-name> -n <namespace>
kubectl describe sealedsecret <sealed-secret-name> -n <namespace>

# Check configmap
kubectl get configmap <configmap-name> -n <namespace> -o yaml
```

---

## Emergency Procedures

### Complete Cluster Restart

```bash
# Graceful restart of all nodes
talosctl -n <control-plane-ip> reboot
talosctl -n <worker-ip> reboot

# Wait for nodes to come back
kubectl get nodes -w
```

### ArgoCD Emergency Access

```bash
# Port-forward to access UI
kubectl port-forward -n argocd svc/argocd-server 8080:443

# Get admin password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

### Disable ArgoCD Auto-Sync

```bash
# Temporarily disable auto-sync for an app
kubectl patch application <app-name> -n argocd --type merge -p '{"spec":{"syncPolicy":null}}'

# Re-enable
kubectl patch application <app-name> -n argocd --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

---

## Monitoring and Logs

### Check Metrics in Grafana Cloud

- Log in to Grafana Cloud
- Navigate to Kubernetes dashboards
- Check for anomalies in resource usage, error rates

### View Centralized Logs

```bash
# Access Grafana Loki (if configured)
# Query logs in Grafana Cloud

# Alternative: kubectl logs
kubectl logs -n <namespace> <pod-name> --tail=100 --follow
```

### Check Node Logs (Talos)

```bash
# Service logs
talosctl -n <node-ip> logs kubelet
talosctl -n <node-ip> logs controller-runtime

# System logs
talosctl -n <node-ip> dmesg

# Service status
talosctl -n <node-ip> services
```

---

## Performance Troubleshooting

### Slow Application Response

1. **Check resource limits**
   ```bash
   kubectl top pods -n <namespace>
   kubectl describe pod <pod-name> -n <namespace> | grep -A 5 Limits
   ```

2. **Check network latency**
   ```bash
   kubectl run debug --rm -it --image=nicolaka/netshoot -- ping <service-name>.<namespace>
   ```

3. **Check database performance**
   - Query slow query logs
   - Check connection pool size
   - Verify indexes

### High Resource Usage

```bash
# Identify top consumers
kubectl top pods -A --sort-by=memory | head -20
kubectl top pods -A --sort-by=cpu | head -20

# Check for memory leaks
kubectl logs -n <namespace> <pod-name> | grep -i "out of memory"

# Analyze resource requests vs actual usage
kubectl describe nodes
```

---

## Related Runbooks

- [Bootstrap](bootstrap.md) - Initial cluster setup
- [Disaster Recovery](disaster-recovery.md) - Recovery procedures

## References

- [Kubernetes Debugging](https://kubernetes.io/docs/tasks/debug/)
- [Talos Troubleshooting](https://www.talos.dev/latest/learn-more/troubleshooting/)
- [ArgoCD Troubleshooting](https://argo-cd.readthedocs.io/en/stable/operator-manual/troubleshooting/)
