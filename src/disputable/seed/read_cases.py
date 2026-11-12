"""The cases whose records get read: the story's three, Ending 2 and the suite's 79, each with its store, its clock and
its answer key. The eval reads them (disputable.evals) and the tests walk them (tests/cases.py re-exports them), so both
share one list. Which ones read records is decide()'s first call, never a list kept by hand."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from disputable.engine import Action, Dispute, decide
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
