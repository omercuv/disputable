"""The live story run: one ending played on the Airwallex sandbox and recorded for the demo.

record() plays the story's beats on a database of its own: the bank's steps through the Issuer, the agent's passes
through loop.run_pass(), and Priya's approvals through Service.check_approval() and approve_at() at the journaled
approve moment, the approval screen's own path, once each plan matches what the story expects of it. A plan that doesn't
stops the run, and --lenient records it as it is. After each sending pass it reads again, 3 seconds apart, while a
response doesn't show yet, up to 5 times. It stops, and copies nothing, when Claude was busy (a busy read can't be
replayed the same way), when Claude's read failed (the recording would show code's hand-off as Claude's reading;
--lenient records it, unless no Claude readings file may hold it), when a live reading holds what the public repo can't,
when a pass names a problem, when the bank's step refuses, or when the story doesn't end.

At the end it writes the recording into the run's own folder, replays it into a second database and compares every case
and every approval with the live run after each step (this catches a scrub that changed an answer the live run relied
on), runs the privacy check with every secret the run learned, and only then copies the four files into
fixtures/story/ending-<n>/, all or nothing. The manifest keeps the run's real totals; a total that differs from the
story's is printed.

CachedReader reads a case live only when no readings file holds its key: the eval's fixtures/readings/high.json, every
fixtures/story/*/readings.json and the per-user cache <data>/readings-cache.json. A reading the privacy check refuses is
never reused, so its key counts as one to read. Each live read that may go into the repo goes into the cache at once, so
a run that stops never pays twice for a reading; one that may not stops the run right after it. run() counts the story's
keys that no file holds, says "0 live reads needed." when there are none, and otherwise asks first
(config.confirm_spend) and holds the spend to the cap. A read a cap stopped stops the run. When --cap stopped it, the
line says to run the story again with a higher --cap. When the cap for one read stopped it (spend.READ_CAP, which the
story command has no flag for), the line says that a higher --cap won't help. A plan the story doesn't expect names the
file its reading came from: every run uses that reading again.

scripts/record_standin.py runs the same record() on the tests' stand-in for the sandbox (tests/fake_airwallex.py),
with airwallex="stand-in" in the manifest, so the demo says so: a fallback for when the sandbox can't take a challenge.

The CLI uses it like this:
    disputable story --ending 1: return run("1", settings, claude=not args.no_claude, lenient=args.lenient,
                                            cap=args.cap, yes=args.yes)
"""

from __future__ import annotations

import functools
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from disputable import airwallex, approvals, lifecycle, privacy, prompts, readings_file, recording, rounds, spend
from disputable.airwallex import AgentClient, Credentials, Session
from disputable.approvals import Tier
from disputable.clock import RecordingClock, ReplayMismatch
from disputable.clock import wall as wall_clock
from disputable.config import PROJECT, Settings, SetupError, confirm_spend
from disputable.db import Database, PassLock
from disputable.desk import ReaderInfo
from disputable.engine import Action
from disputable.fees import CENT, ZERO, usd
from disputable.issuer import MONDAY_CASES, ROUND_TWO, Issuer, Opened, pay_in_browser
from disputable.lifecycle import CaseRecord, Phase
from disputable.loop import PassReport, run_pass, who
from disputable.privacy import Allowed, Scrubber
from disputable.reader import (WHY, ClaudeReader, Failure, NoReader, Reader, ReaderBusy, Reading, ReadRequest,
                               claude_client, failed, to_json)
from disputable.readings_file import NOT_READ_LIVE, Header, ReadingsFile
from disputable.recording import Beat, Manifest, Recording, RecordingTransport, Replay, Sink, State
from disputable.rounds import BUSY_NOTE
from disputable.seed.fernhill import ENDING_2, FERNHILL, FIRST_PACKAGE_FILE, SECOND_PACKAGE_FILE
from disputable.seed.suite import SUITE
from disputable.service import LIVE_DB, PASS_LOCK, Service
from disputable.store import Store

APPROVER = "Priya"
STORES: Mapping[str, Store] = {"fernhill": FERNHILL, "ending_2": ENDING_2}
CACHE = "readings-cache.json"  # in the data folder: every live read the story run made
EVAL_READINGS = PROJECT / "fixtures" / "readings" / "high.json"
LOG = "exchanges.jsonl"  # the run's own log, in its folder; the recording's copy is checked before it is used
SETTLE_PASSES, SETTLE_WAIT = 5, 3.0  # the sandbox can take a moment to show a response

# The story's times and sentences, as the demo's bar shows them.
MONDAY = "Monday 9:{minute:02d}"
LATER = "Three weeks later"
WEEK_LATER = "A week later"
READS = "The agent reads the queue."
SENDS = "The agent sends what is ready."
OPENS = "The bank opens {count} disputes: {cases}."
PUSHES = "The bank turns down the headphones evidence and opens a chargeback, due in {days} days. Its comment: \"{comment}\""
RULES = {"MERCHANT": "The bank rules for the store.", "CUSTOMER": "The bank rules for the customer."}
APPROVES_DRAFT = "{by} approves code's draft on order #{order}: {sends}."
APPROVES_CHALLENGE = "{by} approves the challenge on order #{order}."
APPROVES_ACCEPT = "{by} approves code's accept on order #{order}: {sends}."
APPROVES = "{by} approves the plan on order #{order}: {sends}."
COUNTS = {1: "one", 2: "two", 3: "three", 4: "four"}

