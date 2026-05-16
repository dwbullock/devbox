terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

locals {
  config    = yamldecode(file("${path.module}/../boxes.yaml"))
  globals   = local.config.globals
  defaults  = lookup(local.config, "defaults", {})
  raw_boxes = lookup(local.config, "boxes", {})
  boxes = {
    for name, spec in local.raw_boxes :
    name => merge(local.defaults, spec)
  }
  idle_shutdown_b64 = base64encode(file("${path.module}/scripts/idle-shutdown.sh"))
}

provider "google" {
  project = local.globals.project
  zone    = local.globals.zone
}

# Service account so VMs can stop themselves on idle.
resource "google_service_account" "devbox" {
  account_id   = "devbox-sa"
  display_name = "Devbox VM service account"
}

resource "google_project_iam_member" "devbox_compute" {
  project = local.globals.project
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.devbox.email}"
}

# SSH firewall, shared across all boxes.
resource "google_compute_firewall" "ssh" {
  name    = "devbox-allow-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = local.globals.allowed_ssh_cidrs
  target_tags   = ["devbox"]
}

# Per-box data disk. Destroyed alongside the instance on `devbox destroy`.
resource "google_compute_disk" "data" {
  for_each = local.boxes

  name = "devbox-${each.key}-data"
  type = "pd-balanced"
  size = each.value.data_disk_size_gb
  zone = lookup(each.value, "zone", local.globals.zone)
}

# Per-box static external IP — only created when the box opts in.
# Ephemeral IPs are free while the VM runs; static IPs cost ~$0.005/hr while
# the VM is stopped, but give a stable address across start/stop cycles.
resource "google_compute_address" "ip" {
  for_each = { for k, v in local.boxes : k => v if lookup(v, "static_ip", false) }

  name   = "devbox-${each.key}-ip"
  region = lookup(each.value, "region", replace(lookup(each.value, "zone", local.globals.zone), "/-[a-z]$/", ""))
}

resource "google_compute_instance" "box" {
  for_each = local.boxes

  name         = "devbox-${each.key}"
  machine_type = each.value.machine_type
  zone         = lookup(each.value, "zone", local.globals.zone)
  tags         = ["devbox"]

  allow_stopping_for_update = true

  # GPU boxes can't live-migrate; honored only when guest_accelerator is set.
  dynamic "scheduling" {
    for_each = lookup(each.value, "gpu_type", null) != null ? [1] : []
    content {
      on_host_maintenance = "TERMINATE"
      automatic_restart   = true
      preemptible         = lookup(each.value, "gpu_preemptible", false)
      provisioning_model  = lookup(each.value, "gpu_preemptible", false) ? "SPOT" : "STANDARD"
    }
  }

  dynamic "guest_accelerator" {
    for_each = lookup(each.value, "gpu_type", null) != null ? [1] : []
    content {
      type  = each.value.gpu_type
      count = lookup(each.value, "gpu_count", 1)
    }
  }

  boot_disk {
    initialize_params {
      image = each.value.image
      size  = each.value.boot_disk_size_gb
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data[each.key].id
    device_name = "data"
  }

  network_interface {
    network = "default"
    access_config {
      # When static_ip=false, nat_ip is null and GCE assigns an ephemeral IP.
      nat_ip = try(google_compute_address.ip[each.key].address, null)
    }
  }

  metadata = merge(
    {
      ssh-keys = "${local.globals.ssh_user}:${file(pathexpand(local.globals.ssh_public_key_path))}"
      startup-script = templatefile("${path.module}/scripts/startup.sh.tftpl", {
        ssh_user           = local.globals.ssh_user
        idle_shutdown_b64  = local.idle_shutdown_b64
        idle_threshold_min = lookup(each.value, "idle_threshold_min", 20)
      })
    },
    lookup(each.value, "gpu_type", null) != null ? { install-nvidia-driver = "True" } : {},
  )

  service_account {
    email  = google_service_account.devbox.email
    scopes = ["cloud-platform"]
  }
}
