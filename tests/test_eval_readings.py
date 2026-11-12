"""The eval's readings files: keep() checks and saves live readings, replay finds them by key, refresh checks them again,
and the committed files hold only the seed's own data and still give the committed report.

Every file here is written in a temp folder. The readings are the scripted perfect reader's (tests/cases.py), kept as
the live runner keeps Claude's. The tests of the committed files skip until the first live reading is committed: then
they can only fail.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import cases
from disputable import privacy, prompts, readings_file
from disputable.engine import Status
from disputable.evals import main, report, runner
from disputable.evals.runner import EvalError, World, keep, privacy_problems, recorded, recorded_file, replayed
from disputable.evals.sites import STORY, by_label, request_for, sites
from disputable.lifecycle import cost
from disputable.reader import CHECKS_VERSION, Failure, ReplayReader, failed, revalidate, to_json, validate
from disputable.readings_file import Header, ReadingsFile, load_readings, save_readings
from disputable.seed.fernhill import ENDING_2, FERNHILL
from test_privacy import file_id
from test_rounds import Rounds, play

DAY, LATER = date(2026, 11, 8), date(2026, 11, 9)
ALLOWED = runner.seed_allowed()
COMMITTED = sorted(runner.READINGS.glob("*.json"))
DOCS = sorted(report.DOCS.glob("*")) if report.DOCS.exists() else []
NOT_YET = "No live readings are kept yet."


def story_reading(label="headphones"):
    """The story's reading as the eval keys it: the scripted perfect reader on the site."""
    site = by_label()[label]
    return cases.read_with(label)[0] if label not in ("headphones_round_2", "ending_2") else \
        runner.scripted_reading("perfect", site)


def with_note(reading, note):
    """The reading with one row's note written by hand, as Claude might write it."""
    return replace(reading, rows=(replace(reading.rows[0], note=note), *reading.rows[1:]))


# ----------------------------------------------------------------------------- keep and load
def test_a_kept_reading_loads_back_the_same(tmp_path):
    readings = [story_reading(label) for label in STORY]
    for number, reading in enumerate(readings):
        assert keep("high", reading, allowed=ALLOWED, day=DAY if number % 2 else LATER, folder=tmp_path) == ()
    assert recorded("high", tmp_path) == {reading.key: reading for reading in readings}
    path = tmp_path / "high.json"
    data = path.read_bytes()
    assert data.isascii() and b"\r" not in data and data.endswith(b"]}\n")
    loaded = json.loads(data)
    assert {key: loaded[key] for key in ("format", "source", "model", "effort", "made_by")} == {
        "format": 1, "source": "claude", "model": prompts.MODEL, "effort": "high", "made_by": "eval"}
    assert [entry["key"] for entry in loaded["readings"]] == sorted(reading.key for reading in readings)
    assert load_readings(path, want="claude").dates[readings[0].key] == LATER
    assert recorded("medium", tmp_path) == {} and recorded_file("medium", tmp_path) is None


def test_keeping_a_reading_again_replaces_the_one_with_its_key(tmp_path):
    first = story_reading("earbuds")
    again = replace(first, complete=False)  # read again on purpose: what came back this time
    keep("high", first, allowed=ALLOWED, day=DAY, folder=tmp_path)
    keep("high", story_reading("headphones"), allowed=ALLOWED, day=DAY, folder=tmp_path)
    keep("high", again, allowed=ALLOWED, day=LATER, folder=tmp_path)
    file = load_readings(tmp_path / "high.json")
    assert file.readings[first.key] == again and file.dates[first.key] == LATER and len(file.readings) == 2


def test_replay_finds_each_kept_reading_by_its_key(tmp_path):
    for label in STORY:
        keep("high", story_reading(label), allowed=ALLOWED, day=DAY, folder=tmp_path)
    outcomes = replayed("high", sites(), tmp_path)
    found = {outcome.site.label: outcome for outcome in outcomes}
    assert [label for label, outcome in found.items() if outcome.reading is not None] == list(STORY)
    assert all(outcome.failure == "NOT_RECORDED" and outcome.source == "kept" for label, outcome in found.items()
               if label not in STORY)
    assert found["earbuds"].reading == story_reading("earbuds") and not found["earbuds"].rechecked
    assert runner.unused("high", sites(), tmp_path) == 0
    assert all(outcome.failure == "NOT_RECORDED" for outcome in replayed("medium", sites(), tmp_path))


