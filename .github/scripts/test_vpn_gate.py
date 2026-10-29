"""
VPN gate tests: the real audiobooks-arr compose passes; each way to give qBittorrent a route
around xray fails.
"""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent
_GATE_SCRIPT = _HERE / "vpn_gate_check.py"
_STACK_DIR = _REPO_ROOT / "komodo/stacks/audiobooks-arr"

sys.path.insert(0, str(_HERE))
from vpn_gate_check import check_source
import yaml as _yaml


class _GateCase(unittest.TestCase):
    def setUp(self):
        self.doc = _yaml.safe_load((_STACK_DIR / "compose.yaml").read_text())
        self.guard = (_STACK_DIR / "vpn-guard.sh").read_text()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    @property
    def qbit(self):
        return self.doc["services"]["qbittorrent"]

    @property
    def xray(self):
        return json.loads(self.doc["configs"]["xray"]["content"])

    @xray.setter
    def xray(self, cfg):
        self.doc["configs"]["xray"]["content"] = json.dumps(cfg)

    def check(self, raw=None):
        source = self.tmp / "compose.yaml"
        source.write_text(raw if raw is not None else _yaml.safe_dump(self.doc))
        (self.tmp / "vpn-guard.sh").write_text(self.guard)
        return check_source(source)

    def assertFails(self, needle, raw=None):
        errors = self.check(raw)
        self.assertTrue(any(needle in e for e in errors), f"expected {needle!r} in {errors}")


class TestRealCompose(_GateCase):
    def test_repo_compose_passes(self):
        self.assertEqual(check_source(_STACK_DIR / "compose.yaml"), [])

    def test_round_trip_passes(self):
        self.assertEqual(self.check(), [])


class TestQbittorrent(_GateCase):
    def test_extra_network(self):
        self.qbit["networks"] = ["vpn", "traefik"]
        self.assertFails("networks must be exactly")

    def test_network_mapping_form(self):
        self.qbit["networks"] = {"vpn": None, "indexer": None}
        self.assertFails("networks must be exactly")

    def test_no_networks_means_default(self):
        del self.qbit["networks"]
        self.assertFails("networks must be exactly")

    def test_forbidden_keys(self):
        for key, value in [("ports", ["6881:6881"]), ("network_mode", "host"),
                           ("cap_add", ["NET_ADMIN"]), ("privileged", True),
                           ("devices", ["/dev/net/tun"])]:
            with self.subTest(key=key):
                saved = copy.deepcopy(self.qbit)
                self.qbit[key] = value
                self.assertFails(f"`{key}`")
                self.doc["services"]["qbittorrent"] = saved

    def test_missing_service(self):
        del self.doc["services"]["qbittorrent"]
        self.assertFails("services.qbittorrent is missing")


class TestGuard(_GateCase):
    def test_entrypoint_removed(self):
        del self.qbit["entrypoint"]
        self.assertFails("entrypoint")

    def test_guard_not_mounted(self):
        del self.qbit["configs"]
        self.assertFails("mount exactly one config")

    def test_guard_inline_content(self):
        self.doc["configs"]["vpn-guard"] = {"content": "exec /init\n"}
        self.assertFails("must be a `file:` config")

    def test_guard_file_missing(self):
        self.doc["configs"]["vpn-guard"]["file"] = "./nope.sh"
        self.assertFails("does not exist")

    def test_guard_route_check_removed(self):
        self.guard = "#!/bin/sh\nexec /init\n"
        self.assertFails("/proc/net/route")

    def test_guard_does_not_end_with_init(self):
        self.guard += "exec qbittorrent-nox\n"
        self.assertFails("must end with `exec /init`")


class TestVpnNetwork(_GateCase):
    def test_not_internal(self):
        self.doc["networks"]["vpn"]["internal"] = False
        self.assertFails("internal: true")

    def test_internal_string_is_not_true(self):
        self.doc["networks"]["vpn"]["internal"] = "true"
        self.assertFails("internal: true")

    def test_external(self):
        self.doc["networks"]["vpn"]["external"] = True
        self.assertFails("stack-local")

    def test_missing(self):
        del self.doc["networks"]["vpn"]
        self.assertFails("networks.vpn is missing")

    def test_allowlisted_apps_on_vpn(self):
        for name in ("prowlarr", "audiobookrequest", "questarr"):
            self.assertIn("vpn", self.doc["services"][name]["networks"], name)
        self.assertEqual(self.check(), [])

    def test_unlisted_service_on_vpn(self):
        self.doc["services"]["socks-direct"] = {"image": "serjs/go-socks5-proxy", "networks": ["vpn", "traefik"]}
        self.assertFails("not allowlisted")


class TestXray(_GateCase):
    def test_socks_routed_direct(self):
        cfg = self.xray
        cfg["routing"]["rules"].append({"type": "field", "inboundTag": ["socks"], "outboundTag": "direct"})
        self.xray = cfg
        self.assertFails("non-VLESS outbound")

    def test_catch_all_rule_to_direct(self):
        cfg = self.xray
        cfg["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "direct"})
        self.xray = cfg
        self.assertFails("any inbound")

    def test_direct_as_default_outbound(self):
        cfg = self.xray
        cfg["outbounds"].reverse()
        self.xray = cfg
        self.assertFails("first (default) outbound")

    def test_ui_inbound_unpinned(self):
        cfg = self.xray
        for inbound in cfg["inbounds"]:
            if inbound["tag"] == "qbit-ui":
                inbound["settings"]["followRedirect"] = True
        self.xray = cfg
        self.assertFails("non-VLESS outbound")

    def test_balancer(self):
        cfg = self.xray
        cfg["routing"]["balancers"] = [{"tag": "b", "selector": ["vless", "direct"]}]
        self.xray = cfg
        self.assertFails("balancers")

    def test_invalid_json(self):
        self.doc["configs"]["xray"]["content"] = "{"
        self.assertFails("not valid JSON")


class TestParsing(_GateCase):
    def test_duplicate_keys_rejected(self):
        raw = (_STACK_DIR / "compose.yaml").read_text().replace(
            "  qbittorrent:\n", "  qbittorrent:\n    networks: [traefik]\n", 1)
        self.assertFails("Duplicate key", raw=raw)


class TestCLI(unittest.TestCase):
    def test_default_source_passes(self):
        result = subprocess.run([sys.executable, str(_GATE_SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestDeployWorkflow(unittest.TestCase):
    def test_gate_runs_before_audiobooks_arr_deploy(self):
        wf = (_REPO_ROOT / ".github/workflows/komodo-deploy.yaml").read_text()
        gate = wf.find("vpn_gate_check.py")
        deploy = wf.find('deploy_and_wait "$STACK"')
        self.assertTrue(-1 < gate < deploy)
        self.assertIn('[ "$STACK" = "audiobooks-arr" ]', wf)


if __name__ == "__main__":
    unittest.main()
