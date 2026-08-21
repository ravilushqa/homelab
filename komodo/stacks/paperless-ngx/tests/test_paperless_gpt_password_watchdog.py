import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import paperless_gpt_password_watchdog as watchdog


def config(**overrides):
    values = {
        "base_url": "http://paperless.example",
        "api_token": "secret-token",
        "auto_ocr_tag": "paperless-gpt-ocr-auto",
        "interval_seconds": 900,
        "dry_run": False,
        "once": True,
        "page_size": 100,
    }
    values.update(overrides)
    return watchdog.Config(**values)


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_error=None):
        self.payload = payload or {}
        self.content = content
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.payload


class PaginationTests(unittest.TestCase):
    def test_resolve_tag_id_follows_pagination(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse({"results": [{"id": 1, "name": "other"}], "next": "http://next"}),
            FakeResponse(
                {"results": [{"id": 42, "name": "paperless-gpt-ocr-auto"}], "next": None}
            ),
        ]

        self.assertEqual(watchdog.resolve_tag_id(session, config()), 42)


class PasswordDetectionTests(unittest.TestCase):
    def test_pdf_open_password_error_is_detected(self):
        original_fitz = watchdog.fitz
        try:
            watchdog.fitz = Mock()
            watchdog.fitz.open.side_effect = RuntimeError("fitz: document needs password")

            self.assertTrue(watchdog.pdf_needs_password(b"%PDF"))
        finally:
            watchdog.fitz = original_fitz

    def test_pdf_needs_pass_attribute_is_detected(self):
        original_fitz = watchdog.fitz
        document = Mock(needs_pass=True, is_encrypted=False)
        try:
            watchdog.fitz = Mock()
            watchdog.fitz.open.return_value = document

            self.assertTrue(watchdog.pdf_needs_password(b"%PDF"))
            document.close.assert_called_once()
        finally:
            watchdog.fitz = original_fitz

    def test_non_password_parse_error_is_raised(self):
        original_fitz = watchdog.fitz
        try:
            watchdog.fitz = Mock()
            watchdog.fitz.open.side_effect = RuntimeError("cannot open broken document")

            with self.assertRaises(RuntimeError):
                watchdog.pdf_needs_password(b"not a pdf")
        finally:
            watchdog.fitz = original_fitz


class TagPatchTests(unittest.TestCase):
    def test_remove_tag_preserves_other_tags(self):
        session = Mock()
        session.patch.return_value = FakeResponse()
        document = {"id": 7, "tags": [1, 2, 3]}

        changed = watchdog.remove_tag_from_document(session, config(), document, 2)

        self.assertTrue(changed)
        session.patch.assert_called_once()
        self.assertEqual(session.patch.call_args.kwargs["json"], {"tags": [1, 3]})

    def test_dry_run_does_not_patch(self):
        session = Mock()
        document = {"id": 7, "tags": [{"id": 2}, {"id": 3}]}

        changed = watchdog.remove_tag_from_document(session, config(dry_run=True), document, 2)

        self.assertTrue(changed)
        session.patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
