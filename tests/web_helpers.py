"""Helpers for the screens' tests: a FakeDesk that serves the Boards a test builds, the story and the trap cases as
Boards, the states lifecycle's tests build, a dropped plan's round that keeps its saved rows (read_then_replied()),
saved rows as a page's snapshot reads them (frozen()), demo bars with no Next button (NO_NEXT), a voice-aware HTML
parser, a form's hidden fields as a browser posts them, and a client on 127.0.0.1.

The records are real ones: the fake sandbox and the story's passes (tests/sandbox.py), decide_round with the scripted
readers (test_rounds.Rounds), and every approval through approvals.approve() on the form's own round trip. The real
approval pipeline is the Service's (test_service.py), and over HTTP test_app_live.py's.
"""

from __future__ import annotations

import base64
import functools
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping

from starlette.testclient import TestClient

from cases import READ
from disputable import approvals, ce3, lifecycle, privacy, prompts, rounds
from disputable.approvals import ApprovalRecord
from disputable.db import FrozenSaved
from disputable.desk import Beat, Board, DemoBar, Mode, ReaderInfo, Result, SendLine
from disputable.engine import Action, Evidence, Stage, Status
from disputable.fees import FEES_V1, ChallengeReason, EvidenceClass
from disputable.jsonable import load
from disputable.lifecycle import CaseRecord, Phase, Plan, poll, sending
from disputable.reader import Failure
from disputable.reader import to_json as reading_json
from disputable.rounds import MemorySaved, decide_round
from disputable.seed.fernhill import ENDING_2, FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.seed.scripted import Scripted, fooled_for, perfect_for
from disputable.seed.suite import SUITE
from disputable.store import Store
from disputable.web import create_app, views
from sandbox import MINUTE, Sandbox, Story
from test_rounds import Rounds

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}
DAY = timedelta(days=1)
CLAUDE = ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "claude", prompts.MODEL, None, None)
RECORDED = ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "claude", prompts.MODEL, MONDAY.date(), MONDAY.date())
FIRST_ASK = "Cardholder does not recognise this card-absent transaction."  # round one's comment, as test_walk plays it
RESULTS = {"approve": Result(True, "approved"), "drop_moved": Result(True, "dropped"),
           "demo_next": Result(True, "next-played"), "demo_restart": Result(True, "restarted")}


# ----------------------------------------------------------------------------- the desk
class FakeDesk:
    """A Desk that serves the Board a test built, or what a function of no arguments returns, and keeps every write call
    with its arguments. Each write answers the Result the test set (RESULTS by default)."""

    def __init__(self, board: Board | Callable[[], Board], **answers: Result) -> None:
        self.current, self.answers, self.calls = board, {**RESULTS, **answers}, []

    def board(self) -> Board:
        return self.current() if callable(self.current) else self.current

    def approve(self, dispute_id: str, plan_text: str, reading_key: str) -> Result:
        self.calls.append(("approve", dispute_id, plan_text, reading_key))
        return self.answers["approve"]

    def drop_moved(self, dispute_id: str, version: int) -> Result:
        self.calls.append(("drop_moved", dispute_id, version))
        return self.answers["drop_moved"]

    def demo_next(self, played: int) -> Result:
        self.calls.append(("demo_next", played))
        return self.answers["demo_next"]

    def demo_restart(self, ending: str) -> Result:
        self.calls.append(("demo_restart", ending))
        return self.answers["demo_restart"]


def board_of(records: Mapping[str, CaseRecord] | list[CaseRecord], *, store: Store = FERNHILL,
             saved: rounds.Saved | None = None, now: datetime = MONDAY, mode: Mode = Mode.LIVE,
             rows: Mapping[str, tuple[ApprovalRecord, ...]] | None = None, reader: ReaderInfo = CLAUDE,
             approver: str | None = "Priya", demo: DemoBar | None = None, problems: tuple[str, ...] = (),
             last_pass: datetime | None = None, broken: Mapping[str, str] | None = None,
             sends: Mapping[str, tuple[SendLine, ...]] | None = None) -> Board:
    """A Board as a desk would serve it: every record at version 1, live by default, with Priya approving."""
    if not isinstance(records, Mapping):
        records = {record.dispute_id: record for record in records}
    records = dict(records)
    return Board(mode, now, store, records, {dispute_id: 1 for dispute_id in records}, dict(broken or {}),
                 dict(rows or {}), saved if saved is not None else MemorySaved(), dict(sends or {}), tuple(problems),
                 last_pass, reader, approver, demo)


