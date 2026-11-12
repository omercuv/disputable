"""Storage: one SQLite file per data folder, outside the repo and outside any folder a sync client watches.

A case's record is kept as append-only versions: the newest is the record, and its version number is the optimistic
lock, so a screen's write and a pass never overwrite each other. Approvals, readings, bases, uploads, POST bodies and
their tries are append-only too: SQLite triggers refuse UPDATE and DELETE, and with recursive triggers on, a REPLACE,
which deletes the old row first. Only a pass's row (once, at its end) and the meta rows change. Every write takes the
write lock first (BEGIN IMMEDIATE), so two writers wait for each other. WAL and synchronous FULL: a body saved before a
POST is on disk before the POST goes out. Each row loads on its own, so one that won't load stops only its own case: a
case with a row that won't load (its newest record, an approval or a try) is broken, for the pass and the screens
alike. Each operation opens its own connection, so the screens' threads, the loop and the CLI never share one. rounds and
reader are imported only where a reading or a basis is put or read, so a command that only shows the queue never loads
the Anthropic SDK.

The loop, the send step, the screens and the CLI use it like this:
    db = Database(<data>/live.sqlite3, stamp=clock.peek)          # stamp gives saved_at, and is never journaled
    loaded = db.load(); db.save(record, loaded.versions[id], why="poll")        # Conflict: someone saved first
    rounds.decide_round(record, store, reader, db.saved, now)                     # readings and bases, append-only
    db.approve(after, version, approval)                                          # the approval and the record, or neither
    db.start_send(lifecycle.sending(record, now), version, post), then one POST, db.put_try(Try(...)), and
        db.update(id, lambda r: lifecycle.taken(r, post.request_id, at), why="taken")
    a page: snapshot = db.snapshot(), one read transaction; snapshot.saved can't save
    one pass at a time: lock = PassLock(<data>/pass.lock); if lock.acquire(): try: ... finally: lock.release()
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence

from disputable import approvals, lifecycle
from disputable.airwallex import Outcome, PostRequest
from disputable.approvals import Alert, ApprovalRecord
from disputable.config import SetupError
from disputable.jsonable import Unloadable, dump, load
from disputable.lifecycle import CaseRecord, Phase

if TYPE_CHECKING:
    from disputable.reader import Reading
    from disputable.rounds import Basis

FORMAT = 1  # PRAGMA user_version. A later format adds MIGRATIONS = {2: _to_2, ...}, each in its own transaction
WHYS = frozenset({"poll", "decide", "hold", "drop", "send", "taken", "refused", "approve", "book"})  # why a version
META_KEYS = frozenset({"created_at", "mode", "run", "store", "ending"})
MODES = frozenset({"live", "record", "demo"})
STORES = frozenset({"fernhill", "ending_2"})  # the CLI maps each to its seed store: this module never imports the seed
APPEND_ONLY = ("case_versions", "approvals", "readings", "bases", "uploads", "posts", "tries")
PRAGMAS = ("foreign_keys = ON", "busy_timeout = 10000", "synchronous = FULL",
           "recursive_triggers = ON")  # so a REPLACE's delete of the old row fires the append-only triggers too
BUSY = "BUSY"  # reader.Failure.BUSY's value, as the readings table keeps it

NOT_OURS = "This file isn't a Disputable database."
NEWER = ("This database was made by a newer version of Disputable (format {format}). Use that version, or start a new "
         "database.")
CHANGED = "The case changed since it was loaded. Nothing was saved."
RECORD_UNREADABLE = "The saved record can't be loaded."
OTHER_DISPUTE = "The saved record is for another dispute."
APPROVAL_UNREADABLE = "The approval on file can't be read."
TRY_UNREADABLE = "A saved try of this case's response can't be read."
POST_UNREADABLE = "The saved request for this response can't be read."
UPLOAD_UNREADABLE = "The saved upload of this plan's file can't be read."
PASS_UNREADABLE = "The last pass's lines can't be read."
TIME_UNREADABLE = "A saved time can't be read."
SCOPE_ROW = "The run's saved list of payments has a row this app can't read."
READ_ONLY = "This is a page's read-only snapshot, so nothing is saved through it."
WONT_LOAD = "This {what} wouldn't load back exactly as it is, so it isn't saved."

SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE case_versions (
        dispute_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        record TEXT NOT NULL,
        why TEXT NOT NULL,
        saved_at TEXT NOT NULL,
        PRIMARY KEY (dispute_id, version))""",
    """CREATE TABLE approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id TEXT NOT NULL,
        round_number INTEGER NOT NULL CHECK (round_number >= 1),
        plan_sha256 TEXT NOT NULL,
        approval TEXT NOT NULL,
        saved_at TEXT NOT NULL)""",
    "CREATE INDEX approvals_by_round ON approvals (dispute_id, round_number, id)",
    """CREATE TABLE readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id TEXT NOT NULL,
        round_number INTEGER NOT NULL,
        key TEXT NOT NULL,
        failure TEXT,
        reading TEXT NOT NULL,
        saved_at TEXT NOT NULL)""",
    "CREATE INDEX readings_by_key ON readings (dispute_id, round_number, key, id)",
    """CREATE TABLE bases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id TEXT NOT NULL,
        round_number INTEGER NOT NULL,
        plan_sha256 TEXT NOT NULL,
        basis TEXT NOT NULL,
        saved_at TEXT NOT NULL)""",
    "CREATE INDEX bases_by_round ON bases (dispute_id, round_number, id)",
    """CREATE TABLE uploads (
        request_id TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        dispute_id TEXT NOT NULL,
        round_number INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        pdf_sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (request_id, sha256))""",
    """CREATE TABLE posts (
        request_id TEXT PRIMARY KEY,
        dispute_id TEXT NOT NULL,
        round_number INTEGER NOT NULL,
        path TEXT NOT NULL,
        body TEXT NOT NULL,
        saved_at TEXT NOT NULL)""",
    """CREATE TABLE tries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES posts (request_id),
        at TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('taken', 'failed', 'retry')),
        status INTEGER,
        detail TEXT NOT NULL,
        not_before TEXT)""",
    """CREATE TABLE passes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        problems TEXT NOT NULL DEFAULT '[]',
        alerts TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE store_payments (
        payment_id TEXT PRIMARY KEY,
        order_number TEXT NOT NULL,
        merchant_order_id TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE issuer_dates (
        dispute_id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL,
        opened_at TEXT NOT NULL)""",
    *(f"CREATE TRIGGER {table}_no_{change.lower()} BEFORE {change} ON {table} "
      f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
      for table in APPEND_ONLY for change in ("UPDATE", "DELETE")),
)
HEADS = ("SELECT dispute_id, version, record FROM case_versions AS c WHERE version = "
         "(SELECT MAX(version) FROM case_versions WHERE dispute_id = c.dispute_id) ORDER BY dispute_id")
