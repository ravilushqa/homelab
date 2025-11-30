locals {
  talos = {
    version = "v1.11.5"
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
  url                     = "https://factory.talos.dev/image/55e97b36ef277a94cf245a5e33b30a5dfd0d8e86443fd4616ce07c87c257d6b9/${local.talos.version}/nocloud-amd64.raw.gz"
  decompression_algorithm = "gz"
  overwrite               = false
}