# Plan-only, all providers mocked: no Proxmox/Talos/Kubernetes API call, no credentials.
# Proves the root wiring (count gate, pinned VMID/node) only; it is not evidence of any API call,
# permission or power state. The root has a Terraform Cloud block, so do not run `terraform init`
# in terraform/ for this: use terraform/tests/offline-test.sh (scratch copy without the cloud block).

mock_provider "proxmox" {}
mock_provider "talos" {}
mock_provider "helm" {}
mock_provider "kubernetes" {}

variables {
  proxmox_endpoint = "https://pve.invalid:8006"
}

run "dev_vm_disabled_by_default" {
  command = plan

  assert {
    condition     = length(module.dev_vm) == 0
    error_message = "the dev VM module must stay disabled until the reviewed apply window"
  }
}

run "dev_vm_opt_in_creates_one_vm" {
  command = plan
  variables {
    dev_vm_enabled = true
  }

  assert {
    condition     = length(module.dev_vm) == 1 && module.dev_vm[0].vm_id == 130 && module.dev_vm[0].node_name == "pve01"
    error_message = "opt-in must create exactly the pinned VMID 130 on pve01"
  }
}
