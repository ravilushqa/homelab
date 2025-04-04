# TLS Service Component

This component provides a template for configuring external services that require TLS passthrough routing. It's used for services like Proxmox that handle their own TLS termination.

## Resources Included

- **Service**: Kubernetes Service to define the port and name
- **EndpointSlice**: Points to the external IP where the actual service runs
- **TLSRoute**: Gateway API TLS route for passthrough routing

## Usage

To use this component, create a Kustomization that references this component and provides patches for the service-specific values.

Example:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

# Namespace resource
resources:
  - namespace.yaml

# Use our component
components:
  - ../../../components/tls-service

# Service-specific patches
patches:
  # Update values with your service configuration
```