"""The disputable command: the queue in the terminal, the agent's passes, the screens, the demo, the story run, the
bank's preflight and the eval.

    disputable status [--data P]                          the queue from the database: no network and no key
    disputable pass [--data P] [--cap D] [--yes]          one pass on the sandbox; Claude reads when a key is set
    disputable loop [--every 20] [--data P] [--cap D] [--yes]
                                                          passes until Ctrl+C, after one spend question
    disputable serve [--approver NAME] [--port 8765] [--no-open] [--data P]
                                                          the screens over the live database
    disputable demo [--ending 1|2] [--port 8765] [--no-open] [--no-serve]
                                                          the recorded story: no .env, no key and no network
    disputable story --ending 1|2 [--no-claude] [--lenient] [--cap D] [--yes]
                                                          the live story run that records the demo
    disputable bank preflight [--yes]                     three test payments, to see the sandbox take the app's requests
    disputable eval [ARGS...]                             the eval of Claude's reading (disputable.evals)

Settings come from the .env file in the folder the command runs from, never from the process environment, and only the
commands that need them read it: the demo reads none. The Claude client is reader.claude_client(), which takes no
address, header or proxy from the shell either. serve never runs a pass and builds no Airwallex client and no Claude
reader; with no --approver, approvals are off. The agent reads only the disputes on a story run's own payments, so pass
and loop say so on a data folder with no story run. Before a live Claude read, pass and loop ask once, with a dollar cap
they hold to, and read at most the reads they named; a read the cap or the count stopped goes to a person with code's
draft, and the pass says how many. Each pass prints its alerts as they stand once it ended. A setup problem, a refused
bank step or a replay that stopped matching its recording prints its own plain message, with no traceback, and exits 1:
for the demo's replay, the line the demo's bar shows. The commands that start the screens print their first line before
the slow imports. The demo plays the recordings that come with a clone of the repository, and says whether each was
recorded on the Airwallex sandbox or made on the tests' stand-in for it (scripts/record_standin.py), and whether it holds
Claude's readings. A clone with no recording says how to make the stand-in, with no keys.

python -m disputable runs the same commands. The tests run main(argv) in this process.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

import disputable
from disputable.clock import ReplayMismatch, wall
from disputable.web import words as w

if TYPE_CHECKING:
    from disputable.db import Snapshot
    from disputable.desk import DemoBar
    from disputable.lifecycle import CaseRecord
    from disputable.loop import PassReport
    from disputable.service import Service
    from disputable.store import Store

ENV_FILE = Path(".env")  # in the folder the command runs from
STORY = Path(disputable.__file__).resolve().parents[2] / "fixtures" / "story"  # the committed recordings: ending-1, ...
LIVE_DB = "live.sqlite3"  # service.LIVE_DB, named here so status never loads the Claude layer
ENDINGS = ("1", "2")  # story.ENDINGS, named here so the demo never loads the story run
PORT, EVERY = 8765, 20
ENDING = re.compile(r"[0-9A-Za-z_-]{1,16}")
MAX_APPROVER = 60
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# status
NO_DATABASE = "There's no database in {folder} yet. The pass and loop commands make it."
HEAD = "Disputable: {count} in {path}. {last}"
LAST_PASS = "Last pass {when}."
NO_PASS = "No pass yet."
WAITS = "Waits for the bank."
BEING_SENT = "Being sent."
BROKEN = "This app can't read its saved record. {why}"
TOTALS = "At risk: {risk}. Cost so far: {cost}."
LAST_PROBLEMS = "Problems on the last pass"
ALERTS = "Alerts"

# pass and loop
PASS_LINE = "Pass {id} at {when}: read {read}, sent {sent}, decided {decided}."
SENT = "Sent: {who}. {what}."
OUTCOMES = {"taken": "Airwallex took it", "failed": "Airwallex refused it",
            "retry": "No clear answer, so the same request goes again after the next read"}
PROBLEMS = "Problems"
ZERO_READS = "0 live reads needed."
NO_SCOPE = ("{folder} holds no story run, so a pass here reads nothing: the agent reads only the disputes on a story "
            "run's own payments. A story run (disputable story --ending 1) makes a story-<run> folder in it: use --data "
            "with that folder.")
NEED_CAP = "This {run} may make {reads}. Give it a dollar cap, such as --cap 3."
PASS_WHAT = "This pass reads the cases that need it with Claude ({model}, effort {effort}), with your key."
LOOP_WHAT = ("This loop reads the cases that need it with Claude ({model}, effort {effort}), with your key, until you "
             "press Ctrl+C. A round Claude was busy for is read again on the next pass, and that counts as a read. "
             "Start the loop again for more reads.")
STOPPED_READS = "The spend cap or the read count stopped {reads}. {cases} to a person, with code's draft."
SPENT = "Claude's live reads cost {spent} in {requests} request{s}."
NEXT_PASS = "Next pass in {seconds} seconds. Press Ctrl+C to stop."
LOOP_STOPPED = "The loop stopped."

# demo
NO_RECORDING = ("There's no recording in {path}. Record one with `disputable story --ending {ending}` (it needs the "
                "sandbox keys).")
NO_RECORDING_CLONE = ("There's no recording in {path}. Record one with `disputable story --ending {ending}` (it needs "
                      "the sandbox keys). Or make a stand-in, with no keys: uv run python scripts/record_standin.py "
                      "--ending {ending} (add --no-claude if it says the Claude readings aren't there).")
NO_RECORDINGS = ("There are no recordings in {path}. They come with a clone of the repository: run the demo from the "
                 "clone, after uv sync.")  # an install that isn't a clone
NO_RECORDINGS_CLONE = ("There are no recordings in {path}. Make a stand-in, with no keys: uv run python "
                       "scripts/record_standin.py (add --no-claude if it says the Claude readings aren't there).")
NO_ENDING = "There's no ending {name}. The endings are 1 and 2."  # story.NO_ENDING
DEMO_TITLE = "Disputable demo. {title}."
DEMO_FROM = "Replayed from {folder}, recorded on the Airwallex sandbox on {day}."
DEMO_FROM_STANDIN = ("Replayed from {folder}, made on a stand-in for the Airwallex sandbox. It isn't a recording of "
                     "Airwallex.")
DEMO_OFFLINE = "No keys and no network: every Airwallex answer and Claude reading comes from the recording."
DEMO_OFFLINE_STANDIN = "No keys and no network: every answer and Claude reading comes from the recording."
DEMO_OFFLINE_NONE = "No keys and no network: every Airwallex answer comes from the recording. It holds no Claude reading."
DEMO_OFFLINE_STANDIN_NONE = "No keys and no network: every answer comes from the recording. It holds no Claude reading."

# the command line
STOPPED = "Stopped by you."
BAD_CAP = "--cap is a dollar amount above 0, such as 3 or 2.50."
BAD_PORT = "--port is a whole number from 1 to 65535."
BAD_EVERY = "--every is a whole number of seconds, 1 or more."
BAD_ENDING = "--ending is an ending's name, such as 1 or 2."


# ----------------------------------------------------------------------------- what a live command reaches
# The tests put the fake sandbox, a fake clock and a fake Claude client here.
def _transport() -> Any:
    """The transport under the agent's Airwallex session: None, httpx's own, which takes nothing from the environment."""
    return None


def _throttle(seconds: float) -> None:
    """The session's wait between two calls."""
    time.sleep(seconds)


