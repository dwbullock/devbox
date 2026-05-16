output "boxes" {
  description = "All managed boxes with their IPs and SSH commands."
  value = {
    for name, inst in google_compute_instance.box :
    name => {
      ip   = inst.network_interface[0].access_config[0].nat_ip
      zone = inst.zone
      ssh  = "ssh ${local.globals.ssh_user}@${inst.network_interface[0].access_config[0].nat_ip}"
      host = "devbox-${name}.${inst.zone}.${local.globals.project}"
    }
  }
}

output "project" {
  value = local.globals.project
}
