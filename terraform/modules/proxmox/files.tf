locals {
  talos = {
    version = "v1.11.5"
  }
}

# Create a Talos Image Factory schematic with system extensions
resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = [
          "siderolabs/i915-ucode",
          "siderolabs/intel-ucode",
          "siderolabs/iscsi-tools",
          "siderolabs/qemu-guest-agent",
          "siderolabs/util-linux-tools",
        ]
      }
    }
  })
}

# Generate Image Factory URLs for the schematic
data "talos_image_factory_urls" "this" {
  schematic_id  = talos_image_factory_schematic.this.id
  platform      = "nocloud"
  architecture  = "amd64"
  talos_version = local.talos.version
}

# Download the Talos image using the dynamically generated URL
# Note: Using replace() to change .raw.xz to .raw.gz since Proxmox provider only supports gz/lzo/zst/bz2
resource "proxmox_virtual_environment_download_file" "talos_nocloud_image" {
  content_type = "iso"
  datastore_id = "local"
  node_name    = "pve01"

  file_name               = "talos-${local.talos.version}-nocloud-amd64.img"
  url                     = replace(data.talos_image_factory_urls.this.urls.disk_image, ".raw.xz", ".raw.gz")
  decompression_algorithm = "gz"
  overwrite               = false
}