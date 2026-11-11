"""proxy-fast on/off/status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from . import __version__
from .cloud import Cloud, ProxyError, load_credentials
from .config import apply_config
from .manager import Manager, Settings
from .state import Store, default_state_root, locked


def parser():
    result = argparse.ArgumentParser(description="Create a spot ECS proxy, show subscriptions, or release owned resources.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, help="INI configuration file (default: proxy-fast.ini)")
    actions = result.add_subparsers(dest="action", required=True)
    for action in ("on", "off", "status"):
        sub = actions.add_parser(action)
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="INI configuration file")
        sub.add_argument("--profile", help="Existing Alibaba CLI AK/STS profile")
        sub.add_argument("--state-dir", type=Path, help="Local state root (default: per-user application state)")
        sub.add_argument("--json", action="store_true", help="Machine-readable JSON output")
        sub.add_argument("--timeout", type=int, default=None, help="Wait limit in seconds (30..3600; default 600)")
        if action == "on":
            sub.add_argument("--region", default=None, help="Default: eu-west-2 (Paris)")
            sub.add_argument("--zone", help="Optional exact availability zone")
            sub.add_argument("--instance-type", help="Optional in-stock 1–2 vCPU / 1–4 GiB X86 instance type")
            sub.add_argument("--image-id", help="Optional official Ubuntu 24.04/22.04 X86 image")
            sub.add_argument("--price-limit", default=None,
                             help="Max hourly compute bid in account billing currency; default 0.10. Disk/traffic extra.")
            sub.add_argument("--bandwidth", type=int, default=None, help="Outbound cap Mbit/s (1..100), billed by traffic")
            sub.add_argument("--protection", type=int, choices=(0, 1), default=None, help="Spot protection period; default 1 hour")
            sub.add_argument("--disk-size", type=int, default=None, help="System disk size GiB (20..2048; default 20)")
            sub.add_argument("--disk-category", choices=("auto", "cloud_essd", "cloud_ssd", "cloud_efficiency"), default=None,
                             help="System disk category; default auto selects a compatible in-stock category")
    return result


def emit(result, action, as_json=False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["status"] == "ready":
        # stdout stays useful for piping; progress and diagnostics use stderr.
        print("Mihomo:   " + result["subscriptions"]["mihomo"])
        print("sing-box: " + result["subscriptions"]["sing_box"])
    else:
        print("Status: " + result["status"])
        if result.get("instance_id"):
            print("Instance: " + result["instance_id"])
        if result.get("public_ip"):
            print("Public IPv4: " + result["public_ip"])
        if result.get("last_error"):
            print("Last error: " + result["last_error"])


def main(argv=None, *, cloud_factory=Cloud, bootstrap=None):
    arguments = parser().parse_args(argv)
    started = time.monotonic()
    def log(message):
        print(f"[{time.monotonic()-started:.0f}s] {message}", file=sys.stderr, flush=True)
    store = None
    try:
        config_path = apply_config(arguments)
        if config_path: log(f"Configuration: {config_path}")
        # Validate deployment settings before any cloud call or credential lookup.
        creation_settings = Settings(timeout=arguments.timeout)
        if arguments.action == "on":
            creation_settings = Settings(region=arguments.region or "eu-west-2", timeout=arguments.timeout,
                price_limit=arguments.price_limit, bandwidth=arguments.bandwidth, protection_hours=arguments.protection,
                zone=arguments.zone, instance_type=arguments.instance_type, image_id=arguments.image_id,
                disk_size=arguments.disk_size, disk_category=arguments.disk_category)
        creation_settings.validate()
        credentials = load_credentials(arguments.profile)
        requested = getattr(arguments, "region", None)
        initial_region = requested or "eu-west-2"
        cloud = cloud_factory(credentials, initial_region)
        account = cloud.account_id()
        root = (arguments.state_dir or default_state_root()) / account
        with locked(root):
            store = Store(root, account)
            existing_region = store.data.get("region")
            if arguments.action == "on" and requested and existing_region and requested != existing_region:
                raise ProxyError(f"Existing resources are in {existing_region}. Run off before changing region.")
            region = existing_region or initial_region
            cloud = cloud_factory(credentials, region)
            settings = creation_settings
            settings.region = region
            settings.validate()
            if arguments.action == "on":
                store.data["region"] = region
                store.save()
            path = Path(bootstrap) if bootstrap else Path(__file__).resolve().parent.parent / "proxy-fast.sh"
            if not bootstrap and not path.is_file():
                path = Path(__file__).resolve().parent / "bootstrap.sh"
            manager = Manager(cloud, store, settings, path, log=log)
            try:
                result = getattr(manager, arguments.action)()
            except (ProxyError, KeyboardInterrupt) as exc:
                # Keep all resource IDs and pending create requests so off/retry remains possible.
                store.data["last_error"] = "Interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
                store.save()
                raise
            emit(result, arguments.action, arguments.json)
            log(f"{arguments.action}: {result['status']}; region={region}; state={store.file}")
            return 0
    except ProxyError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        if store:
            print(f"Recovery state: {store.file}; use proxy-fast off to release owned resources.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Resource state is retained; run status or off.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: local configuration or API response could not be processed ({type(exc).__name__}).", file=sys.stderr)
        if store:
            print(f"Keep {store.file}; run status/off before deleting local state.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
