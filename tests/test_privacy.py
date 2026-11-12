"""privacy.py: the spike's scrubbing, moved and given its secrets and allowed addresses, and the checks that refuse a file
before it goes into the public repo.

The parity tests run scripts/spike.py's own functions and the Scrubber on the spike tests' strings and on every committed
spike recording (never state.json or an archive folder). Every value here is made up: the seed's, reserved domains,
documentation ranges, private ranges, or numbers no phone can have.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import cases
from disputable import privacy
from disputable.privacy import (ACCOUNT_ID_STAND_IN, ADDRESS, FOUND_API_ID, FOUND_API_KEY, FOUND_BEARER, FOUND_EMAIL,
                                FOUND_FILE_ID, FOUND_IP, FOUND_KEY_HEADER, FOUND_LOCAL_PATH, FOUND_OPEN_FILE_ID,
                                FOUND_PHONE, FOUND_SECRET, FOUND_SECRET_ENCODED, FOUND_STORE_URL, FOUND_UUID, REDACTED,
                                SECRET_HEADERS, SECRET_KEYS, Allowed, Scrubber, account_id_of, base64_tokens, check_files,
                                decode_base64, reading_problems)
from disputable.reader import to_json
from disputable.readings_file import Header, ReadingsFile, save_readings
from disputable.seed.fernhill import ENDING_2, FERNHILL
from disputable.seed.suite import SUITE

ROOT = Path(__file__).resolve().parent.parent
SPIKE_PATH = ROOT / "scripts" / "spike.py"
RECORDINGS = sorted((ROOT / "fixtures" / "spike").glob("[0-9][0-9][0-9]-*.json"))  # never state.json or archive-*
MADE_UP_ACCOUNT_ID = "9f1c2b3a-1111-4222-8333-444455556666"
MADE_UP_REAL_IP = "2001:db8:85a3::8a2e:370:7334"  # a documentation address, but no allow-list here holds it
TOKEN, CLIENT_ID, API_KEY, COOKIE = "tok_made_up_4242", "cid_made_up_4242", "key_made_up_4242", "made_up_cookie_4242"
ALLOWED = Allowed.of(FERNHILL, ENDING_2, SUITE)
STORY = ("earbuds", "headphones", "headphones_round_2", "ending_2")  # the story's four reads


def _load_spike():
    spec = importlib.util.spec_from_file_location("spike_for_privacy", SPIKE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["spike_for_privacy"] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def b64(text):
    return base64.b64encode(text.encode()).decode()


def file_id(account_id, name):
    """An Airwallex file id as the sandbox makes one: base64 of '<account id>,|,<region>,|,<file name>', unpadded."""
    return b64(f"{account_id},|,sg,|,{name}").rstrip("=")


@pytest.fixture
def same_secrets(monkeypatch):
    """The spike's scrubber and a Scrubber that know the same secrets and let the spike's own addresses stand."""
    def make(*secrets):
        monkeypatch.setattr(spike, "_SECRET_VALUES", set(secrets))
        return Scrubber(secrets, frozenset(spike.OWN_ADDRESSES))
    return make


# ----------------------------------------------------------------------------- parity with the spike
def test_the_scrubbers_rules_are_the_spikes():
    assert (SECRET_HEADERS, SECRET_KEYS, REDACTED) == (spike.SECRET_HEADERS, spike.SECRET_KEYS, spike.REDACTED)
    assert ADDRESS.pattern == spike.ADDRESS.pattern
    assert ACCOUNT_ID_STAND_IN == spike.ACCOUNT_ID_STAND_IN


SPIKE_TEXTS = [  # tests/test_spike_offline.py's strings
    *(f'<p>Your IP: {address}</p> {{"at": "2026-10-30T20:51:14+00:00", "seen": "20:51:14"}}'
      for address in ("198.51.100.23", "2001:db8:85a3::8a2e:370:7334", "2001:0db8:1:2:3:4:5:6")),
    "seen from 198.51.100.23.", "seen from 198.51.100.23:443", "client:198.51.100.23", "seen from 2001:db8:85a3::1.",
    "client:2001:db8:85a3::1",
    "version 1.2.3.4.5", "at 20:51:14.", "v1.2.3.4",
    "127.0.0.1", "203.0.113.7", "127.0.0.1:8000",
    "sent key_SECRET_123456 in a header",
    *(page.format(MADE_UP_REAL_IP) for page in ("Your IP:\n{}\n", "Your IP:\r\n{}", "Your IP:\t{}", "Your IP:\f{}",
                                                 "Your IP:\x01{}")),
]


@pytest.mark.parametrize("text", SPIKE_TEXTS)
def test_scrub_matches_the_spike_on_its_tests_strings(same_secrets, text):
    scrubber = same_secrets("key_SECRET_123456")
    assert scrubber.scrub(text) == spike.scrub(text)
    assert scrubber.scrub_strings({"a": [text, (text,)], "b": 1}) == spike.scrub_strings({"a": [text, (text,)], "b": 1})


def test_redaction_matches_the_spike():
    for headers in ({"Authorization": "Bearer abc", "x-api-key": "k", "x-client-id": "c", "Accept": "json"},
                    {"cf-ray": "a52d4c0000000000-XYZ", "Set-Cookie": "session=x", "Cookie": "y"}, None):
        assert Scrubber.redact_headers(headers) == spike.redact_headers(headers)
    for body in ({"token": "tok_123", "payment_method": {"card": {"number": "4035501000000008", "cvc": "737"}}},
                 {"latest_payment_attempt": {"device_data": {"ip_address": "2001:db8::1", "device_id": "abc"}}},
                 [{"client_secret": "cs_1", "Refresh_Token": "", "api_key": None}, "text", 7]):
        assert Scrubber.redact(body) == spike.redact(body)
    assert Scrubber.redact({"token": "tok_123"}) == {"token": REDACTED}
    assert Scrubber.redact({"card": {"number": "4035501000000008", "cvc": "737"}}) == {
        "card": {"number": "403550******0008", "cvc": "***"}}


def test_hiding_the_account_id_matches_the_spike():
    ids = json.dumps({"file_ids": [file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"), file_id(MADE_UP_ACCOUNT_ID, "two.pdf_2")]})
    url = json.dumps({"store_url": f"https://github.com/{MADE_UP_ACCOUNT_ID}", "note": f"also {MADE_UP_ACCOUNT_ID}"})
    for text in (ids, url, json.dumps({"note": "nothing to hide"})):
        assert Scrubber.hide_account_id(text) == spike.hide_account_id(text)
    hidden = json.loads(Scrubber.hide_account_id(ids))["file_ids"]
    assert [account_id_of(one) for one in hidden] == [ACCOUNT_ID_STAND_IN, ACCOUNT_ID_STAND_IN]
    assert json.loads(Scrubber.hide_account_id(url)) == {"store_url": f"https://github.com/{ACCOUNT_ID_STAND_IN}",
                                                         "note": f"also {ACCOUNT_ID_STAND_IN}"}


@pytest.mark.parametrize("page", ["Your IP:\n{}\n", "Your IP:\r\n{}", "Your IP:\t{}", "Your IP:\f{}", "Your IP:\x01{}"])
def test_a_recording_is_written_the_same_by_both(same_secrets, tmp_path, page):
    scrubber = same_secrets(TOKEN)
    record = {"text": page.format(MADE_UP_REAL_IP), "nested": [{"note": page.format("2001:db8::1"), "token": TOKEN}]}
    name = spike.Recorder(tmp_path).write("blocked", record)
    written = (tmp_path / name).read_text(encoding="utf-8")
    assert scrubber.dumps({"seq": 1, **record}, indent=2, ensure_ascii=False, default=str) == written
    assert MADE_UP_REAL_IP not in written and "2001:db8::1" not in written and TOKEN not in written


PLANTED = {"token": TOKEN, "note": f"seen from 198.51.100.23\nand {MADE_UP_REAL_IP}", "header": f"sent {API_KEY} here",
           "device_data": {"ip_address": "192.0.2.201"}, "card": {"number": "4035501000000008", "cvc": "737"}}


def test_there_are_spike_recordings_to_compare():
    assert len(RECORDINGS) > 50
    inside = [path.relative_to(ROOT).as_posix() for path in RECORDINGS]  # the folder above the project may say anything
    assert not [path for path in inside if path.endswith("/state.json") or "archive" in path]


def test_every_committed_recording_scrubs_as_the_spike_does(same_secrets, tmp_path):
    scrubber = same_secrets(TOKEN, API_KEY)
    swapped_back = 0
    for number, path in enumerate(RECORDINGS):
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        assert scrubber.scrub(text) == spike.scrub(text), path.name
        assert scrubber.hide_account_id(text) == spike.hide_account_id(text), path.name
        assert scrubber.redact(data) == spike.redact(data), path.name
        assert scrubber.scrub_strings(data) == spike.scrub_strings(data), path.name
        # the account id as the sandbox sent it: both give the stand-in back, plain and inside every file id
        swapped = text.replace(ACCOUNT_ID_STAND_IN, MADE_UP_ACCOUNT_ID).replace(b64(ACCOUNT_ID_STAND_IN),
                                                                                b64(MADE_UP_ACCOUNT_ID))
        assert scrubber.hide_account_id(swapped) == spike.hide_account_id(swapped) == text, path.name
        swapped_back += swapped != text
        record = {**json.loads(swapped), "planted": PLANTED}
        folder = tmp_path / f"{number:03d}"
        written = (folder / spike.Recorder(folder).write("parity", record)).read_text(encoding="utf-8")
        assert scrubber.dumps({"seq": 1, **record}, indent=2, ensure_ascii=False, default=str) == written, path.name
        assert MADE_UP_ACCOUNT_ID not in written and TOKEN not in written and "198.51.100.23" not in written
    assert swapped_back > 10  # the uploads, the challenges and the reads after them carry the account id


# ----------------------------------------------------------------------------- a recorded exchange
def _exchanges():
    login = {"request": {"method": "POST", "url": "https://api.sandbox.airwallex.com/api/v1/authentication/login",
                         "headers": Scrubber.redact_headers({"x-client-id": CLIENT_ID, "x-api-key": API_KEY,
                                                             "accept": "application/json"}), "body": None},
             "response": {"status": 201, "json": {"token": TOKEN, "expires_at": "2026-11-10T09:30:00+0000"},
                          "headers": Scrubber.redact_headers({"set-cookie": f"session={COOKIE}; Path=/",
                                                              "cf-ray": "a52d4c-XYZ"})}}
    upload = {"request": {"method": "POST", "url": "https://files.sandbox.airwallex.com/api/v1/files/upload",
                          "headers": Scrubber.redact_headers({"authorization": f"Bearer {TOKEN}"}), "body": None},
              "response": {"status": 201, "headers": Scrubber.redact_headers({"cf-ray": "a52d4c-XYZ"}),
                           "json": {"file_id": file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"), "notes": "Order #1109"}}}
    read = {"request": {"method": "GET", "headers": Scrubber.redact_headers({"authorization": f"Bearer {TOKEN}"}),
                        "url": "https://api.sandbox.airwallex.com/api/v1/pa/payment_disputes/dst_made_up"},
            "response": {"status": 200, "headers": Scrubber.redact_headers({"cf-ray": "a52d4c-XYZ"}), "json": {
                "latest_payment_attempt": {"device_data": {"ip_address": MADE_UP_REAL_IP, "browser": "Edge"}},
                "seller_info": {"store_url": f"https://github.com/{MADE_UP_ACCOUNT_ID}"},
                "file_ids": [file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1")], "echo": f"token {TOKEN} from {MADE_UP_REAL_IP}",
                "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "https://disputable.invalid/made-up"))}}}
    blocked = {"request": {"method": "GET", "url": "https://api.sandbox.airwallex.com/blocked", "headers": {}},
               "response": {"status": 403, "text": f"<p>Blocked. Your IP: {MADE_UP_REAL_IP}</p>", "json": None,
                            "headers": Scrubber.redact_headers({"cf-ray": "a52d4c-XYZ"})}}
    return login, upload, read, blocked


PRIVATE = (MADE_UP_REAL_IP, MADE_UP_ACCOUNT_ID, b64(MADE_UP_ACCOUNT_ID), "a52d4c", "XYZ", "Edge", TOKEN, CLIENT_ID,
           API_KEY, COOKIE, "Bearer")


@pytest.mark.parametrize("learns_the_account", [False, True])
def test_a_recorded_exchange_keeps_nothing_private(tmp_path, learns_the_account):
    login, upload, read, blocked = _exchanges()
    scrubber = Scrubber([CLIENT_ID, API_KEY], ALLOWED.addresses)
    lines = [scrubber.dumps(login)]
    scrubber.learn(TOKEN)  # the recorder learns each login's token
    if learns_the_account:  # as the recorder does, from the first upload's answer, before that line is written
        scrubber.learn(account_id_of(upload["response"]["json"]["file_id"]))
    lines += [scrubber.dumps(exchange) for exchange in (upload, read, blocked)]
    recorded = "\n".join(lines) + "\n"
    for private in PRIVATE:
        assert private not in recorded, private
    hidden = [json.loads(line)["response"]["json"] for line in lines[1:3]]
    assert account_id_of(hidden[0]["file_id"]) == account_id_of(hidden[1]["file_ids"][0]) == ACCOUNT_ID_STAND_IN
    store_url = hidden[1]["seller_info"]["store_url"]
    assert store_url == f"https://github.com/{REDACTED if learns_the_account else ACCOUNT_ID_STAND_IN}"
    path = tmp_path / "exchanges.jsonl"
    path.write_text(recorded, encoding="utf-8", newline="\n")
    assert check_files([path], ALLOWED, scrubber.secrets | {MADE_UP_ACCOUNT_ID}) == []


def test_a_learned_secret_is_scrubbed_from_every_later_text():
    scrubber = Scrubber([], frozenset({privacy.LOCAL}))
    assert scrubber.scrub(f"token {TOKEN}") == f"token {TOKEN}"
    scrubber.learn(TOKEN)
    scrubber.learn("short")  # under 6 characters: it would blank ordinary words, as the spike skips it
    scrubber.learn(None)
    assert scrubber.scrub(f"token {TOKEN}, short") == f"token {REDACTED}, short"
    assert scrubber.secrets == frozenset({TOKEN})


# ----------------------------------------------------------------------------- what the seed holds
def test_allowed_holds_the_seeds_own_values_and_no_others():
    assert {"127.0.0.1", "203.0.113.7", "198.51.100.77", "198.51.100.42", "192.0.2.86"} <= ALLOWED.addresses
    assert {"alex.sample@example.com", "support@fernhill-audio.example", "priya@fernhill-audi0.example"} <= ALLOWED.emails
    assert {"4155550142", "4155550123", "4155550199"} <= ALLOWED.phones
    assert {"59ec5db9-399c-4043-9a6c-fcd1a48d99aa", "d0000001-0000-4000-8000-000000000001"} <= ALLOWED.uuids
    assert MADE_UP_ACCOUNT_ID not in ALLOWED.uuids and ACCOUNT_ID_STAND_IN not in ALLOWED.uuids
    assert "198.51.100.79" not in ALLOWED.addresses
    assert Allowed.of() == Allowed(frozenset({"127.0.0.1"}), frozenset(), frozenset(), frozenset())
    assert Allowed.of(FERNHILL).uuids < ALLOWED.uuids


@pytest.fixture(scope="module")
def story_reading():
    return cases.read_with("headphones")[0]


def with_note(reading, note):
    """The reading with one row's note written by hand, as Claude might write it."""
    return replace(reading, rows=(replace(reading.rows[0], note=note), *reading.rows[1:]))


