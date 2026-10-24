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
TARGET = "/audiobooks"
CIFS_SOURCE = "//192.168.1.2/proxmox"
EXPECTED_DOCKER_PREFIX = [
    "run", "--rm", "--network", "none", "--pull=missing", "--read-only",
    "--mount", f"type=bind,src={MOUNTPOINT},dst={TARGET},readonly",
    "--entrypoint", "node", IMAGE, "-e",
]

# Container-side /proc/self/mountinfo as seen inside the probe (observed on the Komodo host)
GOOD_LINE = (
    "1525 1501 0:63 /audiobooks /audiobooks ro,relatime master:521 - cifs //192.168.1.2/proxmox "
    "rw,vers=3.1.1,cache=strict,uid=0,noforceuid,gid=0,noforcegid,addr=192.168.1.2"
)
OTHER_LINES = [
    "1501 1400 0:120 / / ro,relatime - overlay overlay rw,lowerdir=/var/lib/docker/l/A,upperdir=/u,workdir=/w",
    "1502 1501 0:122 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw",
    "1510 1501 0:125 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755",
]


def mountinfo(*media_lines):
    return "\n".join([*OTHER_LINES, *media_lines]) + "\n"


# Patches fs inside the real `node -e` script: /proc/self/mountinfo and /audiobooks are served from
# env fixtures, any other read is an error, and every access is logged
FS_MOCK = r"""
const fs = require("fs");
const log = (m) => fs.appendFileSync(process.env.FS_LOG, m + "\n");
const realRead = fs.readFileSync;
fs.readFileSync = (p, ...rest) => {
  if (p === process.env.FS_LOG) return realRead(p, ...rest);
  log("readFileSync " + p);
  if (p !== "/proc/self/mountinfo") throw new Error("unexpected readFileSync " + p);
  return process.env.FIXTURE_MOUNTINFO;
};
fs.readdirSync = (p) => {
  log("readdirSync " + p);
  if (p !== "/audiobooks") throw new Error("unexpected readdirSync " + p);
  if (process.env.FIXTURE_READDIR_ERROR) {
    throw Object.assign(new Error(process.env.FIXTURE_READDIR_ERROR + ": denied"), { code: process.env.FIXTURE_READDIR_ERROR });
  }
  return ["Secret Author", "Private Book.m4b"];
};
"""

# Fake `docker`: records argv, then runs the script after `-e` with the host's node + FS_MOCK
DOCKER_STUB = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FIXTURE_DOCKER_EXIT"):
    sys.stderr.write("docker: Error response from daemon: invalid mount config\n")
    sys.exit(int(os.environ["FIXTURE_DOCKER_EXIT"]))
