# Download the Talos image using the URL provided by the Talos module
# Note: Using replace() to change .raw.xz to .raw.gz since Proxmox provider only supports gz/lzo/zst/bz2
resource "proxmox_virtual_environment_download_file" "talos_nocloud_image" {
  content_type = "iso"
  datastore_id = "local"
  node_name    = var.node_name

  file_name               = "talos-${var.talos_version}-nocloud-amd64.img"
  url                     = replace(var.talos_image_url, ".raw.xz", ".raw.gz")
  decompression_algorithm = "gz"
  overwrite               = false
}