# What the run prints.
STEP = "Step {n}, {story}: {text}"
PASSED = "  Read {read}, sent {sent}, decided {decided}."
ZERO_READS = "0 live reads needed."
WHAT = ("The story run of Ending {name} reads {count} case{s} live with Claude ({model}, effort {effort}), with your "
        "key.")
SPENT = "Claude's live reads cost {spent} in {requests} request{s}."
TOTAL_DIFFERS = "This ending cost {total}, not the {expected} the story expects. The manifest keeps the real total."
RECORDED = "Recorded Ending {name}: {beats} steps, {exchanges} calls and {readings} readings. It cost {total}."
COPIED = "The recording is in {folder}."
UNREADABLE_FILE = "{name} can't be read, so its readings aren't reused: {problem}"
REFUSED_FILE = "A reading in {name} can't go into the public repo, so it isn't used again: {problem}"
CACHE_FAILED = "The live reading couldn't be written to {name}, so a later run reads it again."

# Why the run stops. Nothing is copied into the repo then.
NO_ENDING = "There's no ending {name}. The endings are 1 and 2."
NO_CLAUDE = ("No Claude API key is set, and this run reads {count} case{s} live. Add ANTHROPIC_API_KEY to .env, or "
             "record the story with --no-claude: every read case then goes to a person.")
NEED_CAP = "This run reads {count} case{s} live with Claude. Give it a dollar cap, such as --cap 3."
SURPRISE = "{who}, round {round}: the plan isn't what the story expects. It is {got}, and the story expects {expected}."
OTHER_FILES = "{who}, round {round}: the plan isn't what the story expects. It sends other files than the story expects."
LOOK = (" Look at it on the screens (disputable serve --data \"{folder}\"). Run the story again with --lenient to record "
        "it as it is.")  # quoted: a data folder may hold a space, as the macOS default does
REUSED = " Claude's reading of this case comes from {path}, and every run uses it again."
FROM_CACHE = " Delete that file to read the case again."
FAILED_READ = "{who}: Claude's read failed ({why}), so this recording wouldn't show Claude's own reading."
READ_AGAIN = " Run the story again: the readings made so far are reused at no cost."
CAP_AGAIN = " Run the story again with a higher --cap: the readings made so far are reused at no cost."
READ_CAPPED = ("{who}: the cap for one read ({cap}) stopped Claude's read, so a higher --cap won't help. Look at the case "
               "before you run the story again.")
OR_LENIENT = " To record the failed read as it is, run the story with --lenient."
PRIVATE_READING = "Claude's reading of {who} holds what can't go into the public repo, so the run stopped:"
NOT_KEPT = "That reading wasn't kept, so the next run reads the case again and asks first. Nothing was copied."
BUSY = ("Claude was busy, so this run can't be replayed the same way. Run the story again: the readings made so far are "
        "reused at no cost.")
OVER_LIMIT = ("This run needed more live Claude reads than the {limit} it was allowed, so it stopped before it read more. "
              "Run the story again: the readings made so far are reused at no cost.")
OVER_LIMIT_WHY = "the run's live reads are used up"
PROBLEM = "The run stopped: a pass found a problem. {line} Run the story again."
SKIPPED = "Another pass held this run's lock, so the run stopped."
NOT_APPROVED = "{who}: the script's approval was refused ({key}).{message}"
NOT_SHOWN = "Airwallex still doesn't show this app's response on {who} after {count} more reads. Run the story again."
NOT_ENDED = "The story didn't end: {who} is at {phase}. Run the story again."
DOESNT_REPLAY = "The recording doesn't replay as the live run went, at step {n}: {why}"
DIFFERS = "The recording doesn't replay as the live run went: after step {n}, {what} differs."
LEFT = "The replay never made {count} of the recorded agent calls, the first {first}."
PRIVATE = "The recording holds what can't go into the public repo, so it wasn't copied:"
NOTHING_COPIED = " Nothing was copied."


class StoryError(Exception):
    """The story run stopped. The message says why and what to do. Nothing was copied into the repo."""


# ----------------------------------------------------------------------------- the endings
@dataclass(frozen=True)
class Expect:
    """What Priya's screen must show before the script approves, so a surprise is never approved blindly."""
    case_id: str  # the seed case the bank opened the dispute for
    round_number: int
    action: Action
    tier: Tier
    file_hashes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Ending:
    name: str  # "1" or "2"
    store_name: str  # "fernhill" or "ending_2"
    ruling: str | None  # "MERCHANT", or None when the round-two accept ends the story
    reads: tuple[str, ...]  # the eval's labels of the story's reads, whose keys a run may find kept already
    with_claude: tuple[Expect, ...]  # Claude's reading: B 11's expectations
    no_claude: tuple[Expect, ...]  # NoReader: every read round goes to a person, with code's draft
    total: Decimal  # the story's own total with Claude, and with no key
    total_no_claude: Decimal

    @property
    def store(self) -> Store:
        return STORES[self.store_name]

    def expects(self, claude: bool) -> tuple[Expect, ...]:
        return self.with_claude if claude else self.no_claude

    def expected_total(self, claude: bool) -> Decimal:
        return self.total if claude else self.total_no_claude