script = args[args.index("-e") + 1]
sys.exit(subprocess.run([os.environ["REAL_NODE"], "--require", os.environ["FS_MOCK"], "-e", script]).returncode)
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class PreDeployGuardTests(unittest.TestCase):
    """Run the real pre_deploy command (as Komodo shell mode does: `sh -c`) with a stub `docker` on
    PATH. The stub runs the guard's inline Node script against mocked fs fixtures, so no Docker
    daemon, CIFS share, or /mnt/audiobooks is needed."""

    def setUp(self):
        self.pre_deploy = stack_entry()[0][0]["config"]["pre_deploy"]
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o755)
        (self.tmp / "fs_mock.js").write_text(FS_MOCK)
        self.docker_log = self.tmp / "docker.log"
        self.fs_log = self.tmp / "fs.log"
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            REAL_NODE=shutil.which("node"),
            FS_MOCK=str(self.tmp / "fs_mock.js"),
            DOCKER_LOG=str(self.docker_log),
            FS_LOG=str(self.fs_log),
            FIXTURE_MOUNTINFO=mountinfo(GOOD_LINE),
        )

    def run_guard(self, **fixtures):
        return subprocess.run(
            ["sh", "-c", self.pre_deploy["command"]],
            cwd=self.tmp,
            env={**self.env, **fixtures},
            capture_output=True,
            text=True,
        )

    def docker_calls(self):
        return [json.loads(l) for l in self.docker_log.read_text().splitlines()] if self.docker_log.exists() else []

    def fs_calls(self):
        return self.fs_log.read_text().splitlines() if self.fs_log.exists() else []

    def assert_rejected(self, res, message):
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(message, res.stderr)

    def test_schema_shell_mode_default_path(self):
        self.assertEqual(set(self.pre_deploy), {"command", "shell_mode"})
        self.assertIs(self.pre_deploy["shell_mode"], True)

    def test_probe_is_one_disposable_readonly_container_on_the_host_daemon(self):
        res = self.run_guard()
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self.docker_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][: len(EXPECTED_DOCKER_PREFIX)], EXPECTED_DOCKER_PREFIX)
        self.assertEqual(len(calls[0]), len(EXPECTED_DOCKER_PREFIX) + 1)  # only the script follows -e

    def test_probe_image_matches_compose_pin(self):
        self.assertIn(f"image: {IMAGE}", COMPOSE_FILE.read_text())
        self.assertIn(IMAGE, self.pre_deploy["command"])

    def test_observed_cifs_mount_passes(self):
        res = self.run_guard()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.fs_calls(), ["readFileSync /proc/self/mountinfo", "readdirSync /audiobooks"])
        self.assertIn(CIFS_SOURCE, res.stdout)

    def test_filenames_are_not_printed(self):
        res = self.run_guard()
        self.assertEqual(res.returncode, 0, res.stderr)
        for name in ("Secret Author", "Private Book"):
            self.assertNotIn(name, res.stdout + res.stderr)

    def test_stale_ordinary_directory_fails(self):
        # Share not mounted on the LXC: Docker binds the plain local directory instead
        stale = "1525 1501 0:31 /mnt/audiobooks /audiobooks ro,relatime - zfs rpool/data/subvol-122-disk-0 rw,xattr"
        res = self.run_guard(FIXTURE_MOUNTINFO=mountinfo(stale))
        self.assert_rejected(res, "type")
        self.assertNotIn("readdirSync /audiobooks", self.fs_calls())

    def test_wrong_root_subpath_fails(self):
        for root in ("/", "/audiobooks/x", "/audiobooks2"):
            with self.subTest(root=root):
                line = GOOD_LINE.replace(" /audiobooks /audiobooks ", f" {root} /audiobooks ", 1)
                self.assert_rejected(self.run_guard(FIXTURE_MOUNTINFO=mountinfo(line)), "root")

    def test_wrong_source_fails(self):
        for source in ("//192.168.1.3/proxmox", "//192.168.1.2/proxmox2", "//192.168.1.2/other"):
            with self.subTest(source=source):
                line = GOOD_LINE.replace(f" {CIFS_SOURCE} ", f" {source} ")
                self.assert_rejected(self.run_guard(FIXTURE_MOUNTINFO=mountinfo(line)), "source")

    def test_wrong_fs_type_fails(self):
        for fstype in ("smb3", "nfs", "fuse.sshfs"):
            with self.subTest(fstype=fstype):
                line = GOOD_LINE.replace(" - cifs ", f" - {fstype} ")
                self.assert_rejected(self.run_guard(FIXTURE_MOUNTINFO=mountinfo(line)), "type")

    def test_read_write_mount_fails(self):
        line = GOOD_LINE.replace(" ro,relatime ", " rw,relatime ")
        self.assert_rejected(self.run_guard(FIXTURE_MOUNTINFO=mountinfo(line)), "read-only")

    def test_missing_or_duplicate_mount_fails(self):
        for case, info in (("missing", mountinfo()), ("duplicate", mountinfo(GOOD_LINE, GOOD_LINE))):
            with self.subTest(case=case):
                self.assert_rejected(self.run_guard(FIXTURE_MOUNTINFO=info), "exactly one")

    def test_unreadable_mount_fails(self):
        res = self.run_guard(FIXTURE_READDIR_ERROR="EACCES")
        self.assert_rejected(res, "not readable")
        self.assertIn("EACCES", res.stderr)

    def test_docker_failure_fails(self):
        # e.g. --mount on a nonexistent /mnt/audiobooks, or the daemon is unreachable
        res = self.run_guard(FIXTURE_DOCKER_EXIT="125")
        self.assert_rejected(res, "probe failed")
        self.assertEqual(self.fs_calls(), [])

    def test_guard_changes_no_host_state(self):
        # The only side effect allowed is a throwaway `docker run --rm` probe with a readonly bind
        command = self.pre_deploy["command"]
        self.assertEqual(command.count("docker run"), 1)
        self.assertEqual(command.count("docker "), command.count("docker run") + command.count("docker probe"))
        self.assertIn("docker run --rm ", command)
        self.assertNotIn(" -v ", command)
        self.assertNotIn("--volume", command)
        self.assertNotIn("--privileged", command)
        for forbidden in ("mount ", "umount", "mkdir", "chmod", "chown", "rm ", "touch", "systemctl",
                          "writeFile", "appendFile", "mkdirSync", "unlink", "rmSync", ">"):
            # `>&2` is the only allowed redirect; `=>` is JS arrow syntax; `--rm`/`--mount` are probe flags
            stripped = command.replace(">&2", "").replace("=>", "").replace("--rm ", "").replace("--mount ", "")
            self.assertNotIn(forbidden, stripped, forbidden)


if __name__ == "__main__":
    unittest.main()