def store_for(record: CaseRecord) -> Store:
    """The seed store a record's order is in: the story's, else the suite's."""
    number = record.last.order_number
    return FERNHILL if number and any(order.number == number for order in FERNHILL.orders) else SUITE


def snapshot(saved: MemorySaved) -> MemorySaved:
    """A copy of what is saved now, so a later put doesn't reach an earlier Board."""
    copy = MemorySaved()
    copy.readings, copy.bases = saved.readings, saved.bases
    return copy


def demo_bar(**changes: Any) -> DemoBar:
    """A hand-built bar: Ending 1 after Monday's first pass, with both approvals pending, from a recording made on the
    Airwallex sandbox (airwallex="stand-in": one made on the tests' stand-in for it). Its count is 0, so the bar says
    "Step n: ..." as when the count isn't known; count=13 gives "Step n of 13: ..."."""
    played = (Beat("Monday 9:00", "The bank opens three disputes.", "bank"),
              Beat("Monday 9:00", "The agent reads the queue.", "pass"))
    bar = DemoBar("1", ("1", "2"), MONDAY.date(), played,
                  Beat("Monday 9:05", "Priya approves code's draft on order #1098.", "approve"),
                  (("dst_earbuds", "Order #1098"), ("dst_headphones", "Order #1109")), "Priya", None, False, 0,
                  "sandbox", None, 0)
    return replace(bar, **changes)


STOPPED = "Order #1109: the recording holds no reading for this case as the code reads it now."
NO_NEXT = {"the story ended": {"pending": (), "coming": None, "finished": True, "total": Decimal("279.30")},
           "the replay stopped matching": {"pending": (), "mismatch": STOPPED}}  # demo_bar()'s changes: a bar with no Next


def client(desk: Any, **options: Any) -> TestClient:
    """A client of the app over any Desk, as a browser on this computer reaches it: http://127.0.0.1:8765, with
    redirects left for the test to follow. A POST from the app's own page also sends headers=ORIGIN."""
    return TestClient(create_app(desk, port=PORT), base_url=BASE, follow_redirects=False, **options)


# ----------------------------------------------------------------------------- the story, as the screens see it
class Run(Rounds):
    """Story's passes with decide_round at DECIDE, the scripted perfect reader by default, and every approval through
    approvals.approve() on the approval form's round trip, with the reading key current() gives, its row kept."""

    def __init__(self, store: Store = FERNHILL, cases: tuple[str, ...] = ("ear_tips", "earbuds", "headphones"),
                 reader: Any = None) -> None:
        super().__init__(store, cases, Scripted(perfect_for) if reader is None else reader)
        self.rows: dict[str, list[ApprovalRecord]] = {}

    def approve(self, now: datetime, by: str = "Priya") -> None:
        for dispute_id, record in list(self.records.items()):
            if lifecycle.phase(record) is Phase.APPROVE:
                step = rounds.current(record, self.store, self.saved, now)
                seen = load(Plan, json.loads(views.plan_text(record.rounds[-1].plan)))
                key = step.reading.key if step.reading is not None else ""
                self.records[dispute_id], row = approvals.approve(record, seen, by, now, reading_key=key)
                self.rows.setdefault(dispute_id, []).append(approvals.from_json(approvals.to_json(row)))

    def board(self, now: datetime, **options: Any) -> Board:
        rows = {dispute_id: tuple(found) for dispute_id, found in self.rows.items()}
        return board_of(self.records, store=self.store, saved=snapshot(self.saved), now=now, rows=rows, **options)


STEPS = {"1": ("monday", "monday-approved", "monday-sent", "pushback", "pushback-approved", "deciding", "won"),
         "2": ("monday", "monday-approved", "monday-sent", "pushback", "pushback-approved", "accepted")}


