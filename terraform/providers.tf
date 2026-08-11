terraform {
  required_version = ">= 1.5.0"
  cloud {
    organization = "Homelab-gitops" # Replace with your HCP Terraform org name

    workspaces {
      name = "homelab-gitops"
    }
  }
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.60.0"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = true

  # Add this SSH configuration block
  ssh {
    agent       = true
    username    = "root"
    private_key = var.proxmox_ssh_private_key # Replace with id_rsa if using RSA keys
  }
}