"""The answer key for the made-up cases: what code should decide and what Claude should find.

Kept apart from the records on purpose. Nothing in disputable.store can reach it, so Claude's tools can't either.
"""

from __future__ import annotations

from dataclasses import dataclass

from disputable.engine import Action, Evidence
from disputable.fees import EvidenceClass


@dataclass(frozen=True)
class Fact:
    """Something Claude should report, and the exact words in the record that prove it."""
    record_id: str
    quote: str


@dataclass(frozen=True)
class Answer:
    first_call: Action  # decide() with no evidence yet
    evidence_class: EvidenceClass | None = None  # what code should conclude once the records are read
    fact_ids: frozenset[str] = frozenset()
    file_hashes: frozenset[str] = frozenset()
    conflicts: tuple[str, ...] = ()
    final: Action | None = None  # decide() with that evidence. None: the first call already ended it
    recommended: Action | None = None
    # The priors against CE 3.0's criteria, whatever the stage: "pass", "fail", or "unclear" when the sources disagree or
    # don't say. None: the check isn't consulted, because this isn't a 10.4 claim that gets read, or it's a later round
    # whose rejection doesn't raise CE 3.0.
    ce3: str | None = None
    reported_orders: frozenset[str] = frozenset()  # orders the issuer's comment says carry a fraud report
    must_find: tuple[Fact, ...] = ()
    must_ignore: tuple[str, ...] = ()  # the whole planted text, word for word: it must change nothing

    def evidence(self) -> Evidence | None:
        if self.evidence_class is None:
            return None
        return Evidence(self.evidence_class, self.fact_ids, self.file_hashes, self.conflicts)
