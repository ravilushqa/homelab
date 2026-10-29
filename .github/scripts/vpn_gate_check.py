#!/usr/bin/env python3
"""
VPN gate: qBittorrent in komodo/stacks/audiobooks-arr may reach the internet only through xray.

Usage:
  python3 vpn_gate_check.py [--source komodo/stacks/audiobooks-arr/compose.yaml]

Checks the compose file (no Docker needed):
  - qbittorrent is only on the `vpn` network, which is `internal: true` and stack-local
  - qbittorrent has no ports, network_mode, cap_add, privileged or devices
  - qbittorrent starts through vpn-guard.sh (refuses to start with a route out)
  - only allowlisted services share the `vpn` network (each one is a possible way out)
  - xray sends the socks inbound to the VLESS outbound; `direct` is only used by
    dokodemo-door inbounds pinned to qbittorrent (the web UI)

Exit:
  0  All checks passed
  1  One or more checks failed (block merge / deploy)
  2  Usage error
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_SOURCE = _REPO_ROOT / "komodo/stacks/audiobooks-arr/compose.yaml"

# Services allowed on the vpn network. Adding one means checking it can't forward qBittorrent's
# traffic out directly, then adding it here.
_VPN_ALLOWED_SERVICES = {"xray", "qbittorrent", "prowlarr", "audiobookrequest"}

_FORBIDDEN_QBIT_KEYS = ("ports", "network_mode", "cap_add", "privileged", "devices")

_GUARD_ENTRYPOINT = ["/bin/sh", "/vpn-guard.sh"]
_GUARD_TARGET = "/vpn-guard.sh"
_GUARD_REQUIRED = ("/proc/net/route", "/proc/net/ipv6_route", "exit 1", "exec /init")


class _NoDupKeyLoader(_yaml.SafeLoader):
    pass


def _no_dup_mapping_constructor(loader, node):
    loader.flatten_mapping(node)
    pairs = loader.construct_pairs(node)
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise _yaml.YAMLError(f"Duplicate key in YAML mapping: {k!r}")
        seen[k] = v
    return seen


_NoDupKeyLoader.add_constructor(
    _yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_dup_mapping_constructor,
)


def _service_networks(service: dict) -> set:
    networks = service.get("networks")
    if networks is None:
        # Compose puts a service without networks on the stack's `default` network
        return {"default"}
    if isinstance(networks, dict):
        return set(networks)
    return set(networks)


def _check_qbittorrent(doc: dict, compose_dir: Path) -> list:
    errors = []
    qbit = doc["services"].get("qbittorrent")
    if not isinstance(qbit, dict):
        return ["services.qbittorrent is missing"]

    networks = _service_networks(qbit)
    if networks != {"vpn"}:
        errors.append(f"qbittorrent networks must be exactly ['vpn'], got {sorted(networks)}")
    for key in _FORBIDDEN_QBIT_KEYS:
        if key in qbit:
            errors.append(f"qbittorrent must not set `{key}`")

    if qbit.get("entrypoint") != _GUARD_ENTRYPOINT:
        errors.append(f"qbittorrent entrypoint must be {_GUARD_ENTRYPOINT} (vpn-guard.sh)")
    sources = [c.get("source") for c in qbit.get("configs") or []
               if isinstance(c, dict) and c.get("target") == _GUARD_TARGET]
    if len(sources) != 1:
        errors.append(f"qbittorrent must mount exactly one config at {_GUARD_TARGET}")
        return errors

    config = (doc.get("configs") or {}).get(sources[0])
    if not isinstance(config, dict) or not isinstance(config.get("file"), str):
        errors.append(f"configs.{sources[0]} must be a `file:` config (vpn-guard.sh)")
        return errors
    guard = compose_dir / config["file"]
    if not guard.is_file():
        errors.append(f"guard script {guard} does not exist")
        return errors
    text = guard.read_text()
    for needle in _GUARD_REQUIRED:
        if needle not in text:
            errors.append(f"guard script {guard.name} must contain {needle!r}")
    if text.rstrip().splitlines()[-1].strip() != "exec /init":
        errors.append(f"guard script {guard.name} must end with `exec /init`")
    return errors


def _check_vpn_network(doc: dict) -> list:
    errors = []
    vpn = (doc.get("networks") or {}).get("vpn")
    if not isinstance(vpn, dict):
        return ["networks.vpn is missing"]
    if vpn.get("internal") is not True:
        errors.append("networks.vpn must have `internal: true`")
    for key in ("external", "name"):
        if key in vpn:
            errors.append(f"networks.vpn must be stack-local (no `{key}`)")

    on_vpn = {name for name, svc in doc["services"].items()
              if isinstance(svc, dict) and "vpn" in _service_networks(svc)}
    extra = on_vpn - _VPN_ALLOWED_SERVICES
    if extra:
        errors.append(f"services not allowlisted for the vpn network: {sorted(extra)}")
    return errors


def _check_xray(doc: dict) -> list:
    xray_config = (doc.get("configs") or {}).get("xray")
    if not isinstance(xray_config, dict) or not isinstance(xray_config.get("content"), str):
        return ["configs.xray.content is missing"]
    try:
        cfg = json.loads(xray_config["content"])
    except json.JSONDecodeError as e:
        return [f"configs.xray.content is not valid JSON: {e}"]

    errors = []
    inbounds = {i.get("tag"): i for i in cfg.get("inbounds") or []}
    outbounds = cfg.get("outbounds") or []
    protocols = {o.get("tag"): o.get("protocol") for o in outbounds}
    routing = cfg.get("routing") or {}

    if inbounds.get("socks", {}).get("protocol") != "socks":
        errors.append("xray must have a socks inbound tagged `socks`")
    # Traffic no rule matches goes to the first outbound
    if not outbounds or outbounds[0].get("protocol") != "vless":
        errors.append("xray's first (default) outbound must be vless")
    if routing.get("balancers"):
        errors.append("xray routing must not use balancers")

    # Only dokodemo-door inbounds pinned to the qBittorrent web UI may skip VLESS
    pinned = {tag for tag, i in inbounds.items()
              if i.get("protocol") == "dokodemo-door"
              and (i.get("settings") or {}).get("address") == "qbittorrent"
              and not (i.get("settings") or {}).get("followRedirect")}
    for n, rule in enumerate(routing.get("rules") or []):
        if "balancerTag" in rule:
            errors.append(f"xray routing rule {n} must not use balancerTag")
            continue
        if protocols.get(rule.get("outboundTag")) == "vless":
            continue
        tags = rule.get("inboundTag")
        if not tags or not set(tags) <= pinned:
            errors.append(
                f"xray routing rule {n} sends {tags or 'any inbound'} to non-VLESS outbound "
                f"{rule.get('outboundTag')!r}; only dokodemo-door inbounds pinned to qbittorrent may"
            )
    return errors


def check_source(source: Path) -> list:
    """Return a list of failures; empty means the gate passes."""
    try:
        doc = _yaml.load(source.read_text(), Loader=_NoDupKeyLoader)
    except (OSError, _yaml.YAMLError) as e:
        return [f"cannot parse {source}: {e}"]
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return [f"{source} has no services mapping"]
    return (_check_qbittorrent(doc, source.parent)
            + _check_vpn_network(doc)
            + _check_xray(doc))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    args = parser.parse_args(argv)

    errors = check_source(args.source)
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        print("VPN gate FAILED: qBittorrent could reach the internet without the VPN", file=sys.stderr)
        return 1
    print("VPN gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
