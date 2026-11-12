"""The eval's runs: each site read by a scripted reader, replayed from the kept readings, or read live by Claude.

Scripted readers (perfect, empty, no key and the seven attacks) run the real ClaudeReader on the scripted FakeClient:
free, offline, and never written to any file. A replay reads the effort's readings file by each site's key: a site with
no reading there is NOT_RECORDED, and a reading kept under older checks is checked again with no call. A live run reads,
with the user's key and through the meter, each site Claude hasn't read yet: one effort at a time, the story first.

What a live read gives decides what is kept. A reading the model made is kept at once, its own failures too (cut off,
declined, no answer, a malformed answer, a stop this app doesn't expect, too slow), so a rerun never fishes for a better
answer. A busy API, a cap, an API or app failure, or Ctrl+C keeps nothing, so the next run reads that case again. Only
live Claude readings are kept, and only when the privacy checks pass: readings are checked and refused, never scrubbed,
since scrubbing would change the seed's own addresses inside Claude's quotes. A readings file that can't be written (a
program such as OneDrive holds it past the retries) stops the run with a plain line, so no more reads are lost.

The readings live in fixtures/readings/<effort>.json in the readings_file format (source "claude", made by "eval"),
found from the package, so the eval works from any folder. The story run reuses them: the story's four keys are the
ones its own rounds compute. Each kept reading, like each run in runs.json, is dated by the UTC day, as the story run
dates its own reads, whatever the computer's time zone.
"""

from __future__ import annotations

import functools
import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from disputable import clock, privacy, prompts, reader
from disputable.evals.score import CaseScore, score
from disputable.evals.sites import Site, request_for
from disputable.fees import usd
from disputable.privacy import Allowed
from disputable.reader import (CHECKS_VERSION, ClaudeReader, Failure, NoReader, Reading, ReaderBusy, ReadRequest,
                               revalidate, to_json)
from disputable.readings_file import Header, ReadingsFile, load_readings, merge, save_readings
from disputable.seed.fernhill import ENDING_2, FERNHILL
from disputable.seed.scripted import ATTACKS, Scripted, empty, fooled, perfect
from disputable.seed.suite import SUITE
from disputable.spend import READ_CAP, BudgetReached, Meter

ROOT = Path(__file__).resolve().parents[3]  # the repo: this file is <root>/src/disputable/evals/runner.py
READINGS = ROOT / "fixtures" / "readings"
EFFORTS = ("high", "medium")  # the readings files the report looks for, in its column order
LIVE_ORDER = ("medium", "high")  # a live run reads one effort at a time, medium first: the cache is per effort
SCRIPTED = ("perfect", "empty", "no-key", *ATTACKS)
BUSY_WAIT = 60  # seconds before a busy case is tried once more
MAX_MINUTES = 480
FIRST_READS, FAILING = 10, 5  # 5 model failures or reads stopped at their cap, in a run's first 10 live reads, stop it
OVER_CAP_ROW = 3  # and so do 3 reads in a row stopped at their cap, anywhere in the run: none of them is kept
MODEL_FAILURES = frozenset({Failure.CUT_OFF, Failure.REFUSED, Failure.NO_PROPOSAL, Failure.MALFORMED,
                            Failure.UNEXPECTED_STOP, Failure.TOO_SLOW})  # what the model did: kept, as the measurement
NOT_KEPT = frozenset({Failure.NOT_ALLOWED, Failure.ACCOUNT, Failure.BAD_REQUEST, Failure.ERROR})  # API and app failures

STOP_CAP = ("Stopped: the next request could take the run past its {cap} cap. Run the same command with a higher --cap "
            "to go on. The readings already made are kept.")
OVER_READ_CAP = ("{label} was stopped before its next request could pass the {cap} cap for one read. It isn't kept, so "
                 "the next run reads it again.")
GOES_ON = "The run goes on."  # after OVER_READ_CAP, only when the run isn't stopped and another read follows
AGAIN_DONE = "This run read {labels} again already. Leave {them} out of --again when you go on."
STOP_KEY = "Stopped: the Claude API refused the key. Check ANTHROPIC_API_KEY in .env. Nothing more was read."
STOP_ACCOUNT = ("Stopped: the Claude API account can't take more requests (a spend limit, billing or a hold). Check the "
                "Console's billing page.")
