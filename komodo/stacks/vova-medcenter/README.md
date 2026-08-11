# vova-medcenter

Demo deployment for [`Zent7/vova-medcenter`](https://github.com/Zent7/vova-medcenter).

- Public URL: https://vova-medcenter.ravil.space
- Demo UI: https://vova-medcenter.ravil.space/demo/index.html
- Release manifest: [`release.json`](release.json)
- Canonical backend base: `vova-medcenter-backend:5f694c6-offline-overlay`
- Canonical frontend base: `vova-medcenter-frontend:5f694c6-offline-overlay`
- Canonical base source: `5f694c64f2e5127aabdee1adc1ca5c678325ef34`
- Current backend source: `de9fa8664c931305a10cce6ecd2d28d87d62b4b9`
- Current frontend source: `de9fa8664c931305a10cce6ecd2d28d87d62b4b9`

## Release model

The stack is offline at deploy time: it does not clone GitHub, access a registry, or run npm/pip. `generate_overlays.py` reads immutable Git objects from a local `Zent7/vova-medcenter` checkout and creates a cumulative delta from the canonical base. Every release therefore builds directly from the two canonical `5f694c6-offline-overlay` images, never from the previous release tag.

`release.json` is the machine-readable source of release identity. It records the full app revisions, cumulative payload hashes, generated archive checksums, image tags, and canonical bases. The frontend overlay also publishes the public subset as `/demo/release.json`.

The canonical base images must remain on the Komodo Docker host until the stack moves to immutable GHCR images. Pre-deploy verification fails before `DeployStack` when either base is missing or the Komodo API key lacks Inspect permission.

## Generate a release

Run from a checkout containing both repositories. The generator reads committed objects only; working-tree changes are ignored.

```bash
python komodo/stacks/vova-medcenter/generate_overlays.py \
  --app-repo /path/to/vova-medcenter \
  --backend-revision <full-backend-commit> \
  --frontend-revision <full-frontend-commit>
```

The generator:

- creates deterministic cumulative backend/frontend archives and deletion scripts;
- updates `compose.yaml` from `compose.yaml.in`;
- updates `release.json` and removes obsolete transient overlay files;
- refuses dependency or build-tool changes that cannot be represented by the canonical offline base;
- validates archive destinations before generating files.

Verify that committed generated files match their inputs:

```bash
python komodo/stacks/vova-medcenter/generate_overlays.py \
  --app-repo /path/to/vova-medcenter \
  --backend-revision <full-backend-commit> \
  --frontend-revision <full-frontend-commit> \
  --check
```

## Deployment verification

`DeployStack` success is necessary but not sufficient: it only confirms that Komodo accepted/completed the deploy action. It does not prove that the live containers use the declared images.

Both deployment workflows run the protected `.github/scripts/verify_vova_medcenter_live.py` script:

1. Before deployment, inspect both canonical base images on the stack server.
2. Require HTTP success and `.success == true` from `DeployStack`.
3. Inspect the live backend/frontend containers and compare image tag, image ID, running state, source revision label, and cumulative overlay hash label with `release.json`.
4. Poll the public health endpoint and `/demo/release.json` with cache busting.
5. Fail the workflow after five minutes if any expected and actual values differ.

The delegated auto-merge scope remains limited to `komodo/stacks/vova-medcenter/`. The verifier and workflows live outside that scope so a stack-only PR cannot change code executed with Komodo credentials.

## Rollback

Revert the release commit in homelab and deploy the reverted `compose.yaml`. The same live verification checks the reverted image tags and labels. Do not manually retag a transient Docker image: that recreates the state drift this release model is intended to prevent.

## Architecture

- `db`: PostgreSQL 16 with persistent demo data.
- `backend`: FastAPI; runs Alembic migrations on start, then Uvicorn on `:8000`.
- `frontend`: nginx serving the approved static demo and proxying `/api/` to backend.
- Traefik router: `vova-medcenter` on `websecure`, using `cloudflare` certificate resolver and `traefik_default` network.

After the first deployment, seed the legacy demo dataset if required:

```bash
ssh 192.168.1.166 \
  'docker exec vova-medcenter-backend curl -sS -X POST http://127.0.0.1:8000/api/v1/imports/demo-legacy'
```

The next infrastructure step is to publish immutable backend/frontend images to GHCR and reference their registry digests from Compose. That removes the remaining dependency on the two locally retained canonical base images.
