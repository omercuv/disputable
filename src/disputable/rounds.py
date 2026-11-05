"""The round orchestrator: one round at DECIDE, from the records and Claude's reading to the plan keep() makes.

Code's rules (rules.py) conclude the evidence: the class, the fact ids, the files and code's own lines. Claude's reading
adds only fixed lines that send the round to a person, and the tags ce3 checks against the bank's own comment. So the
merge can make a round stricter, never laxer: whatever goes out with no person is exactly what code alone would send. A
read that fails hands the round to a person, with code's draft.

A reading is saved by dispute and round, and reused while its key holds (reader.reading_key): the same records, prompt,
model and limits. So a round decided again after a dropped plan gets the same reasons with no API call, and a recording
replays across sandbox runs. A failed reading is logged and never reused. Right after keep(), the round's Basis is saved:
what the plan was made on, for the screens once the plan is out, and for files_for(), which builds the plan's files again
from today's records before the upload and refuses when they moved. No clock and no network of its own: only the reader
it is given may call Claude.

The loop, the HTTP step and the screens use it like this:
    at DECIDE: step = decide_round(record, store, reader, saved, now); save step.record; show step.note in the queue
    before the upload: files = files_for(record, store, saved). On FilesMoved: lifecycle.drop(record, "its file
        couldn't be built again exactly as planned"), and the round is decided and approved again
    the case page and the approval screen: step = current(record, store, saved, now, profile=..., effort=...), with the
        live reader's profile and effort
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from disputable import approvals, documents, lifecycle, prompts, rules
from disputable.documents import Document
from disputable.engine import Action, Dispute, Evidence, decide
from disputable.fees import FEES_V1, EvidenceClass, FeeTable
from disputable.jsonable import Unloadable, dump, load
from disputable.lifecycle import CaseRecord, Phase
from disputable.reader import (CHECKS_VERSION, WHY, Failure, Reader, ReaderBusy, Reading, ReadRequest, failed,
                               reading_key, revalidate)
from disputable.reader import from_json as reading_from_json
from disputable.reader import to_json as reading_to_json
from disputable.rules import Conclusion
from disputable.store import Case, Store

FORMAT = 1
HELD_FOR_ERROR = "Code hit an error on this case's records. A person handles this round."
BUSY_NOTE = "Claude is busy. Code reads this case again on the next pass."
MAX_BUSY = 3  # busy passes in one round before it goes on as a failed read: a person, with code's draft

# The lines a reading adds after code's own. Each is a fixed sentence, so nothing Claude or a record wrote is in a reason.
G1 = ("Claude says the bank's comment reports an earlier order, and that alone changes the CE 3.0 check here. A person "
      "checks the comment before this accept goes out.")
E1 = "Claude couldn't read this case's records ({why}), so a person reads them before anything is sent."
IN = "Claude didn't read all of this case's records, so a person reads them."
K1 = rules.LABEL + "the customer's email says an earlier order was reported as fraud, and the store can't check that."
K2 = rules.LABEL + "the customer's email takes the dispute back, but the bank still shows it open."
K5 = rules.LABEL + "a record a person should read before anything is sent."
U1 = "Claude raised a point it couldn't back with an exact quote, so a person reads the records."
T1 = "Claude named an order as reported that isn't in this case's history, so a person reads the bank's comment."

# Why files_for() refuses. The HTTP step drops the plan for any of them, so it's made and approved again.
NO_BASIS = "This plan has no saved basis, so its file can't be built again."
OTHER_BASIS = "The saved basis belongs to another plan, so this plan's file can't be built again."
MOVED = "The records behind this plan's file changed after the plan was made."
REBUILD_ERROR = "Code hit an error while it built this plan's file again."
_FAILURES = frozenset(failure.value for failure in Failure)


class FilesMoved(Exception):
    """The plan's files can't be built again exactly as they were approved, so nothing may be uploaded for it."""


@dataclass(frozen=True)
class ReaderInput:
    """Everything code takes from a reading. No text Claude wrote: tags the reader kept, store ids and flags."""
    tags: tuple[str, ...]  # the kept tags: earlier orders of this case's history, on a fraud claim. () after a failure
    prior_claim: bool  # a checked K1
    withdrew: frozenset[str]  # the customer's own emails with a checked withdrawal (K2)
    other: bool  # a checked K5
    unbacked: bool  # a point whose quote failed (U1)
    stray_tags: bool  # a tag that isn't an order of this case's history (T1)
    complete: bool
    failure: Failure | None


@dataclass(frozen=True)
class Step:
    """What one call gives the loop and the screens for a round."""
    record: CaseRecord
    reading: Reading | None  # the reading the round used. None: decide()'s first call settled it, or nothing was read
    conclusion: Conclusion | None  # code's own conclusion from this call, or None when no rule ran
    basis: Basis | None  # the Basis saved for the round, or None
    note: str | None  # why the round is still at DECIDE (BUSY_NOTE), for the queue


@dataclass(frozen=True)
class Basis:
    """What a round's plan was made on, saved right after keep() whenever the round was read. Once the plan is out, the
    screens show this, since today's records would give another answer, and files_for() builds the files again from it."""
    format: int
    dispute_id: str
    round_number: int
    plan_sha256: str  # approvals.plan_sha256() of the kept plan, or "" when the round went to a person with no plan
    reading_key: str  # the key of the reading the plan was made on
    failure: str  # "" or that reading's Failure value
    tags: tuple[str, ...]  # the reading's kept tags: files_for() builds the plan's files again with them
    findings: tuple[str, ...]  # code's findings, ce3's reasons among them
    ce3_reasons: tuple[str, ...]  # the CE 3.0 check's reasons when it ran, else ()
    files: tuple[Document, ...]  # the documents behind the plan's file hashes, or () when the plan sends no file