STOP_BAD = "Stopped: the Claude API refused this app's request twice in a row. That is a bug in this app."
STOP_ERROR = "Stopped: this app hit an error on two reads in a row."
STOP_BUSY = ("Stopped: Claude was busy on three cases in a row. Run the same command later. The readings already made "
             "are kept.")
STOP_FAILING = "Stopped: {failed} of the first {reads} reads failed ({kinds}). Look at them before spending more."
STOP_OVER_CAP = ("Stopped: the cap for one read stopped {stopped} of the first {reads} reads. Check the price of a read "
                 "before spending more.")
STOP_OVER_CAP_ROW = (f"Stopped: the cap for one read stopped the last {OVER_CAP_ROW} reads. Check the price of a read "
                     "before spending more.")
STOP_TIME = "Stopped: the run reached its {minutes}-minute limit."
STOP_YOU = "Stopped by you."
HELD = "{name} couldn't be written, because another program is using it."  # OneDrive, past the writes' retries
UNWRITTEN = "{name} couldn't be written."
STOP_UNWRITTEN = "Stopped: {why} The readings kept before are safe. Run the same command to read {label} again."
OTHER_MODEL = ("{name} holds readings Claude made with another model or effort. Run `disputable eval refresh` first: it "
               "drops the readings no case reads now.")
NOT_SCRIPTED = "There's no scripted reader named {name}. The scripted readers are {names}."


class EvalError(Exception):
    """A problem the user fixes, such as a readings file another model made. The message is the whole text."""


@dataclass(frozen=True)
class Outcome:
    """One site at one effort in one run: its reading, or why there is none, and what keeping it did."""
    site: Site
    effort: str
    source: str  # "read" (live now), "kept" (a kept reading), "scripted", or "none" (the no-key reader)
    reading: Reading | None  # None: nothing to score
    failure: str = ""  # "" or the reading's Failure value; with no reading: BUSY, OVER_CAP, STOPPED, NOT_RECORDED
    cost: Decimal = Decimal(0)  # what the meter charged for it in this run
    kept: bool = False  # saved to the readings file in this run
    refused: tuple[str, ...] = ()  # the privacy lines that kept it out of the file: places and kinds, never values
    rechecked: bool = False  # kept under older checks and checked again, with no call
    changed: bool = False  # checking it again changed it
    stopped_by: str = ""  # on the outcome that stopped the run: "cap" or "you"
    tried: bool = True  # False: the run had stopped before it reached this site
    unwritten: str = ""  # why its readings file couldn't be written, as a plain sentence: that stops the run


@dataclass(frozen=True)
class Run:
    """A live run: every site of its plan at each effort, in the order read, and what it spent."""
    outcomes: tuple[Outcome, ...]
    spent: Decimal
    requests: int
    stopped: str  # "" or the sentence that stopped it
    minutes: int


def tally(outcomes: Sequence[Outcome]) -> Mapping[str, int]:
    """The live reads' failures by kind, BUSY, OVER_CAP and STOPPED among them, sorted."""
    return dict(sorted(Counter(outcome.failure for outcome in outcomes
                               if outcome.source == "read" and outcome.tried and outcome.failure).items()))


def live_client(api_key: str) -> Any:
    """The Claude client for the key: reader.claude_client(), this app's one builder. It names the address and takes
    nothing from the shell. Never printed."""
    return reader.claude_client(api_key)


def utc_day() -> date:
    """Today in UTC: the day the story run dates its reads by, whatever the computer's time zone. clock.wall() is looked
    up at each call, so a test that moves the clock moves the day."""
    return clock.wall().astimezone(timezone.utc).date()


def unwritten(name: str, problem: OSError) -> str:
    """Why a file couldn't be written, as a plain sentence: another program holds it (a PermissionError, as when
    OneDrive holds it past the writes' retries), or any other error from the system."""
    return (HELD if isinstance(problem, PermissionError) else UNWRITTEN).format(name=name)


def again_done(outcomes: Sequence[Outcome], again: frozenset[str], efforts: Sequence[str]) -> tuple[str, ...]:
    """The --again labels this run read again and kept at every effort it reads, in run order: the same command would
    pay for them once more and replace what it just kept. Pure."""
    kept = {(outcome.site.label, outcome.effort) for outcome in outcomes if outcome.source == "read" and outcome.kept}
    return tuple(dict.fromkeys(outcome.site.label for outcome in outcomes if outcome.site.label in again
                               and all((outcome.site.label, effort) in kept for effort in efforts)))


