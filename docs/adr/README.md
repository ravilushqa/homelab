# Architecture Decision Records (ADRs)

An ADR records one significant decision: its context, the choice, the alternatives and the consequences.
Copy [template.md](template.md) to `NNNN-title-in-kebab-case.md` with the next free number.
A replaced decision keeps its file, with status `Superseded` and a link to the new ADR.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-use-talos-linux.md) | Use Talos Linux as Kubernetes OS | Accepted | 2024-04 |
| [0005](0005-use-argocd-gitops.md) | Use ArgoCD for GitOps | Accepted | 2024-06 |

Numbers 0002–0004 and 0006–0008 were reserved for decisions that were never written down
(Cilium, Gateway API, Sealed Secrets, Proxmox CSI, Grafana Cloud, edge Traefik). Grafana Cloud was replaced by the
self-hosted `grafana-lgtm` Komodo stack, and the edge Traefik LXC by SNI passthrough in the Komodo Traefik (2026-10).
