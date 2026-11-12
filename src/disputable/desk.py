"""The seam between the runtime and the screens: the Desk the web layer is given, and the plain values it answers with.

The web layer receives a Desk and nothing else from the runtime: service.Service implements it over the live database,
demo.Demo over a recorded run. Each board() is one read-only snapshot, so a page is a pure function of a Board. What
the screens may know of a send try is a SendLine: never a body, a file id or a payment id. Each write answers a Result,
whose key the web layer maps to its own words. Types only: no clock, no database and no network here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable

from disputable import rounds
from disputable.approvals import ApprovalRecord
from disputable.lifecycle import CaseRecord
from disputable.prompts import StoreProfile
from disputable.store import Store


class Mode(Enum):
    LIVE = "LIVE"  # the database a loop fills from the Airwallex sandbox
    DEMO = "DEMO"  # a recorded run, replayed with no keys and no network


@dataclass(frozen=True)
class ReaderInfo:
    profile: StoreProfile  # passed to rounds.current(), and its support address to inbox.findings()
    effort: str  # passed to rounds.current()
    source: str  # "claude", "none" (no key) or "scripted" (tests only; never in a shipped demo)
    model: str  # "" for none
    first_read: date | None  # demo: the earliest reading's date in the story's readings file
    last_read: date | None


@dataclass(frozen=True)
class SendLine:  # what the screens may know of one POST try: never a body, a file id or a payment id
    at: datetime
    outcome: str  # "taken", "failed", "retry"
    status: int | None
    detail: str  # one plain phrase, at most 200 characters
    round_number: int | None = None  # the round whose plan has the try's request id; None when no round's plan has it


@dataclass(frozen=True)
class Beat:  # a played or coming step of the demo, as the bar shows it
    story: str  # "Monday 9:00", "Three weeks later", "A week later"
    text: str  # the timeline sentence (B 9.3)
    kind: str  # "bank", "pass" or "approve"


@dataclass(frozen=True)
class DemoBar:
    ending: str  # "1" or "2"
    endings: tuple[str, ...]  # the recordings present, in order
    recorded_on: date  # the manifest's day
    played: tuple[Beat, ...]  # in order
    coming: Beat | None  # the next beat, None at the end
    pending: tuple[tuple[str, str], ...]  # (dispute id, "Order #N") of approvals the judge may make now
    approver: str  # the recording's approver, "Priya"
    mismatch: str | None  # set when the replay stopped matching; Next is then off
    finished: bool
    count: int  # the number of beats in the recording: "Step n of count"
    airwallex: str  # the manifest's: "sandbox" (recorded on it) or "stand-in" (a run on the tests' stand-in for it)
    total: Decimal | None  # the manifest's recorded total, shown once finished
    left_unused: int  # recorded agent calls the replay never made (shown as a warning when finished)


@dataclass(frozen=True)
class Board:
    mode: Mode
    now: datetime  # the clock's peek(): the recording's moment in the demo; UTC
    store: Store
    records: Mapping[str, CaseRecord]
    versions: Mapping[str, int]
    broken: Mapping[str, str]  # dispute id -> why its saved record can't be read
    approvals: Mapping[str, tuple[ApprovalRecord, ...]]  # every row, oldest first, by dispute
    saved: rounds.Saved  # a read-only snapshot: put_reading and put_basis raise
    sends: Mapping[str, tuple[SendLine, ...]]  # by dispute, oldest first
    problems: tuple[str, ...]  # the last pass's lines
    last_pass: datetime | None
    reader: ReaderInfo
    approver: str | None  # who approves here; None: approvals are off
    demo: DemoBar | None


@dataclass(frozen=True)
class Result:
    ok: bool
    key: str  # one of RESULT_KEYS; the web layer maps it to its own words
    message: str = ""  # a module's own sentence (lifecycle's Refused, FilesMoved's, a replay line), as written

    def __post_init__(self) -> None:
        if self.key not in RESULT_KEYS:  # a typo would reach a page with no words for it
            raise ValueError(f"{self.key!r} isn't one of the Desk's result keys.")


RESULT_KEYS = frozenset({
    "approved", "already", "approvals-off", "no-case", "broken", "plan-unreadable", "refused", "reading-changed",
    "file-moved", "not-recorded", "stale", "not-in-recording", "dropped", "drop-not-needed", "demo-only", "live-only",
    "demo-moved", "demo-mismatch", "demo-finished", "no-ending", "next-played", "restarted"})


@runtime_checkable
class Desk(Protocol):
    def board(self) -> Board: ...
    def approve(self, dispute_id: str, plan_text: str, reading_key: str) -> Result: ...
    def drop_moved(self, dispute_id: str, version: int) -> Result: ...
    def demo_next(self, played: int) -> Result: ...  # played: how many beats the page showed
    def demo_restart(self, ending: str) -> Result: ...
