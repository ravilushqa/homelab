# Audiobooks arr

Request an audiobook, get it from RuTracker over VLESS, and Audiobookshelf (in this stack too, see
[audiobookshelf.md](audiobookshelf.md)) picks it up.
Search a game in Questarr, the same qBittorrent downloads it, and the PC takes it from the SMB share.

```
Questarr (games.ravil.space) ──HTTPS──▶ IGDB (metadata, search)
     ├──search (Torznab)──▶ Prowlarr
     └──API (add torrent, category games)──▶ qBittorrent ──▶ /mnt/torrents/games ──SMB──▶ PC
ABR (abr.ravil.space) ──search/grab──▶ Prowlarr (prowlarr.ravil.space) ──HTTPS, home IP──▶ rutracker.org
                                             ├──indexer proxy──▶ FlareSolverr ──HTTPS, home IP──▶ Cloudflare-protected indexers
                                             └──API──▶ qBittorrent ──SOCKS5──▶ xray ──VLESS/Reality──▶ peers, tracker
/mnt/torrents/audiobooks ──ro bind──▶ Audiobookshelf (audiobooks.ravil.space, /downloads)
```

- **AudioBookRequest** (`markbeep/audiobookrequest`): search UI. Audible search or *Manual request*
  (Russian books are mostly not on Audible). Sends the request to Prowlarr.
