# Kubernetes Components

This directory contains reusable Kustomize components for various service patterns in our homelab.

## Available Components

### 1. [external-service](./external-service/README.md)

A component for configuring external services that use HTTP routing. This is used for most services that communicate via HTTP and don't need special TLS handling.

Example usage: `make create-external-service SERVICE=name PORT=port IP=ip`

### 2. [tls-service](./tls-service/README.md)

A component for configuring external services that require TLS passthrough routing. Used for services like Proxmox that handle their own TLS termination.

Example usage: `make create-tls-service SERVICE=name PORT=port IP=ip`

## How Components Work

Components provide a templated approach to common deployment patterns. They contain:

1. Base YAML templates for common resources
2. A kustomization.yaml file that defines the component
3. Documentation on how to use the component

## Adding New Services

Service configurations are created in `/k8s/apps/external-components/` and apply JSON patches to customize the component-based templates for a specific service.

See the README in each component directory for more details on their usage.