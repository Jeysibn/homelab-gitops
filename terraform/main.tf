# Download Ubuntu 22.04 Cloud-Init Image to Proxmox 'local' storage
resource "proxmox_download_file" "ubuntu_cloud_image" {
  content_type        = "iso"
  datastore_id        = "local"
  node_name           = var.proxmox_node
  url                 = "https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64.img"
  file_name           = "ubuntu-22.04-server-cloudimg-amd64.img"
  overwrite           = true
  overwrite_unmanaged = true
}

# Create Cloud-Init User Data snippet using proxmox_virtual_environment_file
resource "proxmox_virtual_environment_file" "cloud_config" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node

  source_raw {
    data = format("#cloud-config\n%s", yamlencode({
      users = [
        {
          name                = "ubuntu"
          sudo                = "ALL=(ALL) NOPASSWD:ALL"
          groups              = "users, admin"
          shell               = "/bin/bash"
          ssh_authorized_keys = var.ssh_public_keys
        }
      ]
      package_update = true
      packages = [
        "qemu-guest-agent",
        "open-iscsi",
        "nfs-common"
      ]
      runcmd = [
        "systemctl enable --now qemu-guest-agent",
        "systemctl enable --now iscsid"
      ]
    }))

    file_name = "k3s-cloud-config.yaml"
  }
}

# Provision the K3s VM
resource "proxmox_virtual_environment_vm" "k3s_node" {
  name          = "k3s-node-01"
  node_name     = var.proxmox_node
  vm_id         = 100
  boot_order    = ["scsi0"]
  scsi_hardware = "virtio-scsi-single"

  agent {
    enabled = true
  }

  cpu {
    cores = 4
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  disk {
    datastore_id = "local-lvm"
    file_id      = proxmox_download_file.ubuntu_cloud_image.id
    file_format  = "raw"
    interface    = "scsi0"
    size         = 50
  }

  network_device {
    bridge = "vmbr0"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id      = "local-lvm"
    user_data_file_id = proxmox_virtual_environment_file.cloud_config.id

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
  }
}