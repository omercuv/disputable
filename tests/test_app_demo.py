"""The web app over the demo: create_app(Demo) on Ending 1 and Ending 2 recorded on the fake sandbox, driven through
TestClient as a judge drives it in a browser.

Before Next the queue is empty under the demo banner. Next, pressed through the bar's own form, plays the recording's
beats in order. On Monday $1,450.00 is at risk and only the ear tips alert. Priya's two Monday approvals go through the
approval pages' own forms in reverse order, the headphones first, each at its own recorded moment: the pending window.
They share one story time, so the story's clock never goes back.
Next plays Ending 1 to its end, and the money page shows the replayed total beside the manifest's, $279.30. The bar's
restart form starts Ending 2, whose round-two accept goes through its own form and books no review: $1,512.30. A Next
posted twice with one count plays one beat; an approval form from before the case moved answers lifecycle's own
sentence; a drop is refused, since it works only live; every refusal saves nothing; Claude's words show only inside its
collapsed draft; and no page, error page or PDF holds a sentinel key or the fake's account id, plain or decoded. The
stand-in maker's recordings (scripts/record_standin.py, with and without Claude's readings) play to their ends too, and
every page of them says it replays a run on a stand-in for the sandbox, never one recorded there. The same Monday checks
run on them, and on fixtures/story/ending-1 and ending-2 once they are committed, of either kind.

Offline: the Demo answers from the recordings with no keys and no network, and TestClient talks to the app in this
process. tests/recordings.py records both endings once a session. The same Desk calls without HTTP are test_demo's, and
the web app over a live Service is test_app_live's.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import httpx
import pytest
from starlette.testclient import TestClient

import recordings
from disputable import cli, recording, story
from disputable.airwallex import UPLOAD
from disputable.demo import Demo
from disputable.desk import Board
from disputable.lifecycle import Phase, phase
from disputable.recording import Manifest, Recording, plan_text
from disputable.web import create_app, views
from disputable.web import words as w
from test_app_live import PDF, TOKEN, leaks, said, with_token
from test_demo import OPENS, OTHER_PLAN, doctored
from test_service import TABLES, VOID_STAGE, with_plan
from test_web_app import SENTINELS
from web_helpers import BASE, ORIGIN, PORT, Node, Voices, client, form_fields, pages

DAY = "Nov 2, 2026"  # the day the fake recordings were made, and the day of their readings
READ_LINE = "Claude's readings were recorded from claude-opus-5-5 on Nov 2, 2026."
CLOCK = "Clock: Nov 2, 17:00 UTC, the recording's time."
EAR_TIPS_ALERT = "Order #1121 is due in 2 days, on Nov 4 at 17:15 UTC. It needs no approval and is ready to send."
NOW_APPROVE = w.NOW_LINE.format(now=w.NOW["APPROVE"])
ORDER = {"1": (1, 2, 4, 3, *range(5, 14)), "2": tuple(range(1, 12))}  # the beats as the walk plays them
# The steps, by beats played, at which the walk keeps every page and the refusal pages too: each start, Monday's
# window and each approval in it, round two's window, everything sent, round two's accept approved, and each end.
DEEP = {"1": frozenset({0, 2, 3, 4, 8, 13}), "2": frozenset({0, 5, 8, 9, 11})}
COMMITTED = {path.name.removeprefix("ending-"): path for path in sorted(cli.STORY.glob("ending-*")) if path.is_dir()}


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

    def build(folders=None, ending="1"):
        demo = Demo(recordings.recorded() if folders is None else folders, store_for=story.STORES.__getitem__,
                    ending=ending)
        made.append(demo)
        return demo

    yield build
    for demo in made:
        demo.close()


# ----------------------------------------------------------------------------- reading the pages
@functools.lru_cache(maxsize=64)
def parsed(html: str) -> Voices:
    """A page parsed once: the tests read the same page's banner, bar and main one after another."""
    return Voices(html)


def _within(node: Node, tag: str, name: str = "") -> bool:
    return any(found == tag and (not name or name in classes) for found, classes in node.around)


def banner(html: str) -> list[str]:
    """The banner's lines, without the demo bar inside it."""
    return [node.text for node in parsed(html).nodes
            if _within(node, "section", "banner") and not _within(node, "div", "bar")]


def bar(html: str) -> list[str]:
    """The demo bar: the ending, where the story is, the approvals pending, and the buttons."""
    return [node.text for node in parsed(html).nodes if _within(node, "div", "bar")]


def main(html: str) -> list[str]:
    """What the page itself shows, under the banner and the bar."""
    return [node.text for node in parsed(html).nodes if _within(node, "main")]


def done(html: str) -> list[str]:
    """The line that says what the last form did."""
    return [node.text for node in parsed(html).nodes if _within(node, "p", "done")]


def deadlines(html: str) -> list[str]:
    """The queue's Deadlines block: one line for each alert."""
    texts = main(html)
    if w.DEADLINES not in texts:
        return []
    rest = texts[texts.index(w.DEADLINES) + 1:]
    ends = [index for index, text in enumerate(rest) if text in w.GROUPS.values() or text == w.PROBLEMS]
    return rest[:ends[0]] if ends else rest


def actions(html: str) -> list[str]:
    """Where each form of the page posts, in order."""
    return [action for action, _ in parsed(html).forms]


def pending(*orders: str) -> str:
    """The bar's line for the approvals Priya made in the recording that the judge may make now, in the screens' own
    words: the orders as they read inside a sentence."""
    labels = [views.in_sentence(order) for order in orders]
    if len(labels) == 1:
        return w.PENDING_ONE.format(approver="Priya", order=labels[0])
    return w.PENDING.format(approver="Priya", orders=w.listed(labels))


def everything(demo: Demo) -> tuple:
    """What a refusal leaves as it was, as test_demo.everything() reads it but in one connection: every table's rows,
    the beats played and the replay clock."""
    replay = demo.replay
    connection = replay.service.db.connect()
    try:
        rows = tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in TABLES)
    finally:
        connection.close()
    return rows, replay.played, replay.clock.peek(), replay.clock.left()


def ids_of(recorded: Recording) -> dict[str, str]:
    """seed case id -> dispute id, from the recording's bank step on Monday."""
    return {opened.case_id: opened.dispute_id for opened in recorded.beats[0].dates}


