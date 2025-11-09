#!/bin/bash
# Script to create sealed secret for Inbox Zero
# This is a TEMPLATE - fill in your actual values before running

# Generate random secrets
AUTH_SECRET=$(openssl rand -hex 32)
EMAIL_ENCRYPT_SECRET=$(openssl rand -hex 32)
EMAIL_ENCRYPT_SALT=$(openssl rand -hex 16)
GOOGLE_ENCRYPT_SECRET=$(openssl rand -hex 32)
GOOGLE_ENCRYPT_SALT=$(openssl rand -hex 16)
INTERNAL_API_KEY=$(openssl rand -hex 32)
API_KEY_SALT=$(openssl rand -hex 32)

# TODO: Replace these placeholder values with your actual credentials
DATABASE_URL="postgresql://user:password@host:5432/database?sslmode=require&channel_binding=require"
GOOGLE_CLIENT_ID="YOUR_GOOGLE_CLIENT_ID"
GOOGLE_CLIENT_SECRET="YOUR_GOOGLE_CLIENT_SECRET"
OPENROUTER_API_KEY="YOUR_OPENROUTER_API_KEY"
UPSTASH_REDIS_URL="https://your-redis.upstash.io"
UPSTASH_REDIS_TOKEN="YOUR_UPSTASH_TOKEN"

kubectl create secret generic inbox-zero-secrets \
  --from-literal="database-url=$DATABASE_URL" \
  --from-literal="auth-secret=$AUTH_SECRET" \
  --from-literal="google-client-id=$GOOGLE_CLIENT_ID" \
  --from-literal="google-client-secret=$GOOGLE_CLIENT_SECRET" \
  --from-literal="email-encrypt-secret=$EMAIL_ENCRYPT_SECRET" \
  --from-literal="email-encrypt-salt=$EMAIL_ENCRYPT_SALT" \
  --from-literal="google-encrypt-secret=$GOOGLE_ENCRYPT_SECRET" \
  --from-literal="google-encrypt-salt=$GOOGLE_ENCRYPT_SALT" \
  --from-literal="openrouter-api-key=$OPENROUTER_API_KEY" \
  --from-literal="internal-api-key=$INTERNAL_API_KEY" \
  --from-literal="api-key-salt=$API_KEY_SALT" \
  --from-literal="upstash-redis-url=$UPSTASH_REDIS_URL" \
  --from-literal="upstash-redis-token=$UPSTASH_REDIS_TOKEN" \
  --namespace inbox-zero \
  --dry-run=client -o yaml | \
kubeseal --cert ../../../../terraform/modules/sealed-secrets/certs/sealed-secrets.cert \
  --scope strict --namespace inbox-zero --name inbox-zero-secrets \
  --format yaml > sealed-config.yaml

echo "Sealed secret created at sealed-config.yaml"
echo "The sealed-config.yaml file is encrypted and safe to commit to git"
