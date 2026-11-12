"""The send step: blocker() and verify() before every try, the files built again before the upload and never after the
first try, the body saved with sending() before the POST, a moment of its own for the POST, one POST, and its answer
saved, then applied. A saved answer is applied, never sent again. Every crash point recovers on the next pass.

Through the loop on the fake sandbox, with test_loop's runs. A crash is an exception no catch-all takes, followed by a
new Service on the same file, as a restart makes it.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from disputable import airwallex, approvals, lifecycle, rounds
from disputable.airwallex import DISPUTES, LOGIN, UPLOAD, Outcome
from disputable.db import APPROVAL_UNREADABLE, Database
from disputable.db import TRY_UNREADABLE as TRY_ROW
from disputable.desk import Result
from disputable.lifecycle import HELD, Phase, phase
from disputable.loop import (APPLIED, BODY_MISSING, BODY_UNREADABLE, BROKEN, CANT_BUILD, CONFLICT, DROPPED, NO_ANSWER,
                             NOT_AT_SENT, NOT_SENT, SEND_ERROR, TRY_UNREADABLE, UPLOAD_FAILED, WAIT)
from disputable.seed.fernhill import FERNHILL, FIRST_PACKAGE_FILE
from disputable.service import Service
from fake_airwallex import NOT_SUPPORTED
from test_loop import INFO, MINUTE, Clock, at_step, query
from test_rounds import LATER_DELIVERY, edited

EAR_TIPS, EARBUDS = f"{DISPUTES}/dst_ear_tips/accept", f"{DISPUTES}/dst_earbuds/accept"
CHALLENGE = f"{DISPUTES}/dst_headphones/challenge"
REFUSED = {"code": "validation_error", "message": NOT_SUPPORTED}
REFUSAL = "HTTP 400, validation_error: Dispute transition is not supported"
STALE = "The next pass reads the dispute again before it sends: the last reading is more than 10 minutes old."
NOT_NOW = {"code": "invalid_status_for_operation", "message": "The dispute's status doesn't allow this."}  # Nov 7's repeat
NOT_NOW_WORDS = "HTTP 400, invalid_status_for_operation: The dispute's status doesn't allow this"


class Crash(BaseException):
    """The process stops here. Not an Exception, so no catch-all in the loop takes it."""


def crash_after(method, when=lambda *args: True):
    """method, then a crash, when when(its arguments) says so: what it saved stays saved."""
    def crashing(*args, **options):
        result = method(*args, **options)
        if when(*args):
            raise Crash()
        return result
    return crashing


def request_id(run, dispute_id="dst_headphones"):
    return run.record(dispute_id).rounds[-1].plan.request_id


def posts(run, path):
    return [request for request in run.fake.posts() if request.url.path == path]


def lag(run, dispute_id, before):
    """The next GETs of the dispute show it as it was before the POST, though dated after it: Airwallex's read lags."""
    landed = run.fake.sandbox.disputes[dispute_id]
    run.fake.sandbox.disputes[dispute_id] = {**copy.deepcopy(before), "updated_at": landed["updated_at"]}
    return landed


# ----------------------------------------------------------------------------- the guards before every try
def test_nothing_goes_out_without_both(tmp_path, monkeypatch):
    run = at_step(tmp_path, "approved")
    log = []
    blocker, verify = lifecycle.blocker, approvals.verify
    monkeypatch.setattr(lifecycle, "blocker", lambda record, now: log.append(("blocker", record.dispute_id))
                        or blocker(record, now))
    monkeypatch.setattr(approvals, "verify", lambda record, row: log.append(("verify", record.dispute_id))
                        or verify(record, row))
    run.fake.hooks.append(lambda request: log.append(("POST", request.url.path.split("/")[-2]))
                          if request.url.path.endswith(("/accept", "/challenge")) else None)
    run.fake.fail("POST", CHALLENGE, status=503)  # the first try gets no answer, so a second one goes
    run.pass_(2 * MINUTE)
    run.pass_(MINUTE)
    tries = [entry for entry in log if entry[0] == "POST"]
    assert tries == [("POST", "dst_ear_tips"), ("POST", "dst_earbuds"), ("POST", "dst_headphones"),
                     ("POST", "dst_headphones")]
    for at, (kind, dispute_id) in enumerate(log):
        if kind == "POST":  # since the case's try before, blocker() then verify(), before this one
            before = [index for index, entry in enumerate(log[:at]) if entry == ("POST", dispute_id)]
            window = log[(before[-1] + 1 if before else 0):at]
            assert window.index(("blocker", dispute_id)) < window.index(("verify", dispute_id))
    # Either one alone keeps the POST in: an old reading, then an approval that no longer matches.
    old = at_step(tmp_path / "old", "approved")
    for case in ("ear_tips", "earbuds", "headphones"):
        old.fake.fail("GET", f"{DISPUTES}/dst_{case}", timeout=True, times=2)
    assert old.pass_(6 * MINUTE).sent == () and not old.fake.posts()  # the last reading is 11 minutes old
    unmatched = at_step(tmp_path / "unmatched", "approved")
    monkeypatch.setattr(approvals, "verify", lambda record, row: "The plan changed after it was approved."
                        if lifecycle.needs_approval(record.rounds[-1].plan) else None)
    report = unmatched.pass_(2 * MINUTE)
    assert report.dropped == ("dst_earbuds", "dst_headphones") and not unmatched.fake.posts("/challenge")
    assert [request.url.path for request in unmatched.fake.posts()] == [EAR_TIPS]  # code's own accept needs no row


