"""Story recordings made on the fake sandbox, once a test session, for the replay, demo and app tests.

record_on_fake() runs disputable.story.record() as the live story run does, with the bank on the fake sandbox's
simulator routes and its pay() in place of the browser checkout, and the scripted perfect reader (or NoReader) in place
of Claude. The run writes its own database and recording in a temp folder, replays the recording into a second one,
checks its privacy, and copies the four files to <root>/ending-<n>. Offline: no Airwallex, no Claude, no browser.

A recording made with the scripted reader says source "claude" in its manifest and its readings file: the readings are
ClaudeReader's own, made on a scripted client. And record_on_fake() writes "airwallex": "sandbox" in the manifest by
default: these recordings stand for the ones disputable story makes on the sandbox. Such a recording lives only in a
temp folder and is never committed. The stand-in maker, scripts/record_standin.py, records on the fake through
record_on_fake() too, with "stand-in": standins() runs it, offline, into a temp folder.

The checks below hold for every recording, made here or committed: test_story_recordings runs them on
fixtures/story/ending-*, and test_recording runs them on these, so they can't fail later for a wrong reason.

The tests use it like this:
    folders = recorded()                 # {"1": <root>/ending-1, "2": <root>/ending-2}, the perfect reader's
    folders = recorded("none")           # the same with NoReader: every read case goes to a person
    story = stories()["1"]               # the story.Recorded result: its live database, total and recording
    replay = replayed(folders["1"])      # a Replay of it, not played yet, in a fresh temp folder
    folders = standins().folders()       # both endings as the stand-in maker makes them with --no-claude
"""

from __future__ import annotations

import atexit
import base64
import builtins
import contextlib
import functools
import importlib.util
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from unittest import mock

import dotenv
import dotenv.main
import httpx
import httpx2
from anthropic._base_client import BaseClient  # every client of the SDK's is one: Anthropic, AsyncAnthropic and the rest

from disputable import config, privacy, prompts, story
from disputable.airwallex import DISPUTES
from disputable.lifecycle import ACTOR
from disputable.reader import FORMAT as READER_FORMAT, NoReader
from disputable.readings_file import Header, ReadingsFile, load_readings, save_readings
from disputable.recording import FILES, READINGS, Played, Recording, Replay, check_privacy, formats, load
from disputable.seed.fernhill import MONDAY
from disputable.seed.scripted import Scripted, perfect_for
from fake_airwallex import ACCOUNT_ID, API_KEY, CLIENT_ID, FakeAirwallex, credentials
from sandbox import Sandbox

ENDINGS = ("1", "2")
READERS = ("perfect", "none")
TICK = timedelta(milliseconds=1)
MAKER = Path(__file__).resolve().parents[1] / "scripts" / "record_standin.py"
FAKE_IDS = re.compile(r"(?:int|dst)_fake\d{4}")  # the ids tests/fake_airwallex.py gives its payments and disputes
SANDBOX_WORDS = "recorded on the Airwallex sandbox"  # what nothing may say of a run on the stand-in


class Ticking:
    """The run's wall clock in a test: it starts on Monday at 9:00 in San Francisco, moves a millisecond each time it
    is read, and as long as each sleep. So every moment is newer than the one before, as on a real clock."""

    def __init__(self, start: datetime = MONDAY) -> None:
        self.at = start.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        self.at += TICK
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def scratch(prefix: str = "disputable-recordings-") -> Path:
    """A temp folder that goes when the session ends."""
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, folder, True)
    return folder


def reader_for(name: str) -> tuple[Any, str]:
    """The reader and the readings' source: the scripted perfect reader as Claude, or NoReader."""
    if name == "perfect":
        return Scripted(perfect_for), "claude"
    if name == "none":
        return NoReader(), "none"
    raise ValueError(f"No reader named {name}.")


