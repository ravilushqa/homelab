# Architecture Decision Records (ADRs)

## Overview

This directory contains Architecture Decision Records (ADRs) for the HomeLab infrastructure project. ADRs document significant architectural decisions, their context, and rationale.

## What is an ADR?

An Architecture Decision Record (ADR) is a document that captures an important architectural decision made along with its context and consequences. ADRs help teams understand:

- **Why** decisions were made
- **What** alternatives were considered
- **What** the trade-offs are
- **When** the decision was made

## ADR Format

Each ADR follows a standard template (see [template.md](template.md)) with these sections:

1. **Title**: Short, descriptive name
2. **Status**: Proposed, Accepted, Deprecated, Superseded
3. **Context**: The situation prompting the decision
4. **Decision**: The chosen solution
5. **Consequences**: Trade-offs and implications
6. **Alternatives Considered**: Other options evaluated

## Naming Convention

ADRs are numbered sequentially and stored as markdown files:

```
NNNN-title-in-kebab-case.md
```

Examples:
- `0001-use-talos-linux.md`
- `0002-use-cilium-cni.md`
- `0003-use-gateway-api.md`

## ADR Lifecycle

### Statuses

- **Proposed**: Under consideration, not yet decided
- **Accepted**: Decision made and being implemented
- **Deprecated**: No longer recommended but still in use
- **Superseded**: Replaced by a newer decision (link to new ADR)

### Creating a New ADR

1. Copy the template:
   ```bash
   cp docs/adr/template.md docs/adr/NNNN-your-decision.md
   ```

2. Fill in all sections:
   - Describe the context and problem
   - Document the decision and reasoning
   - List alternatives considered
   - Outline consequences and trade-offs

3. Set status to "Proposed"

4. Review with stakeholders

5. Update status to "Accepted" when finalized

6. Commit to repository

### Superseding an ADR

When a decision is replaced:

1. Create a new ADR for the new decision
2. Update the old ADR:
   - Change status to "Superseded"
   - Add link to the new ADR
   - Add date of supersession

## Current ADRs

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-use-talos-linux.md) | Use Talos Linux as Kubernetes OS | Accepted | 2024-04 |
| [0002](0002-use-cilium-cni.md) | Use Cilium as CNI | Accepted | 2024-04 |
| [0003](0003-use-gateway-api.md) | Use Gateway API for Ingress | Accepted | 2024-04 |
| [0004](0004-use-sealed-secrets.md) | Use Sealed Secrets for Secret Management | Accepted | 2024-04 |
| [0005](0005-use-argocd-gitops.md) | Use ArgoCD for GitOps | Accepted | 2024-06 |
| [0006](0006-use-proxmox-csi.md) | Use Proxmox CSI for Storage | Accepted | 2024-04 |
| [0007](0007-use-grafana-cloud.md) | Use Grafana Cloud for Observability | Accepted | 2024-04 |
| [0008](0008-use-traefik-edge.md) | Use Traefik as Edge Reverse Proxy | Accepted | 2024-04 |

## Decision Categories

### Infrastructure
- Operating System (Talos Linux)
- Hypervisor (Proxmox VE)
- Container Runtime (containerd)

### Networking
- CNI Plugin (Cilium)
- Ingress (Gateway API)
- Edge Proxy (Traefik)
- DNS (CoreDNS, Cloudflare)

### Security
- Secret Management (Sealed Secrets)
- Certificate Management (Cert-Manager)
- Network Policies (Cilium)

### Operations
- GitOps (ArgoCD)
- Monitoring (Grafana Cloud)
- Storage (Proxmox CSI)
- Automation (Terraform, Kustomize)

## Principles

When making architectural decisions, consider:

1. **Simplicity**: Prefer simple solutions over complex ones
2. **Maintainability**: Choose technologies with good documentation and community support
3. **Security**: Security should be a primary concern, not an afterthought
4. **Cost**: Prefer open-source and free solutions where appropriate
5. **Learning**: HomeLab is a learning environment - choose technologies that teach valuable skills
6. **Automation**: Automate everything possible (Infrastructure as Code)
7. **Declarative**: Prefer declarative over imperative approaches
8. **GitOps**: Git as the single source of truth

## References

- [ADR GitHub Organization](https://adr.github.io/)
- [Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [ADR Tools](https://github.com/npryce/adr-tools)
