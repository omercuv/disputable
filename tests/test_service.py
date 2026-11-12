"""The Service: the Desk of a live run. Every refusal of the approval screen as a Result key, the double click (two
clicks whose posts overlap too), the approver fixed at start, check_approval() saving nothing, a saved basis that won't
load, drop_moved() (a bug in the rules while the plan's file is built again lets the plan be dropped, and a bug in the
other checks, files_for()'s own lines among them, is no reason to drop), the board with no body and no file id, each try
with the round whose plan it sent, the demo's path through check_approval() and approve_at(), and how a live run builds
one.

On the fake sandbox through the loop, with test_loop's runs. The pages over the real Service are test_app_live's.
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
from dataclasses import replace

import pytest

from disputable import approvals, documents, privacy, prompts, rounds, rules
from disputable.airwallex import Outcome
from disputable.clock import ReplayClock, WallClock
from disputable.config import Settings
from disputable.db import APPROVAL_UNREADABLE, FrozenSaved
from disputable.desk import Board, Desk, Mode, ReaderInfo, Result, SendLine
from disputable.lifecycle import Phase, phase
from disputable.loop import DECIDE_ERROR, NO_PASS_HERE, run_pass
from disputable.reader import NoReader
from disputable.seed.fernhill import FERNHILL, MONDAY
from disputable.seed.scripted import Scripted, perfect_for
from disputable.service import BAD_APPROVER, OTHER_READER, Service, live, open_live, send_line
from fake_airwallex import ACCOUNT_ID
from test_loop import INFO, MINUTE, at_step, posted, query
from test_rounds import LATER_DELIVERY, edited

TABLES = ("case_versions", "approvals", "readings", "bases", "uploads", "posts", "tries", "passes", "meta")
VOID_STAGE = "This approval is void: the stage changed from RFI to CHARGEBACK."


def everything(run) -> tuple:
    """What a refusal leaves as it was: every table's rows, and the clock's journal."""
    rows = tuple(query(run.db, f"SELECT * FROM {table} ORDER BY rowid") for table in TABLES)
    return rows, tuple(run.clock.labels())


def with_plan(text: str, **changes) -> str:
    """The posted plan with some of its fields edited, as a hand-edited form would carry it."""
    data = json.loads(text)
    for path, value in changes.items():
        here = data
        *parts, last = path.split("__")
        for part in parts:
            here = here[part]
        here[last] = value
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def other(run, approver="Priya", **options) -> Service:
    """Another Service on the run's database and clock, as a second start of the screens makes one."""
    return Service(db=run.db, store=options.pop("store", FERNHILL), clock=run.clock, info=INFO, approver=approver,
                   **options)


def unreadable_basis(run, dispute_id: str, text: str = "{") -> None:
    """A newest basis row of the case's round that won't load, as a hand edit or a rounds.FORMAT change over an old
    database leaves it. The record and its approvals still load, so the case isn't broken."""
    number = len(run.record(dispute_id).rounds)
    query(run.db, "INSERT INTO bases (dispute_id, round_number, plan_sha256, basis, saved_at) VALUES (?, ?, 'x', ?, ?)",
          dispute_id, number, text, MONDAY.isoformat())


def bug_in_files_for(monkeypatch, error: Exception) -> None:
    """A bug in rounds.files_for()'s own lines, outside the rules: the snapshot's saved basis raises error when
    files_for() itself reads it, and only then. Every other read of it, the page's and current()'s, works."""
    real = FrozenSaved.basis

    def basis(self, dispute_id, round_number):
        if sys._getframe(1).f_code is rounds.files_for.__code__:
            raise error
        return real(self, dispute_id, round_number)

    monkeypatch.setattr(FrozenSaved, "basis", basis)