def _claude(api_key: str) -> Any:
    """The live Claude client for the user's own key: reader.claude_client(), the one builder, which names the API's
    address and takes no header, proxy or address from the shell."""
    from disputable.reader import claude_client

    return claude_client(api_key)


def _ask(question: str) -> str:
    return input(question)


sleep: Callable[[float], None] = time.sleep  # the loop's wait between two passes


# ----------------------------------------------------------------------------- the commands
def main(argv: list[str] | None = None) -> int:
    """The disputable command, with argv as the shell gave it (sys.argv[1:] when None). The exit code: 0 done, 1 a
    plain message said why not, 2 the command line was wrong. The eval's own codes come back as they are."""
    given = list(sys.argv[1:] if argv is None else argv)
    _plain_output()
    if given[:1] == ["eval"]:  # the eval reads its own arguments, --help included
        return _guarded(lambda: _eval(given[1:]))
    parser = _parser()
    try:
        args = parser.parse_args(given)
    except SystemExit as stop:  # argparse printed its own line: a usage error, or --help
        return stop.code if isinstance(stop.code, int) else 1
    if args.command is None:
        parser.print_help()
        return 1
    return _guarded(lambda: COMMANDS[args.command](args))


def _guarded(command: Callable[[], int]) -> int:
    """The command's exit code. A plain message is printed as it is, with no traceback: 1. Anything else is a bug and
    comes out with its traceback."""
    try:
        return command()
    except KeyboardInterrupt:
        print(STOPPED)
        return 1
    except Exception as error:
        if not _plain(error):
            raise
        print(str(error))
        return 1


