"""
Focused synthetic tests for short final-preview consent in
agent-workflow/nutrition_writeback.py.

The queue CLI is replaced by an in-memory fake (_run_cli patched) and the
mutation-readiness gate is stubbed; no production CLI, DB, token or network is
touched. Temp dirs live under health scratch with 0700 permissions.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SKILL_PATH = Path(__file__).parent.parent / "agent-workflow" / "nutrition_writeback.py"
_spec = importlib.util.spec_from_file_location("_nw_short", _SKILL_PATH)
nw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nw)

_SCRATCH = Path.home() / ".hermes/profiles/health/cache/scratch"
T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
RID = "rec-1"


def _draft(**over):
    r = {
        "client_record_id": RID, "client_record_version": 1, "state": "draft",
        "name": "Oatmeal", "start_time": "2026-10-12T08:00:00Z",
        "end_time": "2026-10-12T08:15:00Z", "energy_kcal": 300.0,
        "protein_g": 10.0, "carbohydrate_g": 50.0, "fat_g": 5.0,
        "description": "[AI estimate] oats",
    }
    r.update(over)
    return r


class FakeQueue:
    def __init__(self):
        self.rec = _draft()
        self.confirm_calls = 0
        self.fail_confirm = False

    def run(self, cli, db, cli_args, stdin_bytes=None):
        if cli_args[0] == "status":
            return 0, json.dumps(self.rec), ""
        if cli_args[0] == "confirm":
            self.confirm_calls += 1
            if self.fail_confirm:
                return 1, "", "boom"
            self.rec["state"] = "confirmed"
            self.rec["confirmed_by"] = cli_args[cli_args.index("--confirmer") + 1]
            self.rec["confirmed_at"] = "2026-10-12T12:01:00Z"
            return 0, json.dumps(self.rec), ""
        return 1, "", "unsupported"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_SCRATCH, prefix="test_shortprev_"))
        os.chmod(self.root, 0o700)
        self.q = FakeQueue()
        self.now = T0
        self.patches = [
            mock.patch.object(nw, "_resolve",
                              lambda a: (Path("/x/cli"), Path("/x/db"), self.root)),
            mock.patch.object(nw, "_run_cli", self.q.run),
            mock.patch.object(nw, "_check_mutation_readiness", lambda *a, **k: []),
            mock.patch.object(nw, "_now", lambda: self.now),
        ]
        for p in self.patches:
            p.start()
        self.tf_n = 0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cmd(self, fn, **kw):
        base = dict(cli="/x/cli", db="/x/db", isolation_root=str(self.root))
        base.update(kw)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(argparse.Namespace(**base))
        return rc, out.getvalue(), err.getvalue()

    def preview(self, msg="p1", sent=None, ttl=900, user="u1", chat="c1", thread="", rid=RID):
        sent = sent or T0.isoformat()
        return self.run_cmd(
            nw.cmd_preview, id=rid, confirmer="Ravil", user_id=user, chat_id=chat,
            thread_id=thread, preview_message_id=msg, preview_sent_at=sent, ttl_seconds=ttl)

    def reply(self, text="Да", sender="u1", chat="c1", thread="", mid="r1",
              ts=None, reply_to="p1", mode=0o600, raw=None):
        self.tf_n += 1
        p = self.root / f"reply{self.tf_n}.json"
        data = raw if raw is not None else {
            "sender_id": sender, "chat_id": chat, "thread_id": thread,
            "message_id": mid, "timestamp": ts or (T0 + timedelta(seconds=30)).isoformat(),
            "reply_to_message_id": reply_to, "text": text}
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(p, mode)
        return self.run_cmd(nw.cmd_short_confirm, transport_file=str(p))

    def assertRejected(self, res, needle=None):
        rc, out, err = res
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.q.confirm_calls, 0)
        if needle:
            self.assertIn(needle, (out + err).lower())


class TestHappyPath(Base):
    def test_da_confirms_and_replay_rejected(self):
        self.assertEqual(self.preview()[0], 0)
        rc, out, err = self.reply("Да")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.rec["state"], "confirmed")
        files = list((self.root / "pending-previews").glob("preview-*.json"))
        self.assertEqual(json.loads(files[0].read_text())["status"], "consumed")
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")
        # replay
        self.q.rec["state"] = "draft"
        self.q.confirm_calls = 0
        self.assertRejected(self.reply("Да", mid="r2"), "not pending")

    def test_zapishi_and_punctuation(self):
        self.preview()
        self.assertEqual(self.reply("  Запиши! ")[0], 0)

    def test_provenance_is_workflow_attested(self):
        rc, out, _ = self.preview()
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        self.assertEqual(json.loads(f.read_text())["provenance_level"], "workflow_attested")


class TestRejections(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)

    def test_stale_version(self):
        self.q.rec["client_record_version"] = 2
        self.assertRejected(self.reply(), "version")

    def test_same_version_tamper(self):
        self.q.rec["energy_kcal"] = 900.0
        self.assertRejected(self.reply(), "differs")

    def test_description_tamper(self):
        self.q.rec["description"] = "[AI estimate] other"
        self.assertRejected(self.reply(), "differs")

    def test_non_draft(self):
        self.q.rec["state"] = "cancelled"
        self.assertRejected(self.reply(), "draft")

    def test_wrong_user(self):
        self.assertRejected(self.reply(sender="u2"), "sender")

    def test_wrong_chat(self):
        self.assertRejected(self.reply(chat="c2"), "no pending preview")

    def test_wrong_thread(self):
        self.assertRejected(self.reply(thread="t9"), "no pending preview")

    def test_wrong_reply_target(self):
        self.assertRejected(self.reply(reply_to="other"), "no pending preview")

    def test_missing_preview_dir(self):
        shutil.rmtree(self.root / "pending-previews")
        self.assertRejected(self.reply(), "no pending preview")

    def test_predating_reply(self):
        self.assertRejected(self.reply(ts=(T0 - timedelta(seconds=5)).isoformat()), "postdate")

    def test_equal_timestamp_rejected(self):
        self.assertRejected(self.reply(ts=T0.isoformat()), "postdate")

    def test_expired_by_clock(self):
        self.now = T0 + timedelta(seconds=901)
        self.assertRejected(self.reply(ts=(T0 + timedelta(seconds=60)).isoformat()), "expired")

    def test_reply_after_expiry(self):
        self.assertRejected(self.reply(ts=(T0 + timedelta(seconds=950)).isoformat()), "expired")

    def test_negative_and_conditional(self):
        for t in ("нет", "не записывай", "no", "don't record it", "yes, but not now",
                  "да, если калории верные", "maybe", "yes please record everything now"):
            self.assertRejected(self.reply(t, mid="rx" + t))

    def test_naive_timestamp(self):
        self.assertRejected(self.reply(ts="2026-10-12T12:00:30"), "timezone")

    def test_insecure_transport_file(self):
        self.assertRejected(self.reply(mode=0o644))

    def test_caller_cannot_supply_queue_id(self):
        # No --id on short-confirm: id is not even read from the namespace.
        p = nw.build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["short-confirm", "--transport-file", "x", "--id", RID])

    def test_tampered_preview_file(self):
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        d = json.loads(f.read_text())
        d["snapshot"]["energy_kcal"] = 1.0
        f.write_text(json.dumps(d))
        self.assertRejected(self.reply(), "tampered")

    def test_symlinked_preview_dir(self):
        shutil.rmtree(self.root / "pending-previews")
        real = self.root / "real"
        real.mkdir(mode=0o700)
        os.symlink(real, self.root / "pending-previews")
        self.assertRejected(self.reply(), "insecure")

    def test_group_readable_preview_dir(self):
        os.chmod(self.root / "pending-previews", 0o750)
        self.assertRejected(self.reply(), "insecure")


class TestPreviewRegistration(Base):
    def test_rejects_non_draft(self):
        self.q.rec["state"] = "confirmed"
        self.assertEqual(self.preview()[0], 1)

    def test_rejects_future_and_expired(self):
        self.assertEqual(self.preview(sent=(T0 + timedelta(hours=1)).isoformat())[0], 1)
        self.assertEqual(self.preview(sent=(T0 - timedelta(hours=1)).isoformat())[0], 1)

    def test_rejects_naive_and_bad_ttl(self):
        self.assertEqual(self.preview(sent="2026-10-12T12:00:00")[0], 1)
        self.assertEqual(self.preview(ttl=99999)[0], 1)

    def test_duplicate_message_id_refused(self):
        self.assertEqual(self.preview()[0], 0)
        self.assertEqual(self.preview()[0], 1)

    def test_new_preview_supersedes_old(self):
        self.assertEqual(self.preview(msg="p1")[0], 0)
        self.assertEqual(self.preview(msg="p2")[0], 0)
        self.assertRejected(self.reply(reply_to="p1"), "not pending")
        self.assertEqual(self.reply(reply_to="p2", mid="r9")[0], 0)

    def test_ambiguous_previews_rejected(self):
        self.assertEqual(self.preview()[0], 0)
        d = self.root / "pending-previews"
        f = next(d.glob("preview-*.json"))
        dup = d / "preview-dup.json"
        shutil.copy(f, dup)
        os.chmod(dup, 0o600)
        self.assertRejected(self.reply(), "ambiguous")


class TestConcurrencyAndCrash(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)

    def _status(self):
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        return json.loads(f.read_text())["status"]

    def test_cli_failure_releases_untouched_draft(self):
        self.q.fail_confirm = True
        rc, _, _ = self.reply()
        self.assertEqual(rc, 1)
        self.assertEqual(self._status(), "pending")

    def test_crash_after_claim_fails_closed(self):
        orig = self.q.run

        def crash(cli, db, a, stdin_bytes=None):
            if a[0] == "confirm":
                raise RuntimeError("crash")
            return orig(cli, db, a, stdin_bytes)
        with mock.patch.object(nw, "_run_cli", crash):
            with self.assertRaises(RuntimeError):
                self.reply()
        self.assertEqual(self._status(), "consuming")
        self.assertRejected(self.reply(mid="r2"), "not pending")

    def test_claim_loses_race(self):
        # Another process claims between selection and claim.
        real = nw._claim_preview

        def racing(d, path, transport, expect):
            data = json.loads(path.read_text())
            data["status"] = "consuming"
            path.write_text(json.dumps(data))
            return real(d, path, transport, expect)
        with mock.patch.object(nw, "_claim_preview", racing):
            self.assertRejected(self.reply(), "consuming")


class TestLegacyLongGrammarUnchanged(unittest.TestCase):
    def test_long_affirm_phrase_constant(self):
        self.assertEqual(nw._CONFIRM_AFFIRM, "I confirm this meal for Health Connect")

    def test_short_text_not_valid_long_consent(self):
        errs = nw._validate_consent("Да", "x", 1, "Oatmeal", "Ravil")
        self.assertTrue(errs)


if __name__ == "__main__":
    unittest.main()
