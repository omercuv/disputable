"""The disputable command, run in this process through cli.main(argv), never in a subprocess.

Each test runs in its own folder, so the only .env the command can read is the one the test writes, and every data
folder is a temp one. Offline: the live commands' Airwallex session goes to the fake sandbox, the Claude client is a
stand-in the cap stops or one that is always busy, and the screens' server is replaced by a stand-in that keeps the desk
it was given. The demo replays the recordings tests/recordings.py makes on the fake sandbox, and those the stand-in
maker (scripts/record_standin.py) makes there, whose start line says they aren't recordings of Airwallex. A replay that
stops matching prints the line the demo's bar shows, and a clone with no recording names the stand-in maker.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import shlex
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from unittest import mock

import anthropic
import httpx
import httpx2
import pytest

import recordings
from disputable import cli, config, db as storage, lifecycle, loop, recording, service, spend, story
from disputable.clock import ReplayMismatch, WallClock
from disputable.config import NO_AIRWALLEX_KEYS, NO_CLAUDE_KEY
from disputable.db import Database
from disputable.demo import Demo
from disputable.desk import Mode
from disputable.issuer import Issuer, IssuerError
from disputable.reader import WHY, ClaudeReader, Failure
from disputable.readings_file import ReadingsFile
from disputable.seed.fernhill import ENDING_2, FERNHILL, MONDAY
from disputable.web import words as w
from fake_airwallex import API_KEY, CLIENT_ID, FakeAirwallex
from recordings import Ticking
from sandbox import Sandbox
from web_helpers import STATES, demo_bar

KEYS = f"AWX_CLIENT_ID={CLIENT_ID}\nAWX_API_KEY={API_KEY}\n"
CLAUDE_KEY = "ANTHROPIC_API_KEY=sk-made-up-key-of-this-test\n"


@pytest.fixture(autouse=True)
def here(tmp_path, monkeypatch):
    """The test's own folder, where the command looks for .env. There is none until the test writes one."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys, *argv: str) -> tuple[int, list[str]]:
    """The command's exit code and the lines it printed."""
    code = cli.main(list(argv))
    return code, capsys.readouterr().out.splitlines()


def env(folder: Path, text: str) -> None:
    (folder / ".env").write_text(text, encoding="utf-8")


def _no_network(self, request):
    raise AssertionError("The command went to the network.")


# ----------------------------------------------------------------------------- no keys: plain messages
@pytest.mark.parametrize("argv", [["pass"], ["loop"], ["story", "--ending", "1"], ["bank", "preflight", "--yes"]],
                         ids=["pass", "loop", "story", "bank preflight"])
def test_a_command_that_needs_the_sandbox_keys_says_so_plainly(capsys, here, argv):
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        code, said = run(capsys, *argv, "--data", str(here / "data"))
    assert (code, said) == (1, [NO_AIRWALLEX_KEYS])
    assert "Traceback" not in capsys.readouterr().err and not (here / "data").exists()


def test_status_says_when_there_is_no_database_yet_and_makes_none(capsys, here):
    code, said = run(capsys, "status", "--data", str(here / "data"))
    assert (code, said) == (0, [cli.NO_DATABASE.format(folder=(here / "data").resolve())])
    assert not (here / "data" / cli.LIVE_DB).exists()


def test_status_on_an_empty_database(capsys, here):
    path = (here / "data").resolve() / cli.LIVE_DB
    Database(path, stamp=lambda: MONDAY)
    before = path.read_bytes()
    code, said = run(capsys, "status", "--data", str(here / "data"))
    assert code == 0 and said == [f"Disputable: 0 disputes in {path}. No pass yet.", "",
                                  "At risk: $0.00. Cost so far: $0.00.", "Problems on the last pass: none.",
                                  "Alerts: none."]
    assert path.read_bytes() == before  # status writes nothing


def saved(folder: Path, record, now) -> Path:
    """A live database in folder holding this one record."""
    db = Database(folder.resolve() / cli.LIVE_DB, stamp=lambda: now)
    db.save(record, None, "poll")
    return db.path


def test_status_prints_the_rounds_notes_under_the_person_line(capsys, here, monkeypatch):
    late, now = STATES["late refund"]()
    path = saved(here / "late", late, now)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                               "('dst_broken', 1, '{broken', 'poll', ?)", (now.isoformat(),))
    finally:
        connection.close()
    monkeypatch.setattr(cli, "wall", lambda: now)
    code, said = run(capsys, "status", "--data", str(here / "late"))
    assert code == 0 and said[:2] == [f"Disputable: 2 disputes in {path}. No pass yet.", ""]
    person = w.PERSON_LINE.format(why=lifecycle.person_reason(late))
    assert said[2].endswith(person) and said[2].split()[:3] == ["Order", "#1121", "$12.00"]
    assert said[3] == " " * said[2].index(person) + lifecycle.LATE_REFUND  # right under the person line
    assert said[4].startswith("Dispute dst_broken") and said[4].endswith(
        "This app can't read its saved record. The saved record isn't valid JSON.")
    others, now = STATES["other refunds"]()
    saved(here / "others", others, now)
    monkeypatch.setattr(cli, "wall", lambda: now)
    code, said = run(capsys, "status", "--data", str(here / "others"))
    assert code == 0 and said[2].endswith(w.PERSON_LINE.format(why=lifecycle.OTHER_REFUNDS))
    assert said[3].strip() == others.rounds[-1].notes[0] and said[4] == ""  # the accept that didn't show yet