def test_a_reading_under_older_checks_is_checked_again_and_refresh_rewrites_it(tmp_path):
    current = story_reading("headphones")
    keep("high", replace(current, checks=CHECKS_VERSION - 1), allowed=ALLOWED, day=DAY, folder=tmp_path)
    outcome = next(one for one in replayed("high", sites(), tmp_path) if one.site.label == "headphones")
    assert outcome.rechecked and not outcome.changed and outcome.reading == current  # no call: the checks ran again
    # Today's checks change nothing in it but its checks number, so refresh counts it as replay does: not changed.
    said = runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path)
    assert said == ["high.json: 1 readings kept, 0 changed by today's checks, 0 that no case reads any more dropped."]
    file = load_readings(tmp_path / "high.json")
    assert file.readings == {current.key: current} and file.dates == {current.key: DAY}
    assert not next(one for one in replayed("high", sites(), tmp_path) if one.site.label == "headphones").rechecked


def test_refresh_brings_a_kept_model_failure_up_to_todays_checks(tmp_path):
    # A failure of the model's own has nothing to check again, so it fails the same way under today's checks: refresh
    # gives it today's checks number. Without that, the committed-files test fails for good after a bump, and only a
    # paid read again could fix it, which would throw away the kept failure.
    kinds = (Failure.CUT_OFF, Failure.REFUSED, Failure.NO_PROPOSAL, Failure.UNEXPECTED_STOP, Failure.TOO_SLOW,
             Failure.MALFORMED)  # MALFORMED with no answer kept
    places = sites()[:len(kinds)]
    for site, kind in zip(places, kinds):
        old = replace(failed(request_for(site).key, kind), checks=CHECKS_VERSION - 1)
        assert keep("high", old, allowed=ALLOWED, day=DAY, folder=tmp_path) == ()
    assert all(one.rechecked and not one.changed for one in replayed("high", places, tmp_path))
    assert runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path) == [
        "high.json: 6 readings kept, 0 changed by today's checks, 0 that no case reads any more dropped."]
    kept = recorded("high", tmp_path)
    for site, kind in zip(places, kinds):
        reading, request = kept[request_for(site).key], request_for(site)
        assert reading == failed(request.key, kind) and reading.checks == CHECKS_VERSION, kind
        assert revalidate(reading, request) == reading, kind  # what the committed-files test asks of each one
    assert not any(one.rechecked for one in replayed("high", places, tmp_path))
    assert runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path) == [
        "high.json: 6 readings kept, 0 changed by today's checks, 0 that no case reads any more dropped."]


def test_refresh_drops_readings_no_site_reads_and_says_how_many(tmp_path):
    other = prompts.StoreProfile("Another Store", "Sam", "help@another.example")
    site = by_label()["earbuds"]
    stale = replace(story_reading("earbuds"), key=request_for(site, profile=other).key)  # another prompt read it
    keep("high", stale, allowed=ALLOWED, day=DAY, folder=tmp_path)
    keep("high", story_reading("headphones"), allowed=ALLOWED, day=DAY, folder=tmp_path)
    assert runner.unused("high", sites(), tmp_path) == 1
    assert runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path) == [
        "high.json: 1 readings kept, 0 changed by today's checks, 1 that no case reads any more dropped."]
    assert set(recorded("high", tmp_path)) == {story_reading("headphones").key}
    assert runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path / "none") == []


REFUSED = [  # one value per kind keep() refuses, and the kind each one is refused as
    ("sk-" + "ant-made-up-key", privacy.FOUND_API_KEY),
    ("Bearer madeuptoken0123456789", privacy.FOUND_BEARER),
    ("msg_" + "01MadeUpMessageId0123", privacy.FOUND_API_ID),
    ("10.20.30.40", privacy.FOUND_IP),
    ("192.0.2.250", privacy.FOUND_IP),  # a documentation address the seed doesn't hold: the committed files' rule
    ("made.up@unreserved.zz", privacy.FOUND_EMAIL),
    ("+44 20 7946 0958", privacy.FOUND_PHONE),
    ("12345678-1234-4234-8234-123456789abc", privacy.FOUND_UUID),
    (file_id("12345678-1234-4234-8234-123456789abc", "one.pdf_1"), privacy.FOUND_FILE_ID),
    ("C:\\Users\\someone\\Desktop\\notes.txt", privacy.FOUND_LOCAL_PATH),
]


@pytest.mark.parametrize("value, kind", REFUSED)
def test_keep_refuses_keys_ids_and_values_the_seed_doesnt_hold(tmp_path, value, kind):
    reading = with_note(story_reading("headphones"), f"The record says {value} there.")
    problems = keep("high", reading, allowed=ALLOWED, day=DAY, folder=tmp_path)
    assert f"reading {reading.key[:8]}..., rows[0].note: {kind}." in problems
    assert all(value not in line for line in problems) and not (tmp_path / "high.json").exists()


