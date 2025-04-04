#!/bin/bash
set -e

# Check if required parameters are provided
if [ $# -lt 3 ]; then
  echo "Usage: $0 <service-name> <port> <ip>"
  exit 1
fi

SERVICE_NAME=$1
PORT=$2
IP=$3

# Create directory for the service
SERVICE_DIR="/Users/ravil/projects/homelab/k8s/apps/external/$SERVICE_NAME"
mkdir -p "$SERVICE_DIR"

# Create kustomization.yaml with the component and patches
cat > "$SERVICE_DIR/kustomization.yaml" << EOF
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
        value: $SERVICE_NAME
      - op: replace
        path: /metadata/namespace
        value: $SERVICE_NAME
      - op: replace
        path: /spec/ports/0/port
        value: $PORT
    target:
      kind: Service
      name: service-name

  # Update EndpointSlice
  - patch: |-
      - op: replace
        path: /metadata/name
        value: $SERVICE_NAME
      - op: replace
        path: /metadata/namespace
        value: $SERVICE_NAME
      - op: replace
        path: /metadata/labels/kubernetes.io~1service-name
        value: $SERVICE_NAME
      - op: replace
        path: /ports/0/port
        value: $PORT
      - op: replace
        path: /endpoints/0/addresses/0
        value: $IP
    target:
      kind: EndpointSlice
      name: service-name

  # Update HTTPRoute
  - patch: |-
      - op: replace
        path: /metadata/name
        value: $SERVICE_NAME
      - op: replace
        path: /metadata/namespace
        value: $SERVICE_NAME
      - op: replace
        path: /spec/hostnames/0
        value: $SERVICE_NAME.ravil.space
      - op: replace
        path: /spec/rules/0/backendRefs/0/name
        value: $SERVICE_NAME
      - op: replace
        path: /spec/rules/0/backendRefs/0/port
        value: $PORT
    target:
      kind: HTTPRoute
      name: service-name
EOF

# Create namespace.yaml
cat > "$SERVICE_DIR/namespace.yaml" << EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $SERVICE_NAME
EOF

echo "Created external service $SERVICE_NAME configuration in $SERVICE_DIR"
echo "To apply this configuration, run:"
echo "kubectl kustomize $SERVICE_DIR | kubectl apply -f -"