@functools.cache
def story(ending: str = "1") -> dict[str, Board]:
    """Each step of an ending as a Board, in order: Ending 1 on FERNHILL ends won, Ending 2 on ENDING_2 accepts."""
    run, boards = Run(FERNHILL if ending == "1" else ENDING_2), {}
    run.poll(MONDAY)
    run.decide(MONDAY)
    boards["monday"] = run.board(MONDAY)
    run.approve(MONDAY)
    boards["monday-approved"] = run.board(MONDAY)
    run.poll(MONDAY + MINUTE)
    run.send(MONDAY + MINUTE)
    run.poll(MONDAY + 2 * MINUTE)
    boards["monday-sent"] = run.board(MONDAY + 2 * MINUTE)
    run.sandbox.escalate("dst_headphones")
    run.poll(THREE_WEEKS_LATER)
    run.decide(THREE_WEEKS_LATER)
    boards["pushback"] = run.board(THREE_WEEKS_LATER)
    run.approve(THREE_WEEKS_LATER)
    boards["pushback-approved"] = run.board(THREE_WEEKS_LATER)
    run.poll(THREE_WEEKS_LATER + MINUTE)
    run.send(THREE_WEEKS_LATER + MINUTE)
    run.poll(THREE_WEEKS_LATER + 2 * MINUTE)
    if ending == "1":
        boards["deciding"] = run.board(THREE_WEEKS_LATER + 2 * MINUTE)
        run.sandbox.resolve("dst_headphones", Status.WON)
        run.poll(THREE_WEEKS_LATER + DAY)
        boards["won"] = run.board(THREE_WEEKS_LATER + DAY)
    else:
        boards["accepted"] = run.board(THREE_WEEKS_LATER + 2 * MINUTE)
    assert tuple(boards) == STEPS[ending]
    return boards


# ----------------------------------------------------------------------------- the read cases and the traps
def whole_or_cents(amount: Any) -> int | float:
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def opened(sandbox: Sandbox, store: Store, case_id: str, dispute_id: str, now: datetime) -> None:
    """The case opened in the fake sandbox with the seed's own amounts, as test_walk opens it."""
    case = store.case(case_id)
    sandbox.open(case_id, now=now, dispute_id=dispute_id)
    order = store.order(case.order_number)
    dispute = sandbox.disputes[dispute_id]
    payment = sandbox.payments[dispute["payment_intent_id"]]
    dispute["amount"] = whole_or_cents(order.amount if case.amount is None else case.amount)
    payment["amount"] = payment["captured_amount"] = whole_or_cents(order.amount)
    payment["latest_payment_attempt"]["captured_amount"] = whole_or_cents(order.amount)


def polled(sandbox: Sandbox, records: dict[str, CaseRecord], store: Store, now: datetime) -> dict[str, CaseRecord]:
    records, problems = poll(records, sandbox.reads(), now, store=store, opened=sandbox.opened)
    assert problems == ()
    return records


def record_at_decide(store: Store, case_id: str, now: datetime, dispute_id: str) -> CaseRecord:
    """The case as a lifecycle record at DECIDE on its clock, as test_walk.record_at_decide makes it, so as_case() reads
    as the seed case. A later round is played as the bank saw it: round one sent this app's challenge of the facts and
    files the bank turned down, or, with no record of what was sent, someone else challenged."""
    case, sandbox = store.case(case_id), Sandbox(store)
    later = ce3.later_round(case)
    mine = later and bool(case.rejected_fact_ids and case.rejected_file_hashes)
    start = now - DAY if mine else now
    opened(sandbox, store, case_id, dispute_id, start)
    records: dict[str, CaseRecord] = {}
    if mine:
        sandbox.change(dispute_id, stage=Stage.RFI.value, issuer_comment=FIRST_ASK)
        records = polled(sandbox, records, store, start)
        fees = replace(FEES_V1, fight_reason_codes=FEES_V1.fight_reason_codes | {case.reason_code})
        package = Evidence(EvidenceClass.CE3_QUALIFIED, case.rejected_fact_ids, case.rejected_file_hashes)
        record = lifecycle.keep(records[dispute_id], package, start, round_number=1, fees=fees)
        records[dispute_id] = lifecycle.approve(record, record.rounds[-1].plan, "Priya", start, fees)
        records = polled(sandbox, records, store, start + MINUTE)
        record = lifecycle.sending(records[dispute_id], start + MINUTE)
        sandbox.post(record)
        records[dispute_id] = lifecycle.taken(record, record.rounds[-1].plan.request_id, start + MINUTE)
        records = polled(sandbox, records, store, start + 2 * MINUTE)
        sandbox.escalate(dispute_id, case_id=case_id, now=now)
    elif later:
        sandbox.reply(dispute_id, Action.CHALLENGE, "someone@example.com", ChallengeReason.PURCHASE_HISTORY.value,
                      stage=Stage.RFI.value)
    return polled(sandbox, records, store, now)[dispute_id]


