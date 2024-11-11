locals {
  talos = {
    version = "v1.8.2"
  }
}

# customization:
# customization:
#    systemExtensions:
#        officialExtensions:
#            - siderolabs/hello-world-service
#            - siderolabs/i915-ucode
#            - siderolabs/intel-ucode
#            - siderolabs/iscsi-tools
#            - siderolabs/qemu-guest-agent
#            - siderolabs/util-linux-tools
resource "proxmox_virtual_environment_download_file" "talos_nocloud_image" {
  content_type = "iso"
  datastore_id = "local"
  node_name    = "pve01"

  file_name               = "talos-${local.talos.version}-nocloud-amd64.img"
  url                     = "https://factory.talos.dev/image/db04063dfc148665c618225eb9df33d7a6d3bbea131666c794ceb6bf13a21d31/${local.talos.version}/nocloud-amd64.raw.gz"
  decompression_algorithm = "gz"
  overwrite               = false
}