"""Record and replay: the story recorded on the fake sandbox, its four files, the replay that answers from them with no
keys and no network, the story run around them (its expectations, its self-check, its readings and its spend), and the
stand-in maker, scripts/record_standin.py, which records the demo on the fake with "stand-in" in the manifest.

The recordings are tests/recordings.py's: made once a session through story.record(), each with its own self-check and
privacy check. A test that needs a run of its own (a busy read, a scrub that changes an answer) records one in its temp
folder. Offline: the fake sandbox behind httpx.MockTransport, the scripted perfect reader, NoReader.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import functools
import hashlib
import importlib.util
import inspect
import json
import re
import shutil
import socket
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import anthropic
import dotenv
import dotenv.main
import httpx
import httpx2
import pytest

import recordings
from disputable import config, privacy, prompts, recording, spend, story
from disputable.airwallex import DISPUTES, LOGIN, PAYMENTS, UPLOAD, AgentClient, Credentials, Session
from disputable.approvals import Tier
from disputable.clock import ReplayMismatch
from disputable.config import Settings, SetupError
from disputable.engine import Action
from disputable.privacy import ACCOUNT_ID_STAND_IN, REDACTED, Scrubber
from disputable.reader import WHY, Failure, ReaderBusy, failed
from disputable.readings_file import NOT_READ_LIVE, Header, ReadingsFile, load_readings, save_readings
from disputable.recording import (BEATS, EXCHANGES, FILES, MANIFEST, READINGS, RecordingError, RecordingTransport, Replay,
                                  ReplayTransport, Sink, StrictReplayReader, load, save)
from disputable.seed import scripted
from disputable.seed.fernhill import FERNHILL, FIRST_PACKAGE, MONDAY
from disputable.story import CachedReader, StoryError
from fake_airwallex import FakeAirwallex, file_id
from sandbox import Sandbox
from test_rounds import Reads, busy, request_for

EAR_TIPS_COMMENT = FERNHILL.case("ear_tips").issuer_comment
KINDS_1 = ["bank", "pass", "approve", "approve", "pass", "pass", "bank", "pass", "approve", "pass", "pass", "bank", "pass"]


def _no_network(self, request):
    raise AssertionError("The replay went to the network.")


@functools.cache
def ended(ending: str):
    """The recording of an ending replayed to its last beat once a session, with httpx.HTTPTransport switched off (each
    answer comes from the recording). Before each beat it reads the clock and the board as the screens do: peek() must
    use up no recorded moment."""
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        replay, played = recordings.replayed(recordings.recorded()[ending]), []
        for beat in replay.beats:
            for _ in range(2):
                replay.clock.peek()
                replay.service.board()
            played.append(replay.play(beat))
            assert replay.clock.left() == 0
    return replay, tuple(played)


def rows(path: Path, sql: str) -> list:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def edited(recorded: recording.Recording, change) -> recording.Recording:
    """The recording with change(lines) applied to a copy of its exchanges."""
    lines = [copy.deepcopy(dict(line)) for line in recorded.exchanges]
    change(lines)
    return dataclasses.replace(recorded, exchanges=tuple(lines))


def opened(recorded: recording.Recording) -> dict[str, str]:
    """seed case id -> dispute id, from the bank's Monday step."""
    return {day.case_id: day.dispute_id for beat in recorded.beats for day in beat.dates}


def play_until(replay: Replay, n: int) -> None:
    for beat in replay.beats[:n]:
        replay.play(beat)


# ----------------------------------------------------------------------------- the story, recorded and replayed
@pytest.mark.parametrize("ending, total", [("1", "279.30"), ("2", "1512.30")])
def test_record_then_replay(ending, total):
    done = recordings.stories()[ending]
    replay, played = ended(ending)
    assert done.total == Decimal(total) and not done.surprises
    manifest = replay.recording.manifest
    assert replay.totals() == (Decimal(total), Decimal("0.00"), 3) == (manifest.total, manifest.at_risk, manifest.closed)
    assert replay.finished() and replay.transport.left() == []
    live, again = done.database, replay.service.db.path
    for sql in ("SELECT dispute_id, version, record, why FROM case_versions ORDER BY dispute_id, version",
                "SELECT dispute_id, round_number, plan_sha256, approval FROM approvals ORDER BY id",
                "SELECT dispute_id, round_number, key, failure, reading FROM readings ORDER BY id",
                "SELECT dispute_id, round_number, plan_sha256, basis FROM bases ORDER BY id",
                "SELECT request_id, at, outcome, status, detail, not_before FROM tries ORDER BY id",
                "SELECT started_at, finished_at, problems, alerts, summary FROM passes ORDER BY id",
                "SELECT request_id, sha256, round_number, filename, pdf_sha256, size, uploaded_at FROM uploads",
                "SELECT * FROM store_payments ORDER BY payment_id", "SELECT * FROM issuer_dates ORDER BY dispute_id"):
        assert rows(live, sql) == rows(again, sql), sql
    # The bodies are the same bytes, the file ids in them carrying the stand-in for the account id.
    sent = rows(live, "SELECT request_id, path, body FROM posts ORDER BY request_id")
    assert [(rid, path, Scrubber.hide_account_id(body)) for rid, path, body in sent] == rows(
        again, "SELECT request_id, path, body FROM posts ORDER BY request_id")
    assert [step.beat.n for step in played] == list(range(1, len(replay.beats) + 1))


@pytest.mark.parametrize("ending", ["1", "2"])
def test_a_recording_on_the_fake_keeps_every_rule_a_committed_one_keeps(ending):
    """test_story_recordings' checks, on these recordings: so they can't fail later for a wrong reason."""
    folder, (replay, played) = recordings.recorded()[ending], ended(ending)
    recorded = load(folder)
    recordings.assert_it_replays_to_its_own_total(replay, played)
    recordings.assert_only_the_ear_tips_alert_on_monday(played)
    recordings.assert_the_app_responds_as_itself(recorded)
    recordings.assert_the_storys_deadlines(recorded)
    recordings.assert_todays_formats(folder)
    recordings.assert_a_story_the_demo_plays(recorded)
    recordings.assert_the_readings_are_claudes_own(recorded)
    recordings.assert_nothing_private(folder)
    recordings.assert_nothing_private(folder, recordings.fake_secrets(recordings.fakes()[ending]))


def test_the_manifest_says_what_made_the_recording():
    for ending in ("1", "2"):
        manifest = load(recordings.recorded()[ending]).manifest
        assert (manifest.ending, manifest.store, manifest.approver, manifest.source) == (
            ending, story.ENDINGS[ending].store_name, "Priya", "claude")
        assert (manifest.model, manifest.effort) == (prompts.MODEL, prompts.EFFORT)
        assert manifest.first_read == manifest.last_read == date(2026, 11, 2)
        assert manifest.formats == recording.formats()


def test_the_beats_and_the_clock_journal():
    recorded = load(recordings.recorded()["1"])
    beats, cases = recorded.beats, {dispute: case for case, dispute in opened(recorded).items()}
    assert [beat.kind for beat in beats] == KINDS_1
    assert [beat.story for beat in beats] == ["Monday 9:00", "Monday 9:00", "Monday 9:05", "Monday 9:05", "Monday 9:06",
                                              "Monday 9:07", *["Three weeks later"] * 5, "A week later", "A week later"]
    # Monday's two approvals share one story time: the demo may play its window in either order.
    assert beats[0].text == ("The bank opens three disputes: the ear tips ($12, due in 2 days), the earbuds ($189, due "
                             "in 4 days) and the headphones ($1,249, due in 6 days).")
    assert [beat.text for beat in beats if beat.kind == "approve"] == [
        "Priya approves code's draft on order #1098: accept $189.00, no refund.",
        "Priya approves the challenge on order #1109.", "Priya approves the challenge on order #1109."]
    assert beats[6].text == ("The bank turns down the headphones evidence and opens a chargeback, due in 7 days. Its "
                             "comment: \"Order #1038 has a Visa fraud report (TC40) from May; CE 3.0 not met.\"")
    assert (beats[4].text, beats[5].text, beats[11].text) == (story.SENDS, story.READS, "The bank rules for the store.")
    for beat in beats:
        labels = [label for label, _ in beat.clock]
        if beat.kind == "bank":
            assert labels == [] and (bool(beat.payments) == (beat.n == 1) == bool(beat.dates))
        elif beat.kind == "approve":
            assert labels == [f"approve:{beat.dispute_id}"] and beat.by == "Priya" and len(beat.plan_sha256) == 64
        else:
            assert labels[0] == "pass" and labels[-1] == "pass:end"
        moments = [beat.at, *(moment for _, moment in beat.clock)]
        assert moments == sorted(moments) and {moment.utcoffset() for moment in moments} == {timedelta(0)}
    assert [beat.at for beat in beats] == sorted(beat.at for beat in beats)
    # Monday's first pass decides soonest due first; the sending pass sends soonest due first, each with its moments.
    order = [opened(recorded)[case] for case in ("ear_tips", "earbuds", "headphones")]
    assert [label for label, _ in beats[1].clock] == ["pass", *(f"decide:{d}" for d in order), "pass:end"]
    assert [label for label, _ in beats[4].clock] == [
        "pass", *(f"{kind}:{d}" for d in order for kind in ("send", "post", "answer")), "pass:end"]
    assert {cases[beat.dispute_id] for beat in beats if beat.kind == "approve"} == {"earbuds", "headphones"}
    # In the replay, peek() before each beat uses up nothing: every beat still finds its moments, none is left over.
    replay, played = ended("1")
    assert [step.beat for step in played] == list(beats) and replay.transport.left() == []
    assert [step.result.key for step in played if step.result is not None] == ["approved"] * 3


def test_approvals_inside_the_window_play_in_any_order():
    replay = recordings.replayed(recordings.recorded()["1"])
    play_until(replay, 2)
    window = replay.window()
    assert [beat.n for beat in window] == [3, 4] and {beat.kind for beat in window} == {"approve"}
    later, sooner = window[1], window[0]
    record = replay.service.db.load_one(later.dispute_id)[0]
    text = recording.plan_text(record.rounds[-1].plan)
    assert replay.play(later, plan_text=text).result.key == "approved"  # as the approval page would post it
    assert [beat.n for beat in replay.window()] == [3] and replay.play(sooner).result.key == "approved"
    assert replay.window() == () and replay.played == (1, 2, 4, 3)
    with pytest.raises(ReplayMismatch, match="Step 3 was played already"):
        replay.play(sooner)
    replay.play_all()
    assert replay.totals()[0] == Decimal("279.30") and replay.transport.left() == []
    assert recording.differences(replay.state(), ended("1")[0].state()) == []


