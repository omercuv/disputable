"""The clock: labelled moments, journaled when recording and served in replay; peek() for showing only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from disputable.clock import (LABEL, Clock, RecordingClock, ReplayClock, ReplayMismatch, WallClock, check_label)

START = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
STORE_TIME = timezone(timedelta(hours=-8))
LABELS = ("pass", "send:dst_ear_tips", "post:dst_ear_tips", "answer:dst_ear_tips", "decide:dst_headphones",
          "approve:dst_headphones", "pass:end")  # every label the loop, the send step and the approvals ask for


class Ticks:
    """A source that moves a second on each read, starting at START."""

    def __init__(self, start=START):
        self.moment, self.reads = start, 0

    def __call__(self):
        self.reads += 1
        self.moment += timedelta(seconds=1)
        return self.moment


def test_every_label_the_app_asks_for_and_nothing_else():
    for label in LABELS:
        assert check_label(label) == label and LABEL.fullmatch(label)
    for bad in ("", "pass:start", "send:", "send", "approve-dst_x", "approve:dst x", "book:dst_x", "post:dst/x",
                "decide:" + "x" * 65, "PASS", None, 3):
        with pytest.raises(ValueError, match="isn't a moment this app asks for"):
            check_label(bad)


@pytest.mark.parametrize("clock", [WallClock(), RecordingClock(), ReplayClock(START)])
def test_every_clock_is_a_clock_and_refuses_a_label_it_doesnt_know(clock):
    assert isinstance(clock, Clock)
    with pytest.raises(ValueError):
        clock.now("approve-dst_x")


def test_the_wall_clock_is_utc():
    before = datetime.now(timezone.utc)
    moment, shown = WallClock().now("pass"), WallClock().peek()
    assert before <= moment <= shown <= datetime.now(timezone.utc)
    assert moment.utcoffset() == timedelta(0) and shown.tzinfo is timezone.utc
    local = WallClock(lambda: datetime(2026, 11, 2, 9, 0, tzinfo=STORE_TIME))
    assert local.now("pass") == START and local.now("pass").tzinfo is timezone.utc
    with pytest.raises(ValueError, match="time zone"):
        WallClock(lambda: datetime(2026, 11, 2, 9, 0)).now("pass")


def test_recording_keeps_every_now_in_order_and_never_a_peek():
    source = Ticks()
    clock = RecordingClock(source)
    clock.peek()
    moments = [clock.now(label) for label in LABELS]
    clock.peek()
    assert clock.journal == list(zip(LABELS, moments))
    assert moments == sorted(moments) and len(set(moments)) == len(moments)
    assert clock.take() == tuple(zip(LABELS, moments))
    assert clock.journal == [] and clock.take() == ()  # take() clears it, once per beat
    assert source.reads == len(LABELS) + 2


def test_replay_serves_the_recorded_moments_in_order_and_peek_uses_none_up():
    recording = RecordingClock(Ticks())
    first = [(label, recording.now(label)) for label in LABELS[:3]]
    second = [(label, recording.now(label)) for label in LABELS[3:]]
    clock = ReplayClock(START)
    assert clock.peek() == START  # before anything is loaded
    beat_at = first[0][1] - timedelta(seconds=30)
    clock.load(first, at=beat_at)
    assert clock.peek() == clock.peek() == beat_at and clock.left() == 3  # the step's own time before any
    for label, moment in first:
        assert clock.now(label) == moment
        assert clock.peek() == moment  # the last moment served
    clock.done()
    clock.load(second)  # no at: peek() stays on the last moment served
    assert clock.peek() == first[-1][1]
    assert [clock.now(label) for label, _ in second] == [moment for _, moment in second]
    clock.done()


def test_replay_is_loud_about_every_mismatch():
    moments = [(label, START + timedelta(minutes=number)) for number, label in enumerate(LABELS[:2])]
    clock = ReplayClock(START)
    clock.load(moments)
    with pytest.raises(ReplayMismatch, match="asked for the time of decide:dst_x, but the recording's next time is for "
                                             "pass"):
        clock.now("decide:dst_x")
    assert clock.left() == 2  # nothing was used up by the mismatch
    clock.now("pass")
    with pytest.raises(ReplayMismatch, match="1 more time for this step that the code never asked for, the first for "
                                             "send:dst_ear_tips"):
        clock.done()
    with pytest.raises(ReplayMismatch, match="never asked for"):
        clock.load(moments)  # a step's leftovers are never dropped quietly
    clock.now("send:dst_ear_tips")
    with pytest.raises(ReplayMismatch, match="holds no more times for this step"):
        clock.now("pass:end")
    clock.done()


def test_replay_times_are_utc_and_must_have_a_zone():
    clock = ReplayClock(datetime(2026, 11, 2, 9, 0, tzinfo=STORE_TIME))
    assert clock.peek() == START and clock.peek().tzinfo is timezone.utc
    clock.load([("pass", datetime(2026, 11, 2, 9, 5, tzinfo=STORE_TIME))])
    assert clock.now("pass") == START + timedelta(minutes=5)
    with pytest.raises(ValueError, match="time zone"):
        clock.load([("pass", datetime(2026, 11, 2, 9, 5))])
    with pytest.raises(ValueError, match="isn't a moment"):
        clock.load([("book:dst_x", START)])
    with pytest.raises(ValueError, match="time zone"):
        ReplayClock(datetime(2026, 11, 2, 9, 0))


def test_a_mismatch_is_no_httpx_error():
    import httpx

    assert not issubclass(ReplayMismatch, httpx.HTTPError) and issubclass(ReplayMismatch, Exception)
