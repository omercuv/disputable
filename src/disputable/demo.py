"""The demo: a recorded story run, replayed as the screens' Desk, with no keys and no network.

Demo holds one recording.Replay of the chosen ending: a fresh temp database, a ReplayTransport, a ReplayClock, a
StrictReplayReader and a Service in Mode.DEMO whose approver is the recording's. It plays the recording's beats one at a
time, each as the live run played it:
- Next (demo_next) plays the lowest beat not played yet: the bank's rows, a pass at its recorded moments, or an approval
  as the recording's approver at its recorded moment. The page posts how many beats it showed, so a second click, or an
  old page, is told the demo moved on and plays nothing.
- An approval from the approval page runs the Service's checks at the clock's peek() first. They save nothing and use no
  recorded moment, so a refusal leaves the story as it was. Then it plays the approval beat of the pending window (the
  unplayed approve beats that start at the lowest unplayed beat) whose dispute and plan the page posted, in any order
  inside the window. Any other approval isn't in the recording.
- A restart (demo_restart) builds all of it again from the chosen ending's recording.
Every recording is loaded at the start, so a restart never fails half way through a demo. The chosen ending's must load;
another one that can't (stale, half copied, or of another ending) is left out, so the bar doesn't offer it, and warnings
names its folder and why.
One lock around every Desk call and play_all(): FastAPI calls them from its thread pool, so a second click waits for the
first, then finds the demo moved on. A ReplayMismatch stops the demo: the bar shows it, Next is off, and nothing hides
it. After the last beat the bar shows the recording's own total, which the money page shows beside the replayed one, and
how many recorded calls the replay never made. The bar also says what answered for Airwallex in the recording, from its
manifest: the sandbox, or the tests' stand-in for it, so every page can say which one it replays. There is no booking
and no drop in the demo.

This module never imports the seed: the CLI passes the store a recording names.

The CLI uses it like this:
    demo = Demo({"1": <fixtures/story/ending-1>, "2": ...}, store_for=<the seed's store by name>, ending="1")
    print each of demo.warnings; web.serve(demo, port=8765), or demo.play_all(say=print) for --no-serve; demo.close()
"""

from __future__ import annotations

import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from disputable import approvals, desk, recording
from disputable.approvals import Alert
from disputable.clock import ReplayMismatch
from disputable.desk import Board, DemoBar, Result
from disputable.fees import usd
from disputable.loop import PassReport, who
from disputable.recording import Played, Recording, RecordingError, Replay
from disputable.store import Store

TEMP_PREFIX = "disputable-demo-"  # each start's database lives in a fresh folder of the system's temp folder

# Why a demo can't start, or can't offer another ending.
NO_RECORDINGS = "There's no recording to replay."
NOT_RECORDED = "There's no recording of Ending {name}."
OTHER_ENDING = "The recording in {folder} is of Ending {found}, not Ending {name}."
LEFT_OUT = "Ending {name} can't be offered. {why}"

# The bar's line for a ReplayMismatch with no words of its own, so it still says why Next is off.
STOPPED = "The replay stopped matching the recording."

# What play_all() says: a line for each beat, under a pass its counts, problems and alerts, then the totals.
STEP = "Step {n} of {count}, {story}: {text}"
PASSED = "  Read {read}, sent {sent}, decided {decided}."
PROBLEM = "  Problem: {line}"
ALERT = "  Alert: {line}"
ENDED = "The story ends. The replay cost {total}."
RECORDED_TOTAL = "The recording's own total for this ending: {total}."
STILL = "Still at risk: {at_risk}. Disputes closed: {closed}."
LEFT_UNUSED = "The recording holds {count} call{s} this replay never made."