_FIRST_FILE, _SECOND_FILE = frozenset({FIRST_PACKAGE_FILE}), frozenset({SECOND_PACKAGE_FILE})
_MONDAY = (Expect("ear_tips", 1, Action.ACCEPT, Tier.ALONE), Expect("earbuds", 1, Action.ACCEPT, Tier.HAND_OFF))
ENDINGS: Mapping[str, Ending] = {
    "1": Ending("1", "fernhill", "MERCHANT", ("earbuds", "headphones", "headphones_round_2"),
                (*_MONDAY, Expect("headphones", 1, Action.CHALLENGE, Tier.ONE_APPROVAL, _FIRST_FILE),
                 Expect("headphones", 2, Action.CHALLENGE, Tier.ONE_APPROVAL, _SECOND_FILE)),
                (*_MONDAY, Expect("headphones", 1, Action.CHALLENGE, Tier.HAND_OFF, _FIRST_FILE),
                 Expect("headphones", 2, Action.CHALLENGE, Tier.HAND_OFF, _SECOND_FILE)),
                Decimal("279.30"), Decimal("279.30")),
    "2": Ending("2", "ending_2", None, ("earbuds", "headphones", "ending_2"),
                (*_MONDAY, Expect("headphones", 1, Action.CHALLENGE, Tier.ONE_APPROVAL, _FIRST_FILE),
                 Expect("headphones", 2, Action.ACCEPT, Tier.ONE_APPROVAL)),
                (*_MONDAY, Expect("headphones", 1, Action.CHALLENGE, Tier.HAND_OFF, _FIRST_FILE),
                 Expect("headphones", 2, Action.ACCEPT, Tier.HAND_OFF)),
                Decimal("1512.30"), Decimal("1528.30")),
}


@functools.cache
def allowed() -> Allowed:
    """The seed's own made-up values: what the privacy check lets stand, and the scrubber's allowed addresses."""
    return Allowed.of(FERNHILL, ENDING_2, SUITE)


def story_keys(ending: Ending) -> frozenset[str]:
    """The reading keys the ending's reads have, as the eval computes them: round two dated from round one, as
    lifecycle.as_case() dates it."""
    from disputable.evals import sites

    found = {site.label: site for site in sites.read_sites()}
    return frozenset(sites.request_for(found[label]).key for label in ending.reads)


# ----------------------------------------------------------------------------- readings
@dataclass(frozen=True)
class Kept:
    """A reading Claude made live, the day it was made, and the readings file that holds it."""
    reading: Reading
    day: date
    path: Path


def recorded_readings(*, data: Path, project: Path = PROJECT, say: Callable[[str], object] = print) -> dict[str, Kept]:
    """Every reading Claude made live that a readings file holds, by key, with the day it was made and its file: the
    eval's, every story folder's and the per-user cache. Readings that failed aren't reused, nor one that the public
    repo can't hold (refused(), said once each), and a file of another source is passed over. The first file that holds
    a key gives it."""
    paths = [project / "fixtures" / "readings" / "high.json",
             *sorted((project / "fixtures" / "story").glob("*/readings.json")), Path(data) / CACHE]
    found: dict[str, Kept] = {}
    for path in paths:
        try:
            file = readings_file.load_readings(path)
        except FileNotFoundError:
            continue
        except ValueError as problem:
            say(UNREADABLE_FILE.format(name=path.name, problem=problem))
            continue
        if file.header.source != "claude":
            continue
        for key, reading in file.readings.items():
            if reading.failure is not None or key in found:
                continue
            problems = refused(reading)
            if problems:
                say(REFUSED_FILE.format(name=path.name, problem=problems[0]))
                continue
            found[key] = Kept(reading, file.dates[key], path)
    return found


def refused(reading: Reading) -> list[str]:
    """What keeps a reading out of a story recording, as places and kinds, never values: [] when the public repo may
    hold it. It is the check the recording's readings.json gets before it is copied (privacy.check_files), so a reading
    it refuses would stop every run at its end."""
    return privacy.reading_problems(to_json(reading), allowed(), exact=True)


def _who(request: ReadRequest) -> str:
    """How a line names the case a request reads: "Order #1109", as loop.who() names its record."""
    number = request.case.order_number
    return f"Order #{number}" if number else f"Dispute {request.dispute.id}"


class _BudgetReader(spend.BudgetReader):
    """spend.BudgetReader as the story run holds Claude to the meter's two caps: a read a cap stopped is a failed read
    (CAPPED), as there. It also notes which cap stopped it, so the run's line can tell them apart: --cap is the user's
    to raise, and the cap for one read is fixed."""

    def __init__(self, reader: Reader, meter: spend.Meter) -> None:
        super().__init__(reader, meter)
        self._stops: dict[str, spend.BudgetReached] = {}

    def read(self, request: ReadRequest) -> Reading:
        self._stops.pop(request.key, None)
        try:
            with self.meter.read(request.key[:8]):
                return self.reader.read(request)
        except spend.BudgetReached as stop:
            self._stops[request.key] = stop
            return failed(request.key, Failure.CAPPED)

    def capped(self, key: str) -> spend.BudgetReached | None:
        """What stopped the last read of this key, when a cap did: its scope is "run" (--cap) or "read"."""
        return self._stops.get(key)


