"""Storage: the format and the pragmas, append-only rows and optimistic concurrency, rows that won't load, the approval
and the send step's transactions, rounds.Saved on SQLite and on a page's snapshot, and the pass lock across processes.
Every database is a temp file; every record comes from the fake sandbox and the story's passes (tests/sandbox.py).
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from disputable import approvals, db as storage, lifecycle, privacy
from disputable.airwallex import Outcome, PostRequest, body_text, challenge_body, respond_path
from disputable.config import SetupError
from disputable.db import (APPEND_ONLY, APPROVAL_UNREADABLE, NOT_OURS, PASS_UNREADABLE, SCOPE_ROW, TRY_UNREADABLE,
                           Conflict, Database, FrozenSaved, IssuerDate, PassLock, StorePayment, Try, Upload)
from disputable.jsonable import Unloadable
from disputable.lifecycle import Refused, from_json, sending, to_json
from disputable.reader import Failure, NoReader, failed
from disputable.rounds import Basis, MemorySaved
from disputable.seed.fernhill import FERNHILL, FIRST_PACKAGE, MONDAY
from fake_airwallex import ACCOUNT_ID, file_id
from sandbox import MINUTE, Story
from test_rounds import Reads, Rounds, plain, play, request_for

T0 = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
CASES = ("ear_tips", "earbuds", "headphones")
TABLES = {"meta", "case_versions", "approvals", "readings", "bases", "uploads", "posts", "tries", "passes",
          "store_payments", "issuer_dates"}
FILE_ID = file_id(FIRST_PACKAGE.filename, 1793393507092)  # what the fake sandbox would answer: its account id inside


def database(tmp_path, name="live.sqlite3"):
    return Database(tmp_path / name, stamp=lambda: T0)


def query(db, sql, *values):
    connection = db.connect()
    try:
        return connection.execute(sql, values).fetchall()
    finally:
        connection.close()


def rows(db, table):
    return query(db, f"SELECT COUNT(*) FROM {table}")[0][0]


def raw(db, sql, *values):
    """SQL straight into the file, as a crash, a bug or a hand edit could leave it."""
    query(db, sql, *values)


def json_of(records):
    return {dispute_id: to_json(record) for dispute_id, record in records.items()}


# ----------------------------------------------------------------------------- the story, saved as the loop saves it
def saved_story(db, cases=CASES):
    """Monday's first read and decision, each saved: every case at version 2."""
    story = Story(cases=cases)
    story.poll(MONDAY)
    for record in story.records.values():
        assert db.save(record, None, "poll") == 1
    story.decide(MONDAY)
    for record in story.records.values():
        assert db.save(record, 1, "decide") == 2
    return story


def approved(db, story, case_id="headphones"):
    """Priya approves the plan her screen showed: the approval row and the record, as one write."""
    record = story[case_id]
    after, row = approvals.approve(record, record.rounds[-1].plan, "Priya", MONDAY, reading_key="")
    db.approve(after, db.load_one(after.dispute_id)[1], row)
    story.records[after.dispute_id] = after
    return row


def being_sent(db, story, case_id):
    """A fresh read saved, then sending() as the send step would save it with its body: the record and its version."""
    story.poll(MONDAY + MINUTE)
    record = story[case_id]
    version = db.save(record, db.load_one(record.dispute_id)[1], "poll")
    return sending(record, MONDAY + MINUTE), version


def headphones_sent(db):
    """The headphones' challenge saved as the send step saves it: approved, its file uploaded, the body and the SENT
    record in one write, and the answer's try."""
    story = saved_story(db)
    approved(db, story)
    record, version = being_sent(db, story, "headphones")
    plan = record.rounds[-1].plan
    pdf = FIRST_PACKAGE.pdf()
    db.put_upload(Upload(plan.request_id, FIRST_PACKAGE.sha256, record.dispute_id, 1, FILE_ID, FIRST_PACKAGE.filename,
                         hashlib.sha256(pdf).hexdigest(), len(pdf), MONDAY + MINUTE))
    post = PostRequest(plan.request_id, record.dispute_id, 1, respond_path(plan),
                       body_text(challenge_body(plan, [(FIRST_PACKAGE, FILE_ID)])))
    version = db.start_send(record, version, post)
    db.put_try(Try(plan.request_id, MONDAY + MINUTE, Outcome.TAKEN, 200, "Airwallex took it", None))
    return story, record, version, post


