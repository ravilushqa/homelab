terraform {
  required_providers {
    # Version pinned only in the root module (terraform/providers.tf); tests/offline-test.sh
    # pins the scratch copy of this module to the same version.
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}