def _eval(args: list[str]) -> int:
    from disputable import evals

    return evals.main(args)


def _status(args: argparse.Namespace) -> int:
    """The queue from the database: no network, no key, and nothing written."""
    from disputable.db import Database

    settings = _settings(args, need_airwallex=False)
    path = settings.data_dir / LIVE_DB
    if not path.exists():
        print(NO_DATABASE.format(folder=settings.data_dir))
        return 0
    db = Database(path, stamp=wall)
    for line in queue_lines(db.snapshot(), wall(), path):
        print(line)
    return 0


def _pass(args: argparse.Namespace) -> int:
    from disputable.loop import run_pass

    run = _live(args, loop=False)
    if run is None:
        return 1
    desk, session, meter, allowance = run
    try:
        report = run_pass(desk)
    finally:
        session.close()
    for line in report_lines(report, desk):
        print(line)
    _stopped(allowance, 0)
    _spent(meter)
    return 1 if report.skipped else 0


def _loop(args: argparse.Namespace) -> int:
    from disputable.loop import run_pass

    run = _live(args, loop=True)
    if run is None:
        return 1
    desk, session, meter, allowance = run
    try:
        while True:
            before = allowance.stopped if allowance is not None else 0
            report = run_pass(desk)
            for line in report_lines(report, desk):
                print(line)
            _stopped(allowance, before)
            _spent(meter)
            print(NEXT_PASS.format(seconds=args.every), flush=True)
            sleep(args.every)
    except KeyboardInterrupt:  # how a loop is stopped
        print(LOOP_STOPPED)
        return 0
    finally:
        session.close()


def _serve(args: argparse.Namespace) -> int:
    """The screens over the live database. No pass, no Airwallex client and no reader."""
    if args.approver is not None and not _named(args.approver):
        print(w.BAD_APPROVER)
        return 1
    print(w.STARTING, flush=True)
    settings = _settings(args, need_airwallex=False)
    from disputable import service, web

    store = _store(service.open_live(settings).meta("store"))
    desk = service.live(settings, approver=args.approver, store=store)
    if desk.approver is None:
        print(w.APPROVALS_OFF)
    return web.serve(desk, port=args.port, open_browser=not args.no_open)


def _demo(args: argparse.Namespace) -> int:
    """The recorded story, from the recordings beside the package (a clone's fixtures/story): no .env, no key and no
    network. With no recording, a clone's line names the stand-in maker, which needs no keys. A replay that stops
    matching its recording prints the line the demo's bar shows for it, and exits 1."""
    if args.ending not in ENDINGS:
        print(NO_ENDING.format(name=args.ending))
        return 1
    clone = _clone(STORY)
    if not STORY.is_dir():  # an install that isn't a clone has no recordings, and a clone may have none yet
        print((NO_RECORDINGS_CLONE if clone else NO_RECORDINGS).format(path=STORY))
        return 1
    found = _recordings(STORY)
    if args.ending not in found:
        print((NO_RECORDING_CLONE if clone else NO_RECORDING).format(path=STORY / f"ending-{args.ending}",
                                                                     ending=args.ending))
        return 1
    print(w.STARTING, flush=True)
    from disputable.demo import Demo

    demo = Demo(found, store_for=_store, ending=args.ending)
    try:
        board = demo.board()
        bar = board.demo
        print(DEMO_TITLE.format(title=w.ENDINGS.get(bar.ending, w.ENDING_OTHER.format(ending=bar.ending))))
        print(_replayed_from(found[args.ending], bar))
        print(_offline(bar, board.reader.source), flush=True)
        for line in demo.warnings:  # another ending that can't be replayed, left out of the bar
            print(line, flush=True)
        if args.no_serve:
            try:
                demo.play_all(say=print)
            except ReplayMismatch as stopped:  # the demo keeps why it stopped: the bar's line says it, and what to do
                print(_mismatch(demo.board().demo, str(stopped)))
                return 1
            return 0
        from disputable import web

        return web.serve(demo, port=args.port, open_browser=not args.no_open)
    finally:
        demo.close()