def test_a_changed_body_is_loud_and_the_loop_lets_it_out():
    recorded = load(recordings.recorded()["1"])
    headphones = opened(recorded)["headphones"]

    def another_reason(lines):
        for line in lines:
            if line["actor"] == "agent" and line["request"]["url"].endswith(f"/{headphones}/challenge"):
                line["request"]["body"]["reason"] = "OTHER_REASONS"
                return

    replay = Replay(edited(recorded, another_reason), store=FERNHILL, folder=recordings.scratch())
    play_until(replay, 4)
    with pytest.raises(ReplayMismatch) as mismatch:
        replay.play(replay.beats[4])  # the sending pass
    assert str(mismatch.value) == (f"The replay has no recorded answer for POST {DISPUTES}/{{id}}/challenge with this "
                                   "body. The code now asks something the live run didn't.")
    assert headphones not in str(mismatch.value)  # the line reaches the demo's pages: no dispute or payment id
    assert replay.played == (1, 2, 3, 4)  # the beat that stopped isn't counted as played


def test_a_call_the_recording_doesnt_hold_is_loud():
    recorded = load(recordings.recorded()["2"])
    earbuds = opened(recorded)["earbuds"]
    reads = [index for index, line in enumerate(recorded.exchanges) if line["actor"] == "agent"
             and line["request"]["method"] == "GET" and line["request"]["url"].endswith(f"/{earbuds}")]

    def one_read_less(lines):
        del lines[reads[-1]]

    replay = Replay(edited(recorded, one_read_less), store=story.STORES["ending_2"], folder=recordings.scratch())
    with pytest.raises(ReplayMismatch, match=re.escape(f"no recorded answer for GET {DISPUTES}/{{id}}. The code now asks")):
        replay.play_all()
    assert replay.played == tuple(range(1, len(replay.beats)))  # the last pass is the one that asked


def test_a_call_recorded_at_another_step_isnt_served_early():
    """Each step answers from the calls its own step made in the live run: a call the code makes at another step stops
    the replay at the step that asked, never a few steps later with another cause."""
    recorded = load(recordings.recorded()["1"])
    ear_tips = opened(recorded)["ear_tips"]
    first = next(index for index, line in enumerate(recorded.exchanges) if line["actor"] == "agent" and line["beat"] == 2
                 and line["request"]["method"] == "GET" and line["request"]["url"].endswith(f"/{ear_tips}"))

    def later(lines):
        lines[first]["beat"] = 5  # as if the live run had read the ear tips at step 5 instead

    replay = Replay(edited(recorded, later), store=FERNHILL, folder=recordings.scratch())
    replay.play(replay.beats[0])
    with pytest.raises(ReplayMismatch) as mismatch:
        replay.play(replay.beats[1])  # Monday's first pass reads the ear tips
    assert str(mismatch.value) == (f"The replay has no recorded answer for GET {DISPUTES}/{{id}}. The code now asks "
                                   "something the live run didn't.")
    assert replay.played == (1,)
    assert recording.load(recordings.recorded()["1"]).exchanges[first]["beat"] == 2  # the edit was the test's own


def test_a_replay_line_names_a_request_without_its_id():
    """The demo shows a mismatch on every page, and no page shows a payment or dispute id."""
    transport = ReplayTransport([])
    for path, shown in ((f"{PAYMENTS}/int_made_up_0001", f"{PAYMENTS}/{{id}}"), (DISPUTES, DISPUTES),
                        (f"{DISPUTES}/dst_made_up_0001/challenge", f"{DISPUTES}/{{id}}/challenge"),
                        (f"{PAYMENTS}/create", f"{PAYMENTS}/create"), (UPLOAD, UPLOAD)):
        with pytest.raises(ReplayMismatch) as mismatch:
            transport.handle_request(httpx.Request("GET", f"https://api.sandbox.airwallex.com{path}"))
        assert str(mismatch.value) == (f"The replay has no recorded answer for GET {shown}. The code now asks something "
                                       "the live run didn't."), path
    line = {"seq": 1, "beat": 0, "actor": "agent", "label": "", "at": "2026-11-02T17:00:00+00:00", "elapsed_ms": 1,
            "request": {"method": "GET", "url": f"https://api.sandbox.airwallex.com{PAYMENTS}/int_made_up_0001",
                        "headers": {}, "body": None},
            "response": {"status": "200", "headers": {}, "json": None, "text": None, "transport_error": None}}
    with pytest.raises(ReplayMismatch) as unreadable:
        ReplayTransport([line]).handle_request(httpx.Request("GET", line["request"]["url"]))
    assert str(unreadable.value) == f"The recording's answer to GET {PAYMENTS}/{{id}} can't be read."


def test_uploads_replay_by_their_bytes():
    done = recordings.stories()["1"]
    recorded = load(done.copied)
    uploads = [line for line in recorded.exchanges if httpx.URL(line["request"]["url"]).path == UPLOAD]
    assert len(uploads) == 2 and {line["actor"] for line in uploads} == {"agent"}
    shown = uploads[0]["request"]["body"]
    assert set(shown) == {"multipart"} and set(shown["multipart"]) == {"filename", "notes", "content_type", "bytes",
                                                                       "sha256", "body_sha256"}
    assert shown["multipart"]["sha256"] == hashlib.sha256(FIRST_PACKAGE.pdf()).hexdigest()  # the PDF itself isn't kept
    assert (shown["multipart"]["filename"], shown["multipart"]["notes"]) == (FIRST_PACKAGE.filename, FIRST_PACKAGE.notes)
    assert rows(done.database, "SELECT pdf_sha256, size FROM uploads ORDER BY uploaded_at")[0] == (
        shown["multipart"]["sha256"], shown["multipart"]["bytes"])

    def other_bytes(lines):
        for line in lines:
            if httpx.URL(line["request"]["url"]).path == UPLOAD:
                line["request"]["body"]["multipart"]["body_sha256"] = "0" * 64
                return

    replay = Replay(edited(recorded, other_bytes), store=FERNHILL, folder=recordings.scratch())
    play_until(replay, 4)
    with pytest.raises(ReplayMismatch, match=f"no recorded answer for POST {UPLOAD} with this body"):
        replay.play(replay.beats[4])


def test_a_missing_reading_raises_the_strict_mismatch():
    recorded = load(recordings.recorded()["1"])
    done = recordings.stories()["1"]
    earbuds = opened(recorded)["earbuds"]
    key = rows(done.database, f"SELECT key FROM readings WHERE dispute_id = '{earbuds}'")[0][0]
    readings = recorded.readings
    fewer = ReadingsFile(readings.header, {k: r for k, r in readings.readings.items() if k != key},
                         {k: d for k, d in readings.dates.items() if k != key})
    replay = Replay(dataclasses.replace(recorded, readings=fewer), store=FERNHILL, folder=recordings.scratch())
    replay.play(replay.beats[0])
    with pytest.raises(ReplayMismatch) as mismatch:
        replay.play(replay.beats[1])  # Monday's pass reads the earbuds
    assert str(mismatch.value) == "Order #1098: the recording holds no reading for this case as the code reads it now."
    assert rows(replay.service.db.path, "SELECT finished_at FROM passes") == [(None,)]  # the pass didn't finish
    assert not rows(replay.service.db.path, f"SELECT key FROM readings WHERE dispute_id = '{earbuds}'")


def test_the_strict_reader_serves_a_failed_reading_as_itself():
    request = request_for(FERNHILL, "headphones")
    kept = failed(request.key, Failure.CUT_OFF)
    reader = StrictReplayReader({kept.key: kept})
    assert reader.read(request) is kept and (reader.profile, reader.effort) == (prompts.FERNHILL, prompts.EFFORT)
    with pytest.raises(ReplayMismatch, match="^Order #1109: the recording holds no reading"):
        StrictReplayReader({}).read(request)
    nameless = dataclasses.replace(request, case=dataclasses.replace(request.case, order_number=""))
    with pytest.raises(ReplayMismatch, match="^Dispute headphones: the recording holds no reading"):
        StrictReplayReader({}).read(nameless)


# ----------------------------------------------------------------------------- what the recording keeps
@pytest.mark.parametrize("ending", ["1", "2"])
def test_a_recording_holds_nothing_private(ending):
    folder, fake = recordings.recorded()[ending], recordings.fakes()[ending]
    secrets = recordings.fake_secrets(fake)
    assert recordings.private_problems(folder, secrets) == []
    for secret in sorted(secrets):  # the account id above all: as itself, in base64, inside a file id
        assert recordings.holds(folder, secret) == [], secret
    recorded = load(folder)
    ids = recordings.file_ids(recorded)
    assert ids and {privacy.account_id_of(token) for token in ids} == {ACCOUNT_ID_STAND_IN}  # hidden, never dropped
    text = (folder / EXCHANGES).read_text(encoding="ascii")
    assert "https://github.com/<redacted>" in text and f"https://github.com/{ACCOUNT_ID_STAND_IN}" not in text
    for line in recorded.exchanges:
        for name in ("authorization", "x-api-key", "x-client-id", "cookie"):
            assert line["request"]["headers"].get(name) in (None, REDACTED)
        answer = line["response"]["json"]
        if isinstance(answer, dict):
            assert answer.get("token") in (None, REDACTED) and answer.get("client_secret") in (None, REDACTED)
    # Each file is plain ASCII with LF endings and a final LF.
    for name in FILES:
        data = (folder / name).read_bytes()
        assert data.isascii() and b"\r" not in data and data.endswith(b"\n"), name


def test_the_sink_learns_the_account_id_before_it_writes_the_upload(tmp_path):
    made_up = "11111111-2222-4333-8444-555555555555"
    token = "made-up-token-0001-abcdef"
    given = file_id("evidence.pdf", 1, made_up)

    def answer(request):
        if request.url.path == LOGIN:
            return httpx.Response(201, json={"token": token, "expires_at": "2026-11-02T17:30:00+0000"})
        if request.url.path == UPLOAD:
            return httpx.Response(201, json={"file_id": given, "filename": "evidence.pdf"})
        return httpx.Response(200, json={"id": "dst_made_up", "seller_info": {"store_url": f"https://github.com/{made_up}"},
                                         "echo": f"seen {token}", "file_ids": [given]})

    scrubber = Scrubber(["made-up-client-id", "made-up-api-key-0123"], frozenset({privacy.LOCAL}))
    sink = Sink(tmp_path / EXCHANGES, scrubber, clock=lambda: MONDAY)
    credentials = Credentials("made-up-client-id", "made-up-api-key-0123", "https://api.sandbox.airwallex.com",
                              "https://files.sandbox.airwallex.com")
    session = Session(credentials, actor="agent", transport=RecordingTransport(httpx.MockTransport(answer), sink),
                      sleep=lambda seconds: None)
    client = AgentClient(session)
    assert client.upload(FIRST_PACKAGE).file_id == given  # the session gets the answer as it came
    assert client.dispute("dst_made_up").ok
    lines = sink.lines()
    assert [line["seq"] for line in lines] == [1, 2, 3] and [line["label"] for line in lines] == [
        "login", f"upload-{FIRST_PACKAGE.sha256[:16]}", "dispute-dst_made_up"]
    assert {made_up, token} <= scrubber.secrets  # learned from the answers, before their lines were written
    upload, read = lines[1], lines[2]
    assert privacy.account_id_of(upload["response"]["json"]["file_id"]) == ACCOUNT_ID_STAND_IN
    assert read["response"]["json"]["seller_info"]["store_url"] == f"https://github.com/{REDACTED}"
    assert read["response"]["json"]["echo"] == f"seen {REDACTED}" and lines[0]["response"]["json"]["token"] == REDACTED
    text = (tmp_path / EXCHANGES).read_text(encoding="ascii")
    assert made_up not in text and token not in text and "made-up-api-key" not in text
    assert not [found for found in privacy.base64_tokens(text) if made_up in (privacy.decode_base64(found) or "")]
    assert privacy.check_files([tmp_path / EXCHANGES], story.allowed(), scrubber.secrets) == []


