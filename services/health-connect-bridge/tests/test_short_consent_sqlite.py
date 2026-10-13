"""
Real-SQLite tests for the in-transaction snapshot binding of short final-preview
consent.

Unlike test_short_preview_consent (in-memory fake queue), these tests drive the
REAL NutritionStore / nutrition_cli through an isolated shim executable under a
temp isolation root, with a temp SQLite DB. The mutation-readiness gate is
stubbed; no production CLI, DB, token or network is touched.

The photo/text integration test uses SYNTHETIC fixtures and a synthetic
"authenticated transport" reply. It is NOT the actual user's consent and NOT
vision-model validation.
"""

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests.test_short_preview_consent import Base, MPBase, T0, nw

_BRIDGE = Path(__file__).resolve().parent.parent
_REAL_RUN_CLI = nw._run_cli

_qspec = importlib.util.spec_from_file_location("_nq_sqlite", _BRIDGE / "nutrition_queue.py")
nq = importlib.util.module_from_spec(_qspec)
sys.modules["_nq_sqlite"] = nq
_qspec.loader.exec_module(nq)

PAYLOAD = {
    "name": "Oatmeal", "start_time": "2026-10-12T12:00:00Z",
    "end_time": "2026-10-12T12:01:00Z", "energy_kcal": 300.0,
    "protein_g": 10.0, "carbohydrate_g": 50.0, "fat_g": 5.0,
    "description": "oats and milk",
}