class Saved(Protocol):
    """Where readings and bases are kept, append-only. Nothing is ever deleted or changed."""

    def put_reading(self, dispute_id: str, round_number: int, reading: Reading) -> None: ...

    def reading(self, dispute_id: str, round_number: int, key: str) -> Reading | None:
        """The latest reading put for the round with this key and no failure: a failed one is never reused."""
        ...

    def busy(self, dispute_id: str, round_number: int) -> int:
        """How many BUSY readings were put for the round."""
        ...

    def put_basis(self, basis: Basis) -> None: ...

    def basis(self, dispute_id: str, round_number: int) -> Basis | None:
        """The latest basis put for the round."""
        ...


class MemorySaved:
    """Saved state in memory, for tests and dry runs. Every put goes through JSON and back, as the SQLite store's will,
    so a value that saves but won't load fails at put, not at the next DECIDE."""

    def __init__(self) -> None:
        self.readings: tuple[tuple[str, int, Reading], ...] = ()  # every put, oldest first, as loaded back
        self.bases: tuple[Basis, ...] = ()

    def put_reading(self, dispute_id: str, round_number: int, reading: Reading) -> None:
        self.readings = (*self.readings, (dispute_id, round_number, reading_from_json(reading_to_json(reading))))

    def reading(self, dispute_id: str, round_number: int, key: str) -> Reading | None:
        found = [reading for where, number, reading in self.readings
                 if (where, number) == (dispute_id, round_number) and reading.key == key and reading.failure is None]
        return found[-1] if found else None

    def busy(self, dispute_id: str, round_number: int) -> int:
        return sum(reading.failure is Failure.BUSY for where, number, reading in self.readings
                   if (where, number) == (dispute_id, round_number))

    def put_basis(self, basis: Basis) -> None:
        self.bases = (*self.bases, basis_from_json(basis_to_json(basis)))

    def basis(self, dispute_id: str, round_number: int) -> Basis | None:
        found = [basis for basis in self.bases if (basis.dispute_id, basis.round_number) == (dispute_id, round_number)]
        return found[-1] if found else None


# ----------------------------------------------------------------------------- the merge
def reader_input(reading: Reading) -> ReaderInput:
    """What code takes from a reading: the flags the reader worked out, never worked out again from its rows. After a
    failure nothing counts, not even tags: a person reads the records instead."""
    if reading.failure is not None:
        return ReaderInput((), False, frozenset(), False, False, False, False, reading.failure)
    return ReaderInput(tuple(reading.reported), reading.prior_claim, frozenset(reading.withdrew), reading.other,
                       reading.unbacked, reading.stray_tags, reading.complete, None)