# ----------------------------------------------------------------------------- pass and loop on the fake sandbox
def monday(here: Path, monkeypatch, *, claude: bool = False) -> tuple[FakeAirwallex, Ticking]:
    """The bank's Monday on the fake sandbox, into the live database of here/data, with the sandbox keys in .env, the
    agent's session on the fake and the run's clock on the story's Monday."""
    fake, wall = FakeAirwallex(Sandbox(FERNHILL)), Ticking()
    db = Database((here / "data").resolve() / cli.LIVE_DB, stamp=wall)
    Issuer(fake.session("bank", sleep=wall.sleep), FERNHILL, db, run="a1b2c3d4",
           pay=lambda payment_id, secret: fake.pay(payment_id), clock=wall, sleep=wall.sleep).monday()
    env(here, KEYS + (CLAUDE_KEY if claude else ""))
    monkeypatch.setattr(cli, "_transport", fake.transport)
    monkeypatch.setattr(cli, "_throttle", lambda seconds: None)
    monkeypatch.setattr(cli, "wall", wall)
    monkeypatch.setattr(service, "WallClock", lambda: WallClock(wall))
    return fake, wall


def test_pass_then_status_then_a_pass_that_sends(capsys, here, monkeypatch):
    fake, _ = monday(here, monkeypatch)
    data = str(here / "data")
    code, said = run(capsys, "pass", "--data", data)
    assert code == 0 and said[0] == NO_CLAUDE_KEY
    assert said[1].startswith("Pass 1 at Nov 2, 17:00 UTC: read 3 disputes, sent 0, decided 3.")
    assert said[2:] == ["Problems: none.", "Alerts:",  # due 2 days and 15 minutes after the bank opened it
                        "- Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. It needs no approval and is ready to "
                        "send."]  # as it stands once the pass ended: the pass made its plan
    code, said = run(capsys, "status", "--data", data)
    path = (here / "data").resolve() / cli.LIVE_DB
    assert code == 0 and said[0] == f"Disputable: 3 disputes in {path}. Last pass Nov 2, 17:00 UTC."
    rows = [line for line in said if line.startswith("Order #")]
    assert [row.split()[:4] for row in rows] == [["Order", "#1121", "$12.00", "RFI"],
                                                  ["Order", "#1098", "$189.00", "CHARGEBACK"],
                                                  ["Order", "#1109", "$1,249.00", "RFI"]]
    assert rows[0].endswith("Code sends this on its own: accept $12.00 and refund $12.00.")
    assert rows[1].endswith("A person decides. Code's draft: accept $189.00, no refund. Approving it costs a review.")
    assert said[-3:] == ["Problems on the last pass: none.", "Alerts:",
                         "- Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. It needs no approval and is ready to "
                         "send."]
    assert "At risk: $1,450.00. Cost so far: $15.00." in said
    code, said = run(capsys, "pass", "--data", data)
    assert code == 0 and said[1].startswith("Pass 2 at ") and said[2] == "Sent: Order #1121. Airwallex took it."
    assert said[3:] == ["Problems: none.", "Alerts: none."]  # Airwallex took the ear tips' accept: no alert for it now
    assert [path for path in fake.seen("POST") if path.endswith("/accept")] == [
        f"/api/v1/pa/payment_disputes/{fake_id(fake, '1121')}/accept"]
    assert Database(path, stamp=lambda: MONDAY).meta("mode") == "live"


def fake_id(fake: FakeAirwallex, order: str) -> str:
    """The fake sandbox's dispute id of an order."""
    payments = {payment_id for payment_id, payment in fake.sandbox.payments.items()
                if payment.get("metadata", {}).get("order_no") == order}
    return next(dispute_id for dispute_id, dispute in fake.sandbox.disputes.items()
                if dispute["payment_intent_id"] in payments)


def test_a_pass_that_finds_another_running_says_so_and_exits_1(capsys, here, monkeypatch):
    """So a script that runs `disputable pass && ...` stops there: the pass never ran."""
    fake, _ = monday(here, monkeypatch)
    calls = len(fake.calls)
    held = storage.PassLock((here / "data").resolve() / service.PASS_LOCK)
    assert held.acquire()  # a loop's pass, running now
    try:
        code, said = run(capsys, "pass", "--data", str(here / "data"))
    finally:
        held.release()
    assert (code, said) == (1, [NO_CLAUDE_KEY, loop.SKIPPED]) and len(fake.calls) == calls  # nothing went to Airwallex