TRAPS = tuple(label for label, read in READ.items() if read.answer.must_ignore)  # the 14 cases with a planted line
READERS = {"perfect": perfect_for, "obey": fooled_for("obey")}


@functools.cache
def read_case(label: str, reader: str | None) -> Board:
    """A read case as a Board on its own clock: at DECIDE (reader None), or decided by decide_round with the perfect
    reader or the one that obeys the planted lines."""
    read = READ[label]
    record = record_at_decide(read.store, read.case_id, read.now, f"dst_{label}")
    saved = MemorySaved()
    if reader is not None:
        record = decide_round(record, read.store, Scripted(READERS[reader]), saved, read.now).record
    return board_of([record], store=read.store, saved=saved, now=read.now)


# ----------------------------------------------------------------------------- the states lifecycle's tests build
def being_sent(case_id: str = "headphones", captured: int | None = None) -> Story:
    """The plan's first POST try is saved, and Airwallex hasn't answered yet, as test_lifecycle plays it."""
    story = Story(cases=(case_id,))
    if captured is not None:
        story.sandbox.payments[f"int_{case_id}"]["captured_amount"] = captured
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.approve(MONDAY)
    story.poll(MONDAY + MINUTE)
    story.records[f"dst_{case_id}"] = sending(story[case_id], MONDAY + MINUTE)
    return story


def held() -> tuple[CaseRecord, datetime]:
    """Code's own $12 accept on a $30 order is being sent, and a refund of its size shows: every retry is held."""
    story = being_sent("ear_tips", captured=30)
    story.sandbox.pay("dst_ear_tips", refunded=12)
    story.poll(MONDAY + 2 * MINUTE)
    return story["ear_tips"], MONDAY + 2 * MINUTE


def late_refund() -> tuple[CaseRecord, datetime]:
    """The RFI closed as LOST before this app's accept showed, so a person books what moved. Then the accept shows."""
    story = being_sent("ear_tips")
    story.sandbox.pay("dst_ear_tips", refunded=12)
    story.sandbox.change("dst_ear_tips", status="LOST")
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, lifecycle.ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")
    story.poll(MONDAY + 3 * MINUTE)
    return story["ear_tips"], MONDAY + 3 * MINUTE


def other_refunds() -> tuple[CaseRecord, datetime]:
    """The case closed before this app's accept showed, and the payment shows more refunded than that accept can."""
    story = being_sent("ear_tips", captured=30)
    story.sandbox.post(story["ear_tips"], show=False)
    story.sandbox.pay("dst_ear_tips", refunded=17)
    story.poll(MONDAY + 2 * MINUTE)
    return story["ear_tips"], MONDAY + 2 * MINUTE


def slipped() -> tuple[CaseRecord, datetime]:
    """Airwallex dated the headphones' dispute far ahead, so its chargeback reads as an older copy: FUTURE shows."""
    story = Story()
    story.run(MONDAY)
    story.sandbox.change("dst_headphones")
    story.sandbox.disputes["dst_headphones"]["updated_at"] = "2062-11-02T17:00:30+00:00"
    story.poll(MONDAY + timedelta(hours=1))
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    return story["headphones"], THREE_WEEKS_LATER


