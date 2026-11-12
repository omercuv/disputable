"""The demo: a recorded story replayed as the screens' Desk, with no keys and no network.

Over the recordings tests/recordings.py makes on the fake sandbox once a session: Next plays one beat at a time, and a
stale or doubled Next plays nothing, two threads at once included; an approval from the approval page runs the Service's
checks first, which save nothing and use no recorded moment, then plays its beat at the recorded moment, in any order
inside the pending window, and a double click on Approve plays it once; an approval the recording didn't make is
refused; a reading the recording doesn't hold stops the demo loudly; play_all() reaches the manifest's totals for both
endings, with each pass's alerts as they stand once it ended; a restart builds everything again, and waits for a beat
that plays; another ending that can't be replayed is
left out and named, never stopping this one; the bar says what answered for Airwallex as each ending's manifest says,
the sandbox or the stand-in maker's stand-in; and demo.py never imports the seed, the bank, the story, the eval or the
screens.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import re
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx
import pytest

import recordings
from disputable import approvals, desk, lifecycle, recording, story
from disputable.clock import ReplayMismatch
from disputable.demo import Demo
from disputable.readings_file import ReadingsFile
from disputable.recording import RecordingError, load
from disputable.web import views
from test_loop import posted, query
from test_recording import ended
from test_service import TABLES, with_plan

OPENS = ("The bank opens three disputes: the ear tips ($12, due in 2 days), the earbuds ($189, due in 4 days) and the "
         "headphones ($1,249, due in 6 days).")
EARBUDS_TEXT = "Priya approves code's draft on order #1098: accept $189.00, no refund."
HEADPHONES_TEXT = "Priya approves the challenge on order #1109."
NO_READING = "Order #1098: the recording holds no reading for this case as the code reads it now."
OTHER_PLAN = "Order #1098: the plan to approve isn't the one Priya approved in the recording."


def _no_network(self, request):
    raise AssertionError("The demo went to the network.")


@pytest.fixture(autouse=True)
def offline():
    """No httpx.HTTPTransport may answer anything while a demo plays: each answer comes from the recording."""
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        yield


@pytest.fixture
def make():
    """Demos over the fake sandbox's recordings (or the folders given), each closed when the test ends."""
    made = []

    def build(ending="1", folders=None):
        demo = Demo(recordings.recorded() if folders is None else folders, store_for=story.STORES.__getitem__,
                    ending=ending)
        made.append(demo)
        return demo

    yield build
    for demo in made:
        demo.close()


def cases(ending="1") -> dict[str, str]:
    """seed case id -> dispute id, from the recording's bank step on Monday."""
    return {opened.case_id: opened.dispute_id for opened in load(recordings.recorded()[ending]).beats[0].dates}


def everything(demo: Demo) -> tuple:
    """What a refusal leaves as it was: every table's rows, the beats played and the replay clock."""
    replay = demo.replay
    rows = tuple(query(replay.service.db, f"SELECT * FROM {table} ORDER BY rowid") for table in TABLES)
    return rows, replay.played, replay.clock.peek(), replay.clock.left()


def press(demo: Demo, times: int) -> None:
    for _ in range(times):
        assert demo.demo_next(len(demo.board().demo.played)) == desk.Result(True, "next-played")


def doctored(tmp_path: Path, change) -> dict[str, Path]:
    """Ending 1's recording, copied and changed: change(recording) gives the recording to save in its place."""
    folder = tmp_path / "ending-1"
    recording.copy(recordings.recorded()["1"], folder)
    recording.save(change(load(folder)), folder)
    return {"1": folder}


# ----------------------------------------------------------------------------- the start and Next
def test_the_demo_starts_on_monday_before_the_bank(make):
    demo = make()
    assert isinstance(demo, desk.Desk)
    board, manifest = demo.board(), load(recordings.recorded()["1"]).manifest
    assert board.mode is desk.Mode.DEMO and board.approver == "Priya" and dict(board.records) == {}
    assert board.now == load(recordings.recorded()["1"]).beats[0].at  # the recording's clock, never the wall's
    assert board.demo == desk.DemoBar(
        ending="1", endings=("1", "2"), recorded_on=manifest.recorded_at.date(), played=(),
        coming=desk.Beat("Monday 9:00", OPENS, "bank"), pending=(), approver="Priya", mismatch=None, finished=False,
        count=13, airwallex="sandbox", total=None, left_unused=0)
    assert board.reader.source == "claude" and board.reader.first_read == manifest.first_read
    assert demo.drop_moved(cases()["headphones"], 1) == desk.Result(False, "live-only")


