#!/bin/bash
set -e

# Check if required parameters are provided
if [ $# -lt 3 ]; then
  echo "Usage: $0 <service-name> <target-port> <ip>"
  exit 1
fi

SERVICE_NAME=$1
TARGET_PORT=$2
IP=$3

# Get the project root directory (parent of scripts directory)
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Create directory for the service
SERVICE_DIR="$PROJECT_ROOT/k8s/apps/external/$SERVICE_NAME"
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
  - ../../../components/tls-service

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
        path: /spec/ports/0/targetPort
        value: $TARGET_PORT
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
        value: $TARGET_PORT
      - op: replace
        path: /endpoints/0/addresses/0
        value: $IP
    target:
      kind: EndpointSlice
      name: service-name

  # Update TLSRoute
  - patch: |-
      - op: replace
        path: /metadata/name
        value: $SERVICE_NAME-tls
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
        value: 443
    target:
      kind: TLSRoute
      name: service-name-tls
EOF

# Create namespace.yaml
cat > "$SERVICE_DIR/namespace.yaml" << EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $SERVICE_NAME
EOF

echo "Created TLS service $SERVICE_NAME configuration in $SERVICE_DIR"
echo "To apply this configuration, run:"
echo "kubectl kustomize $SERVICE_DIR | kubectl apply -f -"