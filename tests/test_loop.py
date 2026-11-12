"""The loop: passes over this run's disputes, through HTTP on the fake sandbox, with a real database in a temp folder.

The story runs as the CLI runs it: the bank's Monday step writes the run's payments, each pass reads, polls, saves,
alerts, sends and decides, and Priya approves through the Service, as the approval screen does. Offline: the agent's
client talks to tests/fake_airwallex.py through httpx.MockTransport, and the reader is the scripted perfect reader, a
short scripted one or NoReader. The send step's guards are test_send's and the Service's are test_service's; both use the
helpers here.
"""

from __future__ import annotations

import ast
import atexit
import copy
import functools
import importlib.util
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from disputable import jsonable, lifecycle, prompts, rounds, rules
from disputable.airwallex import DISPUTES, PAYMENTS, Outcome
from disputable.clock import RecordingClock, ReplayMismatch
from disputable.db import APPROVAL_UNREADABLE, SCOPE_ROW, TRY_UNREADABLE, Database, IssuerDate, PassLock, StorePayment
from disputable.desk import ReaderInfo, Result
from disputable.engine import Status
from disputable.lifecycle import Phase, cost, phase, to_json
from disputable.loop import (BAD_PAYMENT, BAD_SCOPE, BROKEN, CONFLICT, DECIDE_ERROR, LIST_FAILED, NAMELESS, NOT_IN_SCOPE,
                             NOT_LISTED, OTHER_DISPUTE, OTHER_PAYMENT, OTHER_PAYMENTS, READ_FAILED, SKIPPED, TWO_PAYMENTS,
                             PassReport, run_pass)
from disputable.reader import WHY, Failure, NoReader
from disputable.rounds import BUSY_NOTE, E1, HELD_FOR_ERROR
from disputable.seed.fernhill import ENDING_2, FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.seed.scripted import Scripted, perfect_for
from disputable.service import Service
from fake_airwallex import FakeAirwallex
from sandbox import RUN_DATE, Sandbox
from test_db import child_holding, line_from
from test_rounds import Reads, busy

CASES = ("ear_tips", "earbuds", "headphones")
INFO = ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "scripted", prompts.MODEL, None, None)
MINUTE = timedelta(minutes=1)
NO_KEY = E1.format(why=WHY[Failure.NO_KEY])
ONE_PAYMENT = "Another dispute is on this payment. A person handles it, so the customer is never paid twice."


# ----------------------------------------------------------------------------- a live run on the fake sandbox
class Wall:
    """The run's wall clock: it says what the test set, in UTC, and moves only when told."""

    def __init__(self, at: datetime = MONDAY) -> None:
        self.at = at.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def move(self, length: timedelta) -> datetime:
        self.at += length
        return self.at

    def set(self, at: datetime) -> datetime:
        self.at = at.astimezone(timezone.utc)
        return self.at


class Clock(RecordingClock):
    """The run's clock: the wall clock, journaled, with a hook a test can run right after the moment of a label."""

    def __init__(self, wall: Wall) -> None:
        super().__init__(wall)
        self.after: dict[str, Callable[[], object]] = {}

    def now(self, label: str) -> datetime:
        moment = super().now(label)
        hook = self.after.pop(label, None)
        if hook is not None:
            hook()
        return moment

    def labels(self) -> list[str]:
        return [label for label, _ in self.journal]


