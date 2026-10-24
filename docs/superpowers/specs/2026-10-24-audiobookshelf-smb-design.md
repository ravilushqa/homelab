# Audiobookshelf on Komodo (read-only SMB library) — Approved Design

Date: 2026-10-24 · Status: approved by user · Branch: `feat/audiobookshelf-smb`

## Scope

One new Docker Compose stack `audiobookshelf` on the Komodo host (192.168.1.166, Debian
unprivileged LXC 122). No changes to the host mount, Proxmox config, permissions, global
resource sync, or any existing stack. No merge to `main` for this deployment.

## Design

| Concern | Decision |
|---|---|
| Image | `ghcr.io/advplyr/audiobookshelf:2.37.2` (latest release, 2026-10-23) |
| Hostname | `audiobooks.ravil.space`, Traefik `websecure` + `cloudflare` certresolver, network `traefik_default`, backend port `80` |
| Auth | Native Audiobookshelf auth. **No** Traefik `oidc-auth` middleware (mobile apps) |
| Exposure toggle | `traefik.enable=${AUDIOBOOKSHELF_TRAEFIK_ENABLED:-true}`; first deploy with `false` |
| State | Local named volumes `config:/config`, `metadata:/metadata`. SQLite never on SMB |
| Media | Host `/mnt/audiobooks` (existing RO CIFS bind) → `/audiobooks`, long bind syntax, `read_only: true`, `bind.create_host_path: false` |
| Runtime | `restart: unless-stopped`, no `container_name`, `TZ=Europe/Berlin` |
| Healthcheck | `node -e` GET `http://127.0.0.1/healthcheck` → 200 |
| Komodo resource | `[[stack]] audiobookshelf` in `komodo/stacks.toml`, server `Local`, repo `ravilushqa/homelab`, `branch = "feat/audiobookshelf-smb"`, `auto_update = false`, `deploy = true`. Hermes creates it via API, not via the global sync |

### Source-verified facts (v2.37.2)

- `server/Server.js`: `router.get('/healthcheck', (req, res) => res.sendStatus(200))`. Requests
  without the `ROUTER_BASE_PATH` prefix (default `/audiobookshelf`) are rewritten onto it, so
  `/healthcheck` works at the root.
- `Dockerfile`: the runtime image is `node:24-alpine` with only `tzdata ffmpeg tini` added (no
  curl). `PORT=80`, `CONFIG_PATH=/config`, `METADATA_PATH=/metadata`.
- `POST /init` with body `{"newRoot":{"username","password"}}` works only while no root user exists.
- `POST /api/libraries` with `name`, `folders:[{fullPath}]`, `mediaType:"book"`, and
  `settings.disableWatcher` (bool) / `settings.autoScanCronExpression` (string). `CronManager`
  schedules scans from `autoScanCronExpression`, and `Watcher.js` skips libraries with `disableWatcher`.
- `ServerSettings`: `storeCoverWithItem=false` and `storeMetadataWithItem=false` by default, so
  covers and metadata stay in `/metadata`, not alongside the media.

### Bootstrap (Hermes, server-side)

1. Deploy with `AUDIOBOOKSHELF_TRAEFIK_ENABLED=false`, which leaves the service unreachable publicly.
2. Generate a random root password into a root-owned `0600` file on the server. Never log or commit it.
3. `POST /init` over the container's private IP.
4. Create library `/audiobooks`: `disableWatcher: true`, `autoScanCronExpression: "*/5 * * * *"`.
5. Redeploy with `AUDIOBOOKSHELF_TRAEFIK_ENABLED=true`.

### Known limitations

Read-only media: no UI uploads, no metadata or cover writes into media folders, no podcasts.
Layout: `Author/Book/files`. Windows path: `\\192.168.1.2\proxmox\audiobooks`. LXC backups do not
include the bind-mount contents.

### Out of scope

Multi-user or subscriber features, OIDC/SSO, podcasts, a writable upload mount, any custom scan daemon.

### Future migration to `main`

Only after the user explicitly approves a merge: merge the PR, switch the resource's
`branch = "main"` (and set `auto_update = true` if wanted), then let the global sync manage it.
