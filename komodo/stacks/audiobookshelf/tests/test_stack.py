import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

STACK_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = STACK_DIR / "compose.yaml"
STACKS_TOML = STACK_DIR.parents[1] / "stacks.toml"

IMAGE = "ghcr.io/advplyr/audiobookshelf:2.37.2"
BRANCH = "feat/audiobookshelf-smb"
HOST = "audiobooks.ravil.space"


def render(traefik_enabled=None):
    """Return the compose model as rendered by `docker compose config`."""
    env = {k: v for k, v in os.environ.items() if k != "AUDIOBOOKSHELF_TRAEFIK_ENABLED"}
    if traefik_enabled is not None:
        env["AUDIOBOOKSHELF_TRAEFIK_ENABLED"] = traefik_enabled
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        cwd=STACK_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


@unittest.skipUnless(shutil.which("docker"), "docker CLI not available")
class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = render()
        cls.services = cls.model["services"]
        cls.svc = cls.services["audiobookshelf"]

    def test_single_service(self):
        self.assertEqual(list(self.services), ["audiobookshelf"])

    def test_image_pinned(self):
        self.assertEqual(self.svc["image"], IMAGE)

    def test_media_bind_long_syntax_readonly_no_create(self):
        binds = [v for v in self.svc["volumes"] if v["target"] == "/audiobooks"]
        self.assertEqual(len(binds), 1)
        media = binds[0]
        self.assertEqual(media["type"], "bind")
        self.assertEqual(media["source"], "/mnt/audiobooks")
        self.assertTrue(media.get("read_only"))
        # compose omits create_host_path from rendered JSON when false, so also check the source
        self.assertFalse(media.get("bind", {}).get("create_host_path", False))
        raw = COMPOSE_FILE.read_text()
        self.assertIn("create_host_path: false", raw)
        self.assertNotIn("/mnt/audiobooks:/audiobooks", raw)

    def test_state_on_named_volumes(self):
        by_target = {v["target"]: v for v in self.svc["volumes"]}
        for target in ("/config", "/metadata"):
            self.assertEqual(by_target[target]["type"], "volume", target)
            self.assertIn(by_target[target]["source"], self.model["volumes"])
            self.assertNotIn("driver_opts", self.model["volumes"][by_target[target]["source"]])

    def test_no_writable_extra_mounts(self):
        targets = sorted(v["target"] for v in self.svc["volumes"])
        self.assertEqual(targets, ["/audiobooks", "/config", "/metadata"])
        self.assertNotIn("ports", self.svc)

    def test_traefik_labels(self):
        labels = self.svc["labels"]
        self.assertEqual(labels["traefik.enable"], "true")
        self.assertEqual(labels["traefik.http.routers.audiobookshelf.rule"], f"Host(`{HOST}`)")
        self.assertEqual(labels["traefik.http.routers.audiobookshelf.entrypoints"], "websecure")
        self.assertEqual(labels["traefik.http.routers.audiobookshelf.tls.certresolver"], "cloudflare")
        self.assertEqual(labels["traefik.http.services.audiobookshelf.loadbalancer.server.port"], "80")
        self.assertEqual(labels["traefik.docker.network"], "traefik_default")
        self.assertEqual(self.model["networks"]["traefik"]["name"], "traefik_default")
        self.assertTrue(self.model["networks"]["traefik"]["external"])

    def test_no_oidc_middleware(self):
        self.assertFalse([k for k in self.svc["labels"] if "middlewares" in k])
        self.assertNotIn("oidc", COMPOSE_FILE.read_text().lower())

    def test_traefik_toggle_false(self):
        labels = render("false")["services"]["audiobookshelf"]["labels"]
        self.assertEqual(labels["traefik.enable"], "false")
        self.assertIn("${AUDIOBOOKSHELF_TRAEFIK_ENABLED:-true}", COMPOSE_FILE.read_text())

    def test_no_container_name_restart_tz(self):
        self.assertNotIn("container_name", self.svc)
        self.assertEqual(self.svc["restart"], "unless-stopped")
        self.assertEqual(self.svc["environment"]["TZ"], "Europe/Berlin")

    def test_healthcheck_node_route(self):
        test = self.svc["healthcheck"]["test"]
        self.assertEqual(test[:2], ["CMD", "node"])
        self.assertIn("http://127.0.0.1/healthcheck", " ".join(test))