class Fake(FakeAirwallex):
    """The fake sandbox with hooks, each run before a request is answered, so a test can act in the middle of a pass."""

    def __init__(self, sandbox: Sandbox) -> None:
        super().__init__(sandbox)
        self.hooks: list[Callable[[httpx.Request], object]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        for hook in list(self.hooks):
            hook(request)
        return super().handle(request)

    def posts(self, ending: str = "") -> list[httpx.Request]:
        """The agent's POSTs whose path ends so, in order: "/accept", "/challenge" or "/upload"."""
        return [request for request in self.calls if request.method == "POST" and request.url.path.endswith(ending)
                and not request.url.path.endswith("/login")]

    def gets(self, path: str) -> int:
        return sum(request.method == "GET" and request.url.path == path for request in self.calls)


class Run:
    """A live run as the CLI runs one: the database in a folder, the agent's client on the fake sandbox, the store, a
    wall clock the test moves, and the Service over them. open() plays the bank's Monday step for one case."""

    def __init__(self, folder: Path, *, store=FERNHILL, reader=None, cases=CASES, approver="Priya", fake=None,
                 at: datetime = MONDAY) -> None:
        self.folder, self.store, self.approver = Path(folder), store, approver
        self.wall = Wall(at)
        self.fake = Fake(Sandbox(store)) if fake is None else fake
        self.reader = Scripted(perfect_for) if reader is None else reader
        self.service = self.start()
        for case_id in cases:
            self.open(case_id)

    def start(self) -> Service:
        """A Service on the run's file, as a fresh process makes one: everything else comes from the database."""
        clock = Clock(self.wall)
        return Service(db=Database(self.folder / "live.sqlite3", stamp=clock.peek), store=self.store, clock=clock,
                       info=INFO, approver=self.approver, reader=self.reader, client=self.fake.client(),
                       lock=PassLock(self.folder / "pass.lock"))

    def restart(self) -> Service:
        self.service = self.start()
        return self.service

    @property
    def db(self) -> Database:
        return self.service.db

    @property
    def clock(self) -> Clock:
        return self.service.clock

    def open(self, case_id: str, *, dispute_id: str | None = None, payment_id: str | None = None) -> str:
        """The bank's Monday step: the case's dispute on the fake, its payment in the run's scope, and its opening date."""
        dispute_id = self.fake.sandbox.open(case_id, now=self.wall.at, dispute_id=dispute_id, payment_id=payment_id)
        case, payment_id = self.store.case(case_id), self.fake.sandbox.disputes[dispute_id]["payment_intent_id"]
        self.db.put_store_payment(StorePayment(payment_id, case.order_number, f"FH-{case.order_number}-test", RUN_DATE))
        self.db.put_issuer_date(IssuerDate(dispute_id, case_id, case.opened_at))
        return dispute_id

    def pass_(self, after: timedelta = timedelta()) -> PassReport:
        self.wall.move(after)
        return run_pass(self.service)

    def board(self):
        return self.service.board()

    def record(self, dispute_id: str):
        return self.db.load_one(dispute_id)[0]

    def approve_all(self) -> dict[str, Result]:
        """Priya approves every plan that waits, through the Service, with what the approval screen posts."""
        board = self.board()
        return {dispute_id: self.service.approve(dispute_id, *posted(board, dispute_id))
                for dispute_id in sorted(board.records) if phase(board.records[dispute_id]) is Phase.APPROVE}

    def total(self) -> Decimal:
        return sum((cost(record) for record in self.board().records.values()), Decimal(0))


def plan_text(plan: lifecycle.Plan) -> str:
    """The plan as the approval page renders it into its form."""
    return json.dumps(jsonable.dump(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def posted(board, dispute_id: str) -> tuple[str, str]:
    """What the approval screen posts for a case: the plan it showed, and the key of the reading shown beside it."""
    record = board.records[dispute_id]
    step = rounds.current(record, board.store, board.saved, board.now, profile=board.reader.profile,
                          effort=board.reader.effort)
    return plan_text(record.rounds[-1].plan), step.reading.key if step.reading is not None else ""


def play(run: Run, ruling: Status | None = Status.WON, *, restart: bool = False) -> Run:
    """The story, as the bank and Priya play it with the loop: Monday, the push back three weeks later, the ruling.
    restart: a new Service on the same file before every step, as a crash and a restart would make it."""
    def step() -> None:
        if restart:
            run.restart()
    step(); run.pass_()  # Monday 9:00: the agent reads and decides
    step(); run.wall.move(5 * MINUTE); run.approve_all()  # Priya approves the earbuds and the headphones
    step(); run.pass_(2 * MINUTE)  # the agent sends what is ready
    step(); run.pass_(MINUTE)  # and reads: the ear tips and the earbuds close, the headphones wait for the bank
    step(); run.wall.set(THREE_WEEKS_LATER); run.fake.sandbox.escalate("dst_headphones"); run.pass_()  # round two
    step(); run.wall.move(MINUTE); run.approve_all()
    step(); run.pass_(MINUTE)
    step(); run.pass_(MINUTE)
    if ruling is not None and phase(run.record("dst_headphones")) is Phase.WAIT:
        step(); run.wall.move(timedelta(weeks=1)); run.fake.sandbox.resolve("dst_headphones", ruling); run.pass_()
    return run


# ----------------------------------------------------------------------------- runs played once a session
def _scratch() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="disputable-loop-"))
    atexit.register(shutil.rmtree, folder, True)
    return folder


STORIES = {  # (store, reader, ruling, total)
    "ending 1 won": (FERNHILL, "perfect", Status.WON, "279.30"),
    "ending 1 lost": (FERNHILL, "perfect", Status.LOST, "1778.30"),
    "ending 2": (ENDING_2, "perfect", None, "1512.30"),
    "ending 2, no key": (ENDING_2, "none", None, "1528.30"),
}


@functools.cache
def ended(name: str) -> Run:
    """A story played to its end once a session. Read it only: other tests share it."""
    store, reader, ruling, _ = STORIES[name]
    return play(Run(_scratch(), store=store, reader=NoReader() if reader == "none" else None), ruling)


@functools.cache
def _played(step: str) -> tuple[bytes, Any, datetime]:
    """The run at a step, made once a session: its database file, the fake sandbox's state and the time."""
    run = Run(_scratch())
    run.pass_()  # monday: the ear tips are ready to send, the earbuds and the headphones wait for Priya
    if step in ("approved", "pushback"):
        run.wall.move(5 * MINUTE)
        assert set(run.approve_all().values()) == {Result(True, "approved")}
    if step == "pushback":  # sent, read, then round two at the chargeback stage, decided
        run.pass_(2 * MINUTE)
        run.pass_(MINUTE)
        run.wall.set(THREE_WEEKS_LATER)
        run.fake.sandbox.escalate("dst_headphones")
        run.pass_()
    connection = sqlite3.connect(run.folder / "live.sqlite3")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # so the file alone holds everything
    finally:
        connection.close()
    fake, sandbox = run.fake, run.fake.sandbox
    state = (sandbox.disputes, sandbox.payments, sandbox.opened, sandbox.ticks, fake.files, fake.request_ids, fake.logins,
             fake._made)
    return (run.folder / "live.sqlite3").read_bytes(), copy.deepcopy(state), run.wall.at


def at_step(folder: Path, step: str, **options: Any) -> Run:
    """A run of the story's three cases at a step, in its own folder: "monday" after the first pass, "approved" after
    Priya's two approvals, "pushback" with round two of the headphones decided. A copy of a run made once a session."""
    data, state, at = _played(step)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "live.sqlite3").write_bytes(data)
    fake = Fake(Sandbox(options.get("store", FERNHILL)))
    sandbox = fake.sandbox
    (sandbox.disputes, sandbox.payments, sandbox.opened, sandbox.ticks, fake.files, fake.request_ids, fake.logins,
     fake._made) = copy.deepcopy(state)
    return Run(folder, cases=(), fake=fake, at=at, **options)


# ----------------------------------------------------------------------------- the story
@pytest.mark.parametrize("name", list(STORIES))
def test_the_story_costs_what_it_says(name):
    run, (store, reader, _, total) = ended(name), STORIES[name]
    board = run.board()
    assert {phase(record) for record in board.records.values()} == {Phase.CLOSED} and not board.broken
    assert run.total() == Decimal(total)
    # The agent read and responded, and nothing else: no issuer's route, and each body names this app.
    assert not [path for path in run.fake.seen() if "/simulation" in path]
    for request in run.fake.posts("/accept") + run.fake.posts("/challenge"):
        body = json.loads(request.content)
        assert body.get("accepted_by", body.get("challenged_by")) == "disputable"
    assert len(run.fake.posts("/accept")) == (2 if store is FERNHILL else 3)
    assert len(run.fake.posts("/challenge")) == len(run.fake.posts("/upload")) == (2 if store is FERNHILL else 1)
    # Priya approved each plan that waited for her, through the Service: the earbuds' and both rounds of the headphones.
    assert [(row.dispute_id, row.round_number, row.by) for rows in board.approvals.values() for row in rows] == [
        ("dst_earbuds", 1, "Priya"), ("dst_headphones", 1, "Priya"), ("dst_headphones", 2, "Priya")]
    plans = [r.plan for record in board.records.values() for r in record.rounds]
    if reader == "none":  # every round read goes to a person with code's draft, and approving it costs a review
        assert all(plan.hand_off and NO_KEY in plan.reasons for plan in plans if plan.terms.amount > 50)
    else:
        assert not any(plan.hand_off for plan in plans if plan.terms.dispute_id == "dst_headphones")
    assert not run.db.passes(limit=1)[0].problems  # the last pass found nothing wrong


def test_only_the_ear_tips_alert_on_monday():
    run = ended("ending 1 won")
    monday = run.db.passes(limit=50)[-1]  # newest first: the run's first pass is the last
    assert [(alert.dispute_id, alert.urgency.value) for alert in monday.alerts] == [("dst_ear_tips", "SOON")]
    # Worked out from the poll result, before any decision: the ear tips' plan is made later in the same pass.
    assert monday.alerts[0].message == "Order #1121 is due in 2 days, on Nov 4 at 17:00 UTC. Code hasn't made a plan yet."
    sending = run.db.passes(limit=50)[-2]  # the next pass, which sends it
    assert [alert.message for alert in sending.alerts] == [
        "Order #1121 is due in 1 day, on Nov 4 at 17:00 UTC. It needs no approval and is ready to send."]


def test_the_moments_of_a_pass_in_order(tmp_path):
    run = Run(tmp_path)
    run.pass_()
    run.wall.move(5 * MINUTE)
    run.approve_all()
    run.pass_(2 * MINUTE)
    sends = [f"{kind}:dst_{case}" for case in CASES for kind in ("send", "post", "answer")]
    assert run.clock.labels() == ["pass", "decide:dst_ear_tips", "decide:dst_earbuds", "decide:dst_headphones",
                                  "pass:end", "approve:dst_earbuds", "approve:dst_headphones", "pass", *sends, "pass:end"]
    moments = [moment for _, moment in run.clock.journal]
    assert moments == sorted(moments) and {moment.utcoffset() for moment in moments} == {timedelta(0)}


