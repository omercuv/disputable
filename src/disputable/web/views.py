"""Pure builders: every page as plain values, from one Board. No clock, no network and no write.

Each line a page shows carries whose words it is (Voice): code's (words.py, and the sentences lifecycle, approvals,
rounds, rules, ce3 and the runtime write), a store record's (a field's value, or Claude's quote that the reader proved is
in the field), the bank's comment, Claude's own words (the draft) or an evidence file's line as the bank reads it. The
templates show each voice in its own labelled container, so a planted line can only ever show inside a record, bank,
claude or file container, never as code's. Claude's row notes are never shown, and neither are dropped rows' quotes.

The case page and the approval screen call rounds.current() with the board's reader profile and effort, and
rounds.files_for() only for a plan that hasn't gone out: once its first POST try is saved, the files are the saved
basis's. A saved row of the round that won't load is no bug: the page says so, in rounds' or reader's own plain
sentence or else in a fixed one (the log gets the rest), offers no approval, and in a live run offers to drop a plan
that hasn't gone out, as the Service then drops it. Neither then, nor for a dropped plan's round, does a page say that
code's rules or Claude didn't read the round. Times are UTC and rounded down, so time left never reads as more than
there is. Money shows in the dispute's own currency, and only the fee table's adds up. In the demo only a recording
made on the Airwallex sandbox gets the sandbox's words, and the pages say to press Next only while the bar offers it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import ClassVar, Iterable, Union
from urllib.parse import quote

from disputable import approvals, ce3, documents, inbox, lifecycle, prompts, reader, rounds, rules
from disputable.approvals import ApprovalRecord, Urgency
from disputable.desk import Board, DemoBar, Mode, Result
from disputable.documents import Document
from disputable.engine import Action, Stage
from disputable.fees import CENT, FEES_V1, EvidenceClass, percent, usd
from disputable.jsonable import Unloadable, dump
from disputable.lifecycle import CaseRecord, Phase, Plan, Reply
from disputable.store import Email, Order, Scan, Store, WarrantyRegistration, field_text
from disputable.web import money
from disputable.web import words as w

LOG = logging.getLogger("disputable.web")
MAX_VALUE = 20_000  # characters of one record value a page shows; the rest is counted
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
CLEARS = frozenset({lifecycle.PAYMENT_MOVED, lifecycle.OLDER_COPY, lifecycle.REFUND_FELL})
MARKS = frozenset({"", "passed", "today", "soon", "warn", "note"})
URGENCY = {Urgency.PASSED: "passed", Urgency.TODAY: "today", Urgency.SOON: "soon"}
GROUP_OF = {Phase.APPROVE: "approve", Phase.PERSON: "person", Phase.DECIDE: "out", Phase.SEND: "out",
            Phase.SENT: "out", Phase.WAIT: "bank", Phase.CLOSED: "closed"}
SUCCESS = frozenset({"approved", "already", "dropped", "next-played", "restarted"})
DEMO_DONE = frozenset({"next-played", "restarted"})  # the ?done= keys only the demo's own forms answer with
ENUM_LIKE = re.compile(r"[A-Z][A-Z_]{0,39}")
OWN_UNLOADABLE = frozenset({  # the plain sentences rounds.basis_from_json() and reader.from_json() write themselves
    "The saved basis isn't valid JSON.", f"This isn't a format {rounds.FORMAT} basis.",
    "A file in the saved basis isn't one this app could have built.",
    "The saved basis names a failure this app doesn't know.",
    "The saved reading isn't valid JSON.", f"This isn't a format {reader.FORMAT} reading."})


# ----------------------------------------------------------------------------- what a page is made of
class Voice(Enum):
    CODE = "code"  # words.py, and the sentences other modules of this app write
    RECORD = "record"  # a store record's own text, verbatim, or Claude's quote that the reader proved is in the field
    BANK = "bank"  # the bank's comment, as Airwallex sent it
    CLAUDE = "claude"  # Claude's own words: the draft's reason, its refund text and its note
    FILE = "file"  # a line of an evidence file, as the bank will read it


CONTAINERS = {Voice.CODE: "code", Voice.RECORD: "record", Voice.BANK: "record bank", Voice.CLAUDE: "claude",
              Voice.FILE: "file"}


@dataclass(frozen=True)
class Line:
    text: str
    voice: Voice = Voice.CODE
    source: str = ""  # code's label for whose words they are: required for RECORD, BANK and CLAUDE
    mark: str = ""  # one of MARKS: how the line stands out
    kind: ClassVar[str] = "line"

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.source, str):
            raise TypeError("A line's text and source are text.")
        if self.mark not in MARKS:
            raise ValueError(f"{self.mark!r} isn't a mark the stylesheet knows.")
        if self.voice in (Voice.RECORD, Voice.BANK, Voice.CLAUDE) and not self.source:
            raise ValueError("A quoted line needs code's label for whose words it is.")

    @property
    def css(self) -> str:
        return f"{CONTAINERS[self.voice]} {self.mark}".strip()


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    kind: ClassVar[str] = "link"


@dataclass(frozen=True)
class Form:
    action: str
    button: str
    fields: tuple[tuple[str, str], ...]  # every hidden field, the token included, in order
    note: Line | None = None  # shown above the button
    kind: ClassVar[str] = "form"


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[tuple[Line, ...], ...], ...]  # each cell is a few lines
    kind: ClassVar[str] = "table"


@dataclass(frozen=True)
class Block:
    title: str
    items: tuple[Item, ...]
    open: bool = True  # False: collapsed, with its title as the summary
    kind: ClassVar[str] = "block"


Item = Union[Line, Link, Form, Table, Block]


@dataclass(frozen=True)
class Banner:
    title: str
    lines: tuple[Line, ...]
    mode: str  # "demo" or "live", for the stylesheet


@dataclass(frozen=True)
class Bar:
    """The demo bar: where the story is, the approvals the judge may make, Next and the restarts."""
    title: str
    lines: tuple[Line, ...]
    links: tuple[Link, ...]
    buttons: tuple[Form, ...]


@dataclass(frozen=True)
class Page:
    title: str
    app: str
    mode: str
    nav: tuple[Link, ...]
    banner: Banner | None  # None on an error page, which never asks the desk
    bar: Bar | None
    done: str  # the ?done= key's sentence, or ""


@dataclass(frozen=True)
class Card:
    title: str
    lines: tuple[Item, ...]
    links: tuple[Link, ...]


@dataclass(frozen=True)
class Group:
    title: str
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class QueueView:
    summary: Line
    notes: tuple[Line, ...]  # what the summary's sums leave out, right under it
    alerts: Block | None
    problems: Block | None
    groups: tuple[Group, ...]
    footer: tuple[Line, ...]


@dataclass(frozen=True)
class CaseView:
    title: str
    lead: tuple[Item, ...]
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class ApproveView:
    title: str
    lead: tuple[Item, ...]
    blocks: tuple[Block, ...]
    closing: tuple[Item, ...]  # just above the form
    form: Form | None


@dataclass(frozen=True)
class MoneyView:
    summary: tuple[Line, ...]
    table: Table | None
    notes: Block


@dataclass(frozen=True)
class RefusedView:
    lines: tuple[Item, ...]
    links: tuple[Link, ...]


@dataclass(frozen=True)
class Files:
    """The files a case page shows for the round's plan."""
    documents: tuple[Document, ...]
    moved: str | None  # why the unsent plan's file can't be built again: FilesMoved's sentence, or its basis's
    kept: bool  # Airwallex refused the response: the basis's file stays here for a person
    sends: bool  # the plan sends files at all
    unreadable: str | None = None  # why the sent plan's saved basis can't be read, so its files can't be shown