def test_the_recorder_redacts_each_answers_cf_ray_cookies_and_device_data(tmp_path):
    """The files host answers with a cf-ray and a cookie, and a payment carries the paying browser's device data
    (fixtures/spike/031, 039 and 064; 003, 005 and 007). Every value here is made up."""
    ray, cookie = "0123456789abcdef-XYZ", "__cf_bm=made-up-cookie-4242; path=/; HttpOnly"
    device = {"device_id": "0d1e2f3a-0000-4000-8000-00000000c0de", "ip_address": "192.0.2.10", "browser": "Edge"}
    headers = {"cf-ray": ray, "set-cookie": cookie}

    def answer(request):
        if request.url.path == LOGIN:
            return httpx.Response(201, json={"token": "made-up-token-0003-abcdef"}, headers=headers)
        if request.url.path == UPLOAD:
            return httpx.Response(201, json={"file_id": file_id("evidence.pdf", 1), "filename": "evidence.pdf"},
                                  headers=headers)
        return httpx.Response(200, json={"id": "int_made_up", "latest_payment_attempt": {"device_data": device}},
                              headers={"CF-Ray": ray})

    sink = Sink(tmp_path / EXCHANGES, Scrubber(["made-up-client-id", "made-up-api-key-0123"], frozenset({privacy.LOCAL})),
                clock=lambda: MONDAY)
    credentials = Credentials("made-up-client-id", "made-up-api-key-0123", "https://api.sandbox.airwallex.com",
                              "https://files.sandbox.airwallex.com")
    client = AgentClient(Session(credentials, actor="agent", transport=RecordingTransport(httpx.MockTransport(answer),
                                                                                           sink), sleep=lambda s: None))
    assert client.upload(FIRST_PACKAGE).file_id and client.payment("int_made_up").data["latest_payment_attempt"]
    lines = sink.lines()
    assert [line["response"]["status"] for line in lines] == [201, 201, 200]
    for line in lines:
        shown = {name.lower(): value for name, value in line["response"]["headers"].items()}
        assert shown["cf-ray"] == REDACTED and shown.get("set-cookie", REDACTED) == REDACTED
    assert lines[2]["response"]["json"]["latest_payment_attempt"]["device_data"] == REDACTED
    text = (tmp_path / EXCHANGES).read_text(encoding="ascii")
    for value in (ray, "made-up-cookie-4242", device["device_id"], device["ip_address"], "Edge"):
        assert value not in text, value
    assert recordings.unredacted(lines) == []  # the key check test_story_recordings runs on the committed folders


def test_the_key_check_finds_a_field_kept_as_it_came():
    lines = [{"request": {"headers": {"Authorization": REDACTED}}, "response": {"headers": {"cf-ray": "made-up-SIN"},
                                                                                 "json": {"token": "", "items": [
                                                                                     {"device_data": {"x": 1}}]}}}]
    assert recordings.unredacted(lines) == ["line 1: response.headers.cf-ray", "line 1: response.json.items[0].device_data"]


def test_a_transport_error_is_recorded_and_raised_again_in_the_replay(tmp_path):
    answers = iter([httpx.ReadTimeout("No answer."), {"id": "dst_made_up", "status": "REQUIRES_RESPONSE"}])

    def answer(request):
        if request.url.path == LOGIN:
            return httpx.Response(201, json={"token": "made-up-token-0002-abcdef"})
        found = next(answers)
        if isinstance(found, Exception):
            raise found
        return httpx.Response(200, json=found)

    sink = Sink(tmp_path / EXCHANGES, Scrubber([], frozenset()), clock=lambda: MONDAY)
    credentials = Credentials("made-up-client-id", "made-up-api-key-0123", "https://api.sandbox.airwallex.com",
                              "https://files.sandbox.airwallex.com")
    live = AgentClient(Session(credentials, actor="agent", transport=RecordingTransport(httpx.MockTransport(answer), sink),
                               sleep=lambda seconds: None))
    assert live.dispute("dst_made_up").data["status"] == "REQUIRES_RESPONSE"  # the GET's one retry
    lines = sink.lines()
    assert [(line["response"]["status"], line["response"]["transport_error"]) for line in lines] == [
        (201, None), (None, "ReadTimeout"), (200, None)]
    transport = ReplayTransport(lines)
    assert transport.left() == [f"GET {DISPUTES}/dst_made_up", f"GET {DISPUTES}/dst_made_up"]
    stand_in = Credentials("replay", "replay", "https://api.sandbox.airwallex.com", "https://files.sandbox.airwallex.com")
    replayed = Session(stand_in, actor="agent", transport=transport, sleep=lambda seconds: None)
    assert AgentClient(replayed).dispute("dst_made_up").data["status"] == "REQUIRES_RESPONSE"
    assert transport.left() == []
    read = httpx.Request("GET", f"https://api.sandbox.airwallex.com{DISPUTES}/dst_made_up")
    with pytest.raises(httpx.ReadTimeout):  # raised as its own class, with no response
        ReplayTransport(lines).handle_request(read)
    again = ReplayTransport(lines)
    for _ in range(3):  # a login is answered every time, and isn't left over
        assert again.handle_request(httpx.Request("POST", f"https://api.sandbox.airwallex.com{LOGIN}")).status_code == 201
    with pytest.raises(ReplayMismatch, match=re.escape("no recorded answer for GET /api/v1/pa/payment_intents/{id}.")):
        again.handle_request(httpx.Request("GET", "https://api.sandbox.airwallex.com/api/v1/pa/payment_intents/x"))
    with pytest.raises(ReplayMismatch, match="no login that worked"):
        ReplayTransport(lines[1:]).handle_request(httpx.Request("POST", f"https://api.sandbox.airwallex.com{LOGIN}"))


# ----------------------------------------------------------------------------- the folder
def test_save_writes_back_what_load_read(tmp_path):
    folder = recordings.recorded()["1"]
    recorded = load(folder)
    save(recorded, tmp_path / "again")
    for name in FILES:
        assert (tmp_path / "again" / name).read_bytes() == (folder / name).read_bytes(), name
    assert load(tmp_path / "again") == recorded
    recording.copy(folder, tmp_path / "copied")
    assert sorted(path.name for path in (tmp_path / "copied").iterdir()) == sorted(FILES)


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "ending-1"
    recording.copy(recordings.recorded()["1"], target)
    return target


