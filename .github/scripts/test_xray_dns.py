"""Regression checks for cached VPN endpoint DNS without weakening VPN isolation."""

import json
import unittest
from pathlib import Path

import yaml as _yaml

from vpn_gate_check import check_source


_COMPOSE = Path(__file__).resolve().parent.parent.parent / "komodo/stacks/audiobooks-arr/compose.yaml"


class TestXrayDNS(unittest.TestCase):
    def setUp(self):
        self.doc = _yaml.safe_load(_COMPOSE.read_text())
        # Compose placeholders remain quoted JSON strings; no interpolation needed.
        self.xray = json.loads(self.doc["configs"]["xray"]["content"])

    def test_cached_local_doh_uses_ipv4(self):
        self.assertEqual(self.xray.get("dns"), {
            "servers": ["https+local://1.1.1.1/dns-query", "https+local://8.8.8.8/dns-query"],
            "queryStrategy": "UseIPv4",
            "disableCache": False,
        })

    def test_only_vless_forces_ipv4_resolution(self):
        outbounds = self.xray["outbounds"]
        self.assertEqual([o["tag"] for o in outbounds], ["vless", "direct"])
        self.assertEqual(outbounds[0]["protocol"], "vless")
        self.assertEqual(outbounds[0]["streamSettings"].get("sockopt"), {
            "domainStrategy": "ForceIPv4",
        })
        self.assertEqual(outbounds[1], {"tag": "direct", "protocol": "freedom"})

    def test_reality_and_user_references_preserved(self):
        vless = self.xray["outbounds"][0]
        self.assertEqual(vless["settings"], {"vnext": [{
            "address": "${VLESS_HOST}", "port": 443,
            "users": [{"id": "${VLESS_UUID}", "encryption": "none", "flow": "xtls-rprx-vision"}],
        }]})
        stream = vless["streamSettings"]
        self.assertEqual(stream["network"], "tcp")
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(stream["realitySettings"], {
            "serverName": "${VLESS_SNI}", "fingerprint": "firefox",
            "publicKey": "${VLESS_PBK}", "shortId": "",
        })

    def test_dns_fix_preserves_vpn_isolation(self):
        # Reuse the gate's internal-network, guard and routing checks.
        self.assertEqual(check_source(_COMPOSE), [])
        for name, service in self.doc["services"].items():
            with self.subTest(service=name):
                self.assertNotIn("dns", service)


if __name__ == "__main__":
    unittest.main()
