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
        self.on_confirm = None  # optional hook run before the confirm outcome

    def run(self, cli, db, cli_args, stdin_bytes=None):
        if cli_args[0] == "status":
            return 0, json.dumps(self.rec), ""
        if cli_args[0] == "confirm":
            self.confirm_calls += 1
            if self.on_confirm:
                self.on_confirm(self)
            if self.fail_confirm:
                return 1, "", "boom"
            # Contract of the real CLI: the expected hash is verified atomically
            # with the state change, before any mutation.
            if "--expected-snapshot-hash" in cli_args:
                exp = cli_args[cli_args.index("--expected-snapshot-hash") + 1]
                if nw._record_snapshot_hash(self.rec) != exp:
                    return 1, "", '{"error": "content differs from the expected snapshot"}'
            self.rec["state"] = "confirmed"
            self.rec["confirmed_by"] = cli_args[cli_args.index("--confirmer") + 1]
            self.rec["confirmed_at"] = "2026-10-12T12:01:00Z"
            return 0, json.dumps(self.rec), ""
        return 1, "", "unsupported"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_SCRATCH, prefix="test_shortprev_"))
        os.chmod(self.root, 0o700)
        self.q = self.make_queue()
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

    def make_queue(self):
        return FakeQueue()

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

    def displayed(self):
        """(version, hash) of the draft as currently displayed to the user."""
        rec = self.q.rec
        return rec["client_record_version"], nw._record_snapshot_hash(rec)

    def preview(self, msg="p1", sent=None, ttl=900, user="u1", chat="c1", thread="",
                rid=RID, version=None, shash=None):
        sent = sent or T0.isoformat()
        dv, dh = self.displayed()
        return self.run_cmd(
            nw.cmd_preview, id=rid, confirmer="Ravil", user_id=user, chat_id=chat,
            thread_id=thread, preview_message_id=msg, preview_sent_at=sent, ttl_seconds=ttl,
            expected_version=dv if version is None else version,
            expected_snapshot_hash=dh if shash is None else shash)

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


class TestExpectedBinding(Base):
    def test_requires_matching_displayed_version_and_hash(self):
        v, h = self.displayed()
        self.assertEqual(self.preview(version=v + 1)[0], 1)
        self.assertEqual(self.preview(shash="0" * 64)[0], 1)
        self.assertEqual(self.preview(shash="xyz")[0], 1)
        self.assertEqual(self.preview(version=0)[0], 1)

    def test_mutation_between_display_and_registration_rejected(self):
        v, h = self.displayed()           # user was shown this exact draft
        self.q.rec["protein_g"] = 99.0    # changed at the SAME version before registration
        res = self.preview(version=v, shash=h)
        self.assertEqual(res[0], 1)
        self.assertIn("differs", res[1] + res[2])
        self.assertFalse((self.root / "pending-previews").exists())

    def test_version_bump_between_display_and_registration_rejected(self):
        v, h = self.displayed()
        self.q.rec["client_record_version"] = v + 1
        self.assertEqual(self.preview(version=v, shash=h)[0], 1)

    def test_draft_output_exposes_snapshot_hash(self):
        self.assertEqual(self.displayed()[1], nw._record_snapshot_hash(_draft()))


class TestFailedConfirmRelease(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)

    def _status(self):
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        return json.loads(f.read_text())["status"]

    def test_failed_confirm_with_changed_same_version_content_revokes(self):
        self.q.fail_confirm = True
        self.q.on_confirm = lambda q: q.rec.__setitem__("energy_kcal", 777.0)
        self.assertEqual(self.reply()[0], 1)
        self.assertEqual(self._status(), "revoked")
        # even after the content is restored, the authorization is gone
        self.q.rec["energy_kcal"] = 300.0
        self.q.confirm_calls = 0
        self.assertRejected(self.reply(mid="r2"), "not pending")

    def test_failed_confirm_with_state_change_revokes(self):
        self.q.fail_confirm = True
        self.q.on_confirm = lambda q: q.rec.__setitem__("state", "cancelled")
        self.assertEqual(self.reply()[0], 1)
        self.assertEqual(self._status(), "revoked")

    def test_failed_confirm_untouched_draft_releases(self):
        self.q.fail_confirm = True
        self.assertEqual(self.reply()[0], 1)
        self.assertEqual(self._status(), "pending")