def moment(recorded: Recording, n: int, dispute_id: str):
    """The recorded moment the approval of beat n took."""
    return dict(recorded.beats[n - 1].clock)[f"approve:{dispute_id}"]


def replays_line(manifest: Manifest) -> str:
    """The banner's line for what the recording replays: a run recorded on the sandbox, or one on the tests' stand-in."""
    if manifest.airwallex == "stand-in":
        return w.DEMO_STANDIN
    return w.DEMO_REPLAYS.format(day=views.calendar(manifest.recorded_at.date()))


def readings_line(manifest: Manifest) -> str:
    """The banner's line for the readings a manifest names."""
    if manifest.source == "none":
        return w.READ_NONE
    first, last = manifest.first_read, manifest.last_read
    if first != last:
        return w.READ_RECORDED_SPAN.format(model=manifest.model, first=views.calendar(first), last=views.calendar(last))
    return w.READ_RECORDED.format(model=manifest.model, day=views.calendar(first))


def uploaded(recorded: Recording) -> set[str]:
    """The SHA-256 of each file the live run uploaded, as the recording keeps the upload."""
    return {line["request"]["body"]["multipart"]["sha256"] for line in recorded.exchanges
            if line["actor"] == "agent" and httpx.URL(line["request"]["url"]).path == UPLOAD}


# ----------------------------------------------------------------------------- the moves a judge makes
def press(c: TestClient, page: str | None = None):
    """Next, through the bar's own form on the page the judge has open (the queue, by default)."""
    return c.post("/demo/next", data=form_fields(page or c.get("/").text, "/demo/next"), headers=ORIGIN)


def approve(c: TestClient, dispute_id: str, page: str | None = None):
    """An approval through the case's approval page's own form (that page as the judge has it open, or as it is now)."""
    path = views.case_href(dispute_id, "approve")
    return c.post(path, data=form_fields(page or c.get(path).text, path), headers=ORIGIN)


def refusals(c: TestClient, board: Board, *, every_route: bool) -> Iterator[tuple[str, Any]]:
    """Each refusal and error page a judge can meet at this step. None may save anything, play a beat or start the story
    again: a Next posts a count the page didn't show, and a restart an ending that isn't recorded. every_route: the form
    checks and the missing routes too, whose fixed sentences don't change from step to step."""
    nxt = {"csrf": TOKEN, "played": str(len(board.demo.played))}
    if every_route:
        yield "no page", c.get("/nowhere")
        yield "no such method", c.post("/money", headers=ORIGIN)
        yield "another host", c.get("/", headers={"Host": "evil.example"})
        yield "a form from another site", c.post("/demo/next", data=nxt, headers={"Origin": "http://evil.example"})
        yield "a wrong token", c.post("/demo/next", data={**nxt, "csrf": "x" * 43}, headers=ORIGIN)
        yield "a field too many", c.post("/demo/next", data={**nxt, "by": "Mallory"}, headers=ORIGIN)
    yield "no case", c.get("/case/dst_nobody")
    yield "a count the page didn't show", c.post(
        "/demo/next", data={**nxt, "played": str(len(board.demo.played) + 1)}, headers=ORIGIN)
    yield "an ending that isn't recorded", c.post("/demo/restart", data={"csrf": TOKEN, "ending": "9"}, headers=ORIGIN)
    for dispute_id, record in board.records.items():
        path, plan = views.case_href(dispute_id, "approve"), record.rounds[-1].plan
        yield f"{dispute_id}: no such file", c.get(views.case_href(dispute_id, "file", f"{'0' * 64}.pdf"))
        yield f"{dispute_id}: a plan this app can't read", c.post(
            path, data={"csrf": TOKEN, "plan": "{", "reading": ""}, headers=ORIGIN)
        if plan is not None:
            unseen = with_plan(plan_text(plan), reasons=["A plan no page showed."])
            yield f"{dispute_id}: a plan no page showed", c.post(
                path, data={"csrf": TOKEN, "plan": unseen, "reading": ""}, headers=ORIGIN)
        yield f"{dispute_id}: a drop", c.post(views.case_href(dispute_id, "drop"), data={
            "csrf": TOKEN, "version": str(board.versions[dispute_id])}, headers=ORIGIN)


@dataclass(frozen=True)
class Seen:
    """The screens after one move: the move and its answer, the board, the pages shown and every PDF they link to, the
    refusal pages, the page the answer led to, and whether showing all of it left the demo as it was."""
    move: str  # "start", "next", "approve <case>" or "restart <ending>"
    answer: tuple[int, str] | None  # the POST's status and Location
    board: Board
    played: tuple[int, ...]  # the beats played, in the order they were played
    pages: dict[str, str]  # by path: every page of the board at a deep step, else the queue, money and approvals
    files: dict[str, bytes]  # by path
    errors: dict[str, Any]  # by what was posted or asked: the answer; only at a deep step
    landing: str  # the html of the page the answer led to, or ""
    unchanged: bool


def show(c: TestClient, demo: Demo, move: str, answer=None, *, deep: frozenset[int] = frozenset()) -> Seen:
    """The screens now, read as a browser reads them: the queue, the money page, the approval page of each case that
    waits for one, every PDF they link to, and the page the answer led to. At a deep step (the count of beats played is
    in deep) every page of the board, and the refusal pages too: every route's at the start."""
    before = everything(demo)
    board = demo.board()
    full = len(demo.replay.played) in deep
    paths = pages(board) if full else ["/", "/money", *(views.case_href(dispute_id, "approve") for dispute_id, record
                                                          in board.records.items() if phase(record) is Phase.APPROVE)]
    answers = {path: c.get(path) for path in paths}
    assert {got.status_code for got in answers.values()} == {200}, move
    links = sorted({href for got in answers.values() for href in PDF.findall(got.text)})
    files = {href: c.get(href) for href in links}
    assert {(got.status_code, got.headers["content-type"]) for got in files.values()} <= {(200, "application/pdf")}
    refused = dict(refusals(c, board, every_route=move == "start")) if full else {}
    landing = c.get(answer.headers["location"]).text if answer is not None and answer.status_code == 303 else ""
    return Seen(move, None if answer is None else (answer.status_code, answer.headers.get("location", "")), board,
                demo.replay.played, {path: got.text for path, got in answers.items()},
                {href: got.content for href, got in files.items()}, refused, landing, everything(demo) == before)