def test_a_data_folder_with_no_story_run_says_a_pass_there_reads_nothing(capsys, here, monkeypatch):
    """Only a story run fills the agent's scope: its own payments. A pass on another folder reads no dispute, and says
    why, however many the sandbox holds."""
    fake, _ = monday(here, monkeypatch)
    fresh = here / "fresh"
    calls = len(fake.calls)
    code, said = run(capsys, "pass", "--data", str(fresh))
    assert code == 0 and said[0] == (
        f"{fresh.resolve()} holds no story run, so a pass here reads nothing: the agent reads only the disputes on a "
        "story run's own payments. A story run (disputable story --ending 1) makes a story-<run> folder in it: use "
        "--data with that folder.")
    assert said[1].startswith("Pass 1 at ") and said[1].endswith("read 0 disputes, sent 0, decided 0.")
    assert len(fake.calls) == calls and len(fake.sandbox.disputes) == 3  # three open, and none in this folder's scope
    env(here, KEYS + CLAUDE_KEY)  # with a key too: no read is needed, so no cap or question
    monkeypatch.setattr(cli, "_claude", lambda key: pytest.fail("No read is needed, so no client is built."))
    code, said = run(capsys, "pass", "--data", str(fresh))
    assert code == 0 and said[0].startswith(f"{fresh.resolve()} holds no story run") and said[1].startswith("Pass 2 ")


def test_loop_runs_passes_until_ctrl_c(capsys, here, monkeypatch):
    monday(here, monkeypatch)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sleep", wait)
    code, said = run(capsys, "loop", "--every", "5", "--data", str(here / "data"))
    assert code == 0 and waits == [5, 5] and said[-1] == "The loop stopped."
    passes = [line for line in said if line.startswith("Pass ")]
    assert [line.split(":")[0] for line in passes] == ["Pass 1 at Nov 2, 17", "Pass 2 at Nov 2, 17"]
    assert said.count("Next pass in 5 seconds. Press Ctrl+C to stop.") == 2
    assert "Sent: Order #1121. Airwallex took it." in said


class Unpaid:
    """A Claude client stand-in that must never be asked: the cap stops every request before it goes."""

    class beta:
        class messages:
            @staticmethod
            def create(**params):
                raise AssertionError("A request went out past the cap.")


class Busy:
    """A Claude client stand-in that is never in reach, as on a busy day: each read waits for the next pass."""

    def __init__(self) -> None:
        self.asked = 0
        self.beta, self.messages = self, self

    def create(self, **params):
        self.asked += 1
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def cap_failure() -> Failure:
    """What spend.BudgetReader makes of a read its meter stopped."""
    class Over:
        def read(self, request):
            raise spend.BudgetReached("run", Decimal(0), Decimal("0.01"))

    return spend.BudgetReader(Over()).read(type("Request", (), {"key": "k"})()).failure


def reasons(here: Path) -> dict[str, tuple[str, ...]]:
    """Each live record's current plan reasons, by order."""
    records = Database((here / "data").resolve() / cli.LIVE_DB, stamp=lambda: MONDAY).load().records.values()
    return {cli._who(record): record.rounds[-1].plan.reasons for record in records if record.rounds[-1].plan}


def test_a_claude_key_needs_a_cap_and_a_yes_and_the_cap_holds(capsys, here, monkeypatch):
    fake, _ = monday(here, monkeypatch, claude=True)
    data, calls = str(here / "data"), len(fake.calls)
    monkeypatch.setattr(cli, "_claude", lambda key: pytest.fail("No Claude client may be built before a yes."))
    code, said = run(capsys, "pass", "--data", data)
    assert (code, said) == (1, ["This pass may make 3 live Claude reads. Give it a dollar cap, such as --cap 3."])
    asked = []
    monkeypatch.setattr("builtins.input", lambda question: asked.append(question) or "no")
    code, said = run(capsys, "pass", "--data", data, "--cap", "3")
    assert code == 1 and asked == ["Type yes to go on: "] and said[-1] == "Stopped. Nothing was read."
    assert said[:2] == ["This pass reads the cases that need it with Claude (claude-opus-5-5, effort high), with your key.",
                        "This run makes at most 3 live Claude reads, and stops before it spends more than $3.00."]
    assert len(fake.calls) == calls  # nothing went to Airwallex either
    monkeypatch.setattr(cli, "_claude", lambda key: Unpaid())
    code, said = run(capsys, "pass", "--data", data, "--cap", "0.01", "--yes")
    assert code == 0 and said[-2:] == ["The spend cap or the read count stopped 2 Claude reads. Those cases go to a "
                                       "person, with code's draft.", "Claude's live reads cost $0.00 in 0 requests."]
    records = Database((here / "data").resolve() / cli.LIVE_DB, stamp=lambda: MONDAY).load().records
    assert sorted(lifecycle.phase(record).value for record in records.values()) == ["APPROVE", "APPROVE", "SEND"]
    capped = f"({WHY[cap_failure()]})"  # the cap's own reason, never the Claude account's
    assert sorted(order for order, given in reasons(here).items() if any(capped in reason for reason in given)) == [
        "Order #1098", "Order #1109"]
    code, said = run(capsys, "loop", "--data", data)
    assert (code, said) == (1, ["This loop may make 15 live Claude reads. Give it a dollar cap, such as --cap 3."])
    code, said = run(capsys, "loop", "--data", data, "--cap", "3")
    assert code == 1 and said[:2] == [
        "This loop reads the cases that need it with Claude (claude-opus-5-5, effort high), with your key, until you "
        "press Ctrl+C. A round Claude was busy for is read again on the next pass, and that counts as a read. Start the "
        "loop again for more reads.",
        "This run makes at most 15 live Claude reads, and stops before it spends more than $3.00."]  # 5 a payment


