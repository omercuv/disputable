"""Offline checks for scripts/spike.py: no network, no keys."""

import base64
import binascii
import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

SPIKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "spike.py"
SPIKE_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "spike"
RECORDINGS = sorted(SPIKE_FIXTURES.glob("[0-9]*.json"))
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MADE_UP_ACCOUNT_ID = "9f1c2b3a-1111-4222-8333-444455556666"
MADE_UP_REAL_IP = "2001:db8:85a3::8a2e:370:7334"
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


def test_paying_browser_details_are_redacted():
    attempt = {"latest_payment_attempt": {"device_data": {"ip_address": "2001:db8::1", "device_id": "abc"}}}
    assert spike.redact(attempt)["latest_payment_attempt"]["device_data"] == "<redacted>"


def test_scrub_removes_known_secret_values():
    spike.add_secret("key_SECRET_123456")
    assert "key_SECRET_123456" not in spike.scrub("sent key_SECRET_123456 in a header")


def test_cloudflare_site_code_is_redacted():
    assert spike.redact_headers({"cf-ray": "a52d4c0000000000-XYZ"})["cf-ray"] == "<redacted>"


@pytest.mark.parametrize("address", ["198.51.100.23", "2001:db8:85a3::8a2e:370:7334", "2001:0db8:1:2:3:4:5:6"])
def test_any_address_is_scrubbed_wherever_it_appears(address):
    page = f'<p>Your IP: {address}</p> {{"at": "2026-10-30T20:51:14+00:00", "seen": "20:51:14"}}'
    assert spike.scrub(page) == '<p>Your IP: <redacted></p> {"at": "2026-10-30T20:51:14+00:00", "seen": "20:51:14"}'


@pytest.mark.parametrize("address", ["127.0.0.1", "203.0.113.7"])
def test_the_scripts_own_addresses_stay(address):
    assert spike.scrub(address) == address


def file_id(account_id, name):
    return base64.b64encode(f"{account_id},|,sg,|,{name}".encode()).decode().rstrip("=")


def decoded_file_id(value):
    return base64.b64decode(value + "=" * (-len(value) % 4)).decode()


def test_file_ids_alone_give_the_account_id_away():
    text = json.dumps({"file_ids": [file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"), file_id(MADE_UP_ACCOUNT_ID, "two.pdf_2")]})
    hidden = json.loads(spike.hide_account_id(text))
    assert decoded_file_id(hidden["file_ids"][0]) == f"{spike.ACCOUNT_ID_STAND_IN},|,sg,|,one.pdf_1"
    assert decoded_file_id(hidden["file_ids"][1]) == f"{spike.ACCOUNT_ID_STAND_IN},|,sg,|,two.pdf_2"


def test_a_store_url_alone_gives_the_account_id_away():
    text = json.dumps({"store_url": f"https://github.com/{MADE_UP_ACCOUNT_ID}", "note": f"also {MADE_UP_ACCOUNT_ID}"})
    assert json.loads(spike.hide_account_id(text)) == {"store_url": f"https://github.com/{spike.ACCOUNT_ID_STAND_IN}",
                                                       "note": f"also {spike.ACCOUNT_ID_STAND_IN}"}


def test_the_state_file_keeps_real_file_ids_for_a_resume(tmp_path):
    real = file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1")
    state = spike.State(tmp_path / "state.json")
    state.files["evidence_1"] = {"file_id": real}
    state.save()
    assert spike.State(tmp_path / "state.json").files["evidence_1"]["file_id"] == real


def test_a_recorded_call_keeps_nothing_private(tmp_path):
    def answer(request):
        if request.url.path == spike.LOGIN_PATH:
            return httpx.Response(200, json={"token": "tok_made_up"}, headers={"set-cookie": "session=made_up_cookie; Path=/"})
        if request.url.path.endswith("/blocked"):
            return httpx.Response(403, text=f"<p>Blocked. Your IP: {MADE_UP_REAL_IP}</p>", headers={"cf-ray": "a52d4c-XYZ"})
        return httpx.Response(200, headers={"cf-ray": "a52d4c-XYZ"}, json={
            "file_id": file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"),
            "latest_payment_attempt": {"device_data": {"ip_address": MADE_UP_REAL_IP, "browser": "Edge"}},
            "seller_info": {"store_url": f"https://github.com/{MADE_UP_ACCOUNT_ID}"},
        })

    config = spike.Config("cid_made_up", "key_made_up", "https://api.sandbox.airwallex.com", "https://files.sandbox.airwallex.com")
    api = spike.Api(config, spike.Recorder(tmp_path), spike.State(tmp_path / "state.json"),
                    transport=httpx.MockTransport(answer))
    api.request("GET", "/things/1", label="read")
    api.request("GET", "/things/blocked", label="blocked")
    api.close()

    recorded = "".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("[0-9]*.json"))
    assert spike.ACCOUNT_ID_STAND_IN in recorded
    for private in (MADE_UP_REAL_IP, MADE_UP_ACCOUNT_ID, base64.b64encode(MADE_UP_ACCOUNT_ID.encode()).decode(), "a52d4c",
                    "XYZ", "Edge", "tok_made_up", "cid_made_up", "key_made_up", "made_up_cookie"):
        assert private not in recorded, private


def walk(value, key=None):
    """Every (key, value) in a recording, at any depth."""
    yield key, value
    if isinstance(value, dict):
        for inner_key, inner in value.items():
            yield from walk(inner, inner_key)
    elif isinstance(value, list):
        for inner in value:
            yield from walk(inner, key)


def test_there_are_recordings_to_check():
    assert len(RECORDINGS) > 50


def test_recordings_and_evidence_hold_no_real_address():
    for path in [*RECORDINGS, *SPIKE_FIXTURES.glob("*.pdf")]:
        assert set(spike.ADDRESS.findall(path.read_text(encoding="latin-1"))) <= spike.OWN_ADDRESSES, path.name


def test_recordings_keep_no_browser_or_location_details():
    for path in RECORDINGS:
        for key, value in walk(json.loads(path.read_text(encoding="utf-8"))):
            if key in ("device_data", "cf-ray", "ip_address"):
                assert value == "<redacted>", f"{path.name}: {key}"


def test_recordings_hold_no_real_account_id():
    # The real id can't be named here, so this looks where it hid: at the start of each file id, and in the store URL.
    for path in RECORDINGS:
        for key, value in walk(json.loads(path.read_text(encoding="utf-8"))):
            if key == "store_url":
                assert set(UUID.findall(value)) <= {spike.ACCOUNT_ID_STAND_IN}, path.name
            if isinstance(value, str) and len(value) > 48:
                try:
                    decoded = decoded_file_id(value)
                except (binascii.Error, UnicodeDecodeError):
                    continue
                if ",|," in decoded:
                    assert decoded.split(",|,")[0] == spike.ACCOUNT_ID_STAND_IN, path.name


def test_generated_evidence_files_are_valid():
    spike.check_pdf(spike.make_pdf("Title", ["line one", "line (two)"]))
    spike.check_jpeg(spike.make_jpeg("comment"))