def record_on_fake(ending: str, reader: Any = "perfect", *, root: Path | None = None, out: Path | None = None,
                   fake: FakeAirwallex | None = None, airwallex: str = "sandbox",
                   say: Callable[[str], object] = lambda line: None, **options: Any) -> story.Recorded:
    """One ending recorded on the fake sandbox: the run's data in <root>/data, the copy in <out>/ending-<n>, where out
    is root unless given. reader: "perfect", "none", or a reader of the test's own with source= given. airwallex: what
    the manifest says answered for Airwallex, "sandbox" or "stand-in" (the stand-in maker's)."""
    root = scratch() if root is None else Path(root)
    chosen = story.ENDINGS[ending]
    fake = FakeAirwallex(Sandbox(chosen.store)) if fake is None else fake
    wall = options.pop("wall", None) or Ticking()
    made, source = reader_for(reader) if isinstance(reader, str) else (reader, options.pop("source"))
    return story.record(chosen, reader=made, source=source, credentials=credentials(), work=root / "data",
                        transport=fake.transport(), pay=lambda payment_id, secret: fake.pay(payment_id),
                        out=root if out is None else Path(out), airwallex=airwallex, wall=wall,
                        sleep=getattr(wall, "sleep", lambda seconds: None), say=say, **options)


@functools.cache
def _recorded(reader: str) -> tuple[tuple[str, story.Recorded, FakeAirwallex], ...]:
    root = scratch()
    made = []
    for ending in ENDINGS:
        fake = FakeAirwallex(Sandbox(story.ENDINGS[ending].store))
        made.append((ending, record_on_fake(ending, reader, root=root, fake=fake), fake))
    return tuple(made)


def stories(reader: str = "perfect") -> Mapping[str, story.Recorded]:
    """Ending 1 and Ending 2 recorded on the fake sandbox, once a session: the story.Recorded of each. Read them only:
    other tests share them."""
    return {ending: done for ending, done, _ in _recorded(reader)}


def fakes(reader: str = "perfect") -> Mapping[str, FakeAirwallex]:
    """The fake sandbox each ending was recorded on, as the run left it. Read only."""
    return {ending: fake for ending, _, fake in _recorded(reader)}


def recorded(reader: str = "perfect") -> Mapping[str, Path]:
    """The folders of Ending 1 and Ending 2 recorded on the fake sandbox, once a session: {"1": ..., "2": ...}, each
    holding the four files. Read them only: other tests share them."""
    return {ending: done.copied for ending, done in stories(reader).items()}


def replayed(folder: Path, *, store: Any = None) -> Replay:
    """A Replay of the recording in folder, on a fresh database in a temp folder, not played yet."""
    loaded = load(folder)
    return Replay(loaded, store=story.STORES[loaded.manifest.store] if store is None else store,
                  folder=scratch("disputable-replay-"))


# ----------------------------------------------------------------------------- the stand-in maker
@functools.cache
def maker() -> Any:
    """scripts/record_standin.py as a module, loaded once: the tests run its main()."""
    spec = importlib.util.spec_from_file_location("record_standin", MAKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_standin"] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Made:
    """What the stand-in maker's main() did: its exit code, every line it said, and the folder it wrote ending-1 and
    ending-2 into. project and data: where it looked for readings, so a test can see it changed nothing there."""
    code: int
    said: tuple[str, ...]
    out: Path
    project: Path
    data: Path

    def folders(self) -> dict[str, Path]:
        return {ending: self.out / f"ending-{ending}" for ending in ENDINGS}


def _refused(what: str) -> Callable[..., Any]:
    def refuse(*args: Any, **options: Any) -> Any:
        raise AssertionError(f"The stand-in maker {what}.")

    return refuse


def _local(host: Any) -> bool:
    """Whether a host is this computer's own: localhost, or a loopback address. None: no host at all."""
    if host is None:
        return True
    text = (host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)).strip("[]")
    if text.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text.split("%")[0]).is_loopback
    except ValueError:
        return False


def _lookup(real: Callable[..., Any]) -> Callable[..., Any]:
    def lookup(host: Any, *args: Any, **options: Any) -> Any:
        if not _local(host):
            raise AssertionError(f"The stand-in maker looked up {host} on the network.")
        return real(host, *args, **options)

    return lookup


def _connection(real: Callable[..., Any]) -> Callable[..., Any]:
    def connection(address: Any, *args: Any, **options: Any) -> Any:
        if not _local(address[0]):
            raise AssertionError(f"The stand-in maker connected to {address[0]} on the network.")
        return real(address, *args, **options)

    return connection


def _connect(real: Callable[..., Any]) -> Callable[..., Any]:
    def connect(sock: Any, address: Any, *args: Any) -> Any:
        if isinstance(address, tuple) and not _local(address[0]):  # a file path (AF_UNIX) is this computer's own
            raise AssertionError(f"The stand-in maker connected to {address[0]} on the network.")
        return real(sock, address, *args)

    return connect