# ----------------------------------------------------------------------------- the one approval path
def test_an_approval_saves_its_row_and_the_record_at_the_approve_moment(tmp_path):
    run = at_step(tmp_path, "monday")
    run.wall.move(5 * MINUTE)
    text, key = posted(run.board(), "dst_headphones")
    assert run.service.approve("dst_headphones", text, key) == Result(True, "approved")
    row = run.db.latest_approval("dst_headphones", 1)
    record, version = run.db.load_one("dst_headphones")
    plan = record.rounds[-1].plan
    assert run.clock.journal[-1] == ("approve:dst_headphones", run.wall.at)  # the one moment an approval takes
    assert (row.by, row.at, row.reading_key, row.plan_sha256) == ("Priya", run.wall.at, key, approvals.plan_sha256(plan))
    assert (plan.approved_by, plan.approved_at) == ("Priya", run.wall.at) and approvals.verify(record, row) is None
    assert run.db.versions("dst_headphones")[-1][:2] == (version, "approve") and phase(record) is Phase.SEND


def test_a_double_click_saves_one_row(tmp_path):
    run = at_step(tmp_path, "monday")
    text, key = posted(run.board(), "dst_headphones")
    assert run.service.approve("dst_headphones", text, key) == Result(True, "approved")
    before = everything(run)
    assert run.service.approve("dst_headphones", text, key) == Result(False, "already")  # the approval stands
    assert everything(run) == before and len(run.db.approvals("dst_headphones")) == 1


def test_two_clicks_that_overlap_answer_approved_and_already(tmp_path):
    # Both clicks pass every check before either saves, so the second's save meets a Conflict. The case changed by
    # this same approval, so it answers already, as a later click would: one row, and one review booked.
    run = at_step(tmp_path, "monday")
    text, key = posted(run.board(), "dst_headphones")
    real, both = run.db.approve, threading.Barrier(2, timeout=20)

    def checked_then_saved(record, expected, row):
        both.wait()
        return real(record, expected, row)

    run.db.approve = checked_then_saved
    results, start = [], threading.Barrier(2, timeout=20)

    def click():
        start.wait()
        results.append(run.service.approve("dst_headphones", text, key))

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(result.key for result in results) == ["already", "approved"]
    record = run.record("dst_headphones")
    assert len(run.db.approvals("dst_headphones")) == 1 and phase(record) is Phase.SEND
    assert [booking.key for booking in record.bookings if booking.key.startswith("review:")] == ["review:RFI"]


def test_a_conflict_from_another_writer_is_still_stale(tmp_path):
    # Another approver's Service saves first, between this one's checks and its save: that approval isn't this one's.
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    sam, real = other(run, "Sam Example"), run.db.approve

    def sam_saves_first(record, expected, row):
        run.db.approve = real
        assert sam.approve("dst_headphones", shown, key) == Result(True, "approved")
        return real(record, expected, row)

    run.db.approve = sam_saves_first
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "stale")
    assert [row.by for row in run.db.approvals("dst_headphones")] == ["Sam Example"]


def test_the_approver_is_fixed_at_start(tmp_path):
    run = at_step(tmp_path, "monday")
    assert list(inspect.signature(Service.approve).parameters) == ["self", "dispute_id", "plan_text", "reading_key"]
    assert isinstance(run.service, Desk)
    assert other(run, " Sam Example ").approver == "Sam Example"
    for name in ("", "   ", "disputable", " Disputable ", "DISPUTABLE", "x" * 61, "Pri\nya", "Pri\u200bya", 7):
        with pytest.raises(ValueError, match="^" + BAD_APPROVER.replace(".", r"\.") + "$"):
            other(run, name)
    sam = other(run, "Sam Example")
    assert sam.approve("dst_headphones", *posted(sam.board(), "dst_headphones")) == Result(True, "approved")
    assert run.db.latest_approval("dst_headphones", 1).by == "Sam Example"  # the Service's own name, never a form's
    assert run.record("dst_headphones").rounds[-1].plan.approved_by == "Sam Example"


def test_approvals_off(tmp_path):
    run = at_step(tmp_path, "monday")
    off = other(run, None)
    before = everything(run)
    assert off.approve("dst_headphones", *posted(off.board(), "dst_headphones")) == Result(False, "approvals-off")
    assert off.board().approver is None and everything(run) == before


@pytest.mark.parametrize("text", [
    "{", "[]", "null", '"a plan"', "1e999999", "[" * 100_000, '{"terms": {}}',
    "PLAN-WITHOUT-REASONS", "PLAN-WITH-A-BAD-AMOUNT", "PLAN-WITH-AN-EXTRA-FIELD", "PLAN-WITH-A-BAD-STAGE",
], ids=["cut", "a list", "null", "a string", "a huge number", "deep", "a part", "no reasons", "a bad amount",
        "an extra field", "a bad stage"])