def on(step: Seen) -> str:
    """The page the judge has open after a move: where its answer led, else the queue."""
    return step.landing or step.pages["/"]


def play_monday(c: TestClient, demo: Demo, *, deep: frozenset[int] = frozenset()) -> list[Seen]:
    """Monday through the pages: the start, Next twice, then Priya's two approvals through their own forms in reverse
    order, the headphones first. Each click is on the page the judge has open."""
    ids = ids_of(demo.replay.recording)
    seen = [show(c, demo, "start", deep=deep)]
    for _ in range(2):
        seen.append(show(c, demo, "next", press(c, on(seen[-1])), deep=deep))
    for case_id in ("headphones", "earbuds"):
        page = seen[-1].pages[views.case_href(ids[case_id], "approve")]
        seen.append(show(c, demo, f"approve {case_id}", approve(c, ids[case_id], page), deep=deep))
    return seen


# ----------------------------------------------------------------------------- the walk
@dataclass(frozen=True)
class Walk:
    """Ending 1, then Ending 2 from the bar's restart form, played through the pages."""
    recorded: dict[str, Recording]  # by ending
    seen: dict[str, tuple[Seen, ...]]  # by ending, in order
    stale: tuple[Any, bool]  # round one's approval form posted in round two: the answer, and whether nothing changed
    finished: tuple[Any, bool]  # a Next posted with the count of the story's end


@pytest.fixture(scope="module")
def walk() -> Walk:
    """Ending 1 played once a module as a judge plays it: Next through the bar's form, and the Monday approvals through
    the approval pages' forms in reverse order. Round one's headphones form is posted again once the bank pushed back.
    Then the bar's restart form starts Ending 2: Next plays Monday's approvals as Priya, and its round-two accept goes
    through its own form. Everything is shown at every step, with sentinel keys in the environment. Read it only."""
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network), pytest.MonkeyPatch.context() as patch:
        for name, value in SENTINELS.items():
            patch.setenv(name, value)
        demo = Demo(recordings.recorded(), store_for=story.STORES.__getitem__, ending="1")
        try:
            with with_token(demo) as c:
                return _walk(c, demo)
        finally:
            demo.close()


def _walk(c: TestClient, demo: Demo) -> Walk:
    recorded, deep = {"1": demo.replay.recording}, DEEP["1"]
    one = play_monday(c, demo, deep=deep)
    path = views.case_href(ids_of(recorded["1"])["headphones"], "approve")
    round_one = form_fields(one[2].pages[path], path)  # what the headphones' page posted on Monday
    while len(demo.replay.played) < 8:  # to round two, decided: the window holds its approval
        one.append(show(c, demo, "next", press(c, on(one[-1])), deep=deep))
    before = everything(demo)
    stale = c.post(path, data=round_one, headers=ORIGIN)
    stale_kept = (stale, everything(demo) == before)
    while demo.replay.unplayed():
        one.append(show(c, demo, "next", press(c, on(one[-1])), deep=deep))
    before = everything(demo)
    end = c.post("/demo/next", data={"csrf": TOKEN, "played": str(len(demo.replay.played))}, headers=ORIGIN)
    finished = (end, everything(demo) == before)
    restart = c.post("/demo/restart", data=form_fields(on(one[-1]), "/demo/restart", ending="2"), headers=ORIGIN)
    recorded["2"], deep = demo.replay.recording, DEEP["2"]
    two = [show(c, demo, "restart 2", restart, deep=deep)]
    while len(demo.replay.played) < 8:
        two.append(show(c, demo, "next", press(c, on(two[-1])), deep=deep))
    headphones = ids_of(recorded["2"])["headphones"]
    page = two[-1].pages[views.case_href(headphones, "approve")]
    two.append(show(c, demo, "approve headphones", approve(c, headphones, page), deep=deep))
    while demo.replay.unplayed():
        two.append(show(c, demo, "next", press(c, on(two[-1])), deep=deep))
    return Walk(recorded, {"1": tuple(one), "2": tuple(two)}, stale_kept, finished)


# ----------------------------------------------------------------------------- Monday: the checks every recording keeps
def assert_before_next(start: Seen, recorded: Recording, endings: tuple[str, ...]) -> None:
    """The queue is empty under the demo banner, and the bar offers the story's first step and a restart per ending."""
    manifest, first = recorded.manifest, recorded.beats[0]
    queue = start.pages["/"]
    assert start.played == () and set(start.pages) == {"/", "/money"} and dict(start.board.records) == {}
    assert banner(queue) == [w.DEMO, replays_line(manifest), w.MADE_UP, readings_line(manifest),
                             w.CLOCK_REPLAY_START.format(when=views.when(first.at, first.at))]
    assert (recordings.SANDBOX_WORDS in queue) == (manifest.airwallex == "sandbox")
    assert main(queue) == [w.QUEUE_TITLE, w.QUEUE_SUMMARY.format(open=0, cost="$0.00", risk="$0.00"), w.EMPTY_QUEUE,
                           w.EMPTY_DEMO]
    restarts = [(w.RESTART_SAME if ending == manifest.ending else w.RESTART_OTHER).format(ending=ending)
                for ending in endings]
    assert bar(queue) == [w.ENDINGS[manifest.ending], w.NOT_STARTED, w.NEXT.format(story=first.story, text=first.text),
                          *restarts]
    assert actions(queue) == ["/demo/next", *["/demo/restart"] * len(endings)]
    assert form_fields(queue, "/demo/next")["played"] == "0"
    money = main(start.pages["/money"])
    assert money[1:3] == [w.MONEY_SUMMARY.format(cost="$0.00", risk="$0.00"), w.MONEY_EMPTY]
    assert not [text for text in money if text.startswith("The recording's own total")]


