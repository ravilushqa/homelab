# External Services Directory

This directory contains Kubernetes configurations for external services using the Kustomize components approach.

## Overview

These configurations use Kustomize components to provide a DRY (Don't Repeat Yourself) approach to managing external services. Services are divided into two types:

1. **HTTP Services** (most services) - Using the `external-service` component
   - HTTP-based routing through the Gateway API
   - EndpointSlice pointing to an external IP

2. **TLS Services** (e.g., Proxmox) - Using the `tls-service` component
   - TLS passthrough routing through the Gateway API
   - Service handles its own TLS termination

## Creating a New Service

### For HTTP-based services:

```bash
make create-external-service SERVICE=name PORT=port IP=ip
```

Example:
```bash
make create-external-service SERVICE=my-service PORT=8080 IP=192.168.1.100
```

### For TLS-based services:

```bash
make create-tls-service SERVICE=name PORT=port IP=ip
```

Example:
```bash
make create-tls-service SERVICE=proxmox PORT=8006 IP=192.168.1.100
```

## Applying a Service Configuration

Apply a service configuration with:

```bash
kubectl kustomize k8s/apps/external/my-service | kubectl apply -f -
```

## Directory Structure

Each service directory contains:
- `namespace.yaml` - The service namespace definition
- `kustomization.yaml` - Kustomize configuration that uses the appropriate component and applies service-specific patches

## How it Works

The configuration uses:
1. Reusable Kustomize components in `/k8s/components/`
2. JSON patches to customize each resource for the specific service
3. Namespace-specific resources

For more details on the component implementations, see:
- [external-service component](/k8s/components/external-service/README.md)
- [tls-service component](/k8s/components/tls-service/README.md)

## Advantages of This Approach

- **Maintainability**: Single source of truth for common configurations
- **Consistency**: All services follow the same pattern
- **Simplicity**: Easy to add new services without duplicating code
- **Flexibility**: Service-specific customizations are clearly defined
- **Discoverability**: Clear directory structure makes it easy to find services