def listed(names: Sequence[str]) -> str:
    """Names as a sentence lists them: "a", "a and b", "a, b and c"."""
    return "".join(names) if len(names) < 2 else f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass(frozen=True)
class World:
    """What a command touches outside the code: its files, the .env file, the live client, the console, the clock, the
    day (the UTC day) and the scripted readings. The tests pass their own."""
    env_file: Path = Path(".env")
    readings: Path = READINGS
    docs: Path = ROOT / "docs" / "eval"
    client_for: Callable[[str], Any] = live_client
    ask: Callable[[str], str] = input
    say: Callable[[str], object] = print
    sleep: Callable[[float], object] = time.sleep
    clock: Callable[[], float] = time.monotonic  # seconds, for the run's time limit
    today: Callable[[], date] = utc_day  # dates the kept readings and runs.json
    read: Callable[[str, Site, str], Reading] | None = None  # scripted readings; None: scripted_reading()


# ----------------------------------------------------------------------------- the readings files
def path_for(effort: str, folder: Path = READINGS) -> Path:
    return Path(folder) / f"{effort}.json"


def recorded_file(effort: str, folder: Path = READINGS) -> ReadingsFile | None:
    """The effort's readings file, loaded strictly and only when it holds Claude's own readings; None when there is
    none yet. EvalError, naming the place, when it won't load."""
    try:
        return load_readings(path_for(effort, folder), want="claude")
    except FileNotFoundError:
        return None
    except ValueError as problem:
        raise EvalError(str(problem)) from None


def recorded(effort: str, folder: Path = READINGS) -> dict[str, Reading]:
    """The effort's kept readings by key: {} when there is no file yet."""
    found = recorded_file(effort, folder)
    return dict(found.readings) if found is not None else {}


@functools.cache
def seed_allowed() -> Allowed:
    """The seed's own made-up values: what a kept reading may hold."""
    return Allowed.of(FERNHILL, ENDING_2, SUITE)


def privacy_problems(reading: Reading, allowed: Allowed) -> tuple[str, ...]:
    """What keeps a reading out of the repo, as places and kinds: privacy.reading_problems(), and the scrub rule the
    committed files are checked with (only the seed's own IPs and 127.0.0.1). () when it may be kept."""
    text = to_json(reading)
    found = list(privacy.reading_problems(text, allowed))
    scrubber = privacy.Scrubber((), allowed.addresses)
    found += [f"reading {reading.key[:8]}..., {place}: {privacy.FOUND_IP}."
              for place in _scrubbed(json.loads(text)["reading"], scrubber)]
    return tuple(dict.fromkeys(found))


def keep(effort: str, reading: Reading, *, allowed: Allowed, day: date, folder: Path = READINGS) -> tuple[str, ...]:
    """Keeps a live Claude reading in the effort's file, in place of any with its key, dated day: checked, then saved.
    Returns the privacy lines that kept it out, or () when it was saved. EvalError when the file holds readings
    another model or effort made."""
    problems = privacy_problems(reading, allowed)
    if problems:
        return problems
    old = writable(effort, folder)
    save_readings(path_for(effort, folder), merge(old, {reading.key: (reading, day)}))
    return ()


def writable(effort: str, folder: Path = READINGS) -> ReadingsFile:
    """The effort's file as keep() adds to it: the kept one, or an empty one for today's model. EvalError when the kept
    one holds readings another model or effort made."""
    old = recorded_file(effort, folder) or ReadingsFile(Header("claude", prompts.MODEL, effort, "eval"), {}, {})
    if (old.header.model, old.header.effort) != (prompts.MODEL, effort):
        raise EvalError(OTHER_MODEL.format(name=path_for(effort, folder).name))
    return old


def unused(effort: str, sites: Sequence[Site], folder: Path = READINGS) -> int:
    """Kept readings that no site's key at this effort matches: older prompts, tools or records."""
    keys = {request_for(site, effort).key for site in sites}
    return sum(key not in keys for key in recorded(effort, folder))


def checked_today(reading: Reading, request: ReadRequest) -> Reading:
    """The reading under today's checks, with no call: as it is when it was kept under them, else checked again. A
    failure with nothing to check again (a cut-off or declined answer, say) fails the same way today, so it only takes
    today's checks number."""
    if reading.checks == CHECKS_VERSION:
        return reading
    return replace(revalidate(reading, request), checks=CHECKS_VERSION)