def stack_entry():
    stacks = tomllib.loads(STACKS_TOML.read_text())["stack"]
    return [s for s in stacks if s["name"] == "audiobookshelf"], stacks


class KomodoResourceTests(unittest.TestCase):
    def test_toml_single_entry_feature_branch(self):
        entries, stacks = stack_entry()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertIs(entry["deploy"], True)
        self.assertIs(entry["auto_update"], False)
        config = {k: v for k, v in entry["config"].items() if k != "pre_deploy"}
        self.assertEqual(
            config,
            {
                "server_id": "Local",
                "repo": "ravilushqa/homelab",
                "branch": BRANCH,
                "run_directory": "komodo/stacks/audiobookshelf",
            },
        )
        # Only this new resource tracks the feature branch; existing stacks stay on main
        others = {s["name"]: s["config"]["branch"] for s in stacks if s["name"] != "audiobookshelf"}
        self.assertEqual(set(others.values()), {"main"})
        # No other stack gained a pre_deploy hook
        self.assertFalse([s["name"] for s in stacks if s["name"] != "audiobookshelf" and "pre_deploy" in s["config"]])


MOUNTPOINT = "/mnt/audiobooks"
CIFS_SOURCE = "//192.168.1.2/proxmox[/audiobooks]"


class PreDeployGuardTests(unittest.TestCase):
    """Run the real pre_deploy command (as Komodo shell mode does: `sh -c`) against stubbed
    findmnt/ls on PATH, so no root, CIFS share, or /mnt/audiobooks is needed."""

    def setUp(self):
        self.pre_deploy = stack_entry()[0][0]["config"]["pre_deploy"]
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"

    def stub(self, name, stdout="", code=0):
        script = self.bin / name
        script.write_text(
            "#!/bin/sh\n"
            f'echo "{name} $*" >> "{self.log}"\n'
            f"printf '%s' '{stdout}'\n"
            f"exit {code}\n"
        )
        script.chmod(0o755)

    def run_guard(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(
            ["sh", "-c", self.pre_deploy["command"]],
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_schema_shell_mode_default_path(self):
        self.assertEqual(set(self.pre_deploy), {"command", "shell_mode"})
        self.assertIs(self.pre_deploy["shell_mode"], True)

    def test_correct_cifs_source_passes(self):
        self.stub("findmnt", CIFS_SOURCE + "\n")
        self.stub("ls")
        res = self.run_guard()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"findmnt -rn -M {MOUNTPOINT} -t cifs -o SOURCE", self.calls())
        self.assertTrue([c for c in self.calls() if c.startswith("ls ") and MOUNTPOINT in c])

    def test_wrong_source_fails(self):
        self.stub("ls")
        for wrong in ("//192.168.1.2/proxmox", "//192.168.1.3/proxmox[/audiobooks]",
                      "//192.168.1.2/proxmox[/audiobooks/x]", CIFS_SOURCE + "\n" + CIFS_SOURCE):
            with self.subTest(source=wrong):
                self.stub("findmnt", wrong + "\n")
                res = self.run_guard()
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(CIFS_SOURCE, res.stderr)

    def test_unmounted_existing_directory_fails(self):
        # findmnt prints nothing and exits 1 when nothing (or no cifs) is mounted there;
        # ls succeeds because the empty mountpoint directory still exists
        self.stub("findmnt", code=1)
        self.stub("ls")
        res = self.run_guard()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not mounted", res.stderr)

    def test_unreadable_mount_fails(self):
        self.stub("findmnt", CIFS_SOURCE + "\n")
        self.stub("ls", code=2)
        res = self.run_guard()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not readable", res.stderr)

    def test_guard_changes_no_host_state(self):
        command = self.pre_deploy["command"]
        for forbidden in ("mount ", "umount", "mkdir", "chmod", "chown", "rm ", "touch", "systemctl", ">"):
            # findmnt is the only allowed *mount* token; `>&2` is the only allowed redirect
            stripped = command.replace("findmnt", "").replace(">&2", "").replace(">/dev/null", "")
            self.assertNotIn(forbidden, stripped, forbidden)


if __name__ == "__main__":
    unittest.main()
