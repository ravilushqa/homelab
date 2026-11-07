"""Keep infrastructure DDNS independent of the filtering DNS resolver."""

import unittest
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).parent.parent.parent
_COMPOSE = _REPO_ROOT / "komodo/stacks/cloudflare-ddns/compose.yaml"


class TestCloudflareDDNSCompose(unittest.TestCase):
    def test_ddns_uses_independent_public_dns(self):
        service = yaml.safe_load(_COMPOSE.read_text())["services"]["cloudflare-ddns"]
        self.assertEqual(service.get("dns"), ["1.1.1.1", "8.8.8.8"])


if __name__ == "__main__":
    unittest.main()
