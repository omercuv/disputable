"""Offline checks for scripts/spike.py: no network, no keys."""

import importlib.util
import sys
from pathlib import Path

import pytest

SPIKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "spike.py"
spec = importlib.util.spec_from_file_location("spike", SPIKE_PATH)
spike = importlib.util.module_from_spec(spec)
sys.modules["spike"] = spike  # dataclasses look the module up while it loads
spec.loader.exec_module(spike)


@pytest.mark.parametrize("url", ["https://api.sandbox.airwallex.com", "https://files.sandbox.airwallex.com"])
def test_sandbox_hosts_are_allowed(url):
    assert spike.is_sandbox_url(url)


@pytest.mark.parametrize("url", [
    "https://api.airwallex.com",
    "http://api.sandbox.airwallex.com",
    "https://api.sandbox.airwallex.com.evil.io",
])
def test_other_hosts_are_refused(url):
    assert not spike.is_sandbox_url(url)


def test_secret_headers_are_redacted():
    headers = spike.redact_headers({"Authorization": "Bearer abc", "x-api-key": "k", "x-client-id": "c", "Accept": "json"})
    assert headers == {"Authorization": "<redacted>", "x-api-key": "<redacted>", "x-client-id": "<redacted>",
                       "Accept": "json"}


def test_card_and_token_are_masked():
    body = spike.redact({"token": "tok_123", "payment_method": {"card": {"number": "4035501000000008", "cvc": "737"}}})
    assert body["token"] == "<redacted>"
    assert body["payment_method"]["card"] == {"number": "403550******0008", "cvc": "***"}


def test_scrub_removes_known_secret_values():
    spike.add_secret("key_SECRET_123456")
    assert "key_SECRET_123456" not in spike.scrub("sent key_SECRET_123456 in a header")


def test_generated_evidence_files_are_valid():
    spike.check_pdf(spike.make_pdf("Title", ["line one", "line (two)"]))
    spike.check_jpeg(spike.make_jpeg("comment"))
