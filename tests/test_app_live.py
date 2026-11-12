"""The web app over the real Service, in a live run: create_app(service) on the database the loop filled from the fake
sandbox with the scripted perfect reader.

Over HTTP, as a browser does it: the approval page's hidden plan and reading go back exactly as the page showed them, and
the row saved holds the plan's SHA-256 and the configured approver's name; every refusal of the approval screen answers
its status and its sentence and saves nothing; two posts of one form that overlap answer approved and already; the form
checks refuse a form from anywhere else, or one with an extra field, before the Service runs; a plan whose file moved,
or whose saved basis or reading won't load, is dropped through its own form, and only a file that can't be built is
named as the reason; a saved row in another form shows a plain sentence; a plan whose file still builds stays, and so
does one whose other checks hit a bug, files_for()'s own lines among them, while a bug in the rules as a plan's file is
built again lets that plan be dropped, and no drop answers an error page; Ending 1 played through the pages is the
loop's own story, the money page shows its numbers, each try to send names its round, round one's used approval says
its plan went out, and Claude's words show only in the collapsed draft; and once a challenge went out on the fake
sandbox, whose file ids carry a made-up account id, no page, error page or PDF holds that id, plain or in base64, or any
key.

Offline: TestClient talks to the app in this process, and the loop's client to tests/fake_airwallex.py. The same pages on
FakeDesk boards are test_web_app's, and the Service's own checks are test_service's.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterator

import pytest
from starlette.testclient import TestClient

from disputable import approvals, documents, privacy, rounds, rules
from disputable.airwallex import DISPUTES, TOOK, Outcome
from disputable.db import APPROVAL_UNREADABLE
from disputable.desk import Board
from disputable.engine import Status
from disputable.jsonable import load
from disputable.lifecycle import Phase, Plan, phase, to_json
from disputable.loop import DECIDE_ERROR, READ_FAILED
from disputable.seed.fernhill import (FERNHILL, FIRST_PACKAGE, FIRST_PACKAGE_FILE, MONDAY, SECOND_PACKAGE,
                                      SECOND_PACKAGE_FILE, THREE_WEEKS_LATER)
from disputable.web import create_app, forms, views
from disputable.web import words as w
from fake_airwallex import ACCOUNT_ID, API_KEY, CLIENT_ID, FIRST_MILLIS, file_id
from test_loop import MINUTE, Run, at_step, ended, plan_text, posted, query
from test_rounds import LATER_DELIVERY, edited
from test_service import VOID_STAGE, bug_in_files_for, everything, other, unreadable_basis, with_plan
from test_web_app import FOREIGN, SENTINELS, collapsed
from web_helpers import BASE, ORIGIN, PORT, Voices, client, form_fields, pages, secrets_problems

TOKEN = "made-up-form-token-of-this-test-0123456789abc"  # what an app made by with_token() puts in its forms
CASE, APPROVE, DROP = "/case/dst_headphones", "/case/dst_headphones/approve", "/case/dst_headphones/drop"
NOW_APPROVE, NOW_SEND, NOW_DECIDE = (w.NOW_LINE.format(now=w.NOW[step]) for step in ("APPROVE", "SEND", "DECIDE"))
LINKS = frozenset({w.BACK_APPROVAL, w.BACK_CASE, w.BACK_QUEUE})
PDF = re.compile(r'href="(/case/[^"]+\.pdf)"')
DROPPED = "The plan was dropped: its file couldn't be built again exactly as planned."
NO_DROP = "This app found no reason to drop this plan, so it stays."  # true of every plan that answers drop-not-needed


def with_token(desk: Any) -> TestClient:
    """A client of an app whose forms all carry TOKEN, so a test can also post a form that no page offers now."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(forms, "new_token", lambda: TOKEN)
        return client(desk)


def approval_form(c, path: str = APPROVE) -> dict[str, str]:
    """The approval page's hidden fields, unescaped, as a browser posts them."""
    return form_fields(c.get(path).text, path)


def said(answer) -> list[str]:
    """What a refusal page says under its title, without the links back."""
    texts = Voices(answer.text).texts()
    return [text for text in texts[texts.index(w.NOT_DONE, 1) + 1:] if text not in LINKS]


# ----------------------------------------------------------------------------- the approval round trip
@pytest.mark.parametrize("step, dispute_id", [("monday", "dst_headphones"), ("monday", "dst_earbuds"),
                                              ("pushback", "dst_headphones")],
                         ids=["a challenge", "a hand-off", "round two"])
def test_the_form_posts_back_the_plan_and_the_reading_the_page_showed(tmp_path, step, dispute_id):
    run = at_step(tmp_path, step)
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    path = views.case_href(dispute_id, "approve")
    page = c.get(path).text
    assert w.APPROVE_AS.format(name="Priya") in Voices(page).texts()
    fields = form_fields(page, path)
    board = run.board()
    shown = board.records[dispute_id].rounds[-1].plan
    assert set(fields) == {"csrf", "plan", "reading"} and fields["plan"].isascii() and fields["reading"]
    assert (fields["plan"], fields["reading"]) == posted(board, dispute_id)  # the plan it showed and current()'s key
    assert load(Plan, json.loads(fields["plan"])) == shown and fields["plan"] not in page and "&#34;" in page
    answer = c.post(path, data=fields, headers=ORIGIN)
    assert answer.status_code == 303 and answer.headers["location"] == f"/case/{dispute_id}?done=approved"
    record, version = run.db.load_one(dispute_id)
    row = run.db.latest_approval(dispute_id, len(record.rounds))
    assert phase(record) is Phase.SEND and approvals.verify(record, row) is None
    assert row.plan_sha256 == approvals.plan_sha256(shown) == approvals.plan_sha256(record.rounds[-1].plan)
    assert (row.by, row.at, row.reading_key) == ("Priya", run.wall.at, fields["reading"])
    assert record.rounds[-1].plan.approved_by == "Priya" and run.db.versions(dispute_id)[-1][:2] == (version, "approve")
    assert run.clock.journal[-1] == (f"approve:{dispute_id}", run.wall.at)  # the one moment an approval takes
    texts = Voices(c.get(answer.headers["location"]).text).texts()
    approved = w.APPROVAL.format(by="Priya", number=len(record.rounds), when=views.when(row.at, run.wall.at))
    assert w.DONE["approved"] in texts and texts[texts.index(approved) + 1] == w.APPROVAL_STANDS


