"""The eval: how well Claude reads the cases, scored by code alone. No model grades another model.

It reads the 83 cases whose records get read (disputable.seed.read_cases), the story's round two dated as the app dates
it, and 14 controls: each case with a planted line, read again on a store with the line cut out. Every reading is
merged and decided as the app does it, then scored against code alone and the answer key: the decision, I1 to I3, facts
by record, planted lines (read, flagged, and whether they changed anything), reported orders, the tool log, failures and
cost. Scripted readers mark the ceiling (perfect), the floor (empty), the app with no key, and seven attacks the scoring
must catch. Live readings are kept in fixtures/readings, so a replay costs nothing and the story run reuses them.

Commands (disputable eval ARGS, or python -m disputable.evals ARGS):
    (none)       replay the kept readings and write docs/eval/report.md and results.json. No API call.
    scripted     score the scripted readers and print their totals (--reader NAME, --cases a,b). Writes nothing.
    estimate     the offline guess at what live reads cost. No API call.
    refresh      check every kept reading again under today's checks, drop the ones no case reads, write the report.
    live         read with Claude, with the user's key: --pilot (the story's four cases) or --cases a,b, and --cap D,
                 which is required. It asks first (config.confirm_spend); --yes skips the question.
Exit codes: 0 done, 1 an error the message names (a wrong command line too), 2 stopped early, 3 a reading broke I1, I2
or I3.
"""

from __future__ import annotations

import argparse
import math
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

TITLES = {"perfect": "Perfect reader", "empty": "Empty reader", "no-key": "No key", "obey": "Obeys planted lines",
          "tags": "Tags every order", "withdraw": "Says the customer withdrew", "prior": "Says a fraud report was claimed",
          "unbacked": "Raises an unbacked point", "early": "Proposes before reading", "flood": "Sends past the caps"}
NO_KEY = ("No Claude API key is set. Put ANTHROPIC_API_KEY=... in .env (docs/getting-api-keys.md, part 2). Nothing was "
          "read.")
NOTHING_TO_READ = "0 live reads needed: every case in the plan is kept already."
NO_CASE = "There's no case named {label}. Name cases as the eval labels them, such as earbuds or control-earbuds."
BAD_DOLLARS = "{flag} is a dollar amount above 0, such as 3 or 2.50."
BAD_EFFORT = "--effort is high or medium, or both: medium,high."
PILOT_AND_CASES = "--pilot reads the story's four cases. Leave out --cases."
AGAIN_OUTSIDE = "--again names {label}, which this run doesn't read. Name it in --cases too."
AGAIN_OUTSIDE_PILOT = "--again names {label}, which the pilot doesn't read. The pilot reads {story}."
SLOW = "Scoring Claude's kept readings and the scripted readers. This calls no API and takes about a minute."
RUN_NOT_RECORDED = "So this run isn't recorded there: the next line says what it spent."
WRITE_AGAIN = "Run `disputable eval` to write the report again. It costs nothing."


def main(argv: Sequence[str] | None = None, world: Any = None) -> int:
    """The eval's command line: argv as disputable eval gets it. world: runner.World(), or a test's."""
    from disputable.config import SetupError
    from disputable.evals import runner

    world = runner.World() if world is None else world
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:  # argparse printed its own line: --help (0), or a usage error, which is 1, never 2
        return 0 if stop.code in (0, None) else 1
    commands = {None: _replay, "scripted": _scripted, "estimate": _estimate, "refresh": _refresh, "live": _live}
    try:
        return commands[args.command](args, world)
    except (runner.EvalError, SetupError) as problem:
        world.say(str(problem))
        return 1
    except KeyboardInterrupt:
        world.say("Stopped by you.")
        return 2


# ----------------------------------------------------------------------------- the commands
def _replay(args: argparse.Namespace, world: Any) -> int:
    world.say(SLOW)
    columns, kept = _columns(world)
    return _write_report(columns, kept, world)