def _edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="ascii"))
    change(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii", newline="\n")


def test_load_refuses_what_it_cant_replay_and_names_no_value(tmp_path):
    folder = _copy(tmp_path)
    run = load(folder).manifest.run
    (folder / BEATS).rename(folder / "beats.old")
    with pytest.raises(RecordingError) as missing:
        load(folder)
    assert str(missing.value) == f"The recording in {folder} can't be replayed. It has no {BEATS}."
    (folder / "beats.old").rename(folder / BEATS)
    _edit_json(folder / MANIFEST, lambda data: data["formats"].update(reader=2, prompt="reader-0"))
    with pytest.raises(RecordingError) as stale:
        load(folder)
    assert str(stale.value) == (f"The recording in {folder} can't be replayed. This recording was made by other code: "
                                "prompt, reader changed since. Record the story again.")  # which ending: the folder says
    _edit_json(folder / MANIFEST, lambda data: data.update(format=2))
    with pytest.raises(RecordingError) as other:
        load(folder)
    assert str(other.value) == (f"The recording in {folder} can't be replayed. This recording is format 2, and this code "
                                "reads format 1. Record the story again.")
    folder = _copy(tmp_path)
    lines = (folder / EXCHANGES).read_text(encoding="ascii").splitlines(keepends=True)
    (folder / EXCHANGES).write_text("".join(lines[:-1]), encoding="ascii", newline="\n")
    with pytest.raises(RecordingError) as counts:
        load(folder)
    assert str(counts.value) == (f"The recording in {folder} can't be replayed. The recording's files don't match its "
                                 "manifest. Record the story again.")
    folder = _copy(tmp_path)
    beats = json.loads((folder / BEATS).read_text(encoding="ascii"))
    beats[2]["kind"] = "dance"
    (folder / BEATS).write_text(json.dumps(beats), encoding="ascii")
    with pytest.raises(RecordingError) as bad:
        load(folder)
    assert str(bad.value) == (f"The recording in {folder} can't be replayed. {BEATS} in the recording doesn't hold what a "
                              "recording holds (step 3).")
    folder = _copy(tmp_path)
    (folder / EXCHANGES).write_text("{not json\n", encoding="ascii")
    with pytest.raises(RecordingError, match="exchanges.jsonl, line 1, in the recording isn't valid JSON"):
        load(folder)
    folder = _copy(tmp_path)
    line = json.loads((folder / EXCHANGES).read_text(encoding="ascii").splitlines()[0])
    (folder / EXCHANGES).write_text(json.dumps({**line, "beat": "2"}) + "\n", encoding="ascii")  # a step is a number
    with pytest.raises(RecordingError, match=re.escape(f"{EXCHANGES} in the recording doesn't hold what a recording holds "
                                                       "(line 1).")):
        load(folder)
    assert run not in str(bad.value) + str(stale.value)


def test_a_stand_in_that_other_code_made_says_to_make_the_stand_in_again(tmp_path):
    """No story run made it, so the line names its maker's command, never "Record the story again": that needs the
    sandbox, which can't take a challenge while the stand-in is used. The maker stops without the pilot's Claude
    readings, so the line names --no-claude too."""
    again = ("Make the stand-in again: uv run python scripts/record_standin.py (add --no-claude if it says the Claude "
             "readings aren't there).")
    assert recording.AGAIN_STANDIN == again
    assert recordings.maker().NOT_THERE.startswith("The Claude readings for the story aren't there")  # what it says then
    folder = tmp_path / "ending-1"
    recording.copy(recordings.standins().folders()["1"], folder)
    _edit_json(folder / MANIFEST, lambda data: data["formats"].update(reader=2, prompt="reader-0"))
    with pytest.raises(RecordingError) as stale:
        load(folder)
    assert str(stale.value) == (f"The recording in {folder} can't be replayed. This recording was made by other code: "
                                f"prompt, reader changed since. {again}")
    _edit_json(folder / MANIFEST, lambda data: data.update(format=2))
    with pytest.raises(RecordingError) as other:
        load(folder)
    assert str(other.value) == (f"The recording in {folder} can't be replayed. This recording is format 2, and this code "
                                f"reads format 1. {again}")
    _edit_json(folder / MANIFEST, lambda data: data.pop("airwallex"))  # one that doesn't say the stand-in made it
    with pytest.raises(RecordingError, match=re.escape("reads format 1. Record the story again.")):
        load(folder)
    folder = tmp_path / "ending-2"
    recording.copy(recordings.standins().folders()["2"], folder)
    lines = (folder / EXCHANGES).read_text(encoding="ascii").splitlines(keepends=True)
    (folder / EXCHANGES).write_text("".join(lines[:-1]), encoding="ascii", newline="\n")
    with pytest.raises(RecordingError) as counts:
        load(folder)
    assert str(counts.value) == (f"The recording in {folder} can't be replayed. The recording's files don't match its "
                                 f"manifest. {again}")
    made = load(recordings.standins().folders()["2"])
    with pytest.raises(RecordingError) as unsaved:  # save() holds a manifest to its files the same way
        save(dataclasses.replace(made, manifest=dataclasses.replace(made.manifest, beats=made.manifest.beats + 1)),
             tmp_path / "unsaved")
    assert str(unsaved.value) == f"The recording's files don't match its manifest. {again}"
    assert not (tmp_path / "unsaved").exists()


def test_the_manifest_says_what_answered_for_airwallex(tmp_path):
    """"sandbox" or "stand-in": save() writes it and load() reads it back. Missing or of another kind, load() refuses
    the recording with a line that names the field, never the value, and save() writes no such manifest."""
    folder = _copy(tmp_path)
    recorded = load(folder)
    assert recorded.manifest.airwallex == "sandbox"  # record_on_fake()'s: these stand for disputable story's runs
    written = json.loads((folder / MANIFEST).read_text(encoding="ascii"))
    assert list(written)[4:7] == ["recorded_at", "airwallex", "approver"] and written["airwallex"] == "sandbox"
    stand_in = dataclasses.replace(recorded, manifest=dataclasses.replace(recorded.manifest, airwallex="stand-in"))
    save(stand_in, tmp_path / "stand-in")
    assert load(tmp_path / "stand-in") == stand_in
    assert json.loads((tmp_path / "stand-in" / MANIFEST).read_text(encoding="ascii"))["airwallex"] == "stand-in"
    refused = (f"The recording in {folder} can't be replayed. {MANIFEST} in the recording doesn't hold what a recording "
               "holds (airwallex).")
    for wrong in (None, "", "Sandbox", "stand_in", "standin", "live", 1, ["sandbox"], {"kind": "stand-in"}):
        _edit_json(folder / MANIFEST, lambda data: data.update(airwallex=wrong))
        with pytest.raises(RecordingError) as other:
            load(folder)
        assert str(other.value) == refused, wrong
    _edit_json(folder / MANIFEST, lambda data: data.pop("airwallex"))
    with pytest.raises(RecordingError) as missing:
        load(folder)
    assert str(missing.value) == refused
    live = dataclasses.replace(recorded, manifest=dataclasses.replace(recorded.manifest, airwallex="live"))
    with pytest.raises(RecordingError, match=re.escape(f"{MANIFEST} in the recording doesn't hold what a recording holds "
                                                       "(airwallex).")):
        save(live, tmp_path / "live")
    assert not (tmp_path / "live").exists()


def test_the_story_run_says_what_answered_for_airwallex(tmp_path):
    """disputable story runs on the sandbox, so record() says "sandbox" unless told "stand-in", and nothing else."""
    assert inspect.signature(story.record).parameters["airwallex"].default == "sandbox"
    with pytest.raises(ValueError, match="airwallex is sandbox or stand-in"):
        recordings.record_on_fake("1", "none", root=tmp_path, airwallex="live")
    assert not (tmp_path / "data").exists()  # refused before the run made anything


def test_a_readings_file_of_another_source_is_refused(tmp_path):
    folder = _copy(tmp_path)
    keys = sorted(load(folder).readings.readings)
    none = ReadingsFile(Header("none", "", "", "story"), {key: failed(key, Failure.NO_KEY) for key in keys},
                        {key: date(2026, 11, 2) for key in keys})
    save_readings(folder / READINGS, none)
    with pytest.raises(RecordingError) as refused:
        load(folder)
    assert str(refused.value) == (f"The recording in {folder} can't be replayed. readings.json in the recording: "
                                  "readings.json doesn't hold readings Claude made live, so it can't be used here.")
    folder = _copy(tmp_path)
    _edit_json(folder / MANIFEST, lambda data: data["readings"].update(source="none", model="", effort=""))
    with pytest.raises(RecordingError, match="holds Claude's readings, and this needs the readings made with no key"):
        load(folder)


def test_copy_retries_while_another_program_holds_a_file(tmp_path, monkeypatch):
    held, real = [3], recording.os.replace

    def busy_replace(source, target):
        if held[0]:
            held[0] -= 1
            raise PermissionError("held")
        real(source, target)

    monkeypatch.setattr(recording.os, "replace", busy_replace)
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)
    recording.copy(recordings.recorded()["1"], tmp_path / "copied")
    assert sorted(path.name for path in (tmp_path / "copied").iterdir()) == sorted(FILES)
    held[0] = 100
    with pytest.raises(RecordingError) as stopped:  # a plain message, with no traceback
        recording.copy(recordings.recorded()["1"], tmp_path / "held")
    assert str(stopped.value) == recording.HELD.format(name=MANIFEST, to=tmp_path / "held",
                                                       folder=recordings.recorded()["1"])
    assert not list((tmp_path / "held").iterdir())  # nothing left, no .tmp for git to see


def test_a_copy_that_another_program_stops_changes_nothing(tmp_path, monkeypatch):
    """All or nothing: when OneDrive holds one file past the retries, the files already replaced are put back, so the
    folder never mixes two runs."""
    target, new = tmp_path / "ending-1", recordings.recorded()["1"]
    recording.copy(recordings.recorded()["2"], target)  # another recording is there already
    before = {name: (target / name).read_bytes() for name in FILES}
    real = recording.os.replace

    def held(source, to):
        if Path(to).name == EXCHANGES:
            raise PermissionError("OneDrive holds it")
        real(source, to)

    monkeypatch.setattr(recording.os, "replace", held)
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)
    with pytest.raises(RecordingError) as stopped:
        recording.copy(new, target)
    assert str(stopped.value) == (f"Another program, such as OneDrive, held {EXCHANGES}, so nothing in {target} changed. "
                                  f"The checked recording is in {new}: copy its four files there, or run the story "
                                  "again.")
    assert {name: (target / name).read_bytes() for name in FILES} == before
    assert sorted(path.name for path in target.iterdir()) == sorted(FILES)  # and no .tmp left
    load(target)  # the old recording, whole: it still loads

    def put_back_held_too(source, to):
        if Path(to).name in (EXCHANGES, MANIFEST) and Path(source).name.endswith(".tmp"):
            raise PermissionError("OneDrive holds it")
        real(source, to)

    def manifest_first(source, to):  # the manifest goes in, then exchanges.jsonl is held, then so is the manifest
        if Path(to).name == MANIFEST and not manifest_first.done:
            manifest_first.done = True
            return real(source, to)
        return put_back_held_too(source, to)

    manifest_first.done = False
    monkeypatch.setattr(recording.os, "replace", manifest_first)
    with pytest.raises(RecordingError) as mixed:
        recording.copy(new, target)
    assert str(mixed.value) == (f"Another program, such as OneDrive, held {EXCHANGES}, so {target} may now hold files of "
                                f"two runs. The checked recording is in {new}: copy its four files there, or run the "
                                "story again.")
    assert not list(target.glob("*.tmp"))


def test_a_beat_holds_no_request_id_and_checks_itself():
    names = {field.name for field in dataclasses.fields(recording.Beat)}
    assert names == {"n", "kind", "story", "at", "text", "clock", "dispute_id", "plan_sha256", "by", "payments", "dates"}
    at = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
    for wrong in ({"kind": "dance"}, {"n": 0}, {"clock": (("approve", at),)}, {"dispute_id": "dst_x"},
                  {"kind": "approve", "dispute_id": "dst_x", "by": "Priya", "plan_sha256": "short"},
                  {"at": datetime(2026, 11, 2, 17, 0)}):
        with pytest.raises(ValueError):
            recording.Beat(**{"n": 1, "kind": "pass", "story": "Monday 9:00", "at": at, "text": "A step.",
                              "clock": (), **wrong})


# ----------------------------------------------------------------------------- the story run
def test_a_run_with_a_busy_read_is_not_written(tmp_path):
    with pytest.raises(StoryError) as stopped:
        recordings.record_on_fake("1", Reads(busy), root=tmp_path, source="claude")
    assert str(stopped.value) == story.BUSY
    assert not (tmp_path / "ending-1").exists() and len(list((tmp_path / "data").glob("story-*/live.sqlite3"))) == 1


class Leaky(FakeAirwallex):
    """The fake sandbox with an IP address the seed doesn't hold in the ear tips' comment, as an error page might show
    the caller's: the recording scrubs it, so its answer isn't the one the live run read."""

    def _open_dispute(self, request):
        answer = super()._open_dispute(request)
        for dispute in self.sandbox.disputes.values():
            if dispute["issuer_comment"] == EAR_TIPS_COMMENT:
                dispute["issuer_comment"] = f"{EAR_TIPS_COMMENT} Seen from 203.0.113.250."
        return answer


def test_the_self_check_catches_an_answer_the_scrub_changed(tmp_path):
    with pytest.raises(StoryError) as stopped:
        recordings.record_on_fake("1", root=tmp_path, fake=Leaky(Sandbox(FERNHILL)))
    assert str(stopped.value) == ("The recording doesn't replay as the live run went: after step 2, Order #1121's saved "
                                  "record differs. Nothing was copied.")
    assert not (tmp_path / "ending-1").exists()
    staged = next((tmp_path / "data").glob("story-*/recording"))
    assert recordings.private_problems(staged) == []  # the scrub did its job; the self-check caught what it changed
    assert "203.0.113.250" not in (staged / EXCHANGES).read_text(encoding="ascii")