LATEST_READINGS = (  # what reading() can return: the latest row with no failure for each key of a round
    "SELECT r.dispute_id, r.round_number, r.key, r.reading FROM readings AS r WHERE r.failure IS NULL AND r.id = "
    "(SELECT MAX(id) FROM readings WHERE dispute_id = r.dispute_id AND round_number = r.round_number AND key = r.key "
    "AND failure IS NULL)")
BUSY_COUNTS = (f"SELECT dispute_id, round_number, COUNT(*) FROM readings WHERE failure = '{BUSY}' "
               "GROUP BY dispute_id, round_number")
LATEST_BASES = ("SELECT b.dispute_id, b.round_number, b.basis FROM bases AS b WHERE b.id = "
                "(SELECT MAX(id) FROM bases WHERE dispute_id = b.dispute_id AND round_number = b.round_number)")
TRIES_BY_DISPUTE = ("SELECT p.dispute_id, t.request_id, t.at, t.outcome, t.status, t.detail, t.not_before "
                    "FROM tries AS t JOIN posts AS p ON p.request_id = t.request_id ORDER BY t.id")
APPROVALS_BY_DISPUTE = "SELECT dispute_id, approval FROM approvals ORDER BY id"
PASS_COLUMNS = "id, started_at, finished_at, problems, alerts, summary"


class Conflict(Exception):
    """The case changed since it was loaded. Nothing was saved."""


@dataclass(frozen=True)
class Loaded:
    """Every case as the pass reads it, by the same rule as a page's snapshot: never in both records and broken."""
    records: dict[str, CaseRecord]  # every case whose saved rows all load: its newest record, its approvals and tries
    versions: dict[str, int]  # its newest version
    broken: dict[str, str]  # dispute id -> why: its newest record's message, then a line for each kind of row


@dataclass(frozen=True)
class StorePayment:
    """One of the store's own payments of this run: the agent's scope."""
    payment_id: str
    order_number: str
    merchant_order_id: str
    created_at: datetime


@dataclass(frozen=True)
class IssuerDate:
    """When the simulated issuer opened a dispute in the story, which the sandbox's created_at (the run date) can't say."""
    dispute_id: str
    case_id: str
    opened_at: datetime


@dataclass(frozen=True)
class Upload:
    """A plan's file, uploaded once: a file uploaded for a plan is never uploaded again."""
    request_id: str  # the plan's request id
    sha256: str  # Document.sha256: the text hash in plan.terms.file_hashes
    dispute_id: str
    round_number: int
    file_id: str
    filename: str
    pdf_sha256: str  # of the bytes sent
    size: int
    uploaded_at: datetime


@dataclass(frozen=True)
class Try:
    """One POST try and what its answer meant."""
    request_id: str
    at: datetime
    outcome: Outcome
    status: int | None
    detail: str
    not_before: datetime | None  # after a 429 with Retry-After: no try before it