class CachedReader:
    """A reading Claude made earlier for the same key is used again at no cost. Any other key is read live, at most
    limit times, and a reading that didn't fail goes into the per-user cache at once, when the public repo may hold it.
    One it may not is given to the round but never kept: refused names it, and the run stops after the pass. Past the
    limit it reads nothing: the round waits as if Claude were busy, and over_limit tells the run to stop. It keys as the
    live reader does."""

    def __init__(self, live: Reader, recorded: Mapping[str, Kept], *, cache: Path, limit: int,
                 today: Callable[[], date], say: Callable[[str], object] = print) -> None:
        self.live, self.cache, self.limit, self._today, self._say = live, Path(cache), limit, today, say
        self._recorded = dict(recorded)
        self._made: dict[str, date] = {}
        self._from: dict[str, Path] = {}
        self.reads = 0  # live reads made
        self.over_limit = False
        self.refused: list[tuple[str, tuple[str, ...]]] = []  # (the case, the privacy lines) of each reading not kept

    @property
    def profile(self) -> prompts.StoreProfile:
        return getattr(self.live, "profile", prompts.FERNHILL)

    @property
    def effort(self) -> str:
        return getattr(self.live, "effort", prompts.EFFORT)

    def read(self, request: ReadRequest) -> Reading:
        kept = self._recorded.get(request.key)
        if kept is not None:
            self._made[request.key], self._from[request.key] = kept.day, kept.path
            return kept.reading
        if self.reads >= self.limit:
            self.over_limit = True
            raise ReaderBusy(OVER_LIMIT_WHY)
        self.reads += 1
        reading = self.live.read(request)  # ReaderBusy comes out: the round waits, and the run stops
        day = self._today()
        self._made[request.key] = day
        if reading.failure is None:
            problems = refused(reading)
            if problems:
                self.refused.append((_who(request), tuple(problems)))
            else:
                self._recorded[request.key] = Kept(reading, day, self.cache)
                if self._keep(reading, day):
                    self._from[request.key] = self.cache
        return reading

    def made(self, key: str) -> date | None:
        """The day the reading given for this key was made."""
        return self._made.get(key)

    def source(self, key: str) -> Path | None:
        """The readings file that holds the reading given for this key, which every later run uses again, or None."""
        return self._from.get(key)

    def capped(self, key: str) -> spend.BudgetReached | None:
        """What stopped the live read of this key, when a cap of the live reader's did (_BudgetReader), else None."""
        capped = getattr(self.live, "capped", None)
        return capped(key) if callable(capped) else None

    def _keep(self, reading: Reading, day: date) -> bool:
        """The reading in the cache: whether it was written."""
        header = Header("claude", prompts.MODEL, self.effort, "cache")
        try:
            try:
                old = readings_file.load_readings(self.cache, want="claude")
            except (FileNotFoundError, ValueError):  # none yet, or one this cache can't keep: it starts again
                old = None
            if old is None or old.header != header:
                old = ReadingsFile(header, {}, {})
            readings_file.save_readings(self.cache, readings_file.merge(old, {reading.key: (reading, day)}))
        except (OSError, ValueError):
            self._say(CACHE_FAILED.format(name=self.cache.name))
            return False
        return True


class _Kept:
    """The run's reader as the loop sees it: each reading it gives is kept by its request's key, with the day it was
    made and the case it read, for readings.json and the run's lines. Its profile and effort are the reader's own, so
    the keys stay the same."""

    def __init__(self, reader: Reader, today: Callable[[], date]) -> None:
        self.reader, self._today = reader, today
        self.kept: dict[str, tuple[Reading, date]] = {}
        self.names: dict[str, str] = {}  # key -> "Order #1109"

    @property
    def profile(self) -> prompts.StoreProfile:
        return getattr(self.reader, "profile", prompts.FERNHILL)

    @property
    def effort(self) -> str:
        return getattr(self.reader, "effort", prompts.EFFORT)

    @property
    def over_limit(self) -> bool:
        return bool(getattr(self.reader, "over_limit", False))

    @property
    def refused(self) -> list[tuple[str, tuple[str, ...]]]:
        return list(getattr(self.reader, "refused", ()))

    def source(self, key: str) -> Path | None:
        source = getattr(self.reader, "source", None)
        return source(key) if callable(source) else None

    def capped(self, key: str) -> spend.BudgetReached | None:
        capped = getattr(self.reader, "capped", None)
        return capped(key) if callable(capped) else None

    def read(self, request: ReadRequest) -> Reading:
        reading = self.reader.read(request)
        made = getattr(self.reader, "made", None)
        day = made(request.key) if callable(made) else None
        self.kept[request.key] = (reading, day or self._today())
        self.names[request.key] = _who(request)
        return reading

    def readings(self, source: str) -> ReadingsFile:
        claude = source == "claude"
        header = Header(source, prompts.MODEL if claude else "", self.effort if claude else "", "story")
        return ReadingsFile(header, {key: reading for key, (reading, _) in self.kept.items()},
                            {key: day for key, (_, day) in self.kept.items()})


# ----------------------------------------------------------------------------- the run
@dataclass(frozen=True)
class Recorded:
    """What record() made: the checked recording in the run's own folder, and where it was copied."""
    folder: Path  # <work>/story-<run>/recording: the four files, checked
    copied: Path | None  # <out>/ending-<n>, or None when nothing was to be copied
    recording: Recording
    database: Path  # the live run's own database, for disputable serve --data <its folder>
    total: Decimal
    surprises: tuple[str, ...]  # --lenient: what wasn't as the story expects, recorded as it was


