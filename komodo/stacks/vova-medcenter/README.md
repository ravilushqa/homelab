# vova-medcenter

Demo deployment for [`Zent7/vova-medcenter`](https://github.com/Zent7/vova-medcenter).

- Public URL: https://vova-medcenter.ravil.space
- Demo UI: https://vova-medcenter.ravil.space/demo/index.html
- Upstream backend fix: `1807cea5223362c15cf80be887b61eb143ecd0a5`
- Upstream frontend fix: `3018974987ce4c9a674b7d5785a56fcbe48cc945`
- Frontend image: `3018974-lmk-ambulatory-live-v2`, built on cached image `59f2d0b-persist-deleted-doctors`
- Backend image: `1807cea-ambulatory-runtimepath-v3`, built on cached image `59f2d0b-persist-deleted-doctors`
- Source and template overlays are bundled in this stack directory; the build performs no GitHub, registry, npm, or pip download
- Last redeploy request: 2026-08-11 (install the LMK ambulatory generator at the actual `/app/app` runtime path)

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