REFUSED = [  # one value per row of E 6.3's table, and the kind each one is refused as
    ("sk-" + "ant-made-up-key", FOUND_API_KEY),
    ("x-api-" + "key: made-up", FOUND_KEY_HEADER),
    ("Bearer madeuptoken0123456789", FOUND_BEARER),
    ("req_" + "011MadeUpRequestId012", FOUND_API_ID),
    ("msg_" + "01MadeUpMessageId0123", FOUND_API_ID),
    ("toolu_" + "01MadeUpToolUseId012", FOUND_API_ID),
    ("10.20.30.40", FOUND_IP),  # a private address: no documentation range holds it
    ("fd00::1234:5678", FOUND_IP),
    ("made.up@unreserved.zz", FOUND_EMAIL),
    ("+44 20 7946 0958", FOUND_PHONE),
    ("(012) 345-6789", FOUND_PHONE),  # no area code starts with 0
    ("012-345-6789", FOUND_PHONE),
    (MADE_UP_ACCOUNT_ID, FOUND_UUID),
    (file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"), FOUND_FILE_ID),
    (file_id(ACCOUNT_ID_STAND_IN, "one.pdf_1"), FOUND_FILE_ID),  # hidden or not, a reading never holds one
    ("C:\\Users\\someone\\Desktop\\notes.txt", FOUND_LOCAL_PATH),
    ("/home/someone/disputable", FOUND_LOCAL_PATH),
    ("/Users/someone/disputable", FOUND_LOCAL_PATH),
]


@pytest.mark.parametrize("value, kind", REFUSED)
def test_reading_problems_names_the_place_and_kind_never_the_value(story_reading, value, kind):
    reading = with_note(story_reading, f"The record says {value} there.")
    problems = reading_problems(to_json(reading), ALLOWED)
    assert problems == [f"reading {reading.key[:8]}..., rows[0].note: {kind}."]
    assert value not in problems[0]


PASSES = [
    "203.0.113.7", "198.51.100.77", "192.0.2.201", "2001:db8::5", "127.0.0.1",  # the seed's, documentation, local
    "alex.sample@example.com", "someone@example.org", "a.person@shop.test", "x@y.invalid", "z@fernhill.example",
    "+1 4155550142", "(415) 555-0177", "415-555-0199",  # the seed's, and the 555-01xx numbers kept for fiction
    "59ec5db9-399c-4043-9a6c-fcd1a48d99aa",  # the seed's device id
    "the bearer of the card", "the request_id field", "msg_short", "version 1.2.3.4.5", "at 20:51:14.",
]


@pytest.mark.parametrize("value", PASSES)
def test_the_seeds_own_kinds_of_value_pass(story_reading, value):
    assert reading_problems(to_json(with_note(story_reading, f"The record says {value} there.")), ALLOWED) == []


def test_the_seeds_records_and_the_storys_readings_pass():
    texts = [text for store in (FERNHILL, ENDING_2, SUITE) for text in privacy._texts(store)]
    assert len(texts) > 9000
    assert reading_problems(json.dumps(texts), ALLOWED) == []
    for label in STORY:
        assert reading_problems(to_json(cases.read_with(label)[0]), ALLOWED) == [], label


def test_an_escaped_line_break_cant_hide_a_value(story_reading):
    """The proposal is JSON inside JSON: its own line breaks read \\n in the reading's text, so it is walked again."""
    reading = replace(story_reading, proposal=json.dumps({"note": "call back\n10.20.30.40"}))
    assert "\\\\n10.20.30.40" in to_json(reading)  # where the n would hide the address from a plain search
    assert reading_problems(to_json(reading), ALLOWED) == [f"reading {reading.key[:8]}..., proposal.note: {FOUND_IP}."]


def test_a_text_that_isnt_json_is_read_line_by_line():
    assert reading_problems("fine\nwrite to made.up@unreserved.zz\n", ALLOWED) == [f"line 2: {FOUND_EMAIL}."]
    assert reading_problems('"10.20.30.40"', ALLOWED) == [f"line 1: {FOUND_IP}."]


# ----------------------------------------------------------------------------- base64 and file ids
def test_base64_tokens_find_a_file_id_wherever_it_sits():
    token = file_id(MADE_UP_ACCOUNT_ID, "evidence_1_ab12.pdf_1793393507092")
    for text in (json.dumps({"file_id": token}), f"https://files.example.com/{token}", f"file_{token}", f"id{token}",
                 f"ab1{token}", f"abcd{token}", f"{token}==", f"x-{token}-y"):
        decoded = [decode_base64(found) for found in base64_tokens(text)]
        assert any(privacy.SEPARATOR in one and one.split(privacy.SEPARATOR)[0].endswith(MADE_UP_ACCOUNT_ID)
                   for one in decoded if one), text
    assert base64_tokens("x" * 47) == [] and base64_tokens(token[:47]) == []
    assert base64_tokens(f"a {token} b")[0] == token


GLUED = ("{}s", "{}sxyzw", "ab{}s", "abcd{}s", "{}12", "{}123")  # a character or more glued after the id, or both sides


@pytest.mark.parametrize("name", ["a.pdf_1", "ab.pdf_1", "abc.pdf_1"])  # ids of 0, 2 and 3 characters past a group of 4
def test_base64_tokens_find_a_file_id_with_characters_glued_after_it(tmp_path, name):
    token = file_id(MADE_UP_ACCOUNT_ID, name)
    assert {len(file_id(MADE_UP_ACCOUNT_ID, other)) % 4 for other in ("a.pdf_1", "ab.pdf_1", "abc.pdf_1")} == {0, 2, 3}
    path = tmp_path / "exchanges.jsonl"
    for glued in GLUED:
        text = f"File {glued.format(token)} is not valid"
        decoded = [decode_base64(found) or "" for found in base64_tokens(text)]
        assert any(privacy.SEPARATOR in one and one.split(privacy.SEPARATOR)[0].endswith(MADE_UP_ACCOUNT_ID)
                   for one in decoded), glued
        path.write_text(json.dumps({"request": {}, "response": {"json": {"message": text}}}) + "\n", encoding="utf-8")
        assert check_files([path], ALLOWED, []) == [
            f"{tmp_path.name}/exchanges.jsonl, line 1, response.json.message: {FOUND_OPEN_FILE_ID}."], glued


def test_airwallexs_words_hide_a_file_id_with_a_character_glued_after_it():
    from disputable import airwallex

    for name in ("a.pdf_1", "ab.pdf_1", "abc.pdf_1"):
        token = file_id(MADE_UP_ACCOUNT_ID, name)
        answer = airwallex.Answer(400, {"code": "validation_error", "message": f"File {token}s is not valid."}, "", "", 0,
                                  None)
        assert airwallex.problem(answer) == f"HTTP 400, validation_error: File {airwallex.A_FILE_ID} is not valid", name


def test_decode_base64_reads_standard_and_url_safe_tokens():
    name = "a~?>b.pdf"  # characters whose base64 holds + and /
    standard = b64(f"{MADE_UP_ACCOUNT_ID},|,sg,|,{name}")
    url_safe = base64.urlsafe_b64encode(f"{MADE_UP_ACCOUNT_ID},|,sg,|,{name}".encode()).decode()
    assert {"+", "/"} & set(standard) and {"-", "_"} & set(url_safe)
    assert decode_base64(standard) == decode_base64(url_safe) == decode_base64(url_safe.rstrip("="))
    assert decode_base64("ab+c-d" * 10) is None  # both alphabets at once
    assert decode_base64("abcde") is None  # one character past a whole group
    assert decode_base64("café" * 12) is None and decode_base64("not base64!") is None
    assert privacy.SEPARATOR not in decode_base64("6df49928" * 8)  # a SHA-256 is base64 too, of nothing readable


def test_account_id_of_a_made_up_file_id():
    assert account_id_of(file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1")) == MADE_UP_ACCOUNT_ID
    assert b64(f"{MADE_UP_ACCOUNT_ID},|,sg,|,one.pdf_1").endswith("=")
    assert account_id_of(b64(f"{MADE_UP_ACCOUNT_ID},|,sg,|,one.pdf_1")) == MADE_UP_ACCOUNT_ID  # padded
    url_safe = base64.urlsafe_b64encode(f"{MADE_UP_ACCOUNT_ID},|,sg,|,a~?>b.pdf".encode()).decode()
    assert account_id_of(url_safe) == MADE_UP_ACCOUNT_ID
    assert account_id_of(f" {file_id(MADE_UP_ACCOUNT_ID, 'one.pdf_1')}\n") == MADE_UP_ACCOUNT_ID
    for not_one in ("dst_sgpv8mrgwhnpdll2ndx", b64("no separator in this text at all, none"), "6df49928" * 8,
                    b64(",|,sg,|,one.pdf"), "not base64!", "", 123):
        assert account_id_of(not_one) is None, not_one


# ----------------------------------------------------------------------------- check_files
def test_the_committed_spike_recordings_pass_every_check():
    assert check_files(RECORDINGS, ALLOWED, []) == []


def _folder(tmp_path, *, readings=True):
    """A story folder as the recorder writes it: hidden file ids and request ids in the exchanges, Claude's readings."""
    login, upload, read, blocked = _exchanges()
    scrubber = Scrubber([CLIENT_ID, API_KEY, TOKEN], ALLOWED.addresses)
    scrubber.learn(account_id_of(upload["response"]["json"]["file_id"]))
    (tmp_path / "exchanges.jsonl").write_text("".join(scrubber.dumps(exchange) + "\n" for exchange in _exchanges()),
                                              encoding="utf-8", newline="\n")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "format": 1, "ending": "1", "store": "fernhill", "run": "a1b2c3d4", "recorded_at": "2026-11-10T09:12:00+00:00",
        "approver": "Priya", "readings": {"source": "claude", "model": "claude-opus-5-5", "effort": "high",
                                          "first_read": "2026-11-08", "last_read": "2026-11-08"},
        "expect": {"total": "279.30", "at_risk": "0.00", "closed": 3}}), encoding="utf-8")
    (tmp_path / "beats.json").write_text(json.dumps([{
        "n": 4, "kind": "approve", "story": "Monday 9:00", "text": "Priya approves the headphones' challenge.",
        "clock": [["approve:dst_made_up", "2026-11-10T09:14:03+00:00"]], "dispute_id": "dst_made_up",
        "plan_sha256": "6df49928" * 8, "by": "Priya"}]), encoding="utf-8")
    paths = [tmp_path / name for name in ("exchanges.jsonl", "manifest.json", "beats.json")]
    if readings:
        story = [cases.read_with(label)[0] for label in STORY]
        save_readings(tmp_path / "readings.json", ReadingsFile(Header("claude", "claude-opus-5-5", "high", "story"),
                                                               {reading.key: reading for reading in story},
                                                               {reading.key: date(2026, 11, 8) for reading in story}))
        paths.append(tmp_path / "readings.json")
    return paths, scrubber.secrets


