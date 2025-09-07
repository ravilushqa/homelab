resource "proxmox_virtual_environment_container" "traefik" {
  description = "Managed by Terraform"

  node_name    = "pve01"
  unprivileged = true



  memory {
    swap = 512
  }
  disk {
    datastore_id = "local-lvm"
    size         = "2"
  }

  initialization {
    hostname = "traefik"
    ip_config {
      ipv4 {
        address = "192.168.1.4/24"
        gateway = "192.168.1.1"
      }
    }
    user_account {
      keys = [
        trimspace(tls_private_key.ubuntu_container_key.public_key_openssh)
      ]
      password = random_password.ubuntu_container_password.result
    }
  }

  network_interface {
    name   = "eth0"
    bridge = "vmbr0"
  }

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.latest_ubuntu_20_focal_lxc_img.id
    type             = "ubuntu"
  }

  startup {
    order      = "3"
    up_delay   = "60"
    down_delay = "60"
  }

  connection {
    type        = "ssh"
    host        = "192.168.1.4" # Container's IP
    user        = "root"
    private_key = trimspace(tls_private_key.ubuntu_container_key.private_key_openssh)
  }

  provisioner "remote-exec" {
    inline = [
      "mkdir -p /etc/traefik/conf.d",
    ]
  }

  provisioner "file" {
    source      = "${path.module}/install_traefik.sh"
    destination = "/tmp/install_traefik.sh"
  }

  provisioner "file" {
    source      = "${path.module}/configs/traefik.yml"
    destination = "/etc/traefik/traefik.yml"
  }

  provisioner "file" {
    source      = "${path.module}/configs/tcp_routers.yaml"
    destination = "/etc/traefik/conf.d/tcp_routers.yaml"
  }


  # Add the provisioner block
  provisioner "remote-exec" {
    inline = [
      "chmod +x /tmp/install_traefik.sh",
      "/tmp/install_traefik.sh",
    ]
  }
}

resource "proxmox_virtual_environment_download_file" "latest_ubuntu_20_focal_lxc_img" {
  content_type = "vztmpl"
  datastore_id = "local"
  node_name    = "pve01"
  url          = "http://download.proxmox.com/images/system/ubuntu-20.04-standard_20.04-1_amd64.tar.gz"
}

resource "random_password" "ubuntu_container_password" {
  length           = 16
  override_special = "_%@"
  special          = true
}

resource "tls_private_key" "ubuntu_container_key" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "null_resource" "traefik_config_sync" {
  triggers = {
    tcp_routers_hash    = filemd5("${path.module}/configs/tcp_routers.yaml")
    traefik_config_hash = filemd5("${path.module}/configs/traefik.yml")
  }

  connection {
    type        = "ssh"
    host        = "192.168.1.4"
    user        = "root"
    private_key = trimspace(tls_private_key.ubuntu_container_key.private_key_openssh)
  }

  provisioner "file" {
    source      = "${path.module}/configs/tcp_routers.yaml"
    destination = "/etc/traefik/conf.d/tcp_routers.yaml"
  }

  provisioner "file" {
    source      = "${path.module}/configs/traefik.yml"
    destination = "/etc/traefik/traefik.yml"
  }

  provisioner "remote-exec" {
    inline = [
      "systemctl restart traefik || service traefik restart"
    ]
  }

  depends_on = [proxmox_virtual_environment_container.traefik]
}