def refresh(sites: Sequence[Site], efforts: Sequence[str] = EFFORTS, *, allowed: Allowed,
            folder: Path = READINGS) -> list[str]:
    """Each kept reading under today's checks (checked_today), with no call, and the readings no site reads any more
    dropped; each file written again. Lines say what changed: a reading changed when today's checks changed more than
    its checks number. A reading the privacy checks refuse stops the write of its file, and its lines are returned.
    EvalError, in plain words, when a file can't be written."""
    said = []
    for effort in efforts:
        old = recorded_file(effort, folder)
        if old is None:
            continue
        requests = {request.key: request for request in (request_for(site, effort) for site in sites)}
        readings, dates, changed, problems = {}, {}, 0, []
        for key, reading in old.readings.items():
            if key not in requests:
                continue
            checked = checked_today(reading, requests[key])
            changed += replace(checked, checks=reading.checks) != reading
            problems += privacy_problems(checked, allowed)
            readings[key], dates[key] = checked, old.dates[key]
        path = path_for(effort, folder)
        if problems:
            said += [f"{path.name} wasn't written again:", *problems]
            continue
        dropped = len(old.readings) - len(readings)
        try:
            save_readings(path, ReadingsFile(Header("claude", prompts.MODEL, effort, "eval"), readings, dates))
        except OSError as problem:
            raise EvalError(f"{unwritten(path.name, problem)} Run `disputable eval refresh` again. It costs "
                            "nothing.") from None
        said.append(f"{path.name}: {len(readings)} readings kept, {changed} changed by today's checks, {dropped} that "
                    "no case reads any more dropped.")
    return said


# ----------------------------------------------------------------------------- scripted and replayed
def scripted_reading(name: str, site: Site, effort: str = prompts.EFFORT) -> Reading:
    """A scripted reader's reading of the site, keyed as a live read at this effort would be. The scripted readers get
    the site's own answer, so on a control nothing is planted to flag. A FakeClient ignores the effort."""
    if name not in SCRIPTED:
        raise ValueError(NOT_SCRIPTED.format(name=name, names=", ".join(SCRIPTED)))
    request = request_for(site, effort)
    if name == "no-key":
        return NoReader().read(request)

    def make(asked: ReadRequest) -> Sequence[Any]:
        if name == "perfect":
            return perfect(asked.store, asked.case, site.answer)
        if name == "empty":
            return empty(asked.store, asked.case)
        return fooled(asked.store, asked.case, site.answer, name)

    return Scripted(make, effort=effort).read(request)


def scripted(name: str, sites: Sequence[Site], effort: str = prompts.EFFORT, *,
             read: Callable[[str, Site, str], Reading] = scripted_reading) -> tuple[Outcome, ...]:
    """Every site read by one scripted reader. Nothing is written."""
    if name not in SCRIPTED:
        raise ValueError(NOT_SCRIPTED.format(name=name, names=", ".join(SCRIPTED)))
    source = "none" if name == "no-key" else "scripted"
    return tuple(Outcome(site, effort, source, read(name, site, effort)) for site in sites)


def replayed(effort: str, sites: Sequence[Site], folder: Path = READINGS) -> tuple[Outcome, ...]:
    """Every site from the effort's kept readings: NOT_RECORDED when none has its key; checked again when it was kept
    under older checks. Nothing is written and nothing is called."""
    kept = recorded(effort, folder)
    found = []
    for site in sites:
        request = request_for(site, effort)
        reading = kept.get(request.key)
        if reading is None:
            found.append(Outcome(site, effort, "kept", None, Failure.NOT_RECORDED.value))
            continue
        rechecked = reading.checks != CHECKS_VERSION
        checked = checked_today(reading, request)
        found.append(Outcome(site, effort, "kept", checked, checked.failure.value if checked.failure else "",
                             rechecked=rechecked, changed=rechecked and replace(checked, checks=reading.checks) != reading))
    return tuple(found)


def scores(outcomes: Sequence[Outcome]) -> tuple[CaseScore, ...]:
    """Each outcome scored, a trap with the same run's reading of its control."""
    controls = {outcome.site.trap: outcome for outcome in outcomes if outcome.site.trap}
    found = []
    for outcome in outcomes:
        control = controls.get(outcome.site.label)
        found.append(score(outcome.site, outcome.reading, control=control.reading if control else None,
                           control_site=control.site if control else None, source=outcome.source,
                           failure=outcome.failure, revalidated=outcome.rechecked))
    return tuple(found)


