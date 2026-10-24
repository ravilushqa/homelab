# Audiobookshelf

Audiobook server at **https://audiobooks.ravil.space**. It runs on the Komodo host
(192.168.1.166, LXC 122) and reads a read-only SMB library.

- Image: `ghcr.io/advplyr/audiobookshelf:2.37.2` (pinned; bump manually)
- Auth: native Audiobookshelf login. There is **no** Traefik `oidc-auth` middleware, because the
  mobile apps can't complete a browser OIDC redirect in front of the API.
- State: named volumes `config` (SQLite DB) and `metadata` (covers, cache, item metadata). These
  live on local LXC disk. Never put them on SMB, because SQLite over CIFS corrupts.
- Media: host `/mnt/audiobooks` (existing read-only CIFS bind on the LXC) → `/audiobooks:ro`.
  Compose uses `create_host_path: false`. That only makes the container fail when the
  `/mnt/audiobooks` **path does not exist**, and stops Docker from creating it. If the directory
  exists but the CIFS share is **not mounted**, Compose binds the empty directory and
  Audiobookshelf sees an empty library.
- Mount guard: the Komodo resource has a `pre_deploy` command (`shell_mode = true`, runs in the
  run directory) that aborts the deploy unless the share is mounted, read-only and readable.
  - **Why it goes through Docker:** Komodo Periphery runs in its own container. It has
    `/var/run/docker.sock` and `/etc/komodo` bind-mounted, but not `/mnt/audiobooks`. A
    `findmnt`/`ls` in the pre_deploy shell would inspect Periphery's mount namespace, not the
    LXC's, and fails even when the share is fine. Bind sources in `compose.yaml` are resolved by
    the **host Docker daemon**, so the guard asks that same daemon.
  - **What it runs:** a throwaway probe container
    (`docker run --rm --network none --pull=missing --read-only`) from the pinned
    Audiobookshelf image, with `--entrypoint node` and
    `--mount type=bind,src=/mnt/audiobooks,dst=/audiobooks,readonly`. Unlike `-v`, `--mount`
    fails instead of creating a missing source path. An inline Node script reads
    `/proc/self/mountinfo` and requires exactly one `/audiobooks` entry with fs type `cifs`,
    source `//192.168.1.2/proxmox`, share root `/audiobooks`, and `ro` in the mount options. It
    then calls `readdirSync("/audiobooks")` to prove the share is readable. Filenames are
    discarded, not printed.
  - **What it rejects:** a plain directory left behind when the share is unmounted (fs type is
    then the LXC's disk, e.g. `zfs`), a wrong share/root/type, a read-write bind, an unreadable
    share, or a missing `/mnt/audiobooks` (Docker exits ≥125).
  - It writes nothing to the media and changes nothing on the host. The only side effect is
    the probe container, which `--rm` removes. If the guard fails, fix the mount on the
    LXC/Proxmox side and redeploy. It does not protect a running container if the share drops
    later.

## Adding books

Copy files onto the share from Windows: `\\192.168.1.2\proxmox\audiobooks`. Use one folder per book:

```
Author Name/
  Book Title/
    01 - Chapter.mp3
    cover.jpg
```

The library rescans every 5 minutes (library scan cron `*/5 * * * *`). The filesystem watcher is
disabled because inotify does not work over CIFS. Use **Scan** in the library menu to rescan
right away.

## Limitations (read-only media)

- No uploads through the web UI and no podcast downloads.
- No edits to media files (embedded tags, chapters, renames).
- Covers and metadata edits work, but they are stored in `/metadata`, not next to the media
  (server defaults `storeCoverWithItem` and `storeMetadataWithItem` are `false`; leave them off).
- LXC 122 backups (vzdump) exclude bind-mount contents, so the audiobooks are **not** backed up
  with the LXC. The `config` and `metadata` volumes are on the LXC disk and are included.
- Single-instance setup only. No multi-user or subscriber provisioning is configured here.

## First deploy: bootstrap (not public until root exists)

A fresh Audiobookshelf lets **whoever reaches it first** create the root user. Keep it off the
public router until root is initialized:

1. Create or deploy the Komodo stack with the environment variable
   `AUDIOBOOKSHELF_TRAEFIK_ENABLED=false`. This sets the `traefik.enable=false` label, so Traefik
   creates no route.
2. On the host, generate a random root password into a root-owned `0600` file. Do not echo it,
   log it, or commit it.
3. Find the container's private IP on `traefik_default` and initialize root over plain HTTP from
   the host:
   `POST http://<container-ip>/init` with body `{"newRoot":{"username":"root","password":"<from file>"}}`.
   `GET /status` then returns `"isInit": true`.
4. Log in (`POST /login`), then create the library:
   `POST /api/libraries` with body
   `{"name":"Audiobooks","mediaType":"book","folders":[{"fullPath":"/audiobooks"}],"settings":{"disableWatcher":true,"autoScanCronExpression":"*/5 * * * *"}}`.
5. Set `AUDIOBOOKSHELF_TRAEFIK_ENABLED=true` (or remove it; the default is `true`) and redeploy.

The schemas above were checked against the v2.37.2 source (`server/Server.js`,
`server/controllers/LibraryController.js`, `server/models/Library.js`). The container
healthcheck calls `GET /healthcheck` using `node`, because the image ships no curl.

## Komodo resource and moving to `main`

The `audiobookshelf` entry in `komodo/stacks.toml` sets `branch = "feat/audiobookshelf-smb"`, because
this deployment is not merged. That `stack.config.branch` only selects which branch Komodo clones
for this stack's compose files. The global resource sync (`komodo/sync.toml`) reads `stacks.toml`
from its own branch (`main`), independently, so it doesn't see this entry until merge. The resource
is created through the Komodo API instead.

`auto_update = false` only turns off Komodo's polling for newer images. It does not block
deploys: a Komodo webhook, a resource sync with `deploy = true`, or a manual deploy can still
redeploy the stack.

After a merge is **explicitly approved**:

1. In the same PR (or right after it), change the entry to `branch = "main"`. Optionally set
   `auto_update = true` to match the other stacks.
2. Merge. The `homelab-stacks` sync then adopts the existing `audiobookshelf` resource by name.
3. Do not put `AUDIOBOOKSHELF_TRAEFIK_ENABLED` in the TOML. The default `true` is the
   steady state.
