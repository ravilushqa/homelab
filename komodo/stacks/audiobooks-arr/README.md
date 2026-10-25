# Audiobooks arr

Request an audiobook, get it from RuTracker over VLESS, and Audiobookshelf picks it up.

```
ABR (abr.ravil.space) ──search/grab──▶ Prowlarr (prowlarr.ravil.space) ──HTTPS, home IP──▶ rutracker.org
                                             └──API──▶ qBittorrent ──SOCKS5──▶ xray ──VLESS/Reality──▶ peers, tracker
/mnt/raid1/torrents/audiobooks ──ro bind──▶ Audiobookshelf (/downloads)
```

- **AudioBookRequest** (`markbeep/audiobookrequest`): search UI. Audible search or *Manual request*
  (Russian books are mostly not on Audible). Sends the request to Prowlarr.
- **Prowlarr**: RuTracker indexer and the qBittorrent download client. It talks to RuTracker
  directly (only search and `.torrent` files), not through the VPN.
- **qBittorrent**: is only on the `vpn` network, which is `internal: true` (no gateway). Its only
  way out is `xray:1080`. If xray or the VLESS server is down, torrents stall; they can't fall back
  to the home IP. The web UI is at `qbit.ravil.space` through xray's dokodemo-door inbound.
- **xray**: VLESS (Reality, `xtls-rprx-vision`) client. Config is inline in `compose.yaml`; the
  server is from Komodo secrets `VLESS_HOST`, `VLESS_UUID`, `VLESS_SNI`, `VLESS_PBK`. For Trojan or
  Outline (shadowsocks) swap the `vless` outbound. The SOCKS port is open to every container on
  `traefik_default`.

All UIs are behind the `oidc-auth` middleware; ABR's own login is off
(`ABR_APP__FORCE_LOGIN_TYPE=none`).

Downloads live on local RAID (`/mnt/raid1/torrents`, owned by `1000:1000`), not on the SMB share,
so books keep seeding from where Audiobookshelf reads them.

## First deploy

The host dirs must exist (`create_host_path: false`):
`mkdir -p /mnt/raid1/torrents/{incomplete,audiobooks} && chown -R 1000:1000 /mnt/raid1/torrents`.

1. **qBittorrent** (`qbit.ravil.space`, temporary password in `docker logs`): set a password.
   - *Connection → Proxy*: SOCKS5 `xray` `1080`, enable *Use proxy for peer connections* and
     *Use proxy for hostname lookups* (the `vpn` network has no external DNS).
   - *Downloads*: default path `/data/audiobooks`, incomplete path `/data/incomplete`.
   - Check: *Tools → Execution log* shows the VLESS exit IP as external IP, trackers show *Working*.
2. **Prowlarr**: add the *RuTracker.org* indexer (login + password), add qBittorrent client
   `qbittorrent:8080` with the password from step 1, category `audiobooks`. Copy the API key.
3. **ABR**: *Settings → Download*: Prowlarr `http://prowlarr:9696` + API key, enable auto-download,
   pick the RuTracker indexer.
4. **Audiobookshelf**: *Libraries → Audiobooks → Edit*: add folder `/downloads`. The hourly scan
   picks up finished downloads.

## Limits

- No incoming port through VLESS: seeding only reaches peers that accept connections. Watch the
  RuTracker ratio.
- ABR doesn't rename or import. Book folders are named like the torrent; fix metadata with *Match*
  in Audiobookshelf.