def _opens(real: Callable[..., Any]) -> Callable[..., Any]:
    def opens(file: Any, *args: Any, **options: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)) and os.path.basename(os.fsdecode(file)).casefold() == ".env":
            raise AssertionError("The stand-in maker opened a .env file.")
        return real(file, *args, **options)

    return opens


def _guards() -> list[tuple[Any, str, Any]]:
    """(where, name, its stand-in) for each way out: the stand-ins pass on what they allow to the function as it is now."""
    transport, client, env = (_refused(what) for what in ("built a real transport", "built a Claude client",
                                                           "read a .env file"))
    return [(httpx.HTTPTransport, "__init__", transport), (httpx.AsyncHTTPTransport, "__init__", transport),
            (httpx2.HTTPTransport, "__init__", transport), (httpx2.AsyncHTTPTransport, "__init__", transport),
            (BaseClient, "__init__", client),  # anthropic.Anthropic, AsyncAnthropic and every other client of the SDK's
            (config, "dotenv_values", env), (dotenv, "dotenv_values", env), (dotenv, "load_dotenv", env),
            (dotenv.main, "dotenv_values", env), (dotenv.main, "load_dotenv", env),
            (builtins, "open", _opens(builtins.open)), (io, "open", _opens(io.open)),
            (socket, "getaddrinfo", _lookup(socket.getaddrinfo)),
            (socket, "create_connection", _connection(socket.create_connection)),
            (socket.socket, "connect", _connect(socket.socket.connect)),
            (socket.socket, "connect_ex", _connect(socket.socket.connect_ex))]


@contextlib.contextmanager
def offline() -> Iterator[None]:
    """While the stand-in maker runs, each of these is an AssertionError: building httpx's or httpx2's HTTPTransport or
    AsyncHTTPTransport, sync or async (the fake's httpx.MockTransport is neither); building any of the SDK's Claude
    clients (each starts in its base client); calling config's dotenv_values, or dotenv's dotenv_values or load_dotenv,
    whatever they read; opening a .env file with open() or io.open (a Path read goes through it); and
    socket.getaddrinfo, socket.create_connection, or a socket's connect or connect_ex, for a host that isn't this
    computer's own (localhost or a loopback address). Nothing else is guarded: a read through os.open, or a connection
    made below the socket module (asyncio's on Windows), gets through."""
    with contextlib.ExitStack() as stack:
        for where, name, stand_in in _guards():
            stack.enter_context(mock.patch.object(where, name, stand_in))
        yield


def make_standins(out: Path, *options: str, project: Path, data: Path) -> Made:
    """The stand-in maker's main() with --out out and the options given, on project and data, under offline()'s guards,
    with every line it says kept."""
    said: list[str] = []
    with offline():
        code = maker().main([*options, "--out", str(out)], project=project, data=data, say=said.append)
    return Made(code, tuple(said), Path(out), project, data)


def claude_readings(project: Path) -> Path:
    """The fake recordings' readings, the perfect reader's, as the pilot leaves Claude's readings for the story run:
    <project>/fixtures/readings/high.json. Only in a temp folder: no committed file holds a scripted reading."""
    files = [load(folder).readings for folder in recorded().values()]
    readings = {key: reading for file in files for key, reading in file.readings.items()}
    dates = {key: day for file in files for key, day in file.dates.items()}
    path = Path(project) / "fixtures" / "readings" / "high.json"
    save_readings(path, ReadingsFile(Header("claude", prompts.MODEL, prompts.EFFORT, "eval"), readings, dates))
    return path


def standins(readings: str = "none") -> Made:
    """Both endings made once a session by scripts/record_standin.py into a temp folder, offline. readings "none":
    --no-claude. "claude": the readings Claude made for the story, here claude_readings(). Read the folders only.
    standins(), standins("none") and standins(readings="none") are one cache entry, so the maker runs once for them."""
    return _standins(readings)


@functools.cache
def _standins(readings: str) -> Made:
    root = scratch("disputable-made-")
    project, data = root / "project", root / "data"
    if readings not in ("none", "claude"):
        raise ValueError(f"No readings named {readings}.")
    if readings == "claude":
        claude_readings(project)
    return make_standins(root / "out", *(["--no-claude"] if readings == "none" else []), project=project, data=data)


