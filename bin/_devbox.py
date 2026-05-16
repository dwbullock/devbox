#!/usr/bin/env python3
"""devbox — manage GCP dev VMs declared in boxes.yaml."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

REPO = Path(__file__).resolve().parent.parent
BOXES_PATH = REPO / "boxes.yaml"
TF_DIR = REPO / "terraform"
TOFU = str(REPO / "bin" / "tofu")

# Sensible defaults the CLI fills in when --gpu is set and the user didn't override.
GPU_DEFAULTS = {
    "image": "deeplearning-platform-release/common-cu121-ubuntu-2204",
    "boot_disk_size_gb": 100,
}


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_boxes() -> dict:
    if not BOXES_PATH.exists():
        die(f"boxes.yaml not found at {BOXES_PATH}. Copy boxes.yaml.example and edit it.")
    with open(BOXES_PATH) as f:
        return yaml.safe_load(f) or {}


def save_boxes(data: dict) -> None:
    with open(BOXES_PATH, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


def require_globals(data: dict) -> dict:
    g = data.get("globals") or {}
    missing = [k for k in ("project", "ssh_user", "ssh_public_key_path") if k not in g]
    if missing:
        die(f"boxes.yaml missing globals.{', globals.'.join(missing)}")
    return g


def require_box(data: dict, name: str) -> dict:
    boxes = data.get("boxes") or {}
    if name not in boxes:
        die(f"No box named '{name}'. Register it with: devbox newbox {name} --machine TYPE")
    return boxes[name]


def box_zone(data: dict, name: str) -> str:
    return (data["boxes"][name].get("zone")
            or data.get("globals", {}).get("zone")
            or "us-central1-a")


def tf_init_if_needed() -> None:
    if not (TF_DIR / ".terraform").exists():
        print("→ tofu init (first run)…")
        subprocess.run([TOFU, "init"], cwd=TF_DIR, check=True)


def tf(args: list[str]) -> None:
    tf_init_if_needed()
    subprocess.run([TOFU, *args], cwd=TF_DIR, check=True)


def gcloud_quiet(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["gcloud", *args], **kwargs)


def effective_box(data: dict, name: str) -> dict:
    """Box spec merged with defaults — mirrors Terraform's merge."""
    return {**(data.get("defaults") or {}), **(data["boxes"][name] or {})}


def box_targets(data: dict, name: str) -> list[str]:
    """Terraform -target args for a single box's resources."""
    targets = [
        f'google_compute_instance.box["{name}"]',
        f'google_compute_disk.data["{name}"]',
    ]
    if effective_box(data, name).get("static_ip", False):
        targets.append(f'google_compute_address.ip["{name}"]')
    return targets


# ---- subcommands ----------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    data = load_boxes()
    boxes = data.get("boxes") or {}
    if not boxes:
        print("(no boxes registered)")
        return
    for name, spec in boxes.items():
        gpu = f"  gpu={spec['gpu_type']}" if "gpu_type" in spec else ""
        zone = spec.get("zone", data.get("globals", {}).get("zone", "?"))
        print(f"  {name:<16} {spec.get('machine_type', '?'):<20} zone={zone}{gpu}")


def cmd_newbox(args: argparse.Namespace) -> None:
    data = load_boxes()
    boxes = data.setdefault("boxes", {})
    if args.name in boxes:
        die(f"Box '{args.name}' already registered. Delete it first with: devbox deletebox {args.name}")

    spec: dict = {"machine_type": args.machine}
    if args.gpu:
        spec["gpu_type"] = args.gpu
        spec["gpu_preemptible"] = not args.on_demand
    if args.zone:
        spec["zone"] = args.zone
    if args.data_disk_size:
        spec["data_disk_size_gb"] = args.data_disk_size
    if args.boot_disk_size:
        spec["boot_disk_size_gb"] = args.boot_disk_size
    if args.image:
        spec["image"] = args.image
    if args.static_ip:
        spec["static_ip"] = True

    # If GPU and user didn't override image/boot_disk_size, fill in DL defaults
    # so the box actually has CUDA + enough boot disk.
    if args.gpu:
        for k, v in GPU_DEFAULTS.items():
            spec.setdefault(k, v)

    boxes[args.name] = spec
    save_boxes(data)
    print(f"✓ Registered '{args.name}'. Create it with: devbox build {args.name}")


