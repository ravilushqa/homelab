# vova-medcenter

Demo deployment for [`Zent7/vova-medcenter`](https://github.com/Zent7/vova-medcenter).

- Public URL: https://vova-medcenter.ravil.space
- Demo UI: https://vova-medcenter.ravil.space/demo/index.html
- Upstream commit: `12a38bb66133646c45014bae5989ebb9bc8c6b86`
- Frontend image: `12a38bb-offline-overlay`, built on cached image `980868768daa14eb5ee4afa97e3a821de179dcc8`
- Backend image: `12a38bb-offline-overlay`, built on cached image `3715c1942ca76f96be25a40763db4c6a41ca613d`
- Source and template overlays are bundled in this stack directory; the build performs no GitHub, registry, npm, or pip download
- Last redeploy request: 2026-08-02 (086 sex rules and Word 2019 opening, with current client import)

## Architecture

- `db`: PostgreSQL 16 for demo data.
- `backend`: FastAPI app. Runs Alembic migrations on start, then Uvicorn on `:8000`.
- `frontend`: nginx serving the Vite build and proxying `/api/` to `backend:8000`.

The upstream repo currently ships only a local Windows/demo compose for Postgres, so this stack uses `dockerfile_inline` to keep the Komodo deployment self-contained while building directly from the pinned upstream Git commit.

## Traefik

The frontend is exposed through the standard Komodo Traefik setup:

- router: `vova-medcenter`
- entrypoint: `websecure`
- cert resolver: `cloudflare`
- Docker network: `traefik_default`

No OIDC middleware is attached because this is an external demo.

## First-run demo data import

After first deploy, seed the demo legacy dataset:

```bash
ssh 192.168.1.166 \
  'docker exec vova-medcenter-backend curl -sS -X POST http://127.0.0.1:8000/api/v1/imports/demo-legacy'
```

Expected response shape:

```json
{"source":"/frontend/public/demo/legacy-data.js","created":12029,"updated":2,"total":12032,"imported":12031}
```

## Verification

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://vova-medcenter.ravil.space/
curl -sk https://vova-medcenter.ravil.space/api/v1/health
curl -sk 'https://vova-medcenter.ravil.space/api/v1/clients?limit=1'
ssh 192.168.1.166 'docker logs traefik --tail 200 | grep -i vova-medcenter | tail -20'
```