def test_the_seeds_own_values_and_the_storys_readings_pass():
    for value in ("203.0.113.7", "198.51.100.77", "alex.sample@example.com", "+1 4155550142",
                  "59ec5db9-399c-4043-9a6c-fcd1a48d99aa", "127.0.0.1"):
        assert privacy_problems(with_note(story_reading("headphones"), f"It says {value}."), ALLOWED) == (), value
    for label in STORY:
        assert privacy_problems(story_reading(label), ALLOWED) == (), label
    # An escaped line break can't hide an address from the scrub rule: each string is checked as it reads.
    hidden = with_note(story_reading("headphones"), "First line.\n192.0.2.250")
    assert privacy_problems(hidden, ALLOWED) == (f"reading {hidden.key[:8]}..., rows[0].note: {privacy.FOUND_IP}.",)
    assert "\\n192.0.2.250" in to_json(hidden)


def test_keep_walks_into_the_proposal_for_an_address_past_the_kept_note(tmp_path):
    # Claude's draft note runs past the 200 characters a kept note holds, so the address after its line break lives
    # only in the raw proposal, whose JSON writes the break as "\n" and hides the address from the scrub rule. Only
    # the walk into that JSON finds it; reading_problems() lets a documentation address pass.
    site, perfect = by_label()["headphones"], story_reading("headphones")
    raw = json.loads(perfect.proposal)
    raw["draft"] = {**raw["draft"], "note": "A" * 199 + "\n192.0.2.250"}
    reading = validate(raw, request_for(site), perfect.tool_log, calls=perfect.calls)
    assert reading.failure is None and reading.draft.note == "A" * 199 and "\\n192.0.2.250" in reading.proposal
    assert list(privacy.reading_problems(to_json(reading), ALLOWED)) == []
    assert keep("high", reading, allowed=ALLOWED, day=DAY, folder=tmp_path) == (
        f"reading {reading.key[:8]}..., proposal.draft.note: {privacy.FOUND_IP}.",)
    assert not (tmp_path / "high.json").exists()