def record(ending: Ending, *, reader: Reader, source: str, credentials: Credentials, work: Path,
           transport: httpx.BaseTransport, pay: Callable[[str, str], None], out: Path | None = None,
           lenient: bool = False, approver: str = APPROVER, airwallex: str = "sandbox",
           wall: Callable[[], datetime] = wall_clock, sleep: Callable[[float], None] = time.sleep,
           say: Callable[[str], object] = print, allowed_values: Allowed | None = None) -> Recorded:
    """One ending, live, recorded, replayed, checked, and copied into out/ending-<n> when out is given. source: what
    read the cases, "claude" or "none" (NoReader), as the readings file says it. airwallex: what answered for Airwallex,
    as the manifest says it: "sandbox" (disputable story), or "stand-in" (scripts/record_standin.py, on the tests'
    fake). transport: the one inner transport under both sessions. pay(payment_id, client_secret): the customer's
    checkout. StoryError when the run stops, with nothing copied; IssuerError when a bank step refuses."""
    if source not in readings_file.SOURCES:
        raise ValueError("source is claude or none.")
    if airwallex not in recording.AIRWALLEX:
        raise ValueError("airwallex is sandbox or stand-in.")
    claude, store = source == "claude", ending.store
    values = allowed() if allowed_values is None else allowed_values
    run_id = secrets.token_hex(4)
    folder = Path(work) / f"story-{run_id}"
    clock = RecordingClock(wall)
    db = Database(folder / LIVE_DB, stamp=clock.peek)
    for key, value in (("mode", "record"), ("run", run_id), ("store", ending.store_name), ("ending", ending.name)):
        db.set_meta(key, value)
    scrubber = Scrubber([credentials.client_id, credentials.api_key], values.addresses)
    sink = Sink(folder / LOG, scrubber, clock=wall)
    agent = Session(credentials, actor="agent", transport=RecordingTransport(transport, sink), sleep=sleep)
    bank = Session(credentials, actor="bank", transport=RecordingTransport(transport, sink), sleep=sleep)
    kept = _Kept(reader, lambda: _utc(wall()).date())
    info = ReaderInfo(prompts.FERNHILL, kept.effort, source, prompts.MODEL if claude else "", None, None)
    service = Service(db=db, store=store, clock=clock, info=info, approver=approver, reader=kept,
                      client=AgentClient(agent), lock=PassLock(folder / PASS_LOCK))
    story = _Story(ending, ending.expects(claude), service, Issuer(bank, store, db, run=run_id, pay=pay, clock=wall,
                                                                     sleep=sleep), sink, clock, kept, folder,
                   claude=claude, lenient=lenient, approver=approver, sleep=sleep, say=say)
    try:
        story.play()
    finally:
        agent.close()
        bank.close()
    records = db.load().records.values()
    total = sum((lifecycle.cost(record) for record in records), ZERO)
    expected = ending.expected_total(claude)
    if total != expected:
        say(TOTAL_DIFFERS.format(total=usd(total), expected=usd(expected)))
    readings = kept.readings(source)
    lines = sink.lines()
    days = sorted(readings.dates.values())
    manifest = Manifest(recording.FORMAT, ending.name, ending.store_name, run_id, story.beats[0].at, airwallex, approver,
                        source, readings.header.model, readings.header.effort, days[0] if days else None,
                        days[-1] if days else None, recording.formats(), _cents(total),
                        _cents(sum((lifecycle.at_risk(record) for record in records), ZERO)),
                        sum(record.closed is not None for record in records), len(story.beats), len(lines),
                        len(readings.readings))
    staging = folder / "recording"
    recording.save(Recording(manifest, tuple(story.beats), lines, readings), staging)
    loaded = _self_check(staging, store, folder / "replay", story.states)
    problems = recording.check_privacy(staging, values, scrubber.secrets)
    if problems:
        raise StoryError("\n".join([PRIVATE, *problems]))
    copied = None
    if out is not None:
        copied = Path(out) / f"ending-{ending.name}"
        recording.copy(staging, copied)
    say(RECORDED.format(name=ending.name, beats=manifest.beats, exchanges=manifest.exchanges,
                        readings=manifest.readings, total=usd(total)))
    return Recorded(staging, copied, loaded, db.path, total, tuple(story.surprises))


def _self_check(staging: Path, store: Store, folder: Path, live: list[State]) -> Recording:
    """The recording, loaded back and replayed into a fresh database: after each step, every case and every approval
    must be as the live run left them, and no recorded agent call may be left over."""
    loaded = recording.load(staging)
    replay = Replay(loaded, store=store, folder=folder)
    for index, beat in enumerate(loaded.beats):
        try:
            replay.play(beat)
        except ReplayMismatch as mismatch:
            raise StoryError(DOESNT_REPLAY.format(n=beat.n, why=mismatch) + NOTHING_COPIED) from None
        found = recording.differences(live[index], replay.state())
        if found:
            raise StoryError(DIFFERS.format(n=beat.n, what=found[0]) + NOTHING_COPIED)
    left = replay.transport.left()
    if left:
        raise StoryError(LEFT.format(count=len(left), first=left[0]) + NOTHING_COPIED)
    return loaded


