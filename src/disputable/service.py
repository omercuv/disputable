"""The Desk of a live run: what the screens read, and the one approval path.

board() is one read-only snapshot of the database, so a page is a pure function of what it shows: the cases, every
approval row, the readings and bases rounds.current() reads, each POST try as a SendLine with the round whose plan it
sent (never a body, a file id or a payment id), and the last pass's lines. The time it shows is clock.peek(), which uses
up no recorded moment.

An approval runs check_approval() first, at clock.peek(), and that saves nothing. Its checks, in order: approvals are on;
the case is there and every row of it can be read; the plan the screen showed loads as a Plan; approvals.approve() takes
it (a double click on the plan this approver already approved is no refusal); the reading key the screen showed is
rounds.current()'s; the plan's files can still be built exactly as planned; verify() passes on what would be saved. A
saved basis or reading of the round that won't load refuses it as file-moved: the plan can't be shown as it was made.
Then approve_at() runs the same checks at clock.now("approve:{id}") and saves the approval row and the record in one
transaction, at the version the checks saw. When another writer saved first, the checks run again: if the save was this
same approval, from a double click whose two posts overlapped, the answer is already, else stale. The approver is fixed
when the Service starts, and no form carries a name. The demo plays its approvals through check_approval() and
approve_at() at the recorded moments. drop_moved() drops, in a live run only, a plan whose file can't be built again
exactly as planned, or whose saved basis or reading won't load, so it is decided and approved again. A bug in the rules
while the plan's file is built again counts as a file that can't be built (files_for()'s REBUILD_ERROR), so that plan
can be dropped. A bug in any other check, files_for()'s own lines among them, keeps the plan: it answers
drop-not-needed, and the terminal gets the details. Neither answers an error page.

Nothing here runs a pass, builds an Airwallex client or a Claude reader, or reads the clock with now() except for an
approval. loop.run_pass(service) runs a pass with the service's own reader, client and lock.

The CLI uses it like this:
    service = live(settings, approver="Priya", store=store)                        # serve: no reader and no client
    service = live(settings, approver=None, store=store, reader=reader, client=client); loop.run_pass(service)
    the store comes from open_live(settings).meta("store"), which the CLI maps to the seed's store
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from disputable import approvals, lifecycle, prompts, rounds, rules
from disputable.approvals import ApprovalRecord
from disputable.clock import Clock, WallClock
from disputable.db import Conflict, Database, PassLock, Try
from disputable.desk import Board, Mode, ReaderInfo, Result, SendLine
from disputable.fees import FEES_V1, FeeTable
from disputable.jsonable import Unloadable, load
from disputable.lifecycle import ACTOR, CaseRecord, Plan, Refused
from disputable.loop import NOT_REBUILT
from disputable.store import Store

if TYPE_CHECKING:
    from disputable.airwallex import AgentClient
    from disputable.config import Settings
    from disputable.reader import Reader

LIVE_DB = "live.sqlite3"  # in the data folder
PASS_LOCK = "pass.lock"  # beside the database: one pass at a time per data folder
MAX_APPROVER = 60
MAX_DETAIL = 200  # a SendLine's detail, as desk.SendLine says
SEEN = "the plan the screen showed"

BAD_APPROVER = "The approver is a person's name of 1 to 60 printable characters, never the agent's."
OTHER_READER = ("The reader's profile or effort isn't the one the screens show, so its reading keys wouldn't match the "
                "approval screen's.")
NOT_RECORDED_LOG = "Disputable couldn't record an approval of dispute {id}: {why}"
DROP_CHECK_LOG = "Disputable couldn't check the plan of dispute {id} for a drop, so it stays: {why}"
NOT_READ_BACK = "what this app saved for it can't be read"  # lifecycle.drop()'s why when a saved row of the round won't load


@dataclass(frozen=True)
class Checked:
    """What check_approval() found: the Result (ok when every check passed), and what approve_at() would save."""
    result: Result
    record: CaseRecord | None  # the case as the checks loaded it, or None when it isn't there or can't be read
    version: int | None  # its version then: the approval saves only over it
    plan_sha256: str  # approvals.plan_sha256() of the plan the screen showed, or "" when it couldn't be read
    approved: CaseRecord | None = None  # the record approvals.approve() returned, when every check passed
    row: ApprovalRecord | None = None  # the approval row it returned


class Service:
    """desk.Desk over one database. In a live run it holds the wall clock; in the demo, a replay clock and Mode.DEMO."""

    def __init__(self, *, db: Database, store: Store, clock: Clock, info: ReaderInfo, approver: str | None,
                 mode: Mode = Mode.LIVE, reader: Reader | None = None, client: AgentClient | None = None,
                 lock: PassLock | None = None, fees: FeeTable = FEES_V1) -> None:
        """approver: the one person who approves here, fixed for the Service's life, or None: approvals are off.
        reader and client: only for passes. lock: the data folder's pass lock, by default beside the database.
        ValueError for an approver that isn't a person's name, or a reader whose profile or effort isn't info's."""
        if reader is not None and (getattr(reader, "profile", prompts.FERNHILL),
                                   getattr(reader, "effort", prompts.EFFORT)) != (info.profile, info.effort):
            raise ValueError(OTHER_READER)  # decide_round() keys a reading with them, and current() with info's
        self.db, self.store, self.clock, self.info, self.mode = db, store, clock, info, mode
        self.approver = None if approver is None else _approver(approver)
        self.reader, self.client, self.fees = reader, client, fees
        self.lock = lock if lock is not None else PassLock(Path(db.path).parent / PASS_LOCK)

    # ------------------------------------------------------------------------- the Desk
    def board(self) -> Board:
        """One read transaction's view. Its time is clock.peek(), read after the snapshot, so nothing on the board is
        dated after the time the board shows."""
        snapshot = self.db.snapshot()
        sends = {dispute_id: tuple(send_line(attempt, snapshot.records.get(dispute_id)) for attempt in tries)
                 for dispute_id, tries in snapshot.tries.items()}
        last = snapshot.last_pass.finished_at if snapshot.last_pass is not None else None
        return Board(self.mode, self.clock.peek(), self.store, snapshot.records, snapshot.versions, snapshot.broken,
                     snapshot.approvals, snapshot.saved, MappingProxyType(sends), snapshot.problems, last, self.info,
                     self.approver, None)

    def approve(self, dispute_id: str, plan_text: str, reading_key: str) -> Result:
        """The approval screen's POST: every check at clock.peek(), then the approval at clock.now("approve:{id}"). In
        the demo only the recording's approvals are made, through the Demo."""
        if self.mode is not Mode.LIVE:
            return Result(False, "not-in-recording")
        checked = self.check_approval(dispute_id, plan_text, reading_key, self.clock.peek())
        if not checked.result.ok:
            return checked.result  # nothing saved, and no moment used up
        return self.approve_at(dispute_id, plan_text, reading_key, self.clock.now(f"approve:{dispute_id}"))

    def drop_moved(self, dispute_id: str, version: int) -> Result:
        """Drop the plan of a case at APPROVE or SEND whose file can't be built again exactly as planned (a bug in the
        rules while it is built counts), or whose saved basis or reading won't load, at the version the page showed, so
        code decides the round again and a person approves the new plan: exactly the plans whose case page offers the
        drop. Live runs only."""
        if self.mode is not Mode.LIVE:
            return Result(False, "live-only")
        snapshot = self.db.snapshot()
        if dispute_id in snapshot.broken:
            return Result(False, "broken", snapshot.broken[dispute_id])
        record = snapshot.records.get(dispute_id)
        if record is None:
            return Result(False, "no-case")
        if version != snapshot.versions[dispute_id]:
            return Result(False, "stale")
        try:
            lifecycle.drop(record, NOT_REBUILT)  # pure: it refuses unless the case is at APPROVE or SEND
        except Refused as refused:
            return Result(False, "refused", str(refused))
        why = self._why_drop(record, snapshot.saved)
        if why is None:
            return Result(False, "drop-not-needed")
        try:
            self.db.save(lifecycle.drop(record, why), version, "drop")
        except Conflict:
            return Result(False, "stale")
        return Result(True, "dropped")

    def _why_drop(self, record: CaseRecord, saved: rounds.Saved) -> str | None:
        """Why the unsent plan can be dropped, as lifecycle.drop() words it, or None to keep it. A bug in the rules while
        files_for() builds the plan's file again counts as a file that can't be built (REBUILD_ERROR), so the case page
        offers the drop for it. A bug in any other check, files_for()'s own lines among them, keeps the plan, and the
        terminal gets the details: the case page offers no drop for it."""
        try:
            rounds.files_for(record, self.store, saved)  # it turns a bug in the rules into FilesMoved(REBUILD_ERROR)
        except (rounds.FilesMoved, Unloadable):  # a saved basis that can't be read can't build the file either
            return NOT_REBUILT
        except Exception as error:  # a bug in files_for()'s own lines, outside the rules: keep the plan
            print(DROP_CHECK_LOG.format(id=record.dispute_id, why=error), file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return None
        try:  # what the approval screen shows and check_approval() checks: an accept's basis, or a reading
            rounds.current(record, self.store, saved, self.clock.peek(), self.fees, profile=self.info.profile,
                           effort=self.info.effort)
        except Unloadable:
            return NOT_READ_BACK
        except rules.NoRules:  # the rules can't read today's records: no saved row is at fault, and the page says so
            return None
        except Exception as error:  # a bug in the other checks, such as the rules of a plan with no file: keep it
            print(DROP_CHECK_LOG.format(id=record.dispute_id, why=error), file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return None
        return None

    def demo_next(self, played: int) -> Result:
        return Result(False, "demo-only")

    def demo_restart(self, ending: str) -> Result:
        return Result(False, "demo-only")

    # ------------------------------------------------------------------------- the approval path
    def check_approval(self, dispute_id: str, plan_text: str, reading_key: str, now: datetime) -> Checked:
        """Every check of an approval at now, on one snapshot, in order. It saves nothing: the snapshot can't save."""
        if self.approver is None:
            return Checked(Result(False, "approvals-off"), None, None, "")
        snapshot = self.db.snapshot()
        if dispute_id in snapshot.broken:
            return Checked(Result(False, "broken", snapshot.broken[dispute_id]), None, None, "")
        record = snapshot.records.get(dispute_id)
        if record is None:
            return Checked(Result(False, "no-case"), None, None, "")
        version = snapshot.versions[dispute_id]
        try:
            seen = load(Plan, json.loads(plan_text), SEEN)  # as the screen showed it, never rebuilt from shown values
        except (ValueError, TypeError, LookupError, AttributeError, RecursionError):  # Unloadable is a ValueError
            return Checked(Result(False, "plan-unreadable"), record, version, "")
        digest = approvals.plan_sha256(seen)
        try:
            approved, row = approvals.approve(record, seen, self.approver, now, reading_key=reading_key, fees=self.fees)
        except Refused as refused:
            plan = record.rounds[-1].plan
            if plan is not None and plan.approved_by == self.approver and approvals.plan_sha256(plan) == digest:
                return Checked(Result(False, "already"), record, version, digest)  # a double click: the approval stands
            return Checked(Result(False, "refused", str(refused)), record, version, digest)
        try:
            step = rounds.current(record, self.store, snapshot.saved, now, self.fees, profile=self.info.profile,
                                  effort=self.info.effort)
        except Unloadable as unread:  # a saved basis or reading of the round: the plan can't be shown as it was made
            return Checked(Result(False, "file-moved", str(unread)), record, version, digest)
        if reading_key != (step.reading.key if step.reading is not None else ""):
            return Checked(Result(False, "reading-changed"), record, version, digest)
        if record.rounds[-1].plan.terms.file_hashes:
            try:
                rounds.files_for(record, self.store, snapshot.saved)
            except (rounds.FilesMoved, Unloadable) as moved:
                return Checked(Result(False, "file-moved", str(moved)), record, version, digest)
        problem = approvals.verify(approved, row)
        if problem is not None:  # a bug: what would be saved wouldn't send
            print(NOT_RECORDED_LOG.format(id=dispute_id, why=problem), file=sys.stderr)
            return Checked(Result(False, "not-recorded"), record, version, digest)
        return Checked(Result(True, "approved"), record, version, digest, approved, row)

    def approve_at(self, dispute_id: str, plan_text: str, reading_key: str, now: datetime) -> Result:
        """The same checks at now, then the approval row and the record in one transaction, at the version the checks
        saw. The demo calls it at the recorded approve:{id} moment, and approve() at clock.now(). When another writer
        saved first, the checks run again on what it saved: a double click whose posts overlapped finds its own
        approval there and answers already; anything else is stale."""
        checked = self.check_approval(dispute_id, plan_text, reading_key, now)
        if not checked.result.ok:
            return checked.result
        try:
            self.db.approve(checked.approved, checked.version, checked.row)
        except Conflict:
            again = self.check_approval(dispute_id, plan_text, reading_key, now).result  # it saves nothing
            return again if again.key == "already" else Result(False, "stale")
        except ValueError as error:  # a bug: the row or the record wouldn't load back exactly as it is
            print(NOT_RECORDED_LOG.format(id=dispute_id, why=error), file=sys.stderr)
            return Result(False, "not-recorded")
        return Result(True, "approved")


def send_line(attempt: Try, record: CaseRecord | None) -> SendLine:
    """What the screens may know of one POST try: when, what it meant, the status, Airwallex's phrase, and the round
    whose plan it sent. Every try of a plan sends the plan's request id, so the round is the first one whose plan has
    the try's id: None when no round's plan has it, or when record is None, as for a case with no record that loads."""
    detail = attempt.detail if len(attempt.detail) <= MAX_DETAIL else attempt.detail[:MAX_DETAIL]
    kept = record.rounds if record is not None else ()
    number = next((n for n, here in enumerate(kept, 1)
                   if here.plan is not None and here.plan.request_id == attempt.request_id), None)
    return SendLine(attempt.at, attempt.outcome.value, attempt.status, detail, number)


# ----------------------------------------------------------------------------- a live run
def open_live(settings: Settings, clock: Clock | None = None) -> Database:
    """The live database of the data folder, <data>/live.sqlite3, made when it's new. saved_at comes from clock.peek()."""
    return Database(Path(settings.data_dir) / LIVE_DB, stamp=(clock if clock is not None else WallClock()).peek)


def live(settings: Settings, *, approver: str | None, store: Store, reader: Reader | None = None,
         client: AgentClient | None = None, lock: PassLock | None = None) -> Service:
    """The Service of a live run on the wall clock: <data>/live.sqlite3 and <data>/pass.lock. The screens show Claude as
    the reader when a Claude key is set, else no reader. serve passes no reader and no client, so it never runs a pass."""
    clock = WallClock()
    keyed = bool(settings.anthropic_api_key)
    info = ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "claude" if keyed else "none", prompts.MODEL if keyed else "",
                      None, None)
    return Service(db=open_live(settings, clock), store=store, clock=clock, info=info, approver=approver, reader=reader,
                   client=client, lock=lock if lock is not None else PassLock(Path(settings.data_dir) / PASS_LOCK))


def _approver(name: Any) -> str:
    if not isinstance(name, str):
        raise ValueError(BAD_APPROVER)
    name = name.strip()
    if not (1 <= len(name) <= MAX_APPROVER and name.isprintable()) or name.casefold() == ACTOR:
        raise ValueError(BAD_APPROVER)
    return name