def _story(args: argparse.Namespace) -> int:
    settings = _settings(args, need_airwallex=True)
    from disputable import story

    return story.run(args.ending, settings, claude=not args.no_claude, lenient=args.lenient, cap=args.cap, yes=args.yes,
                     ask=_ask)


def _bank(args: argparse.Namespace) -> int:
    settings = _settings(args, need_airwallex=True)
    from disputable import issuer

    return issuer.preflight(settings, yes=args.yes, ask=_ask)  # it asks its own question, with its own words


COMMANDS: Mapping[str, Callable[[argparse.Namespace], int]] = {
    "status": _status, "pass": _pass, "loop": _loop, "serve": _serve, "demo": _demo, "story": _story, "bank": _bank}


# ----------------------------------------------------------------------------- a live pass
class _Allowance:
    """Claude's reader, held to the reads the user said yes to, inside spend.BudgetReader. Past them it reads nothing
    and the read fails as CAPPED, the run's own limit, so the round goes to a person with code's draft, as when the
    dollar cap stops a read (BudgetReached passes through here to BudgetReader). stopped counts the reads either one
    stopped, for the pass's line. Every other attribute, the profile and the effort among them, is the reader's own,
    so the reading keys stay the same."""

    def __init__(self, reader: Any, limit: int) -> None:
        self.reader, self.limit, self.reads, self.stopped = reader, limit, 0, 0

    def read(self, request: Any) -> Any:
        from disputable.reader import Failure, failed
        from disputable.spend import BudgetReached

        if self.reads >= self.limit:
            self.stopped += 1
            return failed(request.key, Failure.CAPPED)
        self.reads += 1
        try:
            return self.reader.read(request)
        except BudgetReached:  # the dollar cap: BudgetReader, around this, makes it a failed read
            self.stopped += 1
            raise

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.reader, name)


def _live(args: argparse.Namespace, *, loop: bool) -> tuple[Any, Any, Any, _Allowance | None] | None:
    """The live Service of a pass or a loop, its Airwallex session, the spend meter and the read allowance (both None
    when Claude doesn't read), or None when the run stops before it starts: no cap, or no yes. One pass reads at most one
    case per payment (two open disputes on one payment both go to a person); a loop reads each round once, and again
    on the next pass while Claude is busy, and a case has one round a stage. A data folder with no story run's payments
    has nothing to read, and the line says so."""
    from disputable import airwallex, lifecycle, prompts, service, spend
    from disputable.airwallex import AgentClient, Session
    from disputable.config import NO_CLAUDE_KEY, confirm_spend
    from disputable.reader import ClaudeReader, NoReader

    settings = _settings(args, need_airwallex=True)
    db = service.open_live(settings)
    if db.meta("mode") is None:
        db.set_meta("mode", "live")
    payments = {payment.payment_id for payment in db.store_payments()}
    payments |= {record.last.payment_id for record in db.load().records.values()}
    reads = len(payments) * (len(lifecycle.RANK) if loop else 1)
    reader: Any = NoReader()
    meter, allowance = None, None
    if not payments:
        print(NO_SCOPE.format(folder=settings.data_dir))
    elif not settings.anthropic_api_key:
        print(NO_CLAUDE_KEY)
    else:
        if args.cap is None:
            print(NEED_CAP.format(run="loop" if loop else "pass", reads=w.count(reads, "live Claude read")))
            return None
        what = (LOOP_WHAT if loop else PASS_WHAT).format(model=prompts.MODEL, effort=prompts.EFFORT)
        if not confirm_spend(what, reads=reads, cap=args.cap, yes=args.yes, ask=_ask):
            return None
        meter = spend.Meter(args.cap)
        claude = ClaudeReader(meter.wrap(_claude(settings.anthropic_api_key)), profile=prompts.FERNHILL,
                              effort=prompts.EFFORT)
        allowance = _Allowance(claude, reads)
        reader = spend.BudgetReader(allowance, meter)
    session = Session(airwallex.credentials(settings), actor="agent", transport=_transport(), sleep=_throttle)
    desk = service.live(settings, approver=None, store=_store(db.meta("store")), reader=reader,
                        client=AgentClient(session))
    return desk, session, meter, allowance