def test_a_missing_approval_drops_the_plan(tmp_path):
    run = at_step(tmp_path, "monday")
    record, version = run.db.load_one("dst_earbuds")
    run.wall.move(5 * MINUTE)
    unfiled = lifecycle.approve(record, record.rounds[-1].plan, "Priya", run.wall.at)  # approved, with no row on file
    run.db.save(unfiled, version, "approve")
    report = run.pass_(2 * MINUTE)
    assert DROPPED.format(who="Order #1098", why="No approval is on file for this plan.") in report.problems
    assert DROPPED.format(who="Order #1098", why="No approval is on file for this plan.") == (
        "Order #1098: No approval is on file for this plan. The plan was dropped. Code decides the round again, and a "
        "person approves the new plan.")
    assert report.dropped == ("dst_earbuds",) and not posts(run, EARBUDS)
    earbuds = run.record("dst_earbuds")  # decided again in the same pass, from the saved reading: no new read
    assert phase(earbuds) is Phase.APPROVE and run.reader.requests == [] and "dst_earbuds" in report.decided
    assert "The plan was dropped: no approval on file matched it." in earbuds.rounds[-1].notes
    labels = run.clock.labels()
    assert labels.index("send:dst_earbuds") < labels.index("decide:dst_earbuds")
    assert run.approve_all()["dst_earbuds"] == Result(True, "approved")  # a person approves the new plan
    assert dict(run.pass_(MINUTE).sent)["dst_earbuds"] is Outcome.TAKEN


def test_never_dropped_at_sent(tmp_path):
    run = at_step(tmp_path, "approved")
    run.fake.fail("POST", CHALLENGE, status=503)
    run.pass_(2 * MINUTE)
    assert phase(run.record("dst_headphones")) is Phase.SENT

    def hand_edit():  # while the pass runs, after it loaded the case: the newest approval won't load
        query(run.db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
                      "('dst_headphones', 1, 'x', '{', ?)", run.wall.at.isoformat())

    run.clock.after["send:dst_headphones"] = hand_edit
    line = NOT_AT_SENT.format(who="Order #1109", why="The approval on file can't be read.")
    report = run.pass_(MINUTE)
    assert line in report.problems and report.dropped == ()
    assert line == ("Order #1109: The approval on file can't be read. The response may already be with Airwallex, so it "
                    "isn't sent again. A person checks it.")
    report = run.pass_(MINUTE)  # from now on the case is broken, as the screens show it: never dropped, never sent
    assert BROKEN.format(id="dst_headphones", why=APPROVAL_UNREADABLE) in report.problems and report.dropped == ()
    headphones = run.record("dst_headphones")
    assert phase(headphones) is Phase.SENT and headphones.rounds[-1].plan.sending_at is not None
    assert len(posts(run, CHALLENGE)) == 1


def test_moved_files_drop_the_plan(tmp_path):
    run = at_step(tmp_path, "approved")
    run.service.store = moved = edited(FERNHILL, LATER_DELIVERY)  # the delivery scan changed after Priya approved
    report = run.pass_(2 * MINUTE)
    assert DROPPED.format(who="Order #1109", why=rounds.MOVED) in report.problems and report.dropped == ("dst_headphones",)
    assert not run.fake.posts(UPLOAD) and not posts(run, CHALLENGE)  # files_for() ran before any upload
    headphones = run.record("dst_headphones")
    assert "The plan was dropped: its file couldn't be built again exactly as planned." in headphones.rounds[-1].notes
    assert phase(headphones) is Phase.APPROVE  # decided again on today's records: a new file, a new approval
    assert headphones.rounds[-1].plan.terms.file_hashes != {FIRST_PACKAGE_FILE}
    assert run.approve_all() == {"dst_headphones": Result(True, "approved")}
    assert dict(run.pass_(MINUTE).sent) == {"dst_headphones": Outcome.TAKEN}
    (upload,) = run.fake.files.values()
    assert upload["filename"] == rounds.files_for(headphones, moved, run.db.saved)[0].filename


