# vova-medcenter

Demo deployment for [`Zent7/vova-medcenter`](https://github.com/Zent7/vova-medcenter).

- Public URL: https://vova-medcenter.ravil.space
- Demo UI: https://vova-medcenter.ravil.space/demo/index.html
- Live frontend/backend image refs and `EXPECTED_REVISION` are managed in `compose.yaml` by the automated release PR flow from [`Zent7/vova-medcenter`](https://github.com/Zent7/vova-medcenter).
- Images are immutable GHCR artifacts; Komodo pulls digest-pinned image refs.

## Architecture

- `db`: PostgreSQL 16 for demo data.
- `backend`: FastAPI app. Runs Alembic migrations on start, then Uvicorn on `:8000`.
- `frontend`: nginx serving the Vite build and proxying `/api/` to `backend:8000`.
- `release-verifier`: stays healthy only after both running services report the expected embedded Git revision.

The stack never builds application images on the server. The application repository publishes both images to GHCR, and this compose file pins them to one full source commit. Overlay archives and dependencies on transient local images are intentionally unsupported.

The two GHCR packages must be public, or the Docker host must have a read-only GHCR login. A missing image or registry authorization error fails before the services are recreated. A stale running service fails the `release-verifier` healthcheck because its embedded revision does not match the compose revision.

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
./verify-deployment.sh <expected-revision-from-compose.yaml>
curl -sk -o /dev/null -w '%{http_code}\n' https://vova-medcenter.ravil.space/
curl -sk https://vova-medcenter.ravil.space/api/v1/health
curl -sk 'https://vova-medcenter.ravil.space/api/v1/clients?limit=1'
ssh 192.168.1.166 'docker logs traefik --tail 200 | grep -i vova-medcenter | tail -20'
```

`verify-deployment.sh` is the strict host-side post-deploy check. It compares the configured image references from the live containers with the expected GHCR references and then verifies both public build revisions.