def test_the_reads_a_user_said_yes_to_are_all_a_run_reads():
    class Counting:
        profile, effort = "a profile", "an effort"

        def __init__(self):
            self.asked = []

        def read(self, request):
            self.asked.append(request.key)
            if request.key == "capped":
                raise spend.BudgetReached("run", Decimal(0), Decimal(1))  # the dollar cap, on its way to BudgetReader
            return f"read {request.key}"

    inner = Counting()
    allowance = cli._Allowance(inner, 3)
    requests = [type("Request", (), {"key": key})() for key in ("a", "b", "capped", "d")]
    assert [allowance.read(request) for request in requests[:2]] == ["read a", "read b"]
    with pytest.raises(spend.BudgetReached):
        allowance.read(requests[2])
    over = allowance.read(requests[3])
    assert (over.key, over.failure, inner.asked) == ("d", Failure.CAPPED, ["a", "b", "capped"])  # nothing asked
    assert (allowance.reads, allowance.stopped) == (3, 2)  # the cap stopped one, the count one
    assert (allowance.profile, allowance.effort) == ("a profile", "an effort")
    assert WHY[over.failure] == "this run hit a spend cap or the number of reads it was given"


def test_pass_and_loop_hold_claude_to_the_reads_they_asked_for(capsys, here, monkeypatch):
    """Only the allowance holds a loop's busy re-reads to the count confirm_spend named: the dollar cap is apart."""
    fake, _ = monday(here, monkeypatch, claude=True)
    busy, readers, live = Busy(), [], service.live
    monkeypatch.setattr(cli, "_claude", lambda key: busy)
    monkeypatch.setattr(service, "live", lambda settings, **options: readers.append(options["reader"]) or live(
        settings, **options))
    monkeypatch.setattr(cli, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))
    assert run(capsys, "pass", "--data", str(here / "data"), "--cap", "3", "--yes")[0] == 0
    assert run(capsys, "loop", "--data", str(here / "data"), "--cap", "3", "--yes")[0] == 0
    assert [type(reader).__name__ for reader in readers] == ["BudgetReader", "BudgetReader"]
    allowances = [reader.reader for reader in readers]
    assert [(type(held).__name__, held.limit, held.reads) for held in allowances] == [("_Allowance", 3, 2),
                                                                                      ("_Allowance", 15, 2)]
    assert all(isinstance(held.reader, ClaudeReader) for held in allowances) and busy.asked == 4  # one request a read
    over = allowances[1]
    over.reads = over.limit  # once a loop has used up its reads, nothing more is asked
    assert over.read(type("Request", (), {"key": "k"})()).failure is Failure.CAPPED and busy.asked == 4


def test_the_claude_client_of_pass_and_loop_takes_nothing_from_the_shell(monkeypatch):
    """cli._claude is reader.claude_client, built here unpatched (cli-1, cli-5). The shell's proxy is a documentation
    address, and no request is sent: the test looks only at what the client would send through."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example.com")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer from-the-shell\nX-From-The-Shell: yes")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.10:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = cli._claude(" sk-made-up-key ")
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


# ----------------------------------------------------------------------------- serve
@pytest.fixture
def served(monkeypatch):
    """The screens' server, replaced: it keeps what it was given and answers 0."""
    given = []

    def serve(desk, *, port=8765, open_browser=True):
        given.append((desk, port, open_browser, desk.board()))
        return 0

    monkeypatch.setattr("disputable.web.serve", serve)
    return given


@pytest.mark.parametrize("name", ["disputable", "DisPutable", " ", "x" * 61, "Priya\tPatel", "Priya\nPatel"])
def test_serve_refuses_an_approver_who_isnt_a_person(capsys, here, served, name):
    code, said = run(capsys, "serve", "--approver", name, "--data", str(here / "data"))
    assert (code, said, served) == (1, [w.BAD_APPROVER], []) and not (here / "data").exists()


