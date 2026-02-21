# Terraform Refactoring Guide

## Overview
This guide documents the refactoring performed on the Terraform configuration to eliminate code duplication, improve maintainability, and follow DRY principles.

## Changes Made

### 1. Proxmox Module Refactoring

#### Before
- Two separate resources: `talos_cp_01` and `talos_worker_01`
- Hardcoded values duplicated across both resources
- ~100 lines of duplicated configuration

#### After
- Created `locals.tf` with shared configuration
- Split resources into `talos_cp` and `talos_workers` using `for_each`
- Common configuration centralized in `local.common_vm_config`
- VM-specific settings in `local.vms` map
- ~50% reduction in code

**Files Modified:**
- `terraform/modules/proxmox/main.tf` - Refactored to use for_each pattern
- `terraform/modules/proxmox/locals.tf` - NEW: Centralized configuration
- `terraform/modules/proxmox/outputs.tf` - Updated for new resource names
- `terraform/modules/proxmox/variables.tf` - Already had datastore_id variable

### 2. Main Terraform Configuration

#### Before
- Kubeconfig parsing duplicated in helm and kubernetes providers
- Complex base64decode and yamldecode calls repeated

#### After
- Created `terraform/locals.tf` with parsed kubeconfig
- Extracted cluster and client configuration to `local.k8s_cluster` and `local.k8s_client`
- Providers now reference clean local values

**Files Modified:**
- `terraform/main.tf` - Updated providers to use locals
- `terraform/locals.tf` - NEW: Kubeconfig parsing

### 3. Talos Module Refactoring

#### Before
- Hostnames hardcoded in `talos_machine_configuration_apply` resources
- Proxmox node_name hardcoded as "pve01"
- No connection between Proxmox VM names and Talos hostnames

#### After
- Added variables for hostnames: `talos_cp_hostname`, `talos_worker_hostname`, `proxmox_node_name`
- Proxmox module outputs VM hostnames dynamically
- Main configuration passes VM hostnames from Proxmox to Talos
- Single source of truth for VM/hostname configuration

**Files Modified:**
- `terraform/modules/talos/variables.tf` - Added hostname and node_name variables
- `terraform/modules/talos/main.tf` - Use variables instead of hardcoded values
- `terraform/modules/proxmox/outputs.tf` - Export hostnames and node_name
- `terraform/main.tf` - Pass Proxmox outputs to Talos module

## State Migration Required

⚠️ **IMPORTANT**: The refactoring changed resource names, which will cause Terraform to want to destroy and recreate VMs. To avoid downtime, you must migrate the state.

### Migration Commands

Run these commands to migrate the state without destroying resources:

```bash
# Migrate control plane VM
terraform -chdir=./terraform state mv \
  'module.proxmox.proxmox_virtual_environment_vm.talos_cp_01' \
  'module.proxmox.proxmox_virtual_environment_vm.talos_cp["talos-cp-01"]'

# Migrate worker VM
terraform -chdir=./terraform state mv \
  'module.proxmox.proxmox_virtual_environment_vm.talos_worker_01' \
  'module.proxmox.proxmox_virtual_environment_vm.talos_workers["talos-worker-01"]'

# Verify the migration
terraform -chdir=./terraform plan
```

After running these commands, `terraform plan` should show no changes to the VM resources.

## Benefits

1. **Reduced Code Duplication**: ~50% less code in proxmox module
2. **Easier to Scale**: Adding new VMs is now a simple map entry
3. **Centralized Configuration**: Common settings in one place
4. **Better Maintainability**: Changes to common config automatically apply to all VMs
5. **Improved Readability**: Kubeconfig parsing done once, reused cleanly
6. **Single Source of Truth**: VM hostnames defined once in Proxmox, used everywhere
7. **Type Safety**: Structured locals and outputs prevent typos and mismatches

## Future Improvements

Consider these enhancements:

1. **Parameterize VM specifications**: Move VM map to variables for external configuration
2. **Add validation**: Use variable validation to ensure IP addresses are valid
3. **Dynamic worker scaling**: Use count or for_each based on variable
4. **Separate disk configuration**: Allow per-VM disk size customization
5. **Network configuration**: Make bridge and network settings configurable

## Testing

Before applying changes to production:

```bash
# Format code
terraform -chdir=./terraform fmt -recursive

# Validate configuration
terraform -chdir=./terraform validate

# Review plan (before state migration)
terraform -chdir=./terraform plan

# Migrate state (see commands above)

# Verify no changes after migration
terraform -chdir=./terraform plan
```

## Rollback Procedure

If you need to rollback:

```bash
# Restore from git
git checkout HEAD -- terraform/

# Re-initialize
terraform -chdir=./terraform init
```

Note: If you've already migrated state, you'll need to reverse the state moves or restore from a backup.
