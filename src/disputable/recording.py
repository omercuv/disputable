"""Record and replay: every HTTP try of a live story run, scrubbed into a recording folder, and a replay that answers
from it with no keys and no network.

A recording is a folder of four files:
    manifest.json     the format, the ending and its store, the run, when, what answered for Airwallex, who approved,
                      what read the cases, this code's format numbers, and the run's own totals
    beats.json        the story's steps in order, each with the clock moments it read
    exchanges.jsonl   every HTTP try of the run, the agent's and the bank's, one scrubbed line each
    readings.json     the reading each read round used, in the readings_file format (made_by "story")

Recording. The agent's and the bank's sessions run on a RecordingTransport each, over one inner transport, and both hand
every try to one Sink. Before it writes a line, the Sink learns what the try gives away: a login's token, a payment's
client secret, the account id inside an Airwallex file id. Then the line is scrubbed in the spike's order: redact(),
scrub_strings(), json.dumps, scrub(), hide_account_id(). So the account id never shows, as itself or in base64, and each
file id keeps the stand-in. An upload is kept as its hashes, never as the PDF.

Replay. ReplayTransport answers the agent's requests from the agent's lines, by the step that made them and by method,
host, path, query and body, each key in the order recorded, so a dispute read five times gets its five answers. A login
is always answered. A request the step holds no answer for raises ReplayMismatch at once, at the step that asked, and so
does a reading the readings file doesn't hold (StrictReplayReader). The lines name a request without its ids: they reach
the demo's pages. Replay plays the beats into a fresh database through the same loop, Service and clock journal as the
live run: the demo shows it, and the story run checks itself with it before anything is copied into the repo.

What answered for Airwallex is the manifest's "airwallex": "sandbox" when disputable story ran on the Airwallex sandbox,
"stand-in" when scripts/record_standin.py ran the story on the tests' stand-in for it (tests/fake_airwallex.py). The
replay is the same either way: only the words name the kind, the demo's and those of a recording this code can't replay
(a stand-in is made again by its maker, never recorded again on the sandbox).

The story run, the demo and the tests use it like this:
    sink = Sink(<run>/exchanges.jsonl, scrubber); Session(..., transport=RecordingTransport(inner, sink)); sink.beat = n
    save(Recording(manifest, beats, sink.lines(), readings), folder); check_privacy(folder, allowed, scrubber.secrets)
    replay = Replay(load(folder), store=store, folder=<a fresh folder>); for beat in replay.beats: replay.play(beat)
    replay.transport.left(): the recorded agent calls the replay never made
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import httpx

from disputable import approvals, jsonable, lifecycle, privacy, prompts, reader, readings_file, rounds
from disputable.airwallex import DISPUTES, LOGIN, PAYMENTS, UPLOAD, AgentClient, Credentials, Session
from disputable.approvals import ApprovalRecord
from disputable.clock import ReplayClock, ReplayMismatch, check_label, wall
from disputable.config import DEFAULT_BASE_URL, DEFAULT_FILES_URL, PROJECT, SetupError
from disputable.db import STORES, Database, IssuerDate, PassLock, StorePayment
from disputable.desk import Mode, ReaderInfo, Result
from disputable.jsonable import Unloadable
from disputable.lifecycle import CaseRecord, Plan
from disputable.loop import PassReport, run_pass, who
from disputable.privacy import REDACTED, Allowed, Scrubber
from disputable.reader import Reading, ReadRequest
from disputable.readings_file import ReadingsFile
from disputable.service import LIVE_DB, PASS_LOCK, Service
from disputable.store import Store

FORMAT = 1
MANIFEST, BEATS, EXCHANGES, READINGS = "manifest.json", "beats.json", "exchanges.jsonl", "readings.json"
FILES = (MANIFEST, BEATS, EXCHANGES, READINGS)
FOLDER = PROJECT / "fixtures" / "story"  # the committed recordings: ending-1, ending-2
KINDS = ("bank", "pass", "approve")
AIRWALLEX = ("sandbox", "stand-in")  # what answered for Airwallex: the sandbox itself, or the tests' stand-in for it
ACTORS = frozenset({"agent", "bank"})
STAND_IN = "replay"  # the replay's client id and key: it sends nothing anywhere
APPROVALS = "The story script approved as {approver}, through the same checks as the approval screen."
RETRIES, RETRY_WAIT = 5, 0.2  # OneDrive or a virus scanner can hold a file for a moment
MAX_TEXT = 2000  # a body that isn't JSON, as a line keeps it
DROPPED_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection"})
MANIFEST_FIELDS = ("format", "ending", "store", "run", "recorded_at", "airwallex", "approver", "approvals", "readings",
                   "formats", "expect", "counts")
BEAT_FIELDS = ("n", "kind", "story", "at", "text", "clock", "dispute_id", "plan_sha256", "by", "payments", "dates")
LINE_FIELDS = frozenset({"seq", "beat", "actor", "label", "at", "elapsed_ms", "request", "response"})
ENDING = re.compile(r"[0-9A-Za-z_-]{1,16}")
RUN = re.compile(r"[0-9a-z]{4,32}")
SHA256 = re.compile(r"[0-9a-f]{64}")

# The replay's lines.
NO_ANSWER = ("The replay has no recorded answer for {method} {path}{body}. The code now asks something the live run "
             "didn't.")
WITH_BODY = " with this body"
NO_LOGIN = "The recording holds no login that worked, so the replay can't log in."
BAD_ANSWER = "The recording's answer to {method} {path} can't be read."
NO_READING = "{who}: the recording holds no reading for this case as the code reads it now."
PLAYED = "Step {n} was played already."
NOT_A_CASE = "Step {n} approves a dispute the replay doesn't have."
OTHER_PLAN = "{who}: the plan to approve isn't the one {by} approved in the recording."
NOT_APPROVED = "{who}: the approval the recording holds is refused now ({key}).{message}"
SKIPPED_PASS = "The pass of step {n} didn't run: another pass held the lock."

# Why a recording can't be loaded or saved. load() names the folder before each: the user records that ending again,
# or makes the stand-in again when the manifest says the stand-in made it (_again).
CANT_REPLAY = "The recording in {folder} can't be replayed. {why}"
MISSING = "It has no {name}."
NOT_JSON = "{name} in the recording isn't valid JSON."
OTHER_FORMAT = "This recording is format {found}, and this code reads format {format}. {again}"
STALE = "This recording was made by other code: {names} changed since. {again}"
BAD = "{name} in the recording doesn't hold what a recording holds ({where})."
COUNTS = "The recording's files don't match its manifest. {again}"
AGAIN = "Record the story again."
AGAIN_STANDIN = ("Make the stand-in again: uv run python scripts/record_standin.py (add --no-claude if it says the Claude "
                 "readings aren't there).")  # the maker stops without the pilot's readings, and says so
READINGS_BAD = "readings.json in the recording: {problem}"
OTHER_READINGS = "readings.json in the recording names another model or effort than its manifest."
OTHER_APPROVER = "beats.json in the recording names another approver than its manifest."

# Why copy() changed nothing, or may have mixed two runs: the checked recording stays where it was made.
HELD = ("Another program, such as OneDrive, held {name}, so nothing in {to} changed. The checked recording is in "
        "{folder}: copy its four files there, or run the story again.")
MIXED = ("Another program, such as OneDrive, held {name}, so {to} may now hold files of two runs. The checked recording "
         "is in {folder}: copy its four files there, or run the story again.")


class RecordingError(SetupError):
    """A recording that can't be read or written as it is. The message is the whole text: the CLI prints it."""