def test_a_double_click_saves_one_row_and_an_older_tab_is_refused(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    fields = approval_form(c)
    older = {**fields, "plan": with_plan(fields["plan"], reasons=["What an older tab showed."])}
    assert c.post(APPROVE, data=fields, headers=ORIGIN).headers["location"] == f"{CASE}?done=approved"
    before = everything(run)
    again = c.post(APPROVE, data=fields, headers=ORIGIN)  # the same form again: the approval stands
    assert again.status_code == 303 and again.headers["location"] == f"{CASE}?done=already"
    assert w.ALREADY in Voices(c.get(again.headers["location"]).text).texts()
    tab = c.post(APPROVE, data=older, headers=ORIGIN)
    assert (tab.status_code, said(tab)) == (409, [w.NOT_APPROVED, "Nothing waits for an approval. The case is at SEND.",
                                                  NOW_SEND])
    assert everything(run) == before and len(run.db.approvals("dst_headphones")) == 1
    voices = Voices(c.get(APPROVE).text)  # and the page itself no longer offers the form
    assert w.NOTHING_TO_APPROVE in voices.texts() and voices.forms == []


def test_a_double_click_whose_two_posts_overlap_answers_approved_and_already(tmp_path):
    # Both posts pass every check before either saves; the second's save then finds the case changed. It changed by
    # this approval itself, so its page says the approval stands, never that nothing was saved.
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    app = create_app(run.service, port=PORT)
    clients = [TestClient(app, base_url=BASE, follow_redirects=False) for _ in range(2)]
    fields = approval_form(clients[0])
    real, both = run.db.approve, threading.Barrier(2, timeout=20)

    def checked_then_saved(record, expected, row):
        both.wait()  # each post has passed every check: now both save
        return real(record, expected, row)

    run.db.approve = checked_then_saved
    answers, start = [], threading.Barrier(2, timeout=20)

    def click(c):
        start.wait()
        answers.append(c.post(APPROVE, data=fields, headers=ORIGIN))

    threads = [threading.Thread(target=click, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted((answer.status_code, answer.headers.get("location")) for answer in answers) == [
        (303, f"{CASE}?done=already"), (303, f"{CASE}?done=approved")]
    record = run.record("dst_headphones")
    assert len(run.db.approvals("dst_headphones")) == 1 and phase(record) is Phase.SEND
    assert [booking.key for booking in record.bookings if booking.key.startswith("review:")] == ["review:RFI"]


# ----------------------------------------------------------------------------- refusals the person can meet
def plan_changed(**changes):
    return lambda run, fields: {**fields, "plan": with_plan(fields["plan"], **changes)}


def earbuds_plan(run, fields):
    plan, reading = posted(run.board(), "dst_earbuds")  # what the earbuds' approval page shows
    return {**fields, "plan": plan, "reading": reading}


def past_the_deadline(run, fields):
    run.wall.set(run.record("dst_headphones").rounds[-1].plan.terms.due_at + MINUTE)
    return fields


def the_file_moved(run, fields):
    run.service.store = edited(FERNHILL, LATER_DELIVERY)  # the delivery scan moved since the page was shown
    return fields


VOID_PLAN = "This approval is void: the plan changed after the screen showed it."
REFUSED = {  # the cause: (where the form goes, what changed after the page showed it, the status, what the page says)
    "the amount text edited": (APPROVE, plan_changed(terms__amount="1249.00"), 409,
                               [w.NOT_APPROVED, "This approval is void: the amount changed.", NOW_APPROVE]),
    "the reasons moved on": (APPROVE, plan_changed(reasons=["A reason the screen never showed."]), 409,
                             [w.NOT_APPROVED, VOID_PLAN, NOW_APPROVE]),
    "an approver named in the plan": (APPROVE, plan_changed(approved_by="Mallory"), 409,
                                      [w.NOT_APPROVED, VOID_PLAN, NOW_APPROVE]),
    "another dispute's plan": (APPROVE, earbuds_plan, 409,
                               [w.NOT_APPROVED, "This approval is for another dispute.", NOW_APPROVE]),
    "the deadline passed": (APPROVE, past_the_deadline, 409, [w.NOT_APPROVED, "The deadline has passed.", NOW_APPROVE]),
    "the reading changed": (APPROVE, lambda run, fields: {**fields, "reading": "0" * 64}, 409,
                            [w.READING_CHANGED, NOW_APPROVE]),
    "no reading": (APPROVE, lambda run, fields: {**fields, "reading": ""}, 409, [w.READING_CHANGED, NOW_APPROVE]),
    "the file can't be built again": (APPROVE, the_file_moved, 409,
                                      [w.FILE_MOVED.format(why=rounds.MOVED), w.DROP_OFFER, w.DROP_BUTTON, NOW_APPROVE]),
    "a plan cut short": (APPROVE, lambda run, fields: {**fields, "plan": fields["plan"][:-1]}, 400,
                         [w.PLAN_UNREADABLE, NOW_APPROVE]),
    "a field added to the plan": (APPROVE, lambda run, fields: {**fields, "plan": json.dumps(
        {**json.loads(fields["plan"]), "by": "Mallory"})}, 400, [w.PLAN_UNREADABLE, NOW_APPROVE]),
    "no such case": ("/case/dst_nothing_here/approve", lambda run, fields: fields, 404, [w.NO_CASE]),
}


@pytest.mark.parametrize("cause", REFUSED)
def test_each_refusal_answers_its_status_and_sentence_and_saves_nothing(tmp_path, cause):
    path, change, status, lines = REFUSED[cause]
    run = at_step(tmp_path, "monday")
    c = client(run.service)
    data = change(run, approval_form(c))
    before = everything(run)
    answer = c.post(path, data=data, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (status, lines)
    assert everything(run) == before  # no row, no record and no moment of the clock


def test_round_ones_plan_posted_in_round_two(tmp_path):
    run = at_step(tmp_path, "pushback")
    c = client(run.service)
    fields = approval_form(c)  # round two's page
    first = run.db.approvals("dst_headphones")[0]  # what round one's page posted, as its row keeps it
    assert first.round_number == 1 and fields["plan"] != first.plan
    before = everything(run)
    answer = c.post(APPROVE, data={**fields, "plan": first.plan, "reading": first.reading_key}, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [w.NOT_APPROVED, VOID_STAGE, NOW_APPROVE])
    assert everything(run) == before
    texts = Voices(c.get(CASE).text).texts()  # round one's approval was used: its challenge went out on Monday
    approved = w.APPROVAL.format(by="Priya", number=1, when=views.when(first.at, run.wall.at))
    assert texts[texts.index(approved) + 1] == w.APPROVAL_SENT_VOID


def test_a_case_that_changed_after_the_checks_is_stale(tmp_path):
    run = at_step(tmp_path, "monday")
    c = client(run.service)
    fields = approval_form(c)
    real = run.db.approve

    def a_pass_saves_first(record, expected, row):  # a pass saves a new reading between the checks and the save
        head, version = run.db.load_one(record.dispute_id)
        run.db.save(replace(head, blocked=None), version, "poll")
        return real(record, expected, row)

    run.db.approve = a_pass_saves_first
    answer = c.post(APPROVE, data=fields, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [w.STALE, NOW_APPROVE])
    assert run.db.approvals("dst_headphones") == [] and run.db.versions("dst_headphones")[-1][1] == "poll"


def test_an_approval_that_wouldnt_send_is_not_recorded(tmp_path, monkeypatch, capsys):
    run = at_step(tmp_path, "monday")
    c = client(run.service)
    fields = approval_form(c)
    monkeypatch.setattr(approvals, "verify", lambda record, row: "The plan changed after it was approved.")
    before = everything(run)
    answer = c.post(APPROVE, data=fields, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [w.NOT_RECORDED, NOW_APPROVE])
    assert everything(run) == before
    assert "couldn't record an approval of dispute dst_headphones" in capsys.readouterr().err  # the log, not the page


def test_approvals_off(tmp_path):
    run = at_step(tmp_path, "monday")
    off = other(run, None)  # the screens started with no approver
    c = with_token(off)
    voices = Voices(c.get(APPROVE).text)
    assert w.APPROVALS_OFF in voices.texts() and voices.forms == []  # the page offers no form
    text, key = posted(off.board(), "dst_headphones")
    before = everything(run)
    answer = c.post(APPROVE, data={"csrf": TOKEN, "plan": text, "reading": key}, headers=ORIGIN)  # made by hand
    assert (answer.status_code, said(answer)) == (409, [w.APPROVALS_OFF, NOW_APPROVE])
    assert everything(run) == before


def test_a_case_that_cant_be_read_is_refused_and_its_pages_say_why(tmp_path):
    run = at_step(tmp_path, "monday")
    c = client(run.service)
    fields = approval_form(c)
    query(run.db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
                  "('dst_headphones', 1, 'x', '{', ?)", MONDAY.isoformat())  # a row of the case that won't load
    before = everything(run)
    answer = c.post(APPROVE, data=fields, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [w.BROKEN, APPROVAL_UNREADABLE])
    assert everything(run) == before
    for path in ("/", CASE, APPROVE):  # the queue counts it as a person's case, and no page offers a form for it
        voices = Voices(c.get(path).text)
        assert w.BROKEN_RECORD.format(why=APPROVAL_UNREADABLE) in voices.texts() and voices.forms == [], path
    assert w.NO_ORDER_NUMBER.format(id="dst_headphones") in Voices(c.get("/").text).texts()


def test_the_queue_shows_the_last_passs_problems_as_written(tmp_path):
    run = at_step(tmp_path, "monday")
    run.fake.fail("GET", f"{DISPUTES}/dst_headphones", timeout=True, times=2)  # the read times out, and its one retry
    report = run.pass_(MINUTE)
    assert READ_FAILED.format(who="Order #1109", why="no answer in 30 seconds") in report.problems
    texts = Voices(client(run.service).get("/").text).texts()
    first = texts.index(w.PROBLEMS) + 1
    assert texts[first:first + len(report.problems)] == list(report.problems)


# ----------------------------------------------------------------------------- the form checks
def test_a_form_from_anywhere_else_is_refused_and_saves_nothing(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    fields = approval_form(c)
    before = everything(run)
    for case, (change, headers, line) in FOREIGN.items():
        answer = c.post(APPROVE, data=change(fields), headers=headers)
        assert (answer.status_code, said(answer)) == (403, [line]), case
    restarted = client(run.service).post(APPROVE, data=fields, headers=ORIGIN)  # an old page after a restart
    assert (restarted.status_code, said(restarted)) == (403, [w.FORM_OLD])  # it did come from this app's own page
    assert c.post(APPROVE, data=fields, headers={**ORIGIN, "Host": "evil.example:8765"}).status_code == 400
    assert everything(run) == before
    answer = c.post(APPROVE, data=fields, headers={"Referer": f"{BASE}{APPROVE}"})  # the page's own form, Referer only
    assert answer.status_code == 303 and len(run.db.approvals("dst_headphones")) == 1


def test_a_form_with_an_extra_field_is_refused_and_the_row_names_the_configured_approver(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    sam = other(run, "Sam Example")  # the screens started with --approver "Sam Example"
    c = client(sam)
    page = c.get(APPROVE).text
    assert w.APPROVE_AS.format(name="Sam Example") in Voices(page).texts()
    fields = form_fields(page, APPROVE)
    before = everything(run)
    for extra in ({"by": "Mallory"}, {"approver": "Mallory"}, {"approved_by": "Mallory"}):
        answer = c.post(APPROVE, data={**fields, **extra}, headers=ORIGIN)
        assert (answer.status_code, said(answer)) == (400, [w.FORM_BAD]), extra
    assert everything(run) == before
    assert c.post(APPROVE, data=fields, headers=ORIGIN).status_code == 303
    row, record = run.db.latest_approval("dst_headphones", 1), run.record("dst_headphones")
    assert row.by == record.rounds[-1].plan.approved_by == "Sam Example" and approvals.verify(record, row) is None


# ----------------------------------------------------------------------------- dropping a plan whose file moved
def test_a_plan_whose_file_moved_is_dropped_through_its_own_form(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    fields = approval_form(c)  # the page as it was shown
    run.service.store = edited(FERNHILL, LATER_DELIVERY)  # then the delivery scan moved: the file's date changes
    version = run.board().versions["dst_headphones"]
    for path in (CASE, APPROVE):  # both pages say so and offer the drop, and the approval page offers no approval
        voices = Voices(c.get(path).text)
        assert w.FILE_MOVED.format(why=rounds.MOVED) in voices.texts() and w.DROP_OFFER in voices.texts(), path
        assert [action for action, _ in voices.forms] == [DROP], path
    refused = c.post(APPROVE, data=fields, headers=ORIGIN)
    drop = form_fields(refused.text, DROP)  # the refusal page's own drop form
    assert refused.status_code == 409 and drop == {"csrf": fields["csrf"], "version": str(version)}
    before = everything(run)
    answer = c.post(DROP, data=drop, headers=ORIGIN)
    assert answer.status_code == 303 and answer.headers["location"] == f"{CASE}?done=dropped"
    record = run.record("dst_headphones")
    assert phase(record) is Phase.DECIDE and record.rounds[-1].plan is None and record.rounds[-1].notes[-1] == DROPPED
    assert run.db.versions("dst_headphones")[-1][1] == "drop" and run.db.approvals("dst_headphones") == []
    assert everything(run)[1] == before[1]  # no moment taken: a drop isn't an approval
    assert w.DONE["dropped"] in Voices(c.get(answer.headers["location"]).text).texts()
    after = everything(run)
    again = c.post(DROP, data=drop, headers=ORIGIN)  # a second click: the version its page showed is gone
    assert (again.status_code, said(again)) == (409, [w.STALE, NOW_DECIDE])
    now = c.post(DROP, data={**drop, "version": str(run.board().versions["dst_headphones"])}, headers=ORIGIN)  # by hand
    assert (now.status_code, said(now)) == (409, [w.NOT_DROPPED, "The plan can't be dropped now. It is at DECIDE.",
                                                  NOW_DECIDE])
    assert w.BACK_APPROVAL not in Voices(now.text).texts() and everything(run) == after
    # Code decides the round again on today's records, and the new plan goes out once its own page is approved.
    assert "dst_headphones" in run.pass_(MINUTE).decided and phase(run.record("dst_headphones")) is Phase.APPROVE
    run.wall.move(MINUTE)
    new = approval_form(c)
    assert new["plan"] != fields["plan"] and c.post(APPROVE, data=new, headers=ORIGIN).status_code == 303
    assert dict(run.pass_(MINUTE).sent)["dst_headphones"] is Outcome.TAKEN


def test_a_drop_whose_file_builds_again_keeps_the_plan(tmp_path):
    run = at_step(tmp_path, "monday")
    c = client(run.service)
    run.service.store = edited(FERNHILL, LATER_DELIVERY)
    drop = form_fields(c.get(CASE).text, DROP)  # the page showed the drop while the scan had moved
    run.service.store = FERNHILL  # and the records were set right before the click
    before = everything(run)
    answer = c.post(DROP, data=drop, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [NO_DROP, NOW_APPROVE])
    assert everything(run) == before
    assert approval_form(c)["plan"] == plan_text(run.record("dst_headphones").rounds[-1].plan)  # still to approve
    accept = c.post("/case/dst_earbuds/drop", data={**drop, "version": str(run.board().versions["dst_earbuds"])},
                    headers=ORIGIN)  # an accept sends no file, so nothing of it can move
    assert (accept.status_code, said(accept)) == (409, [NO_DROP, NOW_APPROVE])
    assert everything(run) == before


# ----------------------------------------------------------------------------- a saved basis that won't load
NOT_JSON = "The saved basis isn't valid JSON."


@pytest.mark.parametrize("dispute_id", ["dst_headphones", "dst_earbuds"], ids=["a challenge", "an accept"])
def test_a_plan_whose_basis_cant_be_read_is_dropped_through_its_pages(tmp_path, dispute_id):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    case, approve, drop = (views.case_href(dispute_id, *rest) for rest in ((), ("approve",), ("drop",)))
    older = approval_form(c, approve)  # an approval page shown before the row went bad
    unreadable_basis(run, dispute_id)
    assert dispute_id not in run.board().broken  # its record and its approvals load: only the basis doesn't
    for path in (case, approve):  # both pages answer, say why, and offer the drop and nothing else
        answer = c.get(path)
        voices = Voices(answer.text)
        assert answer.status_code == 200 and w.ROUND_UNREADABLE.format(why=NOT_JSON) in voices.texts(), path
        assert [action for action, _ in voices.forms] == [drop], path
    before = everything(run)
    refused = c.post(approve, data=older, headers=ORIGIN)  # the older tab's approval: refused, never a 500
    moved = w.FILE_MOVED if dispute_id == "dst_headphones" else w.ROUND_UNREADABLE  # an accept has no file to move
    assert (refused.status_code, said(refused)[:3]) == (409, [moved.format(why=NOT_JSON), w.DROP_OFFER, w.DROP_BUTTON])
    assert everything(run) == before
    answer = c.post(drop, data=form_fields(refused.text, drop), headers=ORIGIN)  # the refusal page's own drop form
    assert answer.status_code == 303 and answer.headers["location"] == f"{case}?done=dropped"
    record = run.record(dispute_id)
    why = "its file couldn't be built again exactly as planned" if dispute_id == "dst_headphones" else \
        "what this app saved for it can't be read"
    assert phase(record) is Phase.DECIDE and record.rounds[-1].notes[-1] == f"The plan was dropped: {why}."
    assert c.get(case).status_code == 200
    # Code decides the round again on today's records, saving a new basis, and the new plan waits for its approval.
    assert dispute_id in run.pass_(MINUTE).decided and phase(run.record(dispute_id)) is Phase.APPROVE
    assert w.ROUND_UNREADABLE.format(why=NOT_JSON) not in Voices(c.get(case).text).texts()


READING_NOT_JSON = "The saved reading isn't valid JSON."


def test_a_plan_whose_reading_cant_be_read_is_refused_as_a_saved_row_and_dropped(tmp_path):
    # The newest row of the reading the plan was made on won't load; its basis does, so the file still builds as
    # planned. Every page, and the older tab's refusal, says a saved row is why: never that the file moved.
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    older = approval_form(c)  # the approval page as it was shown, before the row went bad
    key = run.db.saved.basis("dst_headphones", 1).reading_key
    query(run.db, "INSERT INTO readings (dispute_id, round_number, key, failure, reading, saved_at) VALUES "
                  "('dst_headphones', 1, ?, NULL, '{', ?)", key, MONDAY.isoformat())
    unread = w.ROUND_UNREADABLE.format(why=READING_NOT_JSON)
    for path in (CASE, APPROVE):
        voices = Voices(c.get(path).text)
        texts = voices.texts()
        assert unread in texts and [action for action, _ in voices.forms] == [DROP], path
        assert w.FILE_HASH.format(sha256=FIRST_PACKAGE_FILE) in texts, path  # the file, built as planned
        assert not [text for text in texts if text.startswith(w.FILE_MOVED.split("{")[0])], path
        assert w.CLAUDE_CANT_SHOW in texts and w.CLAUDE_NOT_READ not in texts, path  # a reading is on file
    before = everything(run)
    refused = c.post(APPROVE, data=older, headers=ORIGIN)
    assert (refused.status_code, said(refused)[:3]) == (409, [unread, w.DROP_OFFER, w.DROP_BUTTON])
    assert everything(run) == before
    answer = c.post(DROP, data=form_fields(refused.text, DROP), headers=ORIGIN)  # the refusal page's own drop form
    assert answer.status_code == 303 and answer.headers["location"] == f"{CASE}?done=dropped"
    record = run.record("dst_headphones")
    assert phase(record) is Phase.DECIDE and record.rounds[-1].notes[-1] == (
        "The plan was dropped: what this app saved for it can't be read.")
    assert c.get(CASE).status_code == 200
    report = run.pass_(MINUTE)  # code decides the round again, and that reading still won't load: a person takes it
    assert DECIDE_ERROR.format(who="Order #1109") in report.problems and phase(run.record("dst_headphones")) is Phase.PERSON


@pytest.mark.parametrize("dispute_id", ["dst_headphones", "dst_earbuds"], ids=["a challenge", "an accept"])
def test_a_basis_in_another_form_shows_a_plain_sentence_on_every_page(tmp_path, dispute_id, caplog):
    # A row of this format whose fields don't fit, as a Basis field added in code with no FORMAT bump leaves it in an
    # old database. jsonable's words for it name fields in code's terms: they go to the log, never to a page.
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    c = client(run.service)
    case, approve, drop = (views.case_href(dispute_id, *rest) for rest in ((), ("approve",), ("drop",)))
    older = approval_form(c, approve)
    basis = json.loads(query(run.db, "SELECT basis FROM bases WHERE dispute_id = ? ORDER BY id DESC LIMIT 1",
                             dispute_id)[0][0])
    del basis["basis"]["ce3_reasons"]
    unreadable_basis(run, dispute_id, json.dumps(basis))
    unread = w.ROUND_UNREADABLE.format(why=w.ROW_FORM)
    with caplog.at_level("WARNING", logger="disputable.web"):
        shown = [Voices(c.get(path).text).texts() for path in (case, approve)]
        refused = c.post(approve, data=older, headers=ORIGIN)
    moved = w.FILE_MOVED.format(why=w.ROW_FORM) if dispute_id == "dst_headphones" else unread  # an accept sends no file
    assert refused.status_code == 409 and said(refused)[0] == moved
    for texts in (*shown, said(refused)):
        assert unread in texts or moved in texts
        assert not [text for text in texts if "doesn't fit" in text or "ce3_reasons" in text]
    assert [record for record in caplog.records if record.name == "disputable.web" and "doesn't fit" in
            record.getMessage()]
    answer = c.post(drop, data=form_fields(refused.text, drop), headers=ORIGIN)  # and the drop still works
    assert answer.status_code == 303 and phase(run.record(dispute_id)) is Phase.DECIDE


def test_a_drop_while_the_checks_hit_a_bug_answers_drop_not_needed(tmp_path, monkeypatch):
    # A bug in the checks that read the round (rounds.current()) keeps the plan. The case page shows it as one and
    # offers no drop for it: a drop posted by hand answers its refusal, never a 500.
    run = at_step(tmp_path, "monday")
    c = client(run.service, raise_server_exceptions=False)
    token = approval_form(c)["csrf"]

    def bug(*args, **options):
        raise documents.NoFileDue("a bug in the rules")

    monkeypatch.setattr(rounds, "current", bug)
    voices = Voices(c.get("/case/dst_earbuds").text)
    assert w.EVIDENCE_BUG in voices.texts() and voices.forms == []
    before = everything(run)
    answer = c.post("/case/dst_earbuds/drop", data={"csrf": token, "version": str(run.board().versions["dst_earbuds"])},
                    headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [NO_DROP, NOW_APPROVE])
    assert everything(run) == before


def test_a_bug_in_the_rules_lets_a_challenge_be_dropped_and_keeps_an_accept(tmp_path, monkeypatch, capsys):
    # A bug in the rules while the challenge's file is built again counts as a file that can't be built, so its page
    # offers the drop and the drop goes through. The accept sends no file: only its other checks meet the bug, so it
    # stays, and the terminal gets the details. Neither drop answers an error page.
    run = at_step(tmp_path, "monday")
    c = client(run.service, raise_server_exceptions=False)
    token = approval_form(c)["csrf"]

    def bug(*args, **options):
        raise KeyError("a bug in the rules")

    monkeypatch.setattr(rules, "evaluate", bug)
    voices = Voices(c.get(CASE).text)
    assert w.EVIDENCE_BUG in voices.texts() and w.FILE_MOVED.format(why=rounds.REBUILD_ERROR) in voices.texts()
    assert [action for action, _ in voices.forms] == [DROP]
    earbuds = Voices(c.get("/case/dst_earbuds").text)
    assert w.EVIDENCE_BUG in earbuds.texts() and earbuds.forms == []
    before = everything(run)
    kept = c.post("/case/dst_earbuds/drop", data={"csrf": token, "version": str(run.board().versions["dst_earbuds"])},
                  headers=ORIGIN)
    assert (kept.status_code, said(kept)) == (409, [NO_DROP, NOW_APPROVE]) and everything(run) == before
    assert "Disputable couldn't check the plan of dispute dst_earbuds for a drop, so it stays" in capsys.readouterr().err
    answer = c.post(DROP, data=form_fields(c.get(CASE).text, DROP), headers=ORIGIN)
    assert answer.status_code == 303 and answer.headers["location"] == f"{CASE}?done=dropped"
    record = run.record("dst_headphones")
    assert phase(record) is Phase.DECIDE and record.rounds[-1].notes[-1] == DROPPED


def test_a_bug_in_files_fors_own_lines_keeps_the_plan_and_never_answers_an_error_page(tmp_path, monkeypatch, capsys):
    # Outside the rules, files_for() turns no bug into a file that can't be built: a drop posted while such a bug is
    # there answers that the plan stays, saves nothing, and the terminal gets the details.
    run = at_step(tmp_path, "monday")
    c = client(run.service, raise_server_exceptions=False)
    token = approval_form(c)["csrf"]
    version = run.board().versions["dst_headphones"]
    bug_in_files_for(monkeypatch, TypeError("a bug in code outside the rules"))
    before = everything(run)
    answer = c.post(DROP, data={"csrf": token, "version": str(version)}, headers=ORIGIN)
    assert (answer.status_code, said(answer)) == (409, [NO_DROP, NOW_APPROVE])
    assert everything(run) == before and phase(run.record("dst_headphones")) is Phase.APPROVE
    logged = capsys.readouterr().err
    assert "Disputable couldn't check the plan of dispute dst_headphones for a drop, so it stays: a bug in code " \
           "outside the rules" in logged and "Traceback" in logged


def test_a_sent_plans_basis_that_cant_be_read_still_shows_its_page(tmp_path):
    run = at_step(tmp_path, "approved")
    run.pass_(2 * MINUTE)  # the headphones' challenge goes out with its file
    plan = run.record("dst_headphones").rounds[-1].plan
    assert plan.sending_at is not None
    unreadable_basis(run, "dst_headphones")
    c = client(run.service)
    voices = Voices(c.get(CASE).text)
    assert w.FILE_UNREADABLE.format(why=NOT_JSON) in voices.texts() and voices.forms == []
    (sha256,) = plan.terms.file_hashes
    assert c.get(f"{CASE}/file/{sha256}.pdf").status_code == 404  # its saved copy can't be read, so none is served
    assert c.get(APPROVE).status_code == 200 and c.get("/").status_code == 200


# ----------------------------------------------------------------------------- Ending 1, played through the pages
@dataclass(frozen=True)
class Shown:
    """One step of the story as the screens showed it: the board, the html of every page and error page, and the bytes
    of every PDF the pages link to, by where each came from."""
    board: Board
    pages: dict[str, str]
    errors: dict[str, tuple[int, str]]  # (status, html)
    files: dict[str, bytes]


@dataclass(frozen=True)
class Screens:
    run: Run
    shown: dict[str, Shown]  # by step, in order


@pytest.fixture(scope="session")
def screens(tmp_path_factory) -> Screens:
    """Ending 1 played once a session as the loop and a browser play it: the passes on the fake sandbox, and Priya's
    three approvals through the approval pages' own forms. Every page is kept at each step, rendered with sentinel keys
    in the environment. Read it only: the tests share it."""
    run = Run(tmp_path_factory.mktemp("screens"))
    shown: dict[str, Shown] = {}
    with pytest.MonkeyPatch.context() as patch:
        for name, value in SENTINELS.items():
            patch.setenv(name, value)
        c = with_token(run.service)

        def approve(dispute_id: str) -> None:
            path = views.case_href(dispute_id, "approve")
            answer = c.post(path, data=approval_form(c, path), headers=ORIGIN)
            assert answer.headers.get("location") == f"/case/{dispute_id}?done=approved", answer.status_code

        run.pass_()  # Monday 9:00: the agent reads and decides
        shown["monday"] = show(c, run)
        run.wall.move(5 * MINUTE)
        approve("dst_earbuds")
        approve("dst_headphones")
        run.pass_(2 * MINUTE)  # it sends what is ready: the headphones' challenge goes out with its file
        shown["monday-sent"] = show(c, run)
        run.pass_(MINUTE)  # and reads: the ear tips and the earbuds close, the headphones wait for the bank
        shown["monday-read"] = show(c, run)
        run.wall.set(THREE_WEEKS_LATER)
        run.fake.sandbox.escalate("dst_headphones")
        run.pass_()  # round two, decided
        shown["pushback"] = show(c, run)
        run.wall.move(MINUTE)
        approve("dst_headphones")
        run.pass_(MINUTE)
        run.pass_(MINUTE)
        shown["pushback-sent"] = show(c, run)
        run.wall.move(timedelta(weeks=1))
        run.fake.sandbox.resolve("dst_headphones", Status.WON)
        run.pass_()
        shown["won"] = show(c, run)
    return Screens(run, shown)


def show(c, run: Run) -> Shown:
    """Every page of the board now, every PDF they link to, and every refusal a person can meet. None of them saves
    anything or takes a moment of the clock."""
    board, before = run.board(), everything(run)
    answers = {path: c.get(path) for path in pages(board)}
    assert {answer.status_code for answer in answers.values()} == {200}
    links = sorted({href for answer in answers.values() for href in PDF.findall(answer.text)})
    files = {href: c.get(href) for href in links}
    assert {(answer.status_code, answer.headers["content-type"]) for answer in files.values()} <= {
        (200, "application/pdf")}
    errors = {what: (answer.status_code, answer.text) for what, answer in refusals(c, board)}
    assert everything(run) == before, "a page or a refusal saved something"
    return Shown(board, {path: answer.text for path, answer in answers.items()}, errors,
                 {href: answer.content for href, answer in files.items()})


def refusals(c, board: Board) -> Iterator[tuple[str, Any]]:
    """Each kind of refusal and error page, from every route, and the Service's own refusals on every case."""
    form = {"csrf": TOKEN, "plan": "{}", "reading": ""}
    yield "no page", c.get("/nowhere")
    yield "no case", c.get("/case/dst_nobody")
    yield "no file", c.get(f"{CASE}/file/{'0' * 64}.pdf")
    yield "no such method", c.post("/money", headers=ORIGIN)
    yield "another host", c.get("/", headers={"Host": "evil.example"})
    yield "a form from another site", c.post(APPROVE, data=form, headers={"Origin": "http://evil.example"})
    yield "a wrong token", c.post(APPROVE, data={**form, "csrf": "x" * 43}, headers=ORIGIN)
    yield "a field too many", c.post(APPROVE, data={**form, "by": "Mallory"}, headers=ORIGIN)
    yield "a plan this app can't read", c.post(APPROVE, data={**form, "plan": "{"}, headers=ORIGIN)
    yield "no such case", c.post("/case/dst_nobody/approve", data=form, headers=ORIGIN)
    yield "next", c.post("/demo/next", data={"csrf": TOKEN, "played": "0"}, headers=ORIGIN)
    yield "restart", c.post("/demo/restart", data={"csrf": TOKEN, "ending": "1"}, headers=ORIGIN)
    for dispute_id, record in board.records.items():
        plan = record.rounds[-1].plan
        if plan is not None:
            unseen = with_plan(plan_text(plan), reasons=["A plan no page showed."])
            yield f"{dispute_id}: a plan no page showed", c.post(views.case_href(dispute_id, "approve"),
                                                                 data={**form, "plan": unseen}, headers=ORIGIN)
        for version in (board.versions[dispute_id], board.versions[dispute_id] - 1):
            yield f"{dispute_id}: a drop at version {version}", c.post(
                views.case_href(dispute_id, "drop"), data={"csrf": TOKEN, "version": str(version)}, headers=ORIGIN)


def test_ending_one_through_the_pages_is_the_loops_own_story(screens):
    run, once = screens.run, ended("ending 1 won")
    board, theirs = run.board(), once.board()
    assert run.total() == once.total() == Decimal("279.30")
    assert {key: to_json(record) for key, record in board.records.items()} == {
        key: to_json(record) for key, record in theirs.records.items()}
    assert board.approvals == theirs.approvals and run.clock.journal == once.clock.journal  # the same moments, too
    assert [(row.dispute_id, row.round_number, row.by) for rows in board.approvals.values() for row in rows] == [
        ("dst_earbuds", 1, "Priya"), ("dst_headphones", 1, "Priya"), ("dst_headphones", 2, "Priya")]
    assert [request.url.path for request in run.fake.posts("/challenge")] == [f"{DISPUTES}/dst_headphones/challenge"] * 2


MONEY = {  # the step: (open cases, cost so far, still at risk)
    "monday": (3, "$15.00", "$1,450.00"), "monday-sent": (3, "$47.00", "$1,450.00"),
    "monday-read": (1, "$248.30", "$1,249.00"), "pushback": (1, "$263.30", "$1,249.00"),
    "pushback-sent": (1, "$279.30", "$1,249.00"), "won": (0, "$279.30", "$0.00")}


@pytest.mark.parametrize("step", MONEY)
def test_the_money_at_each_step(screens, step):
    count, cost, risk = MONEY[step]
    shown = screens.shown[step]
    queue, money = Voices(shown.pages["/"]).texts(), Voices(shown.pages["/money"]).texts()
    assert w.QUEUE_SUMMARY.format(open=count, cost=cost, risk=risk) in queue
    first = money.index(w.MONEY_SUMMARY.format(cost=cost, risk=risk))
    last = "This queue cost $1,186.30 less than that." if step == "won" else w.NOT_FINAL
    assert money[first:first + 3] == [w.MONEY_SUMMARY.format(cost=cost, risk=risk),
                                      "Accepting everything at once would have cost $1,465.60.", last]
    assert not [text for text in queue + money if "2,699" in text]  # the queue's own records, never the store's cases


def test_the_money_page_at_the_end_of_ending_one(screens):
    money = Voices(screens.shown["won"].pages["/money"]).texts()
    rows = money[money.index(w.COLUMNS[-1]) + 1:money.index(w.NOTES_TITLE)]
    assert rows == [
        "Order #1121", "Closed: accepted.", "Refund: $12.00", "Refund fee: $0.30", "$12.30", "$0.00", "$12.30",
        "Order #1098", "Closed: accepted.", "Chargeback fee: $15.00", "A person's review: $16.00",
        "Amount the bank took back: $189.00", "$220.00", "$0.00", "$204.00",
        "Order #1109", "Closed: won.", "A person's review: $16.00", "Chargeback fee: $15.00", "A person's review: $16.00",
        "$47.00", "$0.00", "$1,249.30"]


def test_each_try_to_send_names_its_round_and_says_once_that_airwallex_took_it(screens):
    # Round one's challenge and round two's each went out once: the headphones' page lists one try for each round, and
    # no try says twice that Airwallex took it.
    shown = screens.shown["won"]
    texts = Voices(shown.pages[CASE]).texts()
    tries = texts[texts.index(w.SENDS_TITLE) + 1:texts.index(w.ROUNDS)]
    sends = shown.board.sends["dst_headphones"]
    assert [(line.round_number, line.outcome, line.status, line.detail) for line in sends] == [
        (1, "taken", 200, TOOK), (2, "taken", 200, TOOK)]
    assert tries == [f"Round {line.round_number}, {views.when(line.at, shown.board.now)}: Airwallex took it (HTTP 200)."
                     for line in sends]
    every = [text for step in screens.shown.values() for html in step.pages.values() for text in Voices(html).texts()]
    assert every.count(w.SENDS_TITLE) > 2 and TOOK not in every  # no line on any page is the bare outcome


def test_round_ones_approval_says_its_plan_went_out_and_round_twos_stands(screens):
    shown = screens.shown["won"]
    texts = Voices(shown.pages[CASE]).texts()
    first, second = shown.board.approvals["dst_headphones"]
    assert texts[texts.index(w.APPROVALS) + 1:texts.index(w.CASE_MONEY)] == [
        w.APPROVAL.format(by="Priya", number=1, when=views.when(first.at, shown.board.now)), w.APPROVAL_SENT_VOID,
        w.APPROVAL.format(by="Priya", number=2, when=views.when(second.at, shown.board.now)), w.APPROVAL_STANDS]


def test_claudes_own_words_show_only_inside_the_collapsed_draft(screens):
    drafts = 0
    for step, shown in screens.shown.items():
        for where, html in shown.pages.items():
            voices = Voices(html)
            assert collapsed(voices), (step, where)  # never open beside code's plan
            drafts += any(node.voice == "claude" for node in voices.nodes)
    assert drafts >= 4  # so the check means something: the earbuds' and the headphones' drafts, on both their pages


def test_the_pdf_a_person_downloads_is_the_file_that_went_out(screens):
    files = {step: shown.files for step, shown in screens.shown.items()}
    first, second = f"{CASE}/file/{FIRST_PACKAGE_FILE}.pdf", f"{CASE}/file/{SECOND_PACKAGE_FILE}.pdf"
    assert files["monday"][first] == files["monday-sent"][first] == FIRST_PACKAGE.pdf()  # built, then the saved basis
    assert files["won"] == {second: SECOND_PACKAGE.pdf()}  # only the round's own file
    uploaded = [(entry["filename"], entry["sha256"]) for entry in screens.run.fake.files.values()]
    assert uploaded == [(document.filename, hashlib.sha256(document.pdf()).hexdigest())  # the same bytes went out
                        for document in (FIRST_PACKAGE, SECOND_PACKAGE)]


# ----------------------------------------------------------------------------- secrets
def leaks(html: str, board: Board, secrets: list[str]) -> list[str]:
    """secrets_problems(), and the fake's account id inside any base64 run, at any offset."""
    problems = secrets_problems(html, board, secrets)
    if any(ACCOUNT_ID in (privacy.decode_base64(token) or "") for token in privacy.base64_tokens(html)):
        problems.append("the account id in base64")
    return problems


def test_no_page_holds_the_account_id_a_file_id_or_a_key(screens):
    run = screens.run
    file_ids = list(run.fake.files)
    # So the check means something: the challenges went out with files whose ids carry the fake's account id, and
    # every dispute GET since holds that id in the seller profile and the summary file Airwallex adds.
    assert len(run.fake.posts("/challenge")) == 2 and {privacy.account_id_of(found) for found in file_ids} == {ACCOUNT_ID}
    assert ACCOUNT_ID in json.dumps(run.fake.sandbox.disputes["dst_headphones"])
    secrets = [*SENTINELS.values(), ACCOUNT_ID, CLIENT_ID, API_KEY, *sorted(run.fake.tokens), *file_ids]
    checked = 0
    for step, shown in screens.shown.items():
        for where, html in shown.pages.items():
            assert leaks(html, shown.board, secrets) == [], (step, where)
            checked += 1
        for what, (status, html) in shown.errors.items():
            assert status >= 400 and leaks(html, shown.board, secrets) == [], (step, what, status)
            checked += 1
        for where, content in shown.files.items():
            assert not [secret for secret in secrets if secret.encode() in content], (step, where)
    assert checked > 150


def test_a_refusal_that_quotes_the_file_id_shows_no_file_id(tmp_path):
    run = at_step(tmp_path, "approved")
    sent = file_id(FIRST_PACKAGE.filename, FIRST_MILLIS + len(run.fake.files))  # the id the upload is about to get
    run.fake.fail("POST", f"{DISPUTES}/dst_headphones/challenge", status=400,
                  body={"code": "validation_error", "message": f"The file {sent} can't be used here."})
    report = run.pass_(2 * MINUTE)
    assert dict(report.sent)["dst_headphones"] is Outcome.FAILED and list(run.fake.files) == [sent]
    c, board = with_token(run.service), run.board()
    texts = Voices(c.get(CASE).text).texts()
    detail = "HTTP 400, validation_error: The file (a file id) can't be used here"  # hidden where Airwallex answered
    assert detail in texts and w.FILE_KEPT in texts and phase(board.records["dst_headphones"]) is Phase.PERSON
    (refused,) = board.sends["dst_headphones"]  # the refused try, with Airwallex's own phrase on the line under it
    tried = f"Round 1, {views.when(refused.at, board.now)}: Airwallex refused it (HTTP 400)."
    assert texts[texts.index(detail) - 1] == tried
    assert c.get(f"{CASE}/file/{FIRST_PACKAGE_FILE}.pdf").content == FIRST_PACKAGE.pdf()  # kept for a person
    secrets = [sent, ACCOUNT_ID, *SENTINELS.values()]
    for path in pages(board):
        assert leaks(c.get(path).text, board, secrets) == [], path
    for what, answer in refusals(c, board):
        assert answer.status_code >= 400 and leaks(answer.text, board, secrets) == [], what