def test_the_bar_says_what_answered_for_airwallex_as_each_endings_manifest_says(make):
    """The stand-in maker's recordings say "stand-in" at every step, and a demo of both kinds says each ending's own,
    a restart included."""
    stand_in, sandbox = recordings.standins().folders(), recordings.recorded()
    demo = make("2", stand_in)
    kinds = [demo.board().demo.airwallex]
    while demo.replay.unplayed():
        press(demo, 1)
        kinds.append(demo.board().demo.airwallex)
    bar = demo.board().demo
    assert kinds == ["stand-in"] * 12 and bar.endings == ("1", "2")  # 11 beats, and the start
    assert (bar.finished, bar.total) == (True, Decimal("1528.30"))  # with no Claude reading, Ending 2 costs $1,528.30
    assert demo.replay.totals()[0] == Decimal("1528.30")
    assert demo.demo_restart("1") == desk.Result(True, "restarted") and demo.board().demo.airwallex == "stand-in"
    mixed = make("1", {"1": sandbox["1"], "2": stand_in["2"]})
    assert mixed.board().demo.airwallex == "sandbox"
    assert mixed.demo_restart("2") == desk.Result(True, "restarted") and mixed.board().demo.airwallex == "stand-in"
    assert mixed.demo_restart("1") == desk.Result(True, "restarted") and mixed.board().demo.airwallex == "sandbox"


def test_next_plays_one_beat_and_a_stale_count_plays_nothing(make):
    demo, ids = make(), cases()
    assert demo.demo_next(0) == desk.Result(True, "next-played")
    for stale in (0, 2, 99, -1, True):
        assert demo.demo_next(stale) == desk.Result(False, "demo-moved")
    assert demo.replay.played == (1,)
    assert demo.demo_next(1) == desk.Result(True, "next-played")  # Monday's first pass
    board = demo.board()
    records = board.records
    assert sum((lifecycle.at_risk(record) for record in records.values()), Decimal(0)) == Decimal("1450.00")
    assert [(alert.order_number, alert.urgency.value) for alert in approvals.alerts(records.values(), board.now)] == [
        ("1121", "SOON")]
    bar = board.demo
    assert [beat.kind for beat in bar.played] == ["bank", "pass"]
    assert bar.pending == ((ids["earbuds"], "Order #1098"), (ids["headphones"], "Order #1109"))
    assert bar.coming == desk.Beat("Monday 9:05", EARBUDS_TEXT, "approve")
    shown = views.demo_bar(bar, "a-made-up-form-token")
    assert [line.text for line in shown.lines][:2] == ["Step 2 of 13: Monday 9:00.", "The agent reads the queue."]
    assert shown.buttons[0].fields[1] == ("played", "2")


def test_next_plays_the_recordings_approvals_as_its_approver_at_the_recorded_moment(make):
    demo, ids = make(), cases()
    press(demo, 3)
    beat = demo.replay.beats[2]
    (row,) = demo.replay.service.db.approvals(ids["earbuds"])
    assert (row.by, row.at, row.plan_sha256) == ("Priya", beat.clock[0][1], beat.plan_sha256)
    assert demo.board().demo.pending == ((ids["headphones"], "Order #1109"),)


def test_two_threads_pressing_next_at_once_play_one_beat(make, monkeypatch):
    demo = make()
    replay, played, inside = demo.replay, demo.replay.play, threading.Event()

    def slow(beat, **posted_fields):  # a beat that takes a while, so the second click comes while it plays
        calls.append(beat.n)
        inside.set()
        time.sleep(0.2)
        return played(beat, **posted_fields)

    calls, results, start = [], [], threading.Barrier(2)
    monkeypatch.setattr(replay, "play", slow)

    def click():
        start.wait()
        results.append(demo.demo_next(0))

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert inside.wait(10)
    board = demo.board()  # a page read waits for the beat too, and shows it played
    for thread in threads:
        thread.join(10)
    assert sorted(result.key for result in results) == ["demo-moved", "next-played"]
    assert calls == [1] and replay.played == (1,) and len(board.demo.played) == 1