def test_held_is_never_sent_again(tmp_path):
    run = at_step(tmp_path, "approved")
    before = copy.deepcopy(run.fake.sandbox.disputes["dst_ear_tips"])
    run.fake.fail("POST", EAR_TIPS, lost=True)  # applied: the $12 refund went out, but the answer was lost
    report = run.pass_(2 * MINUTE)
    assert dict(report.sent)["dst_ear_tips"] is Outcome.RETRY
    assert NO_ANSWER.format(who="Order #1121", detail="no answer in 30 seconds") in report.problems
    lag(run, "dst_ear_tips", before)  # the dispute still asks, but the payment already shows the refund
    for _ in range(3):
        run.pass_(MINUTE)
    ear_tips = run.record("dst_ear_tips")
    assert HELD in ear_tips.rounds[-1].notes and phase(ear_tips) is Phase.SENT
    assert lifecycle.blocker(ear_tips, run.wall.at) == "$12.00 would take refunds past the $12.00 captured."  # held
    assert len(posts(run, EAR_TIPS)) == 1


# ----------------------------------------------------------------------------- the tries and their answers
def test_a_retry_sends_the_saved_bytes(tmp_path):
    run = at_step(tmp_path, "approved")
    run.fake.fail("POST", CHALLENGE, status=503)
    first = run.pass_(2 * MINUTE)
    assert dict(first.sent)["dst_headphones"] is Outcome.RETRY and phase(run.record("dst_headphones")) is Phase.SENT
    assert NO_ANSWER.format(who="Order #1109", detail="HTTP 503") == (
        "Order #1109: Airwallex didn't answer the response (HTTP 503). The same request goes again after the next read.")
    assert NO_ANSWER.format(who="Order #1109", detail="HTTP 503") in first.problems
    run.fake.fail("GET", f"{DISPUTES}/dst_headphones", timeout=True, times=2)  # no fresh read: no retry
    assert "dst_headphones" not in dict(run.pass_(MINUTE).sent) and len(posts(run, CHALLENGE)) == 1
    again = run.pass_(MINUTE)  # read again, then the same bytes with the same request id
    assert dict(again.sent)["dst_headphones"] is Outcome.TAKEN
    one, two = posts(run, CHALLENGE)
    rid = request_id(run)
    assert one.content == two.content == run.db.post_for(rid).body.encode("ascii")
    assert json.loads(two.content)["request_id"] == rid and len(run.fake.posts(UPLOAD)) == 1
    assert [attempt.outcome for attempt in run.db.tries(rid)] == [Outcome.RETRY, Outcome.TAKEN]


def test_a_lost_answer_sends_once(tmp_path):
    run = at_step(tmp_path, "approved")
    before = copy.deepcopy(run.fake.sandbox.disputes["dst_earbuds"])
    for path in (EAR_TIPS, EARBUDS):
        run.fake.fail("POST", path, lost=True)  # both applied, both answers lost
    first = run.pass_(2 * MINUTE)
    assert dict(first.sent) == {"dst_ear_tips": Outcome.RETRY, "dst_earbuds": Outcome.RETRY,
                                "dst_headphones": Outcome.TAKEN}
    landed = lag(run, "dst_earbuds", before)  # the earbuds' read lags; the ear tips' shows the accept
    second = run.pass_(MINUTE)
    assert phase(run.record("dst_ear_tips")) is Phase.CLOSED and len(posts(run, EAR_TIPS)) == 1  # never sent again
    assert dict(second.sent) == {"dst_earbuds": Outcome.TAKEN}  # the same request again: Airwallex already had it
    (_, retried) = run.db.tries(request_id(run, "dst_earbuds"))
    assert (retried.outcome, retried.status, retried.detail) == (Outcome.TAKEN, 400, "Airwallex already had this request")
    run.fake.sandbox.disputes["dst_earbuds"] = landed  # the read catches up
    run.pass_(MINUTE)
    assert phase(run.record("dst_earbuds")) is Phase.CLOSED and len(landed["accept_details"]) == 1  # one landed POST
    assert len(posts(run, EARBUDS)) == 2


