"""The committed story recordings, fixtures/story/ending-*: what `make demo` replays.

Each replays to its manifest's own total with no keys and no network, only the ear tips alert at the first pass, every
response the app sent names it as the one responding, the disputes were due 2, 4 and 6 days after the bank opened them,
the readings load with today's formats and the manifest's own source, a manifest that says Claude read the cases holds
no failed read, the manifest says what answered for Airwallex (the sandbox, or the tests' stand-in for it, which
scripts/record_standin.py records on) and the run's ids agree, no recorded agent call is left over, and nothing private
is in any of the four files (the fields the scrubber redacts checked by name too). Skipped until the first recording is
committed; from then on these always run, so a change to the rules, prompts, checks, lifecycle or request bodies that
breaks the demo fails here, and so does a Reading field change without a new reader.FORMAT. test_recording runs the same
checks on recordings made on the fake sandbox, of both kinds.
"""

from __future__ import annotations

import functools
from pathlib import Path
from unittest import mock

import httpx
import pytest

import recordings
from disputable import recording

COMMITTED = sorted(path for path in recording.FOLDER.glob("ending-*") if path.is_dir())
if not COMMITTED:
    pytest.skip("No story recording is committed yet: disputable story --ending 1 makes one.", allow_module_level=True)


def _no_network(self, request):
    raise AssertionError("The replay went to the network.")


@functools.cache
def replayed(folder: Path):
    """The committed recording replayed to its end once a session, with httpx.HTTPTransport switched off (each answer
    comes from the recording)."""
    with mock.patch.object(httpx.HTTPTransport, "handle_request", _no_network):
        replay, played = recordings.replay_to_the_end(folder)
    return replay, tuple(played)


def by_name(path: Path) -> str:
    return path.name


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_each_replays_to_its_own_total_with_no_keys(folder):
    recordings.assert_it_replays_to_its_own_total(*replayed(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_only_the_ear_tips_alert_at_the_first_pass(folder):
    recordings.assert_only_the_ear_tips_alert_on_monday(replayed(folder)[1])


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_the_actor_is_disputable(folder):
    recordings.assert_the_app_responds_as_itself(recording.load(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_the_disputes_were_due_in_2_4_and_6_days(folder):
    recordings.assert_the_storys_deadlines(recording.load(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_the_readings_load_with_todays_formats(folder):
    recordings.assert_todays_formats(folder)


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_it_is_a_story_the_demo_plays(folder):
    recordings.assert_a_story_the_demo_plays(recording.load(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_claudes_readings_are_claudes_own(folder):
    recordings.assert_the_readings_are_claudes_own(recording.load(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_it_says_what_answered_for_airwallex(folder):
    recordings.assert_the_kind_is_what_answered(recording.load(folder))


@pytest.mark.parametrize("folder", COMMITTED, ids=by_name)
def test_no_private_data(folder):
    recordings.assert_nothing_private(folder)
    recordings.assert_nothing_private(folder, recordings.fake_secrets())  # a stand-in's: the fake's keys and account id


def test_both_endings_are_recorded():
    assert [folder.name for folder in COMMITTED if folder.name in ("ending-1", "ending-2")] == ["ending-1", "ending-2"]
