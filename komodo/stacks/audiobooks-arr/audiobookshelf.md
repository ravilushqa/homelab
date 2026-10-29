# Audiobookshelf

Audiobook server at **https://audiobooks.ravil.space**, the `audiobookshelf` service of the
`audiobooks-arr` stack (see [README](README.md)). It runs on the Komodo host (192.168.1.166, LXC 122)
and reads a read-only SMB library.

- Image: `ghcr.io/advplyr/audiobookshelf:2.37.2` (pinned; bump manually)
- Auth: Audiobookshelf's **native OIDC** against Pocket ID; password login is disabled. There is
  no Traefik `oidc-auth` middleware, because the mobile apps can't pass a browser redirect in front
  of the API. Native OIDC works in the apps (`audiobookshelf://oauth` mobile redirect).
- State: named volumes `audiobookshelf_config` (SQLite DB) and `audiobookshelf_metadata` (covers,
  cache, item metadata). These live on local LXC disk. Never put them on SMB, because SQLite over
  CIFS corrupts. They were created by the former standalone `audiobookshelf` stack and are
  `external` here, so a missing volume fails the deploy instead of starting with an empty DB.
- Media: host `/mnt/audiobooks` (existing read-only CIFS bind on the LXC) → `/audiobooks:ro`.
  Compose uses `create_host_path: false`, so the container fails to start if the path is missing.
- Downloads: host `/mnt/torrents/audiobooks` (the fileserver's raid1 subvol, filled by
  qBittorrent in this stack) → `/downloads:ro`, a second folder of the same library.

## Healthcheck gates the public route

Traefik's Docker provider does not route to a container that is `starting` or `unhealthy`. The
healthcheck is only green when it is safe to be on the internet:

1. `/audiobooks` is the CIFS share `//192.168.1.2/proxmox` (from `/proc/self/mountinfo`) and is
   readable. If the share is not mounted on the LXC, Docker binds the empty local directory
   instead (fs type `zfs`); if the NAS drops later, reads fail or time out.
2. `GET /status` reports `isInit: true`. A fresh `config` volume lets **whoever reaches it
   first** create the root user, so it stays private until root exists.
3. `GET /status` reports `authMethods` = `["openid"]` only. Audiobookshelf falls back to
   `local` when the OIDC settings are incomplete, which also keeps it private.

So a lost or recreated `config` volume, a dropped share, or a misconfigured auth setting takes
the route down and shows the container as unhealthy in Komodo, with no manual toggle. The
healthcheck does not stop the library scan: while the share is down, the next scheduled scan
marks items missing, and the first scan after it is back restores them.

## First deploy (and after losing the `config` volume)

The volumes are `external`, so create them first on the LXC:
`docker volume create audiobookshelf_config && docker volume create audiobookshelf_metadata`.
The container is unhealthy and unrouted until steps 2–4 are done. Do them over the private
network: get the container IP on `traefik_default` (`docker inspect`) and use
`http://<container-ip>` from the LXC, or an SSH tunnel to it for the web UI.

1. Deploy the `audiobooks-arr` stack (merge to `main`, or **Deploy** in Komodo).
2. Initialize root: generate a random password into a root-owned `0600` file on the host (don't
   echo, log or commit it), then
   `POST /init` with body `{"newRoot":{"username":"root","password":"<from file>"}}`.
3. In Pocket ID create an OIDC client `audiobookshelf` with callback URLs
   `https://audiobooks.ravil.space/auth/openid/callback` and
   `https://audiobooks.ravil.space/auth/openid/mobile-redirect`.
4. Log in as root. In **Settings → Users** rename `root` to your Pocket ID username: Pocket ID
   sends it as `preferred_username`, and that is what username matching compares. Then
   **Settings → Authentication**:
   - Enable **OpenID Connect**: issuer `https://pocketid.ravil.space` → **Auto-populate**,
     client ID/secret from step 3, allowed mobile redirect URI `audiobookshelf://oauth`,
     *Match existing users by* `username` (the default, *Do not match*, only finds users
     already linked by `sub`, so the first login fails with `User not found`). Leave *Group claim* empty unless Pocket ID sends
     `groups` with `admin`/`user`/`guest`; a configured claim that is missing denies the login.
   - Keep **Username/Password** enabled and log in once through OIDC in a private window. A
     plain `Unauthorized` means no user matched or the claims were rejected; the reason is in
     `docker logs <container> 2>&1 | grep OidcAuth`.
   - Only then disable **Username/Password**. `GET /status` now returns `"authMethods":["openid"]` and the
     container turns healthy within ~30s; Traefik picks up the route.
5. Create the library (UI or `POST /api/libraries`):
   `{"name":"Audiobooks","mediaType":"book","folders":[{"fullPath":"/audiobooks"},{"fullPath":"/downloads"}],"settings":{"disableWatcher":true,"autoScanCronExpression":"0 * * * *"}}`.

Recovery if OIDC breaks and you are locked out: password login can only be re-enabled in the
DB. Stop the `audiobookshelf` service (`docker compose -p audiobooks-arr stop audiobookshelf`), set `authActiveAuthMethods` back to `["local","openid"]` in the JSON `value`
of the `settings` row with key `server-settings` in `absdatabase.sqlite` (in the `config`
volume), start it, fix OIDC
privately (the healthcheck keeps it off Traefik meanwhile), then disable password login again.

The schemas above were checked against the v2.37.2 source (`server/Server.js`,
`server/Auth.js`, `server/objects/settings/ServerSettings.js`,
`server/controllers/LibraryController.js`).

## Adding books

Copy files onto the share from Windows: `\\192.168.1.2\proxmox\audiobooks`. Use one folder per book:

```
Author Name/
  Book Title/
    01 - Chapter.mp3
    cover.jpg
```

The library rescans hourly (library scan cron `0 * * * *`). A full scan walks the whole share
over CIFS, so don't make it more frequent. The filesystem watcher is disabled because inotify does
not work over CIFS. Use **Scan** in the library menu to pick up new books right away.

## Limitations (read-only media)

- No uploads through the web UI and no podcast downloads.
- No edits to media files (embedded tags, chapters, renames).
- Covers and metadata edits work, but they are stored in `/metadata`, not next to the media
  (server defaults `storeCoverWithItem` and `storeMetadataWithItem` are `false`; leave them off).
- LXC 122 backups (vzdump) exclude bind-mount contents, so the audiobooks are **not** backed up
  with the LXC. The `config` and `metadata` volumes are on the LXC disk and are included.

## Migration from the standalone stack

Audiobookshelf was its own `audiobookshelf` stack. On the merge that moved it here,
`komodo-deploy.yaml` sees `audiobookshelf` gone from `komodo/stacks.toml` and runs `DestroyStack`
on it (`compose down`, volumes kept) and deletes the Komodo resource **before** deploying
`audiobooks-arr`. Two containers on the same SQLite volume, or two `audiobookshelf` Traefik routers,
must never run at once. If the destroy fails, nothing is deployed; run **Destroy** on the old
stack in Komodo and re-run the workflow.
