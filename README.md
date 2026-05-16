# devbox

OpenTofu-managed GCP dev VMs, declared in [boxes.yaml](boxes.yaml.example) and
driven from a single `devbox` CLI. Auto-shuts-down on idle.

## Layout

```
boxes.yaml          # your registered boxes (gitignored) — sole source of truth
boxes.yaml.example  # template
.venv/              # auto-created on first `devbox` run (gitignored)
bin/
  devbox            # bash entrypoint — bootstraps venv, dispatches to _devbox.py
  _devbox.py        # Python CLI implementation
  tofu              # OpenTofu binary, project-local (gitignored)
terraform/          # OpenTofu config; reads boxes.yaml via yamldecode
  main.tf
  outputs.tf
  scripts/
    startup.sh.tftpl
    idle-shutdown.sh
```

All tooling is project-local: nothing system-wide except Python itself and gcloud.

## One-time setup

1. **Install prerequisites** (mac, via Homebrew):
   ```bash
   brew install python
   brew install --cask google-cloud-sdk     # if you don't have gcloud
   gcloud auth login && gcloud auth application-default login
   ```
   You'll also need an SSH key pair (`ssh-keygen -t ed25519` if you don't have one).

2. **Drop the OpenTofu binary into bin/** (~109 MB, gitignored):
   ```bash
   curl -fsSL -o /tmp/tofu.zip 'https://github.com/opentofu/opentofu/releases/download/v1.12.0/tofu_1.12.0_darwin_arm64.zip'
   unzip -j /tmp/tofu.zip tofu -d bin/ && chmod +x bin/tofu && rm /tmp/tofu.zip
   ```

3. **Configure boxes.yaml:**
   ```bash
   cp boxes.yaml.example boxes.yaml
   # Edit globals: project, ssh_user, ssh_public_key_path, allowed_ssh_cidrs
   ```

4. **Put `devbox` on your PATH:**
   ```bash
   ln -s "$PWD/bin/devbox" ~/.local/bin/devbox
   ```
   First invocation auto-creates `.venv/` and installs PyYAML into it.

5. **(GPU only) Request quota** in the GCP console: IAM → Quotas → filter for "GPUs" in your region. Approval can take a day.

## Commands

```
devbox list                                   # show registered boxes
devbox status                                 # GCP status of all devbox VMs

devbox newbox <name> --machine TYPE           # register a box in boxes.yaml
devbox deletebox <name>                       # unregister (requires destroyed)

devbox build <name>                           # terraform apply -target for one box
devbox destroy <name>                         # terraform destroy -target (keeps YAML entry)

devbox start <name>                           # gcloud start + wait for SSH
devbox shutdown <name>                        # gcloud stop
devbox vscode <name> [path]                   # start if needed, open VS Code Remote-SSH
```

`newbox`/`deletebox` only edit `boxes.yaml`. `build`/`destroy` reconcile GCP
state with the YAML. `start`/`shutdown` toggle a built VM on and off cheaply.

## Daily use

```bash
devbox newbox work --machine e2-standard-4    # register
devbox build work                             # create in GCP
devbox start work                             # boot, refresh ~/.ssh/config, wait for SSH
# … work …
devbox shutdown work                          # (or let idle-shutdown handle it)
```

VS Code:
```bash
devbox vscode work               # opens /mnt/data on the box; starts it if stopped
devbox vscode work /mnt/data/repo  # open a specific subdirectory
```
Requires the `code` shell command on PATH (VS Code: Command Palette → "Shell Command: Install 'code' command in PATH").

## Adding a GPU box

```bash
devbox newbox ml --machine n1-standard-8 --gpu nvidia-tesla-t4
devbox build ml
```

`--gpu` auto-fills the Deep Learning image and a 100 GB boot disk (overridable
with `--image` / `--boot-disk-size`). Defaults to Spot (~70% cheaper, can be
reclaimed); pass `--on-demand` for stable but pricier instances.

GPU options for `--gpu`: `nvidia-tesla-t4`, `nvidia-l4` (needs a `g2-*` machine),
`nvidia-tesla-v100`, `nvidia-tesla-a100`.

## Layout on the VM

- **`/mnt/data`** — separate data disk, one per box. Put your repos here; survives `start`/`shutdown` (the VM stops, the disk stays). **Destroyed alongside the VM on `devbox destroy`** — back up anything you want to keep before tearing down.
- **Boot disk** — recreated on every `build`. Don't store anything important here.
- **Idle shutdown** — cron runs every 5 min, stops the VM after `idle_threshold_min` minutes idle (default 20: no SSH, low CPU, no GPU activity, no recent VS Code server activity). Set `idle_threshold_min` per-box or under `defaults:` in [boxes.yaml](boxes.yaml), then `devbox build <name>` to apply.

## boxes.yaml shape

```yaml
globals:
  project: my-gcp-project
  zone: us-central1-a
  ssh_user: me
  ssh_public_key_path: ~/.ssh/id_ed25519.pub
  allowed_ssh_cidrs: ["1.2.3.4/32"]

defaults:        # merged into every box, overridable
  data_disk_size_gb: 100
  boot_disk_size_gb: 50
  image: ubuntu-os-cloud/ubuntu-2204-lts
  static_ip: false
  idle_threshold_min: 20

boxes:
  work:
    machine_type: e2-standard-4
  ml:
    machine_type: n1-standard-8
    gpu_type: nvidia-tesla-t4
    gpu_preemptible: true
    image: deeplearning-platform-release/common-cu121-ubuntu-2204
    boot_disk_size_gb: 100
    static_ip: true     # this box gets a stable external IP
```

Per-box keys: `machine_type` (required), `zone`, `data_disk_size_gb`,
`boot_disk_size_gb`, `image`, `static_ip`, `idle_threshold_min`, `gpu_type`,
`gpu_count`, `gpu_preemptible`.

**`static_ip`** (default `false`): when `true`, the box gets a reserved external
IP that survives stop/start. Costs ~$0.005/hr while the VM is stopped. With
`false`, GCE assigns a fresh ephemeral IP each start; `devbox start` re-runs
`gcloud compute config-ssh` so your VS Code / SSH `Host` entries update
automatically. Register with `devbox newbox <name> --machine ... --static-ip`.

## Cost notes

- **CPU (e2-standard-4)**: ~$0.13/hr running, ~$0/hr stopped (you still pay for disks)
- **GPU (n1-standard-8 + T4, Spot)**: ~$0.15/hr running, can be reclaimed
- **GPU (same, on-demand)**: ~$0.55/hr running
- **Persistent disks**: ~$0.10/GB/month regardless of VM state — keep `data_disk_size_gb` reasonable
- **Static IPs** (opt-in via `static_ip: true`): free while attached to a running VM, ~$0.005/hr (~$3.60/mo) when attached to a stopped VM. Default off.

## Troubleshooting

- **SSH hangs after start** — VM is still booting. Wait 30 sec.
- **"Connection refused"** — `gcloud compute config-ssh` again to refresh keys/hostnames.
- **Idle shutdown too aggressive** — bump `idle_threshold_min` in [boxes.yaml](boxes.yaml) (per-box or under `defaults:`) and `devbox build <name>` to push.
- **GPU instance keeps disappearing** — Spot reclamation. Re-register with `--on-demand` (or edit `gpu_preemptible: false` in boxes.yaml).
- **`-target` plan looks weird** — Terraform warns that `-target` is for exceptional use; that's expected here since each `build` targets one box.
