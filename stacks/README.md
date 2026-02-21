# Docker Stacks (Komodo)

Compose files for services managed by [Komodo](https://komo.do).

## Deployment

Komodo pulls these compose files from git and deploys them automatically.
Secrets are stored in Komodo Variables/Secrets — never in this repo.

## Required Secrets in Komodo

### traefik
- `ACME_EMAIL`
- `CF_DNS_API_TOKEN`
- `TRAEFIK_OIDC_CLIENT_ID`
- `TRAEFIK_OIDC_CLIENT_SECRET`
- `OIDC_SECRET`

### paperless-ngx
- `PAPERLESS_DB_PASSWORD`
- `PAPERLESS_SECRET_KEY`
- `PAPERLESS_OIDC_CLIENT_ID`
- `PAPERLESS_OIDC_CLIENT_SECRET`
- `PAPERLESS_API_TOKEN`
- `PAPERLESS_GPT_OPENAI_KEY`

### bytestash
- `BYTESTASH_JWT_SECRET`
- `BYTESTASH_OIDC_CLIENT_ID`
- `BYTESTASH_OIDC_CLIENT_SECRET`

### miniflux
- `MINIFLUX_DB_PASSWORD`
- `MINIFLUX_ADMIN_USERNAME`
- `MINIFLUX_ADMIN_PASSWORD`
- `MINIFLUX_OIDC_CLIENT_ID`
- `MINIFLUX_OIDC_CLIENT_SECRET`

### karakeep
- `KARAKEEP_NEXTAUTH_SECRET`
- `KARAKEEP_MEILI_MASTER_KEY`
- `KARAKEEP_OIDC_CLIENT_ID`
- `KARAKEEP_OIDC_CLIENT_SECRET`
- `KARAKEEP_OPENAI_API_KEY`

### n8n
- `N8N_DB_HOST`
- `N8N_DB_PASSWORD`

### your_spotify
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