def _stopped(allowance: _Allowance | None, before: int) -> None:
    """The pass's line when the cap or the read count stopped a read: those rounds went to a person."""
    count = (allowance.stopped if allowance is not None else 0) - before
    if count > 0:
        print(STOPPED_READS.format(reads=w.count(count, "Claude read"),
                                   cases="That case goes" if count == 1 else "Those cases go"))


def _spent(meter: Any) -> None:
    if meter is not None:
        print(SPENT.format(spent=_usd(meter.spent), requests=meter.requests, s="" if meter.requests == 1 else "s"))


# ----------------------------------------------------------------------------- what the terminal shows
def queue_lines(snapshot: Snapshot, now: datetime, path: Path) -> list[str]:
    """status: one line a dispute, soonest due first, with the round's notes under it; then the money, the last
    pass's problems and the alerts. A dispute first seen closed is left out and counted, as on the screens."""
    from disputable import approvals, lifecycle
    from disputable.lifecycle import Phase

    records = list(snapshot.records.values())
    shown = sorted((record for record in records
                    if not (record.history_only and lifecycle.phase(record) is not Phase.PERSON)),
                   key=lambda record: (_utc(record.last.dispute.due_at), record.dispute_id))
    rows = [(_who(record), _amount(record), record.rounds[-1].stage.value, phase_line(record, now))
            for record in shown]
    rows += [(f"Dispute {dispute_id}", "", "", BROKEN.format(why=why))
             for dispute_id, why in sorted(snapshot.broken.items())]
    last = snapshot.last_pass
    lines = [HEAD.format(count=w.count(len(rows), "dispute"), path=path,
                         last=LAST_PASS.format(when=_when(last.finished_at)) if last is not None and last.finished_at
                         else NO_PASS), ""]
    widths = [max((len(row[index]) for row in rows), default=0) + 2 for index in range(3)]
    notes = [_notes(record, row[3]) for record, row in zip(shown, rows)] + [[] for _ in snapshot.broken]
    for row, below in zip(rows, notes):
        lines.append("".join(cell.ljust(width) for cell, width in zip(row[:3], widths)) + row[3])
        lines += [" " * sum(widths) + note for note in below]
    hidden = sum(record.history_only and lifecycle.phase(record) is Phase.CLOSED for record in records)
    if hidden:
        lines.append(w.HIDDEN.format(count=hidden))
    if rows or hidden:
        lines.append("")
    mine = [record for record in records if not record.history_only]
    lines.append(TOTALS.format(risk=_usd(sum((lifecycle.at_risk(record) for record in mine), Decimal(0))),
                               cost=_usd(sum((lifecycle.cost(record) for record in mine), Decimal(0)))))
    lines += _listed(LAST_PROBLEMS, snapshot.problems)
    lines += _listed(ALERTS, [alert.message for alert in approvals.alerts(records, now)])
    return lines


def phase_line(record: CaseRecord, now: datetime) -> str:
    """Where a case is now, in one line: the person line at PERSON, the tier label while it waits for approval or to
    go out, blocker()'s line while it is being sent."""
    from disputable import approvals, lifecycle
    from disputable.lifecycle import Phase

    step = lifecycle.phase(record)
    if step is Phase.PERSON:
        return w.PERSON_LINE.format(why=lifecycle.person_reason(record))
    if step is Phase.DECIDE:
        return w.NOW["DECIDE"]
    if step in (Phase.APPROVE, Phase.SEND):
        return approvals.tier_label(record)
    if step is Phase.SENT:
        return lifecycle.blocker(record, now) or BEING_SENT
    if step is Phase.WAIT:
        return WAITS
    status = record.closed.value if record.closed is not None else ""
    return w.NOW["CLOSED"].format(status=w.STATUS.get(status, status.lower() or w.UNCLEAR))


def report_lines(report: PassReport, service: Service) -> list[str]:
    """What one pass did: the summary line, each response sent and what Airwallex's answer meant, the problems, and the
    alerts as they stand once the pass ended (over the saved cases, at its last moment). The report keeps the alerts
    from the pass's start, before it sent or decided, for the pass's row."""
    from disputable import approvals

    if report.skipped:
        return [report.skipped]
    records = service.db.load().records
    names = {dispute_id: _who(record) for dispute_id, record in records.items()}
    lines = [PASS_LINE.format(id=report.pass_id, when=_when(report.finished_at), read=w.count(len(report.read), "dispute"),
                              sent=len(report.sent), decided=len(report.decided))]
    lines += [SENT.format(who=names.get(dispute_id, f"Dispute {dispute_id}"), what=OUTCOMES[outcome.value])
              for dispute_id, outcome in report.sent]
    lines += _listed(PROBLEMS, report.problems)
    alerts = approvals.alerts(records.values(), report.finished_at, service.fees)
    lines += _listed(ALERTS, [alert.message for alert in alerts])
    return lines


