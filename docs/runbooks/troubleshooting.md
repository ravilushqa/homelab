# Runbook: Troubleshooting

Start from the request path: DNS → router → Komodo Traefik → backend (Docker stack, LAN host, or K8s gateway).

## A public hostname is down

```bash
dig +short <name>.ravil.space            # LAN via Pi-hole: 192.168.1.166; public: WAN IP
ssh root@192.168.1.166 'docker logs --tail 100 traefik 2>&1 | grep <name>'
```

Traefik access logs are the source of truth, not the `curl` status code.

- **No log line:** DNS or the router. Check the Cloudflare record (`cloudflare-ddns` in namespace `ddns` keeps it on the
  WAN IP) and the UniFi 443 port forward to `192.168.1.166`.
- **K8s hostname:** it must be in the `k8s` TCP router in `komodo/stacks/traefik/dynamic/external.yml`; then check
  `kubectl get gateway,httproute -A` and `kubectl get certificates -A`.
- **Docker stack:** check the stack labels and `docker ps` on the Komodo host; Komodo UI shows deploy logs.
- **LAN backend (HA, Proxmox, PBS):** check the `http` section of `dynamic/external.yml` and that the backend IP answers.
- **401/redirect loops:** PocketID (`pocketid` stack) and the Traefik `oidc-auth` middleware.

## DNS

- Pi-hole is LXC 110 (`192.168.1.3`). Inside it, use `/usr/local/bin/pihole` (`pct exec 110 -- pihole` fails silently).
- Flush its cache: `ssh root@192.168.1.100 'pct exec 110 -- /usr/local/bin/pihole reloaddns'`.
- A LAN client hitting the public IP shows up in Traefik logs as `192.168.1.1` (hairpin): it is not using Pi-hole.

## Komodo stacks

- A merged change did not deploy: check the `Komodo Deploy` workflow run; it syncs `komodo/stacks.toml`, then deploys
  changed stacks one at a time.
- Host CPU pegged after a Komodo restart: a stack probably has `deploy = true` in `stacks.toml`. Remove it.
- Secrets are Komodo Variables (`[[NAME]]` in compose); a missing one leaves the literal placeholder in the container env.

## Kubernetes

```bash
kubectl get nodes
kubectl get pods -A | grep -v -E 'Running|Completed'
kubectl -n argocd get applications
talosctl -n 192.168.1.210 dmesg | tail      # node-level issues
talosctl -n 192.168.1.210 logs kubelet
```

- ArgoCD app `OutOfSync` that will not converge: `kubectl -n argocd get application <name> -o yaml`; restart with
  `make argocd-restart`.
- Pending PVC: Proxmox CSI (`kubectl -n csi-proxmox logs deploy/proxmox-csi-plugin-controller`) and free space on the
  datastore.
- SealedSecret not decrypting: the controller key must be the one in `terraform/modules/sealed-secrets/certs/`.

## Proxmox and backups

```bash
ssh root@192.168.1.100 'pct list; qm list; pvesm status'
```

PBS is LXC 103 (`pbs.ravil.space`), with the `pbs-s3-blackbaze` datastore.

## Logs and metrics

- Docker: `dozzle.ravil.space`; Kubernetes: `dozzle.k8s.ravil.space`
- Metrics and logs: Grafana in the `grafana-lgtm` stack (`grafana.ravil.space`); Proxmox metrics via
  `prometheus-pve-exporter` (LXC 112)