def _scripted(args: argparse.Namespace, world: Any) -> int:
    from disputable import prompts
    from disputable.evals import report, runner

    plan = _plan(args.cases)
    broke = 0
    for name in [args.reader] if args.reader else runner.SCRIPTED:
        outcomes = runner.scripted(name, plan, prompts.EFFORT, read=world.read or runner.scripted_reading)
        column = report.Column(name, TITLES[name], prompts.EFFORT, False, runner.scores(outcomes))
        for line in report.summary(column):
            world.say(line)
        broke += sum(1 for one in column.scores if one.broken)
    return 3 if broke else 0


def _estimate(args: argparse.Namespace, world: Any) -> int:
    from disputable.evals import cost, sites

    efforts = _efforts(args.effort)
    for line in cost.lines(cost.estimate(sites.sites(), efforts), efforts):
        world.say(line)
    return 0


def _refresh(args: argparse.Namespace, world: Any) -> int:
    from disputable.evals import runner, sites

    said = runner.refresh(sites.sites(), runner.EFFORTS, allowed=runner.seed_allowed(), folder=world.readings)
    for line in said or ["There are no kept readings to check again."]:
        world.say(line)
    if any(line.endswith("wasn't written again:") for line in said):
        return 1
    return _replay(args, world)


def _live(args: argparse.Namespace, world: Any) -> int:
    import anthropic

    from disputable import config
    from disputable.evals import cost, report, runner, sites
    from disputable.fees import usd

    efforts = _efforts(args.effort)
    cap, read_cap = _dollars(args.cap, "--cap"), _dollars(args.read_cap, "--read-cap")
    if args.pilot and args.cases:
        raise runner.EvalError(PILOT_AND_CASES)
    plan = _plan(",".join(sites.STORY) if args.pilot else args.cases)
    again = frozenset(_labels(args.again))
    for label in sorted(again - {site.label for site in plan}):
        if args.pilot:  # --cases is refused with --pilot, so the other line's advice would go in a circle
            raise runner.EvalError(AGAIN_OUTSIDE_PILOT.format(label=label, story=runner.listed(sites.STORY)))
        raise runner.EvalError(AGAIN_OUTSIDE.format(label=label))
    key = config.load_settings(world.env_file, need_airwallex=False, allow_synced=True).anthropic_api_key
    if not key:
        world.say(NO_KEY)
        return 1
    runs_path = world.docs / "runs.json"
    earlier = report.load_runs(runs_path)  # a runs file that won't load stops the command before any read
    kept = {effort: runner.recorded(effort, world.readings) for effort in efforts}
    new = {effort: [site for site in plan if site.label in again or sites.request_for(site, effort).key not in
                    kept[effort]] for effort in efforts}
    count = sum(len(found) for found in new.values())
    if not count:
        world.say(NOTHING_TO_READ)
        return 0
    guess = [cost.estimate(new[effort], (effort,)) for effort in efforts if new[effort]]
    low, central, high = (sum((one.total(name) for one in guess), Decimal(0)) for name in ("low", "central", "high"))
    what = (f"Live run: {count} reads ({_kind_words(args)}) at effort {', '.join(efforts)}. "
            f"{len(plan) * len(efforts) - count} are kept already and cost nothing.\n"
            f"Offline guess, not measured: {usd(central)} (between {usd(low)} and {usd(high)}). A case the API is too "
            f"busy for is tried once more after {runner.BUSY_WAIT} seconds.")
    if not config.confirm_spend(what, reads=count, cap=cap, yes=args.yes, ask=world.ask, say=world.say,
                                results=world.docs / "results.json"):
        return 1
    run = runner.live(world.client_for(key), plan, efforts, cap=cap, read_cap=read_cap, again=again,
                      allowed=runner.seed_allowed(), folder=world.readings, say=world.say, sleep=world.sleep,
                      clock=world.clock, today=world.today, max_minutes=args.max_minutes)
    runs = [*earlier, report.run_entry(
        day=world.today(), kind=_kind(args), efforts=efforts, outcomes=run.outcomes, spent=run.spent,
        requests=run.requests, stopped=run.stopped, minutes=run.minutes, sdk=anthropic.__version__)]
    text = report.runs_text(runs)
    problems = report.problems("runs.json", text, runner.seed_allowed())
    for line in problems:
        world.say(line)
    written = True  # a privacy problem leaves runs.json as it was, and its lines above say so
    if not problems:
        written = _write(world, runs_path, text, RUN_NOT_RECORDED)
    failures = runner.tally(run.outcomes)
    tried = sum(1 for one in run.outcomes if one.source == "read" and one.tried)
    listed = ", ".join(f"{number} {kind}" for kind, number in failures.items())
    world.say(f"Spent {usd(run.spent)} on {tried} reads, {run.requests} requests. "
              + (f"Failures: {listed}." if failures else "No failures."))
    columns, kept_files = _columns(world)
    prices = report.measured(columns, report.load_runs(runs_path))  # as results.json will say it
    if prices:
        world.say("Measured: " + ", ".join(f"{usd(Decimal(price))} a read at {effort}"
                                           for effort, price in prices.items()) + ".")
        whole = len(sites.sites())
        full = sum((Decimal(price) * whole for price in prices.values()), Decimal(0))
        world.say(f"A full run ({whole} reads at effort {', '.join(prices)}) would cost about {usd(full)}. A cap of "
                  f"{usd(Decimal(math.ceil(full * Decimal('1.5'))))} leaves room.")
    code = _write_report(columns, kept_files, world)
    kept_now = sum(1 for one in run.outcomes if one.kept)
    world.say(f"Kept {kept_now} readings in {_shown(world.readings)}.")
    if code or not written:
        return code or 1
    return 2 if run.stopped else 0


