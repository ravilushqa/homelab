"""
Tests for personal-chat short consent (`personal-preview` / `personal-confirm`) in
agent-workflow/nutrition_writeback.py.

The scenarios run twice: against the in-memory fake queue and against the REAL
NutritionStore/nutrition_cli on a synthetic temp SQLite DB (isolated shim CLI under a
temp isolation root). The mutation-readiness gate is stubbed; no network, production
CLI, DB, token or record is touched. The "Да" texts are synthetic fixtures, not a
real user's consent.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.test_short_preview_consent import Base, MPBase, RID, T0, nw
from tests.test_short_consent_sqlite import PAYLOAD, RealBase, SqliteMixin, nq

NOTES = "Допущения: порция ~250 г, молоко 2.5%, 3 ложки сахара."


class PersonalMixin:
    scope = "personal-a"

    # ── adapters over fake / real queue ──
    def rid(self):
        return getattr(self.q, "rid", None) or RID

    def mutate(self, **kw):
        if hasattr(self.q, "update"):
            self.q.update(**kw)
        else:
            self.q.rec.update(kw)

    def edit(self, **changes):
        """Correction: same record id, version+1."""
        if hasattr(self.q, "update"):
            payload = dict(PAYLOAD, **changes)
            rc, out, err = nw._run_cli(self.q.shim, self.q.db,
                                       ["edit", self.q.rid, "--version",
                                        str(self.q.rec["client_record_version"])],
                                       json.dumps(payload).encode())
            self.assertEqual(rc, 0, err)
        else:
            self.q.rec.update(changes)
            self.q.rec["client_record_version"] += 1

    def live_record(self):
        return self.q.rec

    def pdir(self):
        return self.root / "personal-previews"

    def pfiles(self):
        return sorted(self.pdir().glob("preview-*.json"))

    def pdata(self):
        return [json.loads(f.read_text()) for f in self.pfiles()]

    def private_file(self, name, text):
        p = self.root / name
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(p, 0o600)
        return p

    def consent_file(self, name, text, age_s=0.0):
        """Consent file with a precise real mtime (now - age_s): workflow freshness."""
        p = self.private_file(name, text)
        t = time.time_ns() - int(age_s * 1e9)
        os.utime(p, ns=(t, t))
        return p

    def ppreview(self, scope=None, notes=NOTES, rid=None, version=None, shash=None, ttl=900,
                 attest=True, notesfile=None, confirmer="Ravil"):
        dv, dh = self.displayed()
        nf = notesfile if notesfile is not None else (
            self.private_file("notes.txt", notes) if notes is not None else None)
        return self.run_cmd(
            nw.cmd_personal_preview, id=rid or self.rid(), confirmer=confirmer,
            scope=scope or self.scope, attest_personal_chat=attest,
            expected_version=dv if version is None else version,
            expected_snapshot_hash=dh if shash is None else shash,
            notes_file=str(nf) if nf else None, ttl_seconds=ttl)

    def pconfirm(self, text="Да", scope=None, attest=True, mode=0o600, path=None):
        self.tf_n += 1
        p = path or self.consent_file(f"consent{self.tf_n}.txt", text)
        if path is None:
            os.chmod(p, mode)
        return self.run_cmd(nw.cmd_personal_confirm, scope=scope or self.scope,
                            attest_personal_chat=attest, consent_file=str(p))

    def assertNoMutation(self, res, needle=None):
        rc, out, err = res
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.q.confirm_calls, 0, "queue confirm must not be reached")
        self.assertEqual(self.live_record()["state"], "draft")
        if needle:
            self.assertIn(needle, (out + err).lower())


class PersonalTests(PersonalMixin):
    # ── happy path / audit ──
    def test_actual_bare_da_confirms_without_ids_or_receipts(self):
        rc, out, err = self.ppreview()
        self.assertEqual(rc, 0, out + err)
        shown = json.loads(out)
        snap = nw._snapshot_of(self.live_record())
        self.assertEqual(shown["display_text"], nw._compose_display(snap, NOTES))
        # ONE returned display text: wrapper summary of the SAVED values + agent notes
        for needle in ("Oatmeal", "300 ккал", "белки 10 г", "углеводы 50 г", "жиры 5 г",
                       snap["start_time"], snap["end_time"], NOTES):
            self.assertIn(needle, shown["display_text"])
        self.assertEqual(shown["provenance_level"], "agent_conversation_attested")
        rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.assertEqual(res["action"], "personal-confirm")
        self.assertEqual(res["state"], "confirmed")
        self.assertEqual(self.live_record()["state"], "confirmed")
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(oct(self.pfiles()[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.pdir().stat().st_mode & 0o777), "0o700")

    def test_zapishi_and_trailing_newline_accepted(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.pconfirm("  Запиши!\n")[0], 0)

    def test_raw_consent_text_is_audited_unchanged(self):
        self.assertEqual(self.ppreview()[0], 0)
        raw = "  Да, запиши.\n"
        rc, out, err = self.pconfirm(raw)
        self.assertEqual(rc, 0, out + err)
        rec = self.pdata()[0]
        self.assertEqual(rec["status"], "consumed")
        self.assertEqual(rec["consent_raw_text"], raw)
        self.assertEqual(rec["consent_file_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(json.loads(out)["personal_consent"]["raw_text"], raw)
        self.assertEqual(rec["provenance_level"], "agent_conversation_attested")
        # nothing fabricated: no user timestamp/author/message id fields
        for k in ("reply_timestamp", "sender_id", "message_id", "reply_to_message_id",
                  "consent_timestamp", "consent_author"):
            self.assertNotIn(k, rec)

    def test_snapshot_and_text_saved(self):
        self.assertEqual(self.ppreview()[0], 0)
        rec = self.pdata()[0]
        self.assertEqual(rec["notes_text"], NOTES)
        self.assertEqual(rec["display_text_sha256"],
                         hashlib.sha256(rec["display_text"].encode()).hexdigest())
        self.assertIn(rec["snapshot"]["start_time"], rec["display_text"])
        self.assertIn(rec["snapshot"]["end_time"], rec["display_text"])
        self.assertIn("created_wall_ns", rec)
        self.assertEqual(rec["snapshot_hash"], self.displayed()[1])
        self.assertIn("description", rec["snapshot"])
        self.assertEqual(rec["mode"], "personal_chat_scope")

    # ── scope / attestation ──
    def test_attestation_and_scope_required(self):
        self.assertNoMutation(self.ppreview(attest=False), "attest")
        self.assertNoMutation(self.ppreview(scope="-100123"), "--scope")
        self.assertNoMutation(self.ppreview(scope="has space"), "--scope")
        self.assertEqual(self.ppreview()[0], 0)
        self.assertNoMutation(self.pconfirm(attest=False), "attest")

    def test_wrong_scope_cannot_confirm(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertNoMutation(self.pconfirm(scope="personal-b"), "no active preview")
        self.assertEqual(self.pdata()[0]["status"], "pending")

    def test_legacy_short_confirm_does_not_see_personal_previews(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertFalse((self.root / "pending-previews").exists())

    # ── supersession / ambiguity / expiry ──
    def test_replacement_supersedes_and_needs_fresh_consent(self):
        self.assertEqual(self.ppreview()[0], 0)
        old = self.pdata()[0]["preview_id"]
        self.edit(energy_kcal=350.0)
        rc, out, err = self.ppreview()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["superseded_previews"], 1)
        by_id = {d["preview_id"]: d for d in self.pdata()}
        self.assertEqual(by_id[old]["status"], "superseded")
        self.assertEqual(self.live_record()["state"], "draft")  # draft not mutated
        # consent file written for the OLD preview is irrelevant: one active slot only
        rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 0, out + err)
        confirmed = [d for d in self.pdata() if d["status"] == "consumed"]
        self.assertEqual(len(confirmed), 1)
        self.assertNotEqual(confirmed[0]["preview_id"], old)
        self.assertEqual(self.live_record()["energy_kcal"], 350.0)

    def test_superseded_other_draft_is_not_mutated(self):
        self.assertEqual(self.ppreview()[0], 0)
        before = dict(self.live_record())
        self.edit(name="Oatmeal")  # touch same id: version bump only
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.live_record()["state"], "draft")
        self.assertNotEqual(before["client_record_version"],
                            self.live_record()["client_record_version"])

    def test_two_active_previews_same_scope_fail_closed(self):
        self.assertEqual(self.ppreview()[0], 0)
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["preview_id"] = "f" * 32
        (self.pdir() / f"preview-{d['preview_id']}.json").write_text(json.dumps(d))
        os.chmod(self.pdir() / f"preview-{d['preview_id']}.json", 0o600)
        self.assertNoMutation(self.pconfirm("Да"), "ambiguous")
        self.assertNoMutation(self.ppreview(), "ambiguous")

    def test_other_scope_is_independent_and_untouched(self):
        self.assertEqual(self.ppreview(scope="personal-a")[0], 0)
        self.assertEqual(self.ppreview(scope="personal-b")[0], 0)
        st = {d["scope"]: d["status"] for d in self.pdata()}
        self.assertEqual(st, {"personal-a": "pending", "personal-b": "pending"})

    def test_expiry(self):
        self.assertEqual(self.ppreview(ttl=60)[0], 0)
        self.now = T0 + timedelta(seconds=61)
        self.assertNoMutation(self.pconfirm("Да"), "expired")

    def test_ttl_bounds(self):
        self.assertNoMutation(self.ppreview(ttl=0), "ttl")
        self.assertNoMutation(self.ppreview(ttl=99999), "ttl")

    def test_expired_preview_replaced_without_ambiguity(self):
        self.assertEqual(self.ppreview(ttl=60)[0], 0)
        self.now = T0 + timedelta(seconds=120)
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(sorted(d["status"] for d in self.pdata()), ["expired", "pending"])
        self.assertEqual(self.pconfirm("Да")[0], 0)

    # ── corrections / drift ──
    def test_correction_invalidates_old_snapshot(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.edit(energy_kcal=400.0)
        self.assertNoMutation(self.pconfirm("Да"), "")
        self.assertEqual(self.live_record()["client_record_version"], 2)
        self.assertEqual(self.pdata()[0]["status"], "revoked")

    def test_same_version_content_drift_blocks_and_revokes(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.mutate(energy_kcal=999.0)
        self.assertNoMutation(self.pconfirm("Да"), "differs")
        self.mutate(energy_kcal=300.0)  # restoring never revives
        self.assertNoMutation(self.pconfirm("Да"))
        self.assertEqual(self.pdata()[0]["status"], "revoked")

    def test_description_drift_blocks(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.mutate(description="[AI estimate] changed")
        self.assertNoMutation(self.pconfirm("Да"), "differs")

    def test_preview_requires_displayed_version_and_hash(self):
        self.assertNoMutation(self.ppreview(version=7), "version")
        self.assertNoMutation(self.ppreview(shash="0" * 64), "content differs")
        self.assertNoMutation(self.ppreview(shash="xyz"), "sha-256")
        self.assertFalse(self.pdir().exists() and self.pfiles())

    def test_display_uses_exact_saved_values_not_notes(self):
        self.edit(energy_kcal=123.456)
        rc, out, err = self.ppreview(notes="Допущения: 999 ккал, 08:00")
        self.assertEqual(rc, 0, out + err)
        disp = json.loads(out)["display_text"]
        self.assertTrue(disp.startswith(nw._canonical_summary(nw._snapshot_of(
            self.live_record()))))
        self.assertIn("123.456 ккал", disp)

    def test_notes_optional_and_validated(self):
        self.assertEqual(self.ppreview(notes=None)[0], 0)
        self.assertEqual(self.pdata()[0]["notes_text"], None)
        self.assertNoMutation(self.ppreview(notes="  \n"), "empty")
        self.assertNoMutation(self.ppreview(notes="a\x00b"), "control")

    def test_non_draft_rejected_at_preview(self):
        self.mutate(state="cancelled")
        rc, out, err = self.ppreview()
        self.assertEqual(rc, 1)
        self.assertIn("draft", out + err)

    # ── negative / conditional input ──
    def test_negative_conditional_and_combined_text_never_authorize(self):
        self.assertEqual(self.ppreview()[0], 0)
        for t in ("нет", "не записывай", "no", "don't record it", "yes, but not now",
                  "да, если калории верные", "maybe", "да, но поправь белки",
                  "да, поменяй на 350", "Да\nнет", "yes please record everything now",
                  "", "   ", "ок", "Да да да да да да да да да да да да да да да да"):
            self.assertNoMutation(self.pconfirm(t))
        self.assertEqual(self.pdata()[0]["status"], "pending")  # still pending, untouched

    def test_insecure_consent_file(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertNoMutation(self.pconfirm("Да", mode=0o644))
        link = self.root / "link.txt"
        real = self.private_file("real.txt", "Да")
        os.symlink(real, link)
        self.assertNoMutation(self.pconfirm(path=link))
        self.assertNoMutation(self.pconfirm(path=self.root / "missing.txt"))

    def test_binary_or_oversize_consent_file(self):
        self.assertEqual(self.ppreview()[0], 0)
        p = self.root / "bin.txt"
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"\xff\xfe\xfa")
        os.close(fd)
        self.assertNoMutation(self.pconfirm(path=p))
        self.assertNoMutation(self.pconfirm("Да" + " " * 300))

    # ── replay / duplicate ──
    def test_replay_after_confirmed_is_read_only(self):
        self.assertEqual(self.ppreview()[0], 0)
        p = self.consent_file("c.txt", "Да")
        self.assertEqual(self.pconfirm(path=p)[0], 0)
        snap = self.pfiles()[0].read_bytes()
        calls = self.q.confirm_calls
        rc, out, err = self.pconfirm(path=p)               # same file
        self.assertEqual(rc, 1)
        self.assertIn("replay", out + err)
        rc, out, err = self.pconfirm("Да")                 # fresh duplicate "Да"
        self.assertEqual(rc, 1)
        self.assertIn("already confirmed", out + err)
        self.assertEqual(self.q.confirm_calls, calls)
        self.assertEqual(self.pfiles()[0].read_bytes(), snap)
        self.assertEqual(len(self.pfiles()), 1)

    def test_new_preview_after_confirm_needs_its_own_consent(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.pconfirm("Да")[0], 0)
        self.mutate(state="draft")  # (fake/real: simulate a later new draft slot)
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.q.confirm_calls, 1)  # preview alone never confirms

    def test_interrupted_confirm_stays_consuming_and_fails_closed(self):
        self.assertEqual(self.ppreview()[0], 0)
        with mock.patch.object(nw, "_finish_preview", lambda *a, **k: None):
            self.assertEqual(self.pconfirm("Да")[0], 0)  # crash before consumed marker
        self.assertEqual(self.pdata()[0]["status"], "consuming")
        self.mutate(state="draft")
        calls = self.q.confirm_calls
        self.assertEqual(self.pconfirm("Да")[0], 1)
        self.assertEqual(self.q.confirm_calls, calls)

    def test_privacy_gate_failure_blocks_everything(self):
        self.assertEqual(self.ppreview()[0], 0)
        with mock.patch.object(nw, "_check_mutation_readiness",
                               lambda *a, **k: ["privacy gate failed"]):
            self.assertNoMutation(self.pconfirm("Да"), "readiness")
        self.assertEqual(self.pdata()[0]["status"], "pending")  # nothing claimed
        self.assertEqual(self.pconfirm("Да")[0], 0)             # gate fixed: still works

    # ── private store defenses ──
    def test_symlinked_store_rejected(self):
        os.mkdir(self.root / "real", 0o700)
        os.symlink(self.root / "real", self.pdir())
        self.assertNoMutation(self.ppreview(), "")
        self.assertNoMutation(self.pconfirm("Да"), "")

    def test_group_readable_store_rejected(self):
        self.assertEqual(self.ppreview()[0], 0)
        os.chmod(self.pdir(), 0o750)
        self.assertNoMutation(self.pconfirm("Да"), "insecure")

    def test_foreign_or_group_readable_entry_fails_closed(self):
        self.assertEqual(self.ppreview()[0], 0)
        os.chmod(self.pfiles()[0], 0o644)
        self.assertNoMutation(self.pconfirm("Да"), "failing closed")
        os.chmod(self.pfiles()[0], 0o600)
        (self.pdir() / "stray.txt").write_text("x")
        self.assertNoMutation(self.pconfirm("Да"), "failing closed")
        self.assertNoMutation(self.ppreview(), "unreadable")

    def test_tampered_snapshot_rejected(self):
        self.assertEqual(self.ppreview()[0], 0)
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["snapshot"]["energy_kcal"] = 1.0
        f.write_text(json.dumps(d))
        self.assertNoMutation(self.pconfirm("Да"), "tampered")

    def test_preview_text_file_must_be_private(self):
        p = self.private_file("open.txt", NOTES)
        os.chmod(p, 0o644)
        self.assertNoMutation(self.ppreview(notesfile=p), "")

    # ── review follow-ups: replay scope, freshness, tamper, contract ──
    def test_used_consent_file_cannot_confirm_next_meal(self):
        self.assertEqual(self.ppreview()[0], 0)
        old = self.consent_file("a.txt", "Да")
        self.assertEqual(self.pconfirm(path=old)[0], 0)
        self.mutate(state="draft")           # a new draft/preview for the scope
        self.assertEqual(self.ppreview()[0], 0)
        calls = self.q.confirm_calls
        rc, out, err = self.pconfirm(path=old)
        self.assertEqual(rc, 1)
        self.assertIn("already used", out + err)
        self.assertEqual(self.q.confirm_calls, calls)
        self.assertEqual(self.live_record()["state"], "draft")
        self.assertEqual(self.pconfirm("Да")[0], 0)   # same text, NEW fresh file: allowed

    def test_used_consent_file_rejected_in_another_scope(self):
        self.assertEqual(self.ppreview(scope="personal-a")[0], 0)
        f = self.consent_file("x.txt", "Да")
        self.assertEqual(self.pconfirm(path=f, scope="personal-a")[0], 0)
        self.mutate(state="draft")
        self.assertEqual(self.ppreview(scope="personal-b")[0], 0)
        rc, out, err = self.pconfirm(path=f, scope="personal-b")
        self.assertEqual(rc, 1)
        self.assertIn("already used", out + err)
        self.assertEqual(self.q.confirm_calls, 1)           # only the first confirm
        self.assertEqual(self.live_record()["state"], "draft")

    def test_old_unused_consent_predating_replacement_rejected(self):
        self.assertEqual(self.ppreview()[0], 0)
        stale = self.consent_file("stale.txt", "Да")
        self.edit(energy_kcal=350.0)
        self.assertEqual(self.ppreview()[0], 0)          # replacement preview
        rc, out, err = self.pconfirm(path=stale)
        self.assertEqual(rc, 1)
        self.assertIn("predates", out + err)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.live_record()["state"], "draft")
        self.assertEqual(sorted(d["status"] for d in self.pdata()), ["pending", "superseded"])
        self.assertEqual(self.pconfirm("Да")[0], 0)      # fresh actual file accepted

    def test_consent_older_than_preview_via_mtime(self):
        self.assertEqual(self.ppreview()[0], 0)
        old = self.consent_file("old.txt", "Да", age_s=3600)
        self.assertNoMutation(self.pconfirm(path=old), "predates")

    def test_display_text_and_hash_tamper_fail_closed(self):
        for field, value in (("display_text", "Записать?"),
                             ("display_text_sha256", "0" * 64),
                             ("notes_text", "подменённые допущения")):
            with self.subTest(field=field):
                self.setUp_fresh_scope()
                f = self.pfiles()[0]
                d = json.loads(f.read_text())
                d[field] = value
                f.write_text(json.dumps(d))
                self.assertNoMutation(self.pconfirm("Да"), "tampered")

    def setUp_fresh_scope(self):
        for f in self.pfiles() if self.pdir().exists() else []:
            f.unlink()
        self.assertEqual(self.ppreview()[0], 0)

    def test_time_or_macro_tamper_in_snapshot_fails_closed(self):
        for key, value in (("start_time", "2026-01-01T00:00:00Z"), ("fat_g", 1.0)):
            with self.subTest(key=key):
                self.setUp_fresh_scope()
                f = self.pfiles()[0]
                d = json.loads(f.read_text())
                d["snapshot"][key] = value
                f.write_text(json.dumps(d))
                self.assertNoMutation(self.pconfirm("Да"), "tampered")

    def test_malformed_timestamps_ttl_and_structure_fail_closed_without_crash(self):
        cases = {
            "expires_at": "not-a-date",
            "created_at": None,
            "preview_id": "short",
            "created_wall_ns": "x",
            "confirmer": "",
            "client_record_version": "1",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.setUp_fresh_scope()
                f = self.pfiles()[0]
                d = json.loads(f.read_text())
                d[key] = value
                f.write_text(json.dumps(d))
                rc, out, err = self.pconfirm("Да")
                self.assertEqual(rc, 1, out + err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.q.confirm_calls, 0)
        # over-max TTL and future creation
        self.setUp_fresh_scope()
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["expires_at"] = (T0 + timedelta(hours=5)).isoformat()
        f.write_text(json.dumps(d))
        self.assertNoMutation(self.pconfirm("Да"), "ttl")
        self.setUp_fresh_scope()
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["created_at"] = (T0 + timedelta(hours=1)).isoformat()
        d["expires_at"] = (T0 + timedelta(hours=1, minutes=5)).isoformat()
        f.write_text(json.dumps(d))
        self.assertNoMutation(self.pconfirm("Да"), "future")

    def test_foreign_record_in_store_fails_closed(self):
        self.assertEqual(self.ppreview()[0], 0)
        junk = self.pdir() / "preview-junk.json"
        junk.write_text(json.dumps({"schema": 1, "status": "pending"}))
        os.chmod(junk, 0o600)
        self.assertNoMutation(self.pconfirm("Да"), "failing closed")
        self.assertNoMutation(self.ppreview(), "failing closed")

    def test_no_fake_message_id_fields_and_personal_key_names(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.pconfirm("Да")[0], 0)
        rec = self.pdata()[0]
        self.assertIn("consent_file_key", rec)
        for k in ("reply_message_id", "used_reply_ids", "reply_to_message_id",
                  "preview_message_id", "chat_id", "user_id"):
            self.assertNotIn(k, rec)

    def test_released_preview_records_consent_file_keys_not_reply_ids(self):
        self.assertEqual(self.ppreview()[0], 0)
        real = nw._run_cli
        if not hasattr(self.q, "fail_confirm"):
            def flaky(cli, db, a, stdin_bytes=None):
                return (1, "", "transient") if a[0] == "confirm" else real(cli, db, a, stdin_bytes)
            ctx = mock.patch.object(nw, "_run_cli", flaky)
        else:
            self.q.fail_confirm = True
            ctx = contextlib.nullcontext()
        with ctx:
            self.assertEqual(self.pconfirm("Да")[0], 1)
        rec = self.pdata()[0]
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(len(rec["used_consent_file_keys"]), 1)
        self.assertNotIn("used_reply_ids", rec)
        self.assertNotIn("consent_file_key", rec)

    def test_invalid_helper_success_output_is_uncertain_not_fabricated(self):
        self.assertEqual(self.ppreview()[0], 0)

        def fake(cli, db, rid, version, name, confirmer, pre_confirm=None, **kw):
            self.assertIsNone(pre_confirm(self.live_record()))
            print("not json")
            return 0
        with mock.patch.object(nw, "_confirm_with_readback", fake):
            rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 1)
        self.assertIn("uncertain", err)
        self.assertNotIn('"ok": true', out)
        self.assertEqual(out, "")
        self.assertEqual(self.pdata()[0]["status"], "consumed")   # no replay
        self.assertEqual(self.pconfirm("Да")[0], 1)

    def test_helper_success_for_wrong_state_is_uncertain(self):
        self.assertEqual(self.ppreview()[0], 0)

        def fake(cli, db, rid, version, name, confirmer, pre_confirm=None, **kw):
            pre_confirm(self.live_record())
            print(json.dumps({"ok": True, "state": "draft", "client_record_id": rid,
                              "client_record_version": version}))
            return 0
        with mock.patch.object(nw, "_confirm_with_readback", fake):
            rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 1)
        self.assertIn("uncertain", err)

    def test_identifiers_are_validated_as_supplied_not_repaired(self):
        self.assertNoMutation(self.ppreview(scope="personal-a\n"), "--scope")
        self.assertNoMutation(self.ppreview(scope=" personal-a"), "--scope")
        dv, dh = self.displayed()
        self.assertNoMutation(self.ppreview(shash=dh.upper()), "sha-256")
        self.assertNoMutation(self.ppreview(shash=" " + dh), "sha-256")
        self.assertNoMutation(self.ppreview(confirmer=" Ravil"), "confirmer")
        self.assertEqual(self.ppreview()[0], 0)
        self.assertNoMutation(self.pconfirm("Да", scope="personal-a\n"), "--scope")

    def test_no_new_sql_row_for_replacement_or_correction(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.edit(energy_kcal=350.0)
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.pconfirm("Да")[0], 0)
        if hasattr(self.q, "confirmed_count"):
            import sqlite3
            conn = sqlite3.connect(str(self.q.db))
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM nutrition_queue").fetchone()[0], 1)
            finally:
                conn.close()

    def test_old_commands_unchanged_in_parser(self):
        p = nw.build_parser()
        for argv in (["preview", "--id", "x", "--confirmer", "n", "--user-id", "u",
                      "--chat-id", "c", "--preview-message-id", "m",
                      "--preview-sent-at", "2026-10-12T12:00:00Z",
                      "--expected-version", "1", "--expected-snapshot-hash", "0" * 64],
                     ["short-confirm", "--transport-file", "t"],
                     ["personal-preview", "--scope", "s", "--attest-personal-chat", "--id", "x",
                      "--confirmer", "n", "--expected-version", "1",
                      "--expected-snapshot-hash", "0" * 64, "--notes-file", "f"],
                     ["personal-confirm", "--scope", "s", "--attest-personal-chat",
                      "--consent-file", "f"]):
            self.assertIsNotNone(p.parse_args(argv).command)


class TestPersonalFake(PersonalTests, Base):
    pass


class TestPersonalRealSqlite(PersonalTests, RealBase):
    def test_confirmed_queue_row_matches_previewed_snapshot(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.assertEqual(self.pconfirm("Да")[0], 0)
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(nq.snapshot_hash_of_row(self.q.rec), self.pdata()[0]["snapshot_hash"])

    def test_late_same_version_mutation_after_pre_check_never_confirms(self):
        self.assertEqual(self.ppreview()[0], 0)
        self.q.before_confirm = lambda q: q.update(energy_kcal=999.0)
        self.assertEqual(self.pconfirm("Да")[0], 1)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.rec["state"], "draft")
        self.assertEqual(self.q.confirmed_count(), 0)
        self.assertEqual(self.pdata()[0]["status"], "revoked")
        self.q.update(energy_kcal=300.0)
        self.q.before_confirm = None
        self.assertEqual(self.pconfirm("Да")[0], 1)
        self.assertEqual(self.q.confirmed_count(), 0)

    def test_cli_refusal_untouched_draft_releases_and_consent_not_reusable(self):
        self.assertEqual(self.ppreview()[0], 0)
        real = nw._run_cli

        def flaky(cli, db, a, stdin_bytes=None):
            if a[0] == "confirm":
                return 1, "", "transient"
            return real(cli, db, a, stdin_bytes)
        p = self.consent_file("c1.txt", "Да")
        with mock.patch.object(nw, "_run_cli", flaky):
            self.assertEqual(self.pconfirm(path=p)[0], 1)
        self.assertEqual(self.pdata()[0]["status"], "pending")
        self.assertEqual(self.q.confirmed_count(), 0)
        rc, out, err = self.pconfirm(path=p)  # same unchanged file: replay
        self.assertEqual(rc, 1)
        self.assertIn("already used", out + err)
        self.assertEqual(self.pconfirm("Да")[0], 0)  # a NEW actual reply works
        self.assertEqual(self.q.confirmed_count(), 1)


class TestPersonalRealMultiprocess(SqliteMixin, PersonalMixin, MPBase):
    def make_queue(self):
        return self.build_real_queue()

    def setUp(self):
        super().setUp()
        self.patches[0].stop()
        p = mock.patch.object(nw, "_resolve", lambda a: (self.q.shim, self.q.db, self.root))
        p.start()
        self.patches[0] = p
        self.create_draft()

    def displayed(self):
        rc, out, err = nw._run_cli(self.q.shim, self.q.db, ["status", self.q.rid])
        rec = json.loads(out)
        return rec["client_record_version"], nw._record_snapshot_hash(rec)

    def job(self, path):
        return lambda: self.run_cmd(nw.cmd_personal_confirm, scope=self.scope,
                                    attest_personal_chat=True, consent_file=str(path))[0]

    def test_concurrent_confirms_exactly_one_winner(self):
        self.assertEqual(self.ppreview()[0], 0)
        files = [self.consent_file(f"mp{i}.txt", "Да") for i in range(5)]
        rcs = self.spawn([self.job(f) for f in files])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(self.pdata()[0]["status"], "consumed")

    def test_concurrent_confirm_vs_content_mutation(self):
        for i in range(3):
            if i:
                self.create_draft()
            self.assertEqual(self.ppreview()[0], 0)
            tf = self.consent_file(f"race{i}.txt", "Да")
            rcs = self.spawn([self.job(tf), lambda: self.q.update(fat_g=50.0) or 0])
            rec = self.q.rec
            if rec["state"] == "confirmed":
                self.assertEqual((rcs[0], rec["fat_g"]), (0, 5.0), (i, rcs))
            else:
                self.assertEqual((rcs[0], rec["fat_g"]), (1, 50.0), (i, rcs))
        conn = __import__("sqlite3").connect(str(self.q.db))
        try:
            n = conn.execute("SELECT COUNT(*) FROM nutrition_queue WHERE state='confirmed' "
                             "AND fat_g != 5.0").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_concurrent_previews_leave_one_active(self):
        def reg():
            return self.run_cmd(nw.cmd_personal_preview, id=self.q.rid, confirmer="Ravil",
                                scope=self.scope, attest_personal_chat=True,
                                expected_version=self.displayed()[0],
                                expected_snapshot_hash=self.displayed()[1],
                                notes_file=str(self.private_file("t.txt", NOTES)),
                                ttl_seconds=900)[0]
        rcs = self.spawn([reg, reg, reg])
        self.assertEqual(rcs, [0, 0, 0])
        st = sorted(d["status"] for d in self.pdata())
        self.assertEqual(st, ["pending", "superseded", "superseded"])


if __name__ == "__main__":
    unittest.main()