def test_the_self_check_compares_every_approval_row():
    replay, _ = ended("1")
    state = replay.state()
    headphones = opened(replay.recording)["headphones"]
    first, *rest = state.approvals[headphones]
    later = dataclasses.replace(first, at=first.at + timedelta(seconds=1))  # the same record, another approval moment
    changed = dataclasses.replace(state, approvals=MappingProxyType({**state.approvals, headphones: (later, *rest)}))
    assert recording.differences(state, changed) == ["the approvals of Order #1109"]
    assert recording.differences(state, state) == []


def test_the_self_check_refuses_a_recorded_call_the_replay_never_made(tmp_path):
    real = recording.Sink.lines

    def one_more(sink):  # the live run's log holds one agent read the code doesn't make again
        lines = real(sink)
        extra = next(line for line in reversed(lines) if line["actor"] == "agent" and line["request"]["method"] == "GET")
        return (*lines, {**extra, "seq": len(lines) + 1})

    with mock.patch.object(recording.Sink, "lines", one_more):
        with pytest.raises(StoryError) as stopped:
            recordings.record_on_fake("1", root=tmp_path)
    assert str(stopped.value).startswith("The replay never made 1 of the recorded agent calls, the first GET /api/v1/")
    assert str(stopped.value).endswith(" Nothing was copied.") and not (tmp_path / "ending-1").exists()


def test_a_plan_the_story_doesnt_expect_stops_the_run_unless_lenient(tmp_path):
    """And with --no-claude the run records NoReader's readings: Ending 2 then costs $1,528.30."""
    no_key = story.ENDINGS["2"].no_claude
    surprise = dataclasses.replace(story.ENDINGS["2"], no_claude=(
        *no_key[:2], dataclasses.replace(no_key[2], tier=Tier.ONE_APPROVAL), no_key[3]))
    with mock.patch.dict(story.ENDINGS, {"2": surprise}):
        with pytest.raises(StoryError) as stopped:
            recordings.record_on_fake("2", "none", root=tmp_path / "strict")
        assert str(stopped.value).startswith(
            "Order #1109, round 1: the plan isn't what the story expects. It is a hand-off, with code's draft: a "
            "challenge with 1 file, and the story expects a challenge with 1 file, for one approval. Look at it on the "
            "screens (disputable serve --data ")
        assert not (tmp_path / "strict" / "ending-2").exists()
        code, said, _ = _run("2", tmp_path / "lenient", store=story.STORES["ending_2"], claude=False, lenient=True,
                             project=tmp_path / "empty", ask=_never, client=_never)
    surprised = str(stopped.value).split(" Look at it")[0]
    assert code == 0 and surprised in said and not [line for line in said if "live reads" in line]
    made = load(tmp_path / "lenient" / "out" / "ending-2")
    assert (made.manifest.source, made.manifest.model, made.manifest.effort) == ("none", "", "")
    assert made.manifest.total == Decimal("1528.30") and len(made.readings.readings) == 3  # the real total, kept
    assert {reading.failure for reading in made.readings.readings.values()} == {Failure.NO_KEY}


class FailsOn:
    """The scripted perfect reader, except that one order's read fails as given."""

    def __init__(self, order: str, failure: Failure) -> None:
        self.inner, self.order, self.failure = recordings.reader_for("perfect")[0], order, failure

    def read(self, request):
        if request.case.order_number == self.order:
            return failed(request.key, self.failure)
        return self.inner.read(request)


def test_a_failed_claude_read_stops_the_run(tmp_path):
    """The earbuds' plan is code's hand-off whether Claude read them or not, so only this check sees a read that failed:
    the recording would show code's hand-off as Claude's reading."""
    with pytest.raises(StoryError) as stopped:
        recordings.record_on_fake("1", FailsOn("1098", Failure.ACCOUNT), root=tmp_path, source="claude")
    assert str(stopped.value) == (
        "Order #1098: Claude's read failed (the Claude API account can't take more requests: a spend limit, billing or "
        "a hold), so this recording wouldn't show Claude's own reading. Run the story again: the readings made so far "
        "are reused at no cost. To record the failed read as it is, run the story with --lenient. Nothing was copied.")
    assert not (tmp_path / "ending-1").exists()


@pytest.mark.parametrize("failure", [Failure.ERROR, Failure.TOO_SLOW, Failure.CUT_OFF, Failure.BAD_REQUEST])
def test_lenient_records_a_failed_claude_read_as_a_surprise(tmp_path, failure):
    line = (f"Order #1098: Claude's read failed ({WHY[failure]}), so this recording wouldn't show Claude's own "
            "reading.")
    with pytest.raises(StoryError, match=re.escape(line)):
        recordings.record_on_fake("1", FailsOn("1098", failure), root=tmp_path / "strict", source="claude")
    said = []
    done = recordings.record_on_fake("1", FailsOn("1098", failure), root=tmp_path / "lenient", source="claude",
                                     lenient=True, say=said.append)
    assert done.surprises == (line,) and line in said and done.total == Decimal("279.30")
    made = load(done.copied)
    assert [reading.failure for reading in made.readings.readings.values() if reading.failure] == [failure]
    with pytest.raises(AssertionError):  # a committed recording may hold none: the banner says Claude read the cases
        recordings.assert_the_readings_are_claudes_own(made)


def test_a_read_the_runs_own_cap_stopped_stops_the_run_even_when_lenient(tmp_path):
    """A capped read holds no answer of Claude's, so no claude readings file may hold it (readings_file)."""
    with pytest.raises(StoryError) as stopped:
        recordings.record_on_fake("1", FailsOn("1109", Failure.CAPPED), root=tmp_path, source="claude", lenient=True)
    assert str(stopped.value) == (
        f"Order #1109: Claude's read failed ({WHY[Failure.CAPPED]}), so this recording wouldn't show Claude's own "
        "reading. Run the story again with a higher --cap: the readings made so far are reused at no cost. Nothing was "
        "copied.")
    assert not (tmp_path / "ending-1").exists()


def test_the_endings_expect_what_each_reader_gives():
    one, two = story.ENDINGS["1"], story.ENDINGS["2"]
    assert (one.expected_total(True), one.expected_total(False)) == (Decimal("279.30"), Decimal("279.30"))
    assert (two.expected_total(True), two.expected_total(False)) == (Decimal("1512.30"), Decimal("1528.30"))
    for ending in (one, two):
        read = [expect for expect in ending.no_claude if expect.case_id != "ear_tips"]
        assert {expect.tier for expect in read} == {Tier.HAND_OFF}  # no key: every read round goes to a person
        assert ending.expects(True)[0] == ending.expects(False)[0] == story.Expect("ear_tips", 1, Action.ACCEPT,
                                                                                    Tier.ALONE)
    assert story.story_keys(one) == frozenset(load(recordings.recorded()["1"]).readings.readings)
    assert story.story_keys(two) == frozenset(load(recordings.recorded()["2"]).readings.readings)


# ----------------------------------------------------------------------------- readings for the run
class Live:
    """A live reader stand-in: it answers each request with a reading of its own and counts the calls."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def read(self, request):
        self.requests.append(request)
        return self.answer(request)


def _readings():
    return load(recordings.recorded()["1"]).readings


def story_key(label: str) -> str:
    """The reading key of one of the story's reads, as the eval and the story run compute it."""
    from disputable.evals import sites

    found = {site.label: site for site in sites.read_sites()}
    return sites.request_for(found[label]).key


def private(reading):
    """The reading with an IP the seed doesn't hold in Claude's draft note: a documentation address, so the eval's
    reading check passes it, but scrubbing with the seed's allow-list would change it, so no recording may hold it."""
    return dataclasses.replace(reading, draft=dataclasses.replace(reading.draft, note=f"{reading.draft.note} 192.0.2.250"))


class Noting:
    """The scripted perfect reader, except that one order's draft note quotes an IP the seed doesn't hold."""

    def __init__(self, order: str) -> None:
        self.inner, self.order = recordings.reader_for("perfect")[0], order

    def read(self, request):
        reading = self.inner.read(request)
        return private(reading) if request.case.order_number == self.order else reading


def test_the_cached_reader_reuses_a_kept_reading_and_caches_a_live_one_at_once(tmp_path):
    kept = _readings()
    request = request_for(FERNHILL, "headphones")
    known, *others = sorted(key for key in kept.readings if key != request.key)
    fresh = dataclasses.replace(kept.readings[known], key=request.key)
    live = Live(lambda asked: fresh)
    cache, high = tmp_path / story.CACHE, tmp_path / "high.json"
    reader = CachedReader(live, {known: story.Kept(kept.readings[known], date(2026, 11, 1), high)}, cache=cache, limit=1,
                          today=lambda: date(2026, 11, 3))
    reused = reader.read(dataclasses.replace(request, key=known))
    assert reused == kept.readings[known] and not live.requests and reader.made(known) == date(2026, 11, 1)
    assert reader.source(known) == high  # where a later run finds it again
    assert reader.read(request) is fresh and len(live.requests) == 1 and reader.reads == 1
    written = load_readings(cache, want="claude")  # at once, before the run goes on
    assert written.header == Header("claude", prompts.MODEL, prompts.EFFORT, "cache")
    assert dict(written.readings) == {request.key: fresh} and written.dates[request.key] == date(2026, 11, 3)
    assert reader.source(request.key) == cache and reader.refused == []
    assert reader.read(request) is fresh and len(live.requests) == 1  # the same key again costs nothing
    other = dataclasses.replace(request, key=others[0])
    with pytest.raises(ReaderBusy):  # past its limit it reads nothing, and the run stops
        reader.read(other)
    assert reader.over_limit and len(live.requests) == 1


def test_a_failed_live_reading_isnt_cached_and_a_cache_of_another_model_starts_again(tmp_path):
    request = request_for(FERNHILL, "headphones")
    cache = tmp_path / story.CACHE
    old = _readings()
    save_readings(cache, ReadingsFile(Header("claude", "claude-opus-5", "high", "cache"), old.readings, old.dates))
    reader = CachedReader(Live(lambda asked: failed(asked.key, Failure.CUT_OFF)), {}, cache=cache, limit=2,
                          today=lambda: date(2026, 11, 3))
    assert reader.read(request).failure is Failure.CUT_OFF
    assert load_readings(cache).header.model == "claude-opus-5"  # a failed read is read again next time
    good = dataclasses.replace(old.readings[sorted(old.readings)[0]], key=request.key)
    reader = CachedReader(Live(lambda asked: good), {}, cache=cache, limit=1, today=lambda: date(2026, 11, 3))
    reader.read(request)
    again = load_readings(cache)
    assert again.header.model == prompts.MODEL and set(again.readings) == {request.key}