def test_a_plan_this_app_cant_read(tmp_path, text):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    data = json.loads(shown)
    if text == "PLAN-WITHOUT-REASONS":
        text = json.dumps({name: value for name, value in data.items() if name != "reasons"})
    elif text == "PLAN-WITH-A-BAD-AMOUNT":
        text = with_plan(shown, terms__amount="lots")
    elif text == "PLAN-WITH-AN-EXTRA-FIELD":
        text = json.dumps({**data, "by": "Someone Else"})
    elif text == "PLAN-WITH-A-BAD-STAGE":
        text = with_plan(shown, terms__stage="ARBITRATION_NOW")
    before = everything(run)
    assert run.service.approve("dst_headphones", text, key) == Result(False, "plan-unreadable")
    assert everything(run) == before


def test_lifecycle_refuses_a_void_or_late_approval_in_its_own_words(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    earbuds, earbuds_key = posted(run.board(), "dst_earbuds")
    before = everything(run)
    for text, why in ((with_plan(shown, terms__amount="1249.00"), "This approval is void: the amount changed."),
                      (with_plan(shown, reasons=["A reason the screen made up."]),
                       "This approval is void: the plan changed after the screen showed it."),
                      (earbuds, "This approval is for another dispute.")):
        assert run.service.approve("dst_headphones", text, key) == Result(False, "refused", why)
    run.wall.set(run.record("dst_headphones").rounds[-1].plan.terms.due_at + MINUTE)
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "refused", "The deadline has passed.")
    assert everything(run) == before  # nothing saved, and no approve moment taken


def test_round_ones_plan_in_round_two(tmp_path):
    run = at_step(tmp_path, "pushback")
    first = run.db.approvals("dst_headphones")[0]  # what Priya approved in round one, as its row keeps it
    assert first.round_number == 1 and phase(run.record("dst_headphones")) is Phase.APPROVE
    before = everything(run)
    result = run.service.approve("dst_headphones", first.plan, first.reading_key)
    assert result == Result(False, "refused", VOID_STAGE) and everything(run) == before
    assert run.service.approve("dst_headphones", *posted(run.board(), "dst_headphones")) == Result(True, "approved")


def test_nothing_waits_once_it_is_approved(tmp_path):
    run = at_step(tmp_path, "approved")
    shown, key = posted(run.board(), "dst_headphones")
    before = everything(run)
    nothing = "Nothing waits for an approval. The case is at SEND."
    assert run.service.approve("dst_headphones", with_plan(shown, reasons=["Another tab's plan."]), key) == Result(
        False, "refused", nothing)  # a second tab with an older page
    ear_tips, ear_tips_key = posted(run.board(), "dst_ear_tips")  # code's own accept: nobody approves it
    assert run.service.approve("dst_ear_tips", ear_tips, ear_tips_key) == Result(False, "refused", nothing)
    assert everything(run) == before