# ----------------------------------------------------------------------------- what every recording must hold
def agent_lines(recording: Recording) -> list[dict[str, Any]]:
    return [dict(line) for line in recording.exchanges if line["actor"] == "agent"]


def bodies(recording: Recording) -> list[dict[str, Any]]:
    """The body of each response the agent sent: every accept and challenge POST."""
    return [line["request"]["body"] for line in agent_lines(recording) if line["request"]["method"] == "POST"
            and httpx.URL(line["request"]["url"]).path.endswith(("/accept", "/challenge"))]


def due_days(recording: Recording) -> list[int]:
    """For each dispute, the days from its created_at to its due_at as the agent first read it, sorted."""
    seen: dict[str, int] = {}
    for line in agent_lines(recording):
        answer = line["response"].get("json")
        path = httpx.URL(line["request"]["url"]).path
        if line["request"]["method"] != "GET" or not isinstance(answer, dict) or path != f"{DISPUTES}/{answer.get('id')}":
            continue
        if answer["id"] not in seen:
            span = datetime.fromisoformat(answer["due_at"]) - datetime.fromisoformat(answer["created_at"])
            seen[answer["id"]] = round(span.total_seconds() / 86400)
    return sorted(seen.values())


def file_ids(recording: Recording) -> list[str]:
    """Every Airwallex file id the exchanges hold, in a body or an answer."""
    text = "\n".join(json.dumps(line) for line in recording.exchanges)
    return [token for token in dict.fromkeys(privacy.base64_tokens(text))
            if privacy.SEPARATOR in (privacy.decode_base64(token) or "") and privacy.account_id_of(token)]


def replay_to_the_end(folder: Path) -> tuple[Replay, list[Played]]:
    """The recording replayed to its last beat, on a fresh database, with no keys and no network."""
    replay = replayed(folder)
    return replay, replay.play_all()


def first_pass_alerts(played: Iterable[Played]) -> list[tuple[str | None, str]]:
    """(order, urgency) of each alert the first pass's report keeps: from the pass's start, before it decided."""
    first = next(step for step in played if step.report is not None)
    return [(alert.order_number, alert.urgency.value) for alert in first.report.alerts]


def actors(recording: Recording) -> set[str]:
    """Who each body names as the one responding."""
    return {body.get("accepted_by", body.get("challenged_by")) for body in bodies(recording)}


def private_problems(folder: Path, secrets: Iterable[str] = ()) -> list[str]:
    """privacy.check_files() on the four files, with the seed's allow-list."""
    return check_privacy(folder, story.allowed(), secrets)


def fake_secrets(fake: FakeAirwallex | None = None) -> set[str]:
    """Every secret a recording on this fake could leak: its keys, each token and client secret it gave, and its
    made-up account id. With no fake at hand (the stand-in maker keeps its own), the stems every fake's tokens and
    client secrets start with stand for them."""
    if fake is None:
        return {CLIENT_ID, API_KEY, ACCOUNT_ID, "fake-token-", "fake-client-secret-"}
    made = {f"fake-token-{n:04d}-" for n in range(1, fake.logins + 1)}
    secrets = {CLIENT_ID, API_KEY, ACCOUNT_ID, *made}
    secrets |= {payment["client_secret"] for payment in fake.sandbox.payments.values() if payment.get("client_secret")}
    return secrets


KEPT_REDACTED = privacy.SECRET_HEADERS | privacy.SECRET_KEYS | {"ip_address"}  # test_spike_offline's names, and more


def unredacted(lines: Iterable[Mapping[str, Any]]) -> list[str]:
    """Each place in exchange lines where a field a recording keeps only redacted holds anything but <redacted> or
    nothing: a cf-ray or cookie header, a token, a client secret, the paying browser's device data, an IP address. As
    "line 3: response.headers.cf-ray". The spike's own tests check these names the same way."""
    found = []

    def walk(value: Any, place: str, number: int) -> None:
        if isinstance(value, dict):
            for name, inner in value.items():
                here = f"{place}.{name}" if place else str(name)
                if str(name).lower() in KEPT_REDACTED and inner not in (privacy.REDACTED, None, ""):
                    found.append(f"line {number}: {here}")
                else:
                    walk(inner, here, number)
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                walk(inner, f"{place}[{index}]", number)

    for number, line in enumerate(lines, start=1):
        walk({"request": line.get("request"), "response": line.get("response")}, "", number)
    return found