def test_recorded_readings_are_claudes_own_from_every_file(tmp_path):
    project, data = tmp_path / "project", tmp_path / "data"
    kept = _readings()
    first, second, third = sorted(kept.readings)
    def file(keys, source="claude", made_by="story", day=date(2026, 11, 2)):
        header = Header(source, prompts.MODEL if source == "claude" else "", prompts.EFFORT if source == "claude" else "",
                        made_by)
        readings = {key: kept.readings[key] if source == "claude" else failed(key, Failure.NO_KEY) for key in keys}
        return ReadingsFile(header, readings, {key: day for key in keys})
    save_readings(project / "fixtures" / "readings" / "high.json", file([first], made_by="eval", day=date(2026, 11, 1)))
    save_readings(project / "fixtures" / "story" / "ending-1" / READINGS, file([first, second]))
    save_readings(project / "fixtures" / "story" / "ending-2" / READINGS, file([third], source="none"))
    failing = ReadingsFile(Header("claude", prompts.MODEL, prompts.EFFORT, "cache"), {third: failed(third, Failure.REFUSED)},
                           {third: date(2026, 11, 3)})
    save_readings(data / story.CACHE, failing)
    (project / "fixtures" / "story" / "ending-3").mkdir(parents=True)
    (project / "fixtures" / "story" / "ending-3" / READINGS).write_text("{not json", encoding="ascii")
    said = []
    found = story.recorded_readings(data=data, project=project, say=said.append)
    assert set(found) == {first, second}
    assert found[first] == story.Kept(kept.readings[first], date(2026, 11, 1), project / "fixtures" / "readings" /
                                      "high.json")  # the first file that holds a key gives it
    assert found[second].path == project / "fixtures" / "story" / "ending-1" / READINGS
    assert said == ["readings.json can't be read, so its readings aren't reused: readings.json isn't valid JSON."]


def test_a_live_reading_the_repo_cant_hold_isnt_kept_and_stops_the_run(tmp_path):
    """The privacy check would refuse it at the end, after every payment: the run stops right after the read, and the
    cache keeps only what a recording may hold."""
    cache = tmp_path / "data" / story.CACHE
    reader = CachedReader(Noting("1109"), {}, cache=cache, limit=3, today=lambda: date(2026, 11, 2))
    with pytest.raises(StoryError) as stopped:
        recordings.record_on_fake("1", reader, root=tmp_path, source="claude")
    assert str(stopped.value) == "\n".join([
        "Claude's reading of Order #1109 holds what can't go into the public repo, so the run stopped:",
        f"reading {story_key('headphones')[:8]}..., draft.note: an IP address the seed doesn't hold.",
        "That reading wasn't kept, so the next run reads the case again and asks first. Nothing was copied."])
    assert not (tmp_path / "ending-1").exists()
    assert set(load_readings(cache, want="claude").readings) == {story_key("earbuds")}  # read the same pass, and fine


def test_a_kept_reading_the_repo_cant_hold_isnt_used_again(tmp_path):
    kept, headphones = _readings(), story_key("headphones")
    readings = {key: private(reading) if key == headphones else reading for key, reading in kept.readings.items()}
    save_readings(tmp_path / "data" / story.CACHE, ReadingsFile(Header("claude", prompts.MODEL, prompts.EFFORT, "cache"),
                                                                readings, dict(kept.dates)))
    asked = []
    code, said, fake = _run("1", tmp_path, project=tmp_path / "empty", settings=_settings(tmp_path / "data",
                                                                                          claude="sk-made-up"),
                            cap=Decimal(3), client=_never, ask=lambda question: asked.append(question) or "no")
    assert code == 1 and asked == ["Type yes to go on: "] and not fake.calls  # it asks first, and nothing was paid
    assert said[:2] == [
        f"A reading in {story.CACHE} can't go into the public repo, so it isn't used again: reading {headphones[:8]}..., "
        "draft.note: an IP address the seed doesn't hold.",
        "The story run of Ending 1 reads 1 case live with Claude (claude-opus-5-5, effort high), with your key."]


@pytest.mark.parametrize("made_by", ["eval", "cache"])
def test_a_surprise_names_the_file_whose_reading_every_run_uses_again(tmp_path, made_by):
    """A reused reading comes back on every run, so starting over can't change the plan: the line says where it is."""
    kept, project, data = _readings(), tmp_path / "project", tmp_path / "data"
    path = project / "fixtures" / "readings" / "high.json" if made_by == "eval" else data / story.CACHE
    save_readings(path, ReadingsFile(Header("claude", prompts.MODEL, prompts.EFFORT, made_by), kept.readings, kept.dates))
    one = story.ENDINGS["1"]
    expects = (*one.with_claude[:2], dataclasses.replace(one.with_claude[2], tier=Tier.HAND_OFF), one.with_claude[3])
    with mock.patch.dict(story.ENDINGS, {"1": dataclasses.replace(one, with_claude=expects)}):
        code, said, _ = _run("1", tmp_path, project=project, ask=_never, client=_never)
    assert code == 1 and said[0] == "0 live reads needed."
    line, look = said[-1].split(" Look at it on the screens (disputable serve --data ")
    assert line == ("Order #1109, round 1: the plan isn't what the story expects. It is a challenge with 1 file, for one "
                    "approval, and the story expects a hand-off, with code's draft: a challenge with 1 file.")
    assert look.split(")", 1)[1] == (
        f". Run the story again with --lenient to record it as it is. Claude's reading of this case comes from {path}, and "
        "every run uses it again." + (" Delete that file to read the case again." if made_by == "cache" else ""))


def test_the_story_runs_claude_client_takes_nothing_from_the_shell(monkeypatch):
    """story._claude_client is reader.claude_client, built here unpatched (cli-1, cli-5). The shell's proxy is a
    documentation address, and no request is sent: the test looks only at what the client would send through."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example.com")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer from-the-shell\nX-From-The-Shell: yes")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.10:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = story._claude_client(" sk-made-up-key ")
    try:
        assert str(client.base_url).rstrip("/") == "https://api.anthropic.com" and client.api_key == "sk-made-up-key"
        headers = {name.lower(): value for name, value in client.default_headers.items()}
        assert "x-from-the-shell" not in headers and "from-the-shell" not in str(headers)
        http = client._client
        assert http.trust_env is False and not http._mounts  # no proxy from the shell either: none is mounted
        sent = http._transport_for_url(httpx2.URL("https://api.anthropic.com/v1/messages"))
        assert sent is http._transport and type(sent._pool).__name__ == "ConnectionPool"  # straight to the API
    finally:
        client.close()


# ----------------------------------------------------------------------------- the story command
def _settings(data: Path, *, claude: str | None = None) -> Settings:
    return Settings("fake-client-id-0123", "fake-api-key-0123456789", claude, "https://api.sandbox.airwallex.com",
                    "https://files.sandbox.airwallex.com", data)


def _run(name, tmp_path, *, fake=None, store=FERNHILL, **options):
    fake = fake or FakeAirwallex(Sandbox(store))
    wall = recordings.Ticking()
    said: list[str] = []
    options.setdefault("settings", _settings(tmp_path / "data"))
    code = story.run(name, options.pop("settings"), out=tmp_path / "out", transport=fake.transport(),
                     pay=lambda payment_id, secret: fake.pay(payment_id), wall=wall, sleep=wall.sleep, say=said.append,
                     **options)
    return code, said, fake


def _never(*args, **options):
    raise AssertionError("This shouldn't be called.")


def test_a_run_with_every_reading_kept_reads_nothing_live_and_asks_nothing(tmp_path):
    project = tmp_path / "project"
    recording.copy(recordings.recorded()["1"], project / "fixtures" / "story" / "ending-1")
    code, said, _ = _run("1", tmp_path, project=project, ask=_never, client=_never)
    assert code == 0 and said[0] == "0 live reads needed." and not [line for line in said if "Type yes" in line]
    made = load(tmp_path / "out" / "ending-1")
    assert made.manifest.source == "claude" and made.manifest.total == Decimal("279.30")
    assert made.manifest.airwallex == "sandbox"  # disputable story's run: on the sandbox, here played by the fake
    assert made.readings.readings == load(recordings.recorded()["1"]).readings.readings  # reused, not read again
    assert said[-1] == f"The recording is in {tmp_path / 'out' / 'ending-1'}."
    assert not (tmp_path / "data" / story.CACHE).exists()


def test_a_run_that_reads_live_needs_a_key_a_cap_and_a_yes(tmp_path):
    project = tmp_path / "empty"
    with pytest.raises(SetupError, match="reads 3 cases live"):
        _run("1", tmp_path, project=project, client=_never)
    keyed = _settings(tmp_path / "data", claude="sk-made-up")
    code, said, fake = _run("1", tmp_path, project=project, settings=keyed, client=_never)
    assert code == 1 and said == ["This run reads 3 cases live with Claude. Give it a dollar cap, such as --cap 3."]
    asked = []
    code, said, fake = _run("1", tmp_path, project=project, settings=keyed, client=_never, cap=Decimal(3),
                            ask=lambda question: asked.append(question) or "no")
    assert code == 1 and asked == ["Type yes to go on: "] and said[-1] == "Stopped. Nothing was read."
    assert said[0] == ("The story run of Ending 1 reads 3 cases live with Claude (claude-opus-5-5, effort high), with "
                       "your key.")
    assert not fake.calls and not (tmp_path / "data").exists() and not (tmp_path / "out").exists()
    code, said, _ = _run("3", tmp_path, project=project, settings=keyed)
    assert (code, said) == (1, ["There's no ending 3. The endings are 1 and 2."])
    with pytest.raises(SetupError, match="AWX_CLIENT_ID"):
        story.run("1", dataclasses.replace(keyed, airwallex_client_id=None), project=project)


class Unpaid:
    """A Claude client stand-in that must never be asked: the cap stops every request before it goes."""

    class beta:
        class messages:
            @staticmethod
            def create(**params):
                raise AssertionError("A request went out past the cap.")


def cap_failure() -> Failure:
    """What spend.BudgetReader makes of a read its meter stopped."""
    class Over:
        def read(self, request):
            raise spend.BudgetReached("run", Decimal(0), Decimal("0.01"))

    return spend.BudgetReader(Over()).read(request_for(FERNHILL, "earbuds")).failure


def test_a_run_holds_its_spend_to_the_cap(tmp_path):
    """The run's --cap stops the first read before any request goes out. That cap is the user's to raise, so the line
    says to run the story again with a higher --cap."""
    keyed = _settings(tmp_path / "data", claude="sk-made-up")
    code, said, fake = _run("1", tmp_path, project=tmp_path / "empty", settings=keyed, cap=Decimal("0.01"), yes=True,
                            client=lambda key: Unpaid())
    capped = cap_failure()  # the cap's own reason: no request went out, and no answer of Claude's is in the reading
    assert capped is Failure.CAPPED and capped in NOT_READ_LIVE  # so --lenient can't record it either
    assert code == 1 and said[-2] == (
        f"Order #1098: Claude's read failed ({WHY[capped]}), so this recording wouldn't show Claude's own reading. Run "
        "the story again with a higher --cap: the readings made so far are reused at no cost. Nothing was "
        "copied.")  # Monday's first pass: the earbuds are read first
    assert said[-1] == "Claude's live reads cost $0.00 in 0 requests."  # nothing was sent past the cap
    assert not (tmp_path / "out").exists() and not (tmp_path / "data" / story.CACHE).exists()


def keeps_reading() -> scripted.FakeClient:
    """A Claude client stand-in whose every reply reads the order history again, about $0.31 a request: a read on it
    never proposes, so a cap always stops it. Its 40 replies are more than a run asks for."""
    return scripted.FakeClient([scripted.message(scripted.thinking(), scripted.tool_use(
        "get_order_history", {"order_number": "1098"}), usage=scripted.usage(3_000, 15_000)) for _ in range(40)])


@pytest.mark.parametrize("cap, lenient, stopped, spent", [
    ("2", False, (f"Order #1098: Claude's read failed ({WHY[Failure.CAPPED]}), so this recording wouldn't show Claude's "
                  "own reading. Run the story again with a higher --cap: the readings made so far are reused at no "
                  "cost. Nothing was copied."), "Claude's live reads cost $1.25 in 4 requests."),
    ("100", False, ("Order #1098: the cap for one read ($2.50) stopped Claude's read, so a higher --cap won't help. Look at "
                    "the case before you run the story again. Nothing was copied."),
     "Claude's live reads cost $3.74 in 12 requests."),
    ("1000", True, ("Order #1098: the cap for one read ($2.50) stopped Claude's read, so a higher --cap won't help. Look at "
                    "the case before you run the story again. Nothing was copied."),
     "Claude's live reads cost $3.74 in 12 requests."),
], ids=["the run's cap", "the cap for one read", "the cap for one read, at a higher --cap and lenient"])
def test_a_capped_reads_line_says_which_cap_stopped_it(tmp_path, cap, lenient, stopped, spent):
    """--cap holds the whole run, and the fixed cap for one read (spend.READ_CAP) holds each read. Only --cap is the
    user's to raise. At --cap 2 the run's cap stops the earbuds' read after 4 requests. At --cap 100 the cap for one
    read stops it after 6, and the headphones' read the same way; at --cap 1000 nothing changes."""
    keyed, client = _settings(tmp_path / "data", claude="sk-made-up"), keeps_reading()
    code, said, _ = _run("1", tmp_path, project=tmp_path / "empty", settings=keyed, cap=Decimal(cap), yes=True,
                         lenient=lenient, client=lambda key: client)
    assert code == 1 and said[-2:] == [stopped, spent]  # Monday's first pass: the earbuds are read first
    assert len(client.requests) == int(spent.split(" in ")[1].split()[0])
    assert not (tmp_path / "out").exists() and not (tmp_path / "data" / story.CACHE).exists()  # a capped read isn't kept