def history_only() -> tuple[CaseRecord, datetime]:
    sandbox = Sandbox()
    sandbox.open("earbuds")
    sandbox.change("dst_earbuds", status="WON")
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    return records["dst_earbuds"], MONDAY


def blocked_history_only() -> tuple[CaseRecord, datetime]:
    """A dispute first seen closed that reopens behind a date slip: it stays history-only, and a person sees it."""
    sandbox = Sandbox()
    sandbox.open("headphones")
    sandbox.reply("dst_headphones", Action.ACCEPT, "priya@fernhill.example", "OTHERS")
    sandbox.change("dst_headphones", status="ACCEPTED")
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    sandbox.change("dst_headphones")
    sandbox.disputes["dst_headphones"]["updated_at"] = "2062-11-02T17:00:30+00:00"
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    sandbox.escalate("dst_headphones", now=MONDAY + DAY)
    records, _ = poll(records, sandbox.reads(), MONDAY + DAY, store=FERNHILL)
    return records["dst_headphones"], MONDAY + DAY


def replied_first(by: str) -> tuple[CaseRecord, datetime]:
    """The headphones' plan waits for Priya, and a challenge in another's name shows on the dispute first: the plan is
    dropped, and the bank decides on a response this app never sent."""
    story = Story(cases=("headphones",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, by, ChallengeReason.PURCHASE_HISTORY.value, stage="RFI")
    story.poll(MONDAY + MINUTE)
    return story["headphones"], MONDAY + MINUTE


def another_currency() -> tuple[CaseRecord, datetime]:
    """The headphones' dispute and its payment in EUR. fees-v1 has only USD fees, so a person handles it."""
    run = Run(cases=("headphones",))
    run.sandbox.disputes["dst_headphones"]["currency"] = "EUR"
    run.sandbox.payments[run.sandbox.disputes["dst_headphones"]["payment_intent_id"]]["currency"] = "EUR"
    run.poll(MONDAY)
    run.decide(MONDAY)
    return run["headphones"], MONDAY


def read_then_replied(by: str = "priya@fernhill.example") -> Board:
    """The headphones' round read and decided through decide_round, its basis and reading kept, then a challenge in
    another's name shows first: the plan is dropped, and the round's saved rows stay, as the database keeps them."""
    run = Run(cases=("headphones",))
    run.poll(MONDAY)
    run.decide(MONDAY)
    run.sandbox.reply("dst_headphones", Action.CHALLENGE, by, ChallengeReason.PURCHASE_HISTORY.value, stage="RFI")
    run.poll(MONDAY + MINUTE)
    return run.board(MONDAY + MINUTE)


STATES = {"held": held, "late refund": late_refund, "other refunds": other_refunds, "slipped": slipped,
          "history-only": history_only, "blocked history-only": blocked_history_only,
          "someone else replied": lambda: replied_first("priya@fernhill.example"),  # in Airwallex's web app, say
          "this app's name, never sent": lambda: replied_first(lifecycle.ACTOR),  # another copy of this app, say
          "another currency": another_currency}


def frozen(saved: MemorySaved, rows: Mapping[tuple[str, int], str] | None = None,
           reading_rows: Mapping[tuple[str, int, str], str] | None = None) -> FrozenSaved:
    """What saved holds, as a page's snapshot reads it from the database: a FrozenSaved over the same rows, each one
    loaded when it is first read. rows: (dispute id, round) -> the text of that round's newest basis row instead, such
    as one that won't load. reading_rows: (dispute id, round, key) -> the text of the newest reading row with that key
    instead."""
    readings = {(dispute_id, number, reading.key): reading_json(reading)
                for dispute_id, number, reading in saved.readings if reading.failure is None}
    readings.update(reading_rows or {})
    busy = Counter((dispute_id, number) for dispute_id, number, reading in saved.readings
                   if reading.failure is Failure.BUSY)
    bases = {(basis.dispute_id, basis.round_number): rounds.basis_to_json(basis) for basis in saved.bases}
    bases.update(rows or {})
    return FrozenSaved([(*where, text) for where, text in readings.items()], [(*where, n) for where, n in busy.items()],
                       [(*where, text) for where, text in bases.items()])


# ----------------------------------------------------------------------------- reading pages
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
QUOTED = ("bank", "claude", "file", "record")  # the containers of everyone's words but code's, the most specific first


@dataclass(frozen=True)
class Node:
    """One text node of a page, with the tag and the classes of every element around it, outermost first."""
    text: str
    around: tuple[tuple[str, frozenset[str]], ...]

    @property
    def voice(self) -> str:
        """Whose words the node shows: a container's voice for the said part of it, else code's (labels included)."""
        if not any("said" in classes for _, classes in self.around):
            return "code"
        for _, classes in reversed(self.around):
            for name in QUOTED:
                if name in classes:
                    return name
        return "code"

    def inside(self, tag: str) -> bool:
        return any(name == tag for name, _ in self.around)


class Voices(HTMLParser):
    """Every text node of a page, and every form with its hidden fields as a browser would post them."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, frozenset[str]]] = []
        self.nodes: list[Node] = []
        self.forms: list[tuple[str, list[tuple[str, str]]]] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag == "form":
            self.forms.append((given.get("action") or "", []))
        if tag == "input" and self.forms:
            self.forms[-1][1].append((given.get("name") or "", given.get("value") or ""))
        if tag not in VOID:
            self.stack.append((tag, frozenset((given.get("class") or "").split())))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.nodes.append(Node(data, tuple(self.stack)))

    def of(self, voice: str) -> list[str]:
        return [node.text for node in self.nodes if node.voice == voice]

    def texts(self) -> list[str]:
        return [node.text for node in self.nodes]


def form_fields(html: str, action: str, **match: str) -> dict[str, str]:
    """The hidden fields of the page's one form that posts to action (and whose fields hold match), unescaped, as a
    browser posts them."""
    found = [dict(fields) for where, fields in Voices(html).forms if where == action
             and all(dict(fields).get(name) == value for name, value in match.items())]
    assert len(found) == 1, f"{len(found)} forms post to {action}"
    return found[0]


def pages(board: Board) -> list[str]:
    """Every GET page a board has: the queue, the money page, and each case's page and approval screen."""
    paths = ["/", "/money"]
    for dispute_id in (*board.records, *board.broken):
        paths += [views.case_href(dispute_id), views.case_href(dispute_id, "approve")]
    return paths


# ----------------------------------------------------------------------------- the secrets check
ALLOWED = privacy.Allowed.of(FERNHILL, ENDING_2, SUITE)
URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
OWN = (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")


def request_ids(board: Board | None) -> set[str]:
    """The request ids a board's plans and approval rows carry: UUIDs made from the terms, never an account's."""
    if board is None:
        return set()
    ids = {here.plan.request_id for record in board.records.values() for here in record.rounds if here.plan}
    return ids | {row.request_id for rows in board.approvals.values() for row in rows}


def secrets_problems(html: str, board: Board | None, sentinels: Iterable[str] = ()) -> list[str]:
    """What a page holds that no page may, each kind once: a sentinel value, as itself or in base64; a base64 token of
    48 or more characters that decodes to text holding a file id's ,|,; a UUID that is neither the seed's nor one of the
    board's request ids, as an Airwallex account id would be; or a URL outside this app. Kinds only, never values."""
    problems = []
    for value in sentinels:
        if value in html or base64.b64encode(value.encode()).decode().rstrip("=") in html:
            problems.append("a sentinel value")
    for token in privacy.base64_tokens(html):
        text = privacy.decode_base64(token)
        if text is not None and privacy.SEPARATOR in text:
            problems.append("a base64 token that holds a file id")
    known = ALLOWED.uuids | {found.lower() for found in request_ids(board)}
    problems += ["a UUID the seed doesn't hold" for found in privacy.UUID.findall(html) if found.lower() not in known]
    problems += ["a URL outside this app" for found in URL.findall(html) if not found.startswith(OWN)]
    return list(dict.fromkeys(problems))