def cmd_deletebox(args: argparse.Namespace) -> None:
    data = load_boxes()
    boxes = data.get("boxes") or {}
    if args.name not in boxes:
        die(f"No box named '{args.name}'.")

    # Refuse if the GCP instance still exists, unless --force.
    g = data.get("globals") or {}
    if "project" in g:
        zone = box_zone(data, args.name)
        r = gcloud_quiet(
            ["compute", "instances", "describe", f"devbox-{args.name}",
             "--zone", zone, "--project", g["project"], "--format=value(status)"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and not args.force:
            die(
                f"Instance devbox-{args.name} still exists in GCP ({r.stdout.strip()}). "
                f"Run 'devbox destroy {args.name}' first, or pass --force to unregister anyway."
            )

    del boxes[args.name]
    save_boxes(data)
    print(f"✓ Unregistered '{args.name}'.")


def cmd_build(args: argparse.Namespace) -> None:
    data = load_boxes()
    require_box(data, args.name)
    tf_args = ["apply"]
    for t in box_targets(data, args.name):
        tf_args.extend(["-target", t])
    if args.auto_approve:
        tf_args.append("-auto-approve")
    tf(tf_args)


def cmd_destroy(args: argparse.Namespace) -> None:
    data = load_boxes()
    require_box(data, args.name)
    tf_args = ["destroy"]
    for t in box_targets(data, args.name):
        tf_args.extend(["-target", t])
    if args.auto_approve:
        tf_args.append("-auto-approve")
    tf(tf_args)


def ensure_running(data: dict, name: str) -> str:
    """Start the named box if needed, wait for SSH, return its ssh host string."""
    require_box(data, name)
    g = require_globals(data)
    zone = box_zone(data, name)
    instance = f"devbox-{name}"

    print(f"→ Starting {instance} in {zone}…")
    r = gcloud_quiet(
        ["compute", "instances", "describe", instance,
         "--zone", zone, "--project", g["project"], "--format=value(status)"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        die(f"✗ {instance} doesn't exist in GCP. Run: devbox build {name}")
    status = r.stdout.strip()
    if status == "RUNNING":
        print("✓ Already running")
    else:
        gcloud_quiet(
            ["compute", "instances", "start", instance,
             "--zone", zone, "--project", g["project"], "--quiet"],
            check=True,
        )

    print("→ Refreshing SSH config…")
    gcloud_quiet(["compute", "config-ssh", "--project", g["project"], "--quiet"],
                 stdout=subprocess.DEVNULL)

    host = f"{instance}.{zone}.{g['project']}"
    print(f"→ Waiting for SSH on {host}…")
    for _ in range(60):
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", host, "true"],
            capture_output=True,
        )
        if r.returncode == 0:
            print(f"✓ Ready: {host}")
            return host
        time.sleep(2)
    die("✗ SSH didn't come up in 2 minutes. Check: gcloud compute instances describe " + instance)


def cmd_start(args: argparse.Namespace) -> None:
    data = load_boxes()
    host = ensure_running(data, args.name)
    print()
    print("Connect with:")
    print(f"  ssh {host}")
    print(f"  devbox vscode {args.name}")


def cmd_vscode(args: argparse.Namespace) -> None:
    if not shutil.which("code"):
        die("✗ `code` not on PATH. In VS Code: Command Palette → 'Shell Command: Install \"code\" command in PATH'.")
    data = load_boxes()
    host = ensure_running(data, args.name)
    remote_path = args.path or "/mnt/data"
    print(f"→ Launching VS Code on {host}:{remote_path}…")
    subprocess.run(["code", "--remote", f"ssh-remote+{host}", remote_path], check=True)


def cmd_shutdown(args: argparse.Namespace) -> None:
    data = load_boxes()
    require_box(data, args.name)
    g = require_globals(data)
    zone = box_zone(data, args.name)
    instance = f"devbox-{args.name}"
    print(f"→ Stopping {instance}…")
    gcloud_quiet(
        ["compute", "instances", "stop", instance,
         "--zone", zone, "--project", g["project"], "--quiet"],
        check=True,
    )
    print("✓ Stopped")


def cmd_status(args: argparse.Namespace) -> None:
    data = load_boxes()
    g = require_globals(data)
    gcloud_quiet([
        "compute", "instances", "list",
        "--project", g["project"],
        "--filter=name~'^devbox-'",
        "--format=table(name,zone.basename(),status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP:label=EXTERNAL_IP)",
    ])


def cmd_help(args: argparse.Namespace, parser: argparse.ArgumentParser,
             subparsers: argparse._SubParsersAction) -> None:
    if not args.topic:
        parser.print_help()
        return
    subp = subparsers.choices.get(args.topic)
    if subp is None:
        die(f"No such command: {args.topic}. Try: devbox help")
    subp.print_help()


# ---- argparse -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devbox", description="Manage GCP dev VMs from boxes.yaml.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="List boxes registered in boxes.yaml.")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("newbox", help="Register a new box in boxes.yaml.")
    sp.add_argument("name")
    sp.add_argument("--machine", required=True, help="GCP machine type (e.g. e2-standard-4)")
    sp.add_argument("--gpu", help="GPU accelerator type (e.g. nvidia-tesla-t4)")
    sp.add_argument("--on-demand", action="store_true",
                    help="With --gpu, disable Spot (more expensive, but won't be reclaimed)")
    sp.add_argument("--zone", help="Override globals.zone for this box")
    sp.add_argument("--data-disk-size", type=int, help="Persistent data disk size (GB)")
    sp.add_argument("--boot-disk-size", type=int, help="Boot disk size (GB)")
    sp.add_argument("--image", help="Boot image (defaults to globals/defaults)")
    sp.add_argument("--static-ip", action="store_true",
                    help="Reserve a static external IP (costs ~$0.005/hr while VM is stopped)")
    sp.set_defaults(func=cmd_newbox)

    sp = sub.add_parser("deletebox", help="Unregister a box (refuses if GCP instance still exists).")
    sp.add_argument("name")
    sp.add_argument("--force", action="store_true", help="Unregister even if GCP instance still exists")
    sp.set_defaults(func=cmd_deletebox)

    sp = sub.add_parser("build", help="Create GCP resources for a box (terraform apply -target).")
    sp.add_argument("name")
    sp.add_argument("--auto-approve", action="store_true")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("destroy", help="Destroy a box's GCP resources (keeps YAML entry).")
    sp.add_argument("name")
    sp.add_argument("--auto-approve", action="store_true")
    sp.set_defaults(func=cmd_destroy)

    sp = sub.add_parser("start", help="Start a stopped box VM and wait for SSH.")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("shutdown", help="Stop a running box VM.")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_shutdown)

    sp = sub.add_parser("vscode", help="Open VS Code Remote-SSH on a box (starts it first if needed).")
    sp.add_argument("name")
    sp.add_argument("path", nargs="?", default="/mnt/data",
                    help="Remote directory to open (default: /mnt/data)")
    sp.set_defaults(func=cmd_vscode)

    sp = sub.add_parser("status", help="Show GCP status of all devbox VMs.")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("help", help="Show usage (optionally for a specific command).")
    sp.add_argument("topic", nargs="?", help="Subcommand name (omit for top-level help)")
    sp.set_defaults(func=lambda a: cmd_help(a, p, sub))

    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