def test_the_reading_beside_the_plan_must_be_currents(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    before = everything(run)
    for wrong in ("", "0" * 64, key.upper()):
        assert run.service.approve("dst_headphones", shown, wrong) == Result(False, "reading-changed")
    assert everything(run) == before
    assert key and run.service.approve("dst_headphones", shown, key) == Result(True, "approved")


def test_a_file_that_cant_be_built_again(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    run.service.store = edited(FERNHILL, LATER_DELIVERY)  # the delivery scan moved since the page was shown
    before = everything(run)
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "file-moved", rounds.MOVED)
    assert everything(run) == before
    earbuds = posted(run.board(), "dst_earbuds")  # an accept sends no file: it doesn't depend on the scan
    assert run.service.approve("dst_earbuds", *earbuds) == Result(True, "approved")


@pytest.mark.parametrize("dispute_id", ["dst_headphones", "dst_earbuds"], ids=["a challenge", "an accept"])
def test_a_plan_whose_saved_basis_cant_be_read_is_refused_and_can_be_dropped(tmp_path, dispute_id):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), dispute_id)  # what the approval page showed before the row went bad
    unreadable_basis(run, dispute_id)
    assert dispute_id not in run.board().broken and phase(run.record(dispute_id)) is Phase.APPROVE
    before = everything(run)
    why = "The saved basis isn't valid JSON."
    checked = run.service.check_approval(dispute_id, shown, key, run.clock.peek())
    assert checked.result == Result(False, "file-moved", why) and everything(run) == before  # never an Unloadable
    assert run.service.approve(dispute_id, shown, key) == Result(False, "file-moved", why)
    assert everything(run) == before  # nothing saved, and no moment taken
    version = run.board().versions[dispute_id]
    assert run.service.drop_moved(dispute_id, version) == Result(True, "dropped")
    record = run.record(dispute_id)
    plain = "its file couldn't be built again exactly as planned" if dispute_id == "dst_headphones" else \
        "what this app saved for it can't be read"  # an accept has no file, so its basis alone can't be read
    assert phase(record) is Phase.DECIDE and record.rounds[-1].notes[-1] == f"The plan was dropped: {plain}."
    report = run.pass_(MINUTE)  # decided again on today's records, with a basis that loads, for a person to approve
    assert dispute_id in report.decided and phase(run.record(dispute_id)) is Phase.APPROVE


def test_rules_that_cant_run_are_no_reason_to_drop(tmp_path, monkeypatch):
    run = at_step(tmp_path, "monday")

    def no_rules(*args, **options):
        raise rules.NoRules("No rule set covers this claim.")

    monkeypatch.setattr(rounds, "current", no_rules)  # no saved row is at fault: the case page shows the rules' line
    assert run.service.drop_moved("dst_earbuds", run.board().versions["dst_earbuds"]) == Result(False, "drop-not-needed")


@pytest.mark.parametrize("error", [documents.NoFileDue("a bug in the rules"), KeyError("a bug in code")],
                         ids=["the rules", "code"])
def test_a_bug_while_the_checks_read_the_round_is_no_reason_to_drop_and_is_logged(tmp_path, monkeypatch, capsys, error):
    # A bug in rounds.current() is one of the other checks: the case page shows it as one and offers no drop for it, so
    # a drop post answers drop-not-needed, never a 500, and the terminal gets the details.
    run = at_step(tmp_path, "monday")

    def bug(*args, **options):
        raise error

    monkeypatch.setattr(rounds, "current", bug)
    before = everything(run)
    assert run.service.drop_moved("dst_earbuds", run.board().versions["dst_earbuds"]) == Result(False, "drop-not-needed")
    assert everything(run) == before
    logged = capsys.readouterr().err
    assert f"Disputable couldn't check the plan of dispute dst_earbuds for a drop, so it stays: {error}" in logged
    assert "Traceback" in logged


def test_a_bug_in_the_rules_while_the_file_is_built_again_lets_the_plan_be_dropped(tmp_path, monkeypatch, capsys):
    # rounds.files_for() counts a bug in the rules as a file that can't be built (REBUILD_ERROR), so a plan that sends a
    # file can be dropped, and deciding the round again hands it to a person. An accept sends no file: only its other
    # checks meet the bug, so it stays, and the terminal gets the details.
    run = at_step(tmp_path, "monday")

    def bug(*args, **options):
        raise KeyError("a bug in the rules")

    monkeypatch.setattr(rules, "evaluate", bug)
    before = everything(run)
    assert run.service.drop_moved("dst_earbuds", run.board().versions["dst_earbuds"]) == Result(False, "drop-not-needed")
    assert everything(run) == before
    assert "Disputable couldn't check the plan of dispute dst_earbuds for a drop, so it stays" in capsys.readouterr().err
    assert run.service.drop_moved("dst_headphones", run.board().versions["dst_headphones"]) == Result(True, "dropped")
    record = run.record("dst_headphones")
    assert phase(record) is Phase.DECIDE and record.rounds[-1].notes[-1] == (
        "The plan was dropped: its file couldn't be built again exactly as planned.")
    report = run.pass_(MINUTE)  # the bug is still there when the round is decided again
    assert DECIDE_ERROR.format(who="Order #1109") in report.problems
    assert phase(run.record("dst_headphones")) is Phase.PERSON


