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

---

## Applications 📦
- [Home Assistant Operating System (HAOS)](https://www.home-assistant.io/installation/operating-system) - Home automation.
- [Immich](https://immich.app/) - Google Photos alternative.
- [Pi-hole](https://pi-hole.net/) - DNS and DHCP server. (currently under the scope of the repository)
- many more to come...

---

## Repository Structure 📂

```shell
.
├── k8s
│   ├── apps  # applications
│   │   ├── external  # external-facing applications
│   │   └── internal  # internal services
│   └── infra # k8s infrastructure
│       ├── argocd    # gitops deployment
│       ├── network   # networking components
│       ├── security  # security components
│       └── storage   # storage components
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

## 🚀 Next Features

Planned features for this project include:

- [**OIDC**](https://openid.net/connect/): OpenID Connect integration for authentication. such as [Authelia](https://www.authelia.com/) 
or [Zitadel](https://github.com/zitadel/zitadel).

---


