"""
Tests for the nutrition reporting/consent UX in agent-workflow/nutrition_writeback.py
and consultation_read.py:

  - canonical user-facing statuses (+ fail-closed unknown state)
  - read-only Europe/Berlin day report (wrapper `day-report`, consultation
    `queue_day_report`): pending / done / excluded with reasons, no totals
  - personal-chat late consent (no time-only expiry) with every other binding intact
  - known-recipe registry (recipe-preview / recipe-confirm / recipe-show)
  - known-recipe text log fast path (personal-log-known)

Everything runs against the REAL NutritionStore / nutrition_cli on a synthetic temp
SQLite DB through an isolated shim CLI under a private temp isolation root in health
scratch. The mutation-readiness gate is stubbed (and separately shown to block); no
network, production CLI, DB, token or record is touched. All meals, recipes, request
texts and "Да" replies are synthetic fixtures, not a real user's data or consent.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests.test_short_preview_consent import MPBase, T0, _SCRATCH, nw
from tests.test_short_consent_sqlite import PAYLOAD, RealBase, SqliteMixin
from tests.test_personal_chat_consent import PersonalMixin

_BRIDGE = Path(__file__).resolve().parent.parent
_WRAPPER = _BRIDGE / "agent-workflow" / "nutrition_writeback.py"
_crspec = importlib.util.spec_from_file_location("_cr_ux", _BRIDGE / "consultation_read.py")
cr = importlib.util.module_from_spec(_crspec)
_crspec.loader.exec_module(cr)

BERLIN = ZoneInfo("Europe/Berlin")

RECIPE = {
    "key": "oat-std",
    "name": "Овсяная каша",
    "aliases": ["овсяную кашу", "овсянку", "Овсянка"],
    "ingredients": [{"item": "овсяные хлопья", "quantity": "50 г"},
                    {"item": "молоко 1.5%", "quantity": "200 мл"}],
    "per_portion": {"energy_kcal": 300, "protein_g": 13, "carbohydrate_g": 45, "fat_g": 7},
    "macro_basis": "label",
}


def berlin_iso(day, hh, mm):
    """UTC ISO string of a Berlin wall time on `day` (a date)."""
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=BERLIN)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def yesterday_berlin():
    return datetime.now(BERLIN).date() - timedelta(days=1)


# ── pure: status mapping ─────────────────────────────────────────────────────

class TestStatusMapping(unittest.TestCase):
    def test_canonical_texts(self):
        cases = {
            ("draft", 0): ("draft", "Черновик — ждёт подтверждения"),
            ("confirmed", 0): ("confirmed", "Принято — ожидает синхронизации"),
            ("confirmed", 1): ("confirmed", "Принято — ожидает синхронизации"),
            ("acked", 1): ("acked", "Телефон подтвердил запись в Health Connect"),
            ("cancelled", 0): ("cancelled", "Не записано: отменено"),
        }
        for (state, dp), (code, text) in cases.items():
            with self.subTest(state=state, dp=dp):
                s = nw._user_status({"state": state, "delivery_possible": dp})
                self.assertEqual((s["code"], s["text"], s["known"]), (code, text, True))
                c = cr._user_status_text(state, dp)
                self.assertEqual((c["code"], c["text"]), (code, text))

    def test_failed_text(self):
        self.assertEqual(nw._failed_status("причина")["text"], "Не записано: причина")

    def test_cancelled_after_possible_delivery_is_not_called_not_written(self):
        for dp in (1, True, None, "x"):
            s = nw._user_status({"state": "cancelled", "delivery_possible": dp})
            self.assertEqual(s["code"], "cancelled_delivery_possible")
            self.assertNotIn("Не записано", s["text"])
        self.assertEqual(nw._user_status({"state": "cancelled"})["code"],
                         "cancelled_delivery_possible")  # absent flag: no claim

    def test_unknown_states_fail_closed(self):
        for rec in ({"state": "weird"}, {"state": None}, {}, None, "draft", {"state": 1},
                    {"state": "Confirmed"}):
            with self.subTest(rec=rec):
                s = nw._user_status(rec)
                self.assertEqual(s["code"], "unknown")
                self.assertFalse(s["known"])
                for claim in ("Принято", "Телефон", "Health Connect"):
                    self.assertNotIn(claim, s["text"])
        self.assertFalse(cr._user_status_text("weird", 0)["known"])

    def test_edited_after_delivery_draft_does_not_promise_phone_correction(self):
        s = nw._user_status({"state": "draft", "delivery_possible": 1})
        self.assertIn("не гарантируется", s["note"])
        self.assertNotIn("note", nw._user_status({"state": "draft", "delivery_possible": 0}))

    def test_wrapper_and_consultation_texts_identical(self):
        self.assertEqual(nw._STATUS_TEXT, cr._STATUS_TEXT)
        for n in ("_NOT_WRITTEN_TEXT", "_UNKNOWN_STATUS_TEXT", "_CANCELLED_REASON",
                  "_CANCELLED_DELIVERY_POSSIBLE_TEXT"):
            self.assertEqual(getattr(nw, n), getattr(cr, n), n)

    def test_failure_status_classification(self):
        f = nw._failure_user_status
        self.assertEqual(f({"error": "uncertain outcome: x"})["code"], "unknown")
        self.assertEqual(f({"error": "readback failed after confirm"})["code"], "unknown")
        self.assertEqual(f({"error": "CLI subprocess timeout"})["code"], "unknown")
        self.assertEqual(f({"error": "this consent file was already used (replay rejected)"})
                         ["code"], "replay")
        self.assertEqual(f({"error": "no active preview for this scope"})["text"],
                         "Не записано: no active preview for this scope")


# ── pure: request recognizer ────────────────────────────────────────────────

ALIASES = ["овсяная каша", "овсяную кашу", "овсянку", "овсянка"]
# Fixed "received" instant: 2026-10-14 12:00 Berlin (CEST)
REQ_DT = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


class TestRequestRecognizer(unittest.TestCase):
    def ok(self, text, date, hm, dt=REQ_DT):
        info, err = nw._parse_log_request(text, ALIASES, dt)
        self.assertIsNone(err, text)
        self.assertEqual((info["local_date"], info["local_time"]), (date, hm), text)
        return info

    def bad(self, text, needle=None, dt=REQ_DT):
        info, err = nw._parse_log_request(text, ALIASES, dt)
        self.assertIsNone(info, text)
        if needle:
            self.assertIn(needle, err, text)
        return err

    def test_reasonable_direct_russian_requests_accepted(self):
        self.ok("Запиши овсянку в 8:30", "2026-10-14", "08:30")
        self.ok("запиши мою обычную овсянку на завтрак сегодня в 08:30", "2026-10-14", "08:30")
        self.ok("Залогируй овсяную кашу вчера в 19:45.", "2026-10-13", "19:45")
        self.ok("Добавь в дневник овсянку, 12 октября, 7:05", "2026-10-12", "07:05")
        self.ok("добавь в дневник питания овсянку 12.10 в 8 утра", "2026-10-12", "08:00")
        self.ok("Пожалуйста, запиши овсянку по рецепту в 1 дня", "2026-10-14", "13:00")
        self.ok("запиши овсянку вчера в 2 дня", "2026-10-13", "14:00")
        self.ok("Внеси в дневник Овсяная каша позавчера в 21:10!", "2026-10-12", "21:10")
        self.ok("Запишите овсянку в 8 вечера вчера", "2026-10-13", "20:00")
        self.ok("ЗАПИШИ ОВСЯНКУ В 13", "2026-10-14", "13:00")
        self.ok("занеси в дневник овсянку 12 октября 2026 в 06:00", "2026-10-12", "06:00")

    def test_negatives_rejected(self):
        for t in ("Не записывай овсянку в 8:30", "запиши овсянку в 8:30, нет, не надо",
                  "Запиши не овсянку в 8:30", "запиши овсянку в 8:30 отмени",
                  "don't log овсянку в 8:30", "Нет, запиши овсянку в 8:30"):
            self.bad(t)

    def test_questions_and_hypotheticals_rejected(self):
        for t in ("Запиши овсянку в 8:30?", "Можешь записать овсянку в 8:30",
                  "запиши овсянку в 8:30 если можно", "Если что, запиши овсянку в 8:30",
                  "запиши бы овсянку в 8:30", "запиши овсянку в 8:30 наверное",
                  "Как записать овсянку в 8:30", "запиши ли овсянку в 8:30",
                  "Давай запишем овсянку в 8:30", "Хочу записать овсянку в 8:30"):
            self.bad(t)

    def test_quoted_or_forwarded_commands_rejected(self):
        for t in ('Запиши "овсянку в 8:30"', "Запиши «овсянку» в 8:30",
                  "Пользователь написал: запиши овсянку в 8:30", "> запиши овсянку в 8:30",
                  "запиши овсянку в 8:30 (пример)", "Например: запиши овсянку в 8:30",
                  "тест: запиши овсянку в 8:30", "запиши овсянку в 8:30 — это задача",
                  "Запиши ‘овсянку’ в 8:30"):
            self.bad(t)

    def test_non_explicit_or_ambiguous_time_rejected(self):
        cases = ("Запиши овсянку", "запиши овсянку утром", "Запиши овсянку в 8",
                 "запиши овсянку около 8:30", "запиши овсянку примерно в 8:30",
                 "запиши овсянку часов в 8", "запиши овсянку в 8:30 или в 9:00",
                 "запиши овсянку в 8.30", "запиши овсянку ~8:30", "запиши овсянку в обед",
                 "запиши овсянку сейчас", "запиши овсянку в 14:00 утра",
                 "запиши овсянку в 8:30 сегодня вчера", "запиши овсянку завтра в 8:30",
                 "запиши овсянку после 8:30", "запиши овсянку полчаса назад")
        for t in cases:
            self.bad(t)

    def test_photo_estimate_mixed_and_correction_rejected(self):
        for t in ("запиши овсянку с фото в 8:30", "Запиши овсянку по фото в 8:30",
                  "запиши овсянку и кофе в 8:30", "запиши овсянку, кофе в 8:30",
                  "запиши половину овсянки в 8:30", "запиши овсянку двойную порцию в 8:30",
                  "запиши овсянку без молока в 8:30", "запиши овсянку но 400 ккал в 8:30",
                  "запиши овсянку 300 г в 8:30", "поправь овсянку на 8:30",
                  "запиши овсянку, исправь время на 8:30", "запиши овсянку ещё раз в 8:30",
                  "оцени и запиши овсянку в 8:30", "запиши овсянку с бананом в 8:30",
                  "запиши овсянку вместо гречки в 8:30", "запиши гречку в 8:30",
                  "запиши овсянку овсянку в 8:30", "запиши рецепт овсянку в 8:30"):
            self.bad(t)

    def test_recipe_save_request_is_not_a_log_request(self):
        for t in ("Сохрани рецепт овсянки", "сохрани овсянку как рецепт",
                  "Да", "Да, запиши", "запиши", "запиши рецепт овсяной каши"):
            self.bad(t)

    def test_multiline_and_long_rejected(self):
        self.bad("запиши овсянку в 8:30\nи кофе в 9:00", "single line")
        self.bad("запиши овсянку в 8:30 " + "пожалуйста " * 40, "longer")

    def test_not_a_blanket_substring_yes(self):
        # the verb must START the message; a log word somewhere inside is not enough
        self.bad("Овсянку в 8:30 запиши", "direct log instruction")
        self.bad("ну вот, запиши овсянку в 8:30", "direct log instruction")

    def test_date_bounds_and_year_rollover(self):
        self.bad("запиши овсянку 1 октября в 8:30", "within the last")
        jan = datetime(2027, 1, 2, 11, 0, tzinfo=timezone.utc)
        self.ok("запиши овсянку 31.12 в 23:30", "2026-12-31", "23:30", dt=jan)
        self.bad("запиши овсянку 31.02 в 8:30", "invalid date")

    def test_meal_far_after_request_rejected(self):
        self.bad("запиши овсянку в 23:30", "too far after")      # request at 12:00 Berlin
        self.ok("запиши овсянку в 12:45", "2026-10-14", "12:45")  # within an hour

    def test_berlin_date_boundaries(self):
        # 2026-10-14 21:59:59Z = 23:59:59 CEST on the 14th; 22:00Z = 00:00 on the 15th
        late = datetime(2026, 10, 14, 21, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 14, 22, 0, 0, tzinfo=timezone.utc)
        self.ok("запиши овсянку сегодня в 23:50", "2026-10-14", "23:50", dt=late)
        self.ok("запиши овсянку вчера в 23:50", "2026-10-14", "23:50", dt=after)
        info = self.ok("запиши овсянку в 00:00", "2026-10-15", "00:00", dt=after)
        self.assertEqual(info["start_utc"], after)

    def test_dst_fall_back_ambiguous_and_spring_gap_rejected(self):
        dt = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        self.bad("запиши овсянку в 2:30 ночи", "ambiguous", dt=dt)
        self.ok("запиши овсянку в 3:30 ночи", "2026-10-25", "03:30", dt=dt)
        spring = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        self.bad("запиши овсянку в 2:30 ночи", "does not exist", dt=spring)
        info = self.ok("запиши овсянку в 3:30 ночи", "2026-03-29", "03:30", dt=spring)
        self.assertEqual(info["start_utc"], datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc))

    def test_utc_instant_is_berlin_wall_time(self):
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        info = self.ok("запиши овсянку в 8:30", "2026-01-15", "08:30", dt=winter)
        self.assertEqual(info["start_utc"], datetime(2026, 1, 15, 7, 30, tzinfo=timezone.utc))
        info = self.ok("запиши овсянку в 8:30", "2026-10-14", "08:30")
        self.assertEqual(info["start_utc"], datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc))


# ── real SQLite: shared helpers ─────────────────────────────────────────────

class UXMixin(PersonalMixin):
    def raw_sql(self, sql, params=()):
        conn = sqlite3.connect(str(self.q.db), timeout=30)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def rows(self):
        conn = sqlite3.connect(str(self.q.db), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM nutrition_queue")]
        finally:
            conn.close()

    def kinds(self, kind):
        return [x for x in self.pdata() if x.get("kind") == kind]

    def save_recipe(self, recipe=None, scope=None):
        self.tf_n += 1
        rf = self.private_file(f"recipe{self.tf_n}.json", json.dumps(recipe or RECIPE))
        rc, out, err = self.run_cmd(nw.cmd_recipe_preview, scope=scope or self.scope,
                                    attest_personal_chat=True, recipe_file=str(rf))
        self.assertEqual(rc, 0, out + err)
        prev = json.loads(out)
        cf = self.consent_file(f"rcons{self.tf_n}.txt", "Да")
        rc, out, err = self.run_cmd(nw.cmd_recipe_confirm, scope=scope or self.scope,
                                    attest_personal_chat=True, consent_file=str(cf))
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.assertEqual(res["recipe_hash"], prev["recipe_hash"])
        return res

    def recipe_show(self, key="oat-std", scope=None):
        rc, out, err = self.run_cmd(nw.cmd_recipe_show, scope=scope or self.scope,
                                    attest_personal_chat=True, recipe_key=key)
        self.assertEqual(rc, 0, out + err)
        return json.loads(out)

    def known_draft(self, day, hh, mm, **over):
        tmpl = self.recipe_show()["draft_template"]
        start = berlin_iso(day, hh, mm)
        end = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(seconds=60)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = dict(tmpl, start_time=start, end_time=end)
        payload["description"] = tmpl["description"]
        payload.update(over)
        return self.create_draft(payload)

    def log_known_args(self, request_text, phrase, local_date, local_time, recipe=None,
                       rid=None, version=1, shash=None, request_path=None, scope=None,
                       attest_req=True, attest_time=True, evidence=None):
        """All files are written HERE (in the parent), never inside spawned jobs."""
        recipe = recipe or self.recipe_show()
        self.tf_n += 1
        rp = request_path or self.private_file(f"req{self.tf_n}.txt", request_text)
        ev = self.private_file(f"ev{self.tf_n}.json", json.dumps(
            evidence if evidence is not None else
            {"time_phrase": phrase, "local_date": local_date, "local_time": local_time},
            ensure_ascii=False))
        dv, dh = self.displayed()
        return dict(
            scope=scope or self.scope, attest_personal_chat=True,
            attest_direct_user_request=attest_req, attest_user_time=attest_time,
            request_file=str(rp), time_evidence_file=str(ev),
            recipe_key=recipe["recipe_key"], recipe_version=recipe["recipe_version"],
            recipe_hash=recipe["recipe_hash"], id=rid or self.q.rid,
            expected_version=version, expected_snapshot_hash=shash or dh, confirmer="Ravil")

    def log_known(self, *a, **kw):
        return self.run_cmd(nw.cmd_personal_log_known, **self.log_known_args(*a, **kw))

    def fresh_log_case(self, hh=8, mm=30):
        """Request file first (as received), then the draft built from the recipe."""
        day = yesterday_berlin()
        text = f"Запиши овсянку вчера в {hh}:{mm:02d}"
        self.tf_n += 1
        rp = self.private_file(f"req{self.tf_n}.txt", text)
        self.known_draft(day, hh, mm)
        return rp, text, f"в {hh}:{mm:02d}", day.isoformat(), f"{hh:02d}:{mm:02d}"


class UXRealBase(UXMixin, RealBase):
    pass


# ── real SQLite: status and day report ──────────────────────────────────────

class TestStatusAndDayReportRealSqlite(UXRealBase):
    def setUp(self):
        super().setUp()
        self.raw_sql("DELETE FROM nutrition_queue")

    def add(self, name, start, state="draft", dp=0, end=None):
        end = end or (datetime.fromisoformat(start.replace("Z", "+00:00"))
                      + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self.create_draft(dict(PAYLOAD, name=name, start_time=start, end_time=end))
        if state != "draft" or dp:
            self.raw_sql("UPDATE nutrition_queue SET state=?, delivery_possible=? "
                         "WHERE client_record_id=?", (state, dp, res["client_record_id"]))
        return res["client_record_id"]

    def report(self, date):
        rc, out, err = self.run_cmd(nw.cmd_day_report, date=date)
        self.assertEqual(rc, 0, out + err)
        return json.loads(out)

    def test_status_single_has_canonical_user_status(self):
        rid = self.add("A", "2026-10-12T08:00:00Z", "confirmed")
        rc, out, err = self.run_cmd(nw.cmd_status, id=rid, state=None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["user_status"]["text"], "Принято — ожидает синхронизации")

    def test_status_unknown_state_fails_closed(self):
        rid = self.add("A", "2026-10-12T08:00:00Z")
        self.raw_sql("UPDATE nutrition_queue SET state='weird' WHERE client_record_id=?", (rid,))
        rc, out, err = self.run_cmd(nw.cmd_status, id=rid, state=None)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["user_status"]["code"], "unknown")
        self.assertIn("unknown state", err)

    def test_status_list_annotates_every_entry(self):
        self.add("A", "2026-10-12T08:00:00Z", "acked", 1)
        self.add("B", "2026-10-12T09:00:00Z", "cancelled")
        rc, out, err = self.run_cmd(nw.cmd_status, id=None, state=None)
        self.assertEqual(rc, 0, err)
        texts = sorted(e["user_status"]["text"] for e in json.loads(out)["entries"])
        self.assertEqual(texts, ["Не записано: отменено",
                                 "Телефон подтвердил запись в Health Connect"])

    def test_day_report_groups_with_reasons_and_never_sums(self):
        d = "2026-10-12"
        self.add("Draft", "2026-10-12T06:00:00Z", "draft")
        self.add("Conf", "2026-10-12T07:00:00Z", "confirmed")
        self.add("Ack", "2026-10-12T08:00:00Z", "acked", 1)
        self.add("Canc", "2026-10-12T09:00:00Z", "cancelled")
        self.add("CancFetched", "2026-10-12T10:00:00Z", "cancelled", 1)
        self.add("Weird", "2026-10-12T11:00:00Z", "weird")
        self.add("OtherDay", "2026-10-13T08:00:00Z", "draft")
        rep = self.report(d)
        self.assertEqual(sorted(e["name"] for e in rep["pending"]), ["Conf", "Draft"])
        self.assertEqual([e["name"] for e in rep["done"]], ["Ack"])
        exc = {e["name"]: e for e in rep["excluded"]}
        self.assertEqual(set(exc), {"Canc", "CancFetched", "Weird"})
        self.assertEqual(exc["Canc"]["reason"], "cancelled")
        self.assertIn("may have fetched", exc["CancFetched"]["reason"])
        self.assertEqual(exc["Weird"]["reason"], "unknown state")
        self.assertEqual(exc["Weird"]["user_status"]["code"], "unknown")
        self.assertIsNone(rep["totals"])
        self.assertNotIn("sum", json.dumps(rep).replace("Never sum", ""))
        self.assertEqual(rep["counts"], {"pending": 2, "done": 1, "excluded": 3})
        self.assertFalse(rep["possibly_incomplete"])
        pend = {e["name"]: e for e in rep["pending"]}
        self.assertEqual(pend["Draft"]["user_status"]["text"], "Черновик — ждёт подтверждения")
        self.assertEqual(pend["Draft"]["local_time"], "12 октября, 08:00–08:01, Берлин")

    def test_day_report_berlin_midnight_boundaries(self):
        self.add("LastSecondOf11", "2026-10-11T21:59:59Z")   # 23:59:59 CEST, 11th
        self.add("FirstOf12", "2026-10-11T22:00:00Z")        # 00:00 CEST, 12th
        self.add("LastOf12", "2026-10-12T21:59:00Z")         # 23:59 CEST, 12th
        self.add("FirstOf13", "2026-10-12T22:00:00Z")        # 00:00, 13th
        names = sorted(e["name"] for e in self.report("2026-10-12")["pending"])
        self.assertEqual(names, ["FirstOf12", "LastOf12"])
        self.assertEqual([e["name"] for e in self.report("2026-10-11")["pending"]],
                         ["LastSecondOf11"])

    def test_day_report_dst_spring_forward_day_is_23_hours(self):
        self.add("Before", "2026-03-28T22:59:00Z")   # 23:59 CET, 28th
        self.add("Start", "2026-03-28T23:00:00Z")    # 00:00 CET, 29th
        self.add("End", "2026-03-29T21:59:00Z")      # 23:59 CEST, 29th
        self.add("Next", "2026-03-29T22:00:00Z")     # 00:00 CEST, 30th
        names = sorted(e["name"] for e in self.report("2026-03-29")["pending"])
        self.assertEqual(names, ["End", "Start"])

    def test_day_report_unusable_times_excluded_with_reason(self):
        rid = self.add("Naive", "2026-10-12T08:00:00Z")
        self.raw_sql("UPDATE nutrition_queue SET start_time='2026-10-12T08:00:00' "
                     "WHERE client_record_id=?", (rid,))
        rep = self.report("2026-10-12")
        self.assertEqual(rep["pending"], [])
        self.assertEqual(rep["excluded"][0]["day_known"], False)
        self.assertIn("timezone", rep["excluded"][0]["reason"])

    def test_day_report_bad_date_rejected(self):
        for bad in ("2026-13-01", "12.10.2026", "today", "2026-1-2"):
            rc, out, err = self.run_cmd(nw.cmd_day_report, date=bad)
            self.assertEqual(rc, 1, bad)

    def test_day_report_is_read_only(self):
        self.add("A", "2026-10-12T08:00:00Z", "confirmed")
        before = self.rows()
        self.report("2026-10-12")
        self.assertEqual(self.rows(), before)  # not a phone fetch: fetched_at/dp untouched

    def test_consultation_snapshot_queue_day_report(self):
        self.add("Draft", "2026-10-12T06:00:00Z", "draft")
        self.add("Ack", "2026-10-12T08:00:00Z", "acked", 1)
        self.add("Canc", "2026-10-12T09:00:00Z", "cancelled")
        self.add("Weird", "2026-10-12T11:00:00Z", "weird")
        rid = self.add("Naive", "2026-10-12T12:00:00Z")
        self.raw_sql("UPDATE nutrition_queue SET start_time='2026-10-12T12:00:00' "
                     "WHERE client_record_id=?", (rid,))
        res = cr.run(self.root / "missing-health.sqlite3", self.q.db, "2026-10-12")
        rep = res["queue_day_report"]
        self.assertEqual([e["name"] for e in rep["pending"]], ["Draft"])
        self.assertEqual([e["name"] for e in rep["done"]], ["Ack"])
        self.assertEqual(sorted(e["name"] for e in rep["excluded"]), ["Canc", "Naive", "Weird"])
        self.assertIsNone(rep["totals"])
        self.assertEqual(rep["pending"][0]["user_status"]["text"],
                         "Черновик — ждёт подтверждения")
        # ingest stays separate; nothing merged
        self.assertEqual(res["date_nutrition_ingest"], [])
        json.dumps(res)


class TestReadOnlyCommandsRealSubprocess(UXRealBase):
    """The installed-style wrapper process (real _resolve, real CLI, /usr/bin/python3)."""

    def run_wrapper(self, *argv):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
        r = subprocess.run(["/usr/bin/python3", "-I", str(_WRAPPER), "--isolation-root",
                            str(self.root), "--cli", str(self.q.shim), "--db", str(self.q.db),
                            *argv], capture_output=True, timeout=60, env=env)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def test_day_report_and_status_via_real_process(self):
        rc, out, err = self.run_wrapper("day-report", "--date", "2026-10-12")
        self.assertEqual(rc, 0, err)
        rep = json.loads(out)
        self.assertEqual([e["client_record_id"] for e in rep["pending"]], [self.q.rid])
        rc, out, err = self.run_wrapper("status", self.q.rid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["user_status"]["code"], "draft")

    def test_personal_log_known_via_real_process_fails_closed_without_recipe(self):
        # no recipe registry, no privacy flags: refuses before any mutation
        req = self.private_file("r.txt", "Запиши овсянку в 8:30")
        ev = self.private_file("e.json", "{}")
        rc, out, err = self.run_wrapper(
            "personal-log-known", "--scope", "personal-a", "--attest-personal-chat",
            "--attest-direct-user-request", "--attest-user-time", "--request-file", str(req),
            "--time-evidence-file", str(ev), "--recipe-key", "oat-std", "--recipe-version", "1",
            "--recipe-hash", "0" * 64, "--id", self.q.rid, "--expected-version", "1",
            "--expected-snapshot-hash", "0" * 64, "--confirmer", "Ravil")
        self.assertEqual(rc, 1)
        self.assertIn("Не записано", err)
        self.assertEqual(self.q.rec["state"], "draft")


# ── real SQLite: late personal consent ──────────────────────────────────────

class TestLateConsentRealSqlite(UXRealBase):
    LATE = timedelta(hours=3)

    def late_preview(self):
        self.assertEqual(self.ppreview(ttl=60)[0], 0)
        self.now = T0 + self.LATE

    def test_late_unchanged_confirms_once_and_replay_is_read_only(self):
        self.late_preview()
        p = self.consent_file("late.txt", "Да")
        rc, out, err = self.pconfirm(path=p)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["user_status"]["text"], "Принято — ожидает синхронизации")
        self.assertEqual(self.q.confirmed_count(), 1)
        db_before = self.rows()
        for again in (p, self.consent_file("late2.txt", "Да")):
            rc, out, err = self.pconfirm(path=again)
            self.assertEqual(rc, 1)
            self.assertIn("Повтор", err)
        self.assertEqual(self.rows(), db_before)
        self.assertEqual(self.q.confirm_calls, 1)

    def test_late_changed_content_never_confirms(self):
        for label, change in (
                ("version", lambda: self.edit(energy_kcal=300.0)),
                ("macros", lambda: self.q.update(fat_g=6.0)),
                ("description", lambda: self.q.update(description="[AI estimate] other")),
                ("time", lambda: self.q.update(start_time="2026-10-12T12:05:00Z")),
                ("name", lambda: self.q.update(name="Other"))):
            with self.subTest(change=label):
                self.create_draft()
                self.late_preview()
                change()
                rc, out, err = self.pconfirm("Да")
                self.assertEqual(rc, 1, out + err)
                self.assertIn("Не записано", err)
                self.assertEqual(self.q.rec["state"], "draft")
                self.assertEqual(self.q.confirmed_count(), 0)
                self.assertEqual([x["status"] for x in self.pdata()], ["revoked"])
                for f in self.pfiles():
                    f.unlink()
                self.now = T0

    def test_late_display_hash_tamper_fails_closed(self):
        self.late_preview()
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["display_text_sha256"] = "0" * 64
        f.write_text(json.dumps(d))
        self.assertNoMutation(self.pconfirm("Да"), "tampered")

    def test_late_superseded_preview_cannot_confirm(self):
        self.late_preview()
        old_consent = self.consent_file("old.txt", "Да")
        self.edit(energy_kcal=350.0)
        self.assertEqual(self.ppreview()[0], 0)       # new current preview
        rc, out, err = self.pconfirm(path=old_consent)  # predates the new preview
        self.assertEqual(rc, 1)
        self.assertIn("predates", err)
        self.assertEqual(sorted(d["status"] for d in self.pdata()), ["pending", "superseded"])
        self.assertEqual(self.q.confirmed_count(), 0)

    def test_late_wrong_scope_cancelled_or_consuming_rejected(self):
        self.late_preview()
        self.assertNoMutation(self.pconfirm("Да", scope="personal-b"), "no active preview")
        self.assertNoMutation(self.pconfirm("Да", attest=False), "attest")
        f = self.pfiles()[0]
        d = json.loads(f.read_text())
        d["status"] = "consuming"
        f.write_text(json.dumps(d))
        self.assertNoMutation(self.pconfirm("Да"))
        d["status"] = "pending"
        f.write_text(json.dumps(d))
        self.q.update(state="cancelled")
        rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 1)
        self.assertEqual(self.q.rec["state"], "cancelled")
        self.assertEqual(self.pdata()[0]["status"], "revoked")

    def test_late_consent_file_rules_unchanged(self):
        self.late_preview()
        stale = self.consent_file("stale.txt", "Да", age_s=3600)  # older than the preview
        self.assertNoMutation(self.pconfirm(path=stale), "predates")
        self.assertNoMutation(self.pconfirm("Да", mode=0o644))
        self.assertNoMutation(self.pconfirm("да, но позже"))
        self.assertEqual(self.pconfirm("Да")[0], 0)

    def test_privacy_gate_still_required_for_late_consent(self):
        self.late_preview()
        with mock.patch.object(nw, "_check_mutation_readiness", lambda *a, **k: ["gate"]):
            self.assertNoMutation(self.pconfirm("Да"), "readiness")
        self.assertEqual(self.pdata()[0]["status"], "pending")

    def test_legacy_receipt_short_confirm_expiry_unchanged(self):
        self.assertEqual(self.preview(ttl=60)[0], 0)
        self.now = T0 + timedelta(seconds=120)
        rc, out, err = self.reply(ts=(T0 + timedelta(seconds=30)).isoformat())
        self.assertEqual(rc, 1)
        self.assertIn("expired", out + err)
        self.assertEqual(self.q.confirmed_count(), 0)


class TestLateConsentMultiprocess(SqliteMixin, UXMixin, MPBase):
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

    def test_concurrent_late_double_confirm_one_winner(self):
        self.assertEqual(self.ppreview(ttl=60)[0], 0)
        self.now = T0 + timedelta(hours=2)
        files = [self.consent_file(f"l{i}.txt", "Да") for i in range(4)]
        rcs = self.spawn([lambda f=f: self.run_cmd(
            nw.cmd_personal_confirm, scope=self.scope, attest_personal_chat=True,
            consent_file=str(f))[0] for f in files])
        self.assertEqual(sorted(rcs), [0, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(len(self.rows()), 1)


# ── real SQLite: known-recipe registry ──────────────────────────────────────

class TestRecipeRegistryRealSqlite(UXRealBase):
    def test_save_shows_quantities_and_never_logs_a_meal(self):
        rows_before = self.rows()
        rf = self.private_file("r.json", json.dumps(RECIPE))
        rc, out, err = self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                    attest_personal_chat=True, recipe_file=str(rf))
        self.assertEqual(rc, 0, out + err)
        disp = json.loads(out)["display_text"]
        self.assertEqual(disp, "Рецепт: Овсяная каша\n• овсяные хлопья — 50 г\n"
                               "• молоко 1.5% — 200 мл\n"
                               "≈300 ккал · Б 13 · Ж 7 · У 45 г на порцию (по этикеткам)\n"
                               "Сохранить рецепт?")
        res = self.save_recipe()
        self.assertEqual(res["recipe_version"], 2)  # second preview of the same key
        self.assertEqual(self.rows(), rows_before)  # nothing in the queue changed
        self.assertEqual(self.q.confirm_calls, 0)
        rec = self.kinds("recipe")
        self.assertEqual(sorted(r["status"] for r in rec), ["active", "superseded"])
        self.assertEqual(oct(self.pdir().stat().st_mode & 0o777), "0o700")
        for f in self.pfiles():
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")

    def test_recipe_show_and_new_version_retires_old(self):
        r1 = self.save_recipe()
        self.assertEqual(r1["recipe_version"], 1)
        shown = self.recipe_show()
        self.assertEqual(shown["recipe_hash"], r1["recipe_hash"])
        self.assertEqual(shown["draft_template"]["description"],
                         "Известный рецепт v1: овсяные хлопья 50 г; молоко 1.5% 200 мл")
        r2 = self.save_recipe(dict(RECIPE, per_portion=dict(RECIPE["per_portion"],
                                                            energy_kcal=310)))
        self.assertEqual(r2["recipe_version"], 2)
        self.assertNotEqual(r2["recipe_hash"], r1["recipe_hash"])
        self.assertEqual(sorted(r["status"] for r in self.kinds("recipe")), ["active", "retired"])
        self.assertEqual(self.recipe_show()["recipe_version"], 2)

    def test_invalid_recipes_rejected(self):
        bad = {
            "estimate basis": dict(RECIPE, macro_basis="estimate"),
            "no quantity": dict(RECIPE, ingredients=[{"item": "хлопья", "quantity": "немного"}]),
            "implausible macros": dict(RECIPE, per_portion=dict(RECIPE["per_portion"],
                                                                energy_kcal=900)),
            "missing macro": dict(RECIPE, per_portion={"energy_kcal": 300}),
            "bool macro": dict(RECIPE, per_portion=dict(RECIPE["per_portion"], fat_g=True)),
            "extra field": dict(RECIPE, photo="x"),
            "bad alias": dict(RECIPE, aliases=["овсянку?"]),
            "bad key": dict(RECIPE, key="has space"),
            "no ingredients": dict(RECIPE, ingredients=[]),
        }
        for label, r in bad.items():
            with self.subTest(label=label):
                rf = self.private_file("bad.json", json.dumps(r))
                rc, out, err = self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                            attest_personal_chat=True, recipe_file=str(rf))
                self.assertEqual(rc, 1, label)
        rf = self.private_file("dup.json", '{"key": "a", "key": "b"}')
        self.assertEqual(self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                      attest_personal_chat=True, recipe_file=str(rf))[0], 1)
        rf = self.private_file("open.json", json.dumps(RECIPE))
        os.chmod(rf, 0o644)
        self.assertEqual(self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                      attest_personal_chat=True, recipe_file=str(rf))[0], 1)

    def test_recipe_consent_rules(self):
        rf = self.private_file("r.json", json.dumps(RECIPE))
        self.assertEqual(self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                      attest_personal_chat=True, recipe_file=str(rf))[0], 0)

        def confirm(path, scope=None):
            return self.run_cmd(nw.cmd_recipe_confirm, scope=scope or self.scope,
                                attest_personal_chat=True, consent_file=str(path))
        self.assertEqual(confirm(self.consent_file("s.txt", "Да", age_s=600))[0], 1)  # stale
        self.assertEqual(confirm(self.consent_file("n.txt", "нет"))[0], 1)
        self.assertEqual(confirm(self.consent_file("o.txt", "Да"), scope="personal-b")[0], 1)
        good = self.consent_file("g.txt", "Да")
        self.assertEqual(confirm(good)[0], 0)
        self.assertEqual(confirm(good)[0], 1)  # replay
        # the recipe consent file can never be reused as a meal consent
        self.assertEqual(self.ppreview()[0], 0)
        rc, out, err = self.pconfirm(path=good)
        self.assertEqual(rc, 1)
        self.assertIn("already used", err)

    def test_recipe_tamper_fails_closed(self):
        self.save_recipe()
        rec_file = next(f for f in self.pfiles() if json.loads(f.read_text()).get("kind"))
        d = json.loads(rec_file.read_text())
        d["recipe"]["per_portion"]["energy_kcal"] = 310.0
        rec_file.write_text(json.dumps(d))
        rc, out, err = self.run_cmd(nw.cmd_recipe_show, scope=self.scope,
                                    attest_personal_chat=True, recipe_key="oat-std")
        self.assertEqual(rc, 1)
        self.assertIn("tampered", err)

    def test_one_open_question_per_scope(self):
        self.assertEqual(self.ppreview()[0], 0)          # meal preview pending
        rf = self.private_file("r.json", json.dumps(RECIPE))
        self.assertEqual(self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                      attest_personal_chat=True, recipe_file=str(rf))[0], 0)
        meal = [x for x in self.pdata() if not x.get("kind")]
        self.assertEqual(meal[0]["status"], "superseded")
        # a "Да" now cannot confirm the superseded meal preview
        self.assertNoMutation(self.pconfirm("Да"), "no active preview")


# ── real SQLite: known-recipe text log fast path ─────────────────────────────

class TestKnownLogRealSqlite(UXRealBase):
    def setUp(self):
        super().setUp()
        self.raw_sql("DELETE FROM nutrition_queue")
        self.recipe = self.save_recipe()

    def go(self, rp, text, phrase, ld, lt, **kw):
        return self.log_known(text, phrase, ld, lt, request_path=rp, **kw)

    def test_happy_path_compact_ack_and_audit(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.assertEqual(res["state"], "confirmed")
        self.assertEqual(res["user_status"]["text"], "Принято — ожидает синхронизации")
        day = yesterday_berlin()
        self.assertEqual(res["ack_text"], f"Овсяная каша — {day.day} "
                         f"{nw._RU_MONTHS[day.month - 1]}, 08:30–08:31, Берлин\n"
                         "Принято — ожидает синхронизации")
        self.assertNotIn("Health Connect", res["ack_text"])  # no delivery claim
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(len(self.rows()), 1)
        logs = self.kinds("known_log")
        self.assertEqual(len(logs), 1)
        lg = logs[0]
        self.assertEqual(lg["status"], "consumed")
        self.assertEqual(lg["request_raw_text"], text)
        self.assertEqual(lg["client_record_id"], self.q.rid)
        self.assertEqual(lg["recipe_hash"], self.recipe["recipe_hash"])
        self.assertEqual(lg["provenance_level"], "agent_conversation_attested")
        self.assertEqual(lg["time_evidence"]["local_time"], "08:30")
        self.assertEqual(self.q.rec["confirmed_by"], "Ravil")
        for k in ("message_id", "sender_id", "reply_to_message_id"):
            self.assertNotIn(k, lg)

    def test_natural_variants_on_real_queue(self):
        day = yesterday_berlin()
        for i, (text, phrase, hh, mm) in enumerate((
                ("залогируй мою овсянку на завтрак вчера в 7:15", "в 7:15", 7, 15),
                ("Добавь в дневник овсяную кашу вчера в 8 утра", "в 8 утра", 8, 0))):
            with self.subTest(text=text):
                rp = self.private_file(f"v{i}.txt", text)
                self.known_draft(day, hh, mm)
                rc, out, err = self.go(rp, text, phrase, day.isoformat(), f"{hh:02d}:{mm:02d}")
                self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.q.confirmed_count(), 2)

    def test_request_replay_is_read_only(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 0)
        snapshot = self.rows()
        # same file again (same record) and same file for a NEW draft
        rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("Повтор", err)
        self.known_draft(yesterday_berlin(), 9, 0)
        rc, out, err = self.go(rp, text, phrase, ld, "09:00")
        self.assertEqual(rc, 1)
        self.assertIn("already used", err)
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual([r for r in self.rows() if r["state"] == "confirmed"],
                         [r for r in snapshot if r["state"] == "confirmed"])

    def test_one_request_cannot_authorize_two_meals_or_a_duplicate(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 0)
        first = self.q.rid
        # the agent writes a NEW file with the same text and a second draft: duplicate
        rp2 = self.private_file("again.txt", text)
        self.known_draft(yesterday_berlin(), 8, 30)
        self.assertNotEqual(self.q.rid, first)
        rc, out, err = self.go(rp2, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("duplicate", err)
        self.assertEqual(self.q.confirmed_count(), 1)

    def test_draft_must_match_recipe_and_stated_time(self):
        day = yesterday_berlin()
        tmpl = self.recipe_show()["draft_template"]
        cases = {
            "time": dict(start_time=berlin_iso(day, 9, 30), end_time=berlin_iso(day, 9, 31)),
            "macros": dict(energy_kcal=350.0),
            "name": dict(name="Овсянка"),
            "description": dict(description="oats"),
            "interval": dict(end_time=berlin_iso(day, 8, 45)),
        }
        for label, over in cases.items():
            with self.subTest(label=label):
                text = "Запиши овсянку вчера в 8:30"
                rp = self.private_file(f"m-{label}.txt", text)
                self.known_draft(day, 8, 30, **over)
                rc, out, err = self.go(rp, text, "в 8:30", day.isoformat(), "08:30")
                self.assertEqual(rc, 1, label)
                self.assertIn("Не записано", err)
        self.assertEqual(self.q.confirmed_count(), 0)
        self.assertEqual(self.q.confirm_calls, 0)
        del tmpl

    def test_stale_hash_version_or_old_draft_rejected(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        self.assertEqual(self.go(rp, text, phrase, ld, lt, shash="0" * 64)[0], 1)
        self.assertEqual(self.go(rp, text, phrase, ld, lt, version=2)[0], 1)
        # a draft created BEFORE the request was received is not "for" this request
        self.raw_sql("UPDATE nutrition_queue SET created_at='2026-01-01T00:00:00Z'")
        rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("not created for this request", err)
        self.assertEqual(self.q.confirm_calls, 0)

    def test_recipe_binding_and_time_attestation_checked(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        wrong = dict(self.recipe_show(), recipe_hash="0" * 64)
        self.assertEqual(self.go(rp, text, phrase, ld, lt, recipe=wrong)[0], 1)
        wrong = dict(self.recipe_show(), recipe_version=9)
        self.assertEqual(self.go(rp, text, phrase, ld, lt, recipe=wrong)[0], 1)
        for ev in ({"time_phrase": "в 9:30", "local_date": ld, "local_time": lt},
                   {"time_phrase": phrase, "local_date": ld, "local_time": "09:30"},
                   {"time_phrase": phrase, "local_date": "2026-01-01", "local_time": lt},
                   {"time_phrase": "утром", "local_date": ld, "local_time": lt},
                   {"time_phrase": phrase, "local_date": ld}):
            with self.subTest(ev=ev):
                rc, out, err = self.go(rp, text, phrase, ld, lt, evidence=ev)
                self.assertEqual(rc, 1)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 0)  # untouched file still fine

    def test_attestations_scope_and_privacy_gate_required(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        self.assertEqual(self.go(rp, text, phrase, ld, lt, attest_req=False)[0], 1)
        self.assertEqual(self.go(rp, text, phrase, ld, lt, attest_time=False)[0], 1)
        rc, out, err = self.go(rp, text, phrase, ld, lt, scope="personal-b")
        self.assertEqual(rc, 1)
        self.assertIn("no active recipe", err)
        with mock.patch.object(nw, "_check_mutation_readiness", lambda *a, **k: ["gate"]):
            rc, out, err = self.go(rp, text, phrase, ld, lt)
            self.assertEqual(rc, 1)
            self.assertIn("readiness", err)
        self.assertEqual(self.kinds("known_log"), [])     # nothing claimed
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 0)

    def test_insecure_stale_or_symlinked_request_file_rejected(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        os.chmod(rp, 0o644)
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 1)
        os.chmod(rp, 0o600)
        link = self.root / "req-link.txt"
        os.symlink(rp, link)
        self.assertEqual(self.go(link, text, phrase, ld, lt)[0], 1)
        hard = self.root / "req-hard.txt"
        os.link(rp, hard)
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 1)  # nlink 2
        os.unlink(hard)
        old = time.time_ns() - int(3600 * 1e9)
        os.utime(rp, ns=(old, old))
        rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("not fresh", err)
        self.assertEqual(self.q.confirm_calls, 0)

    def test_nonexplicit_requests_never_log_on_real_queue(self):
        day = yesterday_berlin()
        for i, text in enumerate((
                "Не записывай овсянку вчера в 8:30", "Запиши овсянку вчера в 8:30?",
                "Запиши «овсянку» вчера в 8:30", "запиши овсянку вчера утром",
                "запиши овсянку с фото вчера в 8:30", "запиши овсянку вчера примерно в 8:30",
                "запиши овсянку и кофе вчера в 8:30", "сохрани рецепт овсянки",
                "Например: запиши овсянку вчера в 8:30")):
            with self.subTest(text=text):
                rp = self.private_file(f"n{i}.txt", text)
                self.known_draft(day, 8, 30) if i == 0 else None
                rc, out, err = self.go(rp, text, "в 8:30", day.isoformat(), "08:30")
                self.assertEqual(rc, 1, text)
                self.assertIn("use personal-preview", err)
        self.assertEqual(self.q.confirm_calls, 0)
        self.assertEqual(self.q.rec["state"], "draft")

    def test_unknown_or_estimated_recipe_has_no_fast_path(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        fake = {"recipe_key": "nope", "recipe_version": 1, "recipe_hash": "1" * 64}
        rc, out, err = self.go(rp, text, phrase, ld, lt, recipe=fake)
        self.assertEqual(rc, 1)
        self.assertIn("no active recipe", err)
        # a pending (not yet user-confirmed) recipe is not usable either
        rf = self.private_file("r2.json", json.dumps(dict(RECIPE, key="oat-new")))
        self.assertEqual(self.run_cmd(nw.cmd_recipe_preview, scope=self.scope,
                                      attest_personal_chat=True, recipe_file=str(rf))[0], 0)
        fake = {"recipe_key": "oat-new", "recipe_version": 1, "recipe_hash": "1" * 64}
        self.assertEqual(self.go(rp, text, phrase, ld, lt, recipe=fake)[0], 1)
        self.assertEqual(self.q.confirm_calls, 0)

    def test_fast_log_supersedes_open_preview_question(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        meal_rid = self.q.rid
        self.create_draft()                      # some other meal being previewed
        self.assertEqual(self.ppreview()[0], 0)
        self.q.rid = meal_rid
        rc, out, err = self.go(rp, text, phrase, ld, lt, rid=meal_rid)
        self.assertEqual(rc, 0, out + err)
        meal = [x for x in self.pdata() if not x.get("kind")]
        self.assertEqual(meal[0]["status"], "superseded")
        rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 1)
        self.assertIn("no active preview", err)
        self.assertEqual(self.q.confirmed_count(), 1)  # only the fast-logged meal

    def test_correction_updates_same_row_and_needs_preview_plus_fresh_yes(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 0)
        self.assertEqual(self.q.rec["state"], "confirmed")
        # correction: existing queue edit of the SAME id -> version 2, back to draft
        day = yesterday_berlin()
        self.edit(**dict(self.recipe_show()["draft_template"],
                         start_time=berlin_iso(day, 9, 0), end_time=berlin_iso(day, 9, 1)))
        self.assertEqual((self.q.rec["state"], self.q.rec["client_record_version"]),
                         ("draft", 2))
        # the old request can never confirm the corrected version, nor can a new
        # direct request (fast path is version-1 only)
        self.assertEqual(self.go(rp, text, phrase, ld, lt, version=2)[0], 1)
        rp2 = self.private_file("corr.txt", "Запиши овсянку вчера в 9:00")
        self.assertEqual(self.go(rp2, "Запиши овсянку вчера в 9:00", "в 9:00", ld, "09:00",
                                 version=2)[0], 1)
        self.assertEqual(self.q.rec["state"], "draft")
        # preview + fresh actual yes confirms the SAME row
        self.assertEqual(self.ppreview()[0], 0)
        rc, out, err = self.pconfirm("Да")
        self.assertEqual(rc, 0, out + err)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["state"], rows[0]["client_record_version"]), ("confirmed", 2))

    def test_confirm_refusal_marks_log_failed_and_request_stays_used(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        real = nw._run_cli

        def flaky(cli, db, a, stdin_bytes=None):
            return (1, "", "transient") if a[0] == "confirm" else real(cli, db, a, stdin_bytes)
        with mock.patch.object(nw, "_run_cli", flaky):
            rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("Не записано", err)
        self.assertEqual(self.kinds("known_log")[0]["status"], "failed")
        self.assertEqual(self.q.rec["state"], "draft")
        rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertIn("Повтор", err)

    def test_uncertain_helper_output_is_never_a_success(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()

        def fake(cli, db, rid, version, name, confirmer, pre_confirm=None, **kw):
            self.assertIsNone(pre_confirm(self.status_record()))
            print("not json")
            return 0
        with mock.patch.object(nw, "_confirm_with_readback", fake):
            rc, out, err = self.go(rp, text, phrase, ld, lt)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("Статус неизвестен", err)
        self.assertEqual(self.kinds("known_log")[0]["status"], "consuming")
        self.assertEqual(self.go(rp, text, phrase, ld, lt)[0], 1)


class TestKnownLogMultiprocess(SqliteMixin, UXMixin, MPBase):
    def make_queue(self):
        return self.build_real_queue()

    def setUp(self):
        super().setUp()
        self.patches[0].stop()
        p = mock.patch.object(nw, "_resolve", lambda a: (self.q.shim, self.q.db, self.root))
        p.start()
        self.patches[0] = p
        self.create_draft()  # unrelated row; fresh_log_case creates the known draft
        self.save_recipe()

    def displayed(self):
        rc, out, err = nw._run_cli(self.q.shim, self.q.db, ["status", self.q.rid])
        rec = json.loads(out)
        return rec["client_record_version"], nw._record_snapshot_hash(rec)

    def status_record(self):
        return json.loads(nw._run_cli(self.q.shim, self.q.db, ["status", self.q.rid])[1])

    def test_concurrent_same_record_exactly_one_winner(self):
        rp, text, phrase, ld, lt = self.fresh_log_case()
        others = [self.private_file(f"c{i}.txt", text) for i in range(2)]
        kws = [self.log_known_args(text, phrase, ld, lt, request_path=p)
               for p in [rp, rp] + others]
        jobs = [lambda kw=kw: self.run_cmd(nw.cmd_personal_log_known, **kw)[0] for kw in kws]
        rcs = self.spawn(jobs)
        self.assertEqual(sorted(rcs), [0, 1, 1, 1], rcs)
        self.assertEqual(self.q.confirmed_count(), 1)
        self.assertEqual(len([x for x in self.kinds("known_log")
                              if x["status"] == "consumed"]), 1)


if __name__ == "__main__":
    unittest.main()