class _Story:
    """The beats of one ending, played live: B 9.3's table, with a settle pass wherever the sandbox lags."""

    def __init__(self, ending: Ending, expects: tuple[Expect, ...], service: Service, issuer: Issuer, sink: Sink,
                 clock: RecordingClock, reader: _Kept, folder: Path, *, claude: bool, lenient: bool, approver: str,
                 sleep: Callable[[float], None], say: Callable[[str], object]) -> None:
        self.ending, self.service, self.issuer, self.sink, self.clock, self.reader = (ending, service, issuer, sink,
                                                                                     clock, reader)
        self.expects = {(expect.case_id, expect.round_number): expect for expect in expects}
        self.folder, self.lenient, self.approver, self.sleep, self.say = folder, lenient, approver, sleep, say
        self.claude = claude  # Claude reads the cases: a failed read is then a surprise, never Claude's reading
        self.beats: list[Beat] = []
        self.states: list[State] = []  # after each beat, for the self-check
        self.surprises: list[str] = []
        self.cases: dict[str, str] = {}  # dispute id -> the seed case the bank opened it for
        self.headphones = ""
        self._checked: set[tuple[str, int]] = set()
        self._failed: set[str] = set()  # the reading keys of failed reads already said
        self._minute = 4  # Monday's approvals are at 9:05, then each pass a minute later

    def play(self) -> None:
        store = self.ending.store
        opened = self._bank(MONDAY.format(minute=0), _opens(store), self.issuer.monday)
        self.cases = {case.dispute_id: case.case_id for case in opened}
        self.headphones = next(case.dispute_id for case in opened if case.case_id == "headphones")
        self._pass(MONDAY.format(minute=0), READS)
        self._approvals(self._monday)
        self._sends(self._monday)
        round_two = store.case(ROUND_TWO)
        self._bank(LATER, PUSHES.format(days=round_two.due_in.days, comment=round_two.issuer_comment),
                   lambda: self.issuer.push_back(self.headphones))
        self._pass(LATER, READS)
        self._approvals(lambda: LATER)
        self._sends(lambda: LATER)
        ruling = self.ending.ruling
        if ruling is not None and self._record(self.headphones).closed is None:
            self._bank(WEEK_LATER, RULES[ruling], lambda: self.issuer.rule(self.headphones, ruling))
            self._pass(WEEK_LATER, READS)
            for _ in range(SETTLE_PASSES):
                if self._record(self.headphones).closed is not None:
                    break
                self.sleep(SETTLE_WAIT)
                self._pass(WEEK_LATER, READS)
        for record in sorted(self.service.db.load().records.values(), key=lambda record: record.dispute_id):
            if record.closed is None:
                raise StoryError(NOT_ENDED.format(who=who(record), phase=lifecycle.phase(record).value) + NOTHING_COPIED)

    # ------------------------------------------------------------------------- beats
    def _bank(self, story: str, text: str, act: Callable[[], Any]) -> Any:
        n = self._start(story, text)
        at = self.clock.peek()
        done = act()
        opened = done if isinstance(done, tuple) and all(isinstance(case, Opened) for case in done) else ()
        self._keep(Beat(n, "bank", story, at, text, self.clock.take(), payments=tuple(case.payment for case in opened),
                        dates=tuple(case.date for case in opened)))
        return done

    def _pass(self, story: str, text: str) -> PassReport:
        n = self._start(story, text)
        at = self.clock.peek()
        report = run_pass(self.service)
        self._keep(Beat(n, "pass", story, at, text, self.clock.take()))
        self.say(PASSED.format(read=len(report.read), sent=len(report.sent), decided=len(report.decided)))
        self._after(report)
        return report

    def _approvals(self, story: Callable[[], str]) -> None:
        """Priya approves each plan that waits for her, in the story's order of cases, as the approval screen does. The
        approvals share one story time: the demo may play them in any order, and its clock never goes back."""
        order = {case_id: index for index, case_id in enumerate(MONDAY_CASES)}
        waiting = [record for record in self.service.db.load().records.values()
                   if lifecycle.phase(record) is Phase.APPROVE]
        if not waiting:
            return
        at = story()  # one story time for the whole window
        for record in sorted(waiting, key=lambda record: (order.get(self.cases.get(record.dispute_id, ""), 99),
                                                          record.dispute_id)):
            self._approve(record.dispute_id, at)

    def _approve(self, dispute_id: str, story: str) -> None:
        board = self.service.board()
        record = board.records[dispute_id]
        plan = record.rounds[-1].plan
        step = rounds.current(record, board.store, board.saved, board.now, profile=board.reader.profile,
                              effort=board.reader.effort)
        text, key = recording.plan_text(plan), step.reading.key if step.reading is not None else ""
        line = _approves(self.approver, record)
        n = self._start(story, line)
        at = self.clock.peek()
        checked = self.service.check_approval(dispute_id, text, key, self.clock.peek())
        result = checked.result if not checked.result.ok else self.service.approve_at(
            dispute_id, text, key, self.clock.now(f"approve:{dispute_id}"))
        if result.key != "approved":
            raise StoryError(NOT_APPROVED.format(who=who(record), key=result.key,
                                                 message=f" {result.message}" if result.message else "")
                             + NOTHING_COPIED)
        self._keep(Beat(n, "approve", story, at, line, self.clock.take(), dispute_id=dispute_id,
                        plan_sha256=approvals.plan_sha256(plan), by=self.approver))

    def _sends(self, story: Callable[[], str]) -> None:
        """The sending pass, then a pass 3 seconds later while a response doesn't show yet, up to 5."""
        self._pass(story(), SENDS)
        for _ in range(SETTLE_PASSES):
            if not self._sent():
                return
            self.sleep(SETTLE_WAIT)
            self._pass(story(), READS)
        waiting = self._sent()
        if waiting:
            raise StoryError(NOT_SHOWN.format(who=who(waiting[0]), count=SETTLE_PASSES) + NOTHING_COPIED)

    def _start(self, story: str, text: str) -> int:
        n = len(self.beats) + 1
        self.sink.beat = n
        self.say(STEP.format(n=n, story=story, text=text))
        return n

    def _keep(self, beat: Beat) -> None:
        self.beats.append(beat)
        self.states.append(recording.state_of(self.service.db))

    def _monday(self) -> str:
        self._minute += 1
        return MONDAY.format(minute=self._minute)

    # ------------------------------------------------------------------------- after each pass
    def _after(self, report: PassReport) -> None:
        if report.skipped:
            raise StoryError(SKIPPED + NOTHING_COPIED)
        if self.reader.over_limit:
            raise StoryError(OVER_LIMIT.format(limit=getattr(self.reader.reader, "limit", 0)))
        refused = self.reader.refused  # the cache didn't keep it, and no recording may hold it
        if refused:
            name, lines = refused[0]
            raise StoryError("\n".join([PRIVATE_READING.format(who=name), *lines, NOT_KEPT]))
        records = self.service.db.load().records
        busy = any(line.endswith(BUSY_NOTE) for line in report.problems) or any(
            self.service.db.saved.busy(record.dispute_id, len(record.rounds)) for record in records.values())
        if busy:
            raise StoryError(BUSY)
        if report.problems:
            raise StoryError(PROBLEM.format(line=report.problems[0]) + NOTHING_COPIED)
        if self.claude:
            self._failed_reads()
        self._check(records)

    def _failed_reads(self) -> None:
        """A read that failed while Claude reads the cases: its round goes to a person with code's draft, which the
        story may expect anyway (the earbuds' does), so only this says it. --lenient records it as a surprise, unless no
        readings file of Claude's may hold it (a read a cap stopped holds no answer of Claude's). A read the fixed cap
        for one read stopped says that a higher --cap won't help: that read may need as much on the next run. Any other
        capped read says to run the story again with a higher --cap, which reuses the readings made so far."""
        for key, (reading, _) in self.reader.kept.items():
            if reading.failure is None or key in self._failed:
                continue
            self._failed.add(key)
            who = self.reader.names.get(key, "A case")
            line = FAILED_READ.format(who=who, why=WHY[reading.failure])
            recordable = reading.failure not in NOT_READ_LIVE
            if not (self.lenient and recordable):
                stop = self.reader.capped(key) if reading.failure is Failure.CAPPED else None
                if stop is not None and stop.scope == "read":
                    raise StoryError(READ_CAPPED.format(who=who, cap=usd(stop.cap)) + NOTHING_COPIED)
                again = CAP_AGAIN if reading.failure is Failure.CAPPED else READ_AGAIN
                raise StoryError(line + again + (OR_LENIENT if recordable else "") + NOTHING_COPIED)
            self.surprises.append(line)
            self.say(line)

    def _check(self, records: Mapping[str, CaseRecord]) -> None:
        """Each round's plan against what the story expects of it, once, when code has made it."""
        for dispute_id, record in sorted(records.items()):
            step = lifecycle.phase(record)
            key = (self.cases.get(dispute_id, ""), len(record.rounds))
            if key in self._checked or step in (Phase.DECIDE, Phase.CLOSED) or (
                    step is Phase.WAIT and record.rounds[-1].plan is None):
                continue
            self._checked.add(key)
            expect = self.expects.get(key)
            got = _terms_of(record)
            if expect is not None and got == (expect.action, expect.tier, expect.file_hashes):
                continue
            if got is not None and expect is not None and got[:2] == (expect.action, expect.tier):
                line = OTHER_FILES.format(who=who(record), round=len(record.rounds))
            else:
                line = SURPRISE.format(who=who(record), round=len(record.rounds),
                                       got=_words(*got) if got is not None else "a person's case, with no draft",
                                       expected=_words(expect.action, expect.tier, expect.file_hashes)
                                       if expect is not None else "no plan here")
            if not self.lenient:
                raise StoryError(line + LOOK.format(folder=self.folder) + self._kept_in(record))
            self.surprises.append(line)
            self.say(line)

    def _kept_in(self, record: CaseRecord) -> str:
        """Where the round's reading comes from, when a readings file holds it: every run uses it again, so starting
        over gives the same plan. "" when the round read nothing kept."""
        basis = self.service.db.saved.basis(record.dispute_id, len(record.rounds))
        path = self.reader.source(basis.reading_key) if basis is not None and basis.reading_key else None
        if path is None:
            return ""
        return REUSED.format(path=path) + (FROM_CACHE if path.name == CACHE else "")

    def _sent(self) -> list[CaseRecord]:
        return [record for record in sorted(self.service.db.load().records.values(), key=lambda r: r.dispute_id)
                if lifecycle.phase(record) is Phase.SENT]

    def _record(self, dispute_id: str) -> CaseRecord:
        return self.service.db.load_one(dispute_id)[0]


