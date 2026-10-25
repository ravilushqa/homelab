# Audiobooks arr

Request an audiobook, get it from RuTracker over VLESS, and Audiobookshelf picks it up.

```
ABR (abr.ravil.space) ──search/grab──▶ Prowlarr (prowlarr.ravil.space) ──HTTPS, home IP──▶ rutracker.org
                                             ├──indexer proxy──▶ FlareSolverr ──HTTPS, home IP──▶ Cloudflare-protected indexers
                                             └──API──▶ qBittorrent ──SOCKS5──▶ xray ──VLESS/Reality──▶ peers, tracker
/mnt/raid1/torrents/audiobooks ──ro bind──▶ Audiobookshelf (/downloads)
```

- **AudioBookRequest** (`markbeep/audiobookrequest`): search UI. Audible search or *Manual request*
  (Russian books are mostly not on Audible). Sends the request to Prowlarr.
- **Prowlarr**: RuTracker indexer and the qBittorrent download client. It talks to RuTracker
  directly (only search and `.torrent` files), not through the VPN.
- **qBittorrent**: is only on the `vpn` network, which is `internal: true` (no gateway). Its only
  way out is `xray:1080`. Checked: a direct `curl` from the container fails, through the proxy it
  shows the VLESS exit IP. If xray or the VLESS server is down, torrents stall; they can't fall back
  to the home IP. The web UI is at `qbit.ravil.space` through xray's dokodemo-door inbound.
- **xray**: VLESS (Reality, `xtls-rprx-vision`) client. Config is inline in `compose.yaml`; the
  server is from Komodo secrets `VLESS_HOST`, `VLESS_UUID`, `VLESS_SNI`, `VLESS_PBK`. For Trojan or
  Outline (shadowsocks) swap the `vless` outbound. The SOCKS port is open to every container on
  `traefik_default`.
- **FlareSolverr** (`ghcr.io/flaresolverr/flaresolverr`): solves Cloudflare challenges for Prowlarr
  indexers. It shares only the stack-local `indexer` network with Prowlarr (not `vpn`, not
  `traefik_default`), has no ports or route, and goes out directly from the home IP.

All UIs are behind the `oidc-auth` middleware (Pocket ID group `ravil` only). ABR is moving to
its own OIDC client (`audiobookrequest`, group `family`) so family members can request books;
until that is done its login type is `none` (stored in its DB) behind the middleware.

Downloads live on local RAID (`/mnt/raid1/torrents`, owned by `1000:1000`), not on the SMB share,
so books keep seeding from where Audiobookshelf reads them.

## First deploy

The host dirs must exist (`create_host_path: false`). `/mnt/raid1` is not writable by the LXC
root, so create them on the Proxmox host:
`mkdir -p /raid1/subvol-122-disk-1/torrents/{incomplete,audiobooks} && chown -R 101000:101000 /raid1/subvol-122-disk-1/torrents`
(`1000:1000` inside the LXC).

Generated passwords are in root-only files on the LXC: `/root/.qbittorrent-password` (qBittorrent
user `admin`) and `/root/.abr-root-password` (ABR root `ravil`, only needed if the login type changes).

1. **qBittorrent** (temporary password in `docker logs`): set a password, then
   - *Connection → Proxy*: SOCKS5 `xray` `1080`, enable *Use proxy for peer connections* and
     *Use proxy for hostname lookups* (the `vpn` network has no external DNS).
   - *Downloads*: default path `/data/audiobooks`, incomplete path `/data/incomplete`; category
     `audiobooks` → `/data/audiobooks`.
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
4. **Audiobookshelf**: *Libraries → Audiobooks → Edit*: add folder `/downloads`. The hourly scan
   picks up finished downloads.

## Limits

- No incoming port through VLESS: seeding only reaches peers that accept connections. Watch the
  RuTracker ratio.
- ABR doesn't rename or import. Book folders are named like the torrent; fix metadata with *Match*
  in Audiobookshelf.