def test_a_bug_in_files_fors_own_lines_is_no_reason_to_drop_and_is_logged(tmp_path, monkeypatch, capsys):
    # Outside the rules, files_for() turns no bug into a file that can't be built: such a bug is one of the other
    # checks, so the plan stays, never an error, and the terminal gets the details.
    run = at_step(tmp_path, "monday")
    error = TypeError("a bug in code outside the rules")
    bug_in_files_for(monkeypatch, error)
    before = everything(run)
    version = run.board().versions["dst_headphones"]
    assert run.service.drop_moved("dst_headphones", version) == Result(False, "drop-not-needed")
    assert everything(run) == before and phase(run.record("dst_headphones")) is Phase.APPROVE
    logged = capsys.readouterr().err
    assert f"Disputable couldn't check the plan of dispute dst_headphones for a drop, so it stays: {error}" in logged
    assert "Traceback" in logged
    monkeypatch.undo()  # so the check above means something: without the bug, the file builds and nothing is logged
    assert run.service.drop_moved("dst_headphones", version) == Result(False, "drop-not-needed")
    assert capsys.readouterr().err == ""


def test_a_drop_still_needs_a_reason_once_the_basis_reads_again(tmp_path):
    run = at_step(tmp_path, "monday")
    version = run.board().versions["dst_earbuds"]
    assert run.service.drop_moved("dst_earbuds", version) == Result(False, "drop-not-needed")  # every row loads
    good = query(run.db, "SELECT basis FROM bases WHERE dispute_id = 'dst_earbuds' ORDER BY id DESC LIMIT 1")[0][0]
    unreadable_basis(run, "dst_earbuds")
    unreadable_basis(run, "dst_earbuds", good)  # a newer row that loads: the newest one is what counts
    assert run.service.drop_moved("dst_earbuds", version) == Result(False, "drop-not-needed")


def test_a_case_that_changed_after_the_checks_is_stale(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    real = run.db.approve

    def a_pass_saves_first(record, expected, row):  # a pass saves a new reading between the checks and the save
        head, version = run.db.load_one(record.dispute_id)
        run.db.save(replace(head, blocked=None), version, "poll")
        return real(record, expected, row)

    run.db.approve = a_pass_saves_first
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "stale")
    assert run.db.approvals("dst_headphones") == [] and run.db.versions("dst_headphones")[-1][1] == "poll"
    assert run.clock.labels()[-1] == "approve:dst_headphones"  # the moment was taken: the checks had passed


def test_an_approval_that_wouldnt_send_is_not_recorded(tmp_path, monkeypatch, capsys):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    monkeypatch.setattr(approvals, "verify", lambda record, row: "The plan changed after it was approved.")
    before = everything(run)
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "not-recorded")
    assert everything(run) == before
    assert "couldn't record an approval of dispute dst_headphones: The plan changed after it was approved." in \
        capsys.readouterr().err