def test_a_story_folder_as_the_recorder_writes_it_passes(tmp_path):
    paths, secrets = _folder(tmp_path)
    assert check_files(paths, ALLOWED, secrets) == []


def test_check_files_finds_each_problem_and_never_quotes_it(tmp_path):
    paths, _ = _folder(tmp_path, readings=False)
    secrets = {CLIENT_ID, API_KEY, TOKEN}  # not the account id here, so each value below is found once, as its kind
    secret_line = b64(f"{TOKEN} was sent in a header of this request, said the error page")
    exchange = {"request": {"method": "GET", "headers": {}}, "response": {"status": 200, "json": {
        "file_id": file_id(MADE_UP_ACCOUNT_ID, "one.pdf_1"), "note": f"left in: {TOKEN}", "blob": secret_line,
        "ip": "192.0.2.201", "seller_info": {"store_url": f"https://github.com/{MADE_UP_ACCOUNT_ID}"}}}}
    (tmp_path / "exchanges.jsonl").write_text(json.dumps(exchange) + "\n", encoding="utf-8")
    hidden = file_id(ACCOUNT_ID_STAND_IN, "one.pdf_1")
    (tmp_path / "readings.json").write_text(json.dumps({"readings": [{"note": hidden}]}), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"run": MADE_UP_ACCOUNT_ID}), encoding="utf-8")
    (tmp_path / "beats.bin").write_bytes(b"\xff\xfe\x00")
    folder = tmp_path.name
    problems = check_files([*paths, tmp_path / "readings.json", tmp_path / "beats.bin", tmp_path / "missing.json"],
                           ALLOWED, secrets)
    assert sorted(problems) == sorted([
        f"{folder}/exchanges.jsonl, line 1, response.json.file_id: {FOUND_OPEN_FILE_ID}.",
        f"{folder}/exchanges.jsonl, line 1, response.json.note: {FOUND_SECRET}.",
        f"{folder}/exchanges.jsonl, line 1, response.json.blob: {FOUND_SECRET_ENCODED}.",
        f"{folder}/exchanges.jsonl, line 1, response.json.ip: {FOUND_IP}.",
        f"{folder}/exchanges.jsonl, line 1, response.json.seller_info.store_url: {FOUND_STORE_URL}.",
        f"{folder}/manifest.json, run: {FOUND_UUID}.",
        f"{folder}/readings.json, readings[0].note: {FOUND_FILE_ID}.",
        f"{folder}/beats.bin: it isn't UTF-8 text.",
        f"{folder}/missing.json: it can't be read.",
    ])
    for value in (MADE_UP_ACCOUNT_ID, TOKEN, secret_line, "192.0.2.201", hidden):
        assert not [line for line in problems if value in line], value