def assert_monday(seen: list[Seen], recorded: Recording) -> None:
    """After the first pass $1,450.00 is at risk and only the ear tips alert. The headphones' approval, made first
    through its own form, plays beat 4 at its recorded moment while beat 3 waits; then the earbuds' plays beat 3."""
    _, bank, monday, headphones, earbuds = seen
    ids, count = ids_of(recorded), len(recorded.beats)
    h, e = ids["headphones"], ids["earbuds"]
    assert [step.answer for step in (bank, monday)] == [(303, "/?done=next-played")] * 2
    assert (bank.played, monday.played) == ((1,), (1, 2))
    queue = monday.pages["/"]
    assert w.QUEUE_SUMMARY.format(open=3, cost="$15.00", risk="$1,450.00") in main(queue)
    (alert,) = deadlines(queue)  # one alert: the ear tips', due first, as the bank's step said: in 2 days
    assert alert.startswith("Order #1121 is due in 2 days, on ")
    assert w.MONEY_SUMMARY.format(cost="$15.00", risk="$1,450.00") in main(monday.pages["/money"])
    assert not [text for path in ("/", "/money") for text in main(monday.pages[path]) if "2,699" in text]  # not store.cases
    assert pending("Order #1098", "Order #1109") in bar(queue)
    for dispute_id, label in ((e, "Order #1098"), (h, "Order #1109")):
        assert f'<a href="{views.case_href(dispute_id, "approve")}">{label}</a>' in queue
        assert w.APPROVE_BUTTON in parsed(monday.pages[views.case_href(dispute_id, "approve")]).texts()
    # The headphones first: beat 4 plays inside the window while beat 3 waits, at its own recorded moment.
    assert headphones.answer == (303, f"/case/{h}?done=approved") and headphones.played == (1, 2, 4)
    assert done(headphones.landing) == [w.DONE_DEMO["approved"].format(approver="Priya")]
    (row,) = headphones.board.approvals[h]
    assert (row.by, row.at, row.plan_sha256) == ("Priya", moment(recorded, 4, h), recorded.beats[3].plan_sha256)
    assert e not in headphones.board.approvals
    shown = bar(headphones.pages["/"])
    assert shown[1:3] == [w.STEP.format(n=3, count=count, story=recorded.beats[3].story), recorded.beats[3].text]
    assert pending("Order #1098") in shown
    assert w.NEXT.format(story=recorded.beats[2].story, text=recorded.beats[2].text) in shown
    # Then the earbuds: beat 3, at its own recorded moment, and nothing waits any more.
    assert earbuds.answer == (303, f"/case/{e}?done=approved") and earbuds.played == (1, 2, 4, 3)
    (row,) = earbuds.board.approvals[e]
    assert (row.by, row.at, row.plan_sha256) == ("Priya", moment(recorded, 3, e), recorded.beats[2].plan_sha256)
    shown = bar(earbuds.pages["/"])
    assert not [text for text in shown if text.startswith("Priya approved")]
    assert w.NEXT.format(story=recorded.beats[4].story, text=recorded.beats[4].text) in shown


def test_before_next_the_queue_is_empty_under_the_demo_banner(walk):
    assert_before_next(walk.seen["1"][0], walk.recorded["1"], ("1", "2"))
    queue = walk.seen["1"][0].pages["/"]
    assert banner(queue)[1:4] == [w.DEMO_REPLAYS.format(day=DAY), w.MADE_UP, READ_LINE]
    assert bar(queue)[2] == f"Next: Monday 9:00: {OPENS}"
    assert w.LIVE not in parsed(queue).texts()


def test_monday_shows_1450_at_risk_and_one_alert_the_ear_tips(walk):
    assert_monday(list(walk.seen["1"][:5]), walk.recorded["1"])
    monday = walk.seen["1"][2]
    assert deadlines(monday.pages["/"]) == [EAR_TIPS_ALERT]
    ids = ids_of(walk.recorded["1"])
    for case_id, alerts in (("ear_tips", [EAR_TIPS_ALERT]), ("earbuds", []), ("headphones", [])):
        page = main(monday.pages[views.case_href(ids[case_id])])
        assert [text for text in page if text.startswith("Order #") and " is due in " in text] == alerts, case_id
    assert bar(monday.pages["/"]) == [
        w.ENDINGS["1"], "Step 2 of 13: Monday 9:00.", "The agent reads the queue.",
        pending("Order #1098", "Order #1109"), "Order #1098", "Order #1109",  # the screens' own words for the window
        "Next: Monday 9:05: Priya approves code's draft on order #1098: accept $189.00, no refund.",
        "Start Ending 1 again", "Start Ending 2"]
    money = main(monday.pages["/money"])
    assert money[1:4] == ["Cost so far: $15.00. Still at risk: $1,450.00.",
                          "Accepting everything at once would have cost $1,465.60.", w.NOT_FINAL]


def test_the_monday_approvals_in_reverse_order_prove_the_pending_window(walk):
    _, _, monday, headphones, earbuds = walk.seen["1"][:5]
    h, e = ids_of(walk.recorded["1"])["headphones"], ids_of(walk.recorded["1"])["earbuds"]
    assert monday.board.demo.pending == ((e, "Order #1098"), (h, "Order #1109"))  # the window: beats 3 and 4
    assert headphones.board.demo.pending == ((e, "Order #1098"),) and earbuds.board.demo.pending == ()
    assert bar(headphones.pages["/"])[1:3] == ["Step 3 of 13: Monday 9:05.", "Priya approves the challenge on order #1109."]
    assert bar(earbuds.pages["/"])[1:3] == [  # one story time for the window, so the order it is played in never shows
        "Step 4 of 13: Monday 9:05.", "Priya approves code's draft on order #1098: accept $189.00, no refund."]
    for step, dispute_id in ((headphones, h), (earbuds, e)):  # each case page now says it waits for the next pass
        page = parsed(step.landing).texts()
        assert w.NOW["SEND"] in page and w.APPROVAL.format(by="Priya", number=1, when="Nov 2, 17:00 UTC") in page
        assert w.APPROVE_BUTTON not in parsed(step.pages[views.case_href(dispute_id, "approve")]).texts()