def evidence_for(store: Store, case: Case, dispute: Dispute, reading: Reading, now: datetime,
                 fees: FeeTable = FEES_V1) -> tuple[Evidence, Conclusion]:
    """The round's evidence, and code's own conclusion beside it. Pure, so the eval calls it too.

    Code's conclusion comes first, with the kept tags. A tag only takes away an order the bank's comment names, so it
    can end CE 3.0 or leave fewer orders in its file. It never lets code build a file that code alone can't (a person
    writes it then, nothing drafted), so every fight is one code alone drafts. A checked withdrawal turns code's NONE
    into UNPRICED_PROOF on the customer's emails: no table prices that class, so nothing is drafted, since accepting a
    withdrawn dispute loses the money. Then each line once, in a fixed order: code's (ce3's first), G1, E1 or IN, K1, K2,
    K5, U1, T1. Code's lines come first, so a reader's line leads only when code has none, and every line is code's own
    sentence.
    """
    rules.check_table(fees)
    given = reader_input(reading)
    conclusion = rules.evaluate(store, case, list(given.tags))
    alone = rules.evaluate(store, case, []) if given.tags else conclusion
    if conclusion.files and not alone.files:  # only the tags made the file possible: code alone's conclusion stands
        conclusion = alone
    evidence = conclusion.evidence
    if given.withdrew and evidence.evidence_class is EvidenceClass.NONE:
        evidence = replace(evidence, evidence_class=EvidenceClass.UNPRICED_PROOF, fact_ids=frozenset(given.withdrew))
    if given.failure is not None:
        lines = [E1.format(why=WHY[given.failure])]
    else:
        lines = [] if given.complete else [IN]
    lines += [line for line, says in ((K1, given.prior_claim), (K2, bool(given.withdrew)), (K5, given.other),
                                      (U1, given.unbacked), (T1, given.stray_tags)) if says]
    before = replace(evidence, conflicts=tuple(dict.fromkeys((*evidence.conflicts, *lines))))
    guard = [G1] if _tags_alone_change_an_accept(conclusion, alone, dispute, before, now, fees) else []
    return replace(evidence, conflicts=tuple(dict.fromkeys((*evidence.conflicts, *guard, *lines)))), conclusion


def _tags_alone_change_an_accept(conclusion: Conclusion, alone: Conclusion, dispute: Dispute, before: Evidence,
                                 now: datetime, fees: FeeTable) -> bool:
    """G1: the tags alone changed code's evidence (through CE 3.0: its outcome, its lines or its file), and the round
    would end in an accept, drafted or decided. A tag is checked only against the bank's comment naming the order, so a
    misread one is the cheapest way to turn a fight into a refund. A person reads the comment first, at any amount.
    Where the tags change nothing code concludes (a later round, another claim, or a sign-up that replaces CE 3.0 at
    CHARGEBACK), the decision is code alone's or a hand-off already. before is the merged evidence with every line but
    this one."""
    if conclusion.evidence == alone.evidence:
        return False
    decision = decide(dispute, before, now, fees)
    return Action.ACCEPT in (decision.action, decision.recommended)