def test_serve_hands_the_live_service_to_the_screens_with_no_pass_client_or_reader(capsys, here, served):
    code, said = run(capsys, "serve", "--approver", " Priya ", "--port", "8800", "--no-open", "--data", str(here / "data"))
    assert code == 0 and said == [w.STARTING]
    (desk, port, open_browser, board), = served
    assert (port, open_browser) == (8800, False)
    assert isinstance(desk, service.Service) and (desk.approver, desk.mode, desk.reader, desk.client) == (
        "Priya", Mode.LIVE, None, None)
    assert board.store is FERNHILL and board.reader.source == "none" and board.demo is None
    db = Database((here / "data").resolve() / cli.LIVE_DB, stamp=lambda: MONDAY)
    db.set_meta("store", "ending_2")
    env(here, CLAUDE_KEY)  # a key changes only what the banner says: serve never reads with it
    code, said = run(capsys, "serve", "--data", str(here / "data"))
    assert code == 0 and said == [w.STARTING, w.APPROVALS_OFF]
    desk, port, open_browser, board = served[1]
    assert (desk.approver, port, open_browser, board.store, desk.reader, desk.client) == (
        None, 8765, True, ENDING_2, None, None)
    assert board.reader.source == "claude"


# ----------------------------------------------------------------------------- demo
@pytest.fixture
def recorded(monkeypatch):
    """The fake sandbox's recordings where the demo looks for the committed ones."""
    folders = recordings.recorded()
    monkeypatch.setattr(cli, "STORY", folders["1"].parent)
    return folders


@pytest.mark.parametrize("ending, count, total", [("1", 13, "$279.30"), ("2", 11, "$1,512.30")])
def test_demo_no_serve_plays_a_recording_with_no_env_and_no_network(capsys, here, recorded, ending, count, total):
    (here / ".env").write_bytes(b"\xff\xfe not a settings file")  # the demo never reads it
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        code, said = run(capsys, "demo", "--ending", ending, "--no-serve")
    assert code == 0 and said[0] == w.STARTING
    assert said[1:4] == [f"Disputable demo. {w.ENDINGS[ending]}.",
                         f"Replayed from {recorded[ending]}, recorded on the Airwallex sandbox on Nov 2, 2026.", OFFLINE]
    assert said[4].startswith(f"Step 1 of {count}, Monday 9:00: The bank opens three disputes")
    assert said[-3:] == [f"The story ends. The replay cost {total}.",
                         f"The recording's own total for this ending: {total}.",
                         "Still at risk: $0.00. Disputes closed: 3."]


STAND_IN_LINE = "Replayed from {folder}, made on a stand-in for the Airwallex sandbox. It isn't a recording of Airwallex."
OFFLINE = "No keys and no network: every Airwallex answer and Claude reading comes from the recording."
STAND_IN_OFFLINE = "No keys and no network: every answer and Claude reading comes from the recording."
OFFLINE_NONE = "No keys and no network: every Airwallex answer comes from the recording. It holds no Claude reading."
STAND_IN_OFFLINE_NONE = "No keys and no network: every answer comes from the recording. It holds no Claude reading."


@pytest.fixture
def stand_ins(monkeypatch):
    """The stand-in maker's recordings (scripts/record_standin.py --no-claude) where the demo looks for the committed
    ones."""
    folders = recordings.standins().folders()
    monkeypatch.setattr(cli, "STORY", folders["1"].parent)
    return folders


def test_the_demo_says_a_stand_in_isnt_a_recording_of_airwallex(capsys, here, stand_ins, served):
    """In place of the lines that say where and when the sandbox run was recorded and that its Airwallex answers come
    from the recording, at the start and, with --no-serve, before any step plays. These stand-ins were made with
    --no-claude, so the line after the stand-in line says the recording holds no Claude reading."""
    code, said = run(capsys, "demo", "--no-open")
    assert (code, said) == (0, [w.STARTING, f"Disputable demo. {w.ENDINGS['1']}.",
                                STAND_IN_LINE.format(folder=stand_ins["1"]), STAND_IN_OFFLINE_NONE])
    (desk, _, _, board), = served
    assert (board.demo.airwallex, board.reader.source) == ("stand-in", "none")
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        code, said = run(capsys, "demo", "--ending", "2", "--no-serve")
    assert code == 0 and said[1:4] == [f"Disputable demo. {w.ENDINGS['2']}.", STAND_IN_LINE.format(folder=stand_ins["2"]),
                                       STAND_IN_OFFLINE_NONE]
    assert said[4].startswith("Step 1 of 11, Monday 9:00: The bank opens three disputes")
    assert said[-3:] == ["The story ends. The replay cost $1,528.30.",  # made with no Claude reading
                         "The recording's own total for this ending: $1,528.30.",
                         "Still at risk: $0.00. Disputes closed: 3."]
    assert not [line for line in said if "recorded on the Airwallex sandbox" in line or "Airwallex answer" in line]
    assert not [line for line in said if "Claude reading comes" in line]
    assert (cli.DEMO_FROM_STANDIN, cli.DEMO_OFFLINE_STANDIN, cli.DEMO_OFFLINE_STANDIN_NONE, cli.DEMO_OFFLINE_NONE) == (
        STAND_IN_LINE, STAND_IN_OFFLINE, STAND_IN_OFFLINE_NONE, OFFLINE_NONE)