- **Questarr** (`ghcr.io/doezer/questarr`, [repo](https://github.com/Doezer/Questarr)): game
  search UI at `games.ravil.space` (IGDB metadata). It pulls indexers from Prowlarr (*Sync from
  Prowlarr*, not a Prowlarr *App*), searches them through Prowlarr's Torznab endpoints and sends the
  release to qBittorrent's API with category `games`. Post-processing (import/move) is off by
  default and stays off, so it doesn't need the downloads folder. It is on `vpn` (to reach
  `qbittorrent:8080`) and on `traefik_default` (route, and internet for IGDB). State (SQLite) is in
  the `questarr` named volume, not on the SMB share. Credentials it stores (Prowlarr API key,
  qBittorrent password) are encrypted with `QUESTARR_CREDENTIALS_KEY`.
- **Prowlarr**: RuTracker indexer and the qBittorrent download client. It talks to RuTracker
  directly (only search and `.torrent` files), not through the VPN.
- **qBittorrent**: is only on the `vpn` network, which is `internal: true` (no gateway). Its only
  way out is `xray:1080`. Checked: a direct `curl` from the container fails, through the proxy it
  shows the VLESS exit IP. If xray or the VLESS server is down, torrents stall; they can't fall back
  to the home IP. The web UI is at `qbit.ravil.space` through xray's dokodemo-door inbound.
  The kill switch is enforced twice, see [Keeping the kill switch](#keeping-the-kill-switch).
- **xray**: VLESS (Reality, `xtls-rprx-vision`) client. Config is inline in `compose.yaml`; the
  server is from Komodo secrets `VLESS_HOST`, `VLESS_UUID`, `VLESS_SNI`, `VLESS_PBK`. For Trojan or
  Outline (shadowsocks) swap the `vless` outbound. The SOCKS port is open to every container on
  `traefik_default`.
- **Audiobookshelf** (`ghcr.io/advplyr/audiobookshelf`): the audiobook server at
  `audiobooks.ravil.space`, native OIDC (the mobile apps can't pass `oidc-auth`). It is only on
  `traefik_default`, not on `vpn`. Its healthcheck keeps it off Traefik until it is safe to be
  public. Setup, auth and recovery: [audiobookshelf.md](audiobookshelf.md).
- **FlareSolverr** (`ghcr.io/flaresolverr/flaresolverr`): solves Cloudflare challenges for Prowlarr
  indexers. It shares only the stack-local `indexer` network with Prowlarr (not `vpn`, not
  `traefik_default`), has no ports or route, and goes out directly from the home IP.

Prowlarr, qBittorrent and Questarr are behind the `oidc-auth` middleware (Pocket ID group `ravil`
only). Questarr has no OIDC or proxy-auth mode, so its own login (admin account) comes after Pocket ID.
ABR uses native OIDC instead (`ABR_APP__FORCE_LOGIN_TYPE=oidc`), so family members can request books:

- Pocket ID client `audiobookrequest`, callback `https://abr.ravil.space/auth/oidc`, allowed group `family`.
- ABR *Settings → Security*: well-known `https://pocketid.ravil.space/.well-known/openid-configuration`,
  scope `openid profile email groups`, username claim `preferred_username`, group claim `groups`,
  redirect scheme `https`.
- ABR roles come from Pocket ID group names: `admin` (ravilushqa), `trusted` (tanya, requests download
  without approval); anyone else in `family` is `untrusted`. ABR only accepts claims listed in Pocket
  ID's `claims_supported`, so custom claims don't work here.
- Backup login: `/login?backup=1` with the root user (Komodo secrets `ABR_ROOT_USERNAME`,
  `ABR_ROOT_PASSWORD`; also `/root/.abr-root-password` on the LXC).

Downloads live on `/mnt/torrents` (owned by `1000:1000`), a bind of
`/raid1/subvol-117-disk-0/torrents` on the Proxmox host. That is the fileserver's (LXC 117) raid1
subvol: the folder is on its SMB share as `torrents`, and it is not backed up (fileserver `mp0` has
`backup=0`). qBittorrent writes to local ZFS, not over CIFS; books keep seeding from where
Audiobookshelf reads them. The same qBittorrent also downloads games (category `games`), which the
PC takes from the SMB share.

## First deploy

The host dirs must exist (`create_host_path: false`). Create them and the LXC bind mount on the
Proxmox host (a bind mount needs root@pam, so Terraform only keeps it):

```bash
mkdir -p /raid1/subvol-117-disk-0/torrents/{incomplete,audiobooks,games}
chown -R 101000:101000 /raid1/subvol-117-disk-0/torrents   # 1000:1000 inside the LXCs
pct set 122 -mp2 /raid1/subvol-117-disk-0/torrents,mp=/mnt/torrents
```

Generated passwords are in root-only files on the LXC: `/root/.qbittorrent-password` (qBittorrent
user `admin`) and `/root/.abr-root-password` (ABR root `ravil`, only needed if the login type changes).

1. **qBittorrent** (temporary password in `docker logs`): set a password, then
   - *Connection → Proxy*: SOCKS5 `xray` `1080`, enable *Use proxy for peer connections* and
     *Use proxy for hostname lookups* (the `vpn` network has no external DNS).
   - *Downloads*: default path `/data/audiobooks`, incomplete path `/data/incomplete`; categories
     `audiobooks` → `/data/audiobooks` and `games` → `/data/games`.
   - Check: *Execution log* shows the VLESS exit IP as external IP, trackers show *Working*.
2. **Prowlarr**: add the qBittorrent client `qbittorrent:8080` (user `admin`), category
   `audiobooks`. Add the *RuTracker.org* indexer with your RuTracker login.
   - FlareSolverr is not wired up by compose; set it in Prowlarr
     ([docs](https://wiki.servarr.com/prowlarr/settings)): *Settings → Indexers → Indexer Proxies →
     FlareSolverr*, host `http://flaresolverr:8191`, tag `flaresolverr`. Add the same
     `flaresolverr` tag to each indexer that needs it. The proxy only applies to indexers with a
     matching tag (without tags it's disabled), and Prowlarr only uses it when it detects a
     Cloudflare challenge; it is not a general proxy for all requests.
3. **ABR**: create the root user on `/init`. *Settings → Prowlarr*: `http://prowlarr:9696` + the
   Prowlarr API key, pick the RuTracker indexer. *Settings → Download*: enable auto-download.
4. **Audiobookshelf**: see [audiobookshelf.md](audiobookshelf.md). The library has the folders
   `/audiobooks` and `/downloads`; the hourly scan picks up finished downloads.

### Questarr (games)

Komodo secrets (stack `audiobooks-arr`): `QUESTARR_IGDB_CLIENT_ID`, `QUESTARR_IGDB_CLIENT_SECRET`,
`QUESTARR_CREDENTIALS_KEY` (`openssl rand -hex 32`; keep it, the stored credentials can't be read
without it).

1. **IGDB credentials**: [Twitch developer console](https://dev.twitch.tv/console) (2FA must be on)
   → *Applications → Register Your Application*: name `questarr`, OAuth redirect
   `http://localhost`, category *Application Integration*, client type *Confidential*. The Client ID
   and a generated Client Secret go into the two `QUESTARR_IGDB_*` secrets.
2. **Pocket ID**: nothing new; `games.ravil.space` uses the shared `oidc-auth` middleware (group
   `ravil`).
3. **Questarr** (`https://games.ravil.space`): create the admin account on first open.
4. **Prowlarr**: Questarr reads indexers through Prowlarr's API, so there is no *Settings → Apps*
   entry to add. Make sure the game indexers (e.g. RuTracker) have the *PC* (`4000`) / *PC Games*
   (`4050`) categories, and copy the API key from *Settings → General*.
   In Questarr *Indexers → Sync from Prowlarr*: URL `http://prowlarr:9696`, the API key, categories
   `4000,4050`, enable *Allow insecure LAN connection* (plain HTTP inside the stack), *Sync*.
5. **qBittorrent**: category `games` → `/data/games` must exist (see step 1 above). In Questarr
   *Downloaders → Add → qBittorrent*: host `qbittorrent`, port `8080`, no SSL, user `admin` + the
   password from `/root/.qbittorrent-password`, category `games`. Leave post-processing off.
6. Check: search a game, grab a release, it shows up in qBittorrent under `games` and later in
   `\\fileserver\torrents\games` on the PC.

## Keeping the kill switch

- **Before merge and deploy**: `.github/scripts/vpn_gate_check.py` fails if `qbittorrent` is on any
  network but `vpn`, gets `ports`/`network_mode`/`cap_add`/`privileged`/`devices`, loses its guard
  entrypoint, if `vpn` stops being `internal`, an unlisted service joins `vpn`, or xray routes the
  socks inbound anywhere but VLESS. It runs on every PR (`VPN Gate` workflow) and in
  `komodo-deploy.yaml` before this stack is deployed. Make `VPN Gate` a required check on `main`
  (branch protection), or a failing PR can still be merged.
- **At container start**: `vpn-guard.sh` is qBittorrent's entrypoint. It exits (no qBittorrent) if the
  container has an IPv4/IPv6 default route or a direct connection to `1.1.1.1` works, then
  `exec /init`. Docker only adds a default route for a non-internal network or `network_mode: host`.
  It runs only at start: a `docker network connect` to a running container is not caught.
- Adding a service to `vpn`: check it can't forward traffic out directly (no proxy, no routing),
  then add it to `_VPN_ALLOWED_SERVICES` in the gate.

## Limits

- No incoming port through VLESS: seeding only reaches peers that accept connections. Watch the
  RuTracker ratio.
- Questarr doesn't import or rename games here (post-processing off); installers stay in the
  torrent's folder under `games` and keep seeding. Delete the torrent in qBittorrent to free space.
- ABR doesn't rename or import. Book folders are named like the torrent; fix metadata with *Match*
  in Audiobookshelf.
