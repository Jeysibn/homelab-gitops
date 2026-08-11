output "k3s_node_ip" {
  description = "IPv4 addresses assigned to the K3s VM"
  value       = proxmox_virtual_environment_vm.k3s_node.ipv4_addresses
}

output "k3s_node_id" {
  description = "ID of the created Proxmox VM"
  value       = proxmox_virtual_environment_vm.k3s_node.vm_id
}

output "k3s_node_name" {
  description = "Name of the VM"
  value       = proxmox_virtual_environment_vm.k3s_node.name
}