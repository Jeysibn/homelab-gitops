variable "proxmox_api_url" {
  type        = string
  description = "https://<PROXMOX-IP>:8006/"
}

variable "proxmox_api_token" {
  type        = string
  description = "USER@PAM!TOKENID=UUID-SECRET"
  sensitive   = true
}

variable "proxmox_node" {
  type    = string
  default = "pve"
}

variable "ssh_public_key" {
  type    = string
  default = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI..."
}

variable "proxmox_ssh_private_key" {
  type        = string
  description = "Private SSH key content for Proxmox disk uploads"
  sensitive   = true
}