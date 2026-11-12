#!/usr/bin/env python3
"""The stand-in demo: the story's endings recorded on the stand-in for the Airwallex sandbox that the tests use, for
while the sandbox can't take a challenge (on Nov 7 it answered every challenge with HTTP 500).

A dev tool, like scripts/spike.py. It plays each ending through disputable.story.record(), the run disputable story
makes on the sandbox, on tests/fake_airwallex.py over tests/sandbox.py instead, with the tests' clock from the story's
Monday, as tests/recordings.py records on the fake. The run replays its own recording and checks it for private data
before anything is written, as a live run does. The manifest says "airwallex": "stand-in", so the demo says on every
page that it replays a run on a stand-in and not a recording of Airwallex. Then the four files of each ending go to
fixtures/story/ending-N (or DIR/ending-N with --out), all or nothing. A recording made on the sandbox is never replaced:
every ending asked for is checked before any is recorded, and a manifest.json that another program holds, or that
can't be read, stops the maker with nothing recorded.

Claude's readings: every reading the story needs, from the claude-source readings files that story.recorded_readings()
finds (the pilot's fixtures/readings/high.json, the story folders, the per-user cache). Nothing is read live, and no
Claude client is built. --no-claude records with no Claude reading: every case Claude would read goes to a person with
code's draft, and Ending 2 then costs $1,528.30. Its readings.json still lists each of those reads, as unread. With
neither, it stops and says to run the pilot first.

It needs no .env, no keys and no network: the fake answers through httpx.MockTransport, and no real transport is built.
It writes no readings file but each recording's own readings.json, and nothing in the per-user data folder. The run's
own files go to a temp folder, which is removed once every ending is written, and kept when the run stops.

Usage
    python scripts/record_standin.py [--ending 1|2|both] [--no-claude] [--out DIR]
        --ending     the ending to record: 1, 2 or both (the default)
        --no-claude  record with no Claude readings: every case Claude would read goes to a person
        --out        write DIR/ending-N instead of fixtures/story/ending-N
    Run it with the project's Python: uv run python scripts/record_standin.py. The tests run main() with --out.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:  # the tests' stand-in for the sandbox: fake_airwallex.py, sandbox.py and recordings.py
    sys.path.insert(0, str(TESTS))

import recordings  # noqa: E402  tests/recordings.py: how the tests record a story on the fake
from disputable import config, recording, story  # noqa: E402
from disputable.config import PROJECT, SetupError  # noqa: E402
from disputable.issuer import IssuerError  # noqa: E402
from disputable.reader import NoReader, Reader  # noqa: E402
from disputable.story import CachedReader, Kept, StoryError  # noqa: E402

STAND_IN = "stand-in"  # the manifest's "airwallex"
CHOICES = {"1": ("1",), "2": ("2",), "both": ("1", "2")}

ABOUT = ("This records the story on the stand-in for the Airwallex sandbox that the tests use. Nothing goes to "
         "Airwallex or to Claude.")
NO_READINGS = "No Claude readings: every case Claude would read goes to a person, with code's draft."
NOT_THERE = ("The Claude readings for the story aren't there yet. Run the pilot first (disputable eval live --pilot "
             "--cap 3), or use --no-claude.")
SANDBOX_KEPT = ("{folder} holds a recording made on the Airwallex sandbox, and a stand-in never replaces one. Delete that "
                "folder first, or use --out with another folder.")
HELD = ("Another program, such as OneDrive, holds the manifest.json in {folder}, so this can't tell what made that "
        "recording. Nothing was recorded. Run it again in a moment.")
UNREADABLE = ("The manifest.json in {folder} can't be read, so this can't tell what made that recording. Nothing was "
              "recorded. Delete that folder first, or use --out with another folder.")
RUN_KEPT = "The run's own files stay in {folder}."


class Unknown(Exception):
    """What made the recording already in a folder can't be told: its manifest is held, or can't be read. The message
    says what to do. Nothing was recorded."""


def main(argv: list[str] | None = None, *, project: Path = PROJECT, data: Path | None = None,
         say: Callable[[str], object] = print) -> int:
    """0 once every ending asked for is recorded, checked and written; 1 when it stopped, with its line said; 2 when the
    command line was wrong. project: where the readings files are looked for. data: the data folder whose readings
    cache is looked at too: the app's per-user folder unless given, since no .env is read."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:  # argparse said its own line: a usage error, or --help
        return stop.code if isinstance(stop.code, int) else 1
    names = CHOICES[args.ending]
    out = recording.FOLDER if args.out is None else Path(args.out)
    say(ABOUT)
    for name in names:  # before anything is recorded, so no ending is written when one can't be
        folder = out / f"ending-{name}"
        try:
            kind = _kind(folder)
        except Unknown as unknown:
            say(str(unknown))
            return 1
        if kind == "sandbox":
            say(SANDBOX_KEPT.format(folder=folder))
            return 1
    recorded: Mapping[str, Kept] = {}
    if args.no_claude:
        say(NO_READINGS)
    else:
        recorded = story.recorded_readings(data=config.default_data_dir() if data is None else Path(data),
                                           project=Path(project), say=say)
        needed = frozenset().union(*(story.story_keys(story.ENDINGS[name]) for name in names)) - recorded.keys()
        if needed:
            say(NOT_THERE)
            return 1
        say(story.ZERO_READS)
    work = Path(tempfile.mkdtemp(prefix="disputable-standin-"))
    try:
        for name in names:
            done = _record(name, recorded, claude=not args.no_claude, work=work, out=out, say=say)
            say(story.COPIED.format(folder=done.copied))
    except (StoryError, SetupError, IssuerError) as stop:  # a recording's copy that another program held is a SetupError
        say(str(stop))
        say(RUN_KEPT.format(folder=work))
        return 1
    shutil.rmtree(work, ignore_errors=True)
    return 0


