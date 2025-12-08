# Create a Talos Image Factory schematic with system extensions
resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = var.system_extensions
      }
    }
  })
}

# Generate Image Factory URLs for the schematic
data "talos_image_factory_urls" "this" {
  schematic_id  = talos_image_factory_schematic.this.id
  platform      = "nocloud"
  architecture  = "amd64"
  talos_version = var.talos_version
}
