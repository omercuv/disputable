"""The one readings-file format: readings kept by key, under a header that says what made them.

The eval keeps its live reads in fixtures/readings/<effort>.json, each story folder keeps the readings its run used in
readings.json, and the story run keeps every live read in a per-user cache. The header's source tells a demo what the
banner may say: "claude" (read live by Claude) or "none" (NoReader, with no key or on a --no-claude run: every reading
is NO_KEY). There is no "scripted" source, so a scripted stand-in can never pass for Claude's own reading. Only the
eval's live keep() and the story recorder write source "claude".

    {"format": 1, "source": "claude", "model": "claude-opus-5-5", "effort": "high", "made_by": "eval", "readings": [
    {"date": "2026-11-08", "key": "6df4...", "reading": {...reader.to_json as an object...}},
    ...
    ]}

Loading is strict, as jsonable loads: another format, a missing or extra field, an unknown source, two readings with one
key, a key that isn't its reading's own, or a reading that won't load raises ValueError naming the place, never a value.
Saving checks the same, sorts by key, writes ASCII with LF endings and a final LF, and replaces the file in one step,
retrying while OneDrive holds it. No clock: each reading's date comes from the caller.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from disputable.jsonable import Unloadable
from disputable.reader import Failure, Reading, from_json, to_json

FORMAT = 1
SOURCES = frozenset({"claude", "none"})  # never "scripted": no scripted reading is ever written
MAKERS = frozenset({"eval", "story", "cache"})
FIELDS = ("format", "source", "model", "effort", "made_by", "readings")
ENTRY = frozenset({"date", "key", "reading"})
NOT_READ_LIVE = frozenset({Failure.NO_KEY, Failure.NOT_RECORDED, Failure.CAPPED})  # failures no live Claude read gives
RETRIES, RETRY_WAIT = 5, 0.2  # OneDrive or a virus scanner can hold a file for a moment
_KEY = re.compile(r"[0-9a-f]{64}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Header:
    source: str  # "claude" (read live by Claude) or "none" (NoReader: NO_KEY readings). Never "scripted".
    model: str  # the model the reads asked for; "" for none
    effort: str  # "" for none
    made_by: str  # "eval", "story" or "cache"

    def __post_init__(self) -> None:
        if not all(type(value) is str for value in (self.source, self.model, self.effort, self.made_by)):
            raise ValueError("A readings file's header holds text only.")
        if self.source not in SOURCES:
            raise ValueError("A readings file's source is claude (read live by Claude) or none (no key).")
        if self.made_by not in MAKERS:
            raise ValueError("A readings file is made by the eval, the story run or the cache.")
        if self.source == "claude" and not (self.model and self.effort):
            raise ValueError("A file of Claude's readings names the model and the effort they were read with.")
        if self.source == "none" and (self.model or self.effort):
            raise ValueError("A file of readings made with no key names no model and no effort.")


@dataclass(frozen=True)
class ReadingsFile:
    header: Header
    readings: Mapping[str, Reading]  # by key
    dates: Mapping[str, date]  # by key: the day the reading was made

    def __post_init__(self) -> None:
        if not isinstance(self.header, Header):
            raise TypeError("A readings file's header is a Header.")
        if set(self.readings) != set(self.dates):
            raise ValueError("A readings file has one date for each reading, and no other.")
        for key, reading in self.readings.items():
            if not isinstance(reading, Reading):
                raise TypeError("Each reading in a readings file is a Reading.")
            if reading.key != key:
                raise ValueError(f"{_named(key)} is filed under a key that isn't its own.")
            if type(self.dates[key]) is not date:
                raise ValueError(f"{_named(key)} has a date that isn't a day.")
            if self.header.source == "none" and reading.failure is not Failure.NO_KEY:
                raise ValueError(f"{_named(key)} wasn't made with no key, so it can't be in a file of readings made "
                                 "with no key.")
            if self.header.source == "claude" and reading.failure in NOT_READ_LIVE:
                raise ValueError(f"{_named(key)} wasn't read live by Claude, so it can't be in a file of Claude's "
                                 "readings.")


def load_readings(path: Path, *, want: str | None = None) -> ReadingsFile:
    """The readings file at path, loaded strictly; ValueError names the place, never a value. want: the source the
    caller needs, so want="claude" refuses a file that doesn't hold Claude's own readings. A missing file raises
    FileNotFoundError."""
    if want is not None and want not in SOURCES:
        raise ValueError("want is claude, none or None.")
    path = Path(path)
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} isn't UTF-8 text.") from None
    file = _parse(text, path.name)
    if want == "claude" and file.header.source != "claude":
        raise ValueError(f"{path.name} doesn't hold readings Claude made live, so it can't be used here.")
    if want == "none" and file.header.source != "none":
        raise ValueError(f"{path.name} holds Claude's readings, and this needs the readings made with no key.")
    return file


def save_readings(path: Path, file: ReadingsFile) -> None:
    """Writes the file at path: every reading must load back as itself, sorted by key, ASCII, LF endings and a final
    LF. It writes <path>.tmp, then replaces path with it, retrying 5 times 0.2 s apart while another program (OneDrive)
    holds the file, as the spike's state file does."""
    if not isinstance(file, ReadingsFile):
        raise TypeError("save_readings() saves a ReadingsFile.")
    path = Path(path)
    for key, reading in file.readings.items():
        if from_json(to_json(reading)) != reading:
            raise ValueError(f"{_named(key)} doesn't load back as itself, so the file isn't saved.")
    text = _text(file)
    if _parse(text, path.name) != file:
        raise ValueError(f"{path.name} wouldn't load back as it was given, so it isn't saved.")
    _write(path, text)