# ----------------------------------------------------------------------------- the file
def test_format_number(tmp_path):
    path = tmp_path / "new" / "folder" / "live.sqlite3"
    db = Database(path, stamp=lambda: T0)  # the folder is made
    assert query(db, "PRAGMA user_version")[0][0] == storage.FORMAT == 1
    tables = {name for (name,) in query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables - {"sqlite_sequence"} == TABLES and "calls" not in tables
    assert db.meta("created_at") == T0.isoformat()
    assert Database(path, stamp=lambda: T0 + timedelta(days=1)).meta("created_at") == T0.isoformat()  # opened again
    newer = tmp_path / "newer.sqlite3"
    Database(newer, stamp=lambda: T0)
    raw(Database(newer, stamp=lambda: T0), "PRAGMA user_version = 2")
    with pytest.raises(SetupError, match=r"made by a newer version of Disputable \(format 2\)\. Use that version, or "
                                         r"start a new database\."):
        Database(newer, stamp=lambda: T0)
    foreign = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE notes (text TEXT)")
    connection.close()
    text = tmp_path / "text.sqlite3"
    text.write_text("Not a database, only some text long enough to fill a page header. " * 20, encoding="utf-8")
    for other in (foreign, text):
        with pytest.raises(SetupError) as refused:
            Database(other, stamp=lambda: T0)
        assert str(refused.value) == NOT_OURS == "This file isn't a Disputable database."
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    assert Database(empty, stamp=lambda: T0).meta("created_at") == T0.isoformat()  # an empty file is a new database


def test_pragmas(tmp_path):
    db = database(tmp_path)
    for _ in range(2):  # every connection, not only the first
        connection = db.connect()
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
            assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1  # REPLACE's delete fires them
            assert connection.isolation_level is None  # BEGIN IMMEDIATE ... COMMIT by hand, never an implicit one
        finally:
            connection.close()


def test_writes_take_the_write_lock_first(tmp_path):
    """Every write opens with BEGIN IMMEDIATE, so two writers wait for each other: a deferred BEGIN would let one check
    its version, then fail at its INSERT with "database is locked", which no caller expects, instead of saving."""
    db = database(tmp_path)
    opened = []
    connect = db.connect

    def traced():
        connection = connect()
        statements: list[str] = []
        opened.append(statements)
        connection.set_trace_callback(statements.append)
        return connection

    db.connect = traced
    story, record, version, post = headphones_sent(db)  # save, approve, put_upload, start_send and put_try
    db.saved.put_reading("dst_headphones", 1, failed("a" * 64, Failure.NO_KEY))
    db.saved.put_basis(Basis(1, "dst_headphones", 1, "", "a" * 64, "NO_KEY", (), (), (), ()))
    db.update("dst_ear_tips", lambda head: replace(head, others=head.others + 1), "poll")
    db.end_pass(db.begin_pass(T0), T0 + MINUTE, [], [], {})
    db.set_meta("mode", "live")
    db.put_store_payment(StorePayment("int_ear_tips", "1121", "FH-1121-test", MONDAY))
    db.put_issuer_date(IssuerDate("dst_ear_tips", "ear_tips", MONDAY))
    writes = [statements for statements in opened
              if any(statement.split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE") for statement in statements)]
    assert len(writes) >= 14 and {statements[0] for statements in writes} == {"BEGIN IMMEDIATE"}
    db.connect = connect
    # Two writers at once: one pauses between its version check and its INSERT, and the other saves another case.
    x, y = story["ear_tips"], story["earbuds"]
    checked, go, saved = threading.Event(), threading.Event(), {}
    insert = storage.Database._insert_version

    def paused(self, connection, dispute_id, text, expected, why):
        if dispute_id == x.dispute_id:
            connection.execute("SELECT MAX(version) FROM case_versions WHERE dispute_id = ?", (dispute_id,)).fetchone()
            checked.set()
            go.wait(30)
        return insert(self, connection, dispute_id, text, expected, why)

    def save(record):
        try:
            saved[record.dispute_id] = db.save(record, db.load_one(record.dispute_id)[1], "poll")
        except Exception as error:  # noqa: BLE001 - the test reports what each writer got
            saved[record.dispute_id] = f"{type(error).__name__}: {error}"

    db._insert_version = paused.__get__(db)
    first = threading.Thread(target=save, args=(x,))
    first.start()
    assert checked.wait(30)
    second = threading.Thread(target=save, args=(y,))
    second.start()
    second.join(1.0)  # it waits for the first writer's lock; a deferred BEGIN would have saved by now
    go.set()
    first.join(30)
    second.join(30)
    assert saved == {"dst_ear_tips": 4, "dst_earbuds": 3}  # both saved: a writer waits, it never fails


# ----------------------------------------------------------------------------- versions and the optimistic lock
def test_a_stale_save_is_refused(tmp_path):
    db = database(tmp_path)
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY)
    first = story["ear_tips"]
    assert db.save(first, None, "poll") == 1
    with pytest.raises(Conflict, match="Nothing was saved"):
        db.save(first, None, "poll")  # a new case, saved twice
    story.decide(MONDAY)
    decided = story["ear_tips"]
    assert db.save(decided, 1, "decide") == 2
    with pytest.raises(Conflict):
        db.save(first, 1, "poll")  # another writer saved version 2 first
    with pytest.raises(Conflict):
        db.save(first, 5, "poll")
    record, version = db.load_one("dst_ear_tips")
    assert (to_json(record), version) == (to_json(decided), 2)
    assert db.versions("dst_ear_tips") == [(1, "poll", T0), (2, "decide", T0)]
    for why in ("", "saved", "Poll", None):
        with pytest.raises(ValueError, match="isn't why a version is saved"):
            db.save(decided, 2, why)
    for expected in (0, -1, "2", True, 2.0):
        with pytest.raises(ValueError, match="expected is the version"):
            db.save(decided, expected, "poll")
    with pytest.raises(KeyError):
        db.load_one("dst_unknown")
    assert db.versions("dst_unknown") == [] and rows(db, "case_versions") == 2


def test_put_round_trips_first(tmp_path):
    """A value that saves but won't load fails when it is put, and nothing of it is kept: a record, a reading, a basis
    or an approval."""
    db = database(tmp_path)
    story = Story(cases=("ear_tips", "headphones"))
    story.poll(MONDAY)
    story.decide(MONDAY)
    record = story["ear_tips"]
    plan = record.rounds[-1].plan
    odd = replace(record, rounds=(replace(record.rounds[-1], plan=replace(plan, request_id="not-from-the-terms")),))
    with pytest.raises(Unloadable, match="request id doesn't come from its terms"):
        db.save(odd, None, "decide")
    request = request_for(FERNHILL, "headphones")
    with pytest.raises(Unloadable):
        db.saved.put_reading("dst_headphones", 1, replace(plain(request), format=2))
    with pytest.raises(Unloadable):
        db.saved.put_basis(Basis(1, "dst_headphones", 1, "", request.key, "SOON", (), (), (), ()))
    headphones = story["headphones"]
    db.save(headphones, None, "decide")
    after, row = approvals.approve(headphones, headphones.rounds[-1].plan, "Priya", MONDAY)
    with pytest.raises(Unloadable):
        db.approve(after, 1, replace(row, plan_sha256="0" * 64))  # its digests no longer fit its plan
    assert [rows(db, table) for table in ("case_versions", "readings", "bases", "approvals")] == [1, 0, 0, 0]


def test_update_applies_again_after_a_conflict(tmp_path):
    db = database(tmp_path)
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY)
    db.save(story["ear_tips"], None, "poll")
    seen = []

    def count_one_more(record):
        seen.append(record)
        if len(seen) == 1:  # a screen writes while the change is applied
            db.save(replace(record, blocked="A person checks it."), 1, "approve")
        return replace(record, others=record.others + 1)

    assert db.update("dst_ear_tips", count_one_more, "taken") == 3
    head, version = db.load_one("dst_ear_tips")
    assert (head.blocked, head.others, version) == ("A person checks it.", 1, 3)  # both changes stand
    assert [record.blocked for record in seen] == [None, "A person checks it."]
    assert db.update("dst_ear_tips", lambda record: record, "taken") == 3  # nothing changed, nothing saved
    assert [why for _, why, _ in db.versions("dst_ear_tips")] == ["poll", "approve", "taken"]

    def always_beaten(record):
        db.save(replace(record, others=record.others + 10), db.load_one("dst_ear_tips")[1], "poll")
        return replace(record, blocked=None)

    with pytest.raises(Conflict):
        db.update("dst_ear_tips", always_beaten, "taken", tries=2)

    def refused(record):
        raise Refused("No response with this request id is being sent.")

    before = rows(db, "case_versions")
    with pytest.raises(Refused):
        db.update("dst_ear_tips", refused, "taken")
    assert rows(db, "case_versions") == before
    with pytest.raises(ValueError, match="tries"):
        db.update("dst_ear_tips", refused, "taken", tries=0)


def test_append_only_tables(tmp_path):
    db = database(tmp_path)
    _, _, _, post = headphones_sent(db)
    db.saved.put_reading("dst_headphones", 1, failed("a" * 64, Failure.NO_KEY))
    db.saved.put_basis(Basis(1, "dst_headphones", 1, "", "a" * 64, "NO_KEY", (), (), (), ()))
    assert {table: rows(db, table) > 0 for table in APPEND_ONLY} == dict.fromkeys(APPEND_ONLY, True)
    for table in APPEND_ONLY:  # REPLACE deletes the old row first: with recursive triggers on, that is refused too
        for sql in (f"UPDATE {table} SET rowid = rowid", f"DELETE FROM {table}", f"INSERT OR REPLACE INTO {table} "
                    f"SELECT * FROM {table}", f"REPLACE INTO {table} SELECT * FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
                raw(db, sql)
    evil = post.body.replace('"STANDARD"', '"AI_DISPUTE_AUTOMATION"')  # another body under the same request id
    with pytest.raises(sqlite3.IntegrityError, match="posts is append-only"):
        raw(db, "INSERT OR REPLACE INTO posts (request_id, dispute_id, round_number, path, body, saved_at) VALUES "
                "(?, ?, ?, ?, ?, ?)", post.request_id, post.dispute_id, 1, post.path, evil, T0.isoformat())
    assert db.post_for(post.request_id) == post
    # Only a pass's row, once at its end, and meta change.
    pass_id = db.begin_pass(T0)
    db.end_pass(pass_id, T0 + MINUTE, ["A line."], [], {"read": 3})
    db.set_meta("mode", "live")
    db.set_meta("mode", "demo")
    assert db.meta("mode") == "demo"


# ----------------------------------------------------------------------------- rows that won't load
def test_a_broken_row_stops_only_its_case(tmp_path):
    db = database(tmp_path)
    saved_story(db)
    raw(db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES (?, 3, ?, 'poll', ?)",
        "dst_ear_tips", "{not json", T0.isoformat())
    raw(db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES (?, 3, ?, 'poll', ?)",
        "dst_earbuds", json.dumps({"format": 2, "record": {}}), T0.isoformat())
    loaded = db.load()
    assert loaded.broken == {"dst_ear_tips": "The saved record isn't valid JSON.",
                             "dst_earbuds": "This isn't a format 1 case record."}
    assert set(loaded.records) == set(loaded.versions) == {"dst_headphones"}  # the others go on
    with pytest.raises(Unloadable, match="valid JSON"):
        db.load_one("dst_ear_tips")  # never version 2 instead: a rollback could send twice
    snapshot = db.snapshot()
    assert dict(snapshot.broken) == loaded.broken and set(snapshot.records) == {"dst_headphones"}
    # A record saved under another dispute's id doesn't load as that dispute.
    raw(db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES ('dst_other', 1, ?, 'poll', ?)",
        to_json(loaded.records["dst_headphones"]), T0.isoformat())
    assert db.load().broken["dst_other"] == storage.OTHER_DISPUTE


# ----------------------------------------------------------------------------- the approval and the record
def test_approval_and_record_in_one_transaction(tmp_path):
    db = database(tmp_path)
    story = saved_story(db)
    record = story["headphones"]
    after, row = approvals.approve(record, record.rounds[-1].plan, "Priya", MONDAY, reading_key="")
    with pytest.raises(Conflict):
        db.approve(after, 1, row)  # stale: the case is at version 2
    assert rows(db, "approvals") == 0
    with pytest.raises(ValueError, match="another dispute or round"):
        db.approve(story["earbuds"], 2, row)
    # The record's write fails after the approval row went in: neither stays.
    raw(db, "CREATE TRIGGER no_more_versions BEFORE INSERT ON case_versions BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        db.approve(after, 2, row)
    assert (rows(db, "approvals"), db.load_one("dst_headphones")[1]) == (0, 2)
    raw(db, "DROP TRIGGER no_more_versions")
    assert db.approve(after, 2, row) == 3
    assert db.latest_approval("dst_headphones", 1) == row and db.approvals("dst_headphones") == [row]
    assert db.latest_approval("dst_headphones", 2) is None and db.approvals("dst_earbuds") == []
    assert db.versions("dst_headphones")[-1][1] == "approve"
    assert approvals.verify(db.load_one("dst_headphones")[0], db.latest_approval("dst_headphones", 1)) is None
    # A double click: the same approval again is a second row, and the newest is the one read.
    again = replace(row, at=row.at + MINUTE)
    raw(db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES (?, 1, ?, ?, ?)",
        "dst_headphones", row.plan_sha256, approvals.to_json(again), T0.isoformat())
    assert db.latest_approval("dst_headphones", 1) == again and db.approvals("dst_headphones") == [row, again]
    raw(db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES (?, 1, ?, ?, ?)",
        "dst_headphones", row.plan_sha256, "{broken", T0.isoformat())
    with pytest.raises(Unloadable):
        db.latest_approval("dst_headphones", 1)  # the send step reads it as "The approval on file can't be read."
    with pytest.raises(Unloadable):
        db.approvals("dst_headphones")


# ----------------------------------------------------------------------------- rounds.Saved, on SQLite and on a snapshot
def behaviour(put, read):
    """test_rounds' MemorySaved test as steps: the puts on put, the reads on read(). Returns what the reads gave."""
    request = request_for(FERNHILL, "headphones")
    good = plain(request)
    for reading in (failed(request.key, Failure.BUSY), good, failed(request.key, Failure.NO_KEY)):
        put.put_reading("dst_headphones", 1, reading)
    put.put_reading("dst_headphones", 2, failed(request.key, Failure.BUSY))
    first = Basis(1, "dst_headphones", 1, "a" * 64, request.key, "", (), (), (), ())
    later = replace(first, plan_sha256="c" * 64)
    for basis in (first, later, replace(first, round_number=2)):
        put.put_basis(basis)
    # A value that saves but won't load fails at put, and nothing of it is kept.
    with pytest.raises(Unloadable):
        put.put_reading("dst_headphones", 1, replace(good, format=2))
    with pytest.raises(Unloadable):
        put.put_basis(replace(first, failure="SOON"))
    saved = read()
    found = (saved.reading("dst_headphones", 1, request.key), saved.reading("dst_headphones", 1, "another key"),
             saved.reading("dst_other", 1, request.key), saved.busy("dst_headphones", 1), saved.busy("dst_headphones", 2),
             saved.busy("dst_other", 1), saved.basis("dst_headphones", 1), saved.basis("dst_headphones", 2),
             saved.basis("dst_headphones", 3))
    assert found[0] == good and found[1] is found[2] is None and found[3:6] == (1, 1, 0)  # the latest with no failure
    assert found[6] == later and found[7] == replace(first, round_number=2) and found[8] is None
    return found


def test_sqlite_saved_is_memory_saved(tmp_path):
    memory = MemorySaved()
    expected = behaviour(memory, lambda: memory)
    db = database(tmp_path)
    assert behaviour(db.saved, lambda: db.saved) == expected
    assert (rows(db, "readings"), rows(db, "bases")) == (len(memory.readings), len(memory.bases)) == (4, 3)
    other = database(tmp_path, "other.sqlite3")
    assert behaviour(other.saved, lambda: other.snapshot().saved) == expected
    frozen = other.snapshot().saved
    request = request_for(FERNHILL, "headphones")
    with pytest.raises(RuntimeError, match="read-only snapshot"):
        frozen.put_reading("dst_headphones", 1, failed(request.key, Failure.BUSY))
    with pytest.raises(RuntimeError, match="read-only snapshot"):
        frozen.put_basis(Basis(1, "dst_headphones", 1, "", request.key, "", (), (), (), ()))
    other.saved.put_reading("dst_headphones", 1, failed(request.key, Failure.BUSY))  # a later put
    assert (frozen.busy("dst_headphones", 1), other.saved.busy("dst_headphones", 1)) == (1, 2)
    assert frozen.reading("dst_headphones", 1, request.key) is frozen.reading("dst_headphones", 1, request.key)


def test_a_reading_or_basis_that_wont_load_raises_when_it_is_read(tmp_path):
    db = database(tmp_path)
    request = request_for(FERNHILL, "headphones")
    db.saved.put_reading("dst_headphones", 1, plain(request))
    db.saved.put_basis(Basis(1, "dst_headphones", 1, "", request.key, "", (), (), (), ()))
    raw(db, "INSERT INTO readings (dispute_id, round_number, key, failure, reading, saved_at) VALUES "
            "('dst_headphones', 1, ?, NULL, '{broken', ?)", request.key, T0.isoformat())
    raw(db, "INSERT INTO bases (dispute_id, round_number, plan_sha256, basis, saved_at) VALUES "
            "('dst_headphones', 1, '', '[]', ?)", T0.isoformat())
    frozen = db.snapshot().saved  # the snapshot itself still works: a row loads only when it is read
    for saved in (db.saved, frozen):
        with pytest.raises(Unloadable, match="reading"):
            saved.reading("dst_headphones", 1, request.key)
        with pytest.raises(Unloadable, match="basis"):
            saved.basis("dst_headphones", 1)
        assert saved.basis("dst_headphones", 2) is None and saved.busy("dst_headphones", 1) == 0


@pytest.mark.parametrize("reads", [False, True], ids=["no key", "plain reads"])
def test_the_story_decides_the_same_on_sqlite(tmp_path, reads):
    def run(saved):
        story = Rounds(reader=Reads(plain) if reads else NoReader())
        story.saved = saved
        return play(story)

    memory, sqlite = run(MemorySaved()), run(database(tmp_path).saved)
    assert json_of(sqlite.records) == json_of(memory.records)
    frozen = Database(tmp_path / "live.sqlite3", stamp=lambda: T0).snapshot().saved
    for dispute_id, record in memory.records.items():
        for number in range(1, len(record.rounds) + 1):
            basis = memory.saved.basis(dispute_id, number)
            assert sqlite.saved.basis(dispute_id, number) == frozen.basis(dispute_id, number) == basis
            assert sqlite.saved.busy(dispute_id, number) == frozen.busy(dispute_id, number) == 0
            if basis is not None and not basis.failure:
                key = basis.reading_key
                assert sqlite.saved.reading(dispute_id, number, key) == frozen.reading(dispute_id, number, key) == \
                    memory.saved.reading(dispute_id, number, key) is not None
    assert lifecycle.cost(sqlite["headphones"]) == lifecycle.cost(memory["headphones"])


def test_the_busy_column_is_the_readers_own():
    assert storage.BUSY == Failure.BUSY.value


# ----------------------------------------------------------------------------- the send step
def test_start_send(tmp_path):
    db = database(tmp_path)
    story = saved_story(db)
    record, version = being_sent(db, story, "ear_tips")
    plan = record.rounds[-1].plan
    body = body_text({"accepted_by": "disputable", "reason": plan.terms.reason,
                      "refund": {"amount": 12, "reason": "REQUESTED_BY_CUSTOMER"}, "request_id": plan.request_id})
    post = PostRequest(plan.request_id, record.dispute_id, 1, respond_path(plan), body)
    assert db.post_for(plan.request_id) is None
    with pytest.raises(ValueError, match="A try needs its request saved first"):
        db.put_try(Try(plan.request_id, MONDAY + MINUTE, Outcome.RETRY, None, "no answer in 30 seconds", None))
    with pytest.raises(Conflict):
        db.start_send(record, version - 1, post)
    assert rows(db, "posts") == 0  # a Conflict saves neither
    sent = db.start_send(record, version, post)
    assert db.post_for(plan.request_id) == post and lifecycle.phase(db.load_one("dst_ear_tips")[0]) is lifecycle.Phase.SENT
    assert db.versions("dst_ear_tips")[-1][1] == "send"
    # The same body again is the same request: a retry saves its new sending() with it.
    assert db.start_send(record, sent, post) == sent + 1
    other = replace(post, body=post.body.replace('"amount":12', '"amount":11'))
    with pytest.raises(ValueError, match="Another request is saved with this request id"):
        db.start_send(record, sent + 1, other)
    assert db.load_one("dst_ear_tips")[1] == sent + 1 and db.post_for(plan.request_id) == post
    with pytest.raises(ValueError, match="isn't the one this record is sending"):
        db.start_send(record, sent + 1, replace(post, request_id="another", body=body.replace(plan.request_id, "another")))
    with pytest.raises(ValueError, match="isn't the one this record is sending"):
        db.start_send(story["earbuds"], 2, post)  # a plan that isn't being sent
    # The tries, oldest first, with what Airwallex asked.
    waited = Try(plan.request_id, MONDAY + 2 * MINUTE, Outcome.RETRY, 429, "Airwallex asked this app to wait",
                 MONDAY + 2 * MINUTE + timedelta(seconds=30))
    took = Try(plan.request_id, MONDAY + 3 * MINUTE, Outcome.TAKEN, 200, "Airwallex took it", None)
    db.put_try(waited)
    db.put_try(took)
    assert db.tries(plan.request_id) == [waited, took] and db.tries("another") == []
    with pytest.raises(TypeError):
        db.put_try(replace(took, outcome="taken"))


def test_a_saved_request_that_wont_load_is_never_sent(tmp_path):
    db = database(tmp_path)
    raw(db, "INSERT INTO posts (request_id, dispute_id, round_number, path, body, saved_at) VALUES "
            "('req', 'dst_ear_tips', 1, '/api/v1/pa/payment_disputes/dst_ear_tips/accept', '{\"request_id\": \"req\"}', ?)",
        T0.isoformat())
    with pytest.raises(Unloadable, match="saved request for this response can't be read"):
        db.post_for("req")


def test_uploads(tmp_path):
    db = database(tmp_path)
    upload = Upload("req", FIRST_PACKAGE.sha256, "dst_headphones", 1, FILE_ID, FIRST_PACKAGE.filename, "b" * 64, 1500,
                    MONDAY)
    assert db.upload_for("req", FIRST_PACKAGE.sha256) is None
    db.put_upload(upload)
    assert db.upload_for("req", FIRST_PACKAGE.sha256) == replace(upload, uploaded_at=MONDAY.astimezone(timezone.utc))
    with pytest.raises(ValueError, match="already on file"):
        db.put_upload(replace(upload, file_id="another"))  # a file uploaded for a plan is never uploaded again
    db.put_upload(replace(upload, sha256="c" * 64))  # another file of the plan
    db.put_upload(replace(upload, request_id="another plan"))
    assert rows(db, "uploads") == 3
    with pytest.raises(TypeError):
        db.put_upload(replace(upload, sha256="d" * 64, size="1500"))


def test_times_are_kept_in_utc(tmp_path):
    db = database(tmp_path)
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY)
    db.save(story["ear_tips"], None, "poll")
    assert MONDAY.utcoffset() == timedelta(hours=-8)
    pass_id = db.begin_pass(MONDAY)
    db.end_pass(pass_id, MONDAY + MINUTE, [], [], {})
    started = db.passes()[0].started_at
    assert started == MONDAY and started.utcoffset() == timedelta(0)
    with pytest.raises(ValueError, match="time zone"):
        db.begin_pass(datetime(2026, 11, 2, 9, 0))


# ----------------------------------------------------------------------------- passes, scope and meta
def test_passes(tmp_path):
    db = database(tmp_path)
    story = saved_story(db)
    alerts = approvals.alerts(story.records.values(), MONDAY)
    assert [alert.dispute_id for alert in alerts] == ["dst_ear_tips"]
    first = db.begin_pass(T0)
    db.end_pass(first, T0 + MINUTE, ["Order #1121: a line.", "Another \ud800 line."], alerts, {"read": 3, "sent": 0})
    with pytest.raises(ValueError, match="already ended"):
        db.end_pass(first, T0 + MINUTE, [], [], {})
    with pytest.raises(ValueError, match="never began"):
        db.end_pass(99, T0 + MINUTE, [], [], {})
    with pytest.raises(TypeError):
        db.end_pass(db.begin_pass(T0), T0, [3], [], {})
    running = db.begin_pass(T0 + 2 * MINUTE)  # a pass that hasn't ended isn't the last pass yet
    row = db.passes()[-1]
    assert (row.id, row.started_at, row.finished_at) == (first, T0, T0 + MINUTE)
    assert row.problems == ("Order #1121: a line.", "Another \ud800 line.") and row.alerts == alerts
    assert dict(row.summary) == {"read": 3, "sent": 0}
    assert [found.id for found in db.passes()] == [running, running - 1, first] and len(db.passes(limit=1)) == 1
    snapshot = db.snapshot()
    assert snapshot.last_pass == row and snapshot.problems == row.problems
    raw(db, "UPDATE passes SET problems = 'not json' WHERE id = ?", first)
    snapshot = db.snapshot()
    assert (snapshot.last_pass, snapshot.problems) == (None, (PASS_UNREADABLE,))
    with pytest.raises(Unloadable):
        db.passes()


def test_scope(tmp_path):
    db = database(tmp_path)
    payment = StorePayment("int_ear_tips", "1121", "FH-1121-a1b2c3d4-1b94", MONDAY)
    db.put_store_payment(payment)
    db.put_store_payment(payment)  # the same row again changes nothing
    with pytest.raises(ValueError, match="Another store_payments row"):
        db.put_store_payment(replace(payment, order_number="1098"))
    db.put_store_payment(StorePayment("int_earbuds", "1098", "FH-1098-a1b2c3d4-4398", MONDAY))
    assert [found.payment_id for found in db.store_payments()] == ["int_ear_tips", "int_earbuds"]
    assert db.store_payments()[1].created_at == MONDAY
    opened = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    db.put_issuer_date(IssuerDate("dst_ear_tips", "ear_tips", opened))
    assert db.opened() == {"dst_ear_tips": opened} and db.scope_problems() == ()
    raw(db, "INSERT INTO store_payments VALUES ('int_odd', '1', 'FH-1', 'not a time')")
    raw(db, "INSERT INTO issuer_dates VALUES ('dst_odd', 'odd', '2026-10-27T08:00:00')")  # no time zone
    assert len(db.store_payments()) == 2 and set(db.opened()) == {"dst_ear_tips"}  # the odd rows are left out
    assert db.scope_problems() == (SCOPE_ROW,) == ("The run's saved list of payments has a row this app can't read.",)
    with pytest.raises(TypeError):
        db.put_store_payment(replace(payment, payment_id=""))


def test_meta(tmp_path):
    db = database(tmp_path)
    for key, value in (("mode", "record"), ("run", "a1b2c3d4"), ("store", "ending_2"), ("ending", "2")):
        db.set_meta(key, value)
        assert db.meta(key) == value
    assert db.meta("nothing") is None and dict(db.snapshot().meta) == {
        "created_at": T0.isoformat(), "mode": "record", "run": "a1b2c3d4", "store": "ending_2", "ending": "2"}
    for key, value in (("created_at", T0.isoformat()), ("approver", "Priya"), ("mode", "replay"), ("store", "suite"),
                       ("run", ""), ("ending", 2)):
        with pytest.raises(ValueError):
            db.set_meta(key, value)


# ----------------------------------------------------------------------------- a page's snapshot
def test_a_snapshot_holds_every_case_approval_and_try(tmp_path):
    db = database(tmp_path)
    story, record, version, post = headphones_sent(db)
    snapshot = db.snapshot()
    assert set(snapshot.records) == set(snapshot.versions) == {"dst_ear_tips", "dst_earbuds", "dst_headphones"}
    assert json_of(snapshot.records)["dst_headphones"] == to_json(record) and snapshot.versions["dst_headphones"] == version
    assert dict(snapshot.broken) == {} and snapshot.last_pass is None and snapshot.problems == ()
    assert snapshot.approvals["dst_headphones"] == (db.latest_approval("dst_headphones", 1),)
    assert set(snapshot.approvals) == {"dst_headphones"}
    assert snapshot.tries == {"dst_headphones": (Try(post.request_id, MONDAY + MINUTE, Outcome.TAKEN, 200,
                                                     "Airwallex took it", None),)}
    with pytest.raises(TypeError):
        snapshot.records["dst_x"] = record  # a page can't change it
    assert isinstance(snapshot.saved, FrozenSaved)


def test_a_snapshot_holds_no_file_id_and_no_post_body(tmp_path):
    db = database(tmp_path)
    _, _, _, post = headphones_sent(db)
    assert FILE_ID in post.body and privacy.account_id_of(FILE_ID) == ACCOUNT_ID  # what the database itself keeps
    snapshot = db.snapshot()
    shown = repr(snapshot) + repr(vars(snapshot.saved))
    assert post.body not in shown and FILE_ID not in shown and ACCOUNT_ID not in shown
    assert not [token for token in privacy.base64_tokens(shown)
                if privacy.SEPARATOR in (privacy.decode_base64(token) or "")]
    assert "supporting_documents" not in shown and "file_ids" not in shown


def test_a_row_that_wont_load_names_its_case_in_broken(tmp_path):
    db = database(tmp_path)
    story, sent, _, post = headphones_sent(db)
    raw(db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
            "('dst_earbuds', 1, '', '{broken', ?)", T0.isoformat())
    raw(db, "INSERT INTO tries (request_id, at, outcome, status, detail) VALUES (?, 'not a time', 'taken', 200, '')",
        post.request_id)
    raw(db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
            "('dst_headphones', 1, '', '[]', ?)", T0.isoformat())
    snapshot = db.snapshot()
    assert dict(snapshot.broken) == {"dst_earbuds": APPROVAL_UNREADABLE,
                                     "dst_headphones": f"{APPROVAL_UNREADABLE} {TRY_UNREADABLE}"}
    assert set(snapshot.records) == set(snapshot.versions) == {"dst_ear_tips"}  # never in both
    assert len(snapshot.approvals["dst_headphones"]) == 1  # the row that loads stays; the broken one is left out
    assert len(snapshot.tries["dst_headphones"]) == 1 and "dst_earbuds" not in snapshot.approvals
    assert APPROVAL_UNREADABLE == "The approval on file can't be read."
    # The pass reads broken as the screens do: such a case is left out of everything, with the same line.
    loaded = db.load()
    assert loaded.broken == dict(snapshot.broken) and set(loaded.records) == set(loaded.versions) == {"dst_ear_tips"}
    assert loaded.versions == dict(snapshot.versions)
    assert to_json(db.load_one("dst_headphones")[0]) == to_json(sent)  # its record still loads, for an answer to apply


def test_a_snapshot_is_one_read_that_a_write_in_progress_never_blocks(tmp_path):
    db = database(tmp_path)
    story = saved_story(db, cases=("ear_tips",))
    writer = db.connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                       "('dst_ear_tips', 3, ?, 'poll', ?)", (to_json(story["ear_tips"]), T0.isoformat()))
        snapshot = db.snapshot()  # WAL: a reader never waits for the writer
        assert snapshot.versions == {"dst_ear_tips": 2}
        writer.execute("COMMIT")
    finally:
        writer.close()
    assert db.snapshot().versions == {"dst_ear_tips": 3} and snapshot.versions == {"dst_ear_tips": 2}


# ----------------------------------------------------------------------------- one pass at a time
def test_one_pass_at_a_time_in_one_process(tmp_path):
    lock, other = PassLock(tmp_path / "data" / "pass.lock"), PassLock(tmp_path / "data" / "pass.lock")
    assert lock.acquire() and lock.held()
    assert not lock.acquire()  # a second pass in this thread
    assert not other.acquire()  # another lock on the same file: the OS lock holds
    started = []
    thread = threading.Thread(target=lambda: started.append(lock.acquire()))
    thread.start()
    thread.join()
    assert started == [False]  # another thread doesn't wait, it skips
    lock.release()
    assert not lock.held() and other.acquire()
    other.release()
    with pytest.raises(RuntimeError, match="isn't held"):
        other.release()
    assert lock.acquire()
    lock.release()


CHILD = """
import json, os, sys
from pathlib import Path
from disputable.db import PassLock
lock = PassLock(Path(sys.argv[1]))
held = lock.acquire()
loaded = sorted(name for name in sys.modules if name.split(".")[0] in ("anthropic", "disputable"))
print(json.dumps({"held": held, "modules": loaded}), flush=True)
if sys.stdin.readline().strip() == "die":
    os._exit(3)  # it ends holding the lock, as a crash would
lock.release()
print("released", flush=True)
sys.stdin.readline()
"""


def child_holding(path):
    return subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def line_from(process, timeout=120):
    found: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: found.put(process.stdout.readline()), daemon=True).start()
    try:
        return found.get(timeout=timeout).strip()
    except queue.Empty:
        process.kill()
        pytest.fail("The child process didn't answer.")