def formats() -> dict[str, int | str]:
    """The format numbers a recording depends on, as this code has them. A recording made by other code is refused: its
    replay would stop at the first change anyway, with a less helpful line."""
    return {"recording": FORMAT, "lifecycle": lifecycle.FORMAT, "reader": reader.FORMAT, "checks": reader.CHECKS_VERSION,
            "prompt": prompts.PROMPT_VERSION, "rounds": rounds.FORMAT, "approvals": approvals.FORMAT,
            "readings": readings_file.FORMAT}


def plan_text(plan: Plan) -> str:
    """The plan as the approval page posts it: jsonable.dump(), sorted keys, no spaces, ASCII."""
    return json.dumps(jsonable.dump(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ----------------------------------------------------------------------------- recording
class Sink:
    """Writes each HTTP try as one scrubbed line of exchanges.jsonl, flushed as it happens. beat is the step now running:
    the story run sets it, so each line says which step made it. clock dates each line."""

    def __init__(self, path: Path, scrubber: Scrubber, *, clock: Callable[[], datetime] = wall) -> None:
        self.path, self.scrubber, self._clock = Path(path), scrubber, clock
        self.beat, self.seq = 0, 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")

    def now(self) -> datetime:
        return _utc(self._clock())

    def write(self, exchange: Mapping[str, Any]) -> None:
        """Learns what the try gives away, then writes its line, scrubbed."""
        self.seq += 1
        line = {"seq": self.seq, "beat": self.beat, **exchange}
        for secret in _given_away(line):
            self.scrubber.learn(secret)
        text = self.scrubber.dumps(line, ensure_ascii=True)
        with self.path.open("ab") as file:
            file.write(text.encode("ascii") + b"\n")
            file.flush()

    def lines(self) -> tuple[dict[str, Any], ...]:
        """Every line written so far, parsed."""
        return tuple(json.loads(line) for line in self.path.read_text(encoding="ascii").splitlines() if line.strip())


class RecordingTransport(httpx.BaseTransport):
    """Hands each try, with its answer or its error, to the Sink, and gives the session the answer as it came. Closing it
    leaves the inner transport open: the agent's and the bank's sessions share one, and the run closes it."""

    def __init__(self, inner: httpx.BaseTransport, sink: Sink) -> None:
        self._inner, self._sink = inner, sink

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        content = request.read()
        tag = request.extensions.get("disputable") or {}
        at, started = self._sink.now(), time.monotonic()
        try:
            response = self._inner.handle_request(request)
            response.read()
        except httpx.RequestError as error:  # a timeout or the connection: the session makes it an Answer
            self._sink.write(_exchange(request, content, tag, at, started, None, type(error).__name__))
            raise
        self._sink.write(_exchange(request, content, tag, at, started, response, ""))
        return response

    def close(self) -> None:
        pass


def _exchange(request: httpx.Request, content: bytes, tag: Mapping[str, Any], at: datetime, started: float,
              response: httpx.Response | None, error: str) -> dict[str, Any]:
    data, text = None, None
    if response is not None and response.content:
        try:
            data = response.json()
        except (ValueError, RecursionError):  # not JSON, or not text
            text = response.text[:MAX_TEXT]
    headers = {} if response is None else Scrubber.redact_headers(dict(response.headers.items()))
    return {"actor": str(tag.get("actor") or ""), "label": str(tag.get("label") or ""), "at": at.isoformat(),
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "request": {"method": request.method, "url": str(request.url),
                        "headers": Scrubber.redact_headers(dict(request.headers.items())),
                        "body": _body(request.headers.get("content-type", ""), content)},
            "response": {"status": None if response is None else response.status_code, "headers": headers,
                         "json": data, "text": text, "transport_error": error or None}}


def _body(kind: str, content: bytes) -> Any:
    """A request body as a line keeps it: JSON parsed, an upload as its hashes, anything else as its size and hash."""
    if not content:
        return None
    if kind.lower().startswith("multipart/form-data"):
        return {"multipart": _upload(kind, content)}
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        return {"raw": {"bytes": len(content), "sha256": _sha256(content)}}


def _upload(kind: str, content: bytes) -> dict[str, Any]:
    """An upload: the file's name, notes and type, its size and hash, and the hash of the whole request body, which the
    replay matches. Never the file itself: the app builds it again from the store."""
    parts = _parts(kind, content)
    name, mime, data = parts.get("file", (None, None, b""))
    notes = parts.get("notes", (None, None, b""))[2].decode("utf-8", "replace")
    return {"filename": name or "", "notes": notes, "content_type": mime or "", "bytes": len(data),
            "sha256": _sha256(data), "body_sha256": _sha256(content)}


def _parts(kind: str, content: bytes) -> dict[str, tuple[str | None, str | None, bytes]]:
    """name -> (file name, content type, content) of each part of a multipart form."""
    found = re.search(r"boundary=([^;\s]+)", kind)
    if found is None:
        return {}
    parts = {}
    for part in content.split(b"--" + found.group(1).strip('"').encode("ascii", "replace"))[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb';\s*name="([^"]*)"', head)
        filename = re.search(rb';\s*filename="([^"]*)"', head)
        mime = re.search(rb"(?im)^content-type:\s*([^\s;]+)", head)
        if name is not None:
            parts[name.group(1).decode("utf-8", "replace")] = (
                filename.group(1).decode("utf-8", "replace") if filename else None,
                mime.group(1).decode("ascii", "replace") if mime else None, value.removesuffix(b"\r\n"))
    return parts


def _given_away(value: Any) -> Iterator[str]:
    """What a try shows that no line may: the value of each secret field at any depth (a login's token, a payment's
    client secret), and the account id inside each Airwallex file id."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() in privacy.SECRET_KEYS and isinstance(inner, str):
                yield inner
            yield from _given_away(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _given_away(inner)
    elif isinstance(value, str) and len(value) >= privacy.MIN_TOKEN:
        account = privacy.account_id_of(value)
        if account:
            yield account


# ----------------------------------------------------------------------------- replay
class ReplayTransport(httpx.BaseTransport):
    """Answers the agent's requests from the agent's recorded lines, with no network. The bank's lines stay for the
    timeline: the agent never asks for them. A request is keyed by the step being played (beat: Replay sets it before
    each step, as the Sink did) and by its method, host, path, sorted query and body: for JSON the body as sorted text,
    for an upload the SHA-256 of the request's bytes. So a call the live run made at another step is never served early,
    and a request the step's own lines don't answer stops the replay at that step. An IP address counts as any other in
    a key, since the recording scrubbed the ones the seed doesn't hold. A recorded transport error is raised again as its
    httpx class, with no response. Logins belong to no step."""

    def __init__(self, exchanges: Sequence[Mapping[str, Any]]) -> None:
        self._queues: dict[tuple[Any, ...], deque[Mapping[str, Any]]] = {}
        self._login: Mapping[str, Any] | None = None
        self.base_url, self.files_url = DEFAULT_BASE_URL, DEFAULT_FILES_URL
        self.beat = 0  # the step being played; the Sink's own number before the first step
        hosts_seen: set[str] = set()
        for line in exchanges:
            if line.get("actor") != "agent":
                continue
            request, response = line["request"], line["response"]
            url = httpx.URL(request["url"])
            origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
            if url.path == LOGIN:
                if "login" not in hosts_seen:
                    self.base_url = origin
                    hosts_seen.add("login")
                if self._login is None and _logged_in(response):
                    self._login = response
                continue
            if url.path == UPLOAD and "upload" not in hosts_seen:
                self.files_url = origin
                hosts_seen.add("upload")
            self._queues.setdefault((line.get("beat"), *_recorded_key(request)), deque()).append(line)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        if request.method == "POST" and request.url.path == LOGIN:
            if self._login is None:
                raise ReplayMismatch(NO_LOGIN)
            return _response(self._login, request)
        key = (self.beat, *_live_key(request))
        queue = self._queues.get(key)
        if not queue:
            raise ReplayMismatch(NO_ANSWER.format(method=request.method, path=_without_ids(request.url.path),
                                                  body=WITH_BODY if key[-1] else ""))
        recorded = queue.popleft()["response"]
        if recorded.get("transport_error"):
            raise _error(str(recorded["transport_error"]), request)
        return _response(recorded, request)

    def left(self) -> list[str]:
        """The recorded agent calls the replay never asked for, logins aside, as "GET /api/v1/...", in recorded order.
        For the terminal and the tests: a page shows only how many."""
        lines = sorted((line for queue in self._queues.values() for line in queue),
                       key=lambda line: line.get("seq") if isinstance(line.get("seq"), int) else 0)
        return [f"{line['request']['method']} {httpx.URL(line['request']['url']).path}" for line in lines]


def _without_ids(path: str) -> str:
    """A request's path as a replay line names it, with no id: the part after the disputes or the payments becomes
    {id}, as in /api/v1/pa/payment_intents/{id}. The demo's pages show these lines, and no page shows a payment or
    dispute id."""
    for collection in (DISPUTES, PAYMENTS):
        first, _, rest = path.removeprefix(collection + "/").partition("/")
        if path.startswith(collection + "/") and first != "create":
            return f"{collection}/{{id}}" + (f"/{rest}" if rest else "")
    return path


def _logged_in(response: Mapping[str, Any]) -> bool:
    status, data = response.get("status"), response.get("json")
    return isinstance(status, int) and 200 <= status < 300 and isinstance(data, dict) and isinstance(data.get("token"), str)


def _recorded_key(request: Mapping[str, Any]) -> tuple[Any, ...]:
    body = request.get("body")
    if body is None:
        text = ""
    elif isinstance(body, dict) and set(body) == {"multipart"} and isinstance(body["multipart"], dict):
        text = f"multipart:{body['multipart'].get('body_sha256')}"
    elif isinstance(body, dict) and set(body) == {"raw"} and isinstance(body["raw"], dict):
        text = f"raw:{body['raw'].get('sha256')}"
    else:
        text = _json_key(body)
    return _key(str(request["method"]), httpx.URL(request["url"]), text)


def _live_key(request: httpx.Request) -> tuple[Any, ...]:
    content = request.content
    if not content:
        text = ""
    elif request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        text = f"multipart:{_sha256(content)}"
    else:
        try:
            text = _json_key(json.loads(content))
        except (ValueError, RecursionError):
            text = f"raw:{_sha256(content)}"
    return _key(request.method, request.url, text)


def _key(method: str, url: httpx.URL, body: str) -> tuple[Any, ...]:
    query = tuple(sorted((_normal(name), _normal(value)) for name, value in url.params.multi_items()))
    return method.upper(), url.host, _normal(url.path), query, body


def _json_key(value: Any) -> str:
    return "json:" + _normal(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _normal(text: str) -> str:
    return privacy.ADDRESS.sub(REDACTED, text)


def _response(recorded: Mapping[str, Any], request: httpx.Request) -> httpx.Response:
    status = recorded.get("status")
    if type(status) is not int:
        raise ReplayMismatch(BAD_ANSWER.format(method=request.method, path=_without_ids(request.url.path)))
    headers = {name: value for name, value in (recorded.get("headers") or {}).items()
               if isinstance(value, str) and name.lower() not in DROPPED_HEADERS and value != REDACTED}
    if recorded.get("json") is not None:
        content = json.dumps(recorded["json"], ensure_ascii=True).encode("ascii")
        headers = {**{name: value for name, value in headers.items() if name.lower() != "content-type"},
                   "content-type": "application/json"}
    elif isinstance(recorded.get("text"), str):
        content = recorded["text"].encode("utf-8")
    else:
        content = b""
    return httpx.Response(status, headers=headers, content=content, request=request)


def _error(name: str, request: httpx.Request) -> Exception:
    kind = getattr(httpx, name, None)
    if not (isinstance(kind, type) and issubclass(kind, httpx.RequestError)):
        kind = httpx.TransportError
    return kind(f"The live run got {name} here.", request=request)


class StrictReplayReader:
    """The demo's reader: the recording's readings by key. A key the file doesn't hold raises ReplayMismatch, so a code
    change that moves a reading key stops the demo instead of handing the case to a person quietly. A recorded failed
    reading is served as itself. profile and effort key the requests as the live reader did."""

    def __init__(self, readings: Mapping[str, Reading], *, profile: prompts.StoreProfile = prompts.FERNHILL,
                 effort: str = prompts.EFFORT) -> None:
        self.readings, self.profile, self.effort = MappingProxyType(dict(readings)), profile, effort

    def read(self, request: ReadRequest) -> Reading:
        reading = self.readings.get(request.key)
        if reading is None:
            case = request.case
            raise ReplayMismatch(NO_READING.format(who=f"Order #{case.order_number}" if case.order_number
                                                   else f"Dispute {case.id}"))
        return reading


# ----------------------------------------------------------------------------- the folder
@dataclass(frozen=True)
class Beat:
    """One step of the story as the recording keeps it. It holds no request id and no UUID: the privacy check refuses
    both outside the exchanges."""
    n: int
    kind: str  # "bank", "pass" or "approve"
    story: str  # the story's time: "Monday 9:00", "Three weeks later", "A week later"
    at: datetime  # the moment it started, in UTC
    text: str  # the timeline's sentence
    clock: tuple[tuple[str, datetime], ...]  # the moments it read, in order
    dispute_id: str = ""  # approve: the case
    plan_sha256: str = ""  # approve: approvals.plan_sha256() of the plan approved
    by: str = ""  # approve: who approved it
    payments: tuple[StorePayment, ...] = ()  # bank, Monday: the run's payments, the agent's scope
    dates: tuple[IssuerDate, ...] = ()  # bank, Monday: the day the story opened each dispute

    def __post_init__(self) -> None:
        if type(self.n) is not int or self.n < 1:
            raise ValueError("A step is numbered from 1.")
        if self.kind not in KINDS:
            raise ValueError("A step is the bank's, a pass or an approval.")
        if not all(isinstance(text, str) and text for text in (self.story, self.text)):
            raise ValueError("A step has a story time and a sentence.")
        _utc(self.at)
        if not isinstance(self.clock, tuple):
            raise ValueError("A step's moments are a tuple.")
        for entry in self.clock:
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise ValueError("A step's moment is a label and a time.")
            check_label(entry[0])
            _utc(entry[1])
        approve = self.kind == "approve"
        if approve != bool(self.dispute_id and self.plan_sha256 and self.by) or (
                not approve and (self.dispute_id or self.plan_sha256 or self.by)):
            raise ValueError("An approval names its dispute, its plan's SHA-256 and who approved, and no other step does.")
        if approve and not SHA256.fullmatch(self.plan_sha256):
            raise ValueError("An approval names its plan by SHA-256.")
        if (self.payments or self.dates) and self.kind != "bank":
            raise ValueError("Only the bank's step writes payments and dates.")
        if not (all(isinstance(item, StorePayment) for item in self.payments)
                and all(isinstance(item, IssuerDate) for item in self.dates)):
            raise ValueError("A step's payments and dates are the database's rows.")


@dataclass(frozen=True)
class Manifest:
    format: int
    ending: str  # "1" or "2"
    store: str  # "fernhill" or "ending_2": the seed's store, which the CLI looks up
    run: str
    recorded_at: datetime
    airwallex: str  # what answered for Airwallex: "sandbox", or "stand-in" (the tests' fake, scripts/record_standin.py)
    approver: str  # the one who approved in the recording: "Priya"
    source: str  # the readings' source: "claude" or "none"
    model: str  # "" for none
    effort: str  # "" for none
    first_read: date | None  # the earliest and latest day a reading was made
    last_read: date | None
    formats: Mapping[str, int | str]  # formats() when it was recorded
    total: Decimal  # what the ending cost in the live run
    at_risk: Decimal
    closed: int  # cases closed at the end
    beats: int  # what the files hold
    exchanges: int
    readings: int


@dataclass(frozen=True)
class Recording:
    manifest: Manifest
    beats: tuple[Beat, ...]
    exchanges: tuple[Mapping[str, Any], ...]  # the lines of exchanges.jsonl, parsed
    readings: ReadingsFile

    def info(self) -> ReaderInfo:
        """What read the cases, as the screens show it."""
        manifest = self.manifest
        return ReaderInfo(prompts.FERNHILL, manifest.effort or prompts.EFFORT, manifest.source, manifest.model,
                          manifest.first_read, manifest.last_read)


def load(folder: Path) -> Recording:
    """The recording in folder, loaded strictly. RecordingError, with a plain message that names the folder, then a file
    and a place and never a value, for a missing file, another format, other code's format numbers, or a file that
    doesn't fit. The readings load with the manifest's source, so a file of another source is refused."""
    folder = Path(folder)
    try:
        return _load(folder)
    except RecordingError as refused:
        raise RecordingError(CANT_REPLAY.format(folder=folder, why=refused)) from None


def _load(folder: Path) -> Recording:
    manifest = _manifest(_json_file(folder, MANIFEST))
    beats = _beats(_json_file(folder, BEATS))
    exchanges = _lines(folder)
    try:
        readings = readings_file.load_readings(folder / READINGS, want=manifest.source)
    except FileNotFoundError:
        raise RecordingError(MISSING.format(name=READINGS)) from None
    except ValueError as problem:
        raise RecordingError(READINGS_BAD.format(problem=problem)) from None
    recording = Recording(manifest, beats, exchanges, readings)
    _check(recording)
    return recording


def save(recording: Recording, folder: Path) -> None:
    """Writes the four files into folder: ASCII, LF endings and a final LF, each to <name>.tmp first and then in place,
    retrying while OneDrive holds a file. RecordingError when its parts don't fit together."""
    _check(recording)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / MANIFEST, (json.dumps(_manifest_data(recording.manifest), indent=2, ensure_ascii=True) + "\n"))
    _write(folder / BEATS, _beats_text(recording.beats))
    _write(folder / EXCHANGES, "".join(json.dumps(line, ensure_ascii=True) + "\n" for line in recording.exchanges))
    readings_file.save_readings(folder / READINGS, recording.readings)


def copy(folder: Path, to: Path) -> None:
    """The four files of folder, byte for byte, into to, all or nothing: each new file is written to <name>.tmp first,
    then each takes its file's place, retrying while OneDrive holds one. When one stays held, the files already replaced
    are put back as they were, and RecordingError says so (or that to may now mix two runs, when one couldn't be put
    back). No .tmp is left either way."""
    folder, to = Path(folder), Path(to)
    to.mkdir(parents=True, exist_ok=True)
    temporary = {name: to / f"{name}.tmp" for name in FILES}
    old: dict[str, bytes | None] = {}  # each file as it was; None when there was none
    try:
        for name in FILES:
            try:
                old[name] = _old(to / name)
                temporary[name].write_bytes((folder / name).read_bytes())
            except PermissionError:
                raise RecordingError(HELD.format(name=name, to=to, folder=folder)) from None
        done: list[str] = []
        for name in FILES:
            try:
                _replace(temporary[name], to / name)
            except PermissionError:
                kept = _put_back(to, done, old)
                raise RecordingError((HELD if kept else MIXED).format(name=name, to=to, folder=folder)) from None
            done.append(name)
    finally:
        for path in temporary.values():
            _remove(path)


def _old(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _put_back(to: Path, done: Sequence[str], old: Mapping[str, bytes | None]) -> bool:
    """The files copy() already replaced, as they were before: whether every one could be put back."""
    kept = True
    for name in done:
        try:
            if old[name] is None:
                (to / name).unlink(missing_ok=True)
            else:
                _write(to / name, old[name])
        except OSError:  # held as well: the message says the folder may now mix two runs
            kept = False
    return kept


def check_privacy(folder: Path, allowed: Allowed, secrets: Iterable[str]) -> list[str]:
    """privacy.check_files() on the four files, with every secret the run learned: [] when the folder may go into the
    public repo, else lines naming places and counts, never values."""
    return privacy.check_files([Path(folder) / name for name in FILES], allowed, secrets)


def _check(recording: Recording) -> None:
    manifest, beats, readings = recording.manifest, recording.beats, recording.readings
    if manifest.airwallex not in AIRWALLEX:  # save() too: a manifest that load() would refuse is never written
        raise RecordingError(BAD.format(name=MANIFEST, where="airwallex"))
    if (len(beats), len(recording.exchanges), len(readings.readings)) != (manifest.beats, manifest.exchanges,
                                                                          manifest.readings):
        raise RecordingError(COUNTS.format(again=_again(manifest.airwallex)))
    if [beat.n for beat in beats] != list(range(1, len(beats) + 1)):
        raise RecordingError(BAD.format(name=BEATS, where="the steps aren't numbered 1, 2, 3 and on"))
    header = readings.header
    if (header.source, header.model, header.effort) != (manifest.source, manifest.model, manifest.effort):
        raise RecordingError(OTHER_READINGS)
    if any(beat.by != manifest.approver for beat in beats if beat.kind == "approve"):
        raise RecordingError(OTHER_APPROVER)


def _again(kind: Any) -> str:
    """What to do about a recording this code can't replay: a stand-in is made again by its maker, and any other
    recording is recorded again by the story run."""
    return AGAIN_STANDIN if kind == "stand-in" else AGAIN


def _json_file(folder: Path, name: str) -> Any:
    path = folder / name
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise RecordingError(MISSING.format(name=name)) from None
    except UnicodeDecodeError:
        raise RecordingError(NOT_JSON.format(name=name)) from None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise RecordingError(NOT_JSON.format(name=name)) from None


def _lines(folder: Path) -> tuple[Mapping[str, Any], ...]:
    path = folder / EXCHANGES
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise RecordingError(MISSING.format(name=EXCHANGES)) from None
    except UnicodeDecodeError:
        raise RecordingError(NOT_JSON.format(name=EXCHANGES)) from None
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):
            raise RecordingError(NOT_JSON.format(name=f"{EXCHANGES}, line {number},")) from None
        if not (isinstance(line, dict) and set(line) == LINE_FIELDS and line["actor"] in ACTORS
                and type(line["beat"]) is int and line["beat"] >= 0
                and isinstance(line["request"], dict) and isinstance(line["response"], dict)
                and isinstance(line["request"].get("method"), str) and isinstance(line["request"].get("url"), str)):
            raise RecordingError(BAD.format(name=EXCHANGES, where=f"line {number}"))
        lines.append(line)
    return tuple(lines)


def _manifest_data(manifest: Manifest) -> dict[str, Any]:
    return {"format": manifest.format, "ending": manifest.ending, "store": manifest.store, "run": manifest.run,
            "recorded_at": _utc(manifest.recorded_at).isoformat(), "airwallex": manifest.airwallex,
            "approver": manifest.approver, "approvals": APPROVALS.format(approver=manifest.approver),
            "readings": {"source": manifest.source, "model": manifest.model, "effort": manifest.effort,
                         "first_read": _day_text(manifest.first_read), "last_read": _day_text(manifest.last_read)},
            "formats": dict(manifest.formats),
            "expect": {"total": str(manifest.total), "at_risk": str(manifest.at_risk), "closed": manifest.closed},
            "counts": {"beats": manifest.beats, "exchanges": manifest.exchanges, "readings": manifest.readings}}


def _manifest(data: Any) -> Manifest:
    def bad(where: str) -> RecordingError:
        return RecordingError(BAD.format(name=MANIFEST, where=where))

    if not isinstance(data, dict):
        raise bad("it isn't an object")
    if type(data.get("format")) is int and data["format"] != FORMAT:
        raise RecordingError(OTHER_FORMAT.format(found=data["format"], format=FORMAT, again=_again(data.get("airwallex"))))
    if set(data) - {"airwallex"} != set(MANIFEST_FIELDS) - {"airwallex"} or data["format"] != FORMAT:
        raise bad("its fields")
    if data.get("airwallex") not in AIRWALLEX:  # missing, or neither the sandbox nor the stand-in
        raise bad("airwallex")
    found = data["formats"]
    if not isinstance(found, dict):
        raise bad("formats")
    today = formats()
    if found != today:
        names = sorted(name for name in today.keys() | found.keys() if found.get(name) != today.get(name))
        raise RecordingError(STALE.format(names=", ".join(names), again=_again(data["airwallex"])))
    texts = {name: data[name] for name in ("ending", "store", "run", "approver", "approvals")}
    if not all(isinstance(value, str) for value in texts.values()) or not ENDING.fullmatch(texts["ending"]) \
            or texts["store"] not in STORES or not RUN.fullmatch(texts["run"]) or not _person(texts["approver"]):
        raise bad("its ending, store, run or approver")
    readings, expect, counts = data["readings"], data["expect"], data["counts"]
    if not (isinstance(readings, dict) and set(readings) == {"source", "model", "effort", "first_read", "last_read"}
            and all(isinstance(readings[name], str) for name in ("source", "model", "effort"))
            and readings["source"] in readings_file.SOURCES):
        raise bad("readings")
    if not (isinstance(expect, dict) and set(expect) == {"total", "at_risk", "closed"} and type(expect["closed"]) is int
            and expect["closed"] >= 0):
        raise bad("expect")
    if not (isinstance(counts, dict) and set(counts) == {"beats", "exchanges", "readings"}
            and all(type(value) is int and value >= 0 for value in counts.values())):
        raise bad("counts")
    try:
        recorded_at = _utc(datetime.fromisoformat(_text(data["recorded_at"])))
        first, last = _day(readings["first_read"]), _day(readings["last_read"])
        total, at_risk = _amount(expect["total"]), _amount(expect["at_risk"])
    except ValueError:
        raise bad("a date or an amount") from None
    return Manifest(FORMAT, texts["ending"], texts["store"], texts["run"], recorded_at, data["airwallex"],
                    texts["approver"], readings["source"], readings["model"], readings["effort"], first, last,
                    MappingProxyType(found), total, at_risk, expect["closed"], counts["beats"], counts["exchanges"],
                    counts["readings"])


def _beats_text(beats: Sequence[Beat]) -> str:
    if not beats:
        return "[]\n"
    return "[\n" + ",\n".join(json.dumps(_beat_data(beat), ensure_ascii=True) for beat in beats) + "\n]\n"


def _beat_data(beat: Beat) -> dict[str, Any]:
    return {"n": beat.n, "kind": beat.kind, "story": beat.story, "at": _utc(beat.at).isoformat(), "text": beat.text,
            "clock": [[label, _utc(moment).isoformat()] for label, moment in beat.clock],
            "dispute_id": beat.dispute_id, "plan_sha256": beat.plan_sha256, "by": beat.by,
            "payments": [jsonable.dump(payment) for payment in beat.payments],
            "dates": [jsonable.dump(opened) for opened in beat.dates]}


def _beats(data: Any) -> tuple[Beat, ...]:
    if not isinstance(data, list):
        raise RecordingError(BAD.format(name=BEATS, where="it isn't a list"))
    beats = []
    for number, item in enumerate(data):
        where = f"step {number + 1}"
        if not (isinstance(item, dict) and tuple(sorted(item)) == tuple(sorted(BEAT_FIELDS))):
            raise RecordingError(BAD.format(name=BEATS, where=where))
        try:
            clock = tuple((_text(label), _utc(datetime.fromisoformat(_text(moment))))
                          for label, moment in _pairs(item["clock"]))
            beats.append(Beat(item["n"], item["kind"], item["story"], _utc(datetime.fromisoformat(_text(item["at"]))),
                              item["text"], clock, item["dispute_id"], item["plan_sha256"], item["by"],
                              tuple(jsonable.load(StorePayment, payment, where) for payment in _list(item["payments"])),
                              tuple(jsonable.load(IssuerDate, opened, where) for opened in _list(item["dates"]))))
        except (ValueError, TypeError):  # Unloadable is a ValueError
            raise RecordingError(BAD.format(name=BEATS, where=where)) from None
    return tuple(beats)


def _pairs(value: Any) -> list[tuple[Any, Any]]:
    if not isinstance(value, list) or not all(isinstance(pair, list) and len(pair) == 2 for pair in value):
        raise ValueError("A step's moments are pairs of a label and a time.")
    return [(pair[0], pair[1]) for pair in value]


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Not a list.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Not text.")
    return value


def _day(value: Any) -> date | None:
    if value is None:
        return None
    if not (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
        raise ValueError("Not a day.")
    return date.fromisoformat(value)


def _day_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(_text(value))
    except InvalidOperation:
        raise ValueError("Not an amount.") from None
    if not amount.is_finite():
        raise ValueError("Not an amount.")
    return amount


def _person(name: str) -> bool:
    return 1 <= len(name) <= 60 and name.isprintable() and name == name.strip() and name.casefold() != lifecycle.ACTOR


def _write(path: Path, data: str | bytes) -> None:
    """data at path through <path>.tmp and one os.replace, retried 5 times 0.2 s apart while another program holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data.encode("ascii") if isinstance(data, str) else data)
    try:
        _replace(temporary, path)
    except PermissionError:
        _remove(temporary)
        raise


def _replace(temporary: Path, path: Path) -> None:
    """os.replace, retried 5 times 0.2 s apart while another program (OneDrive, a virus scanner) holds the file."""
    for attempt in range(RETRIES + 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == RETRIES:
                raise
            time.sleep(RETRY_WAIT)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # held too: .gitignore keeps a .tmp out of the repo
        pass


# ----------------------------------------------------------------------------- playing a recording
@dataclass(frozen=True)
class Played:
    """One step as the replay played it: a pass's report, or an approval's Result."""
    beat: Beat
    report: PassReport | None = None
    result: Result | None = None


@dataclass(frozen=True)
class State:
    """What the story run's self-check compares after each step, live and replayed: every case's saved record as
    lifecycle.to_json() writes it, the cases whose rows can't be read, and every approval row."""
    records: Mapping[str, str]
    broken: Mapping[str, str]
    approvals: Mapping[str, tuple[ApprovalRecord, ...]]
    names: Mapping[str, str]  # dispute id -> "Order #N", to name a case in a line


def state_of(db: Database) -> State:
    snapshot = db.snapshot()
    return State(MappingProxyType({key: lifecycle.to_json(record) for key, record in snapshot.records.items()}),
                 snapshot.broken, snapshot.approvals,
                 MappingProxyType({key: who(record) for key, record in snapshot.records.items()}))


def differences(live: State, replayed: State) -> list[str]:
    """What the replay holds otherwise than the live run, as phrases that name a case and never a value."""
    names = {**replayed.names, **live.names}
    found = []
    for dispute_id in sorted(live.records.keys() | replayed.records.keys() | live.broken.keys() | replayed.broken.keys()):
        name = names.get(dispute_id, f"dispute {dispute_id}")
        if live.records.get(dispute_id) != replayed.records.get(dispute_id) or (
                (dispute_id in live.broken) != (dispute_id in replayed.broken)):
            found.append(f"{name}'s saved record")
        if live.approvals.get(dispute_id, ()) != replayed.approvals.get(dispute_id, ()):
            found.append(f"the approvals of {name}")
    for dispute_id in sorted((live.approvals.keys() | replayed.approvals.keys()) - live.records.keys()
                             - replayed.records.keys() - live.broken.keys() - replayed.broken.keys()):
        if live.approvals.get(dispute_id, ()) != replayed.approvals.get(dispute_id, ()):
            found.append(f"the approvals of dispute {dispute_id}")
    return found


class Replay:
    """A recording played into a fresh database in folder, with no keys and no network: a Service in Mode.DEMO on a
    ReplayTransport, a ReplayClock and a StrictReplayReader. Each beat plays as the live run played it:
    - bank: writes the beat's payments and dates;
    - pass: clock.load(beat.clock), loop.run_pass(service), clock.done();
    - approve: the case's plan must be the one approved (by SHA-256), then approve_at() at the recorded approve moment.
    A ReplayMismatch comes out of play() as it is: the demo stops at its beat, and the story run doesn't copy."""

    def __init__(self, recording: Recording, *, store: Store, folder: Path, approver: str | None = None) -> None:
        manifest, folder = recording.manifest, Path(folder)
        self.recording, self.beats = recording, recording.beats
        self.clock = ReplayClock(recording.beats[0].at if recording.beats else manifest.recorded_at)
        self.transport = ReplayTransport(recording.exchanges)
        session = Session(Credentials(STAND_IN, STAND_IN, self.transport.base_url, self.transport.files_url),
                          actor="agent", transport=self.transport, sleep=_no_wait)
        db = Database(folder / LIVE_DB, stamp=self.clock.peek)
        for key, value in (("mode", "demo"), ("run", manifest.run), ("store", manifest.store),
                           ("ending", manifest.ending)):
            db.set_meta(key, value)
        info = recording.info()
        replayed = StrictReplayReader(recording.readings.readings, profile=info.profile, effort=info.effort)
        self.service = Service(db=db, store=store, clock=self.clock, info=info,
                               approver=manifest.approver if approver is None else approver, mode=Mode.DEMO,
                               reader=replayed, client=AgentClient(session), lock=PassLock(folder / PASS_LOCK))
        self._played: list[int] = []

    @property
    def played(self) -> tuple[int, ...]:
        """The numbers of the beats played, in the order they were played."""
        return tuple(self._played)

    def unplayed(self) -> tuple[Beat, ...]:
        return tuple(beat for beat in self.beats if beat.n not in self._played)

    def window(self) -> tuple[Beat, ...]:
        """The approvals that may be played now: the run of unplayed approve beats that starts at the lowest unplayed
        beat. Inside it they may be played in any order."""
        found = []
        for beat in self.unplayed():
            if beat.kind != "approve":
                break
            found.append(beat)
        return tuple(found)

    def finished(self) -> bool:
        return not self.unplayed()

    def play(self, beat: Beat, *, plan_text: str | None = None, reading_key: str | None = None) -> Played:
        """One beat. plan_text and reading_key: what the approval page posted, for an approve beat; by default the plan
        as the page shows it, and rounds.current()'s reading key."""
        if beat.n in self._played:
            raise ReplayMismatch(PLAYED.format(n=beat.n))
        self.transport.beat = beat.n  # its calls are answered from the calls the live run made at this step
        if beat.kind == "bank":
            self.clock.load(beat.clock, at=beat.at)
            for payment in beat.payments:
                self.service.db.put_store_payment(payment)
            for opened in beat.dates:
                self.service.db.put_issuer_date(opened)
            self.clock.done()
            played = Played(beat)
        elif beat.kind == "pass":
            self.clock.load(beat.clock, at=beat.at)
            report = run_pass(self.service)
            if report.skipped:
                raise ReplayMismatch(SKIPPED_PASS.format(n=beat.n))
            self.clock.done()
            played = Played(beat, report=report)
        else:
            played = Played(beat, result=self._approve(beat, plan_text, reading_key))
        self._played.append(beat.n)
        return played

    def play_all(self) -> list[Played]:
        """Every beat not played yet, in order."""
        return [self.play(beat) for beat in self.unplayed()]

    def state(self) -> State:
        return state_of(self.service.db)

    def totals(self) -> tuple[Decimal, Decimal, int]:
        """What the replayed cases cost, what is still at risk, and how many closed."""
        records = self.service.db.load().records.values()
        return (sum((lifecycle.cost(record) for record in records), Decimal(0)),
                sum((lifecycle.at_risk(record) for record in records), Decimal(0)),
                sum(record.closed is not None for record in records))

    def _approve(self, beat: Beat, posted: str | None, key: str | None) -> Result:
        try:
            record, _ = self.service.db.load_one(beat.dispute_id)
        except (KeyError, Unloadable):
            raise ReplayMismatch(NOT_A_CASE.format(n=beat.n)) from None
        name, plan = who(record), record.rounds[-1].plan
        if plan is None or approvals.plan_sha256(plan) != beat.plan_sha256:
            raise ReplayMismatch(OTHER_PLAN.format(who=name, by=beat.by))
        self.clock.load(beat.clock, at=beat.at)
        text = plan_text(plan) if posted is None else posted
        key = self._reading_key(record) if key is None else key
        result = self.service.approve_at(beat.dispute_id, text, key, self.clock.now(f"approve:{beat.dispute_id}"))
        self.clock.done()
        if result.key != "approved":
            raise ReplayMismatch(NOT_APPROVED.format(who=name, key=result.key,
                                                     message=f" {result.message}" if result.message else ""))
        return result

    def _reading_key(self, record: CaseRecord) -> str:
        info = self.service.info
        step = rounds.current(record, self.service.store, self.service.db.saved, self.clock.peek(), self.service.fees,
                              profile=info.profile, effort=info.effort)
        return step.reading.key if step.reading is not None else ""


def _no_wait(seconds: float) -> None:
    """A replay never waits: no answer is late, and nothing is throttled."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc(moment: Any) -> datetime:
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("A moment needs a time zone.")
    return moment.astimezone(timezone.utc)