def _record(name: str, recorded: Mapping[str, Kept], *, claude: bool, work: Path, out: Path,
            say: Callable[[str], object]) -> story.Recorded:
    """One ending on a fresh stand-in, through story.record(), as recordings.record_on_fake() records it for the
    tests. With Claude's readings, every key the story reads is held, so the reader's limit of 0 live reads is never
    reached: no client is built, and the per-user cache isn't touched."""
    wall = recordings.Ticking()  # the story's Monday: the stand-in's own dates start there
    reader: Reader = NoReader()
    if claude:
        reader = CachedReader(NoReader(), recorded, cache=work / story.CACHE, limit=0, today=lambda: wall().date(),
                              say=say)
    return recordings.record_on_fake(name, reader, source="claude" if claude else "none", root=work, out=out,
                                     airwallex=STAND_IN, wall=wall, say=say)


def _kind(folder: Path) -> str | None:
    """What answered for Airwallex in the recording already in folder, as its manifest says. None when the folder holds
    no manifest, or one that names no kind: then the new copy replaces what is there. A manifest another program holds
    (OneDrive, a virus scanner) is read again, as recording.py retries a held file. Unknown when it stays held, or
    when it can't be read: a sandbox recording can't be told apart then, so nothing may replace it."""
    path = folder / recording.MANIFEST
    for attempt in range(recording.RETRIES + 1):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except (FileNotFoundError, NotADirectoryError):  # no recording there
            return None
        except PermissionError:
            if attempt == recording.RETRIES:
                raise Unknown(HELD.format(folder=folder)) from None
            time.sleep(recording.RETRY_WAIT)
        except (OSError, ValueError):  # it can't be fetched, or it isn't UTF-8
            raise Unknown(UNREADABLE.format(folder=folder)) from None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise Unknown(UNREADABLE.format(folder=folder)) from None
    return data.get("airwallex") if isinstance(data, dict) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="record_standin.py",
                                     description="The story recorded on the tests' stand-in for the Airwallex sandbox.")
    parser.add_argument("--ending", choices=tuple(CHOICES), default="both", help="the ending to record: 1, 2 or both")
    parser.add_argument("--no-claude", action="store_true",
                        help="record with no Claude readings: every case Claude would read goes to a person")
    parser.add_argument("--out", type=Path, metavar="DIR", help="write DIR/ending-N instead of fixtures/story/ending-N")
    return parser


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # a character the terminal can't show prints as ?, never as a crash
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    raise SystemExit(main())