def test_a_refusal_goes_to_a_person(tmp_path):
    run = at_step(tmp_path, "approved")
    run.fake.fail("POST", CHALLENGE, status=400, body=REFUSED)
    assert dict(run.pass_(2 * MINUTE).sent)["dst_headphones"] is Outcome.FAILED
    for _ in range(2):
        run.pass_(MINUTE)
    headphones = run.record("dst_headphones")
    assert phase(headphones) is Phase.PERSON and len(posts(run, CHALLENGE)) == 1  # never sent again
    assert headphones.rounds[-1].person == f"Airwallex refused the response: {REFUSAL}. A person handles this round."
    plan = headphones.rounds[-1].plan  # the plan stays on the round, and its file stays for a person
    assert plan.refused == f"{REFUSAL}." and plan.sending_at is not None
    step = rounds.current(headphones, FERNHILL, run.db.saved, run.wall.at)
    assert [document.sha256 for document in step.basis.files] == [FIRST_PACKAGE_FILE]


@pytest.mark.parametrize("given, date, wait", [
    (30, None, 30),
    (None, None, 30),  # a 429 with no Retry-After: the longest wait this app keeps
    ("soon", None, 30),  # one it can't read
    ("Mon, 02 Nov 2026 17:07:25 GMT", "Mon, 02 Nov 2026 17:07:00 GMT", 25),  # an HTTP date, against the answer's Date
    ("Mon, 02 Nov 2026 17:07:25 GMT", None, 30),  # an HTTP date with no Date to read it against
], ids=["seconds", "none", "unreadable", "http date", "http date, no Date"])
def test_retry_after(tmp_path, given, date, wait):
    run = at_step(tmp_path, "approved")  # 17:05 UTC
    run.fake.fail("POST", EAR_TIPS, status=429, retry_after=given, headers={"Date": date} if date else None)
    first = run.pass_(2 * MINUTE)
    (attempt,) = run.db.tries(request_id(run, "dst_ear_tips"))
    assert attempt.outcome is Outcome.RETRY and attempt.not_before == attempt.at + timedelta(seconds=wait)
    assert not [line for line in first.problems if "didn't answer" in line]  # Airwallex did answer: it asked to wait
    line = WAIT.format(who="Order #1121", time="17:07")
    assert line == "Order #1121: Airwallex asked this app to wait. The same request goes again after 17:07 UTC."
    assert line in first.problems
    early = run.pass_(timedelta(seconds=20))  # read again, but before the time Airwallex asked for
    assert "dst_ear_tips" not in dict(early.sent) and line in early.problems and len(posts(run, EAR_TIPS)) == 1
    later = run.pass_(timedelta(seconds=20))
    assert dict(later.sent)["dst_ear_tips"] is Outcome.TAKEN and len(posts(run, EAR_TIPS)) == 2


def test_a_429_on_the_login_a_response_needs_waits_the_same(tmp_path):
    run = at_step(tmp_path, "monday")  # code's own accept of the ear tips is ready: 17:00 UTC
    expired = []

    def expire_at_the_post(request):  # the token expires at the POST, and the new login is asked to wait, twice
        if request.url.path == EAR_TIPS and not expired:
            expired.append(True)
            run.fake.expire_tokens()
            run.fake.fail("POST", LOGIN, status=429, times=2)

    run.fake.hooks.append(expire_at_the_post)
    first = run.pass_(2 * MINUTE)
    (attempt,) = run.db.tries(request_id(run, "dst_ear_tips"))
    assert (attempt.outcome, attempt.status) == (Outcome.RETRY, 429)
    assert attempt.detail == "this app couldn't log in to Airwallex (Airwallex asked this app to wait)"
    assert attempt.not_before == attempt.at + timedelta(seconds=30)  # no Retry-After: the longest wait this app keeps
    line = WAIT.format(who="Order #1121", time="17:02")
    assert line in first.problems and not [said for said in first.problems if "didn't answer" in said]
    early = run.pass_(timedelta(seconds=20))  # read again, but before the time Airwallex asked for
    assert "dst_ear_tips" not in dict(early.sent) and line in early.problems and len(posts(run, EAR_TIPS)) == 1
    later = run.pass_(timedelta(seconds=20))
    assert dict(later.sent)["dst_ear_tips"] is Outcome.TAKEN and len(posts(run, EAR_TIPS)) == 2