# ----------------------------------------------------------------------------- live
def live(client: Any, plan: Sequence[Site], efforts: Sequence[str], *, cap: Decimal, read_cap: Decimal = READ_CAP,
         again: frozenset[str] = frozenset(), allowed: Allowed, folder: Path = READINGS,
         say: Callable[[str], object] = print, sleep: Callable[[float], object] = time.sleep,
         clock: Callable[[], float] = time.monotonic, today: Callable[[], date] = utc_day,
         max_minutes: int = MAX_MINUTES) -> Run:
    """Reads, with Claude, every site of the plan whose key isn't kept yet (and the labels in again), one effort at a
    time, through a meter held to cap and read_cap. Each reading the model made is kept at once, dated today(): the UTC
    day unless the caller gives another. When its file can't be written, the run stops. Stops as stop_reason() says,
    and then names the labels in again it read already; the sites it didn't reach are STOPPED and not tried. The stop
    is worked out before a read's lines are said, so the line of a read its cap stopped says the run goes on only when
    the run isn't stopped and another read follows."""
    for effort in efforts:
        writable(effort, folder)  # another model's file stops the run before any read
    meter, start = Meter(cap, read_cap), clock()
    metered = meter.wrap(client)
    kept_now = {effort: recorded(effort, folder) for effort in efforts}
    todo = sum(1 for effort in efforts for site in plan
               if site.label in again or request_for(site, effort).key not in kept_now[effort])
    outcomes: list[Outcome] = []
    stopped, number = "", 0
    for effort in efforts:
        claude = ClaudeReader(metered, profile=prompts.FERNHILL, effort=effort)
        kept = kept_now[effort]
        for site in plan:
            request = request_for(site, effort)
            if request.key in kept and site.label not in again:
                outcomes.append(Outcome(site, effort, "kept", kept[request.key]))
                continue
            if stopped:
                outcomes.append(Outcome(site, effort, "read", None, "STOPPED", tried=False))
                continue
            number += 1
            head = f"[{number}/{todo}] {effort} {site.label}"
            outcome = _read(claude, meter, site, effort, request, head=head, say=say, sleep=sleep)
            if outcome.reading is not None and outcome.failure not in {failure.value for failure in NOT_KEPT}:
                try:
                    refused = keep(effort, outcome.reading, allowed=allowed, day=today(), folder=folder)
                except OSError as problem:  # held past the retries, or the disk is full: the next read would be lost too
                    outcome = replace(outcome, unwritten=unwritten(path_for(effort, folder).name, problem))
                else:
                    outcome = replace(outcome, kept=not refused, refused=refused)
            elif outcome.failure in {failure.value for failure in NOT_KEPT}:
                outcome = replace(outcome, reading=None)
            outcomes.append(outcome)
            stopped = stop_reason(outcomes, cap=cap, minutes=(clock() - start) / 60, max_minutes=max_minutes)
            say(f"{head}  {usd(outcome.cost)}  {_status(outcome)}")
            if outcome.failure == "OVER_CAP":
                line = OVER_READ_CAP.format(label=site.label, cap=usd(read_cap))
                say(f"{line} {GOES_ON}" if not stopped and number < todo else line)
            if stopped:
                say(stopped)
                done = again_done(outcomes, again, efforts)
                if done:
                    say(AGAIN_DONE.format(labels=listed(done), them="it" if len(done) == 1 else "them"))
    return Run(tuple(outcomes), meter.spent, meter.requests, stopped, round((clock() - start) / 60))


