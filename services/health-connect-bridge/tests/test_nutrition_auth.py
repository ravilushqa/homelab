"""
Nutrition auth separation tests.

SYNTHETIC — isolated servers, no production writes.
Tests: ingest token cannot read nutrition; nutrition token cannot ingest;
       identical tokens rejected at startup; aliased DB paths rejected.
"""

import os
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path

from ..nutrition_queue import NutritionStore, CONFIRM_AFFIRM_PHRASE
from .helpers import (
    NutritionTestServer,
    TestServer,
    _SYNTHETIC_TOKEN,
    _SYNTHETIC_NUTRITION_TOKEN,
    make_temp_db,
    make_temp_token,
)
from .fixtures import minimal_payload


class TestScopedAuthHTTP(unittest.TestCase):
    def setUp(self):
        self.srv = NutritionTestServer()
        entry = self.srv._nutrition_store.create_entry({
            "name": "Auth Test Meal",
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-15T12:30:00+00:00",
            "energy_kcal": 500.0,
            "protein_g": 30.0,
            "carbohydrate_g": 60.0,
            "fat_g": 15.0,
        })
        self.crid = entry["client_record_id"]
        self.srv._nutrition_store.confirm_entry(
            self.crid, 1, CONFIRM_AFFIRM_PHRASE, "alice"
        )

    def tearDown(self):
        self.srv.stop()

    def test_ingest_token_cannot_get_queue(self):
        """Sending ingest token in X-Nutrition-Token is rejected (wrong value)."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue",
                     headers={"X-Nutrition-Token": self.srv.ingest_token_str})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (400, 401))

    def test_ingest_token_cannot_ack(self):
        import json
        data = json.dumps({
            "client_record_id": self.crid,
            "client_record_version": 1,
        }).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/nutrition/ack", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Nutrition-Token": self.srv.ingest_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (400, 401))

    def test_health_token_header_rejected_on_queue(self):
        """X-Health-Token header is the wrong header for nutrition endpoints."""
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/nutrition/queue",
                     headers={"X-Health-Token": self.srv.ingest_token_str})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertIn(status, (400, 401))

    def test_nutrition_token_cannot_ingest(self):
        """The nutrition token value is wrong for ingest X-Health-Token."""
        import json
        data = json.dumps(minimal_payload()).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/ingest/health-connect", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Health-Token": self.srv.nutrition_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 401)

    def test_ingest_token_can_ingest(self):
        import json
        data = json.dumps(minimal_payload()).encode()
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("POST", "/ingest/health-connect", body=data, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Health-Token": self.srv.ingest_token_str,
        })
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 200)

    def test_nutrition_token_can_get_queue(self):
        status, body = self.srv.nutrition_get(token=self.srv.nutrition_token_str)
        self.assertEqual(status, 200)


class TestPerServerIsolation(unittest.TestCase):
    """Two server instances must not share handler class state."""

    def test_two_servers_have_isolated_state(self):
        """Nutrition disabled on srv2 must not leak srv1 nutrition config."""
        srv1 = NutritionTestServer()  # nutrition enabled
        srv2 = TestServer()           # nutrition disabled (ingest-only)
        try:
            # srv1 nutrition queue works
            status1, _ = srv1.nutrition_get()
            self.assertEqual(status1, 200)

            # srv2 nutrition route returns 404 (not configured)
            conn = HTTPConnection(srv2.host, srv2.port, timeout=10)
            conn.request("GET", "/nutrition/queue",
                         headers={"X-Nutrition-Token": "anything"})
            resp = conn.getresponse()
            status2 = resp.status
            resp.read()
            conn.close()
            self.assertEqual(status2, 404)
        finally:
            srv1.stop()
            srv2.stop()


class TestTokenMaterialReject(unittest.TestCase):
    def test_identical_tokens_rejected(self):
        from ..receiver import BridgeServer
        db = make_temp_db()
        ndb = make_temp_db()
        _, tpath = make_temp_token(_SYNTHETIC_TOKEN)
        _, ntpath = make_temp_token(_SYNTHETIC_TOKEN)  # SAME value
        try:
            srv = BridgeServer(
                host="127.0.0.1", port=0,
                token_path=tpath, db_path=db,
                nutrition_token_path=ntpath, nutrition_db_path=ndb,
            )
            with self.assertRaises(RuntimeError):
                srv.start()
        except RuntimeError:
            pass
        finally:
            for p in (db, ndb, tpath, ntpath):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    def test_partial_nutrition_config_rejected(self):
        """Only one of nutrition_token_path / nutrition_db_path → fail closed."""
        from ..receiver import BridgeServer
        _, tpath = make_temp_token(_SYNTHETIC_TOKEN)
        _, ntpath = make_temp_token(_SYNTHETIC_NUTRITION_TOKEN)
        db = make_temp_db()
        try:
            with self.assertRaises(RuntimeError):
                BridgeServer(
                    host="127.0.0.1", port=0,
                    token_path=tpath, db_path=db,
                    nutrition_token_path=ntpath,
                    nutrition_db_path=None,
                )
        finally:
            for p in (tpath, ntpath, db):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


class TestAliasedDBReject(unittest.TestCase):
    def test_hardlinked_db_paths_rejected(self):
        from ..receiver import _check_no_path_alias
        fd, path_a = tempfile.mkstemp(suffix=".sqlite3", prefix="test_alias_")
        os.close(fd)
        path_b = path_a + "_hardlink"
        try:
            os.link(path_a, path_b)
            with self.assertRaises(RuntimeError):
                _check_no_path_alias(Path(path_a), Path(path_b))
        finally:
            for p in (path_a, path_b):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_symlinked_path_rejected(self):
        from ..receiver import _check_no_path_alias
        fd, path_a = tempfile.mkstemp(suffix=".sqlite3", prefix="test_sym_")
        os.close(fd)
        path_b = path_a + "_symlink"
        try:
            os.symlink(path_a, path_b)
            with self.assertRaises(RuntimeError):
                _check_no_path_alias(Path(path_a), Path(path_b))
        finally:
            for p in (path_a, path_b):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_different_paths_ok(self):
        from ..receiver import _check_no_path_alias
        fd_a, path_a = tempfile.mkstemp(suffix=".sqlite3", prefix="test_diff_a_")
        fd_b, path_b = tempfile.mkstemp(suffix=".sqlite3", prefix="test_diff_b_")
        os.close(fd_a); os.close(fd_b)
        try:
            _check_no_path_alias(Path(path_a), Path(path_b))
        finally:
            for p in (path_a, path_b):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