def _terms_of(record: CaseRecord) -> tuple[Action, Tier, frozenset[str]] | None:
    plan = record.rounds[-1].plan
    return None if plan is None else (plan.terms.action, approvals.tier(record), plan.terms.file_hashes)


def _words(action: Action, tier: Tier, files: frozenset[str]) -> str:
    """A plan as a surprise names it: "a challenge with 1 file, for one approval"."""
    what = "an accept" if action is Action.ACCEPT else f"a challenge with {len(files)} file{'' if len(files) == 1 else 's'}"
    return {Tier.ALONE: f"{what} that code sends on its own", Tier.ONE_APPROVAL: f"{what}, for one approval",
            Tier.HAND_OFF: f"a hand-off, with code's draft: {what}", Tier.PERSON: f"a person's case, with {what} on hold",
            }.get(tier, f"{what}, with nothing to approve")


def _opens(store: Store) -> str:
    """The Monday line: "The bank opens three disputes: the ear tips ($12, due in 2 days), ..."."""
    cases = []
    for case_id in MONDAY_CASES:
        case = store.case(case_id)
        amount = store.order(case.order_number).amount if case.amount is None else case.amount
        shown = f"${amount:,.0f}" if amount == amount.to_integral_value() else usd(amount)
        cases.append(f"the {case_id.replace('_', ' ')} ({shown}, due in {case.due_in.days} days)")
    joined = cases[0] if len(cases) == 1 else f"{', '.join(cases[:-1])} and {cases[-1]}"
    return OPENS.format(count=COUNTS.get(len(cases), str(len(cases))), cases=joined)


