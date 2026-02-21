# ADR 0005: Use ArgoCD for GitOps

## Status

Accepted

Date: 2024-06

## Context

The HomeLab infrastructure requires a reliable way to deploy and manage Kubernetes applications. Manual `kubectl apply` commands are error-prone, don't provide audit trails, and don't support declarative drift detection.

Requirements:
- Git as the single source of truth
- Automatic synchronization of cluster state with Git
- Drift detection and self-healing
- Support for Kustomize and Helm
- Web UI for visibility
- Declarative application definitions
- Multi-application management

The cluster already uses Terraform for infrastructure, and we need a similar declarative approach for application deployment.

## Decision

We will use **ArgoCD** as the GitOps continuous delivery tool for managing all Kubernetes applications in the HomeLab cluster.

ArgoCD continuously monitors the Git repository and automatically synchronizes the cluster state to match the desired state defined in Git manifests.

Key implementation details:
- Deploy ArgoCD via Helm chart during bootstrap process
- Configure auto-sync and self-healing for all applications
- Use ArgoCD Applications to define each service
- Store all manifests in Git repository
- Enable Kustomize with Helm support (`--enable-helm`)
- Expose ArgoCD UI via Gateway API at https://argocd.ravil.space
- Bootstrap critical components manually, then let ArgoCD manage everything else

## Consequences

### Positive

- **Git as Source of Truth**: All changes go through Git, providing audit trail and version control
- **Automatic Synchronization**: ArgoCD detects Git changes and applies them automatically
- **Drift Detection**: Detects manual changes to cluster and alerts or auto-corrects
- **Self-Healing**: Automatically reverts unauthorized changes to maintain desired state
- **Declarative**: All applications defined declaratively in Git
- **Visibility**: Web UI shows deployment status, health, and sync state for all applications
- **Rollback**: Easy rollback by reverting Git commits
- **Multi-Environment**: Can manage multiple clusters from single ArgoCD instance (future)
- **Kustomize + Helm**: Supports both native Kubernetes manifests and Helm charts
- **RBAC**: Built-in role-based access control
- **Notifications**: Can integrate with Slack, email, etc. for deployment notifications

### Negative

- **Additional Component**: One more thing to maintain and monitor
- **Bootstrap Complexity**: ArgoCD itself must be deployed before it can manage other apps
- **Learning Curve**: Team needs to learn ArgoCD concepts and UI
- **Potential Sync Conflicts**: Manual changes will be reverted (can be surprising)
- **Resource Overhead**: ArgoCD components consume cluster resources
- **Dependency**: Cluster deployments depend on ArgoCD being healthy
- **Git Coupling**: All changes must go through Git (even for experiments)

### Neutral

- **Opinionated Workflow**: Enforces GitOps workflow (good for consistency, limits ad-hoc changes)
- **Declarative Only**: Imperative changes are discouraged/reverted
- **Repository Structure**: Requires well-organized Git repository structure

## Alternatives Considered

### Alternative 1: Flux CD

**Description**: Flux CD is another popular GitOps tool for Kubernetes

**Pros**:
- CNCF graduated project (more mature governance)
- GitOps Toolkit architecture (modular)
- Integrated with Flagger for progressive delivery
- Native Helm and Kustomize support
- Strong integration with Git providers
- Image automation for updating container tags

**Cons**:
- No built-in UI (requires third-party UI like Weave GitOps)
- More complex architecture (multiple controllers)
- Steeper learning curve
- Less intuitive status visibility without UI

**Why rejected**: ArgoCD's built-in UI provides better visibility for a HomeLab environment where quick status checks are valuable. Flux's additional complexity isn't needed.

### Alternative 2: Manual kubectl apply

**Description**: Continue using manual `kubectl apply` commands and Makefile targets

**Pros**:
- Simple, no additional components
- Direct control over deployments
- Familiar to most Kubernetes users
- No learning curve
- Easy experimentation

**Cons**:
- No drift detection
- No automatic synchronization
- No audit trail (who deployed what, when)
- Error-prone (forgetting to apply changes)
- No self-healing
- Manual rollback process
- Hard to track cluster state

**Why rejected**: Doesn't align with GitOps principles and Infrastructure as Code practices. No visibility into deployment state.

### Alternative 3: Helm + Helmfile

**Description**: Use Helmfile to declaratively manage Helm releases

**Pros**:
- Declarative Helm release management
- Works with existing Helm charts
- Simpler than ArgoCD (just a CLI tool)
- Can store state in Git

**Cons**:
- No automatic synchronization (must run manually)
- No drift detection
- No web UI
- Helm-only (doesn't support plain Kubernetes manifests well)
- No self-healing
- Requires CI/CD integration for automation

**Why rejected**: Doesn't provide automatic synchronization or drift detection. Still requires external automation.

### Alternative 4: Jenkins X / Tekton

**Description**: Use Jenkins X or Tekton for CI/CD-based deployments

**Pros**:
- Full CI/CD pipeline
- Integrated with Git
- Preview environments
- Automated testing

**Cons**:
- Much more complex than needed for HomeLab
- Heavyweight (many components)
- Designed for multi-team development
- Overkill for personal infrastructure
- Steeper learning curve

**Why rejected**: Too complex for HomeLab use case. ArgoCD provides the needed GitOps capabilities without CI/CD overhead.

## References

- [ArgoCD Documentation](https://argo-cd.readthedocs.io/)
- [ArgoCD vs Flux](https://www.cncf.io/blog/2020/12/04/comparing-cd-solutions-argocd-vs-flux-cd/)
- [GitOps Principles](https://opengitops.dev/)
- [ArgoCD Best Practices](https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/)

## Notes

- ArgoCD is deployed during bootstrap via `make k8s-apply` after Terraform completes
- Critical infrastructure (CRDs, Cert-Manager) is still applied manually during bootstrap
- ArgoCD manages its own configuration (ArgoCD Application of Applications pattern)
- Auto-sync is enabled with self-healing for production-like behavior
- Future consideration: Add Argo Rollouts for progressive delivery (canary, blue-green)
- Consider adding ArgoCD notifications for Slack/email alerts on deployment status