def test_a_restart_reloads_everything():
    again = play(Run(_scratch()), restart=True)
    once = ended("ending 1 won")
    assert again.total() == once.total() == Decimal("279.30")
    assert {key: to_json(record) for key, record in again.board().records.items()} == {
        key: to_json(record) for key, record in once.board().records.items()}
    assert again.board().approvals == once.board().approvals
    assert again.fake.seen("POST").count(f"{DISPUTES}/dst_headphones/challenge") == 2  # never sent twice


# ----------------------------------------------------------------------------- the scope and the reads
def test_only_this_runs_disputes(tmp_path):
    run = Run(tmp_path)
    run.fake.sandbox.open("earbuds", dispute_id="dst_older_run", payment_id="int_older_run")  # on another payment
    run.fake.sandbox.open("headphones", dispute_id="dst_probe", payment_id="int_probe")  # the spike's probe, say
    report = run.pass_()
    listed = [request.url.params.get("payment_intent_id") for request in run.fake.calls
              if request.method == "GET" and request.url.path == DISPUTES]
    assert listed == ["int_ear_tips", "int_earbuds", "int_headphones"]  # the run's own payments, by id
    assert report.read == ("dst_ear_tips", "dst_earbuds", "dst_headphones") and set(run.board().records) == set(report.read)
    for other in ("dst_older_run", "dst_probe", "int_older_run", "int_probe"):
        assert not [path for path in run.fake.seen() if other in path]
    assert not report.problems


class Unfiltered(Fake):
    """A list of disputes that ignores its payment filter while unfiltered is set, as an API change could make it."""

    unfiltered = True

    def _list(self, request: httpx.Request) -> httpx.Response:
        if not self.unfiltered:
            return super()._list(request)
        return httpx.Response(200, json={"items": [copy.deepcopy(dispute) for dispute in self.sandbox.disputes.values()]})