def slowed(monkeypatch, replay):
    """The replay's beats, each taking 0.2 s, so a second click comes while one plays. Set once one has begun."""
    real, inside = replay.play, threading.Event()

    def slow(beat, **posted_fields):
        inside.set()
        time.sleep(0.2)
        return real(beat, **posted_fields)

    monkeypatch.setattr(replay, "play", slow)
    return inside


def at_once(*calls) -> tuple[list, list]:
    """Each call in a thread of its own, started together: (name, Result) in the order they ended, and any error."""
    ended, errors, start = [], [], threading.Barrier(len(calls))

    def run(name, call):
        start.wait()
        try:
            ended.append((name, call()))
        except Exception as error:  # a bug: the test fails on it
            errors.append(error)

    threads = [threading.Thread(target=run, args=(name, call)) for name, call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    return ended, errors


def test_two_approvals_of_one_plan_at_once_play_its_beat_once(make, monkeypatch):
    """A double click on Approve: the second waits for the first, then finds the plan approved."""
    demo, ids = make(), cases()
    press(demo, 2)
    replay, posted_now = demo.replay, posted(demo.board(), ids["earbuds"])
    slowed(monkeypatch, replay)
    ended, errors = at_once(*(("approve", lambda: demo.approve(ids["earbuds"], *posted_now)) for _ in range(2)))
    assert errors == [] and sorted(result.key for _, result in ended) == ["already", "approved"]
    assert demo.mismatch is None and replay.played == (1, 2, 3)
    assert len(replay.service.db.approvals(ids["earbuds"])) == 1


def test_a_restart_waits_for_the_beat_that_plays(make, monkeypatch):
    """Next and a restart pressed at once: the beat plays whole on the replay it began on, then the story starts again."""
    demo = make()
    press(demo, 2)
    replay = demo.replay
    inside = slowed(monkeypatch, replay)

    def restart():
        assert inside.wait(10)  # while Next's beat plays
        return demo.demo_restart("1")

    ended, errors = at_once(("next", lambda: demo.demo_next(2)), ("restart", restart))
    assert errors == [] and ended == [("next", desk.Result(True, "next-played")),
                                      ("restart", desk.Result(True, "restarted"))]  # the restart waited for the beat
    assert replay.played == (1, 2, 3) and demo.replay is not replay
    assert demo.replay.played == () and demo.mismatch is None and demo.board().demo.played == ()


# ----------------------------------------------------------------------------- approvals from the approval page
def test_approvals_from_the_page_play_in_any_order_inside_the_window(make):
    demo, ids = make(), cases()
    press(demo, 2)
    later, sooner = demo.replay.beats[3], demo.replay.beats[2]  # the headphones' approval, then the earbuds'
    assert demo.approve(ids["headphones"], *posted(demo.board(), ids["headphones"])) == desk.Result(True, "approved")
    (row,) = demo.replay.service.db.approvals(ids["headphones"])
    assert (row.by, row.at) == ("Priya", later.clock[0][1])  # its own recorded moment, though played first
    bar = demo.board().demo
    assert demo.replay.played == (1, 2, 4) and bar.played[-1] == desk.Beat("Monday 9:05", HEADPHONES_TEXT, "approve")
    assert bar.pending == ((ids["earbuds"], "Order #1098"),) and bar.coming.text == EARBUDS_TEXT
    assert demo.approve(ids["earbuds"], *posted(demo.board(), ids["earbuds"])) == desk.Result(True, "approved")
    assert demo.replay.played == (1, 2, 4, 3) and demo.board().demo.pending == ()
    assert demo.replay.service.db.approvals(ids["earbuds"])[0].at == sooner.clock[0][1]
    demo.play_all()
    straight, _ = ended("1")  # the recording replayed in its own order, once a session
    assert recording.differences(straight.state(), demo.replay.state()) == []  # the same story, to the last row
    assert demo.replay.totals()[0] == Decimal("279.30") and demo.replay.transport.left() == []


def test_a_refused_approval_saves_nothing_and_uses_no_recorded_moment(make):
    demo, ids = make(), cases()
    press(demo, 2)
    before = everything(demo)
    text, key = posted(demo.board(), ids["earbuds"])
    refusals = [
        (demo.approve(ids["earbuds"], text, "0" * 64), "reading-changed"),
        (demo.approve(ids["earbuds"], with_plan(text, reasons=["What an older tab showed."]), key), "refused"),
        (demo.approve(ids["earbuds"], "{not a plan", key), "plan-unreadable"),
        (demo.approve("dst_nothing_here", text, key), "no-case"),
        (demo.approve(ids["ear_tips"], *posted(demo.board(), ids["ear_tips"])), "refused"),  # code's own: no approval
    ]
    assert [(result.ok, result.key) for result, _ in refusals] == [(False, key) for _, key in refusals]
    assert refusals[1][0].message == "This approval is void: the plan changed after the screen showed it."
    assert refusals[4][0].message == "Nothing waits for an approval. The case is at SEND."
    assert everything(demo) == before  # no row, no beat, and the approval's recorded moment still unused
    assert demo.approve(ids["earbuds"], text, key) == desk.Result(True, "approved")  # the beat still plays
    again = everything(demo)
    assert demo.approve(ids["earbuds"], text, key) == desk.Result(False, "already")  # a double click: one row
    assert everything(demo) == again and len(demo.replay.service.db.approvals(ids["earbuds"])) == 1


def test_an_approval_the_recording_didnt_make_is_refused_and_next_says_why(tmp_path, make):
    def other_plan(recorded):
        beats = list(recorded.beats)
        beats[2] = dataclasses.replace(beats[2], plan_sha256="0" * 64)  # as if Priya had approved another plan
        return dataclasses.replace(recorded, beats=tuple(beats))

    demo, ids = make(folders=doctored(tmp_path, other_plan)), cases()
    press(demo, 2)
    before = everything(demo)
    assert demo.approve(ids["earbuds"], *posted(demo.board(), ids["earbuds"])) == desk.Result(False, "not-in-recording")
    assert everything(demo) == before
    assert demo.demo_next(2) == desk.Result(False, "demo-mismatch", OTHER_PLAN)  # Next plays it, and it isn't the same
    bar = demo.board().demo
    assert (bar.mismatch, bar.pending, bar.finished) == (OTHER_PLAN, (), False)
    assert demo.demo_next(2) == desk.Result(False, "demo-mismatch", OTHER_PLAN)
    assert demo.demo_next(1) == desk.Result(False, "demo-moved")
    assert demo.approve(ids["headphones"], *posted(demo.board(), ids["headphones"])) == desk.Result(
        False, "demo-mismatch", OTHER_PLAN)
    assert demo.replay.service.db.approvals(ids["headphones"]) == []


# ----------------------------------------------------------------------------- mismatches, the end and restarts
def test_a_reading_the_recording_doesnt_hold_stops_the_demo_loudly(tmp_path, make):
    connection = sqlite3.connect(recordings.stories()["1"].database)  # the live run's own database, read only
    try:
        (key,), = connection.execute("SELECT key FROM readings WHERE dispute_id = ?", (cases()["earbuds"],)).fetchall()
    finally:
        connection.close()

    def fewer(recorded):
        readings = recorded.readings
        kept = ReadingsFile(readings.header, {k: r for k, r in readings.readings.items() if k != key},
                            {k: d for k, d in readings.dates.items() if k != key})
        manifest = dataclasses.replace(recorded.manifest, readings=len(kept.readings))
        return dataclasses.replace(recorded, manifest=manifest, readings=kept)

    demo = make(folders=doctored(tmp_path, fewer))
    assert demo.demo_next(0).key == "next-played"
    assert demo.demo_next(1) == desk.Result(False, "demo-mismatch", NO_READING)  # Monday's pass reads the earbuds
    bar = demo.board().demo
    assert bar.mismatch == NO_READING and demo.mismatch == NO_READING
    shown = views.demo_bar(bar, "a-made-up-form-token")
    assert [form.action for form in shown.buttons] == ["/demo/restart"]  # no Next once the replay stopped matching
    with pytest.raises(ReplayMismatch, match=NO_READING):
        demo.play_all()
    assert demo.demo_restart("1") == desk.Result(True, "restarted") and demo.board().demo.mismatch is None


@pytest.mark.parametrize("ending, total, count", [("1", "279.30", 13), ("2", "1512.30", 11)])
def test_play_all_reaches_the_manifests_totals(make, ending, total, count):
    demo, said = make(ending), []
    lines = demo.play_all(say=said.append)
    assert said == lines
    assert lines[0] == f"Step 1 of {count}, Monday 9:00: {OPENS}"
    assert lines[1:4] == [f"Step 2 of {count}, Monday 9:00: The agent reads the queue.", "  Read 3, sent 0, decided 3.",
                          "  Alert: Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. It needs no approval and is ready "
                          "to send."]  # as it stands once the pass ended: the pass made the ear tips' plan
    assert not [line for line in lines if line.startswith("  Problem:")]
    assert lines[-3:] == [f"The story ends. The replay cost ${Decimal(total):,}.",
                          f"The recording's own total for this ending: ${Decimal(total):,}.",
                          "Still at risk: $0.00. Disputes closed: 3."]
    manifest = demo.replay.recording.manifest
    bar = demo.board().demo
    assert (bar.finished, bar.coming, bar.total, bar.left_unused, len(bar.played)) == (
        True, None, manifest.total, 0, count) and manifest.total == Decimal(total)
    assert demo.replay.totals() == (manifest.total, manifest.at_risk, manifest.closed)
    assert demo.demo_next(count) == desk.Result(False, "demo-finished")
    assert demo.play_all() == lines[-3:]  # nothing left to play: only the totals


def under_each_step(lines: list[str]) -> dict[int, list[str]]:
    """play_all()'s lines under each step's own line, by the step's number: a pass's counts, problems and alerts."""
    found: dict[int, list[str]] = {}
    for line in lines:
        step = re.match(r"Step (\d+) of \d+, ", line)
        if step is not None:
            found[int(step.group(1))] = []
        elif line.startswith("  "):
            found[max(found)].append(line)
    return found


@pytest.mark.parametrize("ending", ["1", "2"])
def test_play_all_says_each_passs_alerts_as_they_stand_once_it_ended(make, ending):
    """Under each pass, the alerts over the replay's records right after it, at the moment it ended: its pass:end, which
    peek() then shows. So the first pass's alert says the plan it made, and the pass that sent a case shows no alert for
    it. The pass's report keeps the alerts from its start, as the database does."""
    lines = make(ending).play_all()
    alerts = {n: [line for line in below if line.startswith("  Alert: ")] for n, below in under_each_step(lines).items()}
    replay, expected, kept = recordings.replayed(recordings.recorded()[ending]), {}, {}
    for beat in replay.beats:
        played, expected[beat.n] = replay.play(beat), []
        if played.report is not None:
            assert replay.clock.peek() == played.report.finished_at  # the pass:end moment
            after = approvals.alerts(replay.service.db.load().records.values(), replay.clock.peek(), replay.service.fees)
            expected[beat.n] = [f"  Alert: {alert.message}" for alert in after]
            kept[beat.n] = [alert.message for alert in played.report.alerts]
    assert alerts == expected and sorted(alerts) == [beat.n for beat in replay.beats]
    assert alerts[2] == ["  Alert: Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. It needs no approval and is ready "
                         "to send."]
    assert kept[2] == ["Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. Code hasn't made a plan yet."]
    assert [beat.text for beat in replay.beats[4:6]] == [story.SENDS, story.READS]  # step 5: Monday's sending pass
    assert not [line for line in alerts[5] if "Order #1121" in line] and kept[5]  # it sent the ear tips' accept


def test_a_restart_builds_everything_again(make):
    demo = make()
    press(demo, 5)
    old = Path(demo.replay.service.db.path).parent
    assert demo.demo_restart("3") == desk.Result(False, "no-ending") and demo.replay.played == (1, 2, 3, 4, 5)
    assert demo.demo_restart("2") == desk.Result(True, "restarted")
    board = demo.board()
    assert (demo.ending, board.demo.ending, board.demo.played, board.demo.count, dict(board.records)) == (
        "2", "2", (), 11, {})
    assert board.store is story.STORES["ending_2"] and board.now == load(recordings.recorded()["2"]).beats[0].at
    assert Path(demo.replay.service.db.path).parent != old  # a fresh database, in a folder of its own
    lines = demo.play_all()
    assert lines[-2] == "The recording's own total for this ending: $1,512.30."
    assert demo.demo_restart("1") == desk.Result(True, "restarted")
    assert demo.board().demo.played == () and demo.board().demo.ending == "1"
    press(demo, 2)
    assert len(demo.board().records) == 3


def test_a_demo_needs_a_recording_of_the_ending_it_plays(tmp_path):
    folders = recordings.recorded()
    with pytest.raises(RecordingError, match="There's no recording to replay"):
        Demo({}, store_for=story.STORES.__getitem__)
    with pytest.raises(RecordingError, match="is of Ending 1, not Ending 2"):
        Demo({"2": folders["1"]}, store_for=story.STORES.__getitem__, ending="2")
    with pytest.raises(RecordingError, match="There's no recording of Ending 1"):
        Demo({"2": folders["2"]}, store_for=story.STORES.__getitem__)
    with pytest.raises(RecordingError, match="There's no recording of Ending 2"):
        Demo({"1": folders["1"]}, store_for=story.STORES.__getitem__, ending="2")
    with pytest.raises(RecordingError) as empty:
        Demo({"1": tmp_path}, store_for=story.STORES.__getitem__)
    assert str(empty.value) == f"The recording in {tmp_path} can't be replayed. It has no manifest.json."
    one = Demo({"1": folders["1"]}, store_for=story.STORES.__getitem__)
    try:
        assert one.board().demo.endings == ("1",) and one.demo_restart("2") == desk.Result(False, "no-ending")
        assert one.warnings == ()
    finally:
        one.close()


def test_an_other_ending_that_cant_be_replayed_is_left_out_and_named(tmp_path, make):
    """A stale or half-copied recording of the other ending never stops the demo of this one: the bar doesn't offer it,
    and the line names its folder, so the right ending is recorded again."""
    stale = tmp_path / "ending-2"
    recording.copy(recordings.recorded()["2"], stale)
    manifest = json.loads((stale / recording.MANIFEST).read_text(encoding="ascii"))
    manifest["formats"]["prompt"] = "reader-0"  # recorded by other code
    (stale / recording.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="ascii", newline="\n")
    demo = make(folders={"1": recordings.recorded()["1"], "2": stale})
    assert demo.board().demo.endings == ("1",) and demo.demo_restart("2") == desk.Result(False, "no-ending")
    assert demo.warnings == (f"Ending 2 can't be offered. The recording in {stale} can't be replayed. This recording was "
                             "made by other code: prompt changed since. Record the story again.",)
    half = tmp_path / "half" / "ending-2"
    recording.copy(recordings.recorded()["2"], half)
    (half / recording.BEATS).unlink()  # a copy that stopped half way
    other = make(folders={"1": recordings.recorded()["1"], "2": half, "3": recordings.recorded()["1"]})
    assert other.board().demo.endings == ("1",)
    assert other.warnings == (
        f"Ending 2 can't be offered. The recording in {half} can't be replayed. It has no beats.json.",
        f"Ending 3 can't be offered. The recording in {recordings.recorded()['1']} is of Ending 1, not Ending 3.")
    assert other.play_all()[-3] == "The story ends. The replay cost $279.30."
    with pytest.raises(RecordingError, match=re.escape(f"The recording in {stale} can't be replayed.")):
        make(folders={"1": recordings.recorded()["1"], "2": stale}, ending="2")  # the ending asked for: refused


# ----------------------------------------------------------------------------- the core and the rest
def test_the_demo_never_imports_the_seed_the_bank_the_story_the_eval_or_the_screens():
    tree = ast.parse(Path(importlib.util.find_spec("disputable.demo").origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "disputable.recording" in names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web",
                   "disputable.cli"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], banned
