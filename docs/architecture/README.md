# HomeLab Architecture Documentation

This directory contains comprehensive architecture documentation for the HomeLab infrastructure project.

## Documentation Structure

```
docs/
├── architecture/          # System architecture documentation
│   ├── README.md         # This file
│   ├── 01-system-context.md
│   ├── 02-container-architecture.md
│   ├── 03-component-architecture.md
│   ├── 04-deployment-architecture.md
│   ├── 05-security-architecture.md
│   ├── 06-data-architecture.md
│   └── 07-observability-architecture.md
├── adr/                  # Architecture Decision Records
│   ├── README.md
│   ├── 0001-use-talos-linux.md
│   ├── 0002-use-cilium-cni.md
│   ├── 0003-use-gateway-api.md
│   └── template.md
├── diagrams/            # Architecture diagrams (PlantUML/Mermaid)
│   ├── c4/             # C4 model diagrams
│   ├── deployment/     # Deployment diagrams
│   ├── network/        # Network topology
│   └── sequence/       # Sequence diagrams
└── runbooks/           # Operational procedures
    ├── bootstrap.md
    ├── disaster-recovery.md
    └── troubleshooting.md
```

## Architecture Overview

The HomeLab infrastructure is built on modern cloud-native principles with a focus on:

- **Declarative Infrastructure**: All infrastructure is defined as code using Terraform and Kubernetes manifests
- **GitOps**: ArgoCD manages application lifecycle based on Git repository state
- **Security**: Sealed Secrets for encrypted credentials, Gateway API for traffic management
- **Observability**: Grafana Cloud integration for comprehensive monitoring
- **Storage**: Proxmox CSI plugin for dynamic persistent volume provisioning
- **Networking**: Cilium CNI with eBPF-based networking and security

## Quick Links

- [System Context](01-system-context.md) - High-level system overview
- [Container Architecture](02-container-architecture.md) - Service architecture and deployment
- [Component Architecture](03-component-architecture.md) - Internal component structure
- [Deployment Architecture](04-deployment-architecture.md) - Infrastructure and deployment patterns
- [Security Architecture](05-security-architecture.md) - Security controls and boundaries
- [Data Architecture](06-data-architecture.md) - Data flows and storage
- [Observability Architecture](07-observability-architecture.md) - Monitoring and logging

## Architecture Decision Records (ADRs)

All significant architectural decisions are documented in the [adr/](../adr/) directory. See [ADR README](../adr/README.md) for the decision-making process.

## Diagrams

Visual representations of the architecture are maintained as code using PlantUML and Mermaid in the [diagrams/](../diagrams/) directory.

## Maintenance

This documentation is maintained alongside the codebase. When making architectural changes:

1. Update relevant architecture documents
2. Create an ADR for significant decisions
3. Update or create new diagrams
4. Update operational runbooks if procedures change

## Contact

For questions or clarifications, please refer to the main [project README](../../README.md).