class Demo:
    """desk.Desk over a recorded story run: one Replay of the chosen ending at a time, behind one lock."""

    def __init__(self, recordings: Mapping[str, Path], *, store_for: Callable[[str], Store], ending: str = "1") -> None:
        """recordings: each ending's recording folder, by ending ("1", "2"). store_for: the seed's store a manifest
        names. Every recording is loaded now. When the chosen ending's can't be replayed, the start stops with
        RecordingError, a SetupError whose message the CLI prints. Another ending's that can't is left out: warnings
        says which and why, and the bar doesn't offer it."""
        if not recordings:
            raise RecordingError(NO_RECORDINGS)
        if ending not in recordings:
            raise RecordingError(NOT_RECORDED.format(name=ending))
        self._lock = threading.Lock()
        self._store_for = store_for
        self._recordings: dict[str, Recording] = {}
        warnings = []
        for name, folder in sorted(recordings.items()):
            try:
                self._recordings[name] = _loaded(name, Path(folder))
            except RecordingError as refused:
                if name == ending:
                    raise
                warnings.append(LEFT_OUT.format(name=name, why=refused))
        self.warnings: tuple[str, ...] = tuple(warnings)  # the endings left out, for the CLI to say
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._start(ending)

    @property
    def ending(self) -> str:
        return self._ending

    @property
    def mismatch(self) -> str | None:
        """Why the replay stopped matching the recording, or None."""
        return self._mismatch

    # ------------------------------------------------------------------------- the Desk
    def board(self) -> Board:
        """The replay's Board, with the demo bar."""
        with self._lock:
            board = self.replay.service.board()
            return replace(board, demo=self._bar(board))

    def approve(self, dispute_id: str, plan_text: str, reading_key: str) -> Result:
        """The approval page's POST. The Service's checks run at peek() first: a refusal saves nothing and uses no
        recorded moment. Then the window's approval beat of this dispute and the posted plan plays at its recorded
        moment, as the recording's approver."""
        with self._lock:
            if self._mismatch is not None:
                return Result(False, "demo-mismatch", self._mismatch)
            replay = self.replay
            checked = replay.service.check_approval(dispute_id, plan_text, reading_key, replay.clock.peek())
            if not checked.result.ok:
                return checked.result
            for beat in replay.window():
                if beat.dispute_id == dispute_id and beat.plan_sha256 == checked.plan_sha256:
                    try:
                        played = self._play(beat, plan_text=plan_text, reading_key=reading_key)
                    except ReplayMismatch:
                        return Result(False, "demo-mismatch", self._mismatch)
                    return played.result  # approved: any other answer is a ReplayMismatch
            return Result(False, "not-in-recording")

    def drop_moved(self, dispute_id: str, version: int) -> Result:
        return Result(False, "live-only")

    def demo_next(self, played: int) -> Result:
        """Plays the lowest beat not played yet, when played is how many beats were played: the page's count."""
        with self._lock:
            replay = self.replay
            if type(played) is not int or played != len(replay.played):
                return Result(False, "demo-moved")
            if self._mismatch is not None:
                return Result(False, "demo-mismatch", self._mismatch)
            coming = replay.unplayed()
            if not coming:
                return Result(False, "demo-finished")
            try:
                self._play(coming[0])
            except ReplayMismatch:
                return Result(False, "demo-mismatch", self._mismatch)
            return Result(True, "next-played")

    def demo_restart(self, ending: str) -> Result:
        """The story again from its start: a fresh database, transport, clock, reader and Service."""
        with self._lock:
            if ending not in self._recordings:
                return Result(False, "no-ending")
            self._start(ending)
            return Result(True, "restarted")

    # ------------------------------------------------------------------------- for the CLI and the tests
    def play_all(self, say: Callable[[str], object] | None = None) -> list[str]:
        """--no-serve: every beat not played yet, in order, with a line each, then the totals. Under a pass: its counts,
        its problems, and the alerts as they stand once it ended, as the screens show them then. Each line also goes to
        say as it is made. A ReplayMismatch sets the bar's mismatch and comes out."""
        lines: list[str] = []

        def tell(line: str) -> None:
            lines.append(line)
            if say is not None:
                say(line)

        with self._lock:
            if self._mismatch is not None:
                raise ReplayMismatch(self._mismatch)
            replay = self.replay
            for beat in replay.unplayed():
                tell(STEP.format(n=beat.n, count=len(replay.beats), story=beat.story, text=beat.text))
                played = self._play(beat)
                if played.report is not None:
                    for line in _report(played.report, self._alerts(played.report)):
                        tell(line)
            for line in self._totals():
                tell(line)
        return lines

    def close(self) -> None:
        """Removes the replay's temp folder. Nothing may be asked of the demo after."""
        with self._lock:
            temp, self._temp = self._temp, None
        if temp is not None:
            temp.cleanup()

    # ------------------------------------------------------------------------- inside, under the lock
    def _start(self, ending: str) -> None:
        recorded = self._recordings[ending]
        temp = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, ignore_cleanup_errors=True)
        try:
            replay = Replay(recorded, store=self._store_for(recorded.manifest.store), folder=Path(temp.name))
        except BaseException:
            temp.cleanup()
            raise
        old, self._temp = self._temp, temp
        self.replay: Replay = replay  # read it only: every beat plays through the Desk, behind the lock
        self._ending, self._mismatch = ending, None
        if old is not None:
            old.cleanup()

    def _play(self, beat: recording.Beat, *, plan_text: str | None = None, reading_key: str | None = None) -> Played:
        """One beat. A ReplayMismatch sets the mismatch, which stops the demo, and comes out as it is."""
        try:
            return self.replay.play(beat, plan_text=plan_text, reading_key=reading_key)
        except ReplayMismatch as mismatch:
            self._mismatch = str(mismatch) or STOPPED
            raise

    def _bar(self, board: Board) -> DemoBar:
        replay = self.replay
        manifest = replay.recording.manifest
        beats = {beat.n: beat for beat in replay.beats}
        coming = replay.unplayed()
        finished = not coming
        pending = () if self._mismatch is not None else tuple(
            (beat.dispute_id, _name(board, beat.dispute_id)) for beat in replay.window())
        return DemoBar(ending=self._ending, endings=tuple(self._recordings), recorded_on=manifest.recorded_at.date(),
                       played=tuple(_shown(beats[n]) for n in replay.played),
                       coming=_shown(coming[0]) if coming else None, pending=pending, approver=manifest.approver,
                       mismatch=self._mismatch, finished=finished, count=len(replay.beats),
                       airwallex=manifest.airwallex, total=manifest.total if finished else None,
                       left_unused=len(replay.transport.left()) if finished else 0)

    def _alerts(self, report: PassReport) -> tuple[Alert, ...]:
        """The alerts once the pass ended: over the replay's cases right after it, at its last moment (pass:end, which
        the clock's peek() shows then). The report keeps the alerts from the pass's start, before it sent or decided."""
        service = self.replay.service
        return approvals.alerts(service.db.load().records.values(), report.finished_at, service.fees)

    def _totals(self) -> list[str]:
        replay = self.replay
        total, at_risk, closed = replay.totals()
        lines = [ENDED.format(total=usd(total)), RECORDED_TOTAL.format(total=usd(replay.recording.manifest.total)),
                 STILL.format(at_risk=usd(at_risk), closed=closed)]
        left = len(replay.transport.left())
        if left:
            lines.append(LEFT_UNUSED.format(count=left, s="" if left == 1 else "s"))
        return lines


def _loaded(name: str, folder: Path) -> Recording:
    """The recording in folder, which must be of Ending name. RecordingError names the folder."""
    loaded = recording.load(folder)
    if loaded.manifest.ending != name:
        raise RecordingError(OTHER_ENDING.format(folder=folder, found=loaded.manifest.ending, name=name))
    return loaded


def _shown(beat: recording.Beat) -> desk.Beat:
    """A beat as the bar shows it: its story time, its sentence and its kind."""
    return desk.Beat(beat.story, beat.text, beat.kind)


def _name(board: Board, dispute_id: str) -> str:
    record = board.records.get(dispute_id)
    return who(record) if record is not None else f"Dispute {dispute_id}"


def _report(report: PassReport, alerts: tuple[Alert, ...]) -> list[str]:
    """A pass's lines, as play_all() prints them under its beat, with the alerts as they stand once it ended."""
    return [PASSED.format(read=len(report.read), sent=len(report.sent), decided=len(report.decided)),
            *(PROBLEM.format(line=line) for line in report.problems),
            *(ALERT.format(line=alert.message) for alert in alerts)]