@pytest.mark.parametrize("fault, outcome, detail", [
    ({}, Outcome.TAKEN, "Airwallex took it"),
    ({"status": 400, "body": {"code": "duplicate_request", "message": "The request_id was used before."}},
     Outcome.TAKEN, "Airwallex already had this request"),
    ({"status": 400, "body": REFUSED}, Outcome.FAILED, REFUSAL),
    ({"status": 403}, Outcome.FAILED, "HTTP 403, forbidden: The fake sandbox answered 403"),
    ({"status": 408}, Outcome.FAILED, "HTTP 408, request_timeout: The fake sandbox answered 408"),  # a 4xx (S3)
    ({"status": 429, "retry_after": 5}, Outcome.RETRY, "Airwallex asked this app to wait"),
    ({"status": 503}, Outcome.RETRY, "HTTP 503"),
    ({"timeout": True}, Outcome.RETRY, "no answer in 30 seconds"),
    ({"connect": True}, Outcome.RETRY, "the connection failed"),
    ({"lost": True}, Outcome.RETRY, "no answer in 30 seconds"),
], ids=["200", "duplicate_request", "400", "403", "408", "429", "503", "timeout", "connection", "lost"])
def test_outcomes(tmp_path, fault, outcome, detail):
    run = at_step(tmp_path, "monday")  # code's own accept of the ear tips is ready
    if fault:
        run.fake.fail("POST", EAR_TIPS, **fault)
    report = run.pass_(2 * MINUTE)
    assert report.sent == (("dst_ear_tips", outcome),)
    (attempt,) = run.db.tries(request_id(run, "dst_ear_tips"))
    assert (attempt.outcome, attempt.detail) == (outcome, detail)
    plan = run.record("dst_ear_tips").rounds[-1].plan
    if outcome is Outcome.TAKEN:
        assert plan.taken_at == attempt.at and plan.refused is None
    elif outcome is Outcome.FAILED:
        assert run.record("dst_ear_tips").rounds[-1].person == (f"Airwallex refused the response: {detail}. A person "
                                                                "handles this round.")
    else:
        assert phase(run.record("dst_ear_tips")) is Phase.SENT and plan.taken_at is None and plan.refused is None
        assert [line for line in report.problems if line.startswith("Order #1121: Airwallex ")]


def test_the_retry_wording(tmp_path):
    run = at_step(tmp_path, "monday")
    run.fake.fail("POST", EAR_TIPS, status=503)
    run.pass_(2 * MINUTE)  # the first try gets no answer: it may have landed
    run.fake.fail("POST", EAR_TIPS, status=400, body=REFUSED)
    assert run.pass_(MINUTE).sent == (("dst_ear_tips", Outcome.FAILED),)
    first, retried = run.db.tries(request_id(run, "dst_ear_tips"))
    assert retried.detail == f"Airwallex refused the retry: {REFUSAL}. The first try may have landed"
    person = run.record("dst_ear_tips").rounds[-1].person
    assert person == (f"Airwallex refused the response: {REFUSAL}. That was a retry, and the first try may have landed. "
                      "A person checks what Airwallex has. A person handles this round.")
    assert person.count("refused") == 1


@pytest.mark.parametrize("stop", ["crash", "ctrl-c", "before the post"])
def test_a_post_whose_answer_was_lost_with_the_process_goes_again_as_a_retry(tmp_path, stop):
    """A case saved at SENT with no try on file: a POST may be with Airwallex. Its repeat is a retry, so a refusal says
    the first try may have landed (S3). Where no POST went out, that errs on the safe side: this app can't tell."""
    run = at_step(tmp_path, "approved")
    rid = request_id(run)
    before = copy.deepcopy(run.fake.sandbox.disputes["dst_headphones"])
    if stop == "crash":  # the POST was applied, and its answer was lost with the process
        put_try = run.db.put_try

        def lost(attempt):
            if attempt.request_id == rid:
                raise Crash()
            return put_try(attempt)

        run.db.put_try = lost
    elif stop == "ctrl-c":  # Ctrl+C, how the loop is stopped, while the answer is on its way
        handle = run.fake.handle

        def interrupted(request):
            answer = handle(request)
            if request.url.path == CHALLENGE:
                raise KeyboardInterrupt()
            return answer

        run.fake.handle = interrupted
        run.restart()
    else:  # the body and the SENT record are saved, and no POST went out
        run.db.start_send = crash_after(run.db.start_send, lambda record, version, post: post.request_id == rid)
    with pytest.raises((Crash, KeyboardInterrupt)):
        run.pass_(2 * MINUTE)
    vars(run.fake).pop("handle", None)
    assert run.db.tries(rid) == [] and phase(run.record("dst_headphones")) is Phase.SENT
    assert len(posts(run, CHALLENGE)) == (0 if stop == "before the post" else 1)
    if stop != "before the post":
        lag(run, "dst_headphones", before)  # the next read doesn't show the challenge yet
    run.fake.fail("POST", CHALLENGE, status=400, body=NOT_NOW)  # how the sandbox answered a repeat on Nov 7
    run.restart()
    assert dict(run.pass_(MINUTE).sent) == {"dst_headphones": Outcome.FAILED}
    (attempt,) = run.db.tries(rid)
    assert attempt.detail == f"Airwallex refused the retry: {NOT_NOW_WORDS}. The first try may have landed"
    assert run.record("dst_headphones").rounds[-1].person == (
        f"Airwallex refused the response: {NOT_NOW_WORDS}. That was a retry, and the first try may have landed. A person "
        "checks what Airwallex has. A person handles this round.")