def test_one_pass_at_a_time_across_processes(tmp_path):
    path = tmp_path / "pass.lock"
    child = child_holding(path)
    started = [child]
    try:
        held = json.loads(line_from(child) or "{}")
        assert held.get("held") is True, child.stderr.read() if child.poll() is not None else held
        # The child imported only disputable.db: no Claude layer and no SDK came with it.
        assert "disputable.db" in held["modules"]
        assert not [name for name in held["modules"] if name.split(".")[0] == "anthropic"
                    or name in ("disputable.reader", "disputable.rounds", "disputable.prompts", "disputable.seed")]
        lock = PassLock(path)
        assert not lock.acquire()  # this process's pass doesn't start
        child.stdin.write("\n")
        child.stdin.flush()
        assert line_from(child) == "released"
        assert lock.acquire()
        lock.release()
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=60) == 0
        # A process that dies holding the lock never leaves the folder locked: the OS frees it. (On Windows the venv's
        # python.exe is a launcher, so the child ends itself rather than being killed, and the OS may take a moment.)
        dying = child_holding(path)
        started.append(dying)
        assert json.loads(line_from(dying))["held"] is True
        assert not lock.acquire()
        dying.stdin.write("die\n")
        dying.stdin.flush()
        assert dying.wait(timeout=60) == 3
        deadline = time.monotonic() + 10
        while not lock.acquire():
            assert time.monotonic() < deadline, "The lock stayed held after its process ended."
            time.sleep(0.1)
        lock.release()
    finally:
        for process in started:
            process.stdin.close()  # end of input: a child still waiting ends on its own
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
            process.stdout.close()
            process.stderr.close()


# ----------------------------------------------------------------------------- the modules and the seed
@pytest.mark.parametrize("module", ["db", "clock", "airwallex"])
def test_the_storage_and_client_modules_never_import_the_seed_or_the_screens(module):
    tree = ast.parse(Path(importlib.util.find_spec(f"disputable.{module}").origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
              for alias in node.names}
    assert names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web",
                   "disputable.loop", "disputable.service"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], (module, banned)


def test_amounts_survive_the_round_trip(tmp_path):
    db = database(tmp_path)
    story = Story(cases=("headphones",))
    story.poll(MONDAY)
    db.save(story["headphones"], None, "poll")
    loaded = db.load_one("dst_headphones")[0]
    assert loaded.last.dispute.amount == Decimal("1249") and to_json(loaded) == to_json(story["headphones"])
    assert to_json(from_json(to_json(loaded))) == to_json(loaded)