@dataclass(frozen=True)
class Current:
    """rounds.current() for a page, or the line to show when it can't say."""
    step: rounds.Step | None
    problem: str | None  # code's line for what went wrong, or None
    unreadable: bool = False  # a saved row of the round won't load: no bug, and the plan can't be approved as made
    dropped: bool = False  # the round's saved basis is a dropped plan's, which current() leaves out: it was read


# ----------------------------------------------------------------------------- times, money and names
def utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.utcoffset() is None else moment.astimezone(timezone.utc)


def when(moment: datetime, now: datetime) -> str:
    """Nov 9, 14:00 UTC, with the year when it isn't the clock's. Seconds are dropped: rounded down."""
    moment = utc(moment)
    year = f", {moment.year}" if moment.year != utc(now).year else ""
    return f"{rules.MONTHS[moment.month - 1]} {moment.day}{year}, {moment:%H:%M} UTC"


def day(moment: datetime, now: datetime) -> str:
    """Nov 9, with the year when it isn't the clock's."""
    moment = utc(moment)
    year = f", {moment.year}" if moment.year != utc(now).year else ""
    return f"{rules.MONTHS[moment.month - 1]} {moment.day}{year}"


def calendar(value: date) -> str:
    """A calendar day with its year: Nov 9, 2026."""
    return f"{rules.MONTHS[value.month - 1]} {value.day}, {value.year}"


