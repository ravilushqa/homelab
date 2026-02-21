# ADR 0001: Use Talos Linux as Kubernetes OS

## Status

Accepted

Date: 2024-04

## Context

The HomeLab infrastructure requires a robust, secure, and maintainable operating system for running Kubernetes clusters. Traditional Linux distributions (Ubuntu, CentOS, Debian) require significant manual configuration, security hardening, and maintenance overhead.

Key requirements:
- Minimal attack surface
- Immutable infrastructure
- API-driven configuration (no SSH)
- Easy upgrades and rollbacks
- Built specifically for Kubernetes
- Good integration with Proxmox virtualization

## Decision

We will use **Talos Linux** as the operating system for all Kubernetes nodes (control plane and workers).

Talos Linux is a modern, immutable, API-driven Linux distribution designed specifically for running Kubernetes. All system configuration is managed through the Talos API, and the OS has no SSH access, interactive console, or package manager.

Key implementation details:
- Deploy Talos as VMs on Proxmox VE
- Bootstrap cluster using Terraform
- Configure Cilium CNI via inline manifests
- Manage configuration through talosctl CLI
- Store machine configurations in version control

## Consequences

### Positive

- **Minimal Attack Surface**: No SSH, no shell, no package manager significantly reduces security risks
- **Immutable Infrastructure**: OS is read-only, all changes require new image deployment
- **Declarative Configuration**: All configuration is API-driven and can be version controlled
- **Easy Upgrades**: Atomic OS upgrades with automatic rollback on failure
- **Kubernetes-Native**: Built specifically for Kubernetes, no unnecessary components
- **Predictable**: All nodes identical, reduces configuration drift
- **Fast Boot**: Minimal OS footprint leads to fast startup times
- **API-Driven**: All operations through API, enabling full automation

### Negative

- **Learning Curve**: Different from traditional Linux distributions, requires new skills
- **Debugging Challenges**: No SSH access means debugging requires talosctl and log inspection
- **Limited Ecosystem**: Smaller community compared to Ubuntu/Debian
- **Vendor Lock-in**: Tight coupling to Talos ecosystem and tools
- **Breaking Changes**: Talos is evolving, upgrades may require configuration changes
- **Troubleshooting**: Less familiar to most administrators, harder to find help

### Neutral

- **Opinionated**: Talos makes specific choices about Kubernetes setup (good for consistency, limits flexibility)
- **API-Only**: All operations require talosctl or API calls (enforces automation)
- **Image-Based**: Cannot install packages or make runtime modifications

## Alternatives Considered

### Alternative 1: Ubuntu Server

**Description**: Use Ubuntu Server 22.04 LTS with kubeadm for Kubernetes installation

**Pros**:
- Well-documented and widely used
- Large community support
- Familiar to most administrators
- Flexible, can install any packages
- SSH access for debugging

**Cons**:
- Requires manual security hardening
- Configuration drift over time
- More attack surface (unnecessary services)
- Manual OS updates and security patches
- Not optimized for Kubernetes
- Mutable OS can lead to inconsistencies

**Why rejected**: Too much manual maintenance overhead, security hardening required, and configuration drift concerns.

### Alternative 2: Flatcar Container Linux

**Description**: Flatcar Container Linux, a fork of CoreOS, designed for containers

**Pros**:
- Immutable, auto-updating OS
- Container-optimized
- Minimal attack surface
- Active community support

**Cons**:
- Not Kubernetes-specific
- Requires ignition configs (complex)
- Still has SSH access (security concern)
- More general-purpose than Talos

**Why rejected**: While Flatcar is immutable and container-optimized, Talos is more Kubernetes-specific and has a smaller attack surface (no SSH).

### Alternative 3: K3OS

**Description**: K3OS (deprecated), a lightweight Kubernetes OS from Rancher

**Pros**:
- Kubernetes-specific
- Lightweight
- Integrated with k3s

**Cons**:
- **Deprecated**: No longer maintained
- Coupled to k3s (we want full Kubernetes)
- Less mature than Talos

**Why rejected**: Project is deprecated and no longer maintained.

### Alternative 4: RKE2/RancherOS

**Description**: Rancher's RKE2 on RancherOS

**Pros**:
- Rancher ecosystem integration
- FIPS compliance support
- Enterprise support available

**Cons**:
- RancherOS v2 still in development
- More complex than needed
- Tighter coupling to Rancher

**Why rejected**: Talos is more focused and has better Proxmox integration. RancherOS v2 is less mature.

## References

- [Talos Linux Documentation](https://www.talos.dev/latest/)
- [Talos GitHub Repository](https://github.com/siderolabs/talos)
- [Talos Security Model](https://www.talos.dev/latest/learn-more/security/)
- [Proxmox Integration Guide](https://www.talos.dev/latest/talos-guides/install/virtualized-platforms/proxmox/)

## Notes

- Talos requires learning new tooling (talosctl) but provides better security and consistency
- The API-driven approach aligns well with Infrastructure as Code principles
- Future consideration: Evaluate Talos for production workloads beyond HomeLab
- Talos inline manifests allow deploying Cilium CNI before kubelet starts (solving chicken-and-egg problem)
