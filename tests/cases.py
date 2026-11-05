"""The cases whose records get read, shared by the rules, scripted and walk tests: each one's store, clock and answer
key, code alone on it, and one scripted reading of it per reader. Every value here is made once in a run and then
shared, so the files that walk all 83 cases don't make the same reading twice."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime

from disputable.engine import Action, Dispute, decide
from disputable.rules import evaluate
from disputable.seed.answers import Answer
from disputable.seed.fernhill import ANSWERS, ENDING_2, ENDING_2_ANSWER, FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.seed.suite import ANSWERS as SUITE_ANSWERS, SUITE
from disputable.store import Case, Store

STORES = {"fernhill": FERNHILL, "ending_2": ENDING_2, "suite": SUITE}


@dataclass(frozen=True)
class Read:
    """One case whose records get read, with its store, its clock and its answer key."""
    store_name: str
    case_id: str
    now: datetime
    answer: Answer

    @property
    def store(self) -> Store:
        return STORES[self.store_name]

    @property
    def case(self) -> Case:
        return self.store.case(self.case_id)

    @property
    def dispute(self) -> Dispute:
        return self.store.dispute(self.case_id, self.now)


def read_cases() -> dict[str, Read]:
    """By label: the story's cases (round two on its own clock), Ending 2 and the suite's. Which ones read records is
    decide()'s first call, never a list kept by hand."""
    story = [(case.id, Read("fernhill", case.id, THREE_WEEKS_LATER if case.id == "headphones_round_2" else MONDAY,
                            ANSWERS[case.id])) for case in FERNHILL.cases]
    ending_2 = [("ending_2", Read("ending_2", "headphones_round_2", THREE_WEEKS_LATER, ENDING_2_ANSWER))]
    suite = [(case.id, Read("suite", case.id, MONDAY, SUITE_ANSWERS[case.id])) for case in SUITE.cases]
    return {label: read for label, read in (*story, *ending_2, *suite)
            if decide(read.dispute, None, read.now).action is Action.GATHER_EVIDENCE}


READ = read_cases()


@functools.cache
def code_alone(label: str):
    """evaluate() with nothing reported, and decide() on its evidence. Cached: tests that patch a module call evaluate()."""
    read = READ[label]
    conclusion = evaluate(read.store, read.case, [])
    return conclusion, decide(read.dispute, conclusion.evidence, read.now)


# The readers need the SDK, which takes seconds to import here. The rules tests never read, so they never load it.
@functools.cache
def request(label: str):
    """The read request for a case, keyed as the default reader keys it."""
    from disputable.reader import ReadRequest, reading_key
    read = READ[label]
    return ReadRequest(read.store, read.case, read.dispute, reading_key(read.store, read.case))


def read_with(label: str, reader: str = "perfect"):
    """One reading of a case and the client it ran on: by the perfect reader, the empty one, or an attack of
    seed.scripted.ATTACKS. The real ClaudeReader reads it, on the scripted FakeClient. Made once a run, however it's
    asked for: read_with(label) and read_with(label, "perfect") are the same reading."""
    return _read_with(label, reader)


@functools.cache
def _read_with(label: str, reader: str):
    from disputable.reader import ClaudeReader
    from disputable.seed.scripted import FakeClient, empty, fooled, perfect
    read = READ[label]
    if reader == "perfect":
        script = perfect(read.store, read.case, read.answer)
    elif reader == "empty":
        script = empty(read.store, read.case)
    else:
        script = fooled(read.store, read.case, read.answer, reader)
    client = FakeClient(script)
    return ClaudeReader(client).read(request(label)), client