def test_the_story_time_never_goes_back(walk):
    """The banner and the bar show the story time of the beat played last. Monday's window shares one, so approving it in
    reverse order, the headphones first, never turns the story's clock back: it only moves on, in the recording's
    order of story times."""
    for ending, seen in walk.seen.items():
        order = list(dict.fromkeys(beat.story for beat in walk.recorded[ending].beats))
        shown = []
        for step in seen:
            if not step.played:  # the start: the story hasn't begun
                continue
            queue = step.pages["/"]
            story_time = banner(queue)[-1].split("Story time: ", 1)[1].removesuffix(".")
            assert bar(queue)[1].endswith(f": {story_time}."), (ending, step.played)
            shown.append(order.index(story_time))
        assert shown == sorted(shown) and len(shown) == len(walk.recorded[ending].beats), ending
    assert [step.move for step in walk.seen["1"][3:5]] == ["approve headphones", "approve earbuds"]  # reverse order


# ----------------------------------------------------------------------------- Next to the end, and the money
@pytest.mark.parametrize("ending", ["1", "2"])
def test_next_plays_the_recordings_beats_in_order(walk, ending):
    recorded, seen = walk.recorded[ending], walk.seen[ending]
    beats, count = recorded.beats, len(recorded.beats)
    for step in seen:
        if step.move == "next":
            assert step.answer == (303, "/?done=next-played"), step.played
            assert done(step.landing) == [w.DONE["next-played"]]
        queue = step.pages["/"]
        coming = [beat for beat in beats if beat.n not in step.played]
        nexts = [dict(fields) for action, fields in parsed(queue).forms if action == "/demo/next"]
        if step.played:
            beat = beats[step.played[-1] - 1]  # the beat just played: the bar and the banner's clock name it
            assert bar(queue)[1:3] == [w.STEP.format(n=len(step.played), count=count, story=beat.story), beat.text]
            assert banner(queue)[-1] == w.CLOCK_REPLAY.format(when="Nov 2, 17:00 UTC", story=beat.story)
        if coming:
            assert w.NEXT.format(story=coming[0].story, text=coming[0].text) in bar(queue)
            assert [fields["played"] for fields in nexts] == [str(len(step.played))]
        else:
            assert nexts == [] and step is seen[-1]
    assert [step.played[-1] for step in seen if step.played] == list(ORDER[ending])
    assert seen[-1].played == ORDER[ending] and len(ORDER[ending]) == count


def test_next_plays_ending_one_to_its_end(walk):
    recorded, end = walk.recorded["1"], walk.seen["1"][-1]
    ids = ids_of(recorded)
    queue = end.pages["/"]
    assert bar(queue) == [w.ENDINGS["1"], "Step 13 of 13: A week later.", "The agent reads the queue.",
                          "The story ends. This ending cost $279.30.", "Start Ending 1 again", "Start Ending 2"]
    assert actions(queue) == ["/demo/restart", "/demo/restart"]  # no Next once the story ends
    assert end.board.demo.left_unused == 0 and not [text for text in bar(queue) if text.startswith("The recording holds")]
    texts = main(queue)
    assert w.QUEUE_SUMMARY.format(open=0, cost="$279.30", risk="$0.00") in texts
    assert [text for text in texts if text.startswith("Closed: ")] == ["Closed: accepted.", "Closed: accepted.",
                                                                        "Closed: won."]
    answer, unchanged = walk.finished  # Next once more, with the end's own count
    assert (answer.status_code, said(answer), unchanged) == (409, [w.DEMO_FINISHED], True)
    rows = end.board.approvals
    assert [(row.round_number, row.by, row.at) for row in rows[ids["headphones"]]] == [
        (1, "Priya", moment(recorded, 4, ids["headphones"])), (2, "Priya", moment(recorded, 9, ids["headphones"]))]
    assert [(row.round_number, row.at) for row in rows[ids["earbuds"]]] == [(1, moment(recorded, 3, ids["earbuds"]))]
    record = end.board.records[ids["headphones"]]  # a challenge in each round: a person's review in each
    assert [booking.key for booking in record.bookings if booking.key.startswith("review:")] == [
        "review:RFI", "review:CHARGEBACK"]


@pytest.mark.parametrize("ending, total, compared", [
    ("1", "$279.30", "This queue cost $1,186.30 less than that."),
    ("2", "$1,512.30", "This queue cost $46.70 more than that.")])
def test_the_money_page_shows_the_replayed_total_and_the_manifests(walk, ending, total, compared):
    seen, manifest = walk.seen[ending], walk.recorded[ending].manifest
    assert f"${manifest.total:,}" == total
    money = main(seen[-1].pages["/money"])
    assert money[:5] == [w.MONEY_TITLE, f"Cost so far: {total}. Still at risk: $0.00.",
                         "Accepting everything at once would have cost $1,465.60.", compared,
                         f"The recording's own total for this ending: {total}."]
    for step in seen[:-1]:  # the recording's own total shows only once the story ends
        assert not [text for text in main(step.pages["/money"]) if text.startswith("The recording's own total")]


def test_the_recordings_own_total_comes_from_its_manifest(tmp_path, make):
    def dearer(recorded):
        return dataclasses.replace(recorded, manifest=dataclasses.replace(recorded.manifest, total=Decimal("300.00")))

    demo = make(doctored(tmp_path, dearer))
    demo.play_all()
    c = client(demo)
    money = main(c.get("/money").text)
    assert money[1] == "Cost so far: $279.30. Still at risk: $0.00."  # what the replayed cases cost
    assert "The recording's own total for this ending: $300.00." in money  # and what the manifest says
    assert "The story ends. This ending cost $300.00." in bar(c.get("/").text)


# ----------------------------------------------------------------------------- Ending 2
def test_ending_two_starts_from_the_bars_restart_form(walk):
    start, recorded = walk.seen["2"][0], walk.recorded["2"]
    assert start.answer == (303, "/?done=restarted") and done(start.landing) == [w.DONE["restarted"]]
    assert recorded.manifest.ending == "2" and start.board.store is story.STORES["ending_2"]
    assert_before_next(start, recorded, ("1", "2"))
    assert bar(start.pages["/"])[-2:] == ["Start Ending 1", "Start Ending 2 again"]
    ids = ids_of(recorded)
    monday = walk.seen["2"][4]  # Next approved Monday's two plans as Priya, at their recorded moments
    assert [step.move for step in walk.seen["2"][1:5]] == ["next"] * 4 and monday.played == (1, 2, 3, 4)
    assert [(row.by, row.at) for row in monday.board.approvals[ids["earbuds"]]] == [
        ("Priya", moment(recorded, 3, ids["earbuds"]))]
    assert [(row.by, row.at) for row in monday.board.approvals[ids["headphones"]]] == [
        ("Priya", moment(recorded, 4, ids["headphones"]))]
    assert bar(walk.seen["2"][-1].pages["/"])[3] == "The story ends. This ending cost $1,512.30."