# ----------------------------------------------------------------------------- the stand-in maker
NOT_THERE = ("The Claude readings for the story aren't there yet. Run the pilot first (disputable eval live --pilot "
             "--cap 3), or use --no-claude.")


def test_the_stand_in_maker_records_both_endings_with_no_claude():
    """scripts/record_standin.py --no-claude into a temp folder: both endings load, say "stand-in", replay to the
    story's totals with no Claude reading ($279.30 and $1,528.30), and hold nothing private."""
    made, maker = recordings.standins("none"), recordings.maker()
    assert made.code == 0 and made.said[:2] == (maker.ABOUT, maker.NO_READINGS)
    assert sorted(path.name for path in made.out.iterdir()) == ["ending-1", "ending-2"]
    for ending, total in (("1", "279.30"), ("2", "1528.30")):
        folder = made.folders()[ending]
        recorded = load(folder)
        manifest = recorded.manifest
        assert (manifest.airwallex, manifest.source, manifest.model, manifest.effort) == ("stand-in", "none", "", "")
        assert json.loads((folder / MANIFEST).read_text(encoding="ascii"))["airwallex"] == "stand-in"
        assert {reading.failure for reading in recorded.readings.readings.values()} == {Failure.NO_KEY}
        replay, played = recordings.replay_to_the_end(folder)
        recordings.assert_it_replays_to_its_own_total(replay, played)
        assert manifest.total == replay.totals()[0] == Decimal(total)
        recordings.assert_the_kind_is_what_answered(recorded)
        recordings.assert_nothing_private(folder, recordings.fake_secrets())
        for secret in sorted(recordings.fake_secrets()):  # the fake's keys, tokens, client secrets and account id
            assert recordings.holds(folder, secret) == [], secret
        assert f"Recorded Ending {ending}: {manifest.beats} steps," in "\n".join(made.said)
        assert f"The recording is in {folder}." in made.said
    assert not [line for line in made.said if line.startswith("The run stopped") or "live reads" in line]


def test_the_stand_in_maker_uses_claudes_readings_and_reads_nothing_live():
    """Every key the story reads is in a claude-source readings file, here the perfect reader's where the pilot leaves
    Claude's: 0 live reads. It builds no Claude client and no HTTPTransport or AsyncHTTPTransport of httpx or httpx2,
    and reads no .env through config, dotenv, open() or a Path (recordings.offline() fails each). It writes nothing but
    the two recordings: no cache, no other readings file."""
    made, maker = recordings.standins("claude"), recordings.maker()
    assert made.code == 0 and made.said[:2] == (maker.ABOUT, "0 live reads needed.")
    high = made.project / "fixtures" / "readings" / "high.json"
    assert [path for path in made.project.rglob("*") if path.is_file()] == [high]  # the test's own file, and no other
    assert not made.data.exists()  # no live read, so no cache
    pilot = load_readings(high, want="claude")
    for ending, total in (("1", "279.30"), ("2", "1512.30")):
        recorded = load(made.folders()[ending])
        manifest, keys = recorded.manifest, story.story_keys(story.ENDINGS[ending])
        assert (manifest.airwallex, manifest.source, manifest.model, manifest.effort) == (
            "stand-in", "claude", prompts.MODEL, prompts.EFFORT)
        assert dict(recorded.readings.readings) == {key: pilot.readings[key] for key in keys}  # reused as they are
        assert dict(recorded.readings.dates) == {key: pilot.dates[key] for key in keys}
        replay, played = recordings.replay_to_the_end(made.folders()[ending])
        recordings.assert_it_replays_to_its_own_total(replay, played)
        assert manifest.total == Decimal(total)
        recordings.assert_the_readings_are_claudes_own(recorded)


def test_the_maker_runs_offline_or_not_at_all(tmp_path, monkeypatch):
    """The guards the maker runs under in these tests are real: each one stops what it guards. Here the socket
    functions are stand-ins that note each call that reaches them, so a guard that doesn't hold fails this test without
    reaching the network."""
    reached = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *rest, **options: reached.append(("look up", host)) or [])
    monkeypatch.setattr(socket, "create_connection", lambda address, *rest, **options: reached.append(
        ("connect", address[0])))
    env = tmp_path / ".env"
    env.write_text("AWX_CLIENT_ID=made-up\n", encoding="utf-8")
    with mock.patch.object(socket.socket, "connect", lambda self, address: reached.append(("socket", address[0]))), \
            mock.patch.object(socket.socket, "connect_ex", lambda self, address: reached.append(
                ("socket_ex", address[0])) or 0), recordings.offline():
        for build in (httpx.HTTPTransport, httpx.AsyncHTTPTransport, httpx2.HTTPTransport, httpx2.AsyncHTTPTransport):
            with pytest.raises(AssertionError, match="built a real transport"):  # httpx's, and the SDK's own httpx2
                build()
        for client in (anthropic.Anthropic, anthropic.AsyncAnthropic):
            with pytest.raises(AssertionError, match="built a Claude client"):
                client(api_key="sk-made-up-key")
        with pytest.raises(AssertionError, match="built a Claude client"):  # any other client: the SDK's base client
            anthropic.AnthropicGoogleCloud(skip_auth=True, base_url="https://claude.example.com")
        with pytest.raises(AssertionError, match="^The stand-in maker built a "):  # the one Claude client builder
            story._claude_client("sk-made-up-key")
        with pytest.raises(AssertionError, match="read a .env file"):
            config.load_settings(env, need_airwallex=False, data=tmp_path / "data")
        for read in (dotenv.dotenv_values, dotenv.load_dotenv, dotenv.main.dotenv_values, dotenv.main.load_dotenv):
            with pytest.raises(AssertionError, match="read a .env file"):
                read(env)
        for read in (env.read_text, env.read_bytes, lambda: open(env, encoding="utf-8"), lambda: open(str(env), "rb")):
            with pytest.raises(AssertionError, match="opened a .env file"):
                read()
        for host in ("api.anthropic.com", "api.sandbox.airwallex.com", "192.0.2.10", "2001:db8::10"):
            with pytest.raises(AssertionError, match=f"looked up {re.escape(host)} on the network"):
                socket.getaddrinfo(host, 443)
            with pytest.raises(AssertionError, match=f"connected to {re.escape(host)} on the network"):
                socket.create_connection((host, 443))
        with socket.socket() as sock:  # made, never connected: no packet leaves
            for connect in (sock.connect, sock.connect_ex):  # connect_ex answers an error code where connect raises
                with pytest.raises(AssertionError, match="connected to 192.0.2.10 on the network"):
                    connect(("192.0.2.10", 443))
            for local in ("127.0.0.1", "localhost", "::1"):  # this computer's own: passed on
                socket.getaddrinfo(local, 8765)
                socket.create_connection((local, 8765))
                sock.connect((local, 8765))
                sock.connect_ex((local, 8765))
    assert reached == [(kind, local) for local in ("127.0.0.1", "localhost", "::1")
                       for kind in ("look up", "connect", "socket", "socket_ex")]
    assert env.read_text(encoding="utf-8") == "AWX_CLIENT_ID=made-up\n"  # and every guard goes with the block


LEAKED = "sk-" + "ant-made-up-key-the-fake-leaks"  # made up, and built so no scanner mistakes it for a key


class Telling(FakeAirwallex):
    """The fake sandbox with a made-up Claude key in the ear tips' comment, as a note pasted by mistake might hold one:
    no scrub knows it, so only the run's own privacy check can stop it."""

    def _open_dispute(self, request):
        answer = super()._open_dispute(request)
        for dispute in self.sandbox.disputes.values():
            if dispute["issuer_comment"] == EAR_TIPS_COMMENT:
                dispute["issuer_comment"] = f"{EAR_TIPS_COMMENT} {LEAKED}"
        return answer


@pytest.mark.parametrize("fake, stop", [(Telling, story.PRIVATE), (Leaky, story.DIFFERS.split("{n}")[0])],
                         ids=["a secret the privacy check finds", "an answer the scrub changes"])