# ----------------------------------------------------------------------------- the moments and the uploads
def test_a_slow_upload_takes_a_new_moment_before_the_post(tmp_path):
    run = at_step(tmp_path, "approved")

    def slow(request):  # the upload takes 11 minutes of the run's clock
        if request.url.path == UPLOAD:
            run.wall.move(timedelta(minutes=11))

    run.fake.hooks.append(slow)
    report = run.pass_(2 * MINUTE)
    run.fake.hooks.remove(slow)
    assert dict(report.sent) == {"dst_ear_tips": Outcome.TAKEN, "dst_earbuds": Outcome.TAKEN}
    line = NOT_SENT.format(who="Order #1109", why=STALE)  # sending() refused with blocker()'s line: the pass's own line
    assert line in report.problems and run.db.passes(limit=1)[0].problems == report.problems
    assert line == ("Order #1109: the response wasn't sent. The next pass reads the dispute again before it sends: the "
                    "last reading is more than 10 minutes old.")
    moments = dict(run.clock.journal)
    assert moments["post:dst_headphones"] - moments["send:dst_headphones"] == timedelta(minutes=11)
    assert phase(run.record("dst_headphones")) is Phase.SEND and not posts(run, CHALLENGE)
    assert run.db.upload_for(request_id(run), FIRST_PACKAGE_FILE) is not None  # kept: it isn't uploaded again
    assert dict(run.pass_(MINUTE).sent) == {"dst_headphones": Outcome.TAKEN} and len(run.fake.posts(UPLOAD)) == 1


@pytest.mark.parametrize("fault, why", [
    ({"status": 503}, "HTTP 503"),
    ({"status": 201, "body": {"fileId": "QUJD" * 20, "object_type": "file"}}, "an answer this app can't read"),
    ({"lost": True}, "no answer in 30 seconds"),  # Airwallex kept the file, and the answer never came
], ids=["503", "201 with no file id", "lost"])
def test_an_upload_that_fails_is_tried_again(tmp_path, fault, why):
    run = at_step(tmp_path, "approved")
    run.fake.fail("POST", UPLOAD, **fault)
    report = run.pass_(2 * MINUTE)
    assert UPLOAD_FAILED.format(who="Order #1109", why=why) in report.problems
    assert UPLOAD_FAILED.format(who="Order #1109", why="HTTP 503") == (
        "Order #1109: the evidence file may not have uploaded (HTTP 503). It tries again on the next pass.")
    assert "dst_headphones" not in dict(report.sent) and phase(run.record("dst_headphones")) is Phase.SEND
    assert dict(run.pass_(MINUTE).sent) == {"dst_headphones": Outcome.TAKEN} and len(run.fake.posts(UPLOAD)) == 2


def test_a_drop_saved_during_the_upload_stops_the_post(tmp_path):
    """Only start_send()'s version check, at the version the pass loaded, stands between a screen's drop during the
    upload and the POST. Here the screens' process runs on newer records, so its files_for() fails and the drop is
    allowed while the loop's own upload goes on."""
    run = at_step(tmp_path, "approved")
    clock = Clock(run.wall)
    screens = Service(db=Database(run.folder / "live.sqlite3", stamp=clock.peek), store=edited(FERNHILL, LATER_DELIVERY),
                      clock=clock, info=INFO, approver="Priya")  # serve: no Airwallex client, no reader
    dropped = []

    def drop_during_the_upload(request):  # the case page, opened after the pass saved its poll, drops the plan now
        if request.url.path == UPLOAD and not dropped:
            dropped.append(screens.drop_moved("dst_headphones", screens.board().versions["dst_headphones"]))

    run.fake.hooks.append(drop_during_the_upload)
    report = run.pass_(2 * MINUTE)
    assert dropped == [Result(True, "dropped")] and len(run.fake.posts(UPLOAD)) == 1
    assert not posts(run, CHALLENGE) and "dst_headphones" not in dict(report.sent)  # the person's drop stands
    assert CONFLICT.format(who="Order #1109") in report.problems
    headphones = run.record("dst_headphones")
    assert "The plan was dropped: its file couldn't be built again exactly as planned." in headphones.rounds[-1].notes
    assert "send" not in [why for _, why, _ in run.db.versions("dst_headphones")]
    assert phase(headphones) is Phase.APPROVE  # decided again in the same pass: a person approves the new plan


