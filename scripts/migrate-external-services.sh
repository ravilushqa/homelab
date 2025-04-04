#!/bin/bash
set -e

# Directories
SOURCE_DIR="/Users/ravil/projects/homelab/k8s/apps/external"
TARGET_DIR="/Users/ravil/projects/homelab/k8s/apps/external-components"

# Skip Proxmox
SKIP_SERVICES=("proxmox")

# Function to extract values from a service
extract_service_info() {
  local service_dir="$1"
  local service_name=$(basename "$service_dir")
  
  # Extract service name from service yaml
  local svc_name=$(grep -A2 "metadata:" "$service_dir/svc.yaml" | grep "name:" | head -1 | awk '{print $2}')
  
  # Extract port from service yaml
  local port=$(grep -A5 "spec:" "$service_dir/svc.yaml" | grep "port:" | awk '{print $2}')
  
  # Extract IP from endpoint-slice yaml
  local ip=$(grep -A3 "addresses:" "$service_dir/endpoint-slice.yaml" | grep -v "addresses" | grep -v "conditions" | head -1 | sed 's/.*- //' | tr -d ' ')
  
  # Check if the service uses a tls-route
  if [ -f "$service_dir/tls-route.yaml" ]; then
    local route_type="tls"
  else
    local route_type="http"
  fi
  
  echo "$service_name|$svc_name|$port|$ip|$route_type"
}

# Create target directory if it doesn't exist
mkdir -p "$TARGET_DIR"

echo "Starting migration of external services to component-based approach..."

# Process each service
for service_dir in "$SOURCE_DIR"/*; do
  service_name=$(basename "$service_dir")
  
  # Skip Proxmox or any other service in the skip list
  if [[ " ${SKIP_SERVICES[@]} " =~ " $service_name " ]]; then
    echo "Skipping $service_name (excluded from migration)"
    continue
  fi
  
  echo "Processing $service_name..."
  
  # Extract service info
  service_info=$(extract_service_info "$service_dir")
  service_ns=$(echo "$service_info" | cut -d'|' -f1)
  svc_name=$(echo "$service_info" | cut -d'|' -f2)
  port=$(echo "$service_info" | cut -d'|' -f3)
  ip=$(echo "$service_info" | cut -d'|' -f4)
  route_type=$(echo "$service_info" | cut -d'|' -f5)
  
  echo "- Namespace: $service_ns"
  echo "- Service name: $svc_name"
  echo "- Port: $port"
  echo "- IP: $ip"
  echo "- Route type: $route_type"
  
  # Standard HTTP route services
  if [ "$route_type" = "http" ]; then
    echo "Creating component-based service for $service_name..."
    # Create service using our script (special handling for services with different service name)
    if [ "$service_ns" = "$svc_name" ]; then
      /Users/ravil/projects/homelab/scripts/create-external-service-component.sh "$service_ns" "$port" "$ip"
    else
      # Create directory for the service
      TARGET_SERVICE_DIR="$TARGET_DIR/$service_ns"
      mkdir -p "$TARGET_SERVICE_DIR"
      
      # Create kustomization.yaml with custom patches for different service name
      cat > "$TARGET_SERVICE_DIR/kustomization.yaml" << EOF
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
        value: $svc_name
      - op: replace
        path: /metadata/namespace
        value: $service_ns
      - op: replace
        path: /spec/ports/0/port
        value: $port
    target:
      kind: Service
      name: service-name

  # Update EndpointSlice
  - patch: |-
      - op: replace
        path: /metadata/name
        value: $svc_name
      - op: replace
        path: /metadata/namespace
        value: $service_ns
      - op: replace
        path: /metadata/labels/kubernetes.io~1service-name
        value: $svc_name
      - op: replace
        path: /ports/0/port
        value: $port
      - op: replace
        path: /endpoints/0/addresses/0
        value: $ip
    target:
      kind: EndpointSlice
      name: service-name

  # Update HTTPRoute
  - patch: |-
      - op: replace
        path: /metadata/name
        value: $svc_name
      - op: replace
        path: /metadata/namespace
        value: $service_ns
      - op: replace
        path: /spec/hostnames/0
        value: $service_ns.ravil.space
      - op: replace
        path: /spec/rules/0/backendRefs/0/name
        value: $svc_name
      - op: replace
        path: /spec/rules/0/backendRefs/0/port
        value: $port
    target:
      kind: HTTPRoute
      name: service-name
EOF

      # Create namespace.yaml
      cat > "$TARGET_SERVICE_DIR/namespace.yaml" << EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $service_ns
EOF

    fi
  else
    echo "! Skipping $service_name (uses TLS route, requires manual migration)"
  fi
  
  echo "Done processing $service_name"
  echo "----------------------------"
done

echo "Migration complete!"
echo "New services are available in $TARGET_DIR"
echo "NOTE: You need to update your Makefile's k8s-apply target to use these new configurations"