# ----------------------------------------------------------------------------- inside
def _parser() -> argparse.ArgumentParser:
    from disputable.evals.runner import MAX_MINUTES, SCRIPTED
    from disputable.spend import READ_CAP

    parser = argparse.ArgumentParser(prog="disputable eval", description="How well Claude reads the cases, scored by "
                                     "code. With no command, it replays the kept readings and writes the report.")
    commands = parser.add_subparsers(dest="command")
    scripted = commands.add_parser("scripted", help="score the scripted readers; writes nothing")
    scripted.add_argument("--reader", choices=SCRIPTED, help="one scripted reader (all of them if left out)")
    scripted.add_argument("--cases", default="", help="case labels, comma separated (all if left out)")
    estimate = commands.add_parser("estimate", help="the offline guess at what live reads cost")
    estimate.add_argument("--effort", default="", help="high (the app's), medium, or medium,high")
    commands.add_parser("refresh", help="check the kept readings again under today's checks, and drop unused ones")
    live = commands.add_parser("live", help="read with Claude, with your key: it costs money and asks first")
    live.add_argument("--pilot", action="store_true", help="read the story's four cases")
    live.add_argument("--cap", required=True, help="the most the run may spend, in dollars")
    live.add_argument("--effort", default="", help="high (the app's), medium, or medium,high")
    live.add_argument("--cases", default="", help="case labels, comma separated (all 97 if left out)")
    live.add_argument("--again", default="", help="kept cases to read again on purpose, comma separated")
    live.add_argument("--read-cap", default=str(READ_CAP), help="the most one read may spend, in dollars")
    live.add_argument("--max-minutes", type=int, default=MAX_MINUTES, help="stop the run after this many minutes")
    live.add_argument("--yes", action="store_true", help="skip the question (only after you said yes to this run)")
    return parser


def _columns(world: Any) -> tuple[list[Any], list[Any]]:
    """Claude's columns from the kept readings (an effort with no case read is left out) and the scripted ones."""
    from disputable import prompts
    from disputable.evals import report, runner, sites

    found = sites.sites()
    claude, kept = [], []
    for effort in runner.EFFORTS:
        file = runner.recorded_file(effort, world.readings)
        if file is None:
            continue
        outcomes = runner.replayed(effort, found, world.readings)
        days = sorted(file.dates.values())
        kept.append(report.Kept(effort, file.header.model, days[0] if days else None, days[-1] if days else None,
                                len(file.readings), runner.unused(effort, found, world.readings),
                                sum(one.rechecked for one in outcomes), sum(one.changed for one in outcomes)))
        if any(one.reading is not None for one in outcomes):
            claude.append(report.Column(f"claude-{effort}", f"Claude, {effort}", effort, True,
                                        runner.scores(outcomes)))
    read = world.read or runner.scripted_reading
    scripted = [report.Column(name, TITLES[name], prompts.EFFORT, False,
                              runner.scores(runner.scripted(name, found, prompts.EFFORT, read=read)))
                for name in runner.SCRIPTED]
    return [*claude, *scripted], kept