def _notes(record: CaseRecord, shown: str) -> list[str]:
    """The current round's notes for a person, each once, under the line they explain: the person line, or the line
    shown for the case, which none of them repeats."""
    from disputable import lifecycle

    said = {lifecycle.person_reason(record), shown}
    return [note for note in dict.fromkeys(record.rounds[-1].notes) if note not in said]


def _listed(title: str, items: Iterable[str]) -> list[str]:
    items = list(items)
    return [f"{title}: none."] if not items else [f"{title}:", *(f"- {item}" for item in items)]


def _who(record: CaseRecord) -> str:
    """As loop.who() names a case: "Order #1109", or "Dispute dst_..." when the payment names no order."""
    number = record.last.order_number
    return f"Order #{number}" if number else f"Dispute {record.dispute_id}"


def _amount(record: CaseRecord) -> str:
    dispute = record.last.dispute
    return _usd(dispute.amount) if dispute.currency == "USD" else f"{dispute.amount:,.2f} {dispute.currency}"


def _usd(amount: Decimal) -> str:
    from disputable.fees import usd

    return usd(amount)


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.utcoffset() is None else moment.astimezone(timezone.utc)


def _when(moment: datetime) -> str:
    """Nov 9, 14:00 UTC, as the screens write a time."""
    moment = _utc(moment)
    return f"{MONTHS[moment.month - 1]} {moment.day}, {moment:%H:%M} UTC"