def test_no_case_and_a_case_that_cant_be_read(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    earbuds = posted(run.board(), "dst_earbuds")
    assert run.service.approve("dst_nothing_here", shown, key) == Result(False, "no-case")
    query(run.db, "INSERT INTO approvals (dispute_id, round_number, plan_sha256, approval, saved_at) VALUES "
                  "('dst_headphones', 1, 'x', '{', ?)", MONDAY.isoformat())  # a row of the case that won't load
    query(run.db, "INSERT INTO case_versions (dispute_id, version, record, why, saved_at) VALUES "
                  "('dst_earbuds', 3, '[]', 'poll', ?)", MONDAY.isoformat())  # a newest record that won't load
    before = everything(run)
    assert run.service.approve("dst_headphones", shown, key) == Result(False, "broken", APPROVAL_UNREADABLE)
    assert run.service.approve("dst_earbuds", *earbuds) == Result(False, "broken", "This isn't a format 1 case record.")
    assert run.board().broken == {"dst_headphones": APPROVAL_UNREADABLE, "dst_earbuds": "This isn't a format 1 case "
                                                                                        "record."}
    assert everything(run) == before


# ----------------------------------------------------------------------------- the checks alone, and the demo's path
def test_check_approval_saves_nothing(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    before = everything(run)
    checked = run.service.check_approval("dst_headphones", shown, key, run.clock.peek())
    assert checked.result == Result(True, "approved") and everything(run) == before
    record, version = run.db.load_one("dst_headphones")
    assert (checked.record, checked.version) == (record, version)
    assert checked.plan_sha256 == approvals.plan_sha256(record.rounds[-1].plan) == checked.row.plan_sha256
    assert checked.approved.rounds[-1].plan.approved_by == "Priya"
    with pytest.raises(RuntimeError):  # the snapshot it checks on can't save
        run.board().saved.put_basis(run.db.saved.basis("dst_headphones", 1))


def test_the_demo_approves_at_the_recorded_moment(tmp_path):
    run = at_step(tmp_path, "monday")
    shown, key = posted(run.board(), "dst_headphones")
    recorded = run.wall.at + 5 * MINUTE
    clock = ReplayClock(run.wall.at)
    clock.load([("approve:dst_headphones", recorded)], at=run.wall.at)
    demo = Service(db=run.db, store=FERNHILL, clock=clock, info=INFO, approver="Priya", mode=Mode.DEMO)
    assert demo.approve("dst_headphones", shown, key) == Result(False, "not-in-recording")  # only the Demo's beats
    assert demo.check_approval("dst_headphones", shown, key, clock.peek()).result.ok and clock.left() == 1
    assert demo.approve_at("dst_headphones", shown, key, clock.now("approve:dst_headphones")) == Result(True, "approved")
    clock.done()
    assert run.db.latest_approval("dst_headphones", 1).at == recorded
    assert demo.drop_moved("dst_headphones", 1) == Result(False, "live-only")
    assert (demo.demo_next(0), demo.demo_restart("1")) == (Result(False, "demo-only"), Result(False, "demo-only"))
    assert (run.service.demo_next(3), run.service.demo_restart("2")) == (Result(False, "demo-only"),) * 2


# ----------------------------------------------------------------------------- dropping a plan whose file moved
def test_drop_moved(tmp_path):
    run = at_step(tmp_path, "approved")
    version = run.board().versions["dst_headphones"]
    assert run.service.drop_moved("dst_headphones", version) == Result(False, "drop-not-needed")  # the file still builds
    assert run.service.drop_moved("dst_earbuds", run.board().versions["dst_earbuds"]) == Result(
        False, "drop-not-needed")  # an accept sends no file
    run.service.store = edited(FERNHILL, LATER_DELIVERY)
    assert run.service.drop_moved("dst_headphones", version - 1) == Result(False, "stale")
    assert run.service.drop_moved("dst_nothing_here", 1) == Result(False, "no-case")
    before = everything(run)
    assert run.service.drop_moved("dst_headphones", version) == Result(True, "dropped")
    assert everything(run)[1] == before[1]  # no moment taken: a drop isn't an approval
    headphones = run.record("dst_headphones")
    assert phase(headphones) is Phase.DECIDE and run.db.versions("dst_headphones")[-1][1] == "drop"
    assert headphones.rounds[-1].notes[-1] == "The plan was dropped: its file couldn't be built again exactly as planned."
    report = run.pass_(MINUTE)  # decided again on today's records, for a person to approve
    assert "dst_headphones" in report.decided and phase(run.record("dst_headphones")) is Phase.APPROVE
    run.approve_all()
    run.pass_(MINUTE)
    sent = run.board().versions["dst_headphones"]
    assert run.service.drop_moved("dst_headphones", sent) == Result(False, "refused",
                                                                    "The plan can't be dropped now. It is at SENT.")


# ----------------------------------------------------------------------------- the board
def test_the_board_is_one_snapshot_with_no_body_and_no_file_id(tmp_path):
    run = at_step(tmp_path, "approved")
    report = run.pass_(2 * MINUTE)  # the challenge goes out with its file, whose id carries the account id
    assert [outcome for _, outcome in report.sent] == [Outcome.TAKEN] * 3
    plan = run.record("dst_headphones").rounds[-1].plan
    (sha256,) = plan.terms.file_hashes
    file_id = run.db.upload_for(plan.request_id, sha256).file_id
    assert privacy.account_id_of(file_id) == ACCOUNT_ID  # so the checks below mean something
    board = run.board()
    assert isinstance(board, Board) and board.mode is Mode.LIVE and board.demo is None and board.approver == "Priya"
    assert board.now == run.clock.peek() and board.reader == INFO
    assert board.problems == report.problems and board.last_pass == report.finished_at
    assert dict(board.versions) == run.db.load().versions and set(board.records) == set(board.versions)
    answered = dict(run.clock.journal)["answer:dst_headphones"]
    assert board.sends["dst_headphones"] == (SendLine(answered, "taken", 200, "Airwallex took it", 1),)
    text = repr(board)
    found = [token for token in privacy.base64_tokens(text) if privacy.SEPARATOR in (privacy.decode_base64(token) or "")]
    assert found == [] and file_id not in text and ACCOUNT_ID not in text and "supporting_documents" not in text
    for dispute_id in ("dst_ear_tips", "dst_earbuds", "dst_headphones"):
        assert run.db.post_for(run.record(dispute_id).rounds[-1].plan.request_id).body not in text
    basis = run.db.saved.basis("dst_headphones", 1)
    with pytest.raises(RuntimeError):  # a page's snapshot can't save
        board.saved.put_reading("dst_headphones", 1, run.db.saved.reading("dst_headphones", 1, basis.reading_key))


def test_each_try_names_the_round_whose_plan_it_sent(tmp_path):
    # Every round's plan has its own request id, and every try of a plan sends that id: so a try names its round.
    run = at_step(tmp_path, "pushback")
    record = run.record("dst_headphones")
    first, second = (here.plan.request_id for here in record.rounds)
    (sent,) = run.db.tries(first)  # round one's challenge, which Airwallex took on Monday
    assert send_line(sent, record) == SendLine(sent.at, "taken", 200, "Airwallex took it", 1)
    assert run.board().sends["dst_headphones"] == (send_line(sent, record),)
    assert send_line(replace(sent, request_id=second), record).round_number == 2
    assert send_line(replace(sent, request_id="no-plans-request-id"), record).round_number is None  # no round's plan
    assert send_line(sent, None).round_number is None  # a case with no record that loads


# ----------------------------------------------------------------------------- a live run's Service
def settings(tmp_path, anthropic_api_key=None):
    return Settings(airwallex_client_id=None, airwallex_api_key=None, anthropic_api_key=anthropic_api_key,
                    base_url="https://api.sandbox.airwallex.com", files_url="https://files.sandbox.airwallex.com",
                    data_dir=tmp_path)


def test_live_builds_the_service_of_the_data_folder(tmp_path):
    service = live(settings(tmp_path), approver="Priya", store=FERNHILL)
    assert service.db.path == tmp_path / "live.sqlite3" and service.lock.path == tmp_path / "pass.lock"
    assert isinstance(service.clock, WallClock) and service.mode is Mode.LIVE and service.approver == "Priya"
    assert service.info == ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "none", "", None, None)
    assert (service.reader, service.client) == (None, None)  # serve: no reader, no client, so no pass
    with pytest.raises(ValueError, match="^" + NO_PASS_HERE.replace(".", r"\.") + "$"):
        run_pass(service)
    assert service.board().records == {} and service.board().last_pass is None
    keyed = live(settings(tmp_path, "sk-ant-made-up-key-0123456789"), approver=None, store=FERNHILL, reader=NoReader())
    assert keyed.info == ReaderInfo(prompts.FERNHILL, prompts.EFFORT, "claude", prompts.MODEL, None, None)
    assert "sk-ant-made-up-key" not in repr(keyed.__dict__) + repr(keyed.board())
    assert open_live(settings(tmp_path)).meta("created_at") is not None


def test_a_reader_that_keys_otherwise_is_refused(tmp_path):
    run = at_step(tmp_path, "monday")
    with pytest.raises(ValueError, match="profile or effort"):
        other(run, reader=Scripted(perfect_for, effort="medium"))
    assert OTHER_READER.startswith("The reader's profile or effort isn't the one the screens show")
    assert other(run, reader=NoReader()).reader is not None  # a reader with no profile keys as the default one does