def test_the_demo_names_a_claude_reading_only_when_its_recording_holds_one(capsys, here, served, monkeypatch):
    """A stand-in made with Claude's readings keeps the line that every answer and Claude reading comes from the
    recording. A recording made with --no-claude on the sandbox says it holds no Claude reading, and still names the
    Airwallex answers it holds."""
    made = recordings.standins("claude").folders()
    monkeypatch.setattr(cli, "STORY", made["1"].parent)
    code, said = run(capsys, "demo", "--no-open")
    assert (code, said[2:]) == (0, [STAND_IN_LINE.format(folder=made["1"]), STAND_IN_OFFLINE])
    assert (served[-1][3].demo.airwallex, served[-1][3].reader.source) == ("stand-in", "claude")
    folder = here / "story" / "ending-1"  # a --no-claude run, as if the sandbox had answered it
    recording.copy(recordings.standins().folders()["1"], folder)
    manifest = json.loads((folder / recording.MANIFEST).read_text(encoding="ascii"))
    (folder / recording.MANIFEST).write_text(json.dumps({**manifest, "airwallex": "sandbox"}, indent=2) + "\n",
                                             encoding="ascii", newline="\n")
    monkeypatch.setattr(cli, "STORY", here / "story")
    code, said = run(capsys, "demo", "--no-open")
    assert (code, said[2:]) == (0, [f"Replayed from {folder}, recorded on the Airwallex sandbox on Nov 2, 2026.",
                                    OFFLINE_NONE])
    assert (served[-1][3].demo.airwallex, served[-1][3].reader.source) == ("sandbox", "none")


def test_a_kind_the_cli_doesnt_know_never_passes_for_the_sandbox():
    folder = Path("fixtures") / "story" / "ending-1"
    sandbox = f"Replayed from {folder}, recorded on the Airwallex sandbox on Nov 2, 2026."
    assert (cli._replayed_from(folder, demo_bar()), cli._offline(demo_bar(), "claude")) == (sandbox, OFFLINE)
    for kind in ("stand-in", "something else", ""):
        bar = demo_bar(airwallex=kind)
        assert (cli._replayed_from(folder, bar), cli._offline(bar, "claude")) == (STAND_IN_LINE.format(folder=folder),
                                                                                  STAND_IN_OFFLINE), kind
    for source in ("none", "scripted", ""):  # only a recording of Claude's own readings names a Claude reading
        assert cli._offline(demo_bar(), source) == OFFLINE_NONE, source
        for kind in ("stand-in", "something else", ""):
            assert cli._offline(demo_bar(airwallex=kind), source) == STAND_IN_OFFLINE_NONE, (kind, source)


def test_demo_serves_the_replay_to_the_screens_and_cleans_up_after(capsys, here, recorded, served, monkeypatch):
    closed, close = [], Demo.close
    monkeypatch.setattr(Demo, "close", lambda demo: closed.append(demo) or close(demo))
    code, said = run(capsys, "demo", "--ending", "2", "--port", "8801", "--no-open")
    assert code == 0 and said[0] == w.STARTING and said[1] == f"Disputable demo. {w.ENDINGS['2']}."
    (desk, port, open_browser, board), = served
    assert isinstance(desk, Demo) and (port, open_browser) == (8801, False)
    assert board.mode is Mode.DEMO and (board.demo.ending, board.demo.endings, board.demo.count) == ("2", ("1", "2"), 11)
    assert closed == [desk]  # its temp folder goes once the server stops


def test_demo_without_a_recording_says_how_to_make_one(capsys, here, monkeypatch):
    monkeypatch.setattr(cli, "STORY", here / "story")
    code, said = run(capsys, "demo", "--no-serve")  # no recordings folder at all: an install that isn't a clone
    assert (code, said) == (1, [f"There are no recordings in {here / 'story'}. They come with a clone of the "
                                "repository: run the demo from the clone, after uv sync."])
    (here / "story" / "ending-1").mkdir(parents=True)  # a folder with nothing in it
    code, said = run(capsys, "demo", "--ending", "2")
    assert code == 1 and said == [f"There's no recording in {here / 'story' / 'ending-2'}. Record one with `disputable "
                                  "story --ending 2` (it needs the sandbox keys)."]
    code, said = run(capsys, "demo", "--no-serve")
    assert code == 1 and said[-1] == (f"The recording in {here / 'story' / 'ending-1'} can't be replayed. It has no "
                                      "manifest.json.")
    code, said = run(capsys, "demo", "--ending", "3")
    assert (code, said) == (1, ["There's no ending 3. The endings are 1 and 2."])  # never "record ending 3"
    assert "Traceback" not in capsys.readouterr().err


