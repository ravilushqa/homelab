# Inbox Zero - AI Email Assistant

Inbox Zero deployment for Kubernetes using a custom Docker image built from source.

## Prerequisites

1. **PostgreSQL Database**
   - Neon PostgreSQL database: `ep-proud-bread-94508845-pooler.eu-central-1.aws.neon.tech`
   - Update the `database-url` in the sealed secret with your connection string

2. **Google OAuth Setup**
   - Google Cloud Console project with OAuth credentials
   - People API enabled
   - Redirect URIs configured for your domain

3. **Upstash Redis**
   - Already configured with your existing Upstash instance

4. **OpenRouter API Key**
   - Already configured

5. **Docker Hub Account**
   - Custom image is hosted at: `ravilushqa/inbox-zero:latest`

## Custom Docker Image

The deployment uses a custom-built Docker image because Next.js bakes `NEXT_PUBLIC_*` environment variables at build time (not runtime). The image is built with the correct domain configured.

### Building the Image

```bash
# Clone the repository
git clone https://github.com/elie222/inbox-zero /tmp/inbox-zero

# Build for AMD64 architecture (Talos nodes)
docker buildx build \
  --platform linux/amd64 \
  --build-arg NEXT_PUBLIC_BASE_URL="https://inbox-zero.ravil.space" \
  -t ravilushqa/inbox-zero:latest \
  -f /tmp/inbox-zero/docker/Dockerfile.prod \
  /tmp/inbox-zero \
  --push
```

## Deployment Steps

### 1. Create Sealed Secret

The sealed secret is already created and committed. To regenerate:

```bash
cd /Users/ravil/projects/homelab/k8s/apps/internal/inbox-zero
./create-sealed-secret.sh
```

This will create `sealed-config.yaml` which is safe to commit to git.

### 2. Deploy to Kubernetes

```bash
kubectl apply -k /Users/ravil/projects/homelab/k8s/apps/internal/inbox-zero
```

### 3. Access the Application

The app will be available at: **https://inbox-zero.ravil.space**

## Post-Deployment Configuration

### Update Google OAuth Redirect URIs

Add these redirect URIs to your Google OAuth app:

- `https://inbox-zero.ravil.space/api/auth/callback/google`
- `https://inbox-zero.ravil.space/api/google/linking/callback`

### Upgrade to Premium

As an admin (galaktionov.r@gmail.com), visit:
- https://inbox-zero.ravil.space/admin

## Monitoring

### View Logs

```bash
kubectl logs -f -n inbox-zero -l app=inbox-zero
```

### Check Pod Status

```bash
kubectl get pods -n inbox-zero
kubectl describe pod <pod-name> -n inbox-zero
```

### ArgoCD

View the application in ArgoCD:
- https://argocd.ravil.space

## Troubleshooting

### DNS Resolution Issues

If you see database connection errors, the deployment includes a custom DNS configuration to use Google DNS (8.8.8.8, 8.8.4.4) instead of CoreDNS for external domains. This is configured in the deployment spec:

```yaml
dnsPolicy: "None"
dnsConfig:
  nameservers:
    - 8.8.8.8
    - 8.8.4.4
```

### Pod Won't Start

Check the logs:
```bash
kubectl logs -n inbox-zero -l app=inbox-zero
```

Common issues:
- Database connection failed - check DATABASE_URL and DNS configuration
- Missing secrets - ensure sealed-config.yaml is applied
- Image pull errors - check image tag and registry

### Database Migrations

Migrations are automatically applied on deployment. To manually run:
```bash
kubectl exec -n inbox-zero -l app=inbox-zero -- sh -c "cd /app/apps/web && pnpm exec prisma migrate deploy"
```

### Restart Deployment

```bash
kubectl rollout restart deployment/inbox-zero -n inbox-zero
```

## Configuration

### Environment Variables

All sensitive configuration is stored in the sealed secret. To update:

1. Edit `create-sealed-secret.sh` with your actual values
2. Run `./create-sealed-secret.sh`
3. Commit the new `sealed-config.yaml`
4. ArgoCD will auto-sync

### Resource Limits

Current limits:
- Memory: 512Mi - 2Gi
- CPU: 250m - 1000m

Adjust in `deployment.yaml` if needed.

## Clean Up

To remove the deployment:

```bash
kubectl delete namespace inbox-zero
```

Or remove the directory and let ArgoCD prune it.
