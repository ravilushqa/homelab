# HomeLab Documentation

- [Homelab Architecture Snapshot](architecture/00-homelab-architecture.md) — network, ingress, Proxmox, Kubernetes,
  Komodo, identity, storage, observability and deployment flows. Start here.
- [Network Diagram](diagrams/network/homelab-network.html) — standalone rendered topology (open the file in a browser)
- [Bootstrap](runbooks/bootstrap.md) — rebuild the Talos cluster from scratch
- [Troubleshooting](runbooks/troubleshooting.md) — where to look when something is down
- [ADRs](adr/README.md) — recorded architecture decisions

## Architecture at a glance

```mermaid
flowchart LR
  Users((Users)) -->|HTTPS| Router[UniFi Express<br/>443 forward]
  Router --> Traefik[Komodo Traefik<br/>192.168.1.166]
  Traefik -->|HTTP routes| Stacks[Komodo Docker stacks]
  Traefik -->|HTTP routes| Lan[HA, Proxmox, PBS]
  Traefik -->|SNI passthrough| Gateway[Cilium Gateway<br/>192.168.1.222]
  Gateway --> K8sApps[K8s apps]
  ArgoCD[ArgoCD] -. syncs k8s/ .-> K8sApps
  Komodo[Komodo sync + CI] -. deploys komodo/stacks/ .-> Stacks
  Terraform[Terraform] -. Proxmox VMs/LXCs, Talos .-> Gateway
```

## Keeping docs current

- Change docs in the same PR as the code they describe.
- The architecture snapshot lists its live sources ("Operational source of truth"); refresh it from there.
- Diagrams are Mermaid and render on GitHub. Use `<br/>` for line breaks in labels, never `\n`.