def test_a_clone_without_a_recording_says_how_to_make_a_stand_in(capsys, here, monkeypatch):
    """A clone holds a pyproject.toml in the folder that holds fixtures/. Its lines name the stand-in maker, which needs
    no keys, and never send the person who cloned it back to the clone."""
    clone = here / "clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text("[project]\nname = \"disputable\"\n", encoding="utf-8")
    story_folder = clone / "fixtures" / "story"
    monkeypatch.setattr(cli, "STORY", story_folder)
    code, said = run(capsys, "demo", "--no-serve")  # no fixtures/story at all
    assert (code, said) == (1, [f"There are no recordings in {story_folder}. Make a stand-in, with no keys: uv run python "
                                "scripts/record_standin.py (add --no-claude if it says the Claude readings aren't "
                                "there)."])
    (story_folder / "ending-1").mkdir(parents=True)  # one ending, and not the other
    code, said = run(capsys, "demo", "--ending", "2")
    assert (code, said) == (1, [f"There's no recording in {story_folder / 'ending-2'}. Record one with `disputable story "
                                "--ending 2` (it needs the sandbox keys). Or make a stand-in, with no keys: uv run "
                                "python scripts/record_standin.py --ending 2 (add --no-claude if it says the Claude "
                                "readings aren't there)."])
    for line in (cli.NO_RECORDINGS_CLONE.format(path=story_folder),
                 cli.NO_RECORDING_CLONE.format(path=story_folder / "ending-1", ending="1")):
        assert "scripts/record_standin.py" in line and "run the demo from the clone" not in line
    assert "Traceback" not in capsys.readouterr().err


def test_the_serve_command_a_surprised_story_run_prints_can_be_pasted_as_it_is(tmp_path):
    """A data folder may hold a space (the macOS default does), so the folder is quoted: the command parses back to it."""
    for folder in (config.default_data_dir(platform="darwin", home=Path("/Users/judge")) / "story-1a2b3c4d",
                   tmp_path / "Jo Smith" / "Disputable" / "story-1a2b3c4d"):
        line = story.LOOK.format(folder=folder)
        argv = shlex.split(line.split("(", 1)[1].split(")", 1)[0])
        assert argv[:3] == ["disputable", "serve", "--data"] and len(argv) == 4, argv
        assert cli._parser().parse_args(argv[1:]).data == folder


def test_a_stray_or_broken_ending_folder_never_stops_the_demo_of_another(capsys, here, monkeypatch):
    """A backup named ending-1-old is no ending, and an ending-2 whose copy stopped half way is left out, with a line
    that names it."""
    story_folder = here / "story"
    recording.copy(recordings.recorded()["1"], story_folder / "ending-1")
    recording.copy(recordings.recorded()["1"], story_folder / "ending-1-old")
    recording.copy(recordings.recorded()["2"], story_folder / "ending-2")
    (story_folder / "ending-2" / recording.BEATS).unlink()
    monkeypatch.setattr(cli, "STORY", story_folder)
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        code, said = run(capsys, "demo", "--no-serve")
    assert code == 0 and said[4] == (f"Ending 2 can't be offered. The recording in {story_folder / 'ending-2'} can't be "
                                     "replayed. It has no beats.json.")
    assert said[5].startswith("Step 1 of 13, Monday 9:00:") and said[-3] == "The story ends. The replay cost $279.30."
    assert cli._recordings(story_folder) == {"1": story_folder / "ending-1", "2": story_folder / "ending-2"}


@pytest.mark.parametrize("kind", ["sandbox", "stand-in"])
def test_a_replay_that_stops_matching_prints_its_line_and_exits_1(capsys, here, monkeypatch, kind):
    """The line the web bar shows for the same mismatch, by what answered for Airwallex in the recording: what changed,
    and what to do about it. One reading is taken out, as a changed check or prompt moves a reading key."""
    folder = here / "story" / "ending-1"
    recording.copy((recordings.recorded() if kind == "sandbox" else recordings.standins().folders())["1"], folder)
    recorded = recording.load(folder)
    readings = recorded.readings
    gone = sorted(readings.readings)[0]
    kept = ReadingsFile(readings.header, {key: value for key, value in readings.readings.items() if key != gone},
                        {key: day for key, day in readings.dates.items() if key != gone})
    recording.save(dataclasses.replace(recorded, readings=kept,
                                       manifest=dataclasses.replace(recorded.manifest, readings=len(kept.readings))),
                   folder)
    demo = Demo({"1": folder}, store_for=cli._store)
    try:
        with pytest.raises(ReplayMismatch) as stopped:
            demo.play_all()
        assert demo.board().demo.airwallex == kind
    finally:
        demo.close()
    mismatch = str(stopped.value)
    assert mismatch.endswith(": the recording holds no reading for this case as the code reads it now.")
    monkeypatch.setattr(cli, "STORY", here / "story")
    code, said = run(capsys, "demo", "--no-serve")
    shown = w.MISMATCH if kind == "sandbox" else w.MISMATCH_STANDIN  # as the bar says it
    assert code == 1 and said[-1] == shown.format(mismatch=mismatch) and mismatch not in said[:-1]
    assert said[-2].startswith("Step 2 of 13") and "Traceback" not in capsys.readouterr().err


