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
- [ArgoCD](https://argo-cd.readthedocs.io/) GitOps continuous delivery for Kubernetes.


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
│   └── infra # k8s infrastructure
│       ├── gitops
│       │   └── argocd  # ArgoCD setup and application definitions
│       └── ...
└── terraform
    └── modules
        ├── monitoring          # grafana cloud monitoring
        ├── proxmox             # talos vm deployment
        ├── proxmox-csi-plugin  # proxmox storage for k8s
        ├── sealed-secrets      # k8s secret management
        ├── talos               # talos cluster deployment
        └── traefik             # traefik tls passthrough lxc container
```

## GitOps with ArgoCD

This project uses [ArgoCD](https://argo-cd.readthedocs.io/) for GitOps-based continuous delivery. ArgoCD follows the GitOps pattern of using Git repositories as the source of truth for defining the desired application state.

### Accessing ArgoCD

- Web UI: https://argocd.ravil.space
- Default admin credentials: Get the password with `make argocd-password`

### Managing Applications

Applications are defined as Kubernetes manifests in `k8s/infra/gitops/argocd/applications/`. To add a new application:

1. Create an Application manifest file in the applications directory
2. Add it to the `kustomization.yaml` in the applications directory
3. Apply it with `make argocd-apply-apps`
4. ArgoCD will sync the application from the specified Git repository path

### Getting Started with ArgoCD

After deploying ArgoCD with `make k8s-apply`, follow these steps:

1. Get the initial admin password:
   ```
   make argocd-password
   ```

2. Access the UI at https://argocd.ravil.space

3. Deploy your applications to ArgoCD:
   ```
   make argocd-apply-apps
   ```

4. Change the admin password after first login

---


## 🚀 Next Features

Planned features for this project include:

- [**OIDC**](https://openid.net/connect/): OpenID Connect integration for authentication. such as [Authelia](https://www.authelia.com/) 
or [Zitadel](https://github.com/zitadel/zitadel).

---