def stop_reason(outcomes: Sequence[Outcome], *, cap: Decimal | None = None, minutes: float = 0.0,
                max_minutes: int | None = MAX_MINUTES) -> str:
    """Why a live run stops now, as the sentence it prints, or "". Pure. Only the live reads count: any refused key or
    account; a readings file that couldn't be written; the last two refused requests, or the last two app errors; the
    last three busy cases; 5 or more reads in the run's first 10 that failed as the model's own failure or were stopped
    by the cap for one read (either way, look before spending more); the last three reads, anywhere in the run, when the
    cap for one read stopped each of them (none is kept, so the next run pays for them again); the run's cap; Ctrl+C;
    and the time limit. When the rule for the first 10 reads holds as well as the one for three in a row, the first
    one's line is said: it counts more."""
    tried = [outcome for outcome in outcomes if outcome.source == "read" and outcome.tried]
    kinds = [outcome.failure for outcome in tried]
    if any(outcome.stopped_by == "you" for outcome in tried):
        return STOP_YOU
    if any(outcome.stopped_by == "cap" for outcome in tried):
        return STOP_CAP.format(cap=usd(cap) if cap is not None else "")
    for outcome in tried:
        if outcome.unwritten:
            return STOP_UNWRITTEN.format(why=outcome.unwritten, label=outcome.site.label)
    if Failure.NOT_ALLOWED.value in kinds:
        return STOP_KEY
    if Failure.ACCOUNT.value in kinds:
        return STOP_ACCOUNT
    if kinds[-2:] == [Failure.BAD_REQUEST.value] * 2:
        return STOP_BAD
    if kinds[-2:] == [Failure.ERROR.value] * 2:
        return STOP_ERROR
    if kinds[-3:] == ["BUSY"] * 3:
        return STOP_BUSY
    first = kinds[:FIRST_READS]
    failing = Counter(kind for kind in first if kind in {failure.value for failure in MODEL_FAILURES} | {"OVER_CAP"})
    if sum(failing.values()) >= FAILING:
        if set(failing) == {"OVER_CAP"}:
            return STOP_OVER_CAP.format(stopped=failing["OVER_CAP"], reads=len(first))
        kinds_listed = ", ".join(f"{kind} {count}" for kind, count in sorted(failing.items()))
        return STOP_FAILING.format(failed=sum(failing.values()), reads=len(first), kinds=kinds_listed)
    if kinds[-OVER_CAP_ROW:] == ["OVER_CAP"] * OVER_CAP_ROW:
        return STOP_OVER_CAP_ROW
    if max_minutes is not None and minutes >= max_minutes:
        return STOP_TIME.format(minutes=max_minutes)
    return ""


def _read(claude: ClaudeReader, meter: Meter, site: Site, effort: str, request: Any, *, head: str,
          say: Callable[[str], object], sleep: Callable[[float], object]) -> Outcome:
    """One live read under the read cap: tried once more after BUSY_WAIT seconds when Claude is busy."""
    start = meter.spent
    try:
        with meter.read(site.label):
            try:
                reading = claude.read(request)
            except ReaderBusy:
                say(f"{head}  {usd(meter.spent - start)}  busy, trying again in {BUSY_WAIT} seconds")
                sleep(BUSY_WAIT)
                try:
                    reading = claude.read(request)
                except ReaderBusy:
                    return Outcome(site, effort, "read", None, "BUSY", meter.spent - start)
    except BudgetReached as reached:
        if reached.scope == "read":
            return Outcome(site, effort, "read", None, "OVER_CAP", meter.spent - start)
        return Outcome(site, effort, "read", None, "STOPPED", meter.spent - start, stopped_by="cap")
    except KeyboardInterrupt:
        return Outcome(site, effort, "read", None, "STOPPED", meter.spent - start, stopped_by="you")
    return Outcome(site, effort, "read", reading, reading.failure.value if reading.failure else "", meter.spent - start)


def _status(outcome: Outcome) -> str:
    if outcome.unwritten:
        return "not kept: the readings file couldn't be written"
    if outcome.refused:
        kinds = dict.fromkeys(line.rsplit(": ", 1)[-1].rstrip(".") for line in outcome.refused)
        return f"not kept: it holds {', '.join(kinds)}"
    if outcome.failure == "BUSY":
        return "busy again, so it wasn't read"
    if outcome.failure == "OVER_CAP":
        return "stopped at its cap for one read"
    if outcome.failure == "STOPPED":
        return "stopped"
    if outcome.failure:
        return f"failed: {outcome.failure}"
    return "done"


def _scrubbed(value: Any, scrubber: privacy.Scrubber, place: str = "") -> Iterator[str]:
    """The places of every string, at any depth and inside any JSON a string holds, that scrubbing would change."""
    if isinstance(value, str):
        if scrubber.scrub(value) != value:
            yield place or "the text"
        inner = _json(value)
        if inner is not None:
            yield from _scrubbed(inner, scrubber, place)
    elif isinstance(value, dict):
        for name, inner in value.items():
            yield from _scrubbed(inner, scrubber, f"{place}.{name}" if place else str(name))
    elif isinstance(value, list):
        for number, inner in enumerate(value):
            yield from _scrubbed(inner, scrubber, f"{place}[{number}]")


def _json(text: str) -> Any:
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] not in "{[":
        return None
    try:
        found = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, (dict, list)) else None
