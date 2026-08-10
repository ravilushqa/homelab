#!/usr/bin/env python3
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - validated at runtime
    fitz = None


PASSWORD_ERROR_MARKERS = ("password", "needs password", "encrypted")


@dataclass(frozen=True)
class Config:
    base_url: str
    api_token: str
    auto_ocr_tag: str
    interval_seconds: int
    dry_run: bool
    once: bool
    page_size: int


def log(message: str) -> None:
    print(message, flush=True)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_positive_int(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero")
    return value


def load_config() -> Config:
    api_token = os.environ.get("PAPERLESS_API_TOKEN", "").strip()
    if not api_token:
        raise SystemExit("PAPERLESS_API_TOKEN is required")
    if fitz is None:
        raise SystemExit("PyMuPDF is required")

    return Config(
        base_url=os.environ.get("PAPERLESS_BASE_URL", "http://webserver:8000").rstrip("/"),
        api_token=api_token,
        auto_ocr_tag=os.environ.get("AUTO_OCR_TAG", "paperless-gpt-ocr-auto"),
        interval_seconds=parse_positive_int("WATCHDOG_INTERVAL_SECONDS", "900"),
        dry_run=parse_bool(os.environ.get("WATCHDOG_DRY_RUN", "false")),
        once=parse_bool(os.environ.get("WATCHDOG_ONCE", "false")),
        page_size=parse_positive_int("WATCHDOG_PAGE_SIZE", "100"),
    )


def make_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {config.api_token}"})
    return session


def api_url(config: Config, path: str) -> str:
    return urljoin(f"{config.base_url}/", path.lstrip("/"))


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def paginated_results(session: requests.Session, first_url: str) -> list[dict[str, Any]]:
    url = first_url
    results: list[dict[str, Any]] = []
    while url:
        payload = get_json(session, url)
        page_results = payload.get("results")
        if not isinstance(page_results, list):
            raise RuntimeError(f"Unexpected paginated response from {url}")
        results.extend(page_results)
        next_url = payload.get("next")
        url = urljoin(url, next_url) if next_url else ""
    return results


def resolve_tag_id(session: requests.Session, config: Config) -> int:
    tags_url = api_url(config, f"/api/tags/?page_size={config.page_size}")
    for tag in paginated_results(session, tags_url):
        if tag.get("name") == config.auto_ocr_tag:
            tag_id = tag.get("id")
            if not isinstance(tag_id, int):
                raise RuntimeError(f"Tag {config.auto_ocr_tag} has invalid id")
            return tag_id
    raise RuntimeError(f"Tag {config.auto_ocr_tag} not found")


def list_documents_with_tag(
    session: requests.Session, config: Config, tag_id: int
) -> list[dict[str, Any]]:
    documents_url = api_url(
        config,
        f"/api/documents/?tags__id__all={tag_id}&page_size={config.page_size}",
    )
    return paginated_results(session, documents_url)


def download_original(session: requests.Session, config: Config, document_id: int) -> bytes:
    response = session.get(api_url(config, f"/api/documents/{document_id}/download/"), timeout=120)
    response.raise_for_status()
    return response.content


def is_password_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in PASSWORD_ERROR_MARKERS)


def pdf_needs_password(pdf_bytes: bytes) -> bool:
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        if is_password_error(exc):
            return True
        raise

    try:
        return bool(
            getattr(document, "needs_pass", False)
            or getattr(document, "is_encrypted", False)
        )
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()


def document_tag_ids(document: dict[str, Any]) -> list[int]:
    tags = document.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("document tags field is not a list")

    tag_ids: list[int] = []
    for tag in tags:
        if isinstance(tag, int):
            tag_ids.append(tag)
        elif isinstance(tag, dict) and isinstance(tag.get("id"), int):
            tag_ids.append(tag["id"])
        else:
            raise ValueError(f"unsupported tag value: {tag!r}")
    return tag_ids


def remove_tag_from_document(
    session: requests.Session,
    config: Config,
    document: dict[str, Any],
    tag_id: int,
) -> bool:
    document_id = document.get("id")
    if not isinstance(document_id, int):
        raise ValueError("document id is missing or invalid")

    current_tags = document_tag_ids(document)
    new_tags = [current_tag_id for current_tag_id in current_tags if current_tag_id != tag_id]
    if len(new_tags) == len(current_tags):
        log(f"document {document_id}: tag already absent")
        return False

    if config.dry_run:
        log(f"document {document_id}: dry-run would remove tag {config.auto_ocr_tag}")
        return True

    response = session.patch(
        api_url(config, f"/api/documents/{document_id}/"),
        json={"tags": new_tags},
        timeout=60,
    )
    response.raise_for_status()
    log(f"document {document_id}: removed tag {config.auto_ocr_tag}")
    return True


def process_document(
    session: requests.Session,
    config: Config,
    document: dict[str, Any],
    tag_id: int,
) -> bool:
    document_id = document.get("id", "unknown")
    try:
        if not isinstance(document_id, int):
            raise ValueError("document id is missing or invalid")

        pdf_bytes = download_original(session, config, document_id)
        if pdf_needs_password(pdf_bytes):
            log(f"document {document_id}: password-protected PDF detected")
            return remove_tag_from_document(session, config, document, tag_id)

        log(f"document {document_id}: ok")
    except Exception as exc:
        log(f"document {document_id}: skipped after error: {exc}")
    return False


def run_once(session: requests.Session, config: Config) -> None:
    tag_id = resolve_tag_id(session, config)
    documents = list_documents_with_tag(session, config, tag_id)
    log(f"watchdog: found {len(documents)} document(s) tagged {config.auto_ocr_tag}")

    changed = 0
    for document in documents:
        if process_document(session, config, document, tag_id):
            changed += 1

    log(f"watchdog: removed tag from {changed} document(s)")


def main() -> int:
    try:
        config = load_config()
        session = make_session(config)

        while True:
            run_once(session, config)
            if config.once:
                break
            time.sleep(config.interval_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log(f"watchdog: fatal error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