def test_the_stand_in_maker_writes_nothing_from_a_stand_in_that_misbehaves(tmp_path, monkeypatch, fake, stop):
    """The maker's run checks itself before anything is written, as a story run does: a stand-in that leaks a secret
    fails the privacy check, and one whose answer the scrub changes fails the self-replay. Nothing goes to --out."""
    monkeypatch.setattr(recordings, "FakeAirwallex", fake)  # the stand-in each ending is recorded on
    out, maker = tmp_path / "out", recordings.maker()
    made = recordings.make_standins(out, "--ending", "1", "--no-claude", project=tmp_path, data=tmp_path / "data")
    prefix, suffix = maker.RUN_KEPT.split("{folder}")
    assert made.code == 1 and made.said[-1].startswith(prefix) and made.said[-1].endswith(suffix)
    work = Path(made.said[-1].removeprefix(prefix).removesuffix(suffix))  # the run's own files, kept to look at
    try:
        assert made.said[-2].startswith(stop)
        if fake is Telling:  # each place the comment shows: the dispute's own reads, and the payment's list
            assert ".issuer_comment: an Anthropic API key." in made.said[-2].split("\n")[1]
        else:
            assert made.said[-2].endswith("after step 2, Order #1121's saved record differs. Nothing was copied.")
        assert not out.exists() and not [line for line in made.said if line.startswith("The recording is in")]
        assert LEAKED not in "\n".join(made.said)  # the line names the place and the kind, never the value
        staged = next(work.glob("*/story-*/recording"))  # the run's own recording stays out of the repo
        assert (LEAKED in (staged / EXCHANGES).read_text(encoding="ascii")) == (fake is Telling)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_stand_in_maker_stops_without_the_storys_readings(tmp_path):
    empty, data, out = tmp_path / "empty", tmp_path / "data", tmp_path / "out"
    made = recordings.make_standins(out, project=empty, data=data)
    assert made.code == 1 and made.said == (recordings.maker().ABOUT, NOT_THERE) and not out.exists()
    # Ending 1's readings alone hold Ending 2's round two for neither Ending 2 nor both endings.
    one = load(recordings.recorded()["1"]).readings
    save_readings(tmp_path / "project" / "fixtures" / "story" / "ending-1" / READINGS, one)
    for ending in ("2", "both"):
        made = recordings.make_standins(out, "--ending", ending, project=tmp_path / "project", data=data)
        assert (made.code, made.said[-1]) == (1, NOT_THERE), ending
    assert not out.exists() and not data.exists()
    assert recordings.make_standins(out, "--ending", "3", project=empty, data=data).code == 2  # argparse said why
    assert not out.exists()


def test_the_stand_in_maker_never_replaces_a_recording_made_on_the_sandbox(tmp_path):
    """Checked for every ending asked for before any is recorded. A stand-in's own recording is replaced whole."""
    out = tmp_path / "out"
    recording.copy(recordings.recorded()["1"], out / "ending-1")  # its manifest says the sandbox answered
    recording.copy(recordings.standins().folders()["2"], out / "ending-2")
    before = {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()}
    for ending in ("1", "both"):
        made = recordings.make_standins(out, "--ending", ending, "--no-claude", project=tmp_path, data=tmp_path)
        assert (made.code, made.said[-1]) == (1, recordings.maker().SANDBOX_KEPT.format(folder=out / "ending-1"))
    assert {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()} == before
    old = load(out / "ending-2").manifest.run
    made = recordings.make_standins(out, "--ending", "2", "--no-claude", project=tmp_path, data=tmp_path)
    assert made.code == 0 and made.said[-1] == f"The recording is in {out / 'ending-2'}."
    assert load(out / "ending-2").manifest.run != old and load(out / "ending-2").manifest.airwallex == "stand-in"
    assert {path: data for path, data in before.items() if path.parent.name == "ending-1"} == {
        path: path.read_bytes() for path in sorted((out / "ending-1").iterdir())}


def files_in(folder: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in sorted(Path(folder).rglob("*")) if path.is_file()}


def test_the_stand_in_maker_checks_every_ending_before_it_records_any(tmp_path):
    """A recording made on the sandbox in ending-2 alone stops --ending both before ending-1 is made."""
    out = tmp_path / "out"
    recording.copy(recordings.recorded()["2"], out / "ending-2")  # its manifest says the sandbox answered
    before = files_in(out)
    made = recordings.make_standins(out, "--ending", "both", "--no-claude", project=tmp_path, data=tmp_path)
    maker = recordings.maker()
    assert (made.code, made.said) == (1, (maker.ABOUT, maker.SANDBOX_KEPT.format(folder=out / "ending-2")))
    assert not (out / "ending-1").exists() and files_in(out) == before


def test_a_manifest_another_program_holds_never_lets_the_maker_replace_a_sandbox_recording(tmp_path, monkeypatch):
    """OneDrive can hold a file for a moment, and the repo is in OneDrive: the maker reads the manifest again, as
    recording.py retries a held file. While it stays held, or when it can't be read, the maker can't tell what made the
    recording, so it records nothing."""
    out, maker = tmp_path / "out", recordings.maker()
    recording.copy(recordings.recorded()["1"], out / "ending-1")  # made on the sandbox, as its manifest says
    before = files_in(out)
    target, real, held, waits = (out / "ending-1" / MANIFEST).resolve(), Path.open, [1], []

    def open_held(path, *args, **options):  # Windows' error for a file another program holds open
        if held[0] and Path(path).resolve() == target:
            held[0] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process",
                                  str(path))
        return real(path, *args, **options)

    monkeypatch.setattr(Path, "open", open_held)
    monkeypatch.setattr(time, "sleep", waits.append)  # the maker's wait between two reads

    def make() -> recordings.Made:
        return recordings.make_standins(out, "--ending", "1", "--no-claude", project=tmp_path, data=tmp_path)

    made = make()  # held for the first read only
    assert (made.code, made.said[-1]) == (1, maker.SANDBOX_KEPT.format(folder=out / "ending-1"))
    assert held == [0] and waits == [recording.RETRY_WAIT] and files_in(out) == before
    held[0], waits[:] = 100, []
    made = make()  # held through every retry
    assert (made.code, made.said) == (1, (maker.ABOUT, (
        f"Another program, such as OneDrive, holds the manifest.json in {out / 'ending-1'}, so this can't tell what made "
        "that recording. Nothing was recorded. Run it again in a moment.")))
    assert waits == [recording.RETRY_WAIT] * recording.RETRIES
    held[0] = 0  # let go, so this test can read it
    assert files_in(out) == before
    (out / "ending-1" / MANIFEST).write_bytes(b"{not json")  # it can't be read either
    before = files_in(out)
    made = make()
    assert (made.code, made.said[-1]) == (1, (
        f"The manifest.json in {out / 'ending-1'} can't be read, so this can't tell what made that recording. Nothing "
        "was recorded. Delete that folder first, or use --out with another folder."))
    assert files_in(out) == before


@pytest.mark.parametrize("fault", ["OneDrive can't fetch it", "it isn't UTF-8"])
def test_a_manifest_the_maker_cant_read_never_lets_it_replace_a_sandbox_recording(tmp_path, monkeypatch, fault):
    """A missing manifest, or one that names no kind or a kind other than the sandbox, lets the maker replace the
    folder. An error that isn't a held file (Windows error 362, when OneDrive can't fetch an online-only file), or a
    manifest that isn't UTF-8 (saved again as UTF-16, say), can't tell what made the recording: the maker records
    nothing, at once, and the folder stays as it was."""
    out, maker = tmp_path / "out", recordings.maker()
    recording.copy(recordings.recorded()["1"], out / "ending-1")  # made on the sandbox, as its manifest says
    target, real, failing, waits = (out / "ending-1" / MANIFEST).resolve(), Path.open, [False], []
    if fault == "it isn't UTF-8":
        target.write_bytes(target.read_text(encoding="utf-8").encode("utf-16"))
    else:
        def open_online_only(path, *args, **options):
            if failing[0] and Path(path).resolve() == target:
                raise OSError(22, "The cloud file provider is not running", str(path), 362)
            return real(path, *args, **options)

        monkeypatch.setattr(Path, "open", open_online_only)
    before = files_in(out)
    monkeypatch.setattr(time, "sleep", waits.append)  # the maker's wait between two reads of a held file
    failing[0] = True
    made = recordings.make_standins(out, "--ending", "1", "--no-claude", project=tmp_path, data=tmp_path)
    failing[0] = False  # so this test can read it
    assert (made.code, made.said) == (1, (maker.ABOUT, (
        f"The manifest.json in {out / 'ending-1'} can't be read, so this can't tell what made that recording. Nothing "
        "was recorded. Delete that folder first, or use --out with another folder.")))
    assert waits == [] and files_in(out) == before  # read once: only a held file is read again


@pytest.mark.parametrize("readings", ["none", "claude"])
@pytest.mark.parametrize("ending", ["1", "2"])
def test_a_stand_in_keeps_every_rule_a_committed_recording_keeps(ending, readings):
    """test_story_recordings' checks hold for a recording the stand-in maker makes, with or without Claude's readings,
    as for one made on the sandbox."""
    folder = recordings.standins(readings).folders()[ending]
    recorded = load(folder)
    replay, played = recordings.replay_to_the_end(folder)
    recordings.assert_it_replays_to_its_own_total(replay, played)
    recordings.assert_only_the_ear_tips_alert_on_monday(played)
    recordings.assert_the_app_responds_as_itself(recorded)
    recordings.assert_the_storys_deadlines(recorded)
    recordings.assert_todays_formats(folder)
    recordings.assert_a_story_the_demo_plays(recorded)
    recordings.assert_the_readings_are_claudes_own(recorded)
    recordings.assert_the_kind_is_what_answered(recorded)
    recordings.assert_nothing_private(folder, recordings.fake_secrets())


def test_the_stand_ins_are_made_once_a_session_however_they_are_asked_for():
    """standins() and standins("none") are the same --no-claude stand-ins, so the maker makes them once."""
    assert recordings.standins() is recordings.standins("none") is recordings.standins(readings="none")


@pytest.mark.parametrize("ending", ["1", "2"])
def test_a_run_on_the_fake_labelled_as_the_sandboxs_fails_the_kind_check(ending):
    """record_on_fake() writes "sandbox", as disputable story does: so these never pass for a committed recording."""
    recorded = load(recordings.recorded()[ending])
    assert recorded.manifest.airwallex == "sandbox"
    with pytest.raises(AssertionError):
        recordings.assert_the_kind_is_what_answered(recorded)
    relabelled = dataclasses.replace(recorded, manifest=dataclasses.replace(recorded.manifest, airwallex="stand-in"))
    recordings.assert_the_kind_is_what_answered(relabelled)


# ----------------------------------------------------------------------------- the core and the rest
def test_the_recorder_never_imports_the_seed_the_bank_the_story_the_eval_or_the_screens():
    tree = ast.parse(Path(importlib.util.find_spec("disputable.recording").origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "disputable.loop" in names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web",
                   "disputable.demo", "disputable.cli"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], banned
