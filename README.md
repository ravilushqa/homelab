# HomeLab Project 🏠💻

This repository represents my **HomeLab setup**, showcasing an integration of various modern technologies for managing a
Kubernetes-based infrastructure. It includes configurations for virtualization, networking, storage, and application
deployment.

---

## Core Technologies 🛠️
- [Proxmox](https://www.proxmox.com/) Server management and virtualization.
- [Terraform](https://www.terraform.io/) Infrastructure as Code.
- [Talos Linux](https://www.talos.dev/) Kubernetes OS.
- [Cilium](https://cilium.io/) Network security and observability.
- [Traefik](https://traefik.io/) Edge Router due to its simplicity and TLS passthrough capabilities and my router limitations.
- [Cert-Manager](https://cert-manager.io/) Certificate management.
- [Proxmox CSI](https://github.com/sergelogvinov/proxmox-csi-plugin) Storage provisioning.
- [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) Encrypted secrets management, which is safe to store in Git.
- [Gateway API](https://gateway-api.sigs.k8s.io/) Next generation of Kubernetes Ingress.
- [Grafana Cloud](https://grafana.com/) Monitoring and observability of the cluster.
- [ArgoCD](https://argo-cd.readthedocs.io/) GitOps continuous delivery tool for declarative Kubernetes management.
- [Komodo](https://komo.do/) Docker stack management — deploys compose stacks from the `stacks/` directory.

---

## Applications 📦

### Running in Kubernetes (`k8s/apps/internal/`)
- [Glance](https://github.com/glanceapp/glance) - Personal dashboard.
- [Dozzle](https://dozzle.dev/) - Real-time container log viewer.
- [IT Tools](https://github.com/CorentinTh/it-tools) - Collection of IT utility tools.
- [iSponsorBlockTV](https://github.com/dmunozv04/iSponsorBlockTV) - YouTube sponsor block for smart TVs.
- [Inbox Zero](https://www.inboxzero.com/) - Email management.

### Running on Proxmox, routed via Kubernetes (`k8s/apps/external/`)
- [Home Assistant OS](https://www.home-assistant.io/) - Home automation platform.
- [Open WebUI](https://openwebui.com/) - Web interface for AI models.
- [Grafana](https://grafana.com/) - Monitoring dashboards.
- [PocketID](https://github.com/stonith404/pocket-id) - OIDC identity provider.
- [Change Detection](https://changedetection.io/) - Website change monitoring.
- [Proxmox](https://www.proxmox.com/) - Proxmox VE external access.

### Docker Stacks — Komodo (`stacks/`)
- [Immich](https://immich.app/) - Google Photos alternative.
- [Paperless-ngx](https://docs.paperless-ngx.com/) - Document management with OCR.
- [n8n](https://n8n.io/) - Workflow automation and integrations.
- [Miniflux](https://miniflux.app/) - RSS feed reader.
- [Nextflux](https://github.com/electh/nextflux) - Miniflux web frontend.
- [RSSHub](https://rsshub.app/) - RSS feed generator.
- [Karakeep](https://karakeep.app/) - Bookmark and read-it-later manager.
- [ByteStash](https://github.com/jordan-dalby/ByteStash) - Code snippet manager.
- [Your Spotify](https://github.com/Yooooomi/your_spotify) - Spotify listening stats.
- [S-PDF](https://github.com/Stirling-Tools/Stirling-PDF) - PDF tools.
- [Dozzle](https://dozzle.dev/) - Container log viewer.
- [Traefik](https://traefik.io/) - Reverse proxy for Docker stacks.
- [GitHub Runner](https://github.com/actions/runner) - Self-hosted GitHub Actions runner.

---

## Repository Structure 📂

```shell
.
├── k8s
│   ├── apps  # applications
│   │   ├── external  # external-facing applications (Gateway API routes)
│   │   └── internal  # internal services
│   ├── components    # reusable kustomize components
│   └── infra         # k8s infrastructure
│       ├── argocd    # gitops deployment
│       ├── network   # networking components
│       ├── security  # security components
│       └── storage   # storage components
├── stacks            # docker compose stacks managed by Komodo
└── terraform
    └── modules
        ├── monitoring          # grafana cloud monitoring
        ├── proxmox             # talos vm deployment
        ├── proxmox-csi-plugin  # proxmox storage for k8s
        ├── sealed-secrets      # k8s secret management
        ├── talos               # talos cluster deployment
        └── traefik             # traefik tls passthrough lxc container
```

## GitOps with ArgoCD 🚢

The cluster uses ArgoCD for GitOps-based continuous delivery. All applications and infrastructure components are automatically synchronized from this Git repository.

### Key Features:
- **UI Access**: https://argocd.ravil.space
- **Auto-sync**: All applications are configured for automatic synchronization
- **Self-healing**: Automatic correction of manual cluster changes to match Git state
- **Application Structure**:
  - Infrastructure components (`k8s/infra/*`)
  - Internal services (glance, isponsorblocktv)
  - External applications (`k8s/apps/external/*`)

### Quick Commands:
```bash
# Get ArgoCD admin password
make argocd-password

# Restart ArgoCD components
make argocd-restart

# View application status
kubectl -n argocd get applications
```

---