def merge(old: ReadingsFile, new: Mapping[str, tuple[Reading, date]]) -> ReadingsFile:
    """old with new's readings in it, by key: a key read again replaces the reading and its date. The header stays."""
    readings, dates = dict(old.readings), dict(old.dates)
    for key, (reading, made) in new.items():
        readings[key], dates[key] = reading, made
    return ReadingsFile(old.header, _frozen(readings), _frozen(dates))


# ----------------------------------------------------------------------------- inside
def _parse(text: str, name: str) -> ReadingsFile:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError(f"{name} isn't valid JSON.") from None
    if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != FORMAT:
        raise ValueError(f"{name} isn't a format {FORMAT} readings file.")
    for field in FIELDS:
        if field not in data:
            raise ValueError(f"{name} has no {field} in its header.")
    if set(data) != set(FIELDS):
        raise ValueError(f"{name} has fields a readings file doesn't have.")
    try:
        header = Header(data["source"], data["model"], data["effort"], data["made_by"])
    except ValueError as problem:
        raise ValueError(f"{name}: {problem}") from None
    if not isinstance(data["readings"], list):
        raise ValueError(f"{name}: its readings aren't a list.")
    readings: dict[str, Reading] = {}
    dates: dict[str, date] = {}
    for number, entry in enumerate(data["readings"]):
        where = f"{name}, readings[{number}]"
        key, reading, made = _entry(entry, where)
        if key in readings:
            raise ValueError(f"{where} has the key of an earlier reading.")
        readings[key], dates[key] = reading, made
    try:
        return ReadingsFile(header, _frozen(readings), _frozen(dates))
    except ValueError as problem:
        raise ValueError(f"{name}: {problem}") from None


def _entry(entry: Any, where: str) -> tuple[str, Reading, date]:
    if not isinstance(entry, dict) or set(entry) != ENTRY:
        raise ValueError(f"{where} doesn't hold exactly a date, a key and a reading.")
    key, day = entry["key"], entry["date"]
    if type(key) is not str or not key:
        raise ValueError(f"{where}: its key isn't text.")
    if type(day) is not str or not _DAY.fullmatch(day):
        raise ValueError(f"{where}: its date isn't a day, written YYYY-MM-DD.")
    try:
        made = date.fromisoformat(day)
    except ValueError:
        raise ValueError(f"{where}: its date isn't a day, written YYYY-MM-DD.") from None
    try:
        reading = from_json(json.dumps(entry["reading"], allow_nan=False))
    except (Unloadable, TypeError, ValueError):  # the reasons quote values, so they stay out of the message
        raise ValueError(f"{where}: its reading won't load.") from None
    if reading.key != key:
        raise ValueError(f"{where}: its key isn't the key its reading holds.")
    return key, reading, made


def _text(file: ReadingsFile) -> str:
    """One reading a line, sorted by key, under the header: ASCII, LF endings and a final LF."""
    header = file.header
    head = json.dumps({"format": FORMAT, "source": header.source, "model": header.model, "effort": header.effort,
                       "made_by": header.made_by}, ensure_ascii=True)
    lines = [json.dumps({"date": file.dates[key].isoformat(), "key": key, "reading": json.loads(to_json(reading))},
                        ensure_ascii=True, allow_nan=False) for key, reading in sorted(file.readings.items())]
    body = "\n" + ",\n".join(lines) + "\n" if lines else ""
    return f'{head[:-1]}, "readings": [{body}]}}\n'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("ascii"))
    for attempt in range(RETRIES + 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == RETRIES:
                _remove(temporary)
                raise
            time.sleep(RETRY_WAIT)


def _remove(temporary: Path) -> None:
    try:
        temporary.unlink(missing_ok=True)
    except OSError:  # held too: .gitignore keeps it out of the repo
        pass


def _frozen(by_key: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(sorted(by_key.items())))


def _named(key: object) -> str:
    """A reading as a message names it: by its key's first 8 characters when the key is a SHA-256, which says nothing
    private, else only as a reading."""
    return f"The reading {key[:8]}..." if isinstance(key, str) and _KEY.fullmatch(key) else "A reading"