def test_an_unreadable_try_is_never_sent_again(tmp_path):
    run = at_step(tmp_path, "approved")
    rid = request_id(run)
    before = copy.deepcopy(run.fake.sandbox.disputes["dst_headphones"])
    run.db.put_try = crash_after(run.db.put_try, lambda attempt: attempt.request_id == rid)
    with pytest.raises(Crash):
        run.pass_(2 * MINUTE)  # the challenge landed and its TAKEN answer is saved; the record still says SENT
    lag(run, "dst_headphones", before)  # and the next read doesn't show it yet
    run.restart()

    def hand_edit():  # while the pass runs, after it loaded the case: a try of this request that won't load
        query(run.db, "INSERT INTO tries (request_id, at, outcome, status, detail) VALUES (?, 'not a time', 'taken', "
                      "200, '')", rid)

    run.clock.after["send:dst_headphones"] = hand_edit
    report = run.pass_(MINUTE)
    line = TRY_UNREADABLE.format(who="Order #1109")
    assert line in report.problems and "dst_headphones" not in dict(report.sent)
    assert line == ("Order #1109: a saved try of this response can't be read, so it isn't sent again. A person checks "
                    "what Airwallex has.")
    assert len(posts(run, CHALLENGE)) == 1 and phase(run.record("dst_headphones")) is Phase.SENT
    report = run.pass_(MINUTE)  # from now on the case is broken, as the screens show it: nothing goes out
    assert BROKEN.format(id="dst_headphones", why=TRY_ROW) in report.problems and len(posts(run, CHALLENGE)) == 1


def test_no_rebuild_after_the_post(tmp_path, monkeypatch):
    run = at_step(tmp_path, "approved")
    run.fake.fail("POST", CHALLENGE, status=503)
    run.pass_(2 * MINUTE)  # built, uploaded, and tried once
    monkeypatch.setattr(rounds, "files_for", lambda *args, **options: pytest.fail("files_for() ran after the first try"))
    assert dict(run.pass_(MINUTE).sent) == {"dst_headphones": Outcome.TAKEN}
    first, second = posts(run, CHALLENGE)
    (entry,) = json.loads(second.content)["supporting_documents"]["documents"]
    assert entry["file_ids"] == [run.db.upload_for(request_id(run), FIRST_PACKAGE_FILE).file_id]
    assert len(run.fake.posts(UPLOAD)) == 1 and first.content == second.content


def test_a_body_that_fails_its_check_is_never_sent(tmp_path, monkeypatch):
    run = at_step(tmp_path, "monday")
    monkeypatch.setattr(airwallex, "body_problem", lambda plan, body, files: airwallex.REASON)
    report = run.pass_(2 * MINUTE)
    assert CANT_BUILD.format(who="Order #1121", problem=airwallex.REASON) == (
        "Order #1121: this app couldn't build the request (the reason isn't the plan's, or isn't one Airwallex lists for "
        "this response). Nothing was sent. A person checks it.")
    assert CANT_BUILD.format(who="Order #1121", problem=airwallex.REASON) in report.problems
    assert not run.fake.posts() and phase(run.record("dst_ear_tips")) is Phase.SEND
    assert run.db.post_for(request_id(run, "dst_ear_tips")) is None


def test_an_error_in_one_send_lets_the_others_go(tmp_path, capsys):
    run = at_step(tmp_path, "approved")

    def bug(request):
        if request.url.path == EAR_TIPS and not run.fake.posts("/accept"):
            raise RuntimeError("A bug in the transport.")

    run.fake.hooks.append(bug)
    report = run.pass_(2 * MINUTE)
    assert SEND_ERROR.format(who="Order #1121") in report.problems
    assert SEND_ERROR.format(who="Order #1121") == ("Order #1121: this app hit an error while sending. Nothing more was "
                                                    "sent. It tries again on the next pass.")
    assert dict(report.sent) == {"dst_earbuds": Outcome.TAKEN, "dst_headphones": Outcome.TAKEN}
    assert "A bug in the transport." in capsys.readouterr().err
    assert dict(run.pass_(MINUTE).sent) == {"dst_ear_tips": Outcome.TAKEN}  # the saved body, after a fresh read