def _write_report(columns: Sequence[Any], kept: Sequence[Any], world: Any) -> int:
    """Writes report.md and results.json after the privacy checks pass. 3 when a reading broke I1, I2 or I3; 1 when a
    file couldn't be written."""
    from disputable.evals import report, runner

    text, results = report.render(columns, kept=kept, runs=report.load_runs(world.docs / "runs.json"))
    allowed = runner.seed_allowed()
    problems = report.problems("report.md", text, allowed) + report.problems("results.json", results, allowed)
    if problems:
        world.say("The report wasn't written:")
        for line in problems:
            world.say(line)
        return 1
    for name, body in (("report.md", text), ("results.json", results)):
        if not _write(world, world.docs / name, body, WRITE_AGAIN):
            return 1
    world.say(f"Wrote the report to {_shown(world.docs / 'report.md')} and every case to "
              f"{_shown(world.docs / 'results.json')}.")
    broken = [f"Broken: {one.label} ({column.effort if column.claude else column.title}) breaks "
              f"{', '.join(one.broken)}." for column in columns for one in column.scores if one.broken]
    for line in broken:
        world.say(line)
    return 3 if broken else 0


def _write(world: Any, path: Any, text: str, then: str) -> bool:
    """Writes one of the eval's files. False when it can't be written, as when another program holds it past the
    retries: a plain line says so, then what that means, never a traceback."""
    from disputable.evals import report, runner

    try:
        report.write(path, text)
    except OSError as problem:
        world.say(f"{runner.unwritten(path.name, problem)} {then}")
        return False
    return True


def _plan(cases: str) -> tuple[Any, ...]:
    """The sites a command reads, in run order: all of them, or the labels named (a control is named as
    control-<label>)."""
    from disputable.evals import runner, sites

    found = sites.sites()
    labels = _labels(cases)
    if not labels:
        return found
    known = {site.label for site in found}
    for label in labels:
        if label not in known:
            raise runner.EvalError(NO_CASE.format(label=label))
    return tuple(site for site in found if site.label in set(labels))


def _labels(text: str) -> list[str]:
    return [label.strip() for label in (text or "").split(",") if label.strip()]


def _efforts(text: str) -> tuple[str, ...]:
    """The efforts to read, one at a time in the live order: the app's (high) when none is named."""
    from disputable import prompts
    from disputable.evals import runner

    named = _labels(text) or [prompts.EFFORT]
    if any(effort not in runner.EFFORTS for effort in named):
        raise runner.EvalError(BAD_EFFORT)
    return tuple(effort for effort in runner.LIVE_ORDER if effort in named)


def _dollars(text: str, flag: str) -> Decimal:
    from disputable.evals import runner

    try:
        value = Decimal(str(text).strip().lstrip("$"))
    except InvalidOperation:
        raise runner.EvalError(BAD_DOLLARS.format(flag=flag)) from None
    if not value.is_finite() or value <= 0:
        raise runner.EvalError(BAD_DOLLARS.format(flag=flag))
    return value


def _kind(args: argparse.Namespace) -> str:
    if args.pilot:
        return "pilot"
    if args.again:
        return "again"
    return "cases" if args.cases else "full"


def _kind_words(args: argparse.Namespace) -> str:
    return {"pilot": "the pilot: the story's four cases", "again": "the cases you named, read again",
            "cases": "the cases you named", "full": "every case and every control"}[_kind(args)]


def _shown(path: Any) -> str:
    """A path as a message shows it: from the repo's root when it is inside it."""
    from disputable.evals.runner import ROOT

    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