@dataclass(frozen=True)
class PassRow:
    id: int
    started_at: datetime
    finished_at: datetime | None
    problems: tuple[str, ...]
    alerts: tuple[Alert, ...]
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class Snapshot:
    """One read transaction's view of the database, for a page. Nothing in it can save, and it holds no POST body and no
    file id. Every dispute id is in records or in broken, never both: a case with a row that won't load (its newest
    record, an approval or a try) is broken, and the line names each kind, exactly as load() has it for the pass."""
    records: Mapping[str, CaseRecord]
    versions: Mapping[str, int]
    broken: Mapping[str, str]  # dispute id -> why its saved rows can't be read
    approvals: Mapping[str, tuple[ApprovalRecord, ...]]  # every row that loads, oldest first, by dispute
    saved: FrozenSaved  # rounds.Saved, read-only
    tries: Mapping[str, tuple[Try, ...]]  # every POST try, oldest first, by its request's dispute
    problems: tuple[str, ...]  # the lines of the latest pass that finished
    last_pass: PassRow | None  # the latest pass that finished, or None when there is none or it can't be read
    meta: Mapping[str, str]


class Database:
    """One SQLite file of this app's format: opened, or made when it's new."""

    def __init__(self, path: Path, *, stamp: Callable[[], datetime]) -> None:
        """Opens or creates the file and checks its format. stamp gives saved_at (clock.peek): never journaled.
        SetupError, with a plain message, for a file that isn't this app's or is of a newer format."""
        self.path = Path(path)
        self._stamp = stamp
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()
        self.saved = SqliteSaved(self)

    def connect(self) -> sqlite3.Connection:
        """A new connection with this app's settings: foreign keys on, a 10-second busy wait, synchronous FULL and
        recursive triggers. WAL is set when the file is made, and the file keeps it. Each operation opens its own
        connection and closes it."""
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            for pragma in PRAGMAS:
                connection.execute(f"PRAGMA {pragma}")
        except BaseException:
            connection.close()
            raise
        return connection

    # ------------------------------------------------------------------------- cases
    def load(self) -> Loaded:
        """Every case, by the snapshot's rule: one whose saved rows all load (its newest record, every approval and every
        try) is in records; one with a row that won't load is in broken, so the pass leaves it out of everything, as the
        screens say. An older version is never used instead."""
        with self._read() as connection:
            heads = connection.execute(HEADS).fetchall()
            unreadable = _parts(connection)[2]
        return _cases(heads, unreadable)

    def load_one(self, dispute_id: str) -> tuple[CaseRecord, int]:
        """The case's record and version. KeyError when no case has this id; Unloadable when its newest row won't load:
        an older version is never used instead, since a rollback could send twice."""
        with self._read() as connection:
            row = connection.execute("SELECT version, record FROM case_versions WHERE dispute_id = ? "
                                     "ORDER BY version DESC LIMIT 1", (dispute_id,)).fetchone()
        if row is None:
            raise KeyError(dispute_id)
        return _record(dispute_id, row[1]), row[0]

    def save(self, record: CaseRecord, expected: int | None, why: str) -> int:
        """Saves version expected + 1 (1 for a new case, expected None) and returns it. Raises Conflict, saving nothing,
        when the newest version isn't expected: another writer saved first."""
        _check_why(why)
        _check_expected(expected, new=True)
        text = _record_text(record)
        with self._write() as connection:
            return self._insert_version(connection, record.dispute_id, text, expected, why)

    def update(self, dispute_id: str, change: Callable[[CaseRecord], CaseRecord], why: str, tries: int = 3) -> int:
        """Loads the newest version, applies change and saves it. On a Conflict it loads again and applies change again,
        up to tries times: for taken() and send_failed() after a POST, whose answer must never be lost. A change that
        changes nothing saves nothing. Errors from change come out as they are."""
        _check_why(why)
        if type(tries) is not int or tries < 1:
            raise ValueError("tries is how many times to load and apply the change: 1 or more.")
        for _ in range(tries):
            record, version = self.load_one(dispute_id)
            new = change(record)
            if lifecycle.to_json(new) == lifecycle.to_json(record):
                return version
            try:
                return self.save(new, version, why)
            except Conflict:
                continue
        raise Conflict(CHANGED)

    def versions(self, dispute_id: str) -> list[tuple[int, str, datetime]]:
        """(version, why, saved_at) for each version of the case, oldest first, for the screens."""
        with self._read() as connection:
            rows = connection.execute("SELECT version, why, saved_at FROM case_versions WHERE dispute_id = ? "
                                      "ORDER BY version", (dispute_id,)).fetchall()
        return [(version, why, _time(saved_at)) for version, why, saved_at in rows]

    # ------------------------------------------------------------------------- approvals
    def approve(self, record: CaseRecord, expected: int, row: ApprovalRecord) -> int:
        """One transaction: the approval row first, then the record's next version. A Conflict saves neither."""
        _check_expected(expected, new=False)
        if (row.dispute_id, row.round_number) != (record.dispute_id, len(record.rounds)):
            raise ValueError("The approval is for another dispute or round than the record.")
        approval = approvals.to_json(row)
        if approvals.from_json(approval) != row:
            raise ValueError(WONT_LOAD.format(what="approval"))
        text = _record_text(record)
        with self._write() as connection:
            connection.execute("INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) "
                               "VALUES (?, ?, ?, ?, ?)",
                               (row.dispute_id, row.round_number, row.plan_sha256, approval, self._now()))
            return self._insert_version(connection, record.dispute_id, text, expected, "approve")

    def latest_approval(self, dispute_id: str, round_number: int) -> ApprovalRecord | None:
        """The round's newest approval row, or None. Unloadable when that row won't load."""
        with self._read() as connection:
            row = connection.execute("SELECT approval FROM approvals WHERE dispute_id = ? AND round_number = ? "
                                     "ORDER BY id DESC LIMIT 1", (dispute_id, round_number)).fetchone()
        return None if row is None else _approval(row[0])

    def approvals(self, dispute_id: str) -> list[ApprovalRecord]:
        """Every approval row of the case, oldest first. Unloadable when one won't load."""
        with self._read() as connection:
            rows = connection.execute("SELECT approval FROM approvals WHERE dispute_id = ? ORDER BY id",
                                      (dispute_id,)).fetchall()
        return [_approval(text) for (text,) in rows]

    # ------------------------------------------------------------------------- the send step
    def upload_for(self, request_id: str, sha256: str) -> Upload | None:
        with self._read() as connection:
            row = connection.execute("SELECT request_id, sha256, dispute_id, round_number, file_id, filename, pdf_sha256, "
                                     "size, uploaded_at FROM uploads WHERE request_id = ? AND sha256 = ?",
                                     (request_id, sha256)).fetchone()
        return None if row is None else _upload(row)

    def put_upload(self, upload: Upload) -> None:
        """ValueError when the plan's file is already on file: a file uploaded for a plan is never uploaded again."""
        values = (upload.request_id, upload.sha256, upload.dispute_id, upload.round_number, upload.file_id,
                  upload.filename, upload.pdf_sha256, upload.size, _iso(upload.uploaded_at))
        if not (all(isinstance(value, str) and value for value in (*values[:3], *values[4:7]))
                and type(upload.round_number) is int and type(upload.size) is int):
            raise TypeError("An upload names its request, file, dispute and round, its file id and its size.")
        with self._write() as connection:
            try:
                connection.execute("INSERT INTO uploads (request_id, sha256, dispute_id, round_number, file_id, filename, "
                                   "pdf_sha256, size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            except sqlite3.IntegrityError:
                raise ValueError("This plan's file is already on file, so it isn't uploaded again.") from None

    def post_for(self, request_id: str) -> PostRequest | None:
        """The saved body for the request id, or None. Unloadable when the row won't load: it is never sent again then."""
        with self._read() as connection:
            row = connection.execute("SELECT request_id, dispute_id, round_number, path, body FROM posts "
                                     "WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        try:
            return PostRequest(*row)
        except (TypeError, ValueError):
            raise Unloadable(POST_UNREADABLE) from None

    def start_send(self, record: CaseRecord, expected: int, post: PostRequest) -> int:
        """One transaction: the body (saved, or the same text already there), then the record that sending() returned.
        So the body is on disk before the first try, and every try sends those bytes. Another body for the same request
        id raises ValueError, and a Conflict saves neither."""
        _check_expected(expected, new=False)
        plan = record.rounds[-1].plan
        if lifecycle.phase(record) is not Phase.SENT or plan is None or plan.request_id != post.request_id \
                or (post.dispute_id, post.round_number) != (record.dispute_id, len(record.rounds)):
            raise ValueError("The request isn't the one this record is sending.")
        text = _record_text(record)
        with self._write() as connection:
            row = connection.execute("SELECT dispute_id, round_number, path, body FROM posts WHERE request_id = ?",
                                     (post.request_id,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO posts (request_id, dispute_id, round_number, path, body, saved_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?)", (post.request_id, post.dispute_id, post.round_number,
                                                                post.path, post.body, self._now()))
            elif tuple(row) != (post.dispute_id, post.round_number, post.path, post.body):
                raise ValueError("Another request is saved with this request id, so nothing was saved.")
            return self._insert_version(connection, record.dispute_id, text, expected, "send")

    def put_try(self, attempt: Try) -> None:
        """ValueError when the try's request isn't saved: a body is always saved before its first try."""
        if not isinstance(attempt.outcome, Outcome) or not isinstance(attempt.detail, str):
            raise TypeError("A try has an Outcome and a detail.")
        if attempt.status is not None and type(attempt.status) is not int:
            raise TypeError("A try's status is an HTTP status or None.")
        values = (attempt.request_id, _iso(attempt.at), attempt.outcome.value, attempt.status, attempt.detail,
                  None if attempt.not_before is None else _iso(attempt.not_before))
        with self._write() as connection:
            try:
                connection.execute("INSERT INTO tries (request_id, at, outcome, status, detail, not_before) "
                                   "VALUES (?, ?, ?, ?, ?, ?)", values)
            except sqlite3.IntegrityError:
                raise ValueError("A try needs its request saved first.") from None

    def tries(self, request_id: str) -> list[Try]:
        """Every try of the request, oldest first. Unloadable when one won't load."""
        with self._read() as connection:
            rows = connection.execute("SELECT request_id, at, outcome, status, detail, not_before FROM tries "
                                      "WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
        return [_try(row) for row in rows]

    # ------------------------------------------------------------------------- passes
    def begin_pass(self, at: datetime) -> int:
        with self._write() as connection:
            return connection.execute("INSERT INTO passes (started_at) VALUES (?)", (_iso(at),)).lastrowid

    def end_pass(self, pass_id: int, at: datetime, problems: Sequence[str], alerts: Sequence[Alert],
                 summary: Mapping[str, Any]) -> None:
        """The pass's one change: when it ended, its lines, its alerts and its counts. ValueError when it has already
        ended, or never began."""
        lines = list(problems)
        if not all(isinstance(line, str) for line in lines):
            raise TypeError("A pass's problems are plain lines.")
        values = (_iso(at), _json(lines), _json([dump(alert) for alert in alerts]), _json(dict(summary)), pass_id)
        with self._write() as connection:
            changed = connection.execute("UPDATE passes SET finished_at = ?, problems = ?, alerts = ?, summary = ? "
                                         "WHERE id = ? AND finished_at IS NULL", values).rowcount
            if changed != 1:
                raise ValueError("This pass has already ended, or never began.")

    def passes(self, limit: int = 20) -> list[PassRow]:
        """The newest passes first. Unloadable when one won't load."""
        with self._read() as connection:
            rows = connection.execute(f"SELECT {PASS_COLUMNS} FROM passes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_pass_row(row) for row in rows]

    # ------------------------------------------------------------------------- scope
    def put_store_payment(self, payment: StorePayment) -> None:
        """Written by the bank's Monday step, or by the demo from the recording. The same row again changes nothing;
        another row for the same payment raises ValueError."""
        values = (payment.payment_id, payment.order_number, payment.merchant_order_id, _iso(payment.created_at))
        self._put_once("store_payments", ("payment_id", "order_number", "merchant_order_id", "created_at"), values)

    def store_payments(self) -> list[StorePayment]:
        """The run's payments, by id. A row that won't load is left out: scope_problems() says so."""
        return [payment for payment in self._scope()[0] if payment is not None]

    def put_issuer_date(self, date: IssuerDate) -> None:
        values = (date.dispute_id, date.case_id, _iso(date.opened_at))
        self._put_once("issuer_dates", ("dispute_id", "case_id", "opened_at"), values)

    def opened(self) -> dict[str, datetime]:
        """Dispute id -> when the story opened it, for poll(opened=...). A row that won't load is left out."""
        return {date.dispute_id: date.opened_at for date in self._scope()[1] if date is not None}

    def scope_problems(self) -> tuple[str, ...]:
        """The pass's line when a row of the run's payments or dates won't load, else ()."""
        payments, dates = self._scope()
        return (SCOPE_ROW,) if None in payments or None in dates else ()

    # ------------------------------------------------------------------------- meta
    def meta(self, key: str) -> str | None:
        with self._read() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """mode ("live", "record" or "demo"), run, store ("fernhill" or "ending_2") and ending. created_at is set when the
        file is made."""
        if key not in META_KEYS - {"created_at"} or not (isinstance(value, str) and value):
            raise ValueError("Meta holds mode, run, store and ending, each as text.")
        if (key == "mode" and value not in MODES) or (key == "store" and value not in STORES):
            raise ValueError("The mode is live, record or demo, and the store fernhill or ending_2.")
        with self._write() as connection:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------------- a page's view
    def snapshot(self) -> Snapshot:
        """Everything a page shows, from one read transaction: each case's newest record (one that won't load goes to
        broken), every approval row, the readings and bases rounds.current() can read, every try with its request's
        dispute, the latest pass that finished, and meta. No POST body and no uploaded file id."""
        with self._read() as connection:
            heads = connection.execute(HEADS).fetchall()
            by_dispute, tries, unreadable = _parts(connection)
            saved = FrozenSaved(connection.execute(LATEST_READINGS).fetchall(),
                                connection.execute(BUSY_COUNTS).fetchall(), connection.execute(LATEST_BASES).fetchall())
            last = connection.execute(f"SELECT {PASS_COLUMNS} FROM passes WHERE finished_at IS NOT NULL "
                                      "ORDER BY id DESC LIMIT 1").fetchone()
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        cases = _cases(heads, unreadable)
        last_pass, problems = None, ()
        if last is not None:
            try:
                last_pass = _pass_row(last)
                problems = last_pass.problems
            except Unloadable:
                problems = (PASS_UNREADABLE,)
        return Snapshot(MappingProxyType(cases.records), MappingProxyType(cases.versions), MappingProxyType(cases.broken),
                        MappingProxyType({key: tuple(rows) for key, rows in by_dispute.items()}), saved,
                        MappingProxyType({key: tuple(rows) for key, rows in tries.items()}), tuple(problems), last_pass,
                        MappingProxyType(meta))

    # ------------------------------------------------------------------------- inside
    def _open(self) -> None:
        try:
            connection = self.connect()
        except sqlite3.OperationalError:  # locked, or a folder this user can't write to: not the file's fault
            raise
        except sqlite3.DatabaseError:  # not an SQLite file at all
            raise SetupError(NOT_OURS) from None
        try:
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            except sqlite3.OperationalError:
                raise
            except sqlite3.DatabaseError:
                raise SetupError(NOT_OURS) from None
            if version > FORMAT:
                raise SetupError(NEWER.format(format=version))
            if version == FORMAT:
                return
            if version != 0 or tables:
                raise SetupError(NOT_OURS)
            connection.execute("PRAGMA journal_mode = WAL")  # outside a transaction; the file keeps it
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("PRAGMA user_version").fetchone()[0] != FORMAT:  # another process may have made it
                    for statement in SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT INTO meta (key, value) VALUES ('created_at', ?)", (self._now(),))
                    connection.execute(f"PRAGMA user_version = {FORMAT}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One read transaction (BEGIN, not IMMEDIATE), so every query in it sees the same database."""
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                if connection.in_transaction:
                    connection.execute("COMMIT")
        finally:
            connection.close()

    def _insert_version(self, connection: sqlite3.Connection, dispute_id: str, text: str, expected: int | None,
                        why: str) -> int:
        head = connection.execute("SELECT MAX(version) FROM case_versions WHERE dispute_id = ?", (dispute_id,)).fetchone()[0]
        if head != expected:
            raise Conflict(CHANGED)
        version = 1 if head is None else head + 1
        connection.execute("INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES (?, ?, ?, ?, ?)",
                           (dispute_id, version, text, why, self._now()))
        return version

    def _put_once(self, table: str, columns: Sequence[str], values: Sequence[Any]) -> None:
        if not all(isinstance(value, str) and value for value in values):
            raise TypeError(f"Each value of a {table} row is text.")
        with self._write() as connection:
            row = connection.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {columns[0]} = ?",
                                     (values[0],)).fetchone()
            if row is None:
                connection.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
                                   tuple(values))
            elif tuple(row) != tuple(values):
                raise ValueError(f"Another {table} row is saved for this id.")

    def _scope(self) -> tuple[list[StorePayment | None], list[IssuerDate | None]]:
        with self._read() as connection:
            payments = connection.execute("SELECT payment_id, order_number, merchant_order_id, created_at "
                                          "FROM store_payments ORDER BY payment_id").fetchall()
            dates = connection.execute("SELECT dispute_id, case_id, opened_at FROM issuer_dates "
                                       "ORDER BY dispute_id").fetchall()
        return ([_or_none(lambda row=row: StorePayment(*row[:3], _time(row[3]))) for row in payments],
                [_or_none(lambda row=row: IssuerDate(*row[:2], _time(row[2]))) for row in dates])

    def _now(self) -> str:
        return _iso(self._stamp())


class SqliteSaved:
    """rounds.Saved on this database, exactly as rounds.MemorySaved behaves: every put goes through JSON and back first,
    so a value that saves but won't load fails at put and nothing of it is kept."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def put_reading(self, dispute_id: str, round_number: int, reading: Reading) -> None:
        from disputable import reader  # here, so a command that only shows the queue never loads the Anthropic SDK

        text = reader.to_json(reading)
        loaded = reader.from_json(text)
        with self._db._write() as connection:
            connection.execute("INSERT INTO readings (dispute_id, round_number, key, failure, reading, saved_at) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (dispute_id, round_number, loaded.key, loaded.failure.value if loaded.failure else None,
                                text, self._db._now()))

    def reading(self, dispute_id: str, round_number: int, key: str) -> Reading | None:
        """The latest reading put for the round with this key and no failure: a failed one is never reused."""
        from disputable import reader

        with self._db._read() as connection:
            row = connection.execute("SELECT reading FROM readings WHERE dispute_id = ? AND round_number = ? AND key = ? "
                                     "AND failure IS NULL ORDER BY id DESC LIMIT 1",
                                     (dispute_id, round_number, key)).fetchone()
        return None if row is None else reader.from_json(row[0])

    def busy(self, dispute_id: str, round_number: int) -> int:
        """How many BUSY readings were put for the round."""
        with self._db._read() as connection:
            return connection.execute("SELECT COUNT(*) FROM readings WHERE dispute_id = ? AND round_number = ? "
                                      "AND failure = ?", (dispute_id, round_number, BUSY)).fetchone()[0]

    def put_basis(self, basis: Basis) -> None:
        from disputable import rounds

        text = rounds.basis_to_json(basis)
        loaded = rounds.basis_from_json(text)
        with self._db._write() as connection:
            connection.execute("INSERT INTO bases (dispute_id, round_number, plan_sha256, basis, saved_at) "
                               "VALUES (?, ?, ?, ?, ?)",
                               (loaded.dispute_id, loaded.round_number, loaded.plan_sha256, text, self._db._now()))

    def basis(self, dispute_id: str, round_number: int) -> Basis | None:
        """The latest basis put for the round."""
        from disputable import rounds

        with self._db._read() as connection:
            row = connection.execute("SELECT basis FROM bases WHERE dispute_id = ? AND round_number = ? "
                                     "ORDER BY id DESC LIMIT 1", (dispute_id, round_number)).fetchone()
        return None if row is None else rounds.basis_from_json(row[0])


class FrozenSaved:
    """rounds.Saved over a snapshot's rows: it reads exactly as SqliteSaved does, and put_reading and put_basis raise
    RuntimeError. A row loads when it is first read, so one that won't load raises Unloadable there, as it would from
    SqliteSaved."""

    def __init__(self, readings: Iterable[Sequence[Any]], busy: Iterable[Sequence[Any]],
                 bases: Iterable[Sequence[Any]]) -> None:
        """readings: (dispute id, round, key, text) of the latest row with no failure for each key; busy: (dispute id,
        round, count of BUSY rows); bases: (dispute id, round, text) of the latest basis of each round."""
        self._readings = {(dispute_id, number, key): text for dispute_id, number, key, text in readings}
        self._busy = {(dispute_id, number): count for dispute_id, number, count in busy}
        self._bases = {(dispute_id, number): text for dispute_id, number, text in bases}
        self._loaded: dict[tuple[Any, ...], Any] = {}

    def put_reading(self, dispute_id: str, round_number: int, reading: Reading) -> None:
        raise RuntimeError(READ_ONLY)

    def reading(self, dispute_id: str, round_number: int, key: str) -> Reading | None:
        from disputable import reader

        return self._load(("reading", dispute_id, round_number, key), self._readings.get((dispute_id, round_number, key)),
                          reader.from_json)

    def busy(self, dispute_id: str, round_number: int) -> int:
        return self._busy.get((dispute_id, round_number), 0)

    def put_basis(self, basis: Basis) -> None:
        raise RuntimeError(READ_ONLY)

    def basis(self, dispute_id: str, round_number: int) -> Basis | None:
        from disputable import rounds

        return self._load(("basis", dispute_id, round_number), self._bases.get((dispute_id, round_number)),
                          rounds.basis_from_json)

    def _load(self, key: tuple[Any, ...], text: str | None, loader: Callable[[str], Any]) -> Any:
        if text is None:
            return None
        if key not in self._loaded:
            self._loaded[key] = loader(text)  # Unloadable comes out, and nothing is kept for it
        return self._loaded[key]


# ----------------------------------------------------------------------------- one pass at a time
class PassLock:
    """One pass at a time per data folder: a thread lock and an OS lock on the file, both taken without waiting. A
    second pass, in this process or another, gets False from acquire() and doesn't start. The OS frees its lock when the
    process ends, so a crash never leaves the folder locked."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread = threading.Lock()
        self._fd: int | None = None

    def acquire(self) -> bool:
        if not self._thread.acquire(blocking=False):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except BaseException:
            self._thread.release()
            raise
        try:
            _lock_file(fd)
        except OSError:  # another process holds it, or another lock on this file in this one
            os.close(fd)
            self._thread.release()
            return False
        except BaseException:
            os.close(fd)
            self._thread.release()
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            raise RuntimeError("The pass lock isn't held.")
        self._fd = None
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)
            self._thread.release()

    def held(self) -> bool:
        return self._fd is not None


if sys.platform == "win32":
    import msvcrt

    def _lock_file(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # one byte, without waiting

    def _unlock_file(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ----------------------------------------------------------------------------- rows
def _loaded(rows: Iterable[Sequence[Any]]) -> Loaded:
    records, versions, broken = {}, {}, {}
    for dispute_id, version, text in rows:
        try:
            records[dispute_id] = _record(dispute_id, text)
            versions[dispute_id] = version
        except Unloadable as error:
            broken[dispute_id] = str(error)
    return Loaded(records, versions, broken)


def _parts(connection: sqlite3.Connection) -> tuple[dict[str, list[ApprovalRecord]], dict[str, list[Try]],
                                                    dict[str, list[str]]]:
    """Every approval row and every try (by its request's dispute), each loaded on its own: the rows that load, oldest
    first, by case, and for each case with a row that won't load, one line for each kind, approvals first."""
    approvals_of: dict[str, list[ApprovalRecord]] = {}
    tries_of: dict[str, list[Try]] = {}
    unreadable: dict[str, list[str]] = {}

    def note(dispute_id: str, line: str) -> None:
        lines = unreadable.setdefault(dispute_id, [])
        if line not in lines:
            lines.append(line)

    for dispute_id, text in connection.execute(APPROVALS_BY_DISPUTE).fetchall():
        try:
            approval = _approval(text)
        except Unloadable:
            note(dispute_id, APPROVAL_UNREADABLE)
        else:
            approvals_of.setdefault(dispute_id, []).append(approval)
    for dispute_id, *row in connection.execute(TRIES_BY_DISPUTE).fetchall():
        try:
            attempt = _try(row)
        except Unloadable:
            note(dispute_id, TRY_UNREADABLE)
        else:
            tries_of.setdefault(dispute_id, []).append(attempt)
    return approvals_of, tries_of, unreadable


def _cases(heads: Iterable[Sequence[Any]], unreadable: Mapping[str, Sequence[str]]) -> Loaded:
    """The one rule for load() and snapshot(): each case's newest record, unless it or another row of the case won't
    load. Then the case is broken, its newest record's message first, then a line for each kind of row."""
    loaded = _loaded(heads)
    records, versions, broken = dict(loaded.records), dict(loaded.versions), dict(loaded.broken)
    for dispute_id, lines in unreadable.items():
        text = broken.get(dispute_id, "")
        for line in lines:
            if line not in text:
                text = f"{text} {line}" if text else line
        broken[dispute_id] = text
        records.pop(dispute_id, None)
        versions.pop(dispute_id, None)
    return Loaded(records, versions, broken)


def _record(dispute_id: str, text: Any) -> CaseRecord:
    try:
        record = lifecycle.from_json(text)
    except Unloadable:
        raise
    except (ValueError, TypeError, LookupError, AttributeError, RecursionError):  # each row on its own: a bug too
        raise Unloadable(RECORD_UNREADABLE) from None
    if record.dispute_id != dispute_id:
        raise Unloadable(OTHER_DISPUTE)
    return record


def _record_text(record: CaseRecord) -> str:
    text = lifecycle.to_json(record)
    if lifecycle.to_json(lifecycle.from_json(text)) != text:
        raise ValueError(WONT_LOAD.format(what="record"))
    return text


def _approval(text: Any) -> ApprovalRecord:
    try:
        return approvals.from_json(text)
    except Unloadable:
        raise
    except (ValueError, TypeError, LookupError, AttributeError, RecursionError):
        raise Unloadable(APPROVAL_UNREADABLE) from None


def _upload(row: Sequence[Any]) -> Upload:
    request_id, sha256, dispute_id, round_number, file_id, filename, pdf_sha256, size, uploaded_at = row
    try:
        return Upload(request_id, sha256, dispute_id, round_number, file_id, filename, pdf_sha256, size,
                      _time(uploaded_at))
    except Unloadable:
        raise Unloadable(UPLOAD_UNREADABLE) from None


def _try(row: Sequence[Any]) -> Try:
    request_id, at, outcome, status, detail, not_before = row
    try:
        return Try(request_id, _time(at), Outcome(outcome), status, detail,
                   None if not_before is None else _time(not_before))
    except (Unloadable, ValueError):
        raise Unloadable(TRY_UNREADABLE) from None


def _pass_row(row: Sequence[Any]) -> PassRow:
    pass_id, started_at, finished_at, problems, alerts, summary = row
    try:
        lines, found, counts = json.loads(problems), json.loads(alerts), json.loads(summary)
        if not (isinstance(lines, list) and all(isinstance(line, str) for line in lines) and isinstance(found, list)
                and isinstance(counts, dict)):
            raise Unloadable(PASS_UNREADABLE)
        return PassRow(pass_id, _time(started_at), None if finished_at is None else _time(finished_at), tuple(lines),
                       tuple(load(Alert, item, "the pass's alert") for item in found), MappingProxyType(counts))
    except (ValueError, TypeError, RecursionError):  # Unloadable is a ValueError
        raise Unloadable(PASS_UNREADABLE) from None


def _or_none(build: Callable[[], Any]) -> Any:
    try:
        return build()
    except (Unloadable, TypeError, ValueError):
        return None


def _iso(moment: datetime) -> str:
    """A time as this database keeps it: ISO 8601 text, in UTC, with its offset."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("A time needs a time zone.")
    return moment.astimezone(timezone.utc).isoformat()


def _time(text: Any) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise Unloadable(TIME_UNREADABLE) from None
    if moment.utcoffset() is None:
        raise Unloadable(TIME_UNREADABLE)
    return moment


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


def _check_why(why: str) -> None:
    if why not in WHYS:
        raise ValueError(f"{why!r} isn't why a version is saved: {', '.join(sorted(WHYS))}.")


def _check_expected(expected: int | None, *, new: bool) -> None:
    if expected is None and new:
        return
    if type(expected) is not int or expected < 1:
        raise ValueError("expected is the version the case was loaded at" + (", or None for a new case." if new else "."))