def test_a_write_retries_when_onedrive_holds_the_file(tmp_path, monkeypatch):
    real, held = os.replace, []

    def flaky(source, target):
        if len(held) < 2:
            held.append(target)
            raise PermissionError("The process cannot access the file because another process is using it.")
        return real(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(readings_file.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(report.time, "sleep", lambda seconds: None)
    assert keep("high", story_reading("earbuds"), allowed=ALLOWED, day=DAY, folder=tmp_path) == ()
    assert len(held) == 2 and set(recorded("high", tmp_path)) == {story_reading("earbuds").key}
    held.clear()
    report.write(tmp_path / "docs" / "report.md", "# A report\n")
    assert len(held) == 2 and (tmp_path / "docs" / "report.md").read_bytes() == b"# A report\n"
    assert not list(tmp_path.rglob("*.tmp"))


def test_a_file_held_past_the_retries_gets_a_plain_line_not_a_traceback(tmp_path, monkeypatch):
    old = replace(story_reading("headphones"), checks=CHECKS_VERSION - 1)
    keep("high", old, allowed=ALLOWED, day=DAY, folder=tmp_path)

    def held(source, target):
        raise PermissionError("The process cannot access the file because another process is using it.")

    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(readings_file.time, "sleep", lambda seconds: None)
    line = ("high.json couldn't be written, because another program is using it. Run `disputable eval refresh` again. "
            "It costs nothing.")
    with pytest.raises(EvalError) as stopped:
        runner.refresh(sites(), allowed=ALLOWED, folder=tmp_path)
    assert str(stopped.value) == line
    said = []
    world = World(env_file=tmp_path / ".env", readings=tmp_path, docs=tmp_path / "docs", say=said.append,
                  client_for=None)
    assert main(["refresh"], world) == 1 and said == [line]
    assert recorded("high", tmp_path) == {old.key: old} and not list(tmp_path.rglob("*.tmp"))  # the file as it was


def test_a_readings_file_another_model_made_stops_keep_and_the_run(tmp_path):
    reading = story_reading("earbuds")
    save_readings(tmp_path / "high.json", ReadingsFile(Header("claude", "claude-opus-5", "high", "eval"),
                                                       {reading.key: reading}, {reading.key: DAY}))
    with pytest.raises(EvalError, match="^high.json holds readings Claude made with another model or effort. Run "):
        keep("high", story_reading("headphones"), allowed=ALLOWED, day=DAY, folder=tmp_path)
    with pytest.raises(EvalError):
        runner.live(object(), sites()[:1], ("high",), cap=Decimal("1"), allowed=ALLOWED, folder=tmp_path)
    (tmp_path / "medium.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EvalError, match="^medium.json isn't valid JSON.$"):
        recorded("medium", tmp_path)
    save_readings(tmp_path / "low.json", ReadingsFile(Header("none", "", "", "story"),
                                                       {"k": failed("k", Failure.NO_KEY)}, {"k": DAY}))
    with pytest.raises(EvalError, match="doesn't hold readings Claude made live"):
        recorded("low", tmp_path)


def test_scripted_readings_are_never_written(tmp_path):
    said = []
    world = World(env_file=tmp_path / ".env", readings=tmp_path / "readings", docs=tmp_path / "docs",
                  say=said.append, client_for=None, read=lambda name, site, effort: cases.read_with(site.label, name)[0]
                  if name != "no-key" else failed(request_for(site, effort).key, Failure.NO_KEY))
    for reader in runner.SCRIPTED:
        assert main(["scripted", "--reader", reader, "--cases", "earbuds,headphones"], world) == 0, reader
    assert len(said) == len(runner.SCRIPTED) and list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------------- the committed files
def committed():
    if not COMMITTED:
        pytest.skip(NOT_YET)
    return {path.stem: load_readings(path, want="claude") for path in COMMITTED}


def test_committed_readings_and_eval_files_hold_only_seed_data():
    committed()
    assert privacy.check_files([*COMMITTED, *DOCS], ALLOWED, ()) == []
    for path in COMMITTED:
        file = load_readings(path, want="claude")
        assert (file.header.source, file.header.made_by, file.header.effort) == ("claude", "eval", path.stem)
        for reading in file.readings.values():
            assert privacy_problems(reading, ALLOWED) == (), path.name


def test_every_kept_reading_passes_todays_checks_unchanged():
    for effort, file in committed().items():
        requests = {request.key: request for request in (request_for(site, effort) for site in sites())}
        for key, reading in file.readings.items():
            if key in requests:
                assert reading.checks == CHECKS_VERSION, (effort, key[:8])
                assert revalidate(reading, requests[key]) == reading, (effort, key[:8])


def test_the_story_has_a_reading_for_each_read_at_the_apps_effort():
    files = committed()
    assert prompts.EFFORT in files, f"There's no {prompts.EFFORT}.json in fixtures/readings."
    kept = files[prompts.EFFORT].readings
    missing = [label for label in STORY if request_for(by_label()[label]).key not in kept]
    assert missing == [], f"No kept reading for {', '.join(missing)} at effort {prompts.EFFORT}."


@pytest.mark.parametrize("store, ruling, totals", [
    (FERNHILL, Status.WON, ("279.30",)), (FERNHILL, Status.LOST, ("1778.30",)), (ENDING_2, None, ("1512.30", "1528.30"))])
def test_the_story_replays_through_lifecycle_with_the_kept_readings(store, ruling, totals):
    files = committed()
    story = play(Rounds(store, reader=ReplayReader(dict(files[prompts.EFFORT].readings))), ruling)
    read = [step.reading for steps in story.steps.values() for step in steps if step.reading is not None]
    assert len(read) == 3 and not [reading for reading in read if reading.failure is Failure.NOT_RECORDED]
    assert str(sum(cost(record) for record in story.records.values())) in totals


def test_the_committed_report_is_what_the_readings_give(tmp_path):
    committed()
    if not DOCS:
        pytest.skip("The report isn't committed yet.")
    docs = tmp_path / "docs"
    docs.mkdir()
    if (report.DOCS / "runs.json").exists():
        shutil.copyfile(report.DOCS / "runs.json", docs / "runs.json")
    world = World(env_file=tmp_path / ".env", readings=runner.READINGS, docs=docs, say=lambda line: None,
                  client_for=None)
    assert main([], world) == 0
    for name in ("report.md", "results.json"):
        assert (docs / name).read_bytes() == (report.DOCS / name).read_bytes(), f"{name} isn't what the readings give."
    assert json.loads((docs / "results.json").read_text(encoding="ascii"))["measured"]


def test_the_committed_paths_are_the_repos():
    root = Path(runner.__file__).resolve().parents[3]
    assert runner.READINGS == root / "fixtures" / "readings" and report.DOCS == root / "docs" / "eval"
    assert (root / "pyproject.toml").exists() and runner.ROOT == root
