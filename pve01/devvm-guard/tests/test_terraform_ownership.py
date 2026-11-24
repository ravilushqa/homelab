"""
Terraform owns the dev VM (VMID 130) definition; the host guard owns only the runtime-only network
state and the start/stop lifecycle. These tests pin that split:

* the guard's legacy `destroy` verb must refuse and never call `qm destroy` (Terraform owns the
  VM and its disk);
* the opt-in Terraform module exists, is disabled by default, and its pinned identity matches
  /etc/devvm-guard.conf (VMID, bridge, MAC) and the documented sizing (32 GiB, 2 vCPU, 2 GiB);
* the module uses no provisioner/SSH/import/cloud-image shortcut and no hookscript setter;
* the runbook/design/README no longer construct the VM by hand (`qm create`, `importdisk`, ...)
  and never claim 20 GiB.

Run: python3 -m unittest discover -s pve01/devvm-guard/tests -p 'test_*.py' -v
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parent
REPO = BUNDLE.parent.parent
GUARD = BUNDLE / "devvm-guard"
CONF_EXAMPLE = BUNDLE / "devvm-guard.conf.example"
TF_ROOT = REPO / "terraform"
MODULE = TF_ROOT / "modules" / "dev-vm"
RUNBOOK = REPO / "docs" / "devvm-guard" / "APPLY-RUNBOOK.md"
DESIGN = REPO / "docs" / "devvm-guard" / "DESIGN-DRAFT-v3.md"
README = BUNDLE / "README.md"

ISO = "local:iso/dev-t_379eee20-debian-13.7.0-amd64-netinst.iso"


def _conf(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(\S*)", line)
        if m:
            out[m.group(1)] = m.group(2).strip('"')
    return out


def _module_text() -> str:
    return "\n".join(p.read_text() for p in sorted(MODULE.glob("*.tf")))


def _hcl_default(text: str, var: str) -> str:
    m = re.search(r'variable\s+"%s"\s*\{(.*?)\n\}' % re.escape(var), text, re.S)
    assert m, "variable %s not declared in the dev-vm module" % var
    d = re.search(r"^\s*default\s*=\s*(.+)$", m.group(1), re.M)
    assert d, "variable %s has no default" % var
    return d.group(1).strip().strip('"')


class GuardDestroyRefuses(unittest.TestCase):
    """`devvm-guard destroy` must not bypass Terraform ownership of the VM and its disk."""

    def setUp(self):
        if shutil.which("unshare") is None:
            self.skipTest("unshare not available")
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("user+net namespaces unavailable: %s" % probe.stderr.decode().strip())
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log = self.tmp / "calls.log"
        stub_dir = self.tmp / "bin"
        stub_dir.mkdir()
        # A qm stub that records every call and claims VM 130 exists and is stopped.
        qm = stub_dir / "qm"
        qm.write_text(
            "#!/bin/sh\n"
            'echo "qm $*" >> "$CALL_LOG"\n'
            'case "$1" in status) echo "status: stopped" ;; config) echo "net0: virtio=02:DE:AD:00:01:30,bridge=vmbr1" ;; esac\n'
            "exit 0\n"
        )
        qm.chmod(qm.stat().st_mode | stat.S_IEXEC)
        conf = self.tmp / "guard.conf"
        conf.write_text(CONF_EXAMPLE.read_text().replace("WAN_PUBLIC_IP=\n", "WAN_PUBLIC_IP=203.0.113.7\n"))
        self.env = dict(
            os.environ,
            PATH="%s:%s" % (stub_dir, os.environ.get("PATH", "")),
            CALL_LOG=str(self.log),
            DEVVM_CONF=str(conf),
            DEVVM_STATE_DIR=str(self.tmp / "state"),
            DEVVM_RENDER_DIR=str(self.tmp / "run"),
        )

    def _run(self, *args):
        # Fake root in a disposable user+network namespace: nothing on the host is touched.
        return subprocess.run(
            ["unshare", "-rn", "bash", str(GUARD), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def _calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_destroy_yes_refuses_and_never_calls_qm_destroy(self):
        r = self._run("destroy", "--yes")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("qm destroy", self._calls())
        self.assertRegex(r.stderr, r"(?i)terraform")

    def test_destroy_without_yes_also_refuses_without_side_effects(self):
        r = self._run("destroy")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._calls(), "", "destroy must not touch qm at all")
        self.assertRegex(r.stderr, r"(?i)terraform")

    def test_guard_script_contains_no_qm_destroy_or_vm_construction(self):
        code = "\n".join(
            ln for ln in GUARD.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        for pat in (r"qm\s+destroy", r"qm\s+create", r"qm\s+importdisk", r"qm\s+disk\s+import",
                    r"qm\s+set\b", r"qm\s+disk\s+resize", r"pvesm\s+alloc"):
            self.assertIsNone(re.search(pat, code), "guard must not run `%s`" % pat)


class DevBridgePortPredicate(unittest.TestCase):
    """Runs tests/netns-bridge-ports-test.sh (real bridges/veth/VLAN/nft in a disposable
    namespace). Exit 77 means the real-TAP cases could not run (no /dev/net/tun): reported as a
    skip, never as a pass."""

    def test_bridge_port_predicate_in_disposable_netns(self):
        if shutil.which("unshare") is None:
            self.skipTest("unshare not available")
        r = subprocess.run(
            ["unshare", "-rn", "--mount", "--propagation", "private", "bash",
             str(HERE / "netns-bridge-ports-test.sh")],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 2:
            self.skipTest("namespaces unavailable: %s" % r.stderr.strip())
        self.assertNotIn("\nFAIL", "\n" + r.stdout, r.stdout + r.stderr)
        self.assertIn(r.returncode, (0, 77), r.stdout + r.stderr)
        if r.returncode == 77:
            self.skipTest("rejection/config cases PASSED; real-TAP acceptance cases SKIPPED "
                          "(no /dev/net/tun): %s" % r.stdout.strip().splitlines()[-1])

    def test_no_wildcard_tap_allowance(self):
        code = "\n".join(ln.split("#", 1)[0] for ln in GUARD.read_text().splitlines())
        self.assertIsNone(re.search(r"tap\*|tap\[|\btap.*\.\*", code),
                          "the guard must name tap<VMID>i0 exactly, never a tap* pattern")
        self.assertIn('tap="tap${DEV_VMID}i0"', code)


class TerraformModuleShape(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_dir(), "terraform/modules/dev-vm missing")
        self.mod = _module_text()

    def test_identity_matches_guard_config(self):
        conf = _conf(CONF_EXAMPLE)
        self.assertEqual(_hcl_default(self.mod, "vm_id"), conf["DEV_VMID"])
        self.assertEqual(_hcl_default(self.mod, "name"), conf["DEV_NAME"])
        self.assertEqual(_hcl_default(self.mod, "bridge"), conf["DEV_BRIDGE"])
        self.assertEqual(_hcl_default(self.mod, "mac_address").upper(), conf["DEV_GUEST_MAC"].upper())

    def test_sizing_matches_design(self):
        self.assertEqual(_hcl_default(self.mod, "node_name"), "pve01")
        self.assertEqual(_hcl_default(self.mod, "disk_size_gib"), "32")
        self.assertEqual(_hcl_default(self.mod, "cores"), "2")
        self.assertEqual(_hcl_default(self.mod, "memory_mib"), "2048")
        self.assertEqual(_hcl_default(self.mod, "disk_datastore"), "nvme-thin")
        self.assertEqual(_hcl_default(self.mod, "installer_iso_file_id"), ISO)

    def test_never_started_or_autostarted_by_terraform(self):
        self.assertRegex(self.mod, r"(?m)^\s*started\s*=\s*false\s*(#.*)?$")
        self.assertRegex(self.mod, r"(?m)^\s*on_boot\s*=\s*false\s*(#.*)?$")
        self.assertRegex(self.mod, r"(?m)^\s*reboot_after_update\s*=\s*false\s*(#.*)?$")
        ign = re.search(r"ignore_changes\s*=\s*\[(.*?)\]", self.mod, re.S)
        self.assertIsNotNone(ign, "lifecycle.ignore_changes missing")
        self.assertIn("started", ign.group(1))
        self.assertIn("hook_script_file_id", ign.group(1))
        self.assertRegex(self.mod, r"prevent_destroy\s*=\s*true")

    def test_no_provisioner_ssh_import_or_hookscript_setter(self):
        code = "\n".join(ln.split("#", 1)[0] for ln in self.mod.splitlines())
        for pat in (r"provisioner\s", r"local-exec", r"remote-exec", r"null_resource",
                    r"terraform_data", r"\bssh\s*\{", r"import_from", r"\bfile_id\s*=\s*proxmox",
                    r"download_file", r"\binitialization\s*\{", r"cloud", r"hook_script_file_id\s*=",
                    r"network_linux_bridge", r"\bproxmox_virtual_environment_file\b", r"\bclone\s*\{"):
            self.assertIsNone(re.search(pat, code), "dev-vm module must not use `%s`" % pat)

    def test_root_wiring_is_opt_in_and_disabled_by_default(self):
        root = "\n".join(p.read_text() for p in sorted(TF_ROOT.glob("*.tf")))
        self.assertTrue(
            re.search(r'module\s+"dev_vm"\s*\{[^}]*count\s*=\s*var\.dev_vm_enabled\s*\?\s*1\s*:\s*0', root),
            "root must wire module \"dev_vm\" with count = var.dev_vm_enabled ? 1 : 0",
        )
        self.assertEqual(_hcl_default(root, "dev_vm_enabled"), "false")
        tfvars = (TF_ROOT / "homelab.auto.tfvars").read_text()
        self.assertNotRegex(tfvars, r"(?m)^\s*dev_vm_enabled\s*=", "must stay disabled until the window")


class DocsUseTerraformOwnership(unittest.TestCase):
    def test_runbook_no_longer_constructs_vm_by_hand(self):
        text = RUNBOOK.read_text()
        for pat in (r"qm\s+create", r"qm\s+importdisk", r"qm\s+disk\s+(resize|import)",
                    r"qm\s+set\s+130\s+--(scsi0|net0|ide2|boot|ciuser|ipconfig0|cipassword)",
                    r"genericcloud", r"cloud\.debian\.org", r"curl\s+-fSLO",
                    r"devvm-guard\s+destroy\s+--yes"):
            self.assertIsNone(re.search(pat, text), "runbook still contains `%s`" % pat)
        self.assertIn("dev_vm_enabled", text)
        self.assertIn("--hookscript", text, "root-only hookscript setter must be documented")

    def test_runbook_order_guard_bridge_before_terraform_vm(self):
        text = RUNBOOK.read_text()
        # No claim that missing token privileges fail before anything exists, and no reliance on
        # PVE accepting a NIC on a bridge that does not exist yet.
        self.assertNotRegex(text, r"(?i)fails? before (a|the|any) VM exists")
        self.assertNotRegex(text, r"(?i)which does not exist until step")
        heads = [h.lower() for h in re.findall(r"(?m)^## (.+)$", text)]
        def idx(pat):
            for i, h in enumerate(heads):
                if re.search(pat, h):
                    return i
            self.fail("runbook has no section matching %r: %r" % (pat, heads))
        self.assertLess(idx(r"guard.*(bridge|install)"), idx(r"terraform.*(creates|apply)"))
        self.assertLess(idx(r"terraform.*(creates|apply)"), idx(r"hookscript"))
        self.assertLess(idx(r"hookscript"), idx(r"^\d+\.\s+start"))

    def test_runbook_partial_apply_and_hookscript_are_fail_closed(self):
        text = RUNBOOK.read_text()
        self.assertRegex(text, r"terraform state (list|show)")
        self.assertRegex(text, r"(?i)partial")
        self.assertNotRegex(text, r"(?i)optional:? pre-start hookscript")
        self.assertRegex(text, r"(?i)hookscript.*(required|mandatory)")
        self.assertIn("offline-test.sh", text)
        fenced = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", text, re.S))
        for pat in (r"terraform\s+import", r"-replace", r"qm\s+destroy", r"prevent_destroy\s*=\s*false",
                    r"qm\s+start", r"qm\s+create"):
            self.assertIsNone(re.search(pat, fenced), "runbook command block runs `%s`" % pat)

    def test_https_only_egress_is_documented_not_widened(self):
        tmpl = (BUNDLE / "devvm-guard.nft.tmpl").read_text()
        self.assertNotRegex(tmpl, r"dport\s*\{[^}]*\b80\b")
        text = RUNBOOK.read_text()
        self.assertNotRegex(text, r"(?i)HTTP/HTTPS (are|is) allowed")
        self.assertIn("mirror/protocol=https", text)
        for ln in text.splitlines():
            if re.search(r"curl[^#]*http://deb\.debian\.org", ln):
                self.assertIn("must fail", ln, "plain-HTTP probe must be a negative probe: %s" % ln)
        self.assertNotRegex(DESIGN.read_text(), r"TCP 80,443")

    def test_disk_size_is_32_gib_everywhere(self):
        for doc in (RUNBOOK, DESIGN, README):
            text = doc.read_text()
            self.assertIsNone(re.search(r"\b20\s?Gi?B?\b|\b20G\b", text), "%s still says 20 GiB" % doc.name)
        self.assertIn("32 GiB", DESIGN.read_text())

    def test_bridge_stays_runtime_only_and_guard_owned(self):
        design = DESIGN.read_text()
        self.assertIn("network_linux_bridge", design, "document why the provider bridge resource is not used")
        self.assertRegex(design, r"(?i)runtime-only")

    def test_no_root_token_or_provisioner_suggestions(self):
        # Commands/HCL live in fenced blocks; prose may name these only to rule them out.
        for doc in (RUNBOOK, DESIGN, README):
            text = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", doc.read_text(), re.S))
            for pat in (r"local-exec", r"remote-exec", r"null_resource", r"(?i)root@pam!\w"):
                self.assertIsNone(re.search(pat, text), "%s suggests `%s`" % (doc.name, pat))


if __name__ == "__main__":
    unittest.main()