def test_the_round_two_accept_goes_through_its_form_and_books_no_review(walk):
    recorded, seen = walk.recorded["2"], walk.seen["2"]
    h = ids_of(recorded)["headphones"]
    path = views.case_href(h, "approve")
    before, approved = seen[8], seen[9]
    assert before.played == tuple(range(1, 9)) and before.board.demo.pending == ((h, "Order #1109"),)
    page = main(before.pages[path])
    assert page[:3] == ["Approve order #1109",
                        "One approval sends exactly this: accept $1,249.00, no refund. Any change voids it.",
                        w.REVIEW_NONE]
    assert w.AIRWALLEX_ADDS not in page and w.APPROVE_AS.format(name="Priya") in page  # an accept: Airwallex adds nothing
    assert approved.move == "approve headphones" and approved.answer == (303, f"/case/{h}?done=approved")
    assert approved.played == tuple(range(1, 10)) and done(approved.landing) == [w.DONE_DEMO["approved"].format(
        approver="Priya")]
    assert [(row.round_number, row.by, row.at) for row in approved.board.approvals[h]] == [
        (1, "Priya", moment(recorded, 4, h)), (2, "Priya", moment(recorded, 9, h))]  # Next's, then the form's
    end = seen[-1]
    record = end.board.records[h]
    assert [booking.key for booking in record.bookings if booking.key.startswith("review:")] == ["review:RFI"]
    money = main(end.pages["/money"])
    assert money[money.index("Order #1109"):money.index(w.NOTES_TITLE)] == [
        "Order #1109", "Closed: accepted.", "A person's review: $16.00", "Chargeback fee: $15.00",
        "Amount the bank took back: $1,249.00", "$1,280.00", "$0.00", "$1,249.30"]


# ----------------------------------------------------------------------------- the banner, Next twice, stale forms
def test_every_page_names_the_recorded_readings_and_the_recordings_clock(walk):
    checked = 0
    for ending, seen in walk.seen.items():
        for step in seen:
            shown = [*step.pages.items(), ("landing", step.landing), *(
                (what, answer.text) for what, answer in step.errors.items() if answer.status_code == 409)]
            for where, html in shown:
                if not html:  # the start has no landing
                    continue
                lines = banner(html)
                assert lines[:4] == [w.DEMO, w.DEMO_REPLAYS.format(day=DAY), w.MADE_UP, READ_LINE], (ending, where)
                assert lines[4].startswith(CLOCK) and len(lines) == 5, (ending, where)  # never the wall's clock
                assert bar(html)[0] == w.ENDINGS[ending], (ending, where)
                checked += 1
    assert checked > 150  # 210 today: the walk shows what it should


def test_claudes_words_show_only_inside_the_collapsed_draft(walk):
    """The voice rule: Claude's words sit inside its draft, and the draft stays collapsed (a <details>) beside code's
    plan, on every page the walk shows."""
    found = 0
    for ending, seen in walk.seen.items():
        for step in seen:
            for where, html in [*step.pages.items(), ("landing", step.landing)]:
                claude = [node for node in parsed(html).nodes if node.voice == "claude"] if html else []
                assert all(node.inside("details") for node in claude), (ending, step.move, where)
                found += len(claude)
    assert found > 50  # so the check means something: the headphones' pages show Claude's draft


def test_a_next_posted_twice_plays_one_beat(make):
    demo = make()
    c = client(demo)
    fields = form_fields(c.get("/").text, "/demo/next")
    first = c.post("/demo/next", data=fields, headers=ORIGIN)
    second = c.post("/demo/next", data=fields, headers=ORIGIN)  # the same form again: the same count
    assert (first.status_code, first.headers["location"]) == (303, "/?done=next-played")
    assert (second.status_code, said(second)) == (409, [w.DEMO_MOVED]) and demo.replay.played == (1,)
    assert bar(second.text)[1] == "Step 1 of 13: Monday 9:00."  # here it is now
    assert form_fields(second.text, "/demo/next")["played"] == "1"