# ----------------------------------------------------------------------------- one round at DECIDE
def decide_round(record: CaseRecord, store: Store, reader: Reader, saved: Saved, now: datetime,
                 fees: FeeTable = FEES_V1) -> Step:
    """Decide the round, at DECIDE only, where keep() makes a fresh plan: decide()'s first call, then the reading
    (saved, or read now), the merge and keep(). Any other phase is Refused.

    A round the first call settles is kept with nothing read. A busy API leaves the round at DECIDE with BUSY_NOTE, until
    its MAX_BUSY-th busy pass or the deadline warning: then it goes on as a failed read, to a person with code's draft. A
    reading validated under older checks, a saved one or a recording, is checked again first. A bug in the reader or the
    rules holds the round for a person with nothing drafted. Every new reading is saved before keep(), failed ones too,
    and the Basis right after it. Errors from keep() and from saved aren't caught.
    """
    step = lifecycle.phase(record)
    if step is not Phase.DECIDE:
        raise lifecycle.Refused(f"Code decides a round only at DECIDE. The case is at {step.value}.")
    dispute_id, number, dispute = record.dispute_id, len(record.rounds), lifecycle.as_dispute(record)
    if decide(dispute, None, now, fees).action is not Action.GATHER_EVIDENCE:  # settled without the records
        return Step(lifecycle.keep(record, None, now, round_number=number, fees=fees), None, None, None, None)
    try:
        request = _request(record, store, reader, dispute)
    except Exception:  # the records can't make a prompt or a key: a bug, as in the rules
        return _held(record, None)
    reading = saved.reading(dispute_id, number, request.key)
    if reading is None:
        busy = False
        try:
            reading = reader.read(request)
        except ReaderBusy as error:
            reading, busy = failed(request.key, Failure.BUSY, error.log, error.calls), True
        except Exception:  # a bug in the reader: nothing came back to log
            return _held(record, None)
        if not busy and (not isinstance(reading, Reading) or reading.key != request.key):
            reading = failed(request.key, Failure.ERROR)  # read from other records or another prompt: not this case's
        saved.put_reading(dispute_id, number, reading)  # as the reader gave it: its tool log and calls are logged
        if busy and saved.busy(dispute_id, number) < MAX_BUSY and dispute.due_at - now > fees.deadline_warning:
            return Step(record, reading, None, None, BUSY_NOTE)
    if reading.checks != CHECKS_VERSION:  # validated under older checks, saved or recorded: check it again, no API call
        try:
            checked = revalidate(reading, request)
        except Exception:  # a bug in the checks
            return _held(record, reading)
        if checked != reading:  # a failed read stays as it was: there is nothing to check again
            reading = checked
            saved.put_reading(dispute_id, number, reading)
    try:
        evidence, conclusion = evidence_for(store, request.case, dispute, reading, now, fees)
    except Exception:  # NoRules, NoFileDue, a TypeError: a bug, so a person takes the round with nothing drafted
        return _held(record, reading)
    kept = lifecycle.keep(record, evidence, now, round_number=number, fees=fees)
    basis = _basis(kept, reading, conclusion)
    saved.put_basis(basis)
    return Step(kept, reading, conclusion, basis, None)


def _request(record: CaseRecord, store: Store, reader: Reader, dispute: Dispute) -> ReadRequest:
    """The round's read request, keyed with the reader's own profile and effort: a reader without them (no key, a
    replay) reads as the default one would."""
    case = lifecycle.as_case(record)
    key = reading_key(store, case, profile=getattr(reader, "profile", prompts.FERNHILL),
                      effort=getattr(reader, "effort", prompts.EFFORT))
    return ReadRequest(store, case, dispute, key)


def _held(record: CaseRecord, reading: Reading | None) -> Step:
    return Step(lifecycle.hold(record, HELD_FOR_ERROR), reading, None, None, None)


def _basis(record: CaseRecord, reading: Reading, conclusion: Conclusion) -> Basis:
    """What the kept plan was made on. Files only when the plan sends them: an accept sends none."""
    plan = record.rounds[-1].plan
    files = conclusion.files if plan is not None and plan.terms.file_hashes else ()
    return Basis(FORMAT, record.dispute_id, len(record.rounds), approvals.plan_sha256(plan) if plan else "", reading.key,
                 reading.failure.value if reading.failure else "", reader_input(reading).tags, conclusion.findings,
                 conclusion.check.reasons if conclusion.check else (), files)


# ----------------------------------------------------------------------------- the screens and the upload
def current(record: CaseRecord, store: Store, saved: Saved, now: datetime, fees: FeeTable = FEES_V1, *,
            profile: prompts.StoreProfile = prompts.FERNHILL, effort: str = prompts.EFFORT) -> Step:
    """What the screens show for the round. It never reads, keeps, holds or drops.

    Once the round's plan went out, or the round is past the phases that make one, it's the saved Basis and its reading,
    and no rule runs: as_case() already counts the sent challenge, so today's records would give another answer. While
    the plan is unsent, code's conclusion is worked out again from today's records with the plan's reading, so the screen
    shows the records as they are beside the plan that waits (files_for() says whether its files still hold). Errors in
    the rules come out, for the screen to show.

    The Basis shown is always the round's own: the one its plan was made on, or the one of a read round code handed to a
    person with no plan. A dropped plan's basis isn't shown as the round's (saved.basis() still has it).
    """
    number, plan = len(record.rounds), record.rounds[-1].plan
    basis = saved.basis(record.dispute_id, number)
    if basis is not None and basis.plan_sha256 != (approvals.plan_sha256(plan) if plan is not None else ""):
        basis = None  # it belongs to a plan this round no longer has
    sent = plan is not None and plan.sending_at is not None
    if sent or lifecycle.phase(record) not in (Phase.DECIDE, Phase.APPROVE, Phase.SEND):
        return Step(record, _basis_reading(record, saved, basis), None, basis, None)
    dispute = lifecycle.as_dispute(record)
    if decide(dispute, None, now, fees).action is not Action.GATHER_EVIDENCE:
        return Step(record, None, None, basis, None)
    case = lifecycle.as_case(record)
    if plan is not None and basis is not None and basis.plan_sha256 == approvals.plan_sha256(plan):
        reading = _basis_reading(record, saved, basis)
    else:
        reading = saved.reading(record.dispute_id, number, reading_key(store, case, profile=profile, effort=effort))
    if reading is None:
        conclusion = rules.evaluate(store, case, [])
    else:
        conclusion = evidence_for(store, case, dispute, reading, now, fees)[1]
    return Step(record, reading, conclusion, basis, None)