def span(length: timedelta) -> str:
    """Rounded down, as approvals counts, so the time left never reads as more than there is."""
    if length > DAY:
        return w.count(length // DAY, "day")
    if length >= HOUR:
        return w.count(length // HOUR, "hour")
    return w.UNDER_AN_HOUR


def left(due: datetime, now: datetime) -> str:
    due, now = utc(due), utc(now)
    return w.LEFT_IN.format(span=span(due - now)) if due > now else w.LEFT_AGO.format(span=span(now - due))


def amount(value: Decimal, currency: str = "USD") -> str:
    """USD as $1,249.00, another currency named after the amount. An amount that isn't a finite number is unclear, so
    the formatter never compares a NaN."""
    try:
        if not isinstance(value, Decimal) or not value.is_finite():
            return w.UNCLEAR
        if currency == "USD":
            return usd(value)
        return f"{value.quantize(CENT, ROUND_HALF_UP):,.2f} {currency}"
    except (InvalidOperation, ValueError, TypeError):
        return w.UNCLEAR


def who(record: CaseRecord) -> str:
    number = record.last.order_number
    return w.ORDER.format(number=number) if number else w.NO_ORDER_NUMBER.format(id=record.dispute_id)


def claim(code: str) -> str:
    if code in w.CLAIMS:
        return w.CLAIM.format(name=w.CLAIMS[code], code=code)
    return w.CLAIM_OTHER.format(code=code or w.UNCLEAR)


def stage_name(stage: Stage) -> str:
    return w.STAGES.get(stage.value, w.STAGE_OTHER.format(stage=stage.value))


def head(record: CaseRecord) -> str:
    d = record.last.dispute
    stage = w.STAGE_ROUND.format(stage=stage_name(record.rounds[-1].stage), number=len(record.rounds))
    return w.HEAD.format(order=who(record), amount=amount(d.amount, d.currency), claim=claim(d.reason_code), stage=stage)


def reason_text(value: str) -> str:
    if value in w.REASONS:
        return w.REASON.format(value=value, name=w.REASONS[value])
    return value if ENUM_LIKE.fullmatch(value or "") else w.UNCLEAR


def url_id(dispute_id: str) -> str:
    return quote(dispute_id, safe="")


def case_href(dispute_id: str, *rest: str) -> str:
    return "/".join(("/case", url_id(dispute_id), *rest))


def plan_text(plan: Plan) -> str:
    """The plan as the approval form carries it: jsonable data, sorted, ASCII only, so no encoding can change it."""
    return json.dumps(dump(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fact_label(record_id: str, store: Store) -> str:
    """Code's label for a store record: its kind and its id, never its text."""
    try:
        record = store.record(record_id)
    except KeyError:
        return w.RECORD_LABELS["missing"].format(id=record_id)
    if isinstance(record, Order):
        return w.RECORD_LABELS["order"].format(number=record.number)
    if isinstance(record, Scan):
        kind = documents.key(record.source)
        return w.RECORD_LABELS[kind if kind in ("carrier", "warehouse") else "scan"].format(id=record.id)
    if isinstance(record, Email):
        return w.RECORD_LABELS["email"].format(id=record.id)
    if isinstance(record, WarrantyRegistration):
        return w.RECORD_LABELS["warranty"].format(id=record.id)
    return w.RECORD_LABELS["record"].format(id=record.id)


def ours(reply: Reply, plan: Plan | None) -> bool:
    """Whether a reply is this app's own: only the actor's name on a plan this app sent with the same action."""
    return reply.by == lifecycle.ACTOR and plan is not None and plan.sending_at is not None \
        and reply.action is plan.terms.action


# ----------------------------------------------------------------------------- the frame of every page
def page(board: Board | None, title: str, *, csrf: str, done: str = "") -> Page:
    nav = (Link(w.NAV["queue"], "/"), Link(w.NAV["money"], "/money"))
    if board is None:
        return Page(title, w.APP, "bare", nav, None, None, "")
    bar = demo_bar(board.demo, csrf) if board.mode is Mode.DEMO and board.demo is not None else None
    return Page(title, w.APP, board.mode.value.lower(), nav, banner(board), bar, done_line(board, done))


def banner(board: Board) -> Banner:
    info, now = board.reader, board.now
    if board.mode is Mode.DEMO:
        lines = [Line(replays_line(board.demo))] if board.demo is not None else []
        played = board.demo.played if board.demo is not None else ()
        clock = (w.CLOCK_REPLAY.format(when=when(now, now), story=played[-1].story) if played
                 else w.CLOCK_REPLAY_START.format(when=when(now, now)))
        return Banner(w.DEMO, (*lines, Line(w.MADE_UP), Line(recorded_line(board)), Line(clock)), "demo")
    if info.source == "claude":
        claude = w.LIVE_CLAUDE.format(model=info.model or w.UNCLEAR, effort=info.effort or w.UNCLEAR)
    else:
        claude = w.LIVE_NO_KEY if info.source == "none" else w.READ_SCRIPTED
    last = w.LAST_PASS.format(when=when(board.last_pass, now)) if board.last_pass is not None else w.NO_PASS
    return Banner(w.LIVE, (Line(w.LIVE_READS), Line(w.LIVE_LOOP), Line(claude), Line(w.CLOCK_LIVE.format(
        when=when(now, now))), Line(last)), "live")


def on_sandbox(bar: DemoBar | None) -> bool:
    """Whether the demo replays a run recorded on the Airwallex sandbox: only then does a line use the sandbox's words.
    A run on the tests' stand-in for it, or a kind this code doesn't know, never passes for one."""
    return bar is not None and bar.airwallex == "sandbox"


def replays_line(bar: DemoBar) -> str:
    """What the demo replays: a run recorded on the Airwallex sandbox, or a run on the tests' stand-in for it."""
    if on_sandbox(bar):
        return w.DEMO_REPLAYS.format(day=calendar(bar.recorded_on))
    return w.DEMO_STANDIN


def recorded_line(board: Board) -> str:
    """Whose readings the demo replays. Scripted stand-ins, or a source this code doesn't know, never pass for
    Claude's own."""
    info = board.reader
    if info.source == "none":
        return w.READ_NONE
    if info.source != "claude":
        return w.READ_SCRIPTED
    model, first, last = info.model or w.UNCLEAR, info.first_read, info.last_read
    if first is not None and last is not None and first != last:
        return w.READ_RECORDED_SPAN.format(model=model, first=calendar(first), last=calendar(last))
    if first is not None or last is not None:
        return w.READ_RECORDED.format(model=model, day=calendar(first or last))
    return w.READ_RECORDED_UNDATED.format(model=model)


def demo_bar(bar: DemoBar, csrf: str) -> Bar:
    title = w.ENDINGS.get(bar.ending, w.ENDING_OTHER.format(ending=bar.ending))
    lines: list[Line] = []
    played = len(bar.played)
    if bar.played:
        last = bar.played[-1]
        count = getattr(bar, "count", None)  # the number of beats, once the Desk's DemoBar carries it
        if isinstance(count, int) and count >= played:
            lines.append(Line(w.STEP.format(n=played, count=count, story=last.story)))
        else:
            lines.append(Line(w.STEP_SO_FAR.format(n=played, story=last.story)))
        lines.append(Line(last.text))
    else:
        lines.append(Line(w.NOT_STARTED))
    if bar.mismatch:  # a stand-in is made again by its maker: the sandbox may not be able to record the story
        lines.append(Line((w.MISMATCH if on_sandbox(bar) else w.MISMATCH_STANDIN).format(mismatch=bar.mismatch),
                          mark="warn"))
    moving = not bar.finished and not bar.mismatch
    links: tuple[Link, ...] = ()
    if moving and bar.pending:
        labels = [in_sentence(label) for _, label in bar.pending]
        lines.append(Line(w.PENDING_ONE.format(approver=bar.approver, order=labels[0]) if len(labels) == 1 else
                          w.PENDING.format(approver=bar.approver, orders=w.listed(labels))))
        links = tuple(Link(label, case_href(dispute_id, "approve")) for dispute_id, label in bar.pending)
    buttons = []
    if offers_next(bar):
        buttons.append(Form("/demo/next", w.NEXT.format(story=bar.coming.story, text=bar.coming.text),
                            (("csrf", csrf), ("played", str(played)))))
    if bar.finished:
        lines.append(Line(w.END.format(total=amount(bar.total)) if bar.total is not None else w.END_PLAIN))
        if bar.left_unused > 0:
            lines.append(Line(w.LEFT_UNUSED.format(calls=w.count(bar.left_unused, "call")), mark="warn"))
    for ending in bar.endings:
        label = (w.RESTART_SAME if ending == bar.ending else w.RESTART_OTHER).format(ending=ending)
        buttons.append(Form("/demo/restart", label, (("csrf", csrf), ("ending", ending))))
    return Bar(title, tuple(lines), links, tuple(buttons))


def offers_next(bar: DemoBar) -> bool:
    """Whether the bar has a Next button: a beat is still to play, and the replay still matches the recording."""
    return not bar.finished and not bar.mismatch and bar.coming is not None


def next_offered(board: Board) -> bool:
    """Whether the board's pages show a bar with a Next button: only then does a line say to press Next."""
    return board.mode is Mode.DEMO and board.demo is not None and offers_next(board.demo)


def in_sentence(label: str) -> str:
    """Code's own label for a case, such as Order #1109, as it reads inside a sentence: order #1109."""
    return label[:1].lower() + label[1:]


def done_line(board: Board, done: str) -> str:
    """The sentence for a ?done= key. An unknown key shows nothing: no posted text is ever echoed. Nor does one of the
    demo's own keys on a live page, such as a tab left on the demo's restart page. In the demo it says to press Next
    only while the bar has a Next button: never once the story ended or the replay stopped matching, and a restart's
    page loaded again then shows nothing, since the bar says why the story doesn't go on."""
    if board.mode is not Mode.DEMO:
        return "" if done in DEMO_DONE else w.DONE.get(done, "")
    if done in w.DONE_DEMO:
        words = w.DONE_DEMO if next_offered(board) else w.DONE_DEMO_NO_NEXT
        return words[done].format(approver=approver_of(board))
    return w.DONE.get(done, "")


def approver_of(board: Board) -> str:
    if board.demo is not None and board.demo.approver:
        return board.demo.approver
    return board.approver or w.UNCLEAR


# ----------------------------------------------------------------------------- lines every view shares
def phase_line(record: CaseRecord) -> str:
    step = lifecycle.phase(record)
    if step is Phase.CLOSED:
        status = record.closed.value if record.closed is not None else ""
        return w.NOW["CLOSED"].format(status=w.STATUS.get(status, status.lower() or w.UNCLEAR))
    if step is Phase.WAIT:
        current = record.rounds[-1]
        if current.reply is None:
            return w.NOW["WAIT"]
        return w.NOW["WAIT_OURS"] if ours(current.reply, current.plan) else w.NOW["WAIT_OTHER"]
    return w.NOW[step.value]


def person_lines(record: CaseRecord, shown: Iterable[Line] = ()) -> list[Line]:
    """Why a person handles the case, then the current round's other notes, each once: so a LATE_REFUND note, or the
    accept that didn't show yet, sits right under the line it explains. shown: lines the same page shows below, such as
    send_lines()'s, whose words aren't said twice: a held send's HELD is blocker()'s line too."""
    why = lifecycle.person_reason(record)
    skip = {why, *(line.text for line in shown)}
    notes = [note for note in dict.fromkeys(record.rounds[-1].notes) if note not in skip]
    return [*([Line(why, mark="warn")] if why else []), *(Line(note) for note in notes)]


def send_lines(board: Board, record: CaseRecord) -> list[Line]:
    """On SEND and SENT, blocker()'s line (HELD included), or that the next pass sends it."""
    step = lifecycle.phase(record)
    if step not in (Phase.SEND, Phase.SENT):
        return []
    why = lifecycle.blocker(record, board.now)
    return [Line(why or (w.SEND_READY if step is Phase.SEND else w.SENT_READY))]


def date_lines(board: Board, record: CaseRecord) -> list[Line]:
    lines = []
    if lifecycle.phase(record) is not Phase.CLOSED:
        due = record.last.dispute.due_at
        lines.append(Line(w.DUE.format(when=when(due, board.now), left=left(due, board.now))))
    created = record.last.created_at
    lines.append(Line(w.CREATED.format(when=when(created, board.now)) if created is not None else w.CREATED_UNKNOWN))
    lines.append(Line(w.OPENED.format(day=day(record.opened_at, board.now))))
    return lines


def cost_line(record: CaseRecord) -> Line:
    """What the case cost so far and what is at risk, in the dispute's own currency. The fee table's fees are in its
    own currency, so code can't add up the cost of a dispute in another one."""
    currency = record.last.dispute.currency
    risk = amount(lifecycle.at_risk(record), currency)
    if not money.summed(record):
        return Line(w.COST_OTHER.format(risk=risk, currency=currency))
    return Line(w.COST_RISK.format(cost=amount(lifecycle.cost(record)), risk=risk))


def item_lines(store: Store, record: CaseRecord) -> list[Line]:
    """The disputed order's items as the order names them, in the record's own words."""
    order = order_of(store, record)
    if order is None:
        return []
    return [Line(item.description, Voice.RECORD, w.ITEM_SOURCE) for item in order.items if item.description.strip()]


def order_of(store: Store, record: CaseRecord) -> Order | None:
    number = record.last.order_number
    if not number:
        return None
    try:
        return store.order(number)
    except KeyError:
        return None


def latest_row(board: Board, record: CaseRecord) -> ApprovalRecord | None:
    """The approvals table's newest row for the record's round, as the send step reads it."""
    rows = [row for row in board.approvals.get(record.dispute_id, ()) if row.round_number == len(record.rounds)]
    return rows[-1] if rows else None


def sort_key(record: CaseRecord) -> tuple[float, str]:
    return utc(record.last.dispute.due_at).timestamp(), record.dispute_id


def queued(record: CaseRecord) -> bool:
    """Whether the queue shows the record: every one but a history-only record that isn't blocked."""
    return not record.history_only or lifecycle.phase(record) is Phase.PERSON


def in_queue_order(records: Iterable[CaseRecord]) -> list[CaseRecord]:
    """The records the queue shows, group by group, each soonest due first, then by dispute id."""
    shown = [record for record in records if queued(record)]
    groups = list(w.GROUPS)
    return sorted(shown, key=lambda record: (groups.index(GROUP_OF[lifecycle.phase(record)]), *sort_key(record)))


def left_out(total: money.Totals) -> list[Line]:
    """What the sums leave out, said right under them: the disputes in another currency than the fee table's."""
    if not total.other_currency:
        return []
    return [Line(w.CURRENCY_LEFT_OUT.format(disputes=w.count(total.other_currency, "dispute"), fees=FEES_V1.currency))]


# ----------------------------------------------------------------------------- the queue
def queue(board: Board) -> QueueView:
    records = board.records
    total = money.totals(records.values())
    summary = Line(w.QUEUE_SUMMARY.format(open=total.open, cost=amount(total.cost), risk=amount(total.at_risk)))
    alerts = approvals.alerts(records.values(), board.now)
    alert_block = Block(w.DEADLINES, tuple(Line(alert.message, mark=URGENCY[alert.urgency]) for alert in alerts)) \
        if alerts else None
    problems = Block(w.PROBLEMS, tuple(Line(line, mark="warn") for line in board.problems)) if board.problems else None
    cards: dict[str, list[Card]] = {name: [] for name in w.GROUPS}
    for record in in_queue_order(records.values()):
        try:
            shown = card(board, record)
        except Exception:  # a bug on one case never hides the rest of the queue
            LOG.exception("Code hit an error while it showed a dispute in the queue.")
            shown = Card(who(record), (Line(w.CARD_ERROR, mark="warn"),), (Link(w.OPEN_CASE, case_href(record.dispute_id)),))
        cards[GROUP_OF[lifecycle.phase(record)]].append(shown)
    for dispute_id in unread(board):
        cards["person"].append(Card(w.NO_ORDER_NUMBER.format(id=dispute_id), broken_lines(board, dispute_id),
                                    (Link(w.OPEN_CASE, case_href(dispute_id)),)))
    groups = tuple(Group(w.GROUPS[name], tuple(found)) for name, found in cards.items() if found)
    footer = []
    hidden = sum(not queued(record) for record in records.values())
    if hidden:
        footer.append(Line(w.HIDDEN.format(count=hidden)))
    if not groups:
        footer.append(Line(w.EMPTY_QUEUE))
        if board.mode is not Mode.DEMO:
            footer.append(Line(w.EMPTY_LIVE))
        elif next_offered(board):  # never once the story ended or the replay stopped matching: the bar says why
            footer.append(Line(w.EMPTY_DEMO))
    return QueueView(summary, tuple(left_out(total)), alert_block, problems, groups, tuple(footer))


def unread(board: Board) -> list[str]:
    """The cases of the board with no record that loads: none of the counts or sums can hold them."""
    return sorted(set(board.broken) - set(board.records))


def broken_lines(board: Board, dispute_id: str) -> tuple[Line, ...]:
    return Line(w.BROKEN_RECORD.format(why=board.broken[dispute_id]), mark="warn"), Line(w.BROKEN_OUT)


def card(board: Board, record: CaseRecord) -> Card:
    sending = send_lines(board, record)
    lines: list[Item] = [*item_lines(board.store, record), Line(approvals.tier_label(record)),
                         *person_lines(record, sending), Line(phase_line(record)), *sending,
                         *date_lines(board, record), cost_line(record)]
    if record.dispute_id in board.broken:
        lines.append(Line(w.BROKEN_PART.format(why=board.broken[record.dispute_id]), mark="warn"))
    links = [Link(w.OPEN_CASE, case_href(record.dispute_id))]
    if lifecycle.phase(record) is Phase.APPROVE:
        links.insert(0, Link(w.REVIEW_APPROVE, case_href(record.dispute_id, "approve")))
    return Card(head(record), tuple(lines), tuple(links))


# ----------------------------------------------------------------------------- the case page
def unreadable_why(error: object, dispute_id: str) -> str:
    """Why a saved row of a round won't load, as a page says it: rounds' or reader's own plain sentence as written.
    Any other text, such as jsonable's for a row whose fields don't fit, names fields in code's terms: the page says
    ROW_FORM instead, and the log gets the text."""
    text = str(error)
    if text in OWN_UNLOADABLE:
        return text
    LOG.warning("A saved row of dispute %s isn't in the form this app reads: %s", dispute_id, text)
    return w.ROW_FORM


def current(board: Board, record: CaseRecord) -> Current:
    """rounds.current() with the board's reader profile and effort, and the line to show when it can't say. It also
    says whether the round's saved basis is a dropped plan's: current() leaves that one out, but the round was read."""
    try:
        step = rounds.current(record, board.store, board.saved, board.now, profile=board.reader.profile,
                              effort=board.reader.effort)
    except rules.NoRules as error:
        return Current(None, w.EVIDENCE_ERROR.format(why=error))
    except Unloadable as error:  # a saved basis or reading of the round won't load: what was saved, not a bug in code
        return Current(None, w.ROUND_UNREADABLE.format(why=unreadable_why(error, record.dispute_id)), True)
    except Exception:  # a bug: the page still shows what it can
        LOG.exception("Code hit an error while it showed the records of a dispute.")
        return Current(None, w.EVIDENCE_BUG)
    # current() read this basis already, so it loads; it is left out only when it isn't the round's plan's
    dropped = step.basis is None and board.saved.basis(record.dispute_id, len(record.rounds)) is not None
    return Current(step, None, dropped=dropped)


def case_files(board: Board, record: CaseRecord) -> Files:
    """The round plan's files: built again from today's records while the plan is unsent, and the saved basis's once
    its first POST try is saved. files_for() is never called then: as_case() counts the challenge as a later round. A
    saved basis that won't load can't build the unsent plan's file either, and a sent plan's files can't be shown then."""
    plan = record.rounds[-1].plan
    if plan is None or not plan.terms.file_hashes:
        return Files((), None, False, False)
    if plan.sending_at is None:
        try:
            return Files(rounds.files_for(record, board.store, board.saved), None, False, True)
        except rounds.FilesMoved as error:  # as the Service and the send step read them
            return Files((), str(error), False, True)
        except Unloadable as error:
            return Files((), unreadable_why(error, record.dispute_id), False, True)
    try:
        basis = board.saved.basis(record.dispute_id, len(record.rounds))
    except Unloadable as error:  # the file went out from the uploads; only the copy kept for the screens is lost
        return Files((), None, False, True, unreadable_why(error, record.dispute_id))
    documents_ = basis.files if basis is not None and basis.plan_sha256 == approvals.plan_sha256(plan) else ()
    return Files(documents_, None, plan.refused is not None, True)


def case(board: Board, dispute_id: str, *, csrf: str) -> CaseView | None:
    record = board.records.get(dispute_id)
    if record is None:
        return broken_view(board, dispute_id)
    now = current(board, record)
    files = case_files(board, record)
    blocks = [now_block(board, record), plan_block(board, record), found_block(board, record, now, files, csrf=csrf),
              files_block(board, record, files, csrf=csrf), Block(w.CLAUDE, claude_items(board, record, now)),
              sends_block(board, record), rounds_block(board, record), approvals_block(board, record),
              money_block(record), records_block(board, record)]
    return CaseView(head(record), tuple(item_lines(board.store, record)), tuple(block for block in blocks if block))


def broken_view(board: Board, dispute_id: str) -> CaseView | None:
    if dispute_id not in board.broken:
        return None
    return CaseView(w.NO_ORDER_NUMBER.format(id=dispute_id), (), (
        Block(w.NOW_TITLE, broken_lines(board, dispute_id)),))


def now_block(board: Board, record: CaseRecord) -> Block:
    step = lifecycle.phase(record)
    sending = send_lines(board, record)
    items: list[Item] = [Line(phase_line(record)), Line(approvals.tier_label(record)), *person_lines(record, sending)]
    why = lifecycle.person_reason(record)
    if record.blocked and record.blocked != why:
        items.append(Line(w.BLOCK_RAW.format(why=record.blocked)))  # FUTURE shows over the older copy: show both
    if record.blocked in CLEARS and record.blocked == why and not record.history_only:
        items.append(Line(w.BLOCK_CLEARS))  # never after a slip, which keeps the block until a later copy shows
    items += sending
    if step in (Phase.SEND, Phase.SENT):
        problem = approvals.verify(record, latest_row(board, record))
        if problem:
            words = w.VERIFY_SEND if step is Phase.SEND else w.VERIFY_SENT
            items.append(Line(words.format(why=problem), mark="warn"))
    items += [Line(alert.message, mark=URGENCY[alert.urgency]) for alert in approvals.alerts([record], board.now)]
    if record.history_only:
        items.append(Line(w.HISTORY_SHOWN if queued(record) else w.HISTORY_ONLY))
    if record.dispute_id in board.broken:
        items.append(Line(w.BROKEN_PART.format(why=board.broken[record.dispute_id]), mark="warn"))
    items += [*date_lines(board, record), cost_line(record)]
    if step is Phase.APPROVE:
        items.append(Link(w.REVIEW_APPROVE, case_href(record.dispute_id, "approve")))
    return Block(w.NOW_TITLE, tuple(items))


def plan_block(board: Board, record: CaseRecord) -> Block:
    plan = record.rounds[-1].plan
    if plan is None:
        none = w.NO_PLAN_DECIDE if lifecycle.phase(record) is Phase.DECIDE else w.NO_PLAN_PERSON
        return Block(w.PLAN, (Line(none),))
    items: list[Item] = [Line(approvals.tier_label(record)), why_block(plan), terms_block(board, plan, open=False)]
    if lifecycle.phase(record) is Phase.APPROVE:
        items.append(Link(w.REVIEW_APPROVE, case_href(record.dispute_id, "approve")))
    return Block(w.PLAN, tuple(items))


def why_block(plan: Plan) -> Block:
    return Block(w.WHY, tuple(Line(reason) for reason in plan.reasons) or (Line(w.NONE_LISTED),))


def terms_block(board: Board, plan: Plan, *, open: bool, title: str = w.TERMS) -> Block:
    t, now, store = plan.terms, board.now, board.store

    def term(name: str, value: str) -> Line:
        return Line(w.TERM.format(name=w.TERM_NAMES[name], value=value))

    if not t.refund_amount.is_finite():
        refund = w.UNCLEAR
    elif t.refund_amount > 0:
        refund = w.REFUND_TO_CARD.format(amount=amount(t.refund_amount, t.currency))
    else:
        refund = w.REFUND_NONE_CHARGEBACK if lifecycle.RANK.get(t.stage, 0) >= lifecycle.RANK[Stage.CHARGEBACK] \
            else w.REFUND_NONE
    items: list[Item] = [
        term("dispute", t.dispute_id), term("stage", stage_name(t.stage)),
        term("action", w.ACTIONS.get(t.action.value, t.action.value)), term("reason", reason_text(t.reason)),
        term("amount", amount(t.amount, t.currency)), term("refund", refund),
        term("refunded_before", amount(t.refunded_before, t.currency)),
        term("customer", t.customer_id or w.NO_CUSTOMER)]
    try:
        name = store.customer(t.customer_id).name if t.customer_id else ""
    except KeyError:
        name = ""
    if name.strip():
        items.append(Line(name, Voice.RECORD, w.CUSTOMER_NAME))
    items += [Block(w.TERM_NAMES["facts"], tuple(Line(fact_label(fact, store)) for fact in sorted(t.fact_ids))
                    or (Line(w.NONE_LISTED),)),
              Block(w.TERM_NAMES["files"], tuple(Line(digest) for digest in sorted(t.file_hashes))
                    or (Line(w.NONE_LISTED),)),
              term("due", w.DUE_TERM.format(when=when(t.due_at, now), left=left(t.due_at, now))),
              term("fee_table", t.fee_table), term("request_id", w.REQUEST_ID.format(id=plan.request_id))]
    return Block(title, tuple(items), open=open)


def found_block(board: Board, record: CaseRecord, now: Current, files: Files, *, csrf: str) -> Block:
    """What code found: from code's conclusion while the plan is unsent, else from the round's saved basis. When a
    saved row of the round won't load, the offer to drop the plan sits under that line, unless the files offer it. A
    round with no rule's findings to show says whether its rules ever read the records: they did for a dropped plan."""
    step = now.step
    items: list[Item] = []
    if lifecycle.phase(record) is Phase.DECIDE:
        items.append(Line(w.NOT_DECIDED))
    if now.problem:
        items.append(Line(now.problem, mark="warn"))
        if now.unreadable and files.moved is None:
            items += drop_items(board, record, csrf=csrf)
    ce3_lines: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    read = False
    if step is not None and step.conclusion is not None:
        check = step.conclusion.check
        ce3_lines, findings, read = (check.reasons if check else ()), step.conclusion.findings, True
    elif step is not None and step.basis is not None:
        ce3_lines, findings, read = step.basis.ce3_reasons, step.basis.findings, True
    if ce3_lines:
        notes = [Line(w.NOTE_RFI, mark="note")] if board.mode is Mode.DEMO and record.rounds[-1].stage is Stage.RFI \
            and record.last.dispute.reason_code == ce3.FRAUD_CLAIM else []
        items.append(Block(w.CE3, (*(Line(line) for line in ce3_lines), *notes)))
    if not read and not now.problem:  # before the inbox, which code counts even when no rule ran
        items.append(Line(w.FOUND_DROPPED if now.dropped else w.FOUND_NONE))
    lines = inbox.findings(board.store, record.last.order_number, board.reader.profile.support)
    if lines:
        items.append(Block(w.INBOX, tuple(Line(line) for line in lines)))
    if read:
        others = [finding for finding in findings if finding not in ce3_lines]
        items.append(Block(w.OTHER_FINDINGS, tuple(Line(finding) for finding in others) or (Line(w.FINDINGS_NONE),)))
    return Block(w.FOUND, tuple(items))


def files_block(board: Board, record: CaseRecord, files: Files, *, csrf: str) -> Block | None:
    plan = record.rounds[-1].plan
    if plan is None:
        return None
    if not files.sends:
        return Block(w.FILES, (Line(w.FILES_NONE),))
    items: list[Item] = []
    if files.moved is not None:
        items += moved_items(board, record, files.moved, csrf=csrf)
    if files.unreadable is not None:
        items.append(Line(w.FILE_UNREADABLE.format(why=files.unreadable), mark="warn"))
    if files.kept:
        items.append(Line(w.FILE_KEPT, mark="warn"))
    items += [file_block(record, document) for document in files.documents]
    return Block(w.FILES, tuple(items))


def moved_items(board: Board, record: CaseRecord, why: str, *, csrf: str) -> list[Item]:
    """FilesMoved's sentence, and in a live run the offer to drop the plan, so it doesn't wait for nobody."""
    return [Line(w.FILE_MOVED.format(why=why), mark="warn"), *drop_items(board, record, csrf=csrf)]


def drop_items(board: Board, record: CaseRecord, *, csrf: str) -> list[Item]:
    """In a live run, while the plan hasn't gone out, the offer to drop it, with the version the page shows."""
    if board.mode is not Mode.LIVE or lifecycle.phase(record) not in (Phase.APPROVE, Phase.SEND):
        return []
    version = str(board.versions.get(record.dispute_id, 0))
    return [Line(w.DROP_OFFER), Form(case_href(record.dispute_id, "drop"), w.DROP_BUTTON,
                                     (("csrf", csrf), ("version", version)))]


def file_block(record: CaseRecord, document: Document) -> Block:
    return Block(document.title, (Line(w.FILE_INTRO), *(Line(line, Voice.FILE) for line in document.lines),
                                  Line(w.FILE_HASH.format(sha256=document.sha256)),
                                  Link(w.FILE_DOWNLOAD, case_href(record.dispute_id, "file", f"{document.sha256}.pdf"))))


def claude_items(board: Board, record: CaseRecord, now: Current) -> tuple[Item, ...]:
    """What Claude read for the round. When current() can't say (a saved row won't load, or the rules or code failed),
    the page can't show it, and What code found says why: never that Claude didn't read the round."""
    step = now.step
    reading = step.reading if step is not None else None
    if reading is not None:
        return claude_view(reading, board.store)
    if step is None:
        return (Line(w.CLAUDE_CANT_SHOW),)
    if step.basis is not None:
        return (Line(w.CLAUDE_MISSING),)
    if now.dropped:  # the round was read for a plan it no longer has
        return (Line(w.CLAUDE_DROPPED_PLAN),)
    if record.rounds[-1].plan is not None or (lifecycle.phase(record) is Phase.DECIDE and step.conclusion is None):
        return (Line(w.CLAUDE_NOT_ASKED),)  # decide()'s first call settled the round without the records
    if lifecycle.phase(record) is Phase.DECIDE:
        return (Line(w.CLAUDE_NOT_YET),)
    return (Line(w.CLAUDE_NOT_READ),)


def claude_view(reading: reader.Reading, store: Store) -> tuple[Item, ...]:
    """Any Reading's kept rows, flags, tags, dropped count and draft, as the case page shows them. Each kept quote shows
    in the record's voice under code's label (reader.describe()), never with Claude's own wording around it."""
    if reading.failure is not None:
        return (Line(w.CLAUDE_FAILED.format(why=reader.WHY.get(reading.failure, w.UNCLEAR)), mark="warn"),)
    items: list[Item] = []
    if not reading.complete:
        items.append(Line(w.CLAUDE_PARTIAL, mark="warn"))
    items.append(Line(w.CLAUDE_INTRO))
    kept = [row for row in reading.rows if row.kept]
    facts = [row for row in kept if row.part == "fact"]
    points = [row for row in kept if row.part == "conflict"]
    flags = [row for row in kept if row.part == "flag"]
    if facts:
        items.append(Block(w.CLAUDE_FACTS, tuple(quoted(row, store) for row in facts)))
    if points:
        items.append(Block(w.CLAUDE_POINTS, tuple(line for row in points for line in (
            quoted(row, store), Line(w.COUNTED.get(row.counted_as, w.UNCLEAR))))))
    if reading.reported:
        items.append(Line(w.CLAUDE_TAGS.format(orders=w.orders(reading.reported))))
    if flags:
        items.append(Block(w.CLAUDE_FLAGS, tuple(quoted(row, store) for row in flags)))
    if not (facts or points or flags or reading.reported):
        items.append(Line(w.CLAUDE_NOTHING))
    dropped = [row for row in reading.rows if not row.kept]
    if dropped:
        items.append(Line(w.CLAUDE_DROPPED.format(items=w.count(len(dropped), "item"))))
        items.append(Block(w.CLAUDE_DROPPED_WHY, tuple(Line(why) for why in dict.fromkeys(row.why for row in dropped)),
                           open=False))
    if reading.draft is not None:
        items.append(draft_block(reading.draft))
    return tuple(items)


def quoted(row: reader.Row, store: Store) -> Line:
    voice = Voice.BANK if row.record_id.strip() == prompts.BANK_COMMENT else Voice.RECORD
    return Line(row.quote, voice, reader.describe(row, store))


def draft_block(draft: reader.Draft) -> Block:
    """Claude's draft, collapsed, with code's checks on it. Code never used it."""
    action = w.DRAFT_ACTIONS.get(draft.action, w.UNCLEAR)
    items: list[Item] = [Line(draft.reason, Voice.CLAUDE, w.DRAFT_ACTION.format(action=action))]
    if draft.refund_amount:
        items += [Line(draft.refund_amount, Voice.CLAUDE, w.DRAFT_REFUND),
                  Line(w.DRAFT_CARD_YES if draft.to_card_that_paid else w.DRAFT_CARD_NO)]
    if draft.note:
        items.append(Line(draft.note, Voice.CLAUDE, w.DRAFT_NOTE))
    if draft.problems:
        items.append(Block(w.DRAFT_CHECKS, tuple(Line(problem) for problem in draft.problems)))
    else:
        items.append(Line(w.DRAFT_FINE))
    return Block(w.DRAFT, tuple(items), open=False)


def sends_block(board: Board, record: CaseRecord) -> Block | None:
    """Every POST try, oldest first: the round whose plan it sent, when, what the answer meant and its status, then
    Airwallex's phrase on a line of its own, unless it only says again what the outcome says, as a 2xx's does."""
    tries = board.sends.get(record.dispute_id, ())
    if not tries:
        return None
    items: list[Item] = []
    for attempt in tries:
        outcome = w.OUTCOMES.get(attempt.outcome, w.OUTCOME_OTHER)
        moment = when(attempt.at, board.now)
        if attempt.round_number is not None:
            moment = w.SEND_ROUND.format(number=attempt.round_number, when=moment)
        if attempt.status is None:
            items.append(Line(w.SEND_TRY.format(when=moment, outcome=outcome)))
        else:
            items.append(Line(w.SEND_TRY_STATUS.format(when=moment, outcome=outcome, status=attempt.status)))
        if attempt.detail and not same_words(attempt.detail, outcome):
            items.append(Line(attempt.detail))
    return Block(w.SENDS_TITLE, tuple(items))


def same_words(one: str, other: str) -> bool:
    """Whether two phrases say the same thing word for word, but for case and a final full stop."""
    return one.removesuffix(".").casefold() == other.removesuffix(".").casefold()


def rounds_block(board: Board, record: CaseRecord) -> Block:
    """Every round, oldest first, with every note: lifecycle's own sentences, as written. In the demo a later round says
    why the bank asked again so soon, in the words of what answered for Airwallex in the recording."""
    blocks = []
    for number, here in enumerate(record.rounds, 1):
        items: list[Item] = [Line(w.ROUND_SEEN.format(when=when(here.opened_at, board.now)))]
        comment = here.issuer_comment
        items.append(Line(comment, Voice.BANK, w.BANK_WROTE) if comment.strip() else Line(w.BANK_SILENT))
        if here.reply is not None:
            verb, stage = w.VERBS.get(here.reply.action.value, w.UNCLEAR), stage_name(here.reply.stage)
            items.append(Line(w.REPLY_OURS.format(verb=verb, stage=stage, reason=reason_text(here.reply.reason))
                              if ours(here.reply, here.plan) else w.REPLY_OTHER.format(verb=verb, stage=stage)))
        if here.plan is not None:
            summary = approvals.summary(here.plan.terms)
            if here.plan.sending_at is None:
                items.append(Line(w.UNSENT_PLAN.format(summary=summary)))
            else:
                items.append(Line((w.REFUSED_PLAN if here.plan.refused is not None else w.SENT_PLAN).format(
                    summary=summary)))
        notes = [Line(note) for note in dict.fromkeys(here.notes)]
        if here.person:
            notes.append(Line(w.PERSON_LINE.format(why=here.person)))
        items.append(Block(w.NOTES, tuple(notes) or (Line(w.NO_NOTES),)))
        if board.mode is Mode.DEMO and number > 1:
            items.append(Line(w.NOTE_WEEKS if on_sandbox(board.demo) else w.NOTE_WEEKS_STANDIN, mark="note"))
        blocks.append(Block(w.ROUND.format(number=number, stage=stage_name(here.stage)), tuple(items)))
    return Block(w.ROUNDS, tuple(blocks))


def approvals_block(board: Board, record: CaseRecord) -> Block:
    """Every approval row, oldest first, and whether it stands. An earlier round's approval whose plan this app sent
    says so: it was used, and it is void now that the bank asked again. Every other row says what void_reason() says."""
    rows = board.approvals.get(record.dispute_id, ())
    items: list[Item] = []
    for row in rows:
        items.append(Line(w.APPROVAL.format(by=row.by, number=row.round_number, when=when(row.at, board.now))))
        try:
            void = w.APPROVAL_SENT_VOID if went_out(record, row) else approvals.void_reason(record, row)
        except Exception:  # a row that loaded but can't be compared: say so, never guess
            LOG.exception("Code hit an error while it checked an approval.")
            void = w.APPROVAL_UNCLEAR
        items.append(Line(void or w.APPROVAL_STANDS))
    return Block(w.APPROVALS, tuple(items) or (Line(w.NO_APPROVALS),))


def went_out(record: CaseRecord, row: ApprovalRecord) -> bool:
    """Whether the row approved the plan of an earlier round that this app then sent: that round's plan went out, with
    the row's approver, request id and plan digest on it. A plan code made again before the send has a new digest, so an
    approval of the plan it replaced isn't the sent plan's, though its terms, and so its request id, are the same."""
    if row.dispute_id != record.dispute_id or not 1 <= row.round_number < len(record.rounds):
        return False
    plan = record.rounds[row.round_number - 1].plan
    return plan is not None and plan.sending_at is not None and (plan.approved_by, plan.request_id) == (
        row.by, row.request_id) and approvals.plan_sha256(plan) == row.plan_sha256


def money_block(record: CaseRecord) -> Block:
    entries = [Line(w.ENTRY.format(label=entry.label, amount=amount(entry.amount))) for entry in lifecycle.entries(record)]
    return Block(w.CASE_MONEY, (*(entries or [Line(w.NOTHING_BOOKED)]), cost_line(record)))


def records_block(board: Board, record: CaseRecord) -> Block:
    """The store's records behind the case, as the store holds them: each value in the record's own words."""
    store, order = board.store, order_of(board.store, record)
    if order is None:
        return Block(w.RECORDS, (Line(w.RECORDS_NONE),), open=False)
    number = order.number
    serials = dict.fromkeys(item.serial for item in order.items if item.serial)
    found = (("orders", store.get_order_history(number)), ("scans", store.get_carrier_scans(number)),
             ("emails", store.search_inbox(number)),
             ("warranty", tuple(entry for serial in serials for entry in store.get_warranty_log(serial))))
    items: list[Item] = [Line(w.RECORDS_INTRO)]
    for kind, kept in found:
        items.append(Block(w.RECORD_KINDS[kind].format(number=number),
                           tuple(record_view(one, store) for one in kept) or (Line(w.RECORDS_EMPTY),), open=False))
    return Block(w.RECORDS, tuple(items))


def record_view(record: object, store: Store) -> Block:
    """One store record: each field's plain name, and each of its values verbatim, cut at MAX_VALUE characters. A field
    of parts, such as an order's items, shows each part on its own, each value under its own name. A value with nothing
    in it shows no line."""
    items: list[Item] = []
    for item in fields(record):
        if item.name == "id":
            continue
        value = getattr(record, item.name)
        if isinstance(value, tuple) and value and all(is_dataclass(part) for part in value):
            for number, part in enumerate(value, 1):
                title = w.RECORD_PARTS[item.name].format(n=number) if item.name in w.RECORD_PARTS else \
                    w.RECORD_PART.format(name=field_name(item.name), n=number)
                items.append(Block(title, tuple(value_lines(part)) or (Line(w.RECORDS_EMPTY),)))
        else:
            items += value_lines(record, item.name)
    return Block(fact_label(record.id, store), tuple(items) or (Line(w.RECORDS_EMPTY),))


def value_lines(record: object, *only: str) -> list[Line]:
    """Each value of a record's fields (or only of these), verbatim under the field's plain name, as quote_is_in() reads
    them, cut at MAX_VALUE characters. A value with nothing in it is left out."""
    lines = []
    for item in fields(record):
        if item.name == "id" or (only and item.name not in only):
            continue
        for value in field_text(record, item.name) or ():
            if not value.strip():
                continue
            lines.append(Line(value[:MAX_VALUE], Voice.RECORD, field_name(item.name)))
            if len(value) > MAX_VALUE:
                lines.append(Line(w.RECORD_CUT.format(count=len(value) - MAX_VALUE)))
    return lines


def field_name(name: str) -> str:
    return reader.FIELD_NAMES.get(name, w.PART_NAMES.get(name, name.replace("_", " ")))


# ----------------------------------------------------------------------------- the approval screen
def approval(board: Board, dispute_id: str, *, csrf: str) -> ApproveView | None:
    record = board.records.get(dispute_id)
    if record is None:
        broken = broken_view(board, dispute_id)
        return None if broken is None else ApproveView(broken.title, (), broken.blocks, (), None)
    number = record.last.order_number
    title = w.APPROVE_TITLE.format(number=number) if number else w.APPROVE_TITLE_NO_ORDER.format(id=dispute_id)
    plan, step_now = record.rounds[-1].plan, lifecycle.phase(record)
    if step_now is not Phase.APPROVE or plan is None:
        lines: list[Item] = [Line(w.NOTHING_TO_APPROVE), Line(phase_line(record)), *person_lines(record)]
        if step_now is Phase.PERSON and plan is None:
            lines.append(Line(w.PERSON_RESPONDS))
        lines.append(Link(w.BACK_CASE, case_href(dispute_id)))
        return ApproveView(title, (), (Block(w.NOW_TITLE, tuple(lines)),), (), None)
    now = current(board, record)
    step = now.step
    files = case_files(board, record)
    lead = (Line(approvals.tier_label(record)), Line(review_line(record)), *person_lines(record))
    blocks = [terms_block(board, plan, open=True, title=w.SENDS), why_block(plan),
              files_block(board, record, files, csrf=csrf), found_block(board, record, now, files, csrf=csrf),
              Block(w.READING_BESIDE, reading_beside(board, record, now)), rounds_block(board, record)]
    closing: list[Item] = [Line(w.FRESH)]
    if plan.terms.action is Action.CHALLENGE:  # what goes beside the terms and files: Airwallex's, and fixed fields
        closing += [Line(w.AIRWALLEX_ADDS), Line(w.SENDS_ONLY)]
    form = None
    if board.approver is None:
        closing.append(Line(w.APPROVALS_OFF, mark="warn"))
    elif step is not None and files.moved is None:
        key = step.reading.key if step.reading is not None else ""
        form = Form(case_href(dispute_id, "approve"), w.APPROVE_BUTTON,
                    (("csrf", csrf), ("plan", plan_text(plan)), ("reading", key)),
                    Line(w.APPROVE_AS.format(name=board.approver)))
    return ApproveView(title, lead, tuple(block for block in blocks if block), tuple(closing), form)


def review_line(record: CaseRecord) -> str:
    """What approving books, as lifecycle.approve() books it: a review once a round for a challenge or a hand-off, and
    none for an accept code made itself."""
    plan = record.rounds[-1].plan
    if plan.terms.action is Action.CHALLENGE or plan.hand_off:
        if any(booking.key == f"review:{record.rounds[-1].stage.value}" for booking in record.bookings):
            return w.REVIEW_DONE
        return w.REVIEW_BOOKS.format(cost=amount(FEES_V1.review_cost))
    return w.REVIEW_NONE


def reading_beside(board: Board, record: CaseRecord, now: Current) -> tuple[Item, ...]:
    """The reading the plan was made on, in counts, with Claude's draft; else what the case page says of it."""
    reading = now.step.reading if now.step is not None else None
    if reading is None or reading.failure is not None:
        return claude_items(board, record, now)
    kept = [row for row in reading.rows if row.kept]
    counts = {part: sum(row.part == part for row in kept) for part in ("fact", "conflict", "flag")}
    quoted_from = {row.record_id for row in kept if row.part == "fact"}  # one record can give several quotes
    items: list[Item] = [Line(w.READING_SUMMARY.format(
        quotes=w.count(counts["fact"], "line"), records=w.count(len(quoted_from), "record"),
        points=w.count(counts["conflict"], "point"), flags=w.count(counts["flag"], "line"),
        dropped=w.count(sum(not row.kept for row in reading.rows), "item")))]
    if not reading.complete:
        items.append(Line(w.CLAUDE_PARTIAL, mark="warn"))
    if reading.draft is not None:
        items.append(draft_block(reading.draft))
    items.append(Link(w.READING_QUOTES, case_href(record.dispute_id)))
    return tuple(items)


# ----------------------------------------------------------------------------- money
def money_view(board: Board) -> MoneyView:
    records = in_queue_order(money.handled(board.records.values()))
    total = money.totals(records)
    lost = unread(board)  # cases with no record that loads: their money is in none of the sums
    summary = [Line(w.MONEY_SUMMARY.format(cost=amount(total.cost), risk=amount(total.at_risk))), *left_out(total)]
    if lost:
        summary.append(Line(w.MONEY_BROKEN.format(cases=w.count(len(lost), "case"))))
    if records:
        summary.append(Line(w.ACCEPT_ALL.format(cost=amount(total.accept_everything))))
        if not total.final:
            summary.append(Line(w.NOT_FINAL))
        elif lost:
            summary.append(Line(w.NOT_FINAL_UNREAD))  # a case this app can't read may still be open
        else:
            difference = total.accept_everything - total.cost
            summary.append(Line(w.CHEAPER.format(difference=amount(difference)) if difference > 0 else
                                w.DEARER.format(difference=amount(-difference)) if difference < 0 else w.SAME))
    else:  # a case this app can't read may have cost something: never that nothing did
        summary.append(Line(w.NOT_FINAL_UNREAD if lost else w.MONEY_EMPTY))
    rows = []
    for record in records:
        entries = tuple(Line(w.ENTRY.format(label=entry.label, amount=amount(entry.amount)))
                        for entry in lifecycle.entries(record)) or (Line(w.NOTHING_BOOKED),)
        summed = money.summed(record)  # another currency than the fee table's adds up with none of its fees
        priced = money.accept_everything(record) if summed else None
        cells = (who(record), phase_line(record), None, amount(lifecycle.cost(record)) if summed else w.CANT_SAY,
                 amount(lifecycle.at_risk(record), record.last.dispute.currency),
                 amount(priced) if priced is not None else w.CANT_SAY)
        rows.append(tuple(entries if cell is None else (Line(cell),) for cell in cells))
    notes = [Line(w.FEES.format(version=FEES_V1.version, chargeback=amount(FEES_V1.chargeback_fee),
                                refund=amount(FEES_V1.refund_fee), review=amount(FEES_V1.review_cost),
                                later=amount(FEES_V1.later_stage_fees))),
             Line(w.AT_RISK_NOTE), Line(w.ACCEPT_NOTE)]
    notes += [Line(w.CANT_SAY_NOTE.format(stage=stage_name(stage))) for stage in dict.fromkeys(total.unpriced)]
    if board.mode is Mode.DEMO:
        chance = FEES_V1.chargeback_win_chance.get(EvidenceClass.INDEPENDENT_POSSESSION)
        notes += [Line(w.NOTE_FEE.format(later=amount(FEES_V1.later_stage_fees)), mark="note"),
                  Line(w.NOTE_CHANCES.format(chance=percent(chance) if chance is not None else w.UNCLEAR,
                                             version=FEES_V1.version), mark="note")]
        if board.demo is not None and board.demo.finished and board.demo.total is not None:
            summary.append(Line(w.RECORDED_TOTAL.format(total=amount(board.demo.total))))
    table = Table(w.COLUMNS, tuple(rows)) if rows else None
    return MoneyView(tuple(summary), table, Block(w.NOTES_TITLE, tuple(notes)))


# ----------------------------------------------------------------------------- refusals
RESULTS = {  # a refused Result's key: the status and code's words for it
    "approvals-off": (409, w.APPROVALS_OFF), "no-case": (404, w.NO_CASE), "broken": (409, w.BROKEN),
    "plan-unreadable": (400, w.PLAN_UNREADABLE), "refused": (409, w.NOT_APPROVED),
    "reading-changed": (409, w.READING_CHANGED), "file-moved": (409, ""), "not-recorded": (409, w.NOT_RECORDED),
    "stale": (409, w.STALE), "not-in-recording": (409, w.NOT_IN_RECORDING), "drop-not-needed": (409, w.DROP_NOT_NEEDED),
    "demo-only": (409, w.ONLY_DEMO), "live-only": (409, w.ONLY_LIVE), "demo-moved": (409, w.DEMO_MOVED),
    "demo-mismatch": (409, w.DEMO_MISMATCH), "demo-finished": (409, w.DEMO_FINISHED), "no-ending": (409, w.NO_ENDING),
}
DROP_RESULTS = {**RESULTS, "refused": (409, w.NOT_DROPPED)}  # the drop form's: lifecycle.drop()'s sentence follows


def succeeded(result: Result) -> bool:
    """A double click on the same plan is no refusal: the approval stands, so the page goes on as for a first one."""
    return (result.ok and result.key in SUCCESS) or result.key == "already"


def refused(board: Board, result: Result, dispute_id: str | None, *, csrf: str,
            drop: bool = False) -> tuple[int, RefusedView]:
    """The refusal page for a Result that didn't work: code's words for its key, then the module's own sentence as
    written, and where the case is now. drop: the drop form's Result, so its words are the drop's and the way back is
    the case, not the approval page. A file-moved refusal says what the case page says: that the plan's file can't be
    built again only while files_for() refuses it, with files_for()'s own sentence (never a reading's, which would read
    as the cause), and else that a saved row of the round won't load. In the demo, an approval the recording didn't make
    says to press Next only while the bar offers it."""
    status, words = (DROP_RESULTS if drop else RESULTS).get(result.key, (409, w.UNKNOWN_RESULT))
    record = board.records.get(dispute_id) if dispute_id is not None else None
    lines: list[Item] = []
    if result.key == "file-moved":  # its file can't be built again, or a saved row its plan was made on won't load
        moved = case_files(board, record).moved if record is not None else None
        lines.append(Line(w.FILE_MOVED.format(why=moved) if moved is not None else w.ROUND_UNREADABLE.format(
            why=unreadable_why(result.message, dispute_id or "")), mark="warn"))
        if record is not None:
            lines += drop_items(board, record, csrf=csrf)
    else:
        if result.key == "not-in-recording":  # it says to press Next only while the bar has a Next button
            words = (w.NOT_IN_RECORDING if next_offered(board) else w.NOT_IN_RECORDING_NO_NEXT).format(
                approver=approver_of(board))
        lines.append(Line(words, mark="warn"))
        if result.message:
            lines.append(Line(result.message))
    if record is not None:
        lines.append(Line(w.NOW_LINE.format(now=phase_line(record))))
    links = []
    if record is not None:
        if not drop and result.key in ("refused", "reading-changed", "file-moved", "stale", "not-recorded"):
            links.append(Link(w.BACK_APPROVAL, case_href(record.dispute_id, "approve")))
        links.append(Link(w.BACK_CASE, case_href(record.dispute_id)))
    links.append(Link(w.BACK_QUEUE, "/"))
    return status, RefusedView(tuple(lines), tuple(links))


def plain_refusal(message: str) -> RefusedView:
    """A refusal with one fixed sentence and the way back: a form from elsewhere or one this app can't read, a page
    or a file that isn't there, or an error."""
    return RefusedView((Line(message, mark="warn"),), (Link(w.BACK_QUEUE, "/"),))
