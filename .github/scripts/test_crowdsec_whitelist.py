"""
CrowdSec whitelist tests: only vova-medcenter's failed logins (POST 401 on exactly
/api/v1/auth/login) are ignored; every other Traefik event still reaches the scenarios.

The engine tests replay Traefik access-log lines through the real CrowdSec image pinned in
the traefik compose, with the whitelist bind-mounted exactly as the compose mounts it. They
need Docker and outbound internet (the container installs the hub collections); no ports,
volumes or host networking. Run:

  python3 -m unittest discover -s .github/scripts -p test_crowdsec_whitelist.py -v
"""

import shutil
import subprocess
import time
import unittest
import uuid
from pathlib import Path

import yaml as _yaml

_REPO_ROOT = Path(__file__).parent.parent.parent
_STACK_DIR = _REPO_ROOT / "komodo/stacks/traefik"
_WHITELIST = _STACK_DIR / "crowdsec/vova-medcenter-login-whitelist.yaml"
_WHITELIST_TARGET = "/etc/crowdsec/parsers/s02-enrich/vova-medcenter-login-whitelist.yaml"

_ROUTER = "vova-medcenter@docker"
_LOGIN = "/api/v1/auth/login"


def _crowdsec_service():
    return _yaml.safe_load((_STACK_DIR / "compose.yaml").read_text())["services"]["crowdsec"]


def _whitelist_mount():
    """The compose volume entry that mounts the whitelist, or None."""
    for volume in _crowdsec_service()["volumes"]:
        if volume.split(":")[1] == _WHITELIST_TARGET:
            return volume
    return None


def _access_log(verb, path, status, router=_ROUTER):
    """A Traefik access-log line in the default (CLF) format the live Traefik writes."""
    return (f'203.0.113.7 - - [03/Nov/2026:10:00:00 +0000] "{verb} {path} HTTP/2.0" {status} 25 '
            f'"-" "Mozilla/5.0" 123 "{router}" "http://172.18.0.5:8000" 12ms')


class TestCompose(unittest.TestCase):
    def test_whitelist_mounted_read_only(self):
        self.assertEqual(_whitelist_mount(),
                         f"./crowdsec/{_WHITELIST.name}:{_WHITELIST_TARGET}:ro")

    def test_whitelist_is_expression_only(self):
        # No ip/cidr entries: an IP whitelist would also drop that client's probes and scans
        whitelist = _yaml.safe_load(_WHITELIST.read_text())["whitelist"]
        self.assertEqual(set(whitelist), {"reason", "expression"})
        self.assertEqual(len(whitelist["expression"]), 1)


def _docker_available():
    return shutil.which("docker") and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@unittest.skipUnless(_docker_available(), "needs Docker")
class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service = _crowdsec_service()
        cls.container = f"crowdsec-whitelist-test-{uuid.uuid4().hex[:8]}"
        cmd = ["docker", "run", "-d", "--rm", "--name", cls.container,
               "-e", f"COLLECTIONS={service['environment']['COLLECTIONS']}",
               # No data volume: this throwaway container only replays lines
               "-e", "CROWDSEC_BYPASS_DB_VOLUME_CHECK=true"]
        mount = _whitelist_mount()
        if mount:
            source, target, mode = mount.split(":")
            cmd += ["-v", f"{(_STACK_DIR / source).resolve()}:{target}:{mode}"]
        subprocess.run(cmd + [service["image"]], check=True, capture_output=True)
        # explain needs the local API, which is up once the collections are installed
        for _ in range(90):
            if subprocess.run(["docker", "exec", cls.container, "cscli", "lapi", "status"],
                              capture_output=True).returncode == 0:
                return
            time.sleep(2)
        cls.tearDownClass()
        raise RuntimeError("CrowdSec test container did not become ready")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.container], capture_output=True)

    def explain(self, line):
        result = subprocess.run(
            ["docker", "exec", self.container, "cscli", "explain", "--color", "no",
             "--type", "traefik", "--log", line],
            capture_output=True, text=True, check=True)
        # Fail on unparsed lines rather than mistake them for "not whitelisted"
        self.assertIn("parser success", result.stdout)
        return result.stdout

    def assertWhitelisted(self, line):
        out = self.explain(line)
        self.assertIn("ignored by whitelist", out)
        self.assertNotIn("Scenarios", out)

    def assertNotWhitelisted(self, line, scenario=None):
        out = self.explain(line)
        self.assertNotIn("ignored by whitelist", out)
        if scenario:
            self.assertIn(scenario, out)

    def test_failed_login_ignored(self):
        self.assertWhitelisted(_access_log("POST", _LOGIN, 401))

    def test_other_status_kept(self):
        for status in (400, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.assertNotWhitelisted(_access_log("POST", _LOGIN, status))

    def test_other_verb_kept(self):
        # Not whitelisted only: http-generic-401-bf itself counts POSTs alone
        for verb in ("GET", "PUT", "OPTIONS"):
            with self.subTest(verb=verb):
                self.assertNotWhitelisted(_access_log(verb, _LOGIN, 401))

    def test_other_path_kept(self):
        for path in (f"{_LOGIN}/", f"{_LOGIN}?next=/admin", "/API/v1/auth/login",
                     "/api/v1/auth/login2", "/api/v1/auth/refresh", "/api/v1/users"):
            with self.subTest(path=path):
                self.assertNotWhitelisted(_access_log("POST", path, 401),
                                          "LePresidente/http-generic-401-bf")

    def test_other_router_kept(self):
        for router in ("vova-medcenter-api@docker", "pocketid@docker", "vova-medcenter@file"):
            with self.subTest(router=router):
                self.assertNotWhitelisted(_access_log("POST", _LOGIN, 401, router),
                                          "LePresidente/http-generic-401-bf")

    def test_probes_on_vova_kept(self):
        self.assertNotWhitelisted(_access_log("GET", "/.env", 404),
                                  "crowdsecurity/http-sensitive-files")


if __name__ == "__main__":
    unittest.main()