def test_check_files_names_five_places_of_a_kind_and_counts_the_rest(tmp_path):
    path = tmp_path / "exchanges.jsonl"
    lines = [{"request": {}, "response": {"json": {"file_id": file_id(MADE_UP_ACCOUNT_ID, f"{n}.pdf_1")}}}
             for n in range(7)]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    problems = check_files([path], ALLOWED, [])
    label = f"{tmp_path.name}/exchanges.jsonl"
    assert problems == [*(f"{label}, line {n}, response.json.file_id: {FOUND_OPEN_FILE_ID}." for n in range(1, 6)),
                        f"{label}: {FOUND_OPEN_FILE_ID} in 2 more places."]


def test_check_files_refuses_a_field_a_recording_keeps_only_redacted(tmp_path):
    """The spike's own key check (test_spike_offline): a cf-ray, a cookie or the paying browser's device data is kept only
    redacted, whatever its value looks like. A scrub that missed one leaves nothing else to find."""
    device = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://disputable.invalid/device"))  # a UUID passes in an exchange
    exchange = {"request": {"method": "GET", "headers": {"Cookie": COOKIE, "authorization": REDACTED}},
                "response": {"status": 200, "headers": {"cf-ray": "a52d4c0000000000-XYZ", "set-cookie": REDACTED},
                             "json": {"latest_payment_attempt": {"device_data": {"device_id": device, "browser": "Edge"}},
                                      "token": "", "client_secret": None, "IP_Address": "198.51.100.77"}}}
    path = tmp_path / "exchanges.jsonl"
    path.write_text(json.dumps(exchange) + "\n" + json.dumps({**exchange, "request": {}}) + "\n", encoding="utf-8")
    label = f"{tmp_path.name}/exchanges.jsonl"
    problems = check_files([path], ALLOWED, [])
    assert problems == [f"{label}, line {n}, {place}: {privacy.FOUND_UNREDACTED}." for n, place in (
        (1, "request.headers.Cookie"), (1, "response.headers.cf-ray"),
        (1, "response.json.latest_payment_attempt.device_data"), (1, "response.json.IP_Address"),
        (2, "response.headers.cf-ray"))] + [f"{label}: {privacy.FOUND_UNREDACTED} in 2 more places."]
    for value in (COOKIE, "a52d4c", device, "Edge", "198.51.100.77"):  # the seed's own IP, but kept as it came
        assert not [line for line in problems if value in line], value
    readings = tmp_path / "readings.json"  # outside an exchange the rule doesn't apply: a reading holds no headers
    readings.write_text(json.dumps({"readings": [{"note": "a cookie", "token": "x"}]}), encoding="utf-8")
    assert check_files([readings], ALLOWED, []) == []


def test_reading_problems_can_hold_an_ip_to_the_scrub_rule(story_reading):
    """exact: an IP passes only when the seed holds it or it is 127.0.0.1, as check_files() checks a readings file."""
    documentation = to_json(with_note(story_reading, "The record says 192.0.2.250 there."))
    assert reading_problems(documentation, ALLOWED) == []  # a documentation address passes the eval's own check
    assert reading_problems(documentation, ALLOWED, exact=True) == [
        f"reading {story_reading.key[:8]}..., rows[0].note: {FOUND_IP}."]
    for seed in ("192.0.2.86", "127.0.0.1"):
        assert reading_problems(to_json(with_note(story_reading, f"The record says {seed} there.")), ALLOWED,
                                exact=True) == [], seed


def test_scrubbing_must_leave_even_what_no_string_holds(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"counts": {"exchanges": 4242424242}}), encoding="utf-8")
    assert check_files([path], ALLOWED, ["4242424242"]) == [f"{tmp_path.name}/manifest.json: scrubbing changes its text."]
    assert check_files([path], ALLOWED, []) == []