# ----------------------------------------------------------------------------- story, bank, eval and the rest
def test_story_and_the_bank_preflight_go_to_their_modules(capsys, here, monkeypatch):
    env(here, KEYS)
    calls = []
    monkeypatch.setattr(story, "run", lambda name, settings, **options: calls.append((name, settings, options)) or 7)
    code, _ = run(capsys, "story", "--ending", "2", "--no-claude", "--lenient", "--cap", "2.50", "--yes", "--data",
                  str(here / "data"))
    (name, settings, options), = calls
    assert (code, name, settings.data_dir, settings.airwallex_client_id) == (7, "2", (here / "data").resolve(), CLIENT_ID)
    assert options == {"claude": False, "lenient": True, "cap": Decimal("2.50"), "yes": True, "ask": cli._ask}
    run(capsys, "story", "--ending", "1", "--data", str(here / "data"))
    assert calls[1][2] == {"claude": True, "lenient": False, "cap": None, "yes": False, "ask": cli._ask}
    asked = []
    monkeypatch.setattr("disputable.issuer.preflight", lambda settings, **options: asked.append(options) or 0)
    assert run(capsys, "bank", "preflight", "--data", str(here / "data")) == (0, [])
    assert asked == [{"yes": False, "ask": cli._ask}]
    assert run(capsys, "bank", "preflight", "--yes", "--data", str(here / "data"))[0] == 0 and asked[1]["yes"]


def test_a_bank_step_refused_a_setup_problem_and_ctrl_c_print_plainly_and_a_bug_comes_out(capsys, here, monkeypatch):
    env(here, KEYS)
    for raised, line in ((IssuerError("Order #1109 can't be pushed back yet."), "Order #1109 can't be pushed back yet."),
                         (ReplayMismatch("Step 3 was played already."), "Step 3 was played already."),
                         (KeyboardInterrupt(), cli.STOPPED)):
        def stopped(name, settings, *, error=raised, **options):
            raise error

        monkeypatch.setattr(story, "run", stopped)
        assert run(capsys, "story", "--ending", "1", "--data", str(here / "data")) == (1, [line])
    env(here, KEYS + "AWX_BASE_URL=https://api.airwallex.com\n")
    code, said = run(capsys, "status", "--data", str(here / "data"))
    assert code == 1 and said == ["AWX_BASE_URL must be an https://*.sandbox.airwallex.com address. This app only talks "
                                  "to the Airwallex sandbox."]
    env(here, KEYS)
    monkeypatch.setattr(story, "run", lambda name, settings, **options: 1 / 0)
    with pytest.raises(ZeroDivisionError):  # a bug keeps its traceback
        cli.main(["story", "--ending", "1", "--data", str(here / "data")])


def test_eval_passes_every_argument_through(capsys, monkeypatch):
    given = []
    monkeypatch.setattr("disputable.evals.main", lambda argv: given.append(list(argv)) or 3)
    assert cli.main(["eval", "live", "--pilot", "--cap", "3"]) == 3
    assert cli.main(["eval"]) == 3 and cli.main(["eval", "--help"]) == 3
    assert given == [["live", "--pilot", "--cap", "3"], [], ["--help"]]


def test_python_dash_m_disputable_runs_the_same_commands(monkeypatch):
    from disputable import __main__ as entry

    given = []
    monkeypatch.setattr("disputable.evals.main", lambda argv: given.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["disputable", "eval", "scripted"])
    assert entry.run() == 0 and given == [["scripted"]]


@pytest.mark.parametrize("argv, code", [
    ([], 1), (["--help"], 0), (["dance"], 2), (["story"], 2), (["pass", "--cap", "-1"], 2), (["pass", "--cap", "x"], 2),
    (["demo", "--port", "0"], 2), (["demo", "--ending", "../ending-1"], 2), (["loop", "--every", "0"], 2),
    (["bank"], 2), (["bank", "monday"], 2)])
def test_a_wrong_command_line_says_how_to_write_it(capsys, argv, code):
    assert cli.main(argv) == code
    printed = capsys.readouterr()
    assert "usage: disputable" in printed.out + printed.err and "Traceback" not in printed.err


def test_the_cli_keeps_the_cores_names_and_rules():
    assert cli.LIVE_DB == service.LIVE_DB and cli.STORY == recording.FOLDER
    assert cli.ENDINGS == tuple(story.ENDINGS) and cli.NO_ENDING == story.NO_ENDING  # the demo never loads the story
    assert {name: cli._store(name) for name in storage.STORES} == dict(story.STORES) and cli._store(None) is FERNHILL
    record, _ = STATES["late refund"]()
    nameless = dataclasses.replace(record, last=dataclasses.replace(record.last, order_number=None))
    assert [cli._who(record), cli._who(nameless)] == [loop.who(record), loop.who(nameless)]
    for name in ("Priya", " Priya ", "P", "x" * 60, "x" * 61, "", " ", "disputable", "DISPUTABLE", "a\tb", "Zo\u00eb"):
        try:
            service._approver(name)
            takes = True
        except ValueError:
            takes = False
        assert cli._named(name) == takes, name


def test_the_cli_loads_nothing_slow_before_a_command_runs():
    tree = ast.parse(Path(importlib.util.find_spec("disputable.cli").origin).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:  # the module's own imports, not those inside its functions or under TYPE_CHECKING
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert names == {"__future__", "argparse", "re", "sys", "time", "datetime", "decimal", "pathlib", "typing",
                     "disputable", "disputable.clock", "disputable.web"}