def _basis_reading(record: CaseRecord, saved: Saved, basis: Basis | None) -> Reading | None:
    """The reading a basis names: found by its key, or rebuilt from its failure, which is all a failed one says."""
    if basis is None:
        return None
    if basis.failure:
        return failed(basis.reading_key, Failure(basis.failure))
    return saved.reading(record.dispute_id, basis.round_number, basis.reading_key)


def files_for(record: CaseRecord, store: Store, saved: Saved) -> tuple[Document, ...]:
    """The plan's files, built again from today's records with the tags its basis kept, for the approval screen and the
    upload. A plan that sends no file, such as an accept, gets ().

    Raises FilesMoved, with a plain sentence, when there's no basis for this exact plan, when a builder refuses, or when
    the rebuilt files' hashes aren't exactly the plan's: nothing goes out that a person didn't approve. The HTTP step
    catches FilesMoved and calls lifecycle.drop(record, "its file couldn't be built again exactly as planned"), so the
    round is decided and approved again. Never call it after the POST: as_case() then counts the challenge as a later
    round. A retry at SENT reuses the uploaded file ids and never rebuilds. Nothing here keeps, holds, drops or sends.
    """
    plan = record.rounds[-1].plan
    if plan is None or not plan.terms.file_hashes:
        return ()
    basis = saved.basis(record.dispute_id, len(record.rounds))
    if basis is None:
        raise FilesMoved(NO_BASIS)
    if basis.plan_sha256 != approvals.plan_sha256(plan):
        raise FilesMoved(OTHER_BASIS)
    try:
        files = rules.evaluate(store, lifecycle.as_case(record), list(basis.tags)).files
    except (documents.NoFileDue, documents.PersonWritesFile, rules.NoRules) as refusal:
        raise FilesMoved(f"This plan's file can't be built again: {refusal}") from refusal
    except Exception as error:  # a bug: the plan is dropped, and deciding again holds the round for a person
        raise FilesMoved(REBUILD_ERROR) from error
    if {file.sha256 for file in files} != plan.terms.file_hashes:
        raise FilesMoved(MOVED)
    return files


# ----------------------------------------------------------------------------- saved state
def basis_to_json(basis: Basis) -> str:
    # ASCII only, as lifecycle saves records, so any text a record holds can be stored.
    return json.dumps({"format": FORMAT, "basis": dump(basis)}, sort_keys=True, ensure_ascii=True, allow_nan=False)


def basis_from_json(text: str) -> Basis:
    """Strict: anything that doesn't fit a Basis exactly is Unloadable, a file its own checks refuse included."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise Unloadable("The saved basis isn't valid JSON.") from None
    if not isinstance(data, dict) or set(data) != {"format", "basis"} or type(data["format"]) is not int \
            or data["format"] != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} basis.")
    try:
        basis = load(Basis, data["basis"], "the basis")
    except Unloadable:
        raise
    except (TypeError, ValueError):  # a Document refused by its own checks, as a file this app builds never is
        raise Unloadable("A file in the saved basis isn't one this app could have built.") from None
    if basis.format != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} basis.")
    if basis.failure and basis.failure not in _FAILURES:
        raise Unloadable("The saved basis names a failure this app doesn't know.")
    return basis