def _calendar(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def _replayed_from(folder: Path, bar: DemoBar) -> str:
    """The demo's line for where its recording comes from: recorded on the Airwallex sandbox, or made on the tests'
    stand-in for it (the manifest says which). A kind this code doesn't know never passes for the sandbox."""
    if bar.airwallex == "sandbox":
        return DEMO_FROM.format(folder=folder, day=_calendar(bar.recorded_on))
    return DEMO_FROM_STANDIN.format(folder=folder)


def _offline(bar: DemoBar, source: str) -> str:
    """The demo's line that nothing goes out. Only a recording made on the sandbox holds Airwallex's own answers, so
    any other kind's line names no Airwallex answer, by _replayed_from()'s rule. source is what read the cases, as the
    board's reader says: only Claude's own readings ("claude") are named as a Claude reading, so a recording made with
    --no-claude says it holds none."""
    sandbox = bar.airwallex == "sandbox"
    if source == "claude":
        return DEMO_OFFLINE if sandbox else DEMO_OFFLINE_STANDIN
    return DEMO_OFFLINE_NONE if sandbox else DEMO_OFFLINE_STANDIN_NONE


def _mismatch(bar: DemoBar, why: str) -> str:
    """The line the demo's bar shows when the replay stops matching its recording (views.demo_bar()): what stopped it,
    and what to do. A stand-in is made again by its maker, so only a recording made on the sandbox says to record the
    story again. why: the ReplayMismatch's own words, for a bar that holds none."""
    return (w.MISMATCH if bar.airwallex == "sandbox" else w.MISMATCH_STANDIN).format(mismatch=bar.mismatch or why)


# ----------------------------------------------------------------------------- settings, stores and recordings
def _settings(args: argparse.Namespace, *, need_airwallex: bool) -> Any:
    from disputable.config import load_settings

    return load_settings(ENV_FILE, need_airwallex=need_airwallex, data=args.data, allow_synced=args.allow_synced)


def _store(name: str | None) -> Store:
    """The seed's store a database or a recording names (story.STORES): Fernhill Audio, unless it names Ending 2's."""
    from disputable.seed.fernhill import ENDING_2, FERNHILL

    return {"fernhill": FERNHILL, "ending_2": ENDING_2}[name or "fernhill"]


def _recordings(folder: Path) -> dict[str, Path]:
    """The recordings in folder, by ending: ending-1 is "1". Only the story's endings: a backup such as ending-1-old is
    no ending."""
    return {name: folder / f"ending-{name}" for name in ENDINGS if (folder / f"ending-{name}").is_dir()}


def _clone(story: Path) -> bool:
    """Whether the recordings' folder is in a clone of the repository: the folder that holds fixtures/ holds a
    pyproject.toml. Only a clone has the stand-in maker, scripts/record_standin.py."""
    return (story.parent.parent / "pyproject.toml").is_file()


def _named(name: str) -> bool:
    """A person's name, as the Service takes one: 1 to 60 printable characters once stripped, never the agent's."""
    name = name.strip()
    return 1 <= len(name) <= MAX_APPROVER and name.isprintable() and name.casefold() != "disputable"


def _plain(error: Exception) -> bool:
    """A setup problem, a refused bank step or a replay that stopped matching: the message is the whole text."""
    from disputable.clock import ReplayMismatch
    from disputable.config import SetupError
    from disputable.issuer import IssuerError

    return isinstance(error, (SetupError, IssuerError, ReplayMismatch))


def _plain_output() -> None:
    """A character the terminal can't show prints as ?, never as a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):  # not a text stream, or one that can't change now
            pass


# ----------------------------------------------------------------------------- the command line
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disputable", description="Disputable: the dispute response agent.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    status = commands.add_parser("status", help="the queue from the database, with no network and no key")
    _data(status)
    one = commands.add_parser("pass", help="one pass on the Airwallex sandbox")
    _data(one)
    _spend(one)
    loop = commands.add_parser("loop", help="passes until Ctrl+C")
    loop.add_argument("--every", type=_seconds, default=EVERY, metavar="SECONDS", help="the wait between passes")
    _data(loop)
    _spend(loop)
    serve = commands.add_parser("serve", help="the screens over the live database, with no pass")
    serve.add_argument("--approver", metavar="NAME", help="who approves here; with none, approvals are off")
    _screens(serve)
    _data(serve)
    demo = commands.add_parser("demo", help="the recorded story, with no keys and no network")
    demo.add_argument("--ending", type=_ending, default="1", help="the ending to replay: 1 or 2")
    demo.add_argument("--no-serve", action="store_true", help="play every step here, then the totals")
    _screens(demo)
    story = commands.add_parser("story", help="the live story run that records the demo")
    story.add_argument("--ending", type=_ending, required=True, help="the ending to record: 1 or 2")
    story.add_argument("--no-claude", action="store_true", help="read nothing with Claude: read cases go to a person")
    story.add_argument("--lenient", action="store_true", help="record a plan the story doesn't expect as it is")
    _spend(story)
    _data(story)
    bank = commands.add_parser("bank", help="the simulated bank")
    steps = bank.add_subparsers(dest="step", metavar="STEP", required=True)
    preflight = steps.add_parser("preflight", help="three test payments: does the sandbox take the app's requests?")
    preflight.add_argument("--yes", action="store_true", help="don't ask first")
    _data(preflight)
    commands.add_parser("eval", add_help=False, help="the eval of Claude's reading: disputable eval --help")
    return parser


def _data(command: argparse.ArgumentParser) -> None:
    command.add_argument("--data", type=Path, metavar="P", help="the data folder (else DISPUTABLE_DATA, else the user's)")
    command.add_argument("--allow-synced", action="store_true", help="let the data folder be one a sync client watches")


def _spend(command: argparse.ArgumentParser) -> None:
    command.add_argument("--cap", type=_dollars, metavar="D", help="the most Claude's live reads may cost, in dollars")
    command.add_argument("--yes", action="store_true", help="don't ask before Claude reads")


def _screens(command: argparse.ArgumentParser) -> None:
    command.add_argument("--port", type=_port, default=PORT, help="the port on 127.0.0.1")
    command.add_argument("--no-open", action="store_true", help="don't open the browser")


def _dollars(text: str) -> Decimal:
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(BAD_CAP) from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError(BAD_CAP)
    return amount


def _port(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(BAD_PORT)
    return int(text)


def _seconds(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", text) or int(text) < 1:
        raise argparse.ArgumentTypeError(BAD_EVERY)
    return int(text)


def _ending(text: str) -> str:
    if not ENDING.fullmatch(text):
        raise argparse.ArgumentTypeError(BAD_ENDING)
    return text


if __name__ == "__main__":
    raise SystemExit(main())