class RealQueue:
    """Adapter exposing the FakeQueue surface on top of the real CLI + SQLite DB."""

    def __init__(self, shim, db):
        self.shim, self.db = shim, db
        self.rid = None
        self.confirm_calls = 0
        self.before_confirm = None  # hook: raw late mutation right before the CLI confirm

    def run(self, cli, db, cli_args, stdin_bytes=None):
        if cli_args[0] == "confirm":
            self.confirm_calls += 1
            if self.before_confirm:
                self.before_confirm(self)
        return _REAL_RUN_CLI(self.shim, self.db, cli_args, stdin_bytes)

    def raw(self, sql, params=()):
        conn = sqlite3.connect(str(self.db), timeout=30)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def update(self, **kw):
        sets = ", ".join(f"{k} = ?" for k in kw)
        self.raw(f"UPDATE nutrition_queue SET {sets} WHERE client_record_id = ?",
                 (*kw.values(), self.rid))

    @property
    def rec(self):
        conn = sqlite3.connect(str(self.db), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM nutrition_queue WHERE client_record_id = ?",
                               (self.rid,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def confirmed_count(self):
        conn = sqlite3.connect(str(self.db), timeout=30)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM nutrition_queue WHERE state = 'confirmed'").fetchone()[0]
        finally:
            conn.close()


class SqliteMixin:
    """Builds shim CLI + DB under the (already created) isolation root."""

    def build_real_queue(self):
        root = self.root
        parent = root / "pkgparent"
        parent.mkdir(mode=0o700)
        os.symlink(_BRIDGE, parent / "health_connect_bridge")
        shim = root / "nutrition-queue"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f"export PYTHONPATH={parent}\n"
            f"exec {sys.executable} -m health_connect_bridge.nutrition_cli \"$@\"\n")
        shim.chmod(stat.S_IRWXU)
        db = root / "queue.db"
        return RealQueue(shim, db)

    def create_draft(self, payload=None):
        """Create the draft through wrapper cmd_draft (real create + status readback)."""
        pf = self.root / "draft-input.json"
        pf.write_text(json.dumps(payload or PAYLOAD))
        rc, out, err = self.run_cmd(nw.cmd_draft, file=str(pf))
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.q.rid = res["client_record_id"]
        return res


class RealBase(SqliteMixin, Base):
    def make_queue(self):
        return self.build_real_queue()

    def setUp(self):
        super().setUp()
        # _resolve must point at the shim/DB/root; _run_cli goes to the real CLI
        # via the adapter (which counts confirms).
        self.patches[0].stop()
        p = mock.patch.object(nw, "_resolve", lambda a: (self.q.shim, self.q.db, self.root))
        p.start()
        self.patches[0] = p
        self.draft = self.create_draft()

    def preview(self, *a, **kw):
        kw.setdefault("rid", self.q.rid)
        return super().preview(*a, **kw)

    def displayed(self):
        return self.q.rec["client_record_version"], nw._record_snapshot_hash(
            self.status_record())

    def status_record(self):
        rc, out, err = nw._run_cli(self.q.shim, self.q.db, ["status", self.q.rid])
        self.assertEqual(rc, 0, err)
        return json.loads(out)


class TestStoreHashBinding(unittest.TestCase):
    """Queue-level: NutritionStore.confirm_entry(expected_snapshot_hash=...)."""

    def setUp(self):
        from tests.test_short_preview_consent import _SCRATCH
        import tempfile
        self.dir = Path(tempfile.mkdtemp(dir=_SCRATCH, prefix="test_shortsql_"))
        os.chmod(self.dir, 0o700)
        self.db = self.dir / "q.db"
        self.store = nq.NutritionStore(self.db)
        self.rec = self.store.create_entry(dict(PAYLOAD))
        self.rid = self.rec["client_record_id"]
        self.good = nq.snapshot_hash_of_row(self.store.get_entry(self.rid))

    def tearDown(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _confirm(self, **kw):
        return self.store.confirm_entry(self.rid, 1, nq.CONFIRM_AFFIRM_PHRASE, "Ravil", **kw)

    def _state(self):
        return self.store.get_entry(self.rid)["state"]

    def test_canonicalization_equals_wrapper(self):
        row = self.store.get_entry(self.rid)
        self.assertEqual(self.good, nw._record_snapshot_hash(row))

    def test_matching_hash_confirms(self):
        self.assertEqual(self._confirm(expected_snapshot_hash=self.good)["state"], "confirmed")

    def test_legacy_call_without_hash_confirms(self):
        self.assertEqual(self._confirm()["state"], "confirmed")

    def test_wrong_hash_fails_before_mutation(self):
        before = self.store.get_entry(self.rid)
        with self.assertRaises(nq.ValidationError):
            self._confirm(expected_snapshot_hash="0" * 64)
        self.assertEqual(self.store.get_entry(self.rid), before)
        self.assertEqual(self._state(), "draft")

    def test_malformed_hash_rejected_before_anything(self):
        before = self.store.get_entry(self.rid)
        for bad in ("xyz", "A" * 64, "0" * 63, "0" * 65, "", " " + "0" * 63):
            with self.assertRaises(nq.ValidationError, msg=repr(bad)):
                self._confirm(expected_snapshot_hash=bad)
        self.assertEqual(self.store.get_entry(self.rid), before)

    def test_same_version_raw_mutation_blocks_confirm(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE nutrition_queue SET energy_kcal = 999 WHERE client_record_id = ?",
                     (self.rid,))
        conn.commit()
        conn.close()
        with self.assertRaises(nq.ValidationError):
            self._confirm(expected_snapshot_hash=self.good)
        self.assertEqual(self._state(), "draft")
        self.assertEqual(self.store.get_confirmed_queue(), [])

    def test_description_mutation_blocks_confirm(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE nutrition_queue SET description = 'x' WHERE client_record_id = ?",
                     (self.rid,))
        conn.commit()
        conn.close()
        with self.assertRaises(nq.ValidationError):
            self._confirm(expected_snapshot_hash=self.good)
        self.assertEqual(self._state(), "draft")


class TestWrapperToIsolatedCli(RealBase):
    def test_short_confirm_happy_path_real_sqlite(self):
        self.assertEqual(self.preview()[0], 0)
        rc, out, err = self.reply("Да")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.q.rec["state"], "confirmed")
        self.assertEqual(self.q.rec["confirmed_by"], "Ravil")
        self.assertEqual(self.q.confirmed_count(), 1)

    def test_cli_rejects_mismatched_hash_without_mutation(self):
        rc, out, err = nw._run_cli(
            self.q.shim, self.q.db,
            ["confirm", self.q.rid, "--version", "1", "--affirm", nq.CONFIRM_AFFIRM_PHRASE,
             "--confirmer", "Ravil", "--expected-snapshot-hash", "0" * 64])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.q.rec["state"], "draft")
        rc, out, err = nw._run_cli(
            self.q.shim, self.q.db,
            ["confirm", self.q.rid, "--version", "1", "--affirm", nq.CONFIRM_AFFIRM_PHRASE,
             "--confirmer", "Ravil", "--expected-snapshot-hash", "nothex"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.q.rec["state"], "draft")

    def test_legacy_cli_confirm_without_hash_still_works(self):
        rc, out, err = nw._run_cli(
            self.q.shim, self.q.db,
            ["confirm", self.q.rid, "--version", "1", "--affirm", nq.CONFIRM_AFFIRM_PHRASE,
             "--confirmer", "Ravil"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.q.rec["state"], "confirmed")

    def test_late_same_version_mutation_never_reaches_confirmed(self):
        """Deterministic: mutate AFTER wrapper status/pre-check, right before CLI confirm."""
        self.assertEqual(self.preview()[0], 0)
        self.q.before_confirm = lambda q: q.update(energy_kcal=999.0)
        res = self.reply("Да")
        self.assertEqual(res[0], 1, res)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.rec["state"], "draft")      # never confirmed
        self.assertEqual(self.q.confirmed_count(), 0)
        f = next((self.root / "pending-previews").glob("preview-*.json"))
        self.assertEqual(json.loads(f.read_text())["status"], "revoked")
        # restoring the content must not revive the old consent
        self.q.update(energy_kcal=300.0)
        self.q.before_confirm = None
        n = self.q.confirm_calls
        self.assertRejected_calls(self.reply("Да", mid="r2"), n)

    def test_late_description_mutation_never_reaches_confirmed(self):
        self.assertEqual(self.preview()[0], 0)
        self.q.before_confirm = lambda q: q.update(description="[AI estimate] other")
        self.assertEqual(self.reply("Да")[0], 1)
        self.assertEqual(self.q.confirmed_count(), 0)

    def assertRejected_calls(self, res, n):
        self.assertEqual(res[0], 1, res)
        self.assertEqual(self.q.confirm_calls, n)
        self.assertEqual(self.q.confirmed_count(), 0)


class TestRealSqliteMultiprocess(SqliteMixin, MPBase):
    def make_queue(self):
        return self.build_real_queue()

    def setUp(self):
        super().setUp()
        self.patches[0].stop()
        p = mock.patch.object(nw, "_resolve", lambda a: (self.q.shim, self.q.db, self.root))
        p.start()
        self.patches[0] = p
        self.create_draft()

    def preview(self, *a, **kw):
        kw.setdefault("rid", self.q.rid)
        return super().preview(*a, **kw)

    def displayed(self):
        rc, out, err = nw._run_cli(self.q.shim, self.q.db, ["status", self.q.rid])
        rec = json.loads(out)
        return rec["client_record_version"], nw._record_snapshot_hash(rec)

    def test_confirm_vs_content_mutation_race_real_sqlite(self):
        previewed = self.displayed()[1]
        for i in range(4):
            # fresh draft each round; keep the first preview round simple
            if i:
                self.create_draft()
            self.assertEqual(self.preview(msg=f"p{i}")[0], 0)
            tf = self.write_reply(f"r{i}", reply_to=f"p{i}")
            rid = self.q.rid
            rcs = self.spawn([
                self.confirm_job(tf),
                lambda: self.q.update(fat_g=50.0) or 0,
            ])
            rec = self.q.rec
            h = nq.snapshot_hash_of_row(rec)
            if rec["state"] == "confirmed":
                # Confirm won the race: the confirmed content is exactly what was previewed.
                self.assertEqual(rcs[0], 0, (i, rcs))
                self.assertEqual(rec["fat_g"], 5.0)
            else:
                # Mutation won: it must NOT have been confirmed.
                self.assertEqual(rcs[0], 1, (i, rcs))
                self.assertEqual(rec["fat_g"], 50.0)
            self.assertEqual(rid, self.q.rid)
            self.assertIsNotNone(h)
        # Across every round, no confirmed row carries mutated content.
        conn = sqlite3.connect(str(self.q.db))
        try:
            bad = conn.execute(
                "SELECT COUNT(*) FROM nutrition_queue WHERE state='confirmed' AND fat_g != 5.0"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(bad, 0)
        self.assertTrue(previewed)

    def test_concurrent_replies_exactly_one_confirmed_row(self):
        self.assertEqual(self.preview()[0], 0)
        files = [self.write_reply(f"r{i}") for i in range(5)]
        rcs = self.spawn([self.confirm_job(f) for f in files])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirmed_count(), 1)


class TestPhotoAndTextIntegration(RealBase):
    """ONE integration scenario run for a synthetic PHOTO and a synthetic TEXT fixture.

    Synthetic fixtures only: not the user's consent, not vision-model validation.
    """

    FIXTURES = {
        "photo": {
            "source": "synthetic photo fixture (no real image)",
            "name": "Synthetic plate (photo)",
            "items": [  # (food, explicit portion, kcal, protein, carbs, fat)
                ("rice, cooked", "150 g", 195.0, 4.0, 42.0, 0.5),
                ("grilled chicken", "100 g", 165.0, 31.0, 0.0, 3.6),
            ],
            "assumptions": "portion sizes estimated from plate diameter; no added oil",
        },
        "text": {
            "source": "synthetic text fixture",
            "name": "Synthetic breakfast (text)",
            "items": [
                ("oatmeal", "60 g dry", 228.0, 8.0, 40.0, 4.0),
                ("whole milk", "200 ml", 128.0, 6.8, 9.6, 7.2),
            ],
            "assumptions": "user stated portions; milk 3.5% fat",
        },
    }

    def tearDown(self):
        super().tearDown()

    def _totals(self, fx):
        sums = [round(sum(i[k] for i in fx["items"]), 1) for k in (2, 3, 4, 5)]
        return dict(zip(("energy_kcal", "protein_g", "carbohydrate_g", "fat_g"), sums))

    def _berlin_now(self):
        return nw._now().astimezone(ZoneInfo("Europe/Berlin"))

    def _render_preview(self, fx, draft):
        """Compact final preview as the workflow renders it. id/version/hash stay hidden (internal binding)."""
        rb = draft["readback"]
        start, _ = nw._parse_ts(rb["start_time"], "start")
        end, _ = nw._parse_ts(rb["end_time"], "end")
        bs, be = start.astimezone(ZoneInfo("Europe/Berlin")), end.astimezone(ZoneInfo("Europe/Berlin"))
        portions = "; ".join(f"{f} {p}" for f, p, *_ in fx["items"])
        return (
            f"{rb['name']}: {portions}. Assumptions: {fx['assumptions']}.\n"
            f"{rb['energy_kcal']:g} kcal, P {rb['protein_g']:g} g, C {rb['carbohydrate_g']:g} g, "
            f"F {rb['fat_g']:g} g.\n"
            f"Berlin now: {self._berlin_now():%Y-%m-%d %H:%M}; proposed technical interval "
            f"{bs:%H:%M}-{be:%H:%M} (1 min).\n"
            "Технический интервал в очереди, это не реальная длительность еды.\n"
            "Записать или поправишь?"), start, end

    def _scenario(self, key):
        fx = self.FIXTURES[key]
        payload = {
            "name": fx["name"], "start_time": "2026-10-12T12:00:00Z",
            "end_time": "2026-10-12T12:01:00Z",
            "description": f"{fx['source']}; " + "; ".join(f"{f} {p}" for f, p, *_ in fx["items"])
                           + f"; assumptions: {fx['assumptions']}",
            **self._totals(fx),
        }
        draft = self.create_draft(payload)
        # wrapper draft with readback
        self.assertEqual(draft["state"], "draft")
        self.assertEqual(draft["readback"]["name"], fx["name"])
        self.assertEqual(draft["snapshot_hash"], nq.snapshot_hash_of_row(self.q.rec))
        # exact compact final preview
        text, start, end = self._render_preview(fx, draft)
        self.assertEqual(self._berlin_now().utcoffset(), timedelta(hours=2))  # CEST, controlled-clock fixture (not tool-derived)
        self.assertIn("Berlin now: 2026-10-12 14:00", text)
        self.assertEqual(end - start, timedelta(minutes=1))                   # positive 1-min interval
        self.assertNotIn(draft["client_record_id"], text)
        self.assertNotIn(draft["snapshot_hash"], text)
        self.assertNotRegex(text, r"\bv\d+\b|hash|\bid\b")
        self.assertTrue(text.endswith("Записать или поправишь?"))
        self.assertEqual(len(text.strip().split("\n\n")), 1)
        self.assertIn("не реальная длительность", text)
        for f, p, *_ in fx["items"]:
            self.assertIn(f, text)
            self.assertIn(p, text)
        self.assertIn(fx["assumptions"], text)
        # register the preview bound (internally) to the draft id/version/hash
        self.assertEqual(self.preview(
            msg=f"{key}-p", version=draft["client_record_version"],
            shash=draft["snapshot_hash"])[0], 0)
        # synthetic authenticated-transport reply Да/Запиши
        rc, out, err = self.reply("Запиши" if key == "text" else "Да",
                                  mid=f"{key}-r1", reply_to=f"{key}-p")
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.assertTrue(res.get("ok"), res)
        # wrapper confirmed readback; exactly one confirmed row, matching preview
        rec = self.q.rec
        self.assertEqual(rec["state"], "confirmed")
        self.assertEqual(nq.snapshot_hash_of_row(rec), draft["snapshot_hash"])
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(self.q.confirm_calls, 1)
        # no duplicate: any further reply (new id) is refused without another confirm
        res2 = self.reply("Да", mid=f"{key}-r2", reply_to=f"{key}-p")
        self.assertEqual(res2[0], 1)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.confirmed_count(), 1)

    def test_photo_fixture_end_to_end(self):
        self._scenario("photo")

    def test_text_fixture_end_to_end(self):
        self._scenario("text")

    # ── refusals ────────────────────────────────────────────────────────────
    def _registered(self, **kw):
        self.assertEqual(self.preview(msg="p1", **kw)[0], 0)

    def test_correction_instead_of_consent_is_refused(self):
        self._registered()
        for text in ("Да, но 400 ккал", "Нет, поменяй порцию", "Да, и ещё кофе", "ок"):
            self.assertEqual(self.reply(text, mid=f"c-{text}")[0], 1, text)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.q.confirmed_count(), 0)

    def test_old_preview_refused_after_edit(self):
        self._registered()
        rc, out, err = nw._run_cli(
            self.q.shim, self.q.db,
            ["edit", self.q.rid, "--version", "1", "--file", str(self._write_edit())])
        self.assertEqual(rc, 0, err)
        res = self.reply("Да", mid="old-1")
        self.assertEqual(res[0], 1)
        self.assertEqual(self.q.confirmed_count(), 0)

    def _write_edit(self):
        p = self.root / "edit.json"
        p.write_text(json.dumps(dict(PAYLOAD, energy_kcal=410.0)))
        return p

    def test_old_expired_preview_refused(self):
        self._registered()
        self.now = T0 + timedelta(hours=2)
        self.assertEqual(self.reply("Да", mid="late",
                                    ts=(T0 + timedelta(hours=2)).isoformat())[0], 1)
        self.assertEqual(self.q.confirmed_count(), 0)

    def test_reused_reply_refused(self):
        self._registered()
        self.assertEqual(self.reply("Да", mid="once")[0], 0)
        self.assertEqual(self.reply("Да", mid="once")[0], 1)
        self.assertEqual(self.q.confirm_calls, 1)
        self.assertEqual(self.q.confirmed_count(), 1)

    def test_ambiguous_preview_store_refused(self):
        self._registered()
        d = self.root / "pending-previews"
        f = next(d.glob("preview-*.json"))
        dup = d / "preview-dup.json"
        dup.write_text(f.read_text())
        os.chmod(dup, 0o600)
        self.assertEqual(self.reply("Да", mid="amb")[0], 1)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.q.confirmed_count(), 0)


if __name__ == "__main__":
    unittest.main()