def test_a_listed_dispute_on_another_payment_is_never_read(tmp_path):
    fake = Unfiltered(Sandbox(FERNHILL))
    run = Run(tmp_path, fake=fake)
    fake.sandbox.open("ear_tips", dispute_id="dst_old_ear_tips", payment_id="int_old_ear_tips")  # an older run's
    fake.sandbox.open("headphones", dispute_id="dst_probe", payment_id="int_probe")  # the spike's probe, say
    report = run.pass_()  # each list answers with every dispute
    assert report.read == ("dst_ear_tips", "dst_earbuds", "dst_headphones") and set(run.board().records) == set(report.read)
    for other in ("old_ear_tips", "probe"):
        assert not [path for path in fake.seen() if other in path]
    assert report.problems == tuple(OTHER_PAYMENTS.format(order=order) for order in ("1121", "1098", "1109"))
    assert OTHER_PAYMENTS.format(order="1121") == ("Airwallex listed disputes of other payments under order #1121's "
                                                   "payment. This app doesn't read them from this list.")
    fake.unfiltered = False  # the filter works again: nothing outside the run's payments is read or sent
    report = run.pass_(2 * MINUTE)
    assert not [path for path in fake.seen() if "old_ear_tips" in path or "probe" in path] and not report.problems
    assert [request.url.path for request in fake.posts("/accept")] == [f"{DISPUTES}/dst_ear_tips/accept"]
    assert fake.sandbox.payments["int_old_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 0


def test_a_new_dispute_whose_read_names_another_payment_still_counts_on_its_list(tmp_path):
    run = at_step(tmp_path, "monday")
    run.fake.sandbox.open("earbuds", dispute_id="dst_old", payment_id="int_old")  # another payment, outside the run
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    named = {**run.fake.sandbox.disputes["dst_second"], "payment_intent_id": "int_old"}
    run.fake.fail("GET", f"{DISPUTES}/dst_second", status=200, body=named)  # listed on the ear tips' payment, but...
    report = run.pass_(2 * MINUTE)
    assert READ_FAILED.format(who="Dispute dst_second", why=OTHER_PAYMENT) in report.problems
    assert READ_FAILED.format(who="Dispute dst_second", why=OTHER_PAYMENT) == (
        "Dispute dst_second: this app couldn't read the dispute on this pass (the dispute names a payment that isn't in "
        "the run's list of payments). It reads it again on the next pass.")
    assert not [path for path in run.fake.seen() if "int_old" in path]  # that payment is never read
    ear_tips = run.record("dst_ear_tips")  # it counts on the payment whose list showed it
    assert phase(ear_tips) is Phase.PERSON and ear_tips.rounds[-1].person == ONE_PAYMENT and not run.fake.posts("/accept")
    assert "dst_second" not in run.board().records


@pytest.mark.parametrize("answer", ["a payment id this app won't use", "an answer for another dispute"])
def test_a_new_dispute_whose_read_is_refused_counts_on_its_list(tmp_path, answer):
    """As above, with a read this app refuses before it reads a payment: the GET names a payment id this app won't use,
    or answers for another dispute. The dispute counts on the payment whose list showed it, the ear tips', and on no
    other."""
    run = at_step(tmp_path, "monday")  # code's own accept of the ear tips, with its $12 refund, is ready to go
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    if answer == "a payment id this app won't use":
        body, why = {**run.fake.sandbox.disputes["dst_second"], "payment_intent_id": "int-ear-tips"}, BAD_PAYMENT
    else:
        body, why = copy.deepcopy(run.fake.sandbox.disputes["dst_earbuds"]), OTHER_DISPUTE
    run.fake.fail("GET", f"{DISPUTES}/dst_second", status=200, body=body)
    report = run.pass_(2 * MINUTE)
    assert report.problems == (READ_FAILED.format(who="Dispute dst_second", why=why),)
    ear_tips = run.record("dst_ear_tips")
    assert phase(ear_tips) is Phase.PERSON and ear_tips.rounds[-1].person == ONE_PAYMENT and not run.fake.posts("/accept")
    assert run.fake.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 0
    for other in ("dst_earbuds", "dst_headphones"):  # no other payment's case goes to a person for it
        assert phase(run.record(other)) is Phase.APPROVE
    assert "dst_second" not in run.board().records


SENDS = {"dst_ear_tips": f"{DISPUTES}/dst_ear_tips/accept", "dst_earbuds": f"{DISPUTES}/dst_earbuds/accept",
         "dst_headphones": f"{DISPUTES}/dst_headphones/challenge"}  # what each case of the "approved" step sends


def unlabelled(folder: Path, case_id: str, payment_id: str) -> Run:
    """The "approved" step with a new dispute on payment_id, and lists that ignore their payment filter and leave
    payment_intent_id out of their items, as two API changes at once could make them: the run's first payment, the ear
    tips', lists every dispute, and no item says its payment."""
    run = at_step(folder, "approved")  # the ear tips, the earbuds and the headphones, each ready to send
    run.fake.sandbox.open(case_id, now=MONDAY, dispute_id="dst_second", payment_id=payment_id)
    disputes = run.fake.sandbox.disputes

    def every_dispute(request: httpx.Request) -> httpx.Response:
        items = [{key: value for key, value in dispute.items() if key != "payment_intent_id"}
                 for dispute in copy.deepcopy(list(disputes.values()))]
        return httpx.Response(200, json={"items": items})

    run.fake._list = every_dispute
    return run


@pytest.mark.parametrize("case_id", ["earbuds", "headphones"])
def test_a_new_dispute_counts_on_the_payment_its_own_read_names(tmp_path, case_id):
    """A new dispute listed under the ear tips' payment, but on another of the run's payments, is read and saved on the
    payment it names: that payment's case goes to a person, nothing is sent on it, and the others go out."""
    run = unlabelled(tmp_path, case_id, f"int_{case_id}")
    report = run.pass_(2 * MINUTE)
    held = f"dst_{case_id}"
    for dispute_id in (held, "dst_second"):
        record = run.record(dispute_id)
        assert phase(record) is Phase.PERSON and record.rounds[-1].person == ONE_PAYMENT
    assert run.record("dst_second").last.payment_id == f"int_{case_id}" and "dst_second" in report.read
    assert not [request for request in run.fake.posts() if request.url.path == SENDS[held]]
    assert dict(report.sent) == {dispute_id: Outcome.TAKEN for dispute_id in SENDS if dispute_id != held}
    assert report.problems == ()


def test_a_new_dispute_on_a_payment_outside_the_run_still_counts_on_its_list(tmp_path):
    """As above, with the new dispute on a payment outside the run: that payment is never read, nothing outside the run
    becomes a case of the run, and the dispute counts on the list it was on, the ear tips'."""
    run = unlabelled(tmp_path, "earbuds", "int_old")
    report = run.pass_(2 * MINUTE)
    assert report.problems == (READ_FAILED.format(who="Dispute dst_second", why=OTHER_PAYMENT),)
    assert not [path for path in run.fake.seen() if "int_old" in path] and "dst_second" not in run.board().records
    ear_tips = run.record("dst_ear_tips")
    assert phase(ear_tips) is Phase.PERSON and ear_tips.rounds[-1].person == ONE_PAYMENT
    assert not [request for request in run.fake.posts() if request.url.path == SENDS["dst_ear_tips"]]
    assert dict(report.sent) == {"dst_earbuds": Outcome.TAKEN, "dst_headphones": Outcome.TAKEN}


def test_a_new_dispute_whose_payment_cant_be_read_counts_on_the_payment_its_read_names(tmp_path):
    """As above, with the new dispute on the earbuds' payment, which doesn't answer on this pass. The dispute still counts
    there, not on the list it was on: the earbuds' case goes to a person, and the ear tips' accept goes out."""
    run = unlabelled(tmp_path, "earbuds", "int_earbuds")
    run.fake.fail("GET", f"{PAYMENTS}/int_earbuds", status=503, times=2)  # read once a pass, and its one retry
    report = run.pass_(2 * MINUTE)
    assert report.problems == (READ_FAILED.format(who="Order #1098", why="HTTP 503"),
                               READ_FAILED.format(who="Dispute dst_second", why="HTTP 503"))
    earbuds = run.record("dst_earbuds")
    assert phase(earbuds) is Phase.PERSON and earbuds.rounds[-1].person == ONE_PAYMENT
    assert dict(report.sent) == {"dst_ear_tips": Outcome.TAKEN, "dst_headphones": Outcome.TAKEN}
    assert phase(run.record("dst_ear_tips")) is not Phase.PERSON and "dst_second" not in run.board().records


@pytest.mark.parametrize("case_id", ["ear_tips", "earbuds"])  # code's own accept, or a plan that waits for Priya
def test_a_broken_case_counts_on_every_payment_whose_list_shows_it(tmp_path, case_id):
    """As above, with the earbuds' case broken and the new dispute on the earbuds' payment. A broken case is never read,
    so it counts on each of the run's payments whose list shows it, its own among them: the new dispute goes to a person,
    and nothing goes out on the earbuds' payment, on this pass or the next. Here every list shows it, so every case goes
    to a person."""
    run = unlabelled(tmp_path, case_id, "int_earbuds")
    version = max(row[0] for row in run.db.versions("dst_earbuds")) + 1
    query(run.db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                  "('dst_earbuds', ?, '{', 'poll', ?)", version, MONDAY.isoformat())
    broken = BROKEN.format(id="dst_earbuds", why="The saved record isn't valid JSON.")
    for _ in range(2):
        report = run.pass_(2 * MINUTE)
        assert report.problems == (broken,) and report.sent == () and "dst_second" in report.read
        second = run.record("dst_second")
        assert second.last.payment_id == "int_earbuds" and second.rounds[-1].person == ONE_PAYMENT
        for dispute_id in ("dst_ear_tips", "dst_headphones", "dst_second"):
            assert phase(run.record(dispute_id)) is Phase.PERSON
        run.wall.move(MINUTE)
        assert run.approve_all() == {}  # nothing waits for Priya's approval
    assert not run.fake.posts()
    assert run.fake.sandbox.payments["int_earbuds"]["latest_payment_attempt"]["refunded_amount"] == 0


@pytest.mark.parametrize("named", ["int_earbuds", "int_headphones"])
def test_a_new_dispute_whose_list_and_read_name_two_of_the_runs_payments_counts_on_both(tmp_path, named):
    """The ear tips' list names the ear tips' payment for a new dispute, and the dispute's own read names another of the
    run's payments. It counts on both, with one line: both payments' cases go to a person, and nothing goes out on either.
    The run's third case goes out."""
    run = at_step(tmp_path, "approved")  # the ear tips, the earbuds and the headphones, each ready to send
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    body = {**run.fake.sandbox.disputes["dst_second"], "payment_intent_id": named}
    run.fake.fail("GET", f"{DISPUTES}/dst_second", status=200, body=body)
    report = run.pass_(2 * MINUTE)
    line = TWO_PAYMENTS.format(order="1121")
    assert line == ("Airwallex names two of the run's payments for a dispute listed under order #1121's payment. Both "
                    "payments' cases go to a person.")
    assert report.problems == (line,)
    held = f"dst_{named.removeprefix('int_')}"
    for dispute_id in ("dst_ear_tips", held, "dst_second"):
        record = run.record(dispute_id)
        assert phase(record) is Phase.PERSON and record.rounds[-1].person == ONE_PAYMENT
    assert run.record("dst_second").last.payment_id == named and "dst_second" in report.read
    assert not [request for request in run.fake.posts() if request.url.path in (SENDS["dst_ear_tips"], SENDS[held])]
    assert run.fake.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 0
    third = next(dispute_id for dispute_id in SENDS if dispute_id not in ("dst_ear_tips", held))
    assert dict(report.sent) == {third: Outcome.TAKEN}


def test_a_new_dispute_named_for_two_payments_counts_on_both_when_its_payment_doesnt_answer(tmp_path):
    """As above, with the earbuds' payment not answering on this pass: the new dispute isn't read, and it still counts
    on both payments."""
    run = at_step(tmp_path, "approved")
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    body = {**run.fake.sandbox.disputes["dst_second"], "payment_intent_id": "int_earbuds"}
    run.fake.fail("GET", f"{DISPUTES}/dst_second", status=200, body=body)
    run.fake.fail("GET", f"{PAYMENTS}/int_earbuds", status=503, times=2)  # read once a pass, and its one retry
    report = run.pass_(2 * MINUTE)
    assert report.problems == (READ_FAILED.format(who="Order #1098", why="HTTP 503"),
                               READ_FAILED.format(who="Dispute dst_second", why="HTTP 503"),
                               TWO_PAYMENTS.format(order="1121"))
    for dispute_id in ("dst_ear_tips", "dst_earbuds"):
        record = run.record(dispute_id)
        assert phase(record) is Phase.PERSON and record.rounds[-1].person == ONE_PAYMENT
    assert "dst_second" not in run.board().records and dict(report.sent) == {"dst_headphones": Outcome.TAKEN}
    assert run.fake.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 0


def test_a_new_dispute_named_for_two_payments_counts_on_both_when_its_own_payment_has_no_case(tmp_path):
    """As above, with the payment the dispute's read names in the run's scope but with no case of its own yet: the bank's
    Monday step saves a payment before its dispute opens. The new dispute counts there too, so it goes to a person, and
    nothing goes out on either payment, on this pass or the next."""
    run = Run(tmp_path, cases=("ear_tips",))
    run.fake.sandbox.open("earbuds")  # the earbuds' payment on the fake...
    del run.fake.sandbox.disputes["dst_earbuds"]  # ...with no dispute on it yet
    run.db.put_store_payment(StorePayment("int_earbuds", "1098", "FH-1098-test", RUN_DATE))  # in the run's scope
    run.pass_()
    assert phase(run.record("dst_ear_tips")) is Phase.SEND  # code's own accept, with its $12 refund, ready to go
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    body = {**run.fake.sandbox.disputes["dst_second"], "payment_intent_id": "int_earbuds"}
    run.fake.fail("GET", f"{DISPUTES}/dst_second", status=200, body=body, times=2)  # one read a pass
    for _ in range(2):
        report = run.pass_(2 * MINUTE)
        assert report.problems == (TWO_PAYMENTS.format(order="1121"),) and report.sent == ()
        for dispute_id in ("dst_ear_tips", "dst_second"):
            record = run.record(dispute_id)
            assert phase(record) is Phase.PERSON and record.rounds[-1].person == ONE_PAYMENT
        assert run.record("dst_second").last.payment_id == "int_earbuds" and "dst_second" in report.read
        run.wall.move(MINUTE)
        assert run.approve_all() == {}  # nothing waits for Priya's approval
    assert not run.fake.posts()
    refunded = {payment_id: payment["latest_payment_attempt"]["refunded_amount"]
                for payment_id, payment in run.fake.sandbox.payments.items()}
    assert refunded == {"int_ear_tips": 0, "int_earbuds": 0}


@pytest.mark.parametrize("read", ["answers", "times out"])
def test_a_saved_case_whose_list_and_read_name_two_of_the_runs_payments_counts_on_both(tmp_path, read):
    """The ear tips' list names the ear tips' payment for the earbuds' saved case, whose own read names the earbuds'
    payment, or whose read times out while its record names that payment. It counts on both, with one line on every
    pass: both payments' cases go to a person, and nothing goes out on either. The headphones' challenge goes out."""
    run = at_step(tmp_path, "approved")  # the ear tips, the earbuds and the headphones, each ready to send
    disputes, own = run.fake.sandbox.disputes, run.fake._list

    def ear_tips_list_names_the_earbuds_too(request: httpx.Request) -> httpx.Response:
        answer = own(request)
        if request.url.params.get("payment_intent_id") != "int_ear_tips":
            return answer
        named = {**copy.deepcopy(disputes["dst_earbuds"]), "payment_intent_id": "int_ear_tips"}
        return httpx.Response(200, json={"items": [*answer.json()["items"], named]})

    run.fake._list = ear_tips_list_names_the_earbuds_too
    lines = (TWO_PAYMENTS.format(order="1121"),)
    if read == "times out":
        run.fake.fail("GET", f"{DISPUTES}/dst_earbuds", timeout=True, times=4)  # each pass's read, and its one retry
        lines = (READ_FAILED.format(who="Order #1098", why="no answer in 30 seconds"), *lines)
    sent = []
    for _ in range(2):
        report = run.pass_(2 * MINUTE)
        assert report.problems == lines and ("dst_earbuds" in report.read) is (read == "answers")
        for dispute_id in ("dst_ear_tips", "dst_earbuds"):
            record = run.record(dispute_id)
            assert phase(record) is Phase.PERSON and record.rounds[-1].person == ONE_PAYMENT
        assert run.record("dst_earbuds").last.payment_id == "int_earbuds"
        sent += report.sent
    assert sent == [("dst_headphones", Outcome.TAKEN)]
    assert not [request for request in run.fake.posts() if request.url.path in (SENDS["dst_ear_tips"], SENDS["dst_earbuds"])]
    for payment_id in ("int_ear_tips", "int_earbuds"):
        assert run.fake.sandbox.payments[payment_id]["latest_payment_attempt"]["refunded_amount"] == 0


def test_a_new_dispute_whose_read_failed_still_stops_the_send(tmp_path):
    run = at_step(tmp_path, "monday")
    assert phase(run.record("dst_ear_tips")) is Phase.SEND  # code's own accept, with its $12 refund, ready to go
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")  # its payment
    run.fake.fail("GET", f"{DISPUTES}/dst_second", timeout=True, times=2)  # the read times out, and its one retry
    report = run.pass_(2 * MINUTE)
    assert not run.fake.posts("/accept")  # the ear tips' refund didn't go out
    ear_tips = run.record("dst_ear_tips")
    assert phase(ear_tips) is Phase.PERSON and ear_tips.rounds[-1].person == ONE_PAYMENT
    # Only the pass's own line: poll()'s line for the stand-in ("The amount field is missing") would be false.
    assert report.problems == (READ_FAILED.format(who="Dispute dst_second", why="no answer in 30 seconds"),)
    assert READ_FAILED.format(who="Dispute dst_second", why="no answer in 30 seconds") == (
        "Dispute dst_second: this app couldn't read the dispute on this pass (no answer in 30 seconds). It reads it "
        "again on the next pass.")
    assert "dst_second" not in run.board().records and report.read == ("dst_ear_tips", "dst_earbuds", "dst_headphones")
    report = run.pass_(MINUTE)  # read now: both are a person's cases, and still nothing goes out
    assert {phase(run.record(dispute_id)) for dispute_id in ("dst_ear_tips", "dst_second")} == {Phase.PERSON}
    assert not run.fake.posts("/accept") and run.record("dst_second").rounds[-1].person == ONE_PAYMENT


def test_every_failed_read_has_its_line_and_a_saved_case_stays_as_it_was(tmp_path):
    run = at_step(tmp_path, "monday")
    before = run.record("dst_headphones")
    run.fake.fail("GET", DISPUTES, status=503, times=2)  # the first list, the ear tips' payment's, and its retry
    run.fake.fail("GET", f"{DISPUTES}/dst_headphones", timeout=True, times=2)
    run.fake.fail("GET", f"{PAYMENTS}/int_earbuds", status=404, times=1)
    run.fake.fail("GET", f"{DISPUTES}/dst_ear_tips", status=200, body=run.fake.sandbox.disputes["dst_earbuds"])
    report = run.pass_(MINUTE)
    assert report.problems == (
        LIST_FAILED.format(order="1121", why="HTTP 503"),
        READ_FAILED.format(who="Order #1121", why=OTHER_DISPUTE),
        READ_FAILED.format(who="Order #1098", why="HTTP 404, resource_not_found: The fake sandbox answered 404"),
        READ_FAILED.format(who="Order #1109", why="no answer in 30 seconds"),
        NOT_LISTED.format(who="Order #1121"))
    assert LIST_FAILED.format(order="1121", why="HTTP 503") == (
        "This app couldn't read the list of disputes on order #1121's payment (HTTP 503). The disputes this app knows "
        "are still read. Nothing is sent for this payment on this pass.")
    assert not run.fake.posts()  # the ear tips' accept, ready on the last reading, waits for a list that answers
    assert report.read == () and to_json(run.record("dst_headphones")) == to_json(before)  # kept, and its reading ages
    assert run.db.versions("dst_headphones")[-1][1] == "decide"  # nothing new saved for it


def test_a_list_that_fails_holds_the_sends_on_its_payment(tmp_path):
    run = at_step(tmp_path, "monday")
    assert phase(run.record("dst_ear_tips")) is Phase.SEND  # code's own accept, with its $12 refund, ready to go
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")  # its payment
    run.fake.fail("GET", DISPUTES, status=503, times=2)  # the ear tips' payment's list, and its one retry
    report = run.pass_(2 * MINUTE)
    held = NOT_LISTED.format(who="Order #1121")
    assert held == ("Order #1121: the list of disputes on its payment couldn't be read, so nothing is sent for it on this "
                    "pass.")
    assert LIST_FAILED.format(order="1121", why="HTTP 503") in report.problems and held in report.problems
    assert not run.fake.posts("/accept") and phase(run.record("dst_ear_tips")) is Phase.SEND  # held, not a person's
    assert "send:dst_ear_tips" not in run.clock.labels()  # held before it took a moment of its own
    assert run.fake.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 0
    report = run.pass_(MINUTE)  # the list answers: the second dispute is seen, and both are a person's cases
    assert {phase(run.record(dispute_id)) for dispute_id in ("dst_ear_tips", "dst_second")} == {Phase.PERSON}
    assert run.record("dst_ear_tips").rounds[-1].person == ONE_PAYMENT and not run.fake.posts("/accept")


def test_a_case_whose_payment_isnt_in_the_runs_scope_is_never_sent(tmp_path):
    run = at_step(tmp_path, "monday")
    query(run.db, "UPDATE store_payments SET created_at = 'not a time' WHERE payment_id = 'int_ear_tips'")  # a hand edit
    held = NOT_IN_SCOPE.format(who="Order #1121")
    assert held == ("Order #1121: this app can't find its payment in the run's list of payments, so nothing is sent for "
                    "it. A person checks it.")
    for _ in range(2):
        report = run.pass_(2 * MINUTE)
        assert SCOPE_ROW in report.problems and held in report.problems
    assert not run.fake.posts() and phase(run.record("dst_ear_tips")) is Phase.SEND


def test_two_disputes_on_one_payment_read_it_once(tmp_path):
    run = Run(tmp_path, cases=("ear_tips",))
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")
    run.db.put_issuer_date(IssuerDate("dst_second", "ear_tips", FERNHILL.case("ear_tips").opened_at))
    report = run.pass_()
    assert run.fake.gets(f"{PAYMENTS}/int_ear_tips") == 1 and report.read == ("dst_ear_tips", "dst_second")
    assert {record.rounds[-1].person for record in run.board().records.values()} == {ONE_PAYMENT}


@pytest.mark.parametrize("item", [{"id": "dst_x/../accept"}, {"stage": "RFI", "status": "REQUIRES_RESPONSE"}, "dst_x"],
                         ids=["an id it won't put in a web address", "no id", "not an object"])
def test_a_listed_dispute_with_no_id_this_app_can_use_still_counts(tmp_path, item):
    run = at_step(tmp_path, "monday")
    listed = {"items": [run.fake.sandbox.disputes["dst_ear_tips"], item]}
    run.fake.fail("GET", DISPUTES, status=200, body=listed)  # the first list: the ear tips' payment
    report = run.pass_(MINUTE)
    assert report.problems == (NAMELESS.format(order="1121"),)  # poll()'s "A dispute with no id" line would be false
    assert NAMELESS.format(order="1121") == ("Airwallex listed a dispute on order #1121's payment with no id this app can "
                                             "use. It isn't read, but it counts as a dispute on that payment.")
    assert not [path for path in run.fake.seen() if ".." in path or "dst_x" in path]
    ear_tips = run.record("dst_ear_tips")
    assert phase(ear_tips) is Phase.PERSON and ear_tips.rounds[-1].person == ONE_PAYMENT and not run.fake.posts()


# ----------------------------------------------------------------------------- the order of a pass
def test_sends_before_reads(tmp_path):
    run = Run(tmp_path, reader=Reads(busy))
    first = run.pass_()  # Claude is busy: the earbuds and the headphones stay at DECIDE, the ear tips are ready
    assert first.decided == ("dst_ear_tips",)
    slow = Slow(run.wall, timedelta(minutes=11))
    run.service.reader = slow
    report = run.pass_(MINUTE)
    assert report.sent == (("dst_ear_tips", Outcome.TAKEN),) and report.decided == ("dst_earbuds", "dst_headphones")
    labels = run.clock.labels()
    labels = labels[len(labels) - labels[::-1].index("pass") - 1:]  # the second pass's
    assert labels == ["pass", "send:dst_ear_tips", "post:dst_ear_tips", "answer:dst_ear_tips", "decide:dst_earbuds",
                      "decide:dst_headphones", "pass:end"]
    moments = dict(run.clock.journal)  # the second pass's, the later of each label
    assert moments["post:dst_ear_tips"] == moments["pass"] and len(slow.requests) == 2  # sent on this pass's reading
    # The two reads took 22 minutes: had they gone first, that reading would have been too old to send on.
    assert run.wall.at - moments["pass"] == timedelta(minutes=22) > lifecycle.FRESH


class Slow:
    """The scripted perfect reader, taking minutes of the run's wall clock over each read."""

    def __init__(self, wall: Wall, length: timedelta) -> None:
        self.wall, self.length, self.inner, self.requests = wall, length, Scripted(perfect_for), []

    def read(self, request):
        self.requests.append(request)
        self.wall.move(self.length)
        return self.inner.read(request)


def test_soonest_due_first(tmp_path):
    run = Run(tmp_path, cases=())
    for case_id, dispute_id in (("headphones", "dst_a"), ("earbuds", "dst_b"), ("ear_tips", "dst_c")):
        run.open(case_id, dispute_id=dispute_id, payment_id=f"int_{case_id}")  # ids sort against the due dates
    run.pass_()
    assert [label for label in run.clock.labels() if label.startswith("decide:")] == ["decide:dst_c", "decide:dst_b",
                                                                                      "decide:dst_a"]
    run.wall.move(5 * MINUTE)
    run.approve_all()
    report = run.pass_(MINUTE)
    assert [dispute_id for dispute_id, _ in report.sent] == ["dst_c", "dst_b", "dst_a"]
    assert [label for label in run.clock.labels() if label.startswith("send:")] == ["send:dst_c", "send:dst_b",
                                                                                    "send:dst_a"]


def test_busy_line(tmp_path):
    run = Run(tmp_path, reader=Reads(busy))
    report = run.pass_()
    assert [line for line in report.problems if BUSY_NOTE in line] == [
        "Order #1098: Claude is busy. Code reads this case again on the next pass.",
        "Order #1109: Claude is busy. Code reads this case again on the next pass."]
    assert {phase(run.record(f"dst_{case}")) for case in ("earbuds", "headphones")} == {Phase.DECIDE}
    assert run.board().problems == report.problems  # the screens show the last pass's lines


def test_a_bug_holds_the_round(tmp_path, monkeypatch, capsys):
    run = Run(tmp_path, cases=("headphones",))

    def broken_keep(*args, **options):
        raise lifecycle.Refused("A bug in keep().")  # decide_round() lets keep()'s errors out

    monkeypatch.setattr(lifecycle, "keep", broken_keep)
    report = run.pass_()
    record = run.record("dst_headphones")
    assert phase(record) is Phase.PERSON and record.rounds[-1].person == HELD_FOR_ERROR
    assert report.problems == (DECIDE_ERROR.format(who="Order #1109"),) and report.decided == ("dst_headphones",)
    assert DECIDE_ERROR.format(who="Order #1109") == ("Order #1109: code hit an error while it decided this round. A "
                                                      "person handles this round.")
    assert run.db.versions("dst_headphones")[-1][1] == "hold"
    assert "A bug in keep()." in capsys.readouterr().err  # the traceback goes to stderr, never to the database
    assert "A bug in keep()." not in json.dumps([list(row.problems) for row in run.db.passes()])


class BuggyReader:
    def read(self, request):
        raise KeyError("A bug in the reader.")


@pytest.mark.parametrize("bug", ["reader", "rules"])
def test_a_round_decide_round_holds_itself_has_the_same_line(tmp_path, monkeypatch, capsys, bug):
    """decide_round() holds a round itself for a bug in the reader, the checks or the rules, and returns: the pass still
    writes its line and saves the hold as one, and a reader's traceback goes to stderr."""
    if bug == "rules":
        def broken_evaluate(*args, **options):
            raise TypeError("A bug in the rules.")

        monkeypatch.setattr(rules, "evaluate", broken_evaluate)
    run = Run(tmp_path, cases=("headphones",), reader=BuggyReader() if bug == "reader" else None)
    report = run.pass_()
    record = run.record("dst_headphones")
    assert phase(record) is Phase.PERSON and record.rounds[-1].person == HELD_FOR_ERROR
    assert report.problems == (DECIDE_ERROR.format(who="Order #1109"),) and report.decided == ("dst_headphones",)
    assert run.db.versions("dst_headphones")[-1][1] == "hold"
    err = capsys.readouterr().err
    assert ("A bug in the reader." in err) is (bug == "reader")  # rounds.py swallows a rules bug, out of the loop's reach
    assert "A bug in" not in json.dumps([list(row.problems) for row in run.db.passes()])


def test_a_replay_mismatch_from_the_reader_is_never_held(tmp_path):
    class Strict:
        def read(self, request):
            raise ReplayMismatch("Order #1109: the recording holds no reading for this case as the code reads it now.")

    run = Run(tmp_path, cases=("headphones",), reader=Strict())
    with pytest.raises(ReplayMismatch, match="the recording holds no reading"):
        run.pass_()  # decide_round() holds a round whose reader raises: the loop raises the mismatch again
    assert phase(run.record("dst_headphones")) is Phase.DECIDE and run.db.passes()[0].finished_at is None
    run.service.reader = Scripted(perfect_for)  # the lock was let go
    assert run.pass_(MINUTE).decided == ("dst_headphones",)


def test_a_replay_mismatch_from_a_post_comes_out(tmp_path):
    run = at_step(tmp_path, "monday")

    def mismatch(request):
        if request.url.path.endswith("/accept"):
            raise ReplayMismatch("The replay has no recorded answer for this POST.")

    run.fake.hooks.append(mismatch)
    with pytest.raises(ReplayMismatch):
        run.pass_(MINUTE)  # the send step's catch-all lets it out


# ----------------------------------------------------------------------------- what a pass can't read
def test_a_broken_record_goes_to_a_person(tmp_path):
    run = at_step(tmp_path, "approved")
    query(run.db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                  "('dst_ear_tips', 3, '{', 'poll', ?)", MONDAY.isoformat())
    line = BROKEN.format(id="dst_ear_tips", why="The saved record isn't valid JSON.")
    assert line == ("Dispute dst_ear_tips: part of what this app saved for this case can't be read. The saved record "
                    "isn't valid JSON. Nothing is sent for it. A person handles it in Airwallex.")
    first, second = run.pass_(2 * MINUTE), run.pass_(MINUTE)
    for report in (first, second):  # out of poll and of every send, with a line on every pass
        assert line in report.problems and "dst_ear_tips" not in report.read
    assert [dispute_id for dispute_id, _ in first.sent] == ["dst_earbuds", "dst_headphones"]  # the others go on
    assert run.fake.gets(f"{DISPUTES}/dst_ear_tips") == 0
    assert [request.url.path for request in run.fake.posts("/accept")] == [f"{DISPUTES}/dst_earbuds/accept"]
    assert run.db.load().broken == {"dst_ear_tips": "The saved record isn't valid JSON."}  # an older row isn't used


@pytest.mark.parametrize("row", ["approval", "try"])
def test_a_case_the_screens_call_broken_is_never_sent(tmp_path, row):
    """A case with a row that won't load, but not its newest record, nor a row the send step reads: the screens call it
    broken and say nothing is sent for it, so the pass leaves it out of everything too."""
    if row == "approval":  # an approval row of another round of the ear tips, ready with code's own accept
        run = at_step(tmp_path, "monday")
        query(run.db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
                      "('dst_ear_tips', 2, 'x', '{', ?)", MONDAY.isoformat())
        dispute_id, path, why = "dst_ear_tips", f"{DISPUTES}/dst_ear_tips/accept", APPROVAL_UNREADABLE
    else:  # a try row of round one's challenge, while round two's challenge is approved
        run = at_step(tmp_path, "pushback")
        run.wall.move(MINUTE)
        assert run.approve_all() == {"dst_headphones": Result(True, "approved")}
        first = run.record("dst_headphones").rounds[0].plan.request_id
        query(run.db, "INSERT INTO tries (request_id, at, outcome, status, detail) VALUES (?, 'not a time', 'taken', "
                      "200, '')", first)
        dispute_id, path, why = "dst_headphones", f"{DISPUTES}/dst_headphones/challenge", TRY_UNREADABLE
    assert dict(run.board().broken) == run.db.load().broken == {dispute_id: why}  # the pass and the screens agree
    for _ in range(2):
        report = run.pass_(2 * MINUTE)
        assert BROKEN.format(id=dispute_id, why=why) in report.problems and dispute_id not in report.read
    assert not [request for request in run.fake.posts() if request.url.path == path]
    assert run.fake.gets(f"{DISPUTES}/{dispute_id}") == 0
    assert BROKEN.format(id="dst_ear_tips", why=APPROVAL_UNREADABLE) == (
        "Dispute dst_ear_tips: part of what this app saved for this case can't be read. The approval on file can't be "
        "read. Nothing is sent for it. A person handles it in Airwallex.")


@pytest.mark.parametrize("row", ["record", "approval"])
def test_a_case_that_cant_be_read_still_counts_on_its_payment(tmp_path, row):
    run = Run(tmp_path, cases=("ear_tips",))
    run.pass_()
    if row == "record":
        query(run.db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                      "('dst_ear_tips', 3, '{', 'poll', ?)", MONDAY.isoformat())
        why = "The saved record isn't valid JSON."
    else:
        query(run.db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
                      "('dst_ear_tips', 1, 'x', '{', ?)", MONDAY.isoformat())
        why = APPROVAL_UNREADABLE
    run.fake.sandbox.open("ear_tips", now=MONDAY, dispute_id="dst_second", payment_id="int_ear_tips")  # its payment
    report = run.pass_(MINUTE)
    second = run.record("dst_second")
    assert phase(second) is Phase.PERSON and second.rounds[-1].person == ONE_PAYMENT and not run.fake.posts()
    assert not [line for line in report.problems if line.startswith("Dispute dst_ear_tips can't")]  # its own line only
    assert BROKEN.format(id="dst_ear_tips", why=why) in report.problems


def test_odd_scope_rows_and_answers_are_lines_and_the_pass_goes_on(tmp_path):
    run = at_step(tmp_path, "monday")
    run.db.put_store_payment(StorePayment("int_x/../y", "9", "FH-9-test", RUN_DATE))  # a saved id unfit for a web address
    query(run.db, "INSERT INTO store_payments VALUES ('int_odd', '1', 'FH-1-test', 'not a time')")  # a row that won't load
    named = {**run.fake.sandbox.disputes["dst_earbuds"], "payment_intent_id": "int_x/../y"}
    run.fake.fail("GET", f"{DISPUTES}/dst_earbuds", status=200, body=named)  # a dispute naming such a payment
    report = run.pass_(MINUTE)
    assert run.db.passes(limit=1)[0].finished_at is not None  # the pass ended, with a line for each
    for line in (BAD_SCOPE.format(order="9"), SCOPE_ROW, READ_FAILED.format(who="Order #1098", why=BAD_PAYMENT)):
        assert line in report.problems
    assert BAD_SCOPE.format(order="9") == ("Order #9: the run's saved payment id won't go into a web address, so its "
                                           "disputes aren't listed.")
    assert not [path for path in run.fake.seen() if ".." in path or "int_x" in path or "int_odd" in path]
    assert dict(report.sent) == {"dst_ear_tips": Outcome.TAKEN}  # the run's other cases go on


def query(db: Database, sql: str, *values: Any) -> list:
    """SQL straight into the file, as a crash, a bug or a hand edit could leave it."""
    connection = db.connect()
    try:
        return connection.execute(sql, values).fetchall()
    finally:
        connection.close()


# ----------------------------------------------------------------------------- screens and passes at once
def test_an_approval_during_a_pass_waits_for_the_next_read(tmp_path):
    run = at_step(tmp_path, "monday")

    def approve_now() -> None:  # right after the pass took its moment, before it loaded anything or read Airwallex
        run.wall.move(timedelta(seconds=30))
        board = run.board()
        assert run.service.approve("dst_headphones", *posted(board, "dst_headphones")) == Result(True, "approved")

    run.wall.move(5 * MINUTE)
    run.clock.after["pass"] = approve_now
    report = run.pass_()
    headphones = run.record("dst_headphones")
    assert phase(headphones) is Phase.SEND and headphones.last.at < headphones.rounds[-1].plan.approved_at
    assert lifecycle.blocker(headphones, run.wall.at) == ("The next pass reads the dispute again before it sends: the "
                                                          "last reading is older than the approval.")
    assert "dst_headphones" not in dict(report.sent) and not run.fake.posts("/challenge")
    assert not [line for line in report.problems if "changed while this pass ran" in line]  # saved over the approval
    assert dict(run.pass_(MINUTE).sent)["dst_headphones"] is Outcome.TAKEN  # a reading after the approval: it goes


def test_a_screen_write_during_a_pass(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)

    def approve_during_the_reads(request: httpx.Request) -> None:
        if request.url.path == f"{DISPUTES}/dst_headphones" and not approved:
            approved.append(run.service.approve("dst_headphones", *posted(run.board(), "dst_headphones")))

    approved: list[Result] = []
    run.fake.hooks.append(approve_during_the_reads)
    report = run.pass_()
    assert approved == [Result(True, "approved")]
    assert CONFLICT.format(who="Order #1109") in report.problems  # the pass's version is never saved over hers
    headphones, version = run.db.load_one("dst_headphones")
    assert headphones.rounds[-1].plan.approved_by == "Priya" and run.db.versions("dst_headphones")[-1][1] == "approve"
    assert CONFLICT.format(who="Order #1109") == ("Order #1109: the case changed while this pass ran. It's read again on "
                                                  "the next pass.")
    assert not run.fake.posts("/challenge")
    assert dict(run.pass_(MINUTE).sent)["dst_headphones"] is Outcome.TAKEN


def test_one_pass_at_a_time(tmp_path):
    run = Run(tmp_path, cases=("ear_tips",))
    inside: list[PassReport] = []
    run.clock.after["pass"] = lambda: inside.append(run_pass(run.service))  # a second pass while this one runs
    report = run.pass_()
    assert inside[0].skipped == SKIPPED and inside[0].pass_id is None and report.pass_id is not None
    assert inside[0].skipped == "Another pass is running, so this one didn't start."
    assert [row.id for row in run.db.passes()] == [report.pass_id]  # the skipped pass left nothing
    # Across processes: a pass in another process holds the lock, and this one doesn't start.
    child = child_holding(tmp_path / "pass.lock")
    try:
        assert json.loads(line_from(child))["held"] is True
        skipped = run.pass_(MINUTE)
        assert skipped.skipped == SKIPPED and len(run.db.passes()) == 1 and run.clock.labels()[-1] == "pass:end"
        child.stdin.write("\n")
        child.stdin.flush()
        assert line_from(child) == "released"
        assert run.pass_(MINUTE).skipped == ""
    finally:
        child.stdin.close()
        child.wait(timeout=60)
        child.stdout.close()
        child.stderr.close()


def test_a_service_with_no_client_or_reader_runs_no_pass(tmp_path):
    run = Run(tmp_path, cases=("ear_tips",))
    for missing in ({"client": None}, {"reader": None}):
        service = Service(db=run.db, store=FERNHILL, clock=run.clock, info=INFO, approver="Priya",
                          **{"client": run.service.client, "reader": run.reader, **missing})
        with pytest.raises(ValueError, match="runs no pass"):
            run_pass(service)
    assert not run.fake.calls and not run.db.passes()


# ----------------------------------------------------------------------------- the core and the rest
@pytest.mark.parametrize("module", ["loop", "service"])
def test_the_loop_and_the_service_never_import_the_seed_the_bank_the_eval_or_the_screens(module):
    tree = ast.parse(Path(importlib.util.find_spec(f"disputable.{module}").origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
              for alias in node.names}
    assert "disputable.lifecycle" in names or "disputable.lifecycle.CaseRecord" in names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web",
                   "disputable.recording", "disputable.demo", "disputable.cli"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], (module, banned)
