# External Service Component

This Kustomize component provides a reusable template for creating external service facades in Kubernetes.

## Overview

The external service component creates the following resources:
- Service
- EndpointSlice (for routing to an external IP)
- HTTPRoute (for Gateway API routing)

Each service configuration also adds a Namespace resource.

## Usage

To use this component in your service configuration:

1. Create a new service configuration with:
   ```
   make create-external-service SERVICE=my-service PORT=8080 IP=192.168.1.100
   ```

   This will create all necessary files in `k8s/apps/external-components/my-service/`.

2. Apply the configuration:
   ```
   kubectl kustomize k8s/apps/external-components/my-service | kubectl apply -f -
   ```

## Manual Creation

If you prefer to manually create a service:

1. Create a directory for your service:
   ```
   mkdir -p k8s/apps/external-components/my-service
   ```

2. Create a `namespace.yaml`:
   ```yaml
   apiVersion: v1
   kind: Namespace
   metadata:
     name: my-service
   ```

3. Create a `kustomization.yaml` that includes the component and patches:
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   
   # Namespace resource
   resources:
     - namespace.yaml
   
   # Use our component
   components:
     - ../../../components/external-service
   
   # Service-specific patches
   patches:
     # Update Service
     - patch: |-
         - op: replace
           path: /metadata/name
           value: my-service
         - op: replace
           path: /metadata/namespace
           value: my-service
         - op: replace
           path: /spec/ports/0/port
           value: 8080
       target:
         kind: Service
         name: service-name
   
     # Update EndpointSlice
     - patch: |-
         - op: replace
           path: /metadata/name
           value: my-service
         - op: replace
           path: /metadata/namespace
           value: my-service
         - op: replace
           path: /metadata/labels/kubernetes.io~1service-name
           value: my-service
         - op: replace
           path: /ports/0/port
           value: 8080
         - op: replace
           path: /endpoints/0/addresses/0
           value: 192.168.1.100
       target:
         kind: EndpointSlice
         name: service-name
   
     # Update HTTPRoute
     - patch: |-
         - op: replace
           path: /metadata/name
           value: my-service
         - op: replace
           path: /metadata/namespace
           value: my-service
         - op: replace
           path: /spec/hostnames/0
           value: my-service.ravil.space
         - op: replace
           path: /spec/rules/0/backendRefs/0/name
           value: my-service
         - op: replace
           path: /spec/rules/0/backendRefs/0/port
           value: 8080
       target:
         kind: HTTPRoute
         name: service-name
   ```

## Component Structure

The component is defined with the following files:
- `kustomization.yaml` - Main component definition
- `service.yaml` - Service template
- `endpoint-slice.yaml` - EndpointSlice template
- `http-route.yaml` - HTTPRoute template

## Advantages Over Previous Approaches

1. **Cleaner Structure**: Uses Kustomize components for true template reuse
2. **More Declarative**: Clearly shows what's being customized without complex JSON manipulations
3. **Easier to Understand**: Much more intuitive than using patching or replacements directly
4. **Better Separation of Concerns**: Templates live in a components directory, services in applications directory
5. **Simpler to Extend**: Add new resources or behavior to the component once, all services inherit