def _approves(by: str, record: CaseRecord) -> str:
    """The approval beat's line, from the plan's terms: what Priya's screen showed her."""
    plan, order = record.rounds[-1].plan, record.last.order_number or record.dispute_id
    terms, tier = plan.terms, approvals.tier(record)
    if terms.action is Action.CHALLENGE:
        count = len(terms.file_hashes)
        sends = f"challenge with {count} file{'' if count == 1 else 's'}"
    elif terms.refund_amount > ZERO:
        sends = f"accept {usd(terms.amount)} and refund {usd(terms.refund_amount)}"
    else:
        sends = f"accept {usd(terms.amount)}, no refund"
    if tier is Tier.HAND_OFF:
        return APPROVES_DRAFT.format(by=by, order=order, sends=sends)
    if tier is Tier.ONE_APPROVAL:
        line = APPROVES_CHALLENGE if terms.action is Action.CHALLENGE else APPROVES_ACCEPT
        return line.format(by=by, order=order, sends=sends)
    return APPROVES.format(by=by, order=order, sends=sends)


def _cents(amount: Decimal) -> Decimal:
    """An amount with its cents written, as the manifest keeps it: 0.00, never 0. A finer amount stays as it is."""
    return amount.quantize(CENT) if amount == amount.quantize(CENT) else amount


def _utc(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("A moment needs a time zone.")
    return moment.astimezone(timezone.utc)


# ----------------------------------------------------------------------------- the command
def run(name: str, settings: Settings, *, claude: bool = True, lenient: bool = False, cap: Decimal | None = None,
        yes: bool = False, say: Callable[[str], object] = print, ask: Callable[[str], str] = input,
        out: Path = recording.FOLDER, project: Path = PROJECT, transport: httpx.BaseTransport | None = None,
        pay: Callable[[str, str], None] = pay_in_browser, client: Callable[[str], Any] | None = None,
        wall: Callable[[], datetime] = wall_clock, sleep: Callable[[float], None] = time.sleep) -> int:
    """disputable story --ending N: 0 once the ending is recorded, checked and copied into out/ending-<N>; 1 when it
    stopped, with its line printed. SetupError (keys, or a copy that another program held: recording.RecordingError)
    and IssuerError (a bank step refused) come out for the CLI to print. client(api_key) builds the Claude client
    (reader.claude_client); the tests pass a fake one."""
    ending = ENDINGS.get(name)
    if ending is None:
        say(NO_ENDING.format(name=name))
        return 1
    keys = airwallex.credentials(settings)
    meter: spend.Meter | None = None
    if claude:
        recorded = recorded_readings(data=settings.data_dir, project=project, say=say)
        needed = sorted(story_keys(ending) - recorded.keys())
        live: Reader = NoReader()  # never asked when nothing is needed: the limit is 0
        if not needed:
            say(ZERO_READS)
        else:
            count, s = len(needed), "" if len(needed) == 1 else "s"
            if not settings.anthropic_api_key:
                raise SetupError(NO_CLAUDE.format(count=count, s=s))
            if cap is None:
                say(NEED_CAP.format(count=count, s=s))
                return 1
            what = WHAT.format(name=ending.name, count=count, s=s, model=prompts.MODEL, effort=prompts.EFFORT)
            if not confirm_spend(what, reads=count, cap=cap, yes=yes, ask=ask, say=say):
                return 1
            meter = spend.Meter(cap)
            built = (client or _claude_client)(settings.anthropic_api_key)
            live = _BudgetReader(ClaudeReader(meter.wrap(built), profile=prompts.FERNHILL, effort=prompts.EFFORT), meter)
        reader: Reader = CachedReader(live, recorded, cache=Path(settings.data_dir) / CACHE, limit=len(needed),
                                      today=lambda: _utc(wall()).date(), say=say)
        source = "claude"
    else:
        reader, source = NoReader(), "none"
    inner = transport if transport is not None else httpx.HTTPTransport(trust_env=False)
    try:
        done = record(ending, reader=reader, source=source, credentials=keys, work=settings.data_dir, transport=inner,
                      pay=pay, out=out, lenient=lenient, airwallex="sandbox", wall=wall, sleep=sleep, say=say)
    except StoryError as stop:
        say(str(stop))
        return 1
    finally:
        if transport is None:
            inner.close()
        if meter is not None:
            say(SPENT.format(spent=usd(meter.spent), requests=meter.requests, s="" if meter.requests == 1 else "s"))
    say(COPIED.format(folder=done.copied))
    return 0


def _claude_client(api_key: str) -> Any:
    """The live Claude client for the user's own key: reader.claude_client(), the one builder, which names the API's
    address and takes no header, proxy or address from the shell."""
    return claude_client(api_key)
