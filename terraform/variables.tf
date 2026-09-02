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

variable "ssh_public_keys" {
  type        = list(string)
  description = "SSH public keys authorized to access the provisioned VM"

  validation {
    condition     = length(var.ssh_public_keys) > 0 && alltrue([for key in var.ssh_public_keys : length(trimspace(key)) > 0])
    error_message = "ssh_public_keys must contain at least one non-empty SSH public key."
  }
}

variable "proxmox_ssh_private_key" {
  type        = string
  description = "Private SSH key content for Proxmox disk uploads"
  sensitive   = true
}