def holds(folder: Path, value: str) -> list[str]:
    """The files of the folder that hold value: as itself, in base64 (standard or URL-safe), or inside any base64 run
    of 48 or more characters, at any of its alignments, as privacy.base64_tokens() finds them."""
    forms = {value, base64.b64encode(value.encode()).decode().rstrip("="),
             base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")}
    found = []
    for name in FILES:
        text = (Path(folder) / name).read_text(encoding="utf-8")
        if any(form in text for form in forms) or any(
                value in (privacy.decode_base64(token) or "") for token in privacy.base64_tokens(text)):
            found.append(name)
    return found


# ----------------------------------------------------------------------------- the rules every recording keeps
def assert_it_replays_to_its_own_total(replay: Replay, played: Sequence[Played]) -> None:
    """Every beat played with no keys and no network, the manifest's totals reached, and no recorded call left over."""
    manifest = replay.recording.manifest
    assert replay.finished() and len(played) == manifest.beats
    assert replay.totals() == (manifest.total, manifest.at_risk, manifest.closed)
    assert replay.transport.left() == []


def assert_only_the_ear_tips_alert_on_monday(played: Sequence[Played]) -> None:
    assert first_pass_alerts(played) == [("1121", "SOON")]


def assert_the_app_responds_as_itself(recorded: Recording) -> None:
    assert bodies(recorded) and actors(recorded) == {ACTOR}


def assert_the_storys_deadlines(recorded: Recording) -> None:
    """Due in 2, 4 and 6 days from when the bank opened each, as the agent first read them."""
    assert due_days(recorded) == [2, 4, 6]


def assert_todays_formats(folder: Path) -> None:
    """load() refuses other code's formats; the readings load with the manifest's own source, as the story made them."""
    recorded = load(folder)
    assert dict(recorded.manifest.formats) == formats()
    file = load_readings(Path(folder) / READINGS, want=recorded.manifest.source)
    assert file.header.made_by == "story" and file.header.source in ("claude", "none")
    assert {reading.format for reading in file.readings.values()} == {READER_FORMAT}


def assert_the_readings_are_claudes_own(recorded: Recording) -> None:
    """When the manifest says Claude read the cases, every reading is Claude's own: none failed, so none shows code's
    hand-off in its place under a banner that names the model."""
    if recorded.manifest.source == "claude":
        assert [key[:8] for key, reading in recorded.readings.readings.items() if reading.failure is not None] == []


def assert_a_story_the_demo_plays(recorded: Recording) -> None:
    manifest, beats = recorded.manifest, recorded.beats
    assert manifest.ending in story.ENDINGS and manifest.store == story.ENDINGS[manifest.ending].store_name
    assert manifest.approver == story.APPROVER and {beat.by for beat in beats if beat.kind == "approve"} == {"Priya"}
    assert [beat.kind for beat in beats[:2]] == ["bank", "pass"] and beats[-1].kind == "pass"
    assert len(beats[0].payments) == len(beats[0].dates) == 3


def assert_the_kind_is_what_answered(recorded: Recording) -> None:
    """The manifest says what answered for Airwallex, and the run's own ids agree: on the tests' stand-in every payment
    and dispute is int_fakeNNNN or dst_fakeNNNN, and on the sandbox none is. So a run on the stand-in never goes into
    the repo labelled as the sandbox's. The recordings record_on_fake() labels "sandbox" fail it: they are never
    committed."""
    first = recorded.beats[0]
    ids = [payment.payment_id for payment in first.payments] + [opened.dispute_id for opened in first.dates]
    assert recorded.manifest.airwallex in ("sandbox", "stand-in") and ids
    assert {bool(FAKE_IDS.fullmatch(found)) for found in ids} == {recorded.manifest.airwallex == "stand-in"}


def assert_nothing_private(folder: Path, secrets: Iterable[str] = ()) -> None:
    """The privacy checks on the four files, every Airwallex file id carries the stand-in for the account id, and every
    field a recording keeps only redacted is (the spike's own check, by name)."""
    assert private_problems(folder, secrets) == []
    recorded = load(folder)
    ids = file_ids(recorded)
    assert {privacy.account_id_of(token) for token in ids} <= {privacy.ACCOUNT_ID_STAND_IN}
    assert unredacted(recorded.exchanges) == []