# ----------------------------------------------------------------------------- crashes and what is on file
@pytest.mark.parametrize("point", ["upload", "start_send", "post", "put_try"])
def test_crash_points(tmp_path, point):
    run = at_step(tmp_path, "approved")
    rid = request_id(run)
    if point == "upload":  # the file is uploaded and its row saved
        run.db.put_upload = crash_after(run.db.put_upload)
    elif point == "start_send":  # the body and the SENT record are saved, and no POST went out
        run.db.start_send = crash_after(run.db.start_send, lambda record, version, post: post.request_id == rid)
    elif point == "post":  # the POST was applied, and its answer was lost with the process
        def lost(attempt):
            if attempt.request_id == rid:
                raise Crash()
            return put_try(attempt)
        put_try, run.db.put_try = run.db.put_try, lost
    else:  # the answer is saved, the record isn't
        run.db.put_try = crash_after(run.db.put_try, lambda attempt: attempt.request_id == rid)
    with pytest.raises(Crash):
        run.pass_(2 * MINUTE)
    run.restart()  # a new process: everything comes from the file
    report = run.pass_(MINUTE)
    sent = dict(report.sent)
    if point in ("upload", "start_send"):
        assert sent == {"dst_headphones": Outcome.TAKEN}
        assert [attempt.outcome for attempt in run.db.tries(rid)] == [Outcome.TAKEN]
        assert posts(run, CHALLENGE)[0].content == run.db.post_for(rid).body.encode("ascii")
    else:  # the next read shows the challenge: nothing is sent again
        assert sent == {} and phase(run.record("dst_headphones")) is Phase.WAIT
    assert len(posts(run, CHALLENGE)) == len(run.fake.posts(UPLOAD)) == 1
    assert len(run.fake.sandbox.disputes["dst_headphones"]["challenge_details"]) == 1


@pytest.mark.parametrize("answer", ["taken", "failed"])
def test_a_saved_answer_is_applied_not_sent_again(tmp_path, answer):
    run = at_step(tmp_path, "approved")
    rid = request_id(run)
    before = copy.deepcopy(run.fake.sandbox.disputes["dst_headphones"])
    if answer == "failed":
        run.fake.fail("POST", CHALLENGE, status=400, body=REFUSED)
    run.db.put_try = crash_after(run.db.put_try, lambda attempt: attempt.request_id == rid)
    with pytest.raises(Crash):
        run.pass_(2 * MINUTE)  # the answer is on file; the record still says SENT
    lag(run, "dst_headphones", before)  # and the next read doesn't show the challenge yet
    run.restart()
    report = run.pass_(MINUTE)
    assert "dst_headphones" not in dict(report.sent) and len(posts(run, CHALLENGE)) == 1
    assert APPLIED.format(who="Order #1109") in report.problems
    assert APPLIED.format(who="Order #1109") == ("Order #1109: Airwallex's answer to an earlier try is on file, so it's "
                                                 "applied now and nothing is sent again.")
    (attempt,) = run.db.tries(rid)
    headphones = run.record("dst_headphones")
    if answer == "taken":
        assert headphones.rounds[-1].plan.taken_at == attempt.at and phase(headphones) is Phase.SENT
        assert lifecycle.blocker(headphones, run.wall.at) == ("Airwallex took the response. It waits for it to show on "
                                                              "the dispute.")
    else:
        assert phase(headphones) is Phase.PERSON
        assert headphones.rounds[-1].person == f"Airwallex refused the response: {REFUSAL}. A person handles this round."
    run.pass_(MINUTE)
    assert len(posts(run, CHALLENGE)) == 1 and APPLIED.format(who="Order #1109") not in run.board().problems


@pytest.mark.parametrize("loss", ["missing", "unreadable"])
def test_a_lost_body_is_never_rebuilt(tmp_path, loss):
    run = at_step(tmp_path, "approved")
    rid = request_id(run)

    def lose_the_body(record, expected, post):  # the SENT record is saved, its body row isn't, or won't load
        if post.request_id != rid:
            return start_send(record, expected, post)
        run.db.save(record, expected, "send")
        if loss == "unreadable":
            query(run.db, "INSERT INTO posts (request_id, dispute_id, round_number, path, body, saved_at) VALUES "
                          "(?, 'dst_headphones', 1, ?, '{', ?)", rid, CHALLENGE, run.wall.at.isoformat())
        raise Crash()

    start_send, run.db.start_send = run.db.start_send, lose_the_body
    with pytest.raises(Crash):
        run.pass_(2 * MINUTE)
    run.restart()
    line = (BODY_MISSING if loss == "missing" else BODY_UNREADABLE).format(who="Order #1109")
    for _ in range(2):
        assert line in run.pass_(MINUTE).problems
    assert not posts(run, CHALLENGE) and len(run.fake.posts(UPLOAD)) == 1  # nothing rebuilt, nothing sent
    assert phase(run.record("dst_headphones")) is Phase.SENT
    assert BODY_MISSING.format(who="Order #1109") == ("Order #1109: the saved request for this response is missing, so "
                                                      "it isn't sent again. A person checks what Airwallex has.")