def test_two_clicks_on_next_at_once_play_one_beat(make, monkeypatch):
    demo = make()
    app = create_app(demo, port=PORT)
    clients = [TestClient(app, base_url=BASE, follow_redirects=False) for _ in range(2)]
    fields = form_fields(clients[0].get("/").text, "/demo/next")
    replay, real, inside = demo.replay, demo.replay.play, threading.Event()

    def slow(beat, **posted):  # a beat that takes a while, so the second click comes while it plays
        inside.set()
        time.sleep(0.2)
        return real(beat, **posted)

    monkeypatch.setattr(replay, "play", slow)
    answers, start = [], threading.Barrier(2)

    def click(c):
        start.wait()
        answers.append(c.post("/demo/next", data=fields, headers=ORIGIN))

    threads = [threading.Thread(target=click, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert inside.is_set() and sorted(answer.status_code for answer in answers) == [303, 409]
    assert [said(answer) for answer in answers if answer.status_code == 409] == [[w.DEMO_MOVED]]
    assert replay.played == (1,)


def test_an_approval_form_from_before_the_case_moved_answers_lifecycles_sentence(walk):
    answer, unchanged = walk.stale  # round one's form, posted once round two waits for its approval
    assert (answer.status_code, said(answer), unchanged) == (409, [w.NOT_APPROVED, VOID_STAGE, NOW_APPROVE], True)
    h = ids_of(walk.recorded["1"])["headphones"]
    assert f'href="{views.case_href(h, "approve")}"' in answer.text  # the way back to the page as it is now
    last = walk.seen["1"][-1].board.approvals[h][-1]  # round two's approval still played, at its own recorded moment
    assert (last.round_number, last.at) == (2, moment(walk.recorded["1"], 9, h))


def test_an_approval_tab_left_open_while_next_approved_the_plan_answers_already(make):
    demo = make()
    c = client(demo)
    for _ in range(2):
        press(c)
    ids = ids_of(demo.replay.recording)
    page = c.get(views.case_href(ids["earbuds"], "approve")).text  # the judge opens the earbuds' approval page
    assert press(c).headers["location"] == "/?done=next-played"  # and Next approves it as Priya first
    before = everything(demo)
    answer = approve(c, ids["earbuds"], page)  # then the old tab's button
    assert (answer.status_code, answer.headers["location"]) == (303, f"/case/{ids['earbuds']}?done=already")
    assert done(c.get(answer.headers["location"]).text) == [w.DONE_DEMO["already"].format(approver="Priya")]
    assert everything(demo) == before and len(demo.replay.service.db.approvals(ids["earbuds"])) == 1
    answer = approve(c, ids["headphones"])  # the window goes on: the headphones' approval still plays
    assert answer.headers["location"] == f"/case/{ids['headphones']}?done=approved"
    assert demo.replay.played == (1, 2, 3, 4)


def test_an_approval_the_recording_didnt_make_and_a_mismatch_over_http(tmp_path, make):
    def other_plan(recorded):
        beats = list(recorded.beats)
        beats[2] = dataclasses.replace(beats[2], plan_sha256="0" * 64)  # as if Priya had approved another plan
        return dataclasses.replace(recorded, beats=tuple(beats))

    demo = make(doctored(tmp_path, other_plan))
    c = client(demo)
    for _ in range(2):
        press(c)
    earbuds = ids_of(demo.replay.recording)["earbuds"]
    before = everything(demo)
    answer = approve(c, earbuds)
    assert (answer.status_code, said(answer)) == (409, [w.NOT_IN_RECORDING.format(approver="Priya"), NOW_APPROVE])
    assert everything(demo) == before
    answer = press(c)  # Next plays the earbuds' approval, and the plan isn't the one the recording approved
    assert (answer.status_code, said(answer)) == (409, [w.DEMO_MISMATCH, OTHER_PLAN])
    queue = c.get("/").text
    assert w.MISMATCH.format(mismatch=OTHER_PLAN) in bar(queue) and actions(queue) == ["/demo/restart"]
    assert not [text for text in bar(queue) if text.startswith("Priya approved")]
    answer = c.post("/demo/restart", data=form_fields(queue, "/demo/restart"), headers=ORIGIN)
    assert answer.headers["location"] == "/?done=restarted" and actions(c.get("/").text)[0] == "/demo/next"


# ----------------------------------------------------------------------------- refusals, drops and secrets
def test_a_drop_is_refused_in_the_demo(walk):
    deep = 0
    for ending, seen in walk.seen.items():
        for step in seen:
            assert not [action for html in step.pages.values() for action in actions(html) if action.endswith("/drop")]
            if not step.errors:
                continue
            deep += 1
            drops = {what.split(": ")[0]: answer for what, answer in step.errors.items() if what.endswith(": a drop")}
            assert set(drops) == set(step.board.records), (ending, step.move)  # every case, at its own version
            for dispute_id, answer in drops.items():
                now = w.NOW_LINE.format(now=views.phase_line(step.board.records[dispute_id]))
                assert (answer.status_code, said(answer)) == (409, [w.ONLY_LIVE, now]), (ending, dispute_id)
                assert w.BACK_APPROVAL not in parsed(answer.text).texts()  # the way back is the case and the queue
            assert step.unchanged, (ending, step.move)
    assert deep == len(DEEP["1"]) + len(DEEP["2"])


REFUSED = {  # what was posted or asked: the status, and what the page says first
    "no page": (404, w.NO_PAGE), "no case": (404, w.NO_CASE), "no such method": (405, w.NO_METHOD),
    "another host": (400, None), "a form from another site": (403, w.FORM_FOREIGN), "a wrong token": (403, w.FORM_OLD),
    "a field too many": (400, w.FORM_BAD), "a count the page didn't show": (409, w.DEMO_MOVED),
    "an ending that isn't recorded": (409, w.NO_ENDING), "no such file": (404, w.NO_FILE),
    "a plan this app can't read": (400, w.PLAN_UNREADABLE), "a plan no page showed": (409, w.NOT_APPROVED),
    "a drop": (409, w.ONLY_LIVE)}


def test_every_refusal_answers_its_status_and_saves_nothing(walk):
    kinds = set()
    for ending, seen in walk.seen.items():
        for step in seen:
            assert step.unchanged, (ending, step.move)  # no page, PDF or refusal saved a row or used a moment
            for what, answer in step.errors.items():
                kind = what.split(": ")[-1]
                status, first = REFUSED[kind]
                assert answer.status_code == status, (ending, step.move, what)
                assert first is None or said(answer)[0] == first, (ending, step.move, what)
                kinds.add(kind)
    assert kinds == set(REFUSED)
    answer, unchanged = walk.stale
    assert unchanged and walk.finished[1]


def test_no_page_holds_a_sentinel_or_the_fakes_account_id(walk):
    fakes = recordings.fakes()
    secrets = set(SENTINELS.values())
    for fake in fakes.values():
        secrets |= recordings.fake_secrets(fake) | set(fake.files)  # its keys, tokens, client secrets and file ids
    # So the check means something: the replayed answers carry file ids, which hold an account id in base64.
    assert all(recordings.file_ids(recorded) for recorded in walk.recorded.values())
    secrets = sorted(secrets)
    checked = 0
    for ending, seen in walk.seen.items():
        for step in seen:
            for where, html in [*step.pages.items(), ("landing", step.landing)]:
                assert leaks(html, step.board, secrets) == [], (ending, step.move, where)
                checked += 1
            for what, answer in step.errors.items():
                assert leaks(answer.text, step.board, secrets) == [], (ending, step.move, what)
                checked += 1
            for where, content in step.files.items():
                assert not [secret for secret in secrets if secret.encode() in content], (ending, step.move, where)
    for answer, _ in (walk.stale, walk.finished):
        assert leaks(answer.text, walk.seen["1"][-1].board, secrets) == []
    assert checked > 200  # 282 today


def test_the_pdfs_are_the_files_the_recording_uploaded(walk):
    for ending, seen in walk.seen.items():
        downloaded = {hashlib.sha256(content).hexdigest() for step in seen for content in step.files.values()}
        assert downloaded == uploaded(walk.recorded[ending]) and len(downloaded) == {"1": 2, "2": 1}[ending]


# ----------------------------------------------------------------------------- a run on the stand-in
@dataclass(frozen=True)
class StandIn:
    """The stand-in maker's two endings, played through the pages to their ends, the second from the bar's restart
    form. By ending: each page as (where, status, html), every page of the board at every step, the page each Next led
    to, and every refusal page at the end; and the last board."""
    readings: str  # what the maker took: "none" (--no-claude) or "claude"
    recorded: dict[str, Recording]
    shown: dict[str, tuple[tuple[str, int, str], ...]]
    ended: dict[str, Board]


def play_through(c: TestClient, demo: Demo) -> tuple[tuple[tuple[str, int, str], ...], Board]:
    """Next through the bar's form to the story's end, every page of the board read at every step."""
    shown = []
    while True:
        board = demo.board()
        step = len(board.demo.played)
        for path in pages(board):
            got = c.get(path)
            shown.append((f"step {step}: {path}", got.status_code, got.text))
        if board.demo.finished:
            break
        answer = press(c)
        assert answer.status_code == 303, step
        landing = c.get(answer.headers["location"])
        shown.append((f"step {step}: landing", landing.status_code, landing.text))
    shown += [(f"the end: {what}", answer.status_code, answer.text)
              for what, answer in refusals(c, board, every_route=True)]
    return tuple(shown), board


@pytest.fixture(scope="module", params=["none", "claude"])
def stand_in(request) -> StandIn:
    """Played once a module for each kind of readings the maker takes. Read it only."""
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        demo = Demo(recordings.standins(request.param).folders(), store_for=story.STORES.__getitem__, ending="1")
        try:
            c = with_token(demo)
            recorded, shown, ended = {}, {}, {}
            for ending in ("1", "2"):
                if demo.ending != ending:
                    fields = form_fields(c.get("/").text, "/demo/restart", ending=ending)
                    assert c.post("/demo/restart", data=fields, headers=ORIGIN).status_code == 303
                recorded[ending] = demo.replay.recording
                shown[ending], ended[ending] = play_through(c, demo)
            return StandIn(request.param, recorded, shown, ended)
        finally:
            demo.close()


def test_every_page_of_a_stand_in_says_it_isnt_a_recording_of_airwallex(stand_in):
    """w.DEMO_STANDIN on every page that has the banner, the readings line as for the sandbox's, and no page saying it
    was recorded on the sandbox. No other line on any page names the sandbox either, so none can say the sandbox did
    what the stand-in did. Only a form the app refuses before it reads the demo gets the bare page."""
    checked = 0
    for ending, shown in stand_in.shown.items():
        manifest = stand_in.recorded[ending].manifest
        assert (manifest.airwallex, manifest.source) == ("stand-in", stand_in.readings)
        for where, status, html in shown:
            assert recordings.SANDBOX_WORDS not in html and w.LIVE_READS not in html, (ending, where)
            naming = [text for text in parsed(html).texts() if "sandbox" in text.casefold()]
            assert set(naming) <= {w.DEMO_STANDIN}, (ending, where, sorted(set(naming) - {w.DEMO_STANDIN}))
            assert html.casefold().count("sandbox") == len(naming), (ending, where)  # nor in a tag or an attribute
            lines = banner(html)
            if not lines:
                assert where.startswith("the end: ") and status in (400, 403, 404, 405), (ending, where)
                continue
            assert lines[:4] == [w.DEMO, w.DEMO_STANDIN, w.MADE_UP, readings_line(manifest)], (ending, where)
            assert lines[4].startswith(CLOCK) and len(lines) == 5, (ending, where)
            assert bar(html)[0] == w.ENDINGS[ending], (ending, where)
            checked += 1
    assert checked > 150


def test_a_stand_in_plays_to_its_own_total_on_the_money_page(stand_in):
    """$279.30 and $1,512.30 with Claude's readings; with none, every read case goes to a person: Ending 2 $1,528.30."""
    totals = {"none": ("$279.30", "$1,528.30"), "claude": ("$279.30", "$1,512.30")}[stand_in.readings]
    for ending, total in zip(("1", "2"), totals):
        bar_now = stand_in.ended[ending].demo
        assert (bar_now.finished, bar_now.airwallex, f"${bar_now.total:,}") == (True, "stand-in", total)
        money = [html for where, _, html in stand_in.shown[ending] if where.endswith(": /money")][-1]
        assert main(money)[1] == f"Cost so far: {total}. Still at risk: $0.00."
        assert f"The recording's own total for this ending: {total}." in main(money)


@pytest.mark.parametrize("readings", ["none", "claude"])
@pytest.mark.parametrize("ending", recordings.ENDINGS)
def test_monday_on_a_stand_in(make, ending, readings):
    """The committed recordings' Monday checks hold for the stand-in maker's recordings too, whichever readings."""
    demo = make(recordings.standins(readings).folders(), ending)
    seen = play_monday(with_token(demo), demo)
    assert_before_next(seen[0], demo.replay.recording, ("1", "2"))
    assert_monday(seen, demo.replay.recording)
    assert all(step.unchanged for step in seen)


# ----------------------------------------------------------------------------- the committed recordings
@pytest.mark.parametrize("ending", recordings.ENDINGS)
def test_monday_on_the_committed_recordings(make, ending):
    if ending not in COMMITTED:
        pytest.skip(f"fixtures/story/ending-{ending} isn't committed yet: disputable story --ending {ending} makes it.")
    demo = make(COMMITTED, ending)
    seen = play_monday(with_token(demo), demo)
    assert_before_next(seen[0], demo.replay.recording, tuple(COMMITTED))
    assert_monday(seen, demo.replay.recording)
    assert all(step.unchanged for step in seen)


def test_the_committed_recordings_are_looked_for_where_the_demo_replays_them():
    assert cli.STORY == recording.FOLDER == Path(__file__).resolve().parents[1] / "fixtures" / "story"
    assert set(COMMITTED) <= set(recordings.ENDINGS)
