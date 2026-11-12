"""The clock the loop, the send step and the approvals read: labelled moments, journaled when recording, served in replay.

A moment that changes state is asked for by its label: now("pass") before a pass's GETs, now("send:{id}") before a send's
checks, now("post:{id}") right before its POST, now("answer:{id}") when the answer comes back, now("decide:{id}") before a
round is decided, now("approve:{id}") for an approval, and now("pass:end") when the pass ends. So poll(), blocker(),
sending(), taken(), send_failed(), decide_round() and approve() get exactly the recorded moments, in the recorded order,
when a recording is replayed. A wall-clock approval in a replay would be newer than every replayed reading, and nothing
would ever be sent. peek() is for showing only: it is never journaled and never uses up a replayed moment, so the
screens read it and nothing else. Every moment is UTC. No network here.

The loop, the story recorder and the demo use it like this:
    live: clock = WallClock()
    recording: clock = RecordingClock(); after each beat, beat.clock = clock.take()
    replay: clock = ReplayClock(start); for each beat: clock.load(beat.clock, at=beat.at), play it, then clock.done()
    a ReplayMismatch is never swallowed: the demo stops at its beat and says so
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence, runtime_checkable

ID = r"[A-Za-z0-9_]{1,64}"  # a dispute id, as airwallex.ID lets one into a web address
LABEL = re.compile(rf"(?:pass|pass:end|(?:send|post|answer|decide|approve):{ID})")
BAD_LABEL = ("{label!r} isn't a moment this app asks for. The labels are pass, pass:end, and send, post, answer, decide "
             "or approve with a dispute id, as in send:dst_example.")
OTHER_LABEL = ("The code asked for the time of {asked}, but the recording's next time is for {recorded}. The code reads "
               "the clock in another order than the live run did.")
NONE_LEFT = "The code asked for the time of {asked}, but the recording holds no more times for this step."
LEFT_OVER = ("The recording holds {count} more time{s} for this step that the code never asked for, the first for "
             "{label}.")


class ReplayMismatch(Exception):
    """The code asked for something the recording doesn't hold, or left some of it unused. The demo stops and says so.
    It isn't an httpx error, so the Airwallex client lets it out, and every broad except in the loop raises it again."""


@runtime_checkable
class Clock(Protocol):
    def now(self, label: str) -> datetime:
        """A moment that changes state: journaled when recording, served in replay."""
        ...

    def peek(self) -> datetime:
        """For showing only: never journaled, and it never uses up a replayed moment."""
        ...


def wall() -> datetime:
    return datetime.now(timezone.utc)


def check_label(label: str) -> str:
    """The label, or ValueError when it isn't one this app asks for: the same rule live, recording and in replay, so a
    typo fails in every test, not only when a recording is played."""
    if not (isinstance(label, str) and LABEL.fullmatch(label)):
        raise ValueError(BAD_LABEL.format(label=label))
    return label


class WallClock:
    """The computer's clock, as UTC. source is for tests: a function that gives an aware datetime."""

    def __init__(self, source: Callable[[], datetime] = wall) -> None:
        self._source = source

    def now(self, label: str) -> datetime:
        check_label(label)
        return _utc(self._source())

    def peek(self) -> datetime:
        return _utc(self._source())


class RecordingClock(WallClock):
    """The wall clock, with every now() kept in order as (label, moment) in journal. take() returns the journal so far
    and clears it, once per beat. peek() is never kept."""

    def __init__(self, source: Callable[[], datetime] = wall) -> None:
        super().__init__(source)
        self.journal: list[tuple[str, datetime]] = []

    def now(self, label: str) -> datetime:
        moment = super().now(label)
        self.journal.append((label, moment))
        return moment

    def take(self) -> tuple[tuple[str, datetime], ...]:
        entries = tuple(self.journal)
        self.journal.clear()
        return entries


class ReplayClock:
    """A recording's moments, served in order. now() must ask for the label the recording holds next, else
    ReplayMismatch. peek() is the last moment served, or the step's own time before any, or start before anything."""

    def __init__(self, start: datetime) -> None:
        self._shown = _utc(start)
        self._entries: deque[tuple[str, datetime]] = deque()

    def load(self, entries: Sequence[tuple[str, datetime]], *, at: datetime | None = None) -> None:
        """The journal of one step, in order. at: the step's own recorded time, which peek() shows until a moment is
        served. A step whose times weren't all used must call done() first: loading over them raises ReplayMismatch."""
        self._left_over()
        loaded = []
        for label, moment in entries:
            loaded.append((check_label(label), _utc(moment)))
        self._entries = deque(loaded)
        if at is not None:
            self._shown = _utc(at)

    def now(self, label: str) -> datetime:
        check_label(label)
        if not self._entries:
            raise ReplayMismatch(NONE_LEFT.format(asked=label))
        recorded, moment = self._entries[0]
        if recorded != label:
            raise ReplayMismatch(OTHER_LABEL.format(asked=label, recorded=recorded))
        self._entries.popleft()
        self._shown = moment
        return moment

    def peek(self) -> datetime:
        return self._shown

    def done(self) -> None:
        """The step is over: a time it didn't ask for raises ReplayMismatch, since the code then reads the clock less
        often than the live run did."""
        self._left_over()

    def left(self) -> int:
        """How many of the step's times are still to be served."""
        return len(self._entries)

    def _left_over(self) -> None:
        if self._entries:
            count = len(self._entries)
            raise ReplayMismatch(LEFT_OVER.format(count=count, s="" if count == 1 else "s", label=self._entries[0][0]))


def _utc(moment: datetime) -> datetime:
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("A moment needs a time zone.")
    return moment.astimezone(timezone.utc)
