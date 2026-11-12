"""readings_file.py: the one readings-file format, strict to load, sorted, ASCII and atomic to save.

The readings are real ones: the story's four reads by the scripted perfect reader (tests/cases.py), and failed readings
as rounds keeps them. Nothing here reads live.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import cases
from disputable import readings_file
from disputable.reader import FORMAT as READING_FORMAT
from disputable.reader import Failure, failed, from_json, to_json
from disputable.readings_file import (FORMAT, Header, ReadingsFile, load_readings, merge, save_readings)

STORY = ("earbuds", "headphones", "headphones_round_2", "ending_2")
CLAUDE = Header("claude", "claude-opus-5-5", "high", "eval")
NO_KEY = Header("none", "", "", "story")
DAY = date(2026, 11, 8)
PLANTED = "made.up@unreserved.zz"  # a value no message may quote


def key(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(scope="module")
def story():
    return {reading.key: reading for reading in (cases.read_with(label)[0] for label in STORY)}


def claude_file(readings, made=DAY):
    readings = list(readings.values()) if isinstance(readings, dict) else list(readings)
    return ReadingsFile(CLAUDE, {reading.key: reading for reading in readings}, {reading.key: made for reading in readings})


def written(tmp_path, data, name="high.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def document(story, **changes):
    """A readings file as JSON data, to break one part at a time."""
    data = {"format": FORMAT, "source": "claude", "model": "claude-opus-5-5", "effort": "high", "made_by": "eval",
            "readings": [{"date": "2026-11-08", "key": one.key, "reading": json.loads(to_json(one))}
                         for one in story.values()]}
    data.update(changes)
    return data


# ----------------------------------------------------------------------------- round trip
def test_a_saved_file_loads_back_the_same(tmp_path, story):
    cut_off = failed(key("cut off"), Failure.CUT_OFF)
    first = next(iter(story.values()))
    noted = replace(first, rows=(replace(first.rows[0], note="café ☕ and half an emoji \ud83d"),
                                 *first.rows[1:]), key=key("noted"))
    file = ReadingsFile(CLAUDE, {**story, cut_off.key: cut_off, noted.key: noted},
                        {**{one: DAY for one in story}, cut_off.key: date(2026, 11, 9), noted.key: DAY})
    path = tmp_path / "high.json"
    save_readings(path, file)
    back = load_readings(path)
    assert back == file
    assert back.readings[noted.key].rows[0].note == "café ☕ and half an emoji \ud83d"
    assert back.dates[cut_off.key] == date(2026, 11, 9)
    assert isinstance(back.readings, MappingProxyType) and isinstance(back.dates, MappingProxyType)


def test_a_saved_file_is_sorted_ascii_lf_with_a_final_lf(tmp_path, story):
    path = tmp_path / "high.json"
    save_readings(path, claude_file(reversed(list(story.values()))))
    raw = path.read_bytes()
    assert all(byte < 128 for byte in raw)
    assert b"\r" not in raw and raw.endswith(b"]}\n") and not raw.endswith(b"\n\n")
    data = json.loads(raw)
    assert list(data) == ["format", "source", "model", "effort", "made_by", "readings"]
    assert [entry["key"] for entry in data["readings"]] == sorted(story)
    assert [entry["reading"] for entry in data["readings"]] == [json.loads(to_json(story[one])) for one in sorted(story)]
    lines = raw.decode("ascii").split("\n")
    assert len(lines) == len(story) + 3  # the header, one reading a line, the closing line, and the final LF
    assert not list(tmp_path.glob("*.tmp"))


def test_an_empty_file_saves_and_loads(tmp_path):
    path = tmp_path / "nested" / "cache.json"
    file = ReadingsFile(Header("claude", "claude-opus-5-5", "high", "cache"), {}, {})
    save_readings(path, file)
    assert path.read_text(encoding="ascii").endswith('"readings": []}\n')
    assert load_readings(path) == file


def test_readings_made_with_no_key_have_their_own_source(tmp_path):
    readings = [failed(key(name), Failure.NO_KEY) for name in ("earbuds", "headphones")]
    file = ReadingsFile(NO_KEY, {one.key: one for one in readings}, {one.key: DAY for one in readings})
    save_readings(tmp_path / "readings.json", file)
    assert load_readings(tmp_path / "readings.json", want="none") == file


# ----------------------------------------------------------------------------- strict loading
@pytest.mark.parametrize("change, message", [
    (lambda data: "not JSON {", "high.json isn't valid JSON."),
    (lambda data: [data], "high.json isn't a format 1 readings file."),
    (lambda data: {**data, "format": 2}, "high.json isn't a format 1 readings file."),
    (lambda data: {**data, "format": "1"}, "high.json isn't a format 1 readings file."),
    (lambda data: {**data, "format": True}, "high.json isn't a format 1 readings file."),
    (lambda data: {name: value for name, value in data.items() if name != "format"},
     "high.json isn't a format 1 readings file."),
    *((lambda data, field=field: {name: value for name, value in data.items() if name != field},
       f"high.json has no {field} in its header.") for field in ("source", "model", "effort", "made_by", "readings")),
    (lambda data: {**data, "note": PLANTED}, "high.json has fields a readings file doesn't have."),
    (lambda data: {**data, "source": "scripted"},
     "high.json: A readings file's source is claude (read live by Claude) or none (no key)."),
    (lambda data: {**data, "source": PLANTED},
     "high.json: A readings file's source is claude (read live by Claude) or none (no key)."),
    (lambda data: {**data, "source": 1}, "high.json: A readings file's header holds text only."),
    (lambda data: {**data, "made_by": PLANTED},
     "high.json: A readings file is made by the eval, the story run or the cache."),
    (lambda data: {**data, "model": ""},
     "high.json: A file of Claude's readings names the model and the effort they were read with."),
    (lambda data: {**data, "source": "none", "effort": ""},
     "high.json: A file of readings made with no key names no model and no effort."),
    (lambda data: {**data, "readings": {}}, "high.json: its readings aren't a list."),
])
def test_a_file_that_isnt_one_is_refused_by_name(tmp_path, story, change, message):
    path = written(tmp_path, change(document(story)))
    with pytest.raises(ValueError) as refused:
        load_readings(path)
    assert str(refused.value) == message
    assert PLANTED not in str(refused.value)


def _entries(story, change):
    data = document(story)
    data["readings"] = change(data["readings"])
    return data


@pytest.mark.parametrize("change, message", [
    (lambda entries: [*entries, entries[0]], "high.json, readings[4] has the key of an earlier reading."),
    (lambda entries: [{**entries[0], "key": key(PLANTED)}, *entries[1:]],
     "high.json, readings[0]: its key isn't the key its reading holds."),
    (lambda entries: [{**entries[0], "key": PLANTED}, *entries[1:]],
     "high.json, readings[0]: its key isn't the key its reading holds."),
    (lambda entries: [{**entries[0], "key": 7}, *entries[1:]], "high.json, readings[0]: its key isn't text."),
    (lambda entries: [{**entries[0], "reading": {**entries[0]["reading"], "format": 2}}, *entries[1:]],
     "high.json, readings[0]: its reading won't load."),
    (lambda entries: [{**entries[0], "reading": {"format": READING_FORMAT, "reading": {"key": PLANTED}}}],
     "high.json, readings[0]: its reading won't load."),
    (lambda entries: [{**entries[0], "reading": PLANTED}], "high.json, readings[0]: its reading won't load."),
    *((lambda entries, day=day: [{**entries[0], "date": day}], "high.json, readings[0]: its date isn't a day, written "
       "YYYY-MM-DD.") for day in ("2026-13-01", "20261108", 20261108, "2026-11-08T09:00:00+00:00")),
    (lambda entries: [{**entries[0], "note": PLANTED}],
     "high.json, readings[0] doesn't hold exactly a date, a key and a reading."),
    (lambda entries: [entries[0]["reading"]], "high.json, readings[0] doesn't hold exactly a date, a key and a reading."),
])
def test_a_reading_that_doesnt_fit_is_refused_by_place(tmp_path, story, change, message):
    path = written(tmp_path, _entries(story, change))
    with pytest.raises(ValueError) as refused:
        load_readings(path)
    assert str(refused.value) == message
    assert PLANTED not in str(refused.value) and key(PLANTED) not in str(refused.value)


def test_a_source_holds_only_readings_it_can_make(tmp_path, story):
    no_key = failed(key("no key"), Failure.NO_KEY)
    claude = document(story)
    claude["readings"].append({"date": "2026-11-08", "key": no_key.key, "reading": json.loads(to_json(no_key))})
    with pytest.raises(ValueError, match=r"^high\.json: The reading [0-9a-f]{8}\.\.\. wasn't read live by Claude"):
        load_readings(written(tmp_path, claude))
    replayed = failed(key("replayed"), Failure.NOT_RECORDED)
    with pytest.raises(ValueError, match="wasn't read live by Claude"):
        claude_file([replayed])
    capped = failed(key("capped"), Failure.CAPPED)  # this run's own cap stopped it: no answer of Claude's is in it
    with pytest.raises(ValueError, match="wasn't read live by Claude"):
        claude_file([capped])
    none = document(story, source="none", model="", effort="")
    with pytest.raises(ValueError, match=r"^high\.json: The reading [0-9a-f]{8}\.\.\. wasn't made with no key"):
        load_readings(written(tmp_path, none))


def test_a_file_that_isnt_text_is_refused(tmp_path):
    path = tmp_path / "high.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(ValueError, match=r"^high\.json isn't UTF-8 text\.$"):
        load_readings(path)
    with pytest.raises(FileNotFoundError):
        load_readings(tmp_path / "missing.json")


def test_want_claude_refuses_any_other_source(tmp_path, story):
    path = tmp_path / "high.json"
    save_readings(path, claude_file(story))
    assert load_readings(path, want="claude").header == CLAUDE
    with pytest.raises(ValueError, match="this needs the readings made with no key"):
        load_readings(path, want="none")
    readings = [failed(key("earbuds"), Failure.NO_KEY)]
    save_readings(path, ReadingsFile(NO_KEY, {one.key: one for one in readings}, {one.key: DAY for one in readings}))
    with pytest.raises(ValueError) as refused:
        load_readings(path, want="claude")
    assert str(refused.value) == "high.json doesn't hold readings Claude made live, so it can't be used here."
    with pytest.raises(ValueError):
        load_readings(path, want="scripted")


def test_a_header_never_says_scripted():
    for source in ("scripted", "", "Claude"):
        with pytest.raises(ValueError):
            Header(source, "claude-opus-5-5", "high", "eval")
    with pytest.raises(ValueError):
        Header("claude", "claude-opus-5-5", "high", "test")
    assert readings_file.SOURCES == {"claude", "none"}


def test_a_readings_file_is_checked_when_it_is_made(story):
    one = next(iter(story.values()))
    with pytest.raises(ValueError, match="filed under a key that isn't its own"):
        ReadingsFile(CLAUDE, {key("other"): one}, {key("other"): DAY})
    with pytest.raises(ValueError, match="one date for each reading"):
        ReadingsFile(CLAUDE, {one.key: one}, {})
    with pytest.raises(ValueError, match="a date that isn't a day"):
        ReadingsFile(CLAUDE, {one.key: one}, {one.key: datetime(2026, 11, 8, tzinfo=timezone.utc)})
    with pytest.raises(TypeError):
        ReadingsFile(CLAUDE, {one.key: to_json(one)}, {one.key: DAY})
    with pytest.raises(ValueError, match="^A reading is filed under"):  # a key that isn't a SHA-256 is never quoted
        ReadingsFile(CLAUDE, {PLANTED: one}, {PLANTED: DAY})


# ----------------------------------------------------------------------------- saving
def test_a_reading_that_doesnt_load_back_as_itself_isnt_saved(tmp_path, story):
    one = next(iter(story.values()))
    odd = replace(one, reported=frozenset(one.reported) or frozenset({"1012"}))  # it loads back as a tuple
    assert from_json(to_json(odd)) != odd
    with pytest.raises(ValueError, match=r"^The reading [0-9a-f]{8}\.\.\. doesn't load back as itself"):
        save_readings(tmp_path / "high.json", claude_file([odd]))
    assert not list(tmp_path.iterdir())


def test_a_save_replaces_the_file_in_one_step(tmp_path, story, monkeypatch):
    path = tmp_path / "high.json"
    save_readings(path, claude_file(list(story.values())[:1]))
    seen = []
    real_replace = readings_file.os.replace

    def replace_file(source, target):
        seen.append((Path(source).name, Path(target).name, len(json.loads(Path(target).read_bytes())["readings"])))
        real_replace(source, target)

    monkeypatch.setattr(readings_file.os, "replace", replace_file)
    save_readings(path, claude_file(story))
    assert seen == [("high.json.tmp", "high.json", 1)]  # the old file stayed whole until the one replace
    assert len(load_readings(path).readings) == len(story)
    assert not list(tmp_path.glob("*.tmp"))


def test_a_save_retries_while_onedrive_holds_the_file(tmp_path, story, monkeypatch):
    path = tmp_path / "high.json"
    real_replace, tries, waits = readings_file.os.replace, [], []

    def held_twice(source, target):
        tries.append(1)
        if len(tries) <= 2:
            raise PermissionError(13, "The process cannot access the file")
        real_replace(source, target)

    monkeypatch.setattr(readings_file.os, "replace", held_twice)
    monkeypatch.setattr(readings_file.time, "sleep", waits.append)
    save_readings(path, claude_file(story))
    assert len(tries) == 3 and waits == [0.2, 0.2]
    assert load_readings(path) == claude_file(story)
    assert not list(tmp_path.glob("*.tmp"))


def test_a_save_gives_up_after_five_retries_and_leaves_the_old_file(tmp_path, story, monkeypatch):
    path = tmp_path / "high.json"
    save_readings(path, claude_file(list(story.values())[:1]))
    before = path.read_bytes()
    tries, waits = [], []

    def always_held(source, target):
        tries.append(1)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(readings_file.os, "replace", always_held)
    monkeypatch.setattr(readings_file.time, "sleep", waits.append)
    with pytest.raises(PermissionError):
        save_readings(path, claude_file(story))
    assert len(tries) == 6 and waits == [0.2] * 5
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


# ----------------------------------------------------------------------------- merge
def test_merge_replaces_a_key_read_again_and_keeps_the_rest(story):
    readings = list(story.values())
    old = claude_file(readings[:2])
    again = failed(readings[0].key, Failure.CUT_OFF)  # the same key, read again on a later day
    later = date(2026, 11, 9)
    merged = merge(old, {again.key: (again, later), readings[2].key: (readings[2], later)})
    assert merged.header == old.header
    assert list(merged.readings) == sorted({one.key for one in readings[:3]})
    assert merged.readings[again.key] == again and old.readings[again.key] == readings[0]
    assert merged.dates == {readings[0].key: later, readings[1].key: DAY, readings[2].key: later}
    assert old.dates[readings[0].key] == DAY  # the old file is left as it was
    with pytest.raises(ValueError, match="filed under a key that isn't its own"):
        merge(old, {key("other"): (readings[3], later)})
