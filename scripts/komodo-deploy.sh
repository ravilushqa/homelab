#!/usr/bin/env bash
set -euo pipefail

# Komodo deploy script — reads optional komodo.yaml for stack configuration
# Required env: KOMODO_API, KOMODO_KEY, KOMODO_SECRET, KOMODO_SERVER_ID

STACK="$1"
STACK_DIR="stacks/$STACK"
KOMODO_YAML="$STACK_DIR/komodo.yaml"

komodo_read() {
  curl -sf -X POST "$KOMODO_API/read" \
    -H "X-Api-Key: $KOMODO_KEY" \
    -H "X-Api-Secret: $KOMODO_SECRET" \
    -H "Content-Type: application/json" \
    -d "$1"
}

komodo_write() {
  curl -sf -X POST "$KOMODO_API/write" \
    -H "X-Api-Key: $KOMODO_KEY" \
    -H "X-Api-Secret: $KOMODO_SECRET" \
    -H "Content-Type: application/json" \
    -d "$1"
}

komodo_execute() {
  curl -sf -X POST "$KOMODO_API/execute" \
    -H "X-Api-Key: $KOMODO_KEY" \
    -H "X-Api-Secret: $KOMODO_SECRET" \
    -H "Content-Type: application/json" \
    -d "$1"
}

# Build config JSON from komodo.yaml + defaults
build_config() {
  local config

  # Start with defaults
  config=$(jq -n \
    --arg server_id "$KOMODO_SERVER_ID" \
    --arg repo "ravilushqa/homelab" \
    --arg branch "main" \
    --arg clone_path "stacks/$STACK" \
    '{server_id: $server_id, repo: $repo, branch: $branch, clone_path: $clone_path}')

  # Merge komodo.yaml config overrides if present
  if [ -f "$KOMODO_YAML" ]; then
    local yaml_config
    yaml_config=$(yq -o json '.config // {}' "$KOMODO_YAML")
    config=$(echo "$config" | jq --argjson overrides "$yaml_config" '. * $overrides')
  fi

  echo "$config"
}

# Build top-level fields (description, tags) from komodo.yaml
build_top_level() {
  local params="{}"

  if [ -f "$KOMODO_YAML" ]; then
    local desc tags
    desc=$(yq -r '.description // ""' "$KOMODO_YAML")
    tags=$(yq -o json '.tags // []' "$KOMODO_YAML")

    params=$(jq -n --arg desc "$desc" --argjson tags "$tags" \
      '{description: $desc, tags: $tags}')
  fi

  echo "$params"
}

echo "🚀 Deploying $STACK..."

# Check if stack exists
existing=$(komodo_read '{"type": "ListStacks", "params": {}}' \
  | jq -r ".[] | select(.name == \"$STACK\") | ._id.\"\$oid\"")

if [ -z "$existing" ]; then
  echo "📦 Stack $STACK not found in Komodo, creating..."
  stack_id=$(komodo_write "{\"type\": \"CreateStack\", \"params\": {\"name\": \"$STACK\"}}" \
    | jq -r '._id."$oid"')
  echo "  Created with id: $stack_id"
else
  stack_id="$existing"
  echo "  Stack exists: $stack_id"
fi

# Build and apply config
config=$(build_config)
top_level=$(build_top_level)

update_params=$(jq -n \
  --arg id "$stack_id" \
  --argjson config "$config" \
  --argjson top "$top_level" \
  '{id: $id, config: $config} * {description: $top.description, tags: $top.tags}')

echo "  Updating config..."
komodo_write "$(jq -n --argjson params "$update_params" '{type: "UpdateStack", params: $params}')" > /dev/null

# Pull and deploy
echo "  Pulling latest..."
komodo_execute "{\"type\": \"PullStack\", \"params\": {\"stack\": \"$STACK\"}}" > /dev/null

echo "  Deploying..."
komodo_execute "{\"type\": \"DeployStack\", \"params\": {\"stack\": \"$STACK\"}}" > /dev/null

echo "✅ $STACK deployed"