class TestUnreadableStoreFailsClosed(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)
        self.d = self.root / "pending-previews"

    def _plant(self, name, content="{not json", mode=0o600):
        p = self.d / name
        p.write_text(content)
        os.chmod(p, mode)
        return p

    def test_corrupt_file_blocks_confirm_and_registration(self):
        self._plant("preview-corrupt.json")
        self.assertRejected(self.reply(), "unreadable")
        self.assertEqual(self.preview(msg="p2")[0], 1)

    def test_insecure_mode_file_blocks_confirm(self):
        self._plant("preview-open.json", "{}", mode=0o644)
        self.assertRejected(self.reply(), "unreadable")

    def test_symlink_entry_blocks_confirm(self):
        target = self.root / "elsewhere.json"
        target.write_text("{}")
        os.symlink(target, self.d / "preview-link.json")
        self.assertRejected(self.reply(), "unreadable")

    def test_foreign_entry_blocks_confirm(self):
        self._plant("stray.txt", "x")
        self.assertRejected(self.reply(), "unreadable")

    def test_unsupported_schema_blocks_confirm(self):
        self._plant("preview-schema.json", json.dumps({"schema": 99}))
        self.assertRejected(self.reply(), "unreadable")


class TestReplayAfterRelease(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)

    def _rec(self):
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        return json.loads(f.read_text())

    def test_same_reply_rejected_after_untouched_release(self):
        self.q.fail_confirm = True
        self.assertEqual(self.reply(mid="same")[0], 1)
        self.assertEqual(self._rec()["status"], "pending")
        self.assertEqual(self._rec()["used_reply_ids"], ["same"])
        self.q.fail_confirm = False
        before = self.q.confirm_calls
        res = self.reply(mid="same")
        self.assertEqual(res[0], 1)
        self.assertIn("already used", (res[1] + res[2]).lower())
        self.assertEqual(self.q.confirm_calls, before)
        self.assertEqual(self.q.rec["state"], "draft")

    def test_new_actual_reply_accepted_after_release(self):
        self.q.fail_confirm = True
        self.assertEqual(self.reply(mid="first")[0], 1)
        self.q.fail_confirm = False
        self.assertEqual(self.reply(mid="second")[0], 0)
        self.assertEqual(self.q.rec["state"], "confirmed")
        self.assertEqual(self._rec()["status"], "consumed")

    def test_used_ids_accumulate_over_repeated_failures(self):
        self.q.fail_confirm = True
        for mid in ("a", "b"):
            self.assertEqual(self.reply(mid=mid)[0], 1)
        self.assertEqual(self._rec()["used_reply_ids"], ["a", "b"])
        for mid in ("a", "b"):
            self.assertEqual(self.reply(mid=mid)[0], 1)
        self.assertEqual(self._rec()["used_reply_ids"], ["a", "b"])


class TestClaimTimeRecheck(Base):
    """Anything changing between selection/readiness and the claim lock must fail closed."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.preview()[0], 0)
        self.d = self.root / "pending-previews"
        self.pfile = next(self.d.glob("preview-*.json"))

    def reply_with_hook(self, hook, **kw):
        def readiness(*a, **k):
            hook()
            return []
        with mock.patch.object(nw, "_check_mutation_readiness", readiness):
            return self.reply(**kw)

    def test_duplicate_file_for_same_target_appears(self):
        def hook():
            dup = self.d / "preview-dup.json"
            dup.write_text(self.pfile.read_text())
            os.chmod(dup, 0o600)
        self.assertRejected(self.reply_with_hook(hook))
        self.assertEqual(json.loads(self.pfile.read_text())["status"], "pending")

    def test_corrupt_file_appears(self):
        def hook():
            c = self.d / "preview-corrupt.json"
            c.write_text("{broken")
            os.chmod(c, 0o600)
        self.assertRejected(self.reply_with_hook(hook), "unreadable")

    def test_matched_file_binding_changed(self):
        def hook():
            data = json.loads(self.pfile.read_text())
            data["expires_at"] = (T0 + timedelta(days=30)).isoformat()
            self.pfile.write_text(json.dumps(data))
        res = self.reply_with_hook(hook)
        self.assertRejected(res)
        self.assertEqual(json.loads(self.pfile.read_text())["status"], "pending")

    def test_matched_file_snapshot_hash_swapped(self):
        def hook():
            data = json.loads(self.pfile.read_text())
            data["snapshot_hash"] = "0" * 64
            self.pfile.write_text(json.dumps(data))
        self.assertRejected(self.reply_with_hook(hook))

    def test_superseded_between_selection_and_claim(self):
        self.assertEqual(self.reply_with_hook(lambda: self.preview(msg="p2")[0])[0], 1)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.q.rec["state"], "draft")
        statuses = sorted(json.loads(f.read_text())["status"] for f in self.d.glob("preview-*.json"))
        self.assertEqual(statuses, ["pending", "superseded"])

    def test_content_drift_after_selection_revokes_and_restore_does_not_revive(self):
        self.assertEqual(
            self.reply_with_hook(lambda: self.q.rec.__setitem__("energy_kcal", 999.0))[0], 1)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(json.loads(self.pfile.read_text())["status"], "revoked")
        self.q.rec["energy_kcal"] = 300.0   # restore: must not revive consent
        self.assertRejected(self.reply(mid="r2"), "not pending")


class FileQueue:
    """Cross-process fake queue: JSON state + flock, so forked children share it."""

    def __init__(self, root):
        self.path = root / "queue.json"
        self.lockp = root / "queue.lock"
        self.confirm_delay = 0.0
        self.save({"rec": _draft(), "confirm_calls": 0})

    def _locked(self):
        import fcntl
        fd = os.open(str(self.lockp), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, st):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.path)

    def update(self, **kw):
        fd = self._locked()
        try:
            st = self.load()
            st["rec"].update(kw)
            self.save(st)
        finally:
            os.close(fd)

    @property
    def rec(self):
        return self.load()["rec"]

    @property
    def confirm_calls(self):
        return self.load()["confirm_calls"]

    @confirm_calls.setter
    def confirm_calls(self, v):
        pass

    def run(self, cli, db, cli_args, stdin_bytes=None):
        import time
        if cli_args[0] == "status":
            return 0, json.dumps(self.rec), ""
        if cli_args[0] == "confirm":
            time.sleep(self.confirm_delay)  # widen the race window
            fd = self._locked()
            try:
                st = self.load()
                st["confirm_calls"] += 1
                if "--expected-snapshot-hash" in cli_args:
                    exp = cli_args[cli_args.index("--expected-snapshot-hash") + 1]
                    if nw._record_snapshot_hash(st["rec"]) != exp:
                        self.save(st)
                        return 1, "", '{"error": "content differs from the expected snapshot"}'
                if st["rec"]["state"] != "draft":
                    self.save(st)
                    return 1, "", "not draft"
                st["rec"]["state"] = "confirmed"
                st["rec"]["confirmed_by"] = cli_args[cli_args.index("--confirmer") + 1]
                st["rec"]["confirmed_at"] = "2026-10-12T12:01:00Z"
                self.save(st)
                return 0, json.dumps(st["rec"]), ""
            finally:
                os.close(fd)
        return 1, "", "unsupported"


def _mp_ctx():
    import multiprocessing
    return multiprocessing.get_context("fork")  # children inherit the patches


class MPBase(Base):
    def make_queue(self):
        q = FileQueue(self.root)
        q.confirm_delay = 0.3
        return q

    def spawn(self, jobs, timeout=60):
        """Run callables in real processes released together by a Barrier.
        Each job returns a JSON-able value; results come back in job order."""
        ctx = _mp_ctx()
        barrier = ctx.Barrier(len(jobs))
        results = ctx.Queue()

        def runner(i, job):
            try:
                barrier.wait(timeout=30)
                results.put((i, job()))
            except BaseException as exc:  # report, never hang the parent
                results.put((i, f"EXC {exc!r}"))

        procs = [ctx.Process(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
        for pr in procs:
            pr.start()
        out = {}
        for _ in procs:
            i, v = results.get(timeout=timeout)
            out[i] = v
        for pr in procs:
            pr.join(timeout)
            self.assertEqual(pr.exitcode, 0)
        return [out[i] for i in range(len(jobs))]

    def write_reply(self, mid, reply_to="p1", text="Да"):
        self.tf_n += 1
        p = self.root / f"mp-reply-{self.tf_n}.json"
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"sender_id": "u1", "chat_id": "c1", "thread_id": "", "message_id": mid,
                       "timestamp": (T0 + timedelta(seconds=30)).isoformat(),
                       "reply_to_message_id": reply_to, "text": text}, fh)
        return str(p)

    def confirm_job(self, tf):
        return lambda: self.run_cmd(nw.cmd_short_confirm, transport_file=tf)[0]


class TestMultiprocess(MPBase):
    def test_concurrent_confirm_exactly_one_winner(self):
        self.assertEqual(self.preview()[0], 0)
        files = [self.write_reply(f"r{i}") for i in range(6)]
        rcs = self.spawn([self.confirm_job(f) for f in files])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.rec["state"], "confirmed")
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        self.assertEqual(json.loads(f.read_text())["status"], "consumed")

    def test_concurrent_replay_of_same_reply_file(self):
        self.assertEqual(self.preview()[0], 0)
        tf = self.write_reply("r1")
        rcs = self.spawn([self.confirm_job(tf) for _ in range(5)])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirm_calls, 1)

    def test_replay_after_success_in_new_process(self):
        self.assertEqual(self.preview()[0], 0)
        self.assertEqual(self.spawn([self.confirm_job(self.write_reply("r1"))]), [0])
        self.q.update(state="draft")
        before = self.q.confirm_calls
        self.assertEqual(self.spawn([self.confirm_job(self.write_reply("r2"))]), [1])
        self.assertEqual(self.q.confirm_calls, before)

    def test_concurrent_registration_same_message_one_winner(self):
        v, h = self.displayed()

        def reg():
            return self.preview(version=v, shash=h)[0]
        rcs = self.spawn([reg for _ in range(5)])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1, 1], rcs)
        self.assertEqual(len(list((self.root / "pending-previews").glob("preview-*.json"))), 1)

    def test_registration_vs_content_mutation_race(self):
        original = _draft()
        for i in range(6):
            self.q.update(**original)
            v, h = self.displayed()
            msg = f"race{i}"
            rcs = self.spawn([
                lambda: self.preview(msg=msg, version=v, shash=h)[0],
                lambda: self.q.update(energy_kcal=999.0) or 0,
            ])
            self.assertEqual(rcs[1], 0)
            # Content is now mutated at the SAME version: whether or not the
            # registration won the race, confirming must be impossible.
            res = self.reply(reply_to=msg, mid=f"rr{i}")
            self.assertEqual(res[0], 1, (i, rcs, res))
            self.assertEqual(self.q.confirm_calls, 0)
            self.assertEqual(self.q.rec["state"], "draft")

    def test_confirm_vs_content_mutation_race(self):
        self.assertEqual(self.preview()[0], 0)
        tf = self.write_reply("r1")
        rcs = self.spawn([self.confirm_job(tf), lambda: self.q.update(fat_g=50.0) or 0])
        # The expected snapshot is verified inside the queue transaction, so a
        # mutated record can NEVER reach confirmed, whichever side wins the race.
        rec = self.q.rec
        prev_hash = nw._snapshot_hash(nw._snapshot_of(_draft()))
        if rec["state"] == "confirmed":
            self.assertEqual(rcs[0], 0)
            self.assertEqual(nw._record_snapshot_hash(rec), prev_hash)
        else:
            self.assertEqual(rcs[0], 1)
            self.assertNotEqual(nw._record_snapshot_hash(rec), prev_hash)


class TestLegacyLongGrammarUnchanged(unittest.TestCase):
    def test_long_affirm_phrase_constant(self):
        self.assertEqual(nw._CONFIRM_AFFIRM, "I confirm this meal for Health Connect")

    def test_short_text_not_valid_long_consent(self):
        errs = nw._validate_consent("Да", "x", 1, "Oatmeal", "Ravil")
        self.assertTrue(errs)


if __name__ == "__main__":
    unittest.main()
