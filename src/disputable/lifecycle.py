"""The case state machine: one record per Airwallex dispute, round by round.

A round is one time the bank asked for a response. Only GET readings move a record. A POST's own answer never does: the
sandbox's said CHARGEBACK/CHALLENGED where the next GET said PRE_ARBITRATION/PENDING_DECISION. A reading code can't place
stops the case for a person and changes nothing else, though its payment still counts: a plan that no longer fits it is
dropped, and a higher refund is kept. A refund never goes down, so no reading that shows less, or doesn't say, erases one
the record saw.

decide() still makes every money decision. This module keeps its answer as the round's plan, binds a person's approval to
the plan their screen showed, says when the plan may be sent, and books what the case has really cost. Pure code: no
network, no clock, no randomness and no model. A request id comes from the terms it sends. Every time a record keeps is UTC.

The loop, the Claude layer and the screens use it like this:
    records, problems = poll(records, reads, now, store=store, opened=opened)       # GETs only
    if phase(record) is Phase.DECIDE: record = keep(record, evidence, now, round_number=len(record.rounds))
    on the approval screen: record = approve(record, plan_it_showed, "Priya", now)
    if blocker(record, now) is None: record = sending(record, now); save it; POST with plan.request_id and ACTOR
    a 2xx or duplicate_request: taken(); an error no retry fixes: send_failed(); anything else: read again, then retry
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from disputable.engine import Action, Dispute, Evidence, Stage, Status, decide, dispute_from_api, refund_problem
from disputable.fees import FEES_V1, ZERO, EvidenceClass, FeeTable
from disputable.jsonable import Unloadable, canonical, dump, load
from disputable.ledger import Entry, Ledger
from disputable.store import Case, Store

ACTOR = "disputable"  # sent as accepted_by and challenged_by: only this text marks a reply as the app's own
FORMAT = 1
FRESH = timedelta(minutes=10)  # a plan goes out only on a reading this recent
SLIPPED = timedelta(days=2)  # Airwallex's dates never run this far ahead of this app's clock, short of a slip
REQUEST_IDS = uuid.uuid5(uuid.NAMESPACE_URL, "https://disputable.invalid/request-id")
RANK = {Stage.RFI: 0, Stage.PRE_CHARGEBACK: 1, Stage.CHARGEBACK: 2, Stage.PRE_ARBITRATION: 3, Stage.ARBITRATION: 4}
WAITING = frozenset({Status.CHALLENGED, Status.PENDING_DECISION, Status.PENDING_CLOSURE})
FINAL = frozenset({Status.ACCEPTED, Status.WON, Status.LOST, Status.REVERSED, Status.EXPIRED})  # see _final()
NO_ORDER = "This payment isn't tied to an order in the store's records. A person handles it."
ONE_CE3_ATTEMPT = "Visa allows one CE 3.0 attempt, and this case is past its first round."
OTHER_REFUNDS = "The payment shows refunds this app didn't make. A person books them."
LATE_REFUND = "This app's RFI refund shows now, and this app booked it. A person checks that it isn't booked twice."
HELD = ("The payment showed a new refund while this app's response was being sent. It may be this response's own, so it "
        "isn't sent again.")
LANDED = ("Airwallex showed the bank deciding while this app's challenge was being sent, so the bank has it or another "
          "response. CE 3.0's one attempt counts as used.")
CLOCK_BACK = "This app's clock went back, but Airwallex's dates say this reading is newer. A person checks it."
OLDER_COPY = "Airwallex returned an older copy of this dispute than one this app read before. A person checks it."
FUTURE = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
PAYMENT_MOVED = "The dispute and its payment came back out of step, so one of them may be an old copy. A person checks it."
REFUND_FELL = ("The payment shows less refunded than an earlier reading did, so this reading may be an old copy. A person "
               "checks it.")
UNSURE_FIGHT = ("This app's challenge never showed on the dispute, and Airwallex never answered it. The later-stage fees "
                "are booked as if it reached the bank. A person checks it.")
HALF_EMOJI = re.compile("[\ud800-\udfff]")  # a lone surrogate: what's left of an emoji cut in half


class Refused(ValueError):
    """An event the case can't take now. The message says why, in plain words."""


class Unreadable(ValueError):
    """A response code can't place. poll() makes it a line for a person, and the rest of the queue goes on."""


class Phase(Enum):
    DECIDE = "DECIDE"  # the bank waits and nothing is planned: run decide(), then keep()
    APPROVE = "APPROVE"  # a plan waits for a person
    SEND = "SEND"  # a plan may go out once blocker() says nothing
    SENT = "SENT"  # a plan is being sent: every retry reuses its request id
    WAIT = "WAIT"  # a response is on the dispute, or the bank is deciding
    PERSON = "PERSON"  # a person handles the case: nothing goes out
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Reply:
    """One entry of the dispute's accept_details or challenge_details."""
    action: Action  # ACCEPT or CHALLENGE
    stage: Stage
    by: str  # accepted_by or challenged_by: free text, so only ACTOR marks the app's own
    reason: str


@dataclass(frozen=True)
class Reading:
    """One GET of a dispute and its payment intent."""
    at: datetime  # the caller's clock, as UTC
    dispute: Dispute  # as dispute_from_api() parsed it, before the record adds what the bank turned down
    payment_id: str
    opened_at: datetime  # when the dispute was first opened: only the first reading's counts
    created_at: datetime | None  # for screens. It never moves, but in the sandbox it's the run date
    updated_at: datetime | None  # Airwallex moves it on every change, so an older reading is ignored
    order_number: str | None  # the payment's metadata.order_no
    customer_id: str | None  # from the store's order: no dispute response carries the customer
    issuer_comment: str
    replies: tuple[Reply, ...]


@dataclass(frozen=True)
class Terms:
    """What the response sends. The request id comes from it, and any difference voids an approval."""
    dispute_id: str
    stage: Stage  # one round per stage, so this binds the round too
    action: Action  # ACCEPT or CHALLENGE
    amount: Decimal
    currency: str
    customer_id: str
    reason: str  # the AcceptReason or ChallengeReason value
    refund_amount: Decimal
    refunded_before: Decimal  # what the payment showed as refunded when the plan was made
    fact_ids: frozenset[str]  # empty for an accept
    file_hashes: frozenset[str]  # each file's content SHA-256; empty for an accept
    due_at: datetime
    fee_table: str


@dataclass(frozen=True)
class Plan:
    """decide()'s answer for one round, kept on the round from then on: what was sent is never forgotten. An approval binds
    the plan as the screen showed it, reasons included, so the SHA-256 step hashes canonical() of that plan."""
    terms: Terms
    request_id: str  # request_id_for(terms): the POST and every retry send it
    reasons: tuple[str, ...]  # decide()'s, for the screen
    read_at: datetime  # the reading it was made from
    needs_approval: bool
    hand_off: bool = False  # a person's call on code's draft: approving it costs a review, whatever the action
    approved_by: str | None = None
    approved_at: datetime | None = None
    sending_at: datetime | None = None  # the latest POST try, saved before it. From the first, the plan is never replaced
    taken_at: datetime | None = None  # Airwallex answered with a 2xx or duplicate_request, so it's never sent again
    refused: str | None = None  # Airwallex answered with an error no retry fixes


@dataclass(frozen=True)
class Round:
    """One time the bank asked for a response. Every round before the last ended because the bank asked again."""
    stage: Stage
    opened_at: datetime  # when this app first saw the ask
    issuer_comment: str  # the bank's words when it asked
    plan: Plan | None = None
    reply: Reply | None = None  # the response Airwallex shows for this round, by anyone
    notes: tuple[str, ...] = ()  # for a person, in plain words, each once
    person: str | None = None  # why a person handles this round. Only a new round, or a person's booking, ends it


@dataclass(frozen=True)
class Booking:
    """A cost that became real, booked with Ledger's own methods."""
    key: str  # "chargeback_fee", "review:RFI", "refund", "outcome" or "person:1": each books once
    at: datetime
    entries: tuple[Entry, ...]
    fee_table: str


@dataclass(frozen=True)
class CaseRecord:
    dispute_id: str
    opened_at: datetime  # the first round's date: CE 3.0 counts from it in every round, as Visa does
    rounds: tuple[Round, ...]
    last: Reading  # the last reading applied. Its refund never goes down: see _kept() and _payment_news()
    bookings: tuple[Booking, ...] = ()  # append-only
    closed: Status | None = None
    history_only: bool = False  # first seen closed: never in the queue or the totals, but it marks a second dispute
    blocked: str | None = None  # why the latest reading wasn't applied; the next applied one clears it
    others: int = 0  # the most challenges seen on the dispute that this app didn't send. It never goes down
    refunded_at_start: Decimal = ZERO  # what the payment showed as refunded when this app first saw the case


# ----------------------------------------------------------------------------- readings
def read(dispute: Mapping[str, Any], payment: Mapping[str, Any], now: datetime, *, store: Store | None = None,
         opened_at: datetime | None = None) -> Reading:
    """One GET of a dispute and its payment intent. Raises Unreadable on anything code can't place.

    opened_at is when the dispute was first opened. Demo and replay runs pass the seed case's date, because the sandbox's
    created_at is the run date. Left out, created_at counts, then the time of this reading.
    """
    now = _utc(now)
    if opened_at is not None and opened_at.utcoffset() is None:
        raise ValueError("opened_at needs a time zone")
    try:
        if not isinstance(dispute, Mapping) or not isinstance(payment, Mapping):
            raise Unreadable("The response isn't an object.")
        parsed = dispute_from_api(dispute, payment)
        for name, kind, value in (("stage", Stage, parsed.stage), ("status", Status, parsed.status)):
            if value is kind.UNKNOWN:
                raise Unreadable(f"Airwallex reported the {name} {dispute.get(name)!r}, which this code doesn't know.")
        if not (isinstance(parsed.id, str) and parsed.id):
            raise Unreadable("The dispute has no id.")
        if not (isinstance(parsed.currency, str) and isinstance(parsed.reason_code, str)):
            raise Unreadable("The currency or the reason code isn't text.")
        if parsed.due_at.utcoffset() is None:
            raise Unreadable("The due date has no time zone.")
        if not (parsed.amount.is_finite() and parsed.amount > ZERO):
            raise Unreadable("The disputed amount isn't a positive number.")
        if not (parsed.captured_amount.is_finite() and parsed.captured_amount >= ZERO):
            raise Unreadable("The captured amount isn't a usable number.")
        refunded = parsed.refunded_amount  # NaN on purpose when Airwallex's answers don't settle it
        if not (refunded.is_qnan() or (refunded.is_finite() and refunded >= ZERO)):
            raise Unreadable("The refunded amount isn't a usable number.")
        payment_id = payment.get("id")
        if not (isinstance(payment_id, str) and payment_id):
            raise Unreadable("The payment has no id.")
        if dispute.get("payment_intent_id") != payment_id:
            raise Unreadable("The payment isn't this dispute's.")
        if any(HALF_EMOJI.search(text) for text in (parsed.id, parsed.currency, parsed.reason_code, payment_id)):
            raise Unreadable("An id or a code isn't valid text.")
        comment = dispute.get("issuer_comment")
        if comment is not None and not isinstance(comment, str):
            raise Unreadable("The bank's comment isn't text.")
        number = (payment.get("metadata") or {}).get("order_no")
        number = _mend(number) if isinstance(number, str) and number else None
        created_at = _time(dispute.get("created_at"), "created_at")
        return Reading(now, parsed, payment_id, opened_at or created_at or now, created_at,
                       _time(dispute.get("updated_at"), "updated_at"), number, _customer(store, number),
                       _mend(comment or ""), _replies(dispute))
    except Unreadable:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        raise Unreadable(_why(error)) from error


def observe(record: CaseRecord | None, reading: Reading, fees: FeeTable = FEES_V1) -> CaseRecord:
    """Fold one reading into the record. A dispute first seen closed becomes a history-only record."""
    if record is None:
        return _start(reading, fees)
    last = record.last
    undated = reading.updated_at is None and _slipped(record)  # no date can show that the slip is over
    if undated or reading.at < last.at or (reading.updated_at and last.updated_at and reading.updated_at < last.updated_at):
        # Older than a reading already applied, or undated after a slip: the GET lagged, as far as code can tell. Unless
        # the clocks disagree, it has lagged for long, or the payment's own GET shows something new.
        moved = record.closed is None and _payment_moved(reading, last)
        record = _payment_news(record, reading) if moved else record
        if reading.updated_at and last.updated_at and reading.updated_at > last.updated_at:
            return replace(record, blocked=CLOCK_BACK)
        if reading.at - last.at > FRESH:
            return replace(record, blocked=OLDER_COPY)
        return replace(record, blocked=PAYMENT_MOVED) if moved else record
    if record.history_only:
        if not _final(reading.dispute):
            return _reopen(reading, fees)
        if RANK[reading.dispute.stage] < RANK[record.rounds[-1].stage]:
            return record  # a closed dispute doesn't go back a stage: an old copy
        if _dated_ahead(last) and reading.updated_at == last.updated_at:  # the slipped copy read again hides nothing:
            return replace(record, last=replace(reading, at=last.at))  # it keeps its first read time, and the block stays
        return replace(record, last=reading, blocked=None)
    if dump(replace(reading, at=last.at, opened_at=last.opened_at)) == dump(last):  # a duplicate only moves the read time
        kept = _kept(record, reading)  # or not even that, once Airwallex's date slipped
        return replace(_check_plan(record, record, reading), last=kept, blocked=None)  # but the deadline still passes
    problem = _out_of_place(record, reading)
    if problem:
        moved = record.closed is None and _payment_moved(reading, last)
        return replace(_payment_news(record, reading) if moved else record, blocked=problem)
    kept, checked = _kept(record, reading), _check_plan(record, _apply(record, reading, fees), reading)
    if kept.dispute is not reading.dispute:  # a refund the record kept binds the plan too: no later GET may show it again
        checked = _check_plan(record, checked, kept)
    return replace(checked, last=kept, blocked=None)


def poll(records: Mapping[str, CaseRecord], reads: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]], now: datetime,
         *, store: Store | None = None, opened: Mapping[str, datetime] | None = None, fees: FeeTable = FEES_V1,
         ) -> tuple[dict[str, CaseRecord], tuple[str, ...]]:
    """One pass over the queue: each (dispute, payment) pair is one GET of each. opened: dispute id to the date it opened,
    in demo and replay runs. A dispute code can't read or apply becomes a line for a person and the rest go on. Then every
    open dispute on a payment that has another dispute goes to a person, so the customer is never paid twice. A dispute
    this pass couldn't read counts too."""
    records, problems, unread, now = dict(records), [], [], _utc(now)
    for dispute, payment in reads:
        known = dispute.get("id") if isinstance(dispute, Mapping) else None
        known = known if isinstance(known, str) and known else None
        try:
            reading = read(dispute, payment, now, store=store, opened_at=(opened or {}).get(known))
            records[reading.dispute.id] = observe(records.get(reading.dispute.id), reading, fees)
            continue
        except Unreadable as error:
            why = f"can't be read: {error}"
        except Exception:  # a broken saved record, or a bug: one dispute never stops the queue
            why = "couldn't be handled, because this app hit an error."
        line = f"{'Dispute ' + _mend(known) if known else 'A dispute with no id'} {why} A person checks it."
        problems.append(line)
        if known in records:
            records[known] = replace(records[known], blocked=line)
        unread.extend((payment_id, known or f"#{len(problems)}") for payment_id in _payment_ids(dispute, payment))
    return _one_payment_one_case(records, unread), tuple(problems)


# ----------------------------------------------------------------------------- what the record says
def phase(record: CaseRecord) -> Phase:
    if person_reason(record):
        return Phase.PERSON
    if record.closed is not None:  # a history-only record is closed too
        return Phase.CLOSED
    current = record.rounds[-1]
    if current.reply is not None:
        return Phase.WAIT
    plan = current.plan
    if plan is not None:
        if plan.sending_at:
            return Phase.SENT
        return Phase.APPROVE if _needs_approval(plan) and not plan.approved_by else Phase.SEND
    d = record.last.dispute
    if d.status is Status.REQUIRES_RESPONSE and d.stage is current.stage:
        return Phase.DECIDE
    return Phase.WAIT


def person_reason(record: CaseRecord) -> str | None:
    """Why a person handles the case now, or None. For the queue screen. A history-only record shows only a block: one
    can hide a reopened case."""
    if record.blocked:
        # After a slip, the copy that reads as older is Airwallex's current one: the slip is what a person must know. Not
        # CLOCK_BACK: Airwallex's dates call that copy newer, so only this app's clock held it back.
        stale = record.blocked in (OLDER_COPY, PAYMENT_MOVED)
        return FUTURE if stale and _slipped(record) else record.blocked
    if record.history_only:
        return None
    if record.rounds[-1].person:
        return record.rounds[-1].person
    if _slipped(record):
        return FUTURE  # a slip in Airwallex's date: every newer copy reads as an old one, so it's never applied
    unshown = _unshown(record)
    if unshown is not None:
        return (f"This app's response to {unshown.stage.value} doesn't show, and the bank moved the case to "
                f"{record.last.dispute.stage.value}. It may land there, so a person handles it.")
    if record.closed is None and record.last.customer_id is None:
        return NO_ORDER
    return None


def rejected(record: CaseRecord) -> tuple[frozenset[str], frozenset[str]]:
    """Every fact and file the bank turned down: those of each challenge this app sent before the bank asked again, landed
    or not, since the bank may have seen it. Both or neither: a challenge this app didn't send blanks both, so the engine
    hands off instead of treating part of the record as all of it."""
    if record.others:
        return frozenset(), frozenset()
    sent = [r.plan.terms for r in record.rounds[:-1] if _sent(r.plan) and r.plan.terms.action is Action.CHALLENGE]
    return (frozenset().union(*(terms.fact_ids for terms in sent)),
            frozenset().union(*(terms.file_hashes for terms in sent)))


def as_dispute(record: CaseRecord) -> Dispute:
    """The current round as decide() reads it: the last reading, plus everything the bank turned down. A refund this app's
    accept shows counts even before the payment shows it, so a later round never pays the customer twice."""
    facts, files = rejected(record)
    current = record.last.dispute
    refunded = current.refunded_amount
    if refunded.is_finite():
        refunded = max(refunded, _our_refunds(record))
    return replace(current, prior_challenges=max(current.prior_challenges, _challenges(record)),
                   rejected_fact_ids=facts, rejected_file_hashes=files, refunded_amount=refunded)


def as_case(record: CaseRecord) -> Case:
    """The current round as the CE 3.0 check and the Claude layer read it. Days count from the first round's date, as
    Visa's do, and any later round has used Visa's single CE 3.0 attempt: pass it to ce3.consulted(), ce3.evidence() and
    the documents builders. Its prior_challenges counts that attempt and every challenge but this round's own while it's
    being sent, so its file can be built again for the upload, a retry or a person after a refused POST. Once Airwallex
    took it, it shows on the dispute behind any reply, or a reading showed the bank deciding on it (LANDED), the bank has
    it. as_dispute() has the count Airwallex shows."""
    d, last, current = as_dispute(record), record.last, record.rounds[-1]
    plan = current.plan
    shown = any(_is_ours(reply, plan) and reply.stage is current.stage
                for reply in (*last.replies, *((current.reply,) if current.reply else ())))
    own = int(_sent(plan) and plan.terms.action is Action.CHALLENGE and not plan.taken_at and not shown
              and LANDED not in current.notes)
    return Case(d.id, last.order_number or "", d.reason_code, d.stage, record.opened_at, d.due_at - last.at,
                last.issuer_comment, amount=d.amount, status=d.status,
                prior_challenges=max(d.prior_challenges - own, len(record.rounds) - 1),
                ai_automation_challenged=d.ai_automation_challenged, rejected_fact_ids=d.rejected_fact_ids,
                rejected_file_hashes=d.rejected_file_hashes)


def cost(record: CaseRecord) -> Decimal:
    return sum((entry.amount for entry in entries(record)), ZERO)


def entries(record: CaseRecord) -> tuple[Entry, ...]:
    """What the case has really cost, in the order it happened. For the money screen."""
    return tuple(entry for booking in record.bookings for entry in booking.entries)


def at_risk(record: CaseRecord) -> Decimal:
    """The disputed amount while the case is open. Once this app's RFI accept shows, its refund is a cost instead, until
    the bank asks again at a later stage."""
    current = record.rounds[-1]
    if record.closed is not None or (current.reply is not None and _is_ours(current.reply, current.plan)
                                     and current.plan.terms.refund_amount > ZERO):
        return ZERO
    return record.last.dispute.amount


def request_id_for(terms: Terms) -> str:
    """The same terms always give the same id, so a retry repeats it and any change makes a new one."""
    return str(uuid.uuid5(REQUEST_IDS, canonical(terms)))


# ----------------------------------------------------------------------------- what the agent and a person do
def keep(record: CaseRecord, evidence: Evidence | None, now: datetime, *, round_number: int,
         fees: FeeTable = FEES_V1) -> CaseRecord:
    """Run decide() on the current round and keep its answer as the round's plan. Nothing is sent from here.

    round_number is the round the evidence was read for, so a package read for one round is never kept in another. An
    unsent plan is replaced, and its approval with it, unless decide() says the same again: the same terms for the same
    reasons, and no stricter. A new conflict in the records is a new reason, so a person sees it before anything goes out.
    A read that misses one never takes a person, or the hand-off and its reasons, off a plan with the same terms.
    """
    now = _utc(now)
    if round_number != len(record.rounds):
        raise Refused(f"This evidence was read for round {round_number}, but the case is in round {len(record.rounds)}.")
    step = phase(record)
    if step not in (Phase.DECIDE, Phase.APPROVE, Phase.SEND):
        raise Refused(f"The case can't take a new plan now. It is at {step.value}.")
    if evidence is not None and evidence.evidence_class is EvidenceClass.CE3_QUALIFIED and len(record.rounds) > 1:
        raise Refused(ONE_CE3_ATTEMPT)
    basis = as_dispute(record)
    decision = decide(basis, evidence, now, fees)
    if decision.action in (Action.GATHER_EVIDENCE, Action.NO_ACTION):
        raise Refused(f"There's nothing to keep yet. The engine says {decision.action.value}.")
    hand_off = decision.action is Action.HAND_OFF
    action = decision.recommended if hand_off else decision.action
    package = evidence or Evidence(EvidenceClass.NONE)
    reason = {Action.ACCEPT: decision.accept_reason, Action.CHALLENGE: decision.challenge_reason}.get(action)
    if reason is None or (action is Action.CHALLENGE and not (package.fact_ids and package.file_hashes)):
        return _to_person(_set_current(record, plan=None), *decision.reasons)  # a hand-off with no draft to approve
    fight = action is Action.CHALLENGE
    terms = Terms(basis.id, basis.stage, action, basis.amount, basis.currency, record.last.customer_id, reason.value,
                  decision.refund_amount, basis.refunded_amount, package.fact_ids if fight else frozenset(),
                  package.file_hashes if fight else frozenset(), basis.due_at, fees.version)
    plan = Plan(terms, request_id_for(terms), decision.reasons, record.last.at, decision.needs_approval or hand_off,
                hand_off)
    current = record.rounds[-1].plan
    if current is not None and canonical(current.terms) == canonical(terms):
        stricter = (hand_off and not current.hand_off) or (decision.needs_approval and not current.needs_approval)
        # A read that missed a conflict, say: over $50 a person approves either way, but the conflict is why. One that
        # finds only some of a hand-off's conflicts keeps them all on the screen.
        laxer = (_needs_approval(current) and not _needs_approval(plan)) \
            or (current.hand_off and (not hand_off or set(decision.reasons) <= set(current.reasons)))
        if not stricter and (laxer or decision.reasons == current.reasons):
            return record  # the plan, and any approval, stand
    return _set_current(record, plan=plan)


def approve(record: CaseRecord, seen: Plan, by: str, now: datetime, fees: FeeTable = FEES_V1) -> CaseRecord:
    """A person approves the plan their screen showed. Any other plan is void, and so is any approval from another round."""
    _named(by)
    now = _utc(now)
    current = record.rounds[-1]
    if seen.terms.dispute_id != record.dispute_id:
        raise Refused("This approval is for another dispute.")
    if seen.terms.stage is not current.stage:
        raise Refused(f"This approval is void: the stage changed from {seen.terms.stage.value} to {current.stage.value}.")
    step = phase(record)
    if step is not Phase.APPROVE:
        raise Refused(f"Nothing waits for an approval. The case is at {step.value}.")
    plan = current.plan
    if now >= plan.terms.due_at:
        raise Refused("The deadline has passed.")
    changed = _differences(seen.terms, plan.terms)
    if changed:
        raise Refused(f"This approval is void: {_listed(changed)} changed.")
    if dump(seen) != dump(plan):  # the same terms, but decide()'s reasons, or the reading they came from, moved on
        raise Refused("This approval is void: the plan changed after the screen showed it.")
    record = _set_current(record, plan=replace(plan, approved_by=by.strip(), approved_at=now))
    if plan.terms.action is Action.CHALLENGE or plan.hand_off:  # an accept that code decided costs no review, as in weigh()
        record = _book(record, f"review:{current.stage.value}", now, Ledger.reviewed, fees)  # once a round
    return record


def blocker(record: CaseRecord, now: datetime) -> str | None:
    """Why the round's plan can't be sent now, or None: send it with plan.request_id, and the same id on every retry."""
    now = _utc(now)
    step = phase(record)
    if step not in (Phase.SEND, Phase.SENT):
        return {Phase.APPROVE: "It waits for a person's approval.", Phase.PERSON: person_reason(record),
                Phase.CLOSED: "The case is closed."}.get(step, "There's nothing to send.")
    plan, last = record.rounds[-1].plan, record.last
    if plan.taken_at:
        return "Airwallex took the response. It waits for it to show on the dispute."
    if _needs_approval(plan) and not plan.approved_by:
        return "It waits for a person's approval."
    if last.at < plan.read_at or (plan.approved_at and last.at <= plan.approved_at):
        return "Read the dispute again first. The last reading is older than the approval."
    if plan.sending_at and last.at <= plan.sending_at:
        return "Read the dispute again first. It may already show this response."
    if now < last.at:
        return "Read the dispute again first. This app's clock is behind the last reading."
    if now - last.at > FRESH:
        return f"Read the dispute again first. The last reading is more than {FRESH.seconds // 60} minutes old."
    if last.dispute.status is not Status.REQUIRES_RESPONSE or last.dispute.stage is not plan.terms.stage:
        return "Airwallex no longer waits for this response."
    if now >= min(plan.terms.due_at, last.dispute.due_at):
        return "The deadline has passed."
    if plan.terms.refund_amount > ZERO:
        problem = refund_problem(as_dispute(record), plan.terms.refund_amount, plan.terms.currency)
        return HELD if problem is None and HELD in record.rounds[-1].notes else problem  # see _check_plan()
    return None


def sending(record: CaseRecord, now: datetime) -> CaseRecord:
    """Mark a POST try. Save the record before the POST: after a crash, the retry sends the same request id."""
    now = _utc(now)
    problem = blocker(record, now)
    if problem:
        raise Refused(problem)
    return _set_current(record, plan=replace(record.rounds[-1].plan, sending_at=now))


def taken(record: CaseRecord, request_id: str, now: datetime) -> CaseRecord:
    """Airwallex answered the POST with a 2xx or duplicate_request. The plan is never sent again: readings show it."""
    now = _utc(now)
    plan = _in_flight(record, request_id)
    return record if plan.taken_at else _set_current(record, plan=replace(plan, taken_at=now))


def send_failed(record: CaseRecord, request_id: str, error: str, now: datetime) -> CaseRecord:
    """Airwallex answered the POST with an error no retry fixes: a 4xx, but not a 429 or duplicate_request. After a
    timeout, read and retry instead. The plan stays on the round, so a reply that shows later is still the app's."""
    _utc(now)
    plan = _in_flight(record, request_id)
    if plan.taken_at:
        raise Refused("Airwallex already took this response.")
    record = _set_current(record, plan=replace(plan, refused=error))
    return _to_person(record, f"Airwallex refused the response: {error} A person handles this round.")


def book(record: CaseRecord, label: str, amount: Decimal, by: str, now: datetime) -> CaseRecord:
    """A person books a cost code can't see, such as what an RFI that closed as LOST took. A closed case is then done.
    A negative amount corrects an earlier booking, and zero says nothing more moved."""
    _named(by)
    now = _utc(now)
    if record.history_only or not (isinstance(amount, Decimal) and amount.is_finite()) or not label.strip():
        raise Refused("A cost needs a label and an amount, on a case this app handles.")
    number = sum(booking.key.startswith("person:") for booking in record.bookings) + 1
    entry = Entry(f"{label.strip()} (booked by {by.strip()})", amount)
    record = replace(record, bookings=(*record.bookings, Booking(f"person:{number}", now, (entry,), "a person")))
    return _set_current(record, person=None) if record.closed is not None else record


# ----------------------------------------------------------------------------- saved state
def to_json(record: CaseRecord) -> str:
    # ASCII only, so any text Airwallex sent can be stored, even half of an emoji.
    return json.dumps({"format": FORMAT, "record": dump(record)}, sort_keys=True, ensure_ascii=True, allow_nan=False)


def from_json(text: str) -> CaseRecord:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise Unloadable("The saved record isn't valid JSON.") from None
    if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} case record.")
    record = load(CaseRecord, data.get("record"))
    try:
        problem = _broken(record)
    except UnicodeError:  # half an emoji in the terms: no request id can come from them
        problem = "The saved record holds text that isn't valid."
    if problem:
        raise Unloadable(problem)
    return record


# ----------------------------------------------------------------------------- inside
def _final(d: Dispute) -> bool:
    # An RFI or pre-chargeback that expired isn't over: the bank may still open a chargeback.
    return d.status in FINAL and not (d.status is Status.EXPIRED and RANK[d.stage] < RANK[Stage.CHARGEBACK])


def _start(reading: Reading, fees: FeeTable) -> CaseRecord:
    d = reading.dispute
    record = CaseRecord(d.id, reading.opened_at, (Round(d.stage, reading.at, reading.issuer_comment),), reading,
                        refunded_at_start=d.refunded_amount)
    if _final(d):
        return replace(record, closed=d.status, history_only=True)  # old sandbox disputes stay in the queue
    if RANK[d.stage] >= RANK[Stage.CHARGEBACK]:
        record = _book(record, "chargeback_fee", reading.at, Ledger.chargeback_opened, fees)
    record = _match(record, reading, fees)  # someone may have responded before the app looked
    return _unanswered(replace(record, others=_others(record, reading)), reading)


def _reopen(reading: Reading, fees: FeeTable) -> CaseRecord:
    """A dispute first seen closed that Airwallex shows open again: a case this app handles from now on. A person handles
    the round, since code never saw what happened before."""
    d = reading.dispute
    why = (f"Airwallex reopened a dispute that was closed when this app first saw it. It's now {d.stage.value}/"
           f"{d.status.value}. A person handles it.")
    record = _start(reading, fees)
    return _set_current(record, notes=(why, *record.rounds[-1].notes), person=why)


def _apply(record: CaseRecord, reading: Reading, fees: FeeTable) -> CaseRecord:
    d = reading.dispute
    if RANK[d.stage] >= RANK[Stage.CHARGEBACK]:
        record = _book(record, "chargeback_fee", reading.at, Ledger.chargeback_opened, fees)  # once, kept even on a win
    record = _open_round(record, reading)
    record = _match(record, reading, fees, seen=record.last.replies)
    record = replace(record, others=max(record.others, _others(record, reading)))
    if _final(d):
        return record if record.closed is not None else _close(record, reading, fees)
    return _unanswered(record, reading)


def _out_of_place(record: CaseRecord, reading: Reading) -> str | None:
    new, last, current = reading.dispute, record.last, record.rounds[-1]
    if record.closed is not None and (new.status is not record.closed or new.stage is not last.dispute.stage):
        return (f"Airwallex changed a closed case from {last.dispute.stage.value}/{record.closed.value} to "
                f"{new.stage.value}/{new.status.value}. A person checks it. The ledger stays as it was.")
    if len(reading.replies) < len(last.replies):
        return "Airwallex now shows fewer responses on this dispute than before. A person checks it."
    if RANK[new.stage] < RANK[last.dispute.stage]:
        return f"The stage went back from {last.dispute.stage.value} to {new.stage.value}. A person checks it."
    refunded, before = new.refunded_amount, last.dispute.refunded_amount
    if record.closed is None and refunded.is_finite() and before.is_finite() and refunded < before:
        return REFUND_FELL  # a refund never goes down, so the payment's GET lagged
    # Asking again: the status came back to REQUIRES_RESPONSE, or the due date moved. A reply can show before the status
    # moves, so a reading that still asks as before isn't a new ask.
    asks = new.status is Status.REQUIRES_RESPONSE and (last.dispute.status is not Status.REQUIRES_RESPONSE
                                                       or new.due_at != last.dispute.due_at)
    if asks and current.reply is not None:
        if current.reply.action is Action.ACCEPT:
            return "The bank asks for a response after the case was accepted. A person checks it."
        if new.stage is current.stage:
            return "The bank asks again at the same stage after a response. A person checks it."
    answered = current.reply is not None or (_sent(current.plan) and not current.plan.refused)
    if new.status is Status.EXPIRED and not _final(new) and answered:
        return f"Airwallex marked the {new.stage.value} stage expired after a response. A person checks it."
    return None


def _open_round(record: CaseRecord, reading: Reading) -> CaseRecord:
    """A round opens when the bank asks at a later stage, or when a reply shows at one: the bank must have asked there."""
    d, current = reading.dispute, record.rounds[-1]
    there = any(reply.stage is d.stage for reply in reading.replies)
    if RANK[d.stage] <= RANK[current.stage] or not (d.status is Status.REQUIRES_RESPONSE or there):
        return record
    plan = current.plan
    if plan is not None and not plan.sending_at:
        record = _note(_set_current(record, plan=None), f"The plan was dropped: the bank moved the case to {d.stage.value}.")
    elif _sent(plan) and current.reply is None and plan.terms.action is Action.CHALLENGE \
            and not any(r.stage is current.stage for r in reading.replies):  # person_reason() says it may still land
        record = _note(record, ("Airwallex refused this app's challenge. Its evidence still counts as turned down, to be "
                                "safe.") if plan.refused else ("A response was being sent when the bank moved the case "
                                                               "on. Its evidence counts as turned down, since the bank "
                                                               "may have seen it."))
    return replace(record, rounds=(*record.rounds, Round(d.stage, reading.at, reading.issuer_comment)))


def _match(record: CaseRecord, reading: Reading, fees: FeeTable, seen: tuple[Reply, ...] = ()) -> CaseRecord:
    """Put each reply on the round of its stage: the app's own when it matches a plan this app sent, else someone else's.
    The app's own reply at a stage it sent nothing for is a response that landed after the bank moved on. seen: the last
    applied reading's replies. Their alarms went up then, so they don't flag a new round or a case a person closed."""
    for reply in reading.replies:
        index = next((i for i, r in enumerate(record.rounds) if r.stage is reply.stage), None)
        here = None if index is None else record.rounds[index]
        if here is not None and here.reply is not None:
            if here.reply != reply and reply not in seen:
                record = _to_person(record, f"Airwallex shows a second response at {reply.stage.value}. A person "
                                            "checks it.", index=index)
            continue
        if here is not None and _is_ours(reply, here.plan):
            record, plan = _set_round(record, index, reply=reply), here.plan
            if reply.reason != plan.terms.reason:  # the sandbox stores a made-up reason as OTHER_REASONS
                record = _note(record, f"Airwallex stored the reason as {reply.reason or 'nothing'}, not "
                                       f"{plan.terms.reason}.", index=index)
            if plan.terms.action is Action.ACCEPT and plan.terms.refund_amount > ZERO:  # the RFI refund went out
                refund = plan.terms.refund_amount
                record = _book(record, "refund", reading.at, lambda ledger: ledger.accepted_at_rfi(refund), fees)
                if record.rounds[-1].person or any(booking.key.startswith("person:") for booking in record.bookings):
                    # A person handles the case, or did: they may have booked this refund already, or be about to.
                    record = _to_person(record, LATE_REFUND)
                    if record.rounds[-1].person == OTHER_REFUNDS and not _refunds_unbooked(record, reading.dispute):
                        record = _set_current(record, person=LATE_REFUND)  # that line was about this refund, so it's wrong
                elif record.closed is not None and _refunds_unbooked(record, reading.dispute):
                    record = _to_person(record, OTHER_REFUNDS)  # the case closed before this entry showed
            continue
        mislaid = _mislaid(record, reply)
        if mislaid is not None and reply not in seen:
            record = _to_person(record, f"This app's response to {mislaid.stage.value} shows at {reply.stage.value}. A "
                                        "person checks it.")
        if here is None:
            continue  # a stage this app never saw ask: _others() counts a challenge there
        record = _set_round(record, index, reply=reply)
        if _sent(here.plan):
            if mislaid is None:
                record = _to_person(record, "Someone else responded while this app was sending. A person checks it.",
                                    index=index)
        elif here.plan is not None:
            record = _set_round(record, index, plan=None)
            if mislaid is None:
                record = _note(record, "Someone else responded first, so the plan was dropped.", index=index)
    return record


def _check_plan(before: CaseRecord, record: CaseRecord, reading: Reading) -> CaseRecord:
    """A plan binds the reading it was made on. An unsent plan that no longer fits is dropped. One being sent may have
    landed, so it's never dropped or sent again: a change hands the round to a person. before is the record as this
    reading found it, so a change that decide() reads and the terms don't bind counts too."""
    current, d = record.rounds[-1], reading.dispute
    plan = current.plan
    if plan is None or current.reply is not None:
        return record
    seen = replace(plan.terms, dispute_id=d.id, stage=d.stage, amount=d.amount, currency=d.currency,
                   customer_id=reading.customer_id or "", refunded_before=d.refunded_amount, due_at=d.due_at)
    changed, old = _differences(plan.terms, seen), before.last
    news = [text for text, happened in (
        ("the reason code changed", d.reason_code != old.dispute.reason_code),
        ("someone else's challenge showed", record.others > before.others),
        ("AI Dispute Automation challenged the case", d.ai_automation_challenged > old.dispute.ai_automation_challenged),
        ("the captured amount changed", d.captured_amount != old.dispute.captured_amount),
    ) if happened]
    if plan.sending_at:
        mine = plan.terms.refunded_before + plan.terms.refund_amount
        maybe_ours = plan.terms.refund_amount > ZERO and (d.refunded_amount.is_nan() or d.refunded_amount == mine)
        if maybe_ours:
            changed = [name for name in changed if name != "the refunds so far"]  # this app's own refund may move them
        changed = [name for name in changed if name != "the due date"]  # a response doesn't carry it
        found = ([f"{_listed(changed)} changed"] if changed else []) + news
        asks = d.status is Status.REQUIRES_RESPONSE and d.stage is plan.terms.stage
        if found and asks:
            why = _listed(found)
            return _to_person(record, f"{why[0].upper()}{why[1:]} while a response was being sent. A person checks what "
                                      "Airwallex has.")
        if d.status is Status.REQUIRES_RESPONSE and reading.at >= min(d.due_at, plan.terms.due_at):
            return _to_person(record, "This app's response doesn't show on the dispute, and the deadline has passed. A "
                                      "person checks what Airwallex has.")  # answered or not, it never showed
        # A refund that shows while the bank decides counts too: the bank may ask again at this stage.
        waits = d.stage is plan.terms.stage and (asks or d.status in WAITING)
        if maybe_ours and waits and not plan.taken_at:  # or someone else's of the same size: code can't tell them apart
            return _note(record, HELD)  # blocker() holds every retry, so a reading that shows less later can't lift it
        if plan.terms.action is Action.CHALLENGE and d.stage is plan.terms.stage and d.status in WAITING \
                and not plan.taken_at:  # it landed with its 2xx lost, or another response did: kept, as HELD is
            return _note(record, LANDED)
        return record
    if reading.issuer_comment != old.issuer_comment:  # the evidence was read against the bank's words
        news.append("the bank's comment changed")
    found = ([f"{_listed(changed)} changed"] if changed else []) + news
    why = (_listed(found) if found else
           f"the case is now {d.status.value}" if d.status is not Status.REQUIRES_RESPONSE else
           "the deadline passed before it was sent" if reading.at >= plan.terms.due_at else None)
    return record if why is None else _note(_set_current(record, plan=None), f"The plan was dropped: {why}.")


def _unanswered(record: CaseRecord, reading: Reading) -> CaseRecord:
    """Airwallex shows the bank deciding, or a later stage, with nothing answered or sent in this round."""
    d, current = reading.dispute, record.rounds[-1]
    later = RANK[d.stage] > RANK[current.stage]
    if not (d.status in WAITING or later) or current.reply is not None or _sent(current.plan):
        return record
    if d.status in WAITING and not later:
        return _to_person(record, "Airwallex shows the case waiting on the bank, but no response is on record here. A "
                                  "person checks it.")
    return _to_person(record, f"The dispute moved to {d.stage.value} with no response at {current.stage.value}. A person "
                              "checks it.")


def _close(record: CaseRecord, reading: Reading, fees: FeeTable) -> CaseRecord:
    """The outcome's cost, once. A closed case waits for a person when code can't tell what it cost. A round a person
    handles stays theirs: they book what code can't see, and that closes the case for good."""
    d, current = reading.dispute, record.rounds[-1]
    record, unshown = replace(record, closed=d.status), ZERO
    if d.status in (Status.WON, Status.REVERSED):
        pass  # the chargeback fee stays, as it does on a win
    elif RANK[d.stage] >= RANK[Stage.CHARGEBACK]:  # the bank took the amount back, and it stays gone
        fought = _fought(record, reading)
        lose = Ledger.accepted_at_chargeback if fought is False else Ledger.lost_after_fighting
        record = _book(record, "outcome", reading.at, lambda ledger: lose(ledger, d.amount), fees)
        if fought is None:
            record = _to_person(record, UNSURE_FIGHT)
    elif not any(booking.key == "refund" for booking in record.bookings):  # an RFI close without this app's refund
        if not (d.status is Status.ACCEPTED and current.reply is None and _sent(current.plan)
                and current.plan.terms.action is Action.ACCEPT):
            return _to_person(record, f"The case closed as {d.status.value} at the {d.stage.value} stage. Code can't tell "
                                      "what money moved, so a person books it.")
        # _match() books this app's refund when its accept shows. A payment that shows more goes to a person now: the rest
        # isn't this app's, whoever accepted, and an entry that never shows would hide it for good.
        record = _note(record, "When the case closed, this app's accept didn't show on the dispute yet. Its refund is "
                               "booked when it shows.")
        unshown = current.plan.terms.refund_amount
    if _refunds_unbooked(record, d, unshown):
        return _to_person(record, OTHER_REFUNDS)
    return record


def _refunds_unbooked(record: CaseRecord, d: Dispute, unshown: Decimal = ZERO) -> bool:
    """Whether the payment shows refunds that this app didn't make and that weren't there when it first saw the case. d's
    payment GET may lag, so what the reading before it showed counts too: in _close() and _match() that's record.last.
    unshown: the refund of this app's accept that doesn't show on the dispute yet, which the payment may already show."""
    ours = _our_refunds(record) + unshown
    before = record.last.dispute.refunded_amount
    if before.is_finite() and before > (ours if record.refunded_at_start.is_nan() else record.refunded_at_start + ours):
        return True
    if d.refunded_amount.is_nan():  # refunds are listed: this app's own while the payment catches up, or someone else's
        return ours == ZERO
    if record.refunded_at_start.is_nan():  # what the payment showed at the start isn't known
        return d.refunded_amount > ours
    return d.refunded_amount > record.refunded_at_start + ours


def _our_refunds(record: CaseRecord) -> Decimal:
    """What this app's accepts that show on the dispute refunded."""
    return sum((r.plan.terms.refund_amount for r in record.rounds if r.reply is not None and _is_ours(r.reply, r.plan)),
               ZERO)


def _fought(record: CaseRecord, reading: Reading) -> bool | None:
    """Whether a challenge reached the bank at the chargeback stage or later: a loss then costs the later-stage fees too.
    True when Airwallex's entries say so, or Airwallex took a challenge this app sent. None when only a challenge this app
    sent says so, with no answer and no entry. A refused challenge never reached the bank, and neither did one sent at a
    stage that closed as ACCEPTED or EXPIRED."""
    d, chargeback = reading.dispute, RANK[Stage.CHARGEBACK]
    late = [r for r in record.rounds if RANK[r.stage] >= chargeback]
    if any(reply.action is Action.CHALLENGE and RANK[reply.stage] >= chargeback for reply in reading.replies) \
            or any(r.reply is not None and r.reply.action is Action.CHALLENGE for r in late):
        return True
    sent = [r.plan for r in late if r.reply is None and _sent(r.plan) and not r.plan.refused
            and r.plan.terms.action is Action.CHALLENGE
            and not (r.stage is d.stage and d.status in (Status.ACCEPTED, Status.EXPIRED))]
    if any(plan.taken_at for plan in sent):
        return True
    return None if sent else False


def _others(record: CaseRecord, reading: Reading) -> int:
    """Challenges on the dispute that aren't one of this app's sent plans, matched one to one by stage."""
    ours = {r.stage for r in record.rounds if _sent(r.plan) and r.plan.terms.action is Action.CHALLENGE}
    count = 0
    for reply in reading.replies:
        if reply.action is not Action.CHALLENGE:
            continue
        if reply.by == ACTOR and reply.stage in ours:
            ours.discard(reply.stage)
            continue
        count += 1
    return count


def _challenges(record: CaseRecord) -> int:
    return record.others + sum(_sent(r.plan) and r.plan.terms.action is Action.CHALLENGE for r in record.rounds)


def _unshown(record: CaseRecord) -> Round | None:
    """A response this app sent that doesn't show, though the bank moved the case past its stage: it may still land, at
    the new stage. Only the last two rounds count, and nothing once the current round has a reply or the case closed."""
    current, stage = record.rounds[-1], record.last.dispute.stage
    if record.closed is not None or current.reply is not None:
        return None
    return next((r for r in record.rounds[-2:] if r.reply is None and _sent(r.plan) and not r.plan.refused
                 and RANK[stage] > RANK[r.stage]), None)


def _mislaid(record: CaseRecord, reply: Reply) -> Round | None:
    """The round whose response this reply is, when the app's own reply shows at another stage."""
    if reply.by != ACTOR:
        return None
    return next((r for r in record.rounds if r.stage is not reply.stage and r.reply is None and _sent(r.plan)
                 and not r.plan.refused and r.plan.terms.action is reply.action), None)


def _one_payment_one_case(records: dict[str, CaseRecord], unread: list[tuple[str, str]]) -> dict[str, CaseRecord]:
    """unread: (payment id, dispute) for each dispute the pass couldn't read, so one that can't be read still counts."""
    by_payment: dict[str, set[str]] = {}
    for record in records.values():  # history-only records count: an old chargeback leaves refunded_amount alone
        by_payment.setdefault(record.last.payment_id, set()).add(record.dispute_id)
    for payment_id, dispute_id in unread:
        by_payment.setdefault(payment_id, set()).add(dispute_id)
    for ids in by_payment.values():
        if len(ids) > 1:
            for dispute_id in ids & records.keys():
                if records[dispute_id].closed is None:
                    records[dispute_id] = _to_person(records[dispute_id], "Another dispute is on this payment. A person "
                                                                          "handles it, so the customer is never paid twice.")
    return records


def _payment_ids(dispute: Any, payment: Any) -> set[str]:
    """The payment ids a response names, for a dispute code couldn't read."""
    named = (dispute.get("payment_intent_id") if isinstance(dispute, Mapping) else None,
             payment.get("id") if isinstance(payment, Mapping) else None)
    return {value for value in named if isinstance(value, str) and value}


def _payment_moved(new: Reading, old: Reading) -> bool:
    """Whether the payment's side of two readings differs. It has its own GET, which may be newer than the dispute's."""
    def side(reading: Reading) -> Any:
        d = reading.dispute
        return dump((d.captured_amount, d.refunded_amount, reading.order_number, reading.customer_id))
    return side(new) != side(old)


def _payment_news(record: CaseRecord, reading: Reading) -> CaseRecord:
    """What a reading that isn't applied still says: its payment has its own GET, which may be the newer one. The plan is
    checked against that payment. A refund never goes down, so one higher than the last reading's, or shown where that
    one was unclear, is kept as the last reading's: a later GET that lags then reads as an old copy, and decide() sees
    the refund."""
    last = record.last
    record = _check_plan(record, record, _with_payment(last, reading))
    now, before = reading.dispute.refunded_amount, last.dispute.refunded_amount
    if now.is_finite() and (now > before if before.is_finite() else now > ZERO):
        record = replace(record, last=_with_payment(last, reading))
    return record


def _kept(record: CaseRecord, reading: Reading) -> Reading:
    """An applied reading as the record keeps it. A refund never goes down, so one that shows less refunded than the last
    reading did, or doesn't say, keeps the last one's amount: an unclear GET or a lagging one never erases a refund the
    record saw. On an open case REFUND_FELL holds a lower one back, but a closed case applies it. _apply() reads the
    reading as it came, and _check_plan() reads it both ways. Once Airwallex's date slipped, the slipped copy read again
    keeps the time it was first read, so the slip can't age out while that copy is the one applied."""
    shown, before = reading.dispute.refunded_amount, record.last.dispute.refunded_amount
    if before.is_finite() and before > ZERO and (shown.is_nan() or shown < before):
        reading = replace(reading, dispute=replace(reading.dispute, refunded_amount=before))
    if _slipped(record) and reading.updated_at == record.last.updated_at:
        reading = replace(reading, at=record.last.at)  # _slipped() measures from it
    return reading


def _slipped(record: CaseRecord) -> bool:
    """Whether Airwallex dated the last reading of a case this app handles far after this app first read that copy: a slip
    in its date. Every newer copy then reads as an old one, so even a closed case could be reopened unseen. It lasts until
    a copy dated after it is applied: a copy with no date can't end it."""
    return not record.history_only and _dated_ahead(record.last)


def _dated_ahead(reading: Reading) -> bool:
    """Whether Airwallex dated a reading far after this app first read that copy. On a history-only record no FUTURE
    shows, but its block is kept the same way: see observe()."""
    return reading.updated_at is not None and reading.updated_at > reading.at + SLIPPED


def _with_payment(reading: Reading, other: Reading) -> Reading:
    """reading with other's payment side: a payment GET that came with an older or odd copy of the dispute."""
    d = other.dispute
    return replace(reading, dispute=replace(reading.dispute, captured_amount=d.captured_amount,
                                            refunded_amount=d.refunded_amount),
                   order_number=other.order_number, customer_id=other.customer_id)


def _in_flight(record: CaseRecord, request_id: str) -> Plan:
    plan = record.rounds[-1].plan
    if phase(record) is not Phase.SENT or plan.request_id != request_id:
        raise Refused("No response with this request id is being sent.")
    return plan


def _sent(plan: Plan | None) -> bool:
    return plan is not None and plan.sending_at is not None


def _is_ours(reply: Reply, plan: Plan | None) -> bool:
    return reply.by == ACTOR and _sent(plan) and reply.action is plan.terms.action


def _needs_approval(plan: Plan) -> bool:
    # Worked out again from the terms, so a saved flag that went wrong can't skip a person. Never laxer than decide().
    terms = plan.terms
    return (plan.needs_approval or plan.hand_off or terms.action is Action.CHALLENGE
            or terms.amount > FEES_V1.auto_accept_limit)


def _book(record: CaseRecord, key: str, at: datetime, charge: Callable[[Ledger], None], fees: FeeTable) -> CaseRecord:
    if any(booking.key == key for booking in record.bookings):
        return record
    ledger = Ledger(fees)
    charge(ledger)
    return replace(record, bookings=(*record.bookings, Booking(key, at, tuple(ledger.entries), fees.version)))


def _set_round(record: CaseRecord, index: int, **changes: Any) -> CaseRecord:
    rounds = list(record.rounds)
    rounds[index] = replace(rounds[index], **changes)
    return replace(record, rounds=tuple(rounds))


def _set_current(record: CaseRecord, **changes: Any) -> CaseRecord:
    return _set_round(record, len(record.rounds) - 1, **changes)


def _note(record: CaseRecord, note: str, index: int = -1) -> CaseRecord:
    index = index % len(record.rounds)
    here = record.rounds[index]
    return record if note in here.notes else _set_round(record, index, notes=(*here.notes, note))


def _to_person(record: CaseRecord, why: str, *more: str, index: int = -1) -> CaseRecord:
    """The round goes to a person. An earlier round's alarm goes on the current round too, since only the current round
    reaches the queue. The first reason stays the one the queue shows, unless a later reading proves it wrong."""
    for note in (why, *more):
        record = _note(record, note, index)
    for at in {index % len(record.rounds), len(record.rounds) - 1}:
        record = _set_round(record, at, person=record.rounds[at].person or why)
    return record


TERM_NAMES = {"dispute_id": "the dispute", "stage": "the stage", "action": "the action", "amount": "the amount",
              "currency": "the currency", "customer_id": "the customer", "reason": "the reason",
              "refund_amount": "the refund", "refunded_before": "the refunds so far", "fact_ids": "the facts",
              "file_hashes": "the files", "due_at": "the due date", "fee_table": "the fee table"}


def _differences(old: Terms, new: Terms) -> list[str]:
    """What differs, in the order of Terms. Compared as saved text, because a NaN never equals itself."""
    return [TERM_NAMES[item.name] for item in fields(Terms)
            if dump(getattr(old, item.name)) != dump(getattr(new, item.name))]


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _named(by: str) -> None:
    if not (isinstance(by, str) and by.strip()) or by.strip().lower() == ACTOR:
        raise Refused("Only a named person can approve or book, never the agent itself.")


def _customer(store: Store | None, number: str | None) -> str | None:
    if store is None or number is None:
        return None
    try:
        return store.order(number).customer_id
    except KeyError:
        return None


def _replies(dispute: Mapping[str, Any]) -> tuple[Reply, ...]:
    found = []
    for key, action, by in (("accept_details", Action.ACCEPT, "accepted_by"),
                            ("challenge_details", Action.CHALLENGE, "challenged_by")):
        for entry in dispute.get(key) or []:
            if not isinstance(entry, Mapping):
                raise Unreadable(f"An entry of {key} isn't an object.")
            stage = Stage(entry.get("stage"))
            if stage is Stage.UNKNOWN:
                raise Unreadable(f"An entry of {key} names the stage {entry.get('stage')!r}, which this code doesn't know.")
            who, reason = entry.get(by), entry.get("reason")
            found.append(Reply(action, stage, _mend(who) if isinstance(who, str) else "",
                               _mend(reason) if isinstance(reason, str) else ""))
    return tuple(found)


def _time(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Unreadable(f"{name} isn't text.")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise Unreadable(f"{name} has no time zone.")
    return parsed


def _utc(moment: datetime) -> datetime:
    """A caller's time as UTC. Two times in one zone compare by the wall clock, so a store-time clock would get the hour
    that happens twice in November backwards."""
    if moment.utcoffset() is None:
        raise ValueError("A time needs a time zone.")
    return moment.astimezone(timezone.utc)


def _mend(text: str) -> str:
    """Free text as it can be saved and shown: what's left of an emoji cut in half becomes U+FFFD."""
    return HALF_EMOJI.sub("�", text)


def _why(error: Exception) -> str:
    if isinstance(error, KeyError):
        return f"The {error.args[0] if error.args else 'a'} field is missing."
    return "A field isn't in the shape this code expects."  # the exception stays chained, for the log


def _broken(record: CaseRecord) -> str | None:
    """What makes a loaded record impossible, or None."""
    stages = [r.stage for r in record.rounds]
    if not stages or Stage.UNKNOWN in stages or any(RANK[a] >= RANK[b] for a, b in zip(stages, stages[1:])):
        return "The rounds aren't one per stage, in order."
    last, replies = record.last.dispute, (*record.last.replies, *(r.reply for r in record.rounds if r.reply))
    if Stage.UNKNOWN in (last.stage, *(reply.stage for reply in replies)) or last.status is Status.UNKNOWN \
            or any(reply.action not in (Action.ACCEPT, Action.CHALLENGE) for reply in replies):
        return "A reading holds a stage, a status or an action this code doesn't know."
    if RANK[last.stage] < RANK[stages[-1]] or not (last.amount.is_finite() and last.captured_amount.is_finite()):
        return "The last reading doesn't fit the rounds."
    if any(r.plan is not None and not _sent(r.plan) for r in record.rounds[:-1]):
        return "An earlier round still holds a plan that was never sent."
    plans = [(r, r.plan) for r in record.rounds if r.plan is not None]
    if any(plan.terms.dispute_id != record.dispute_id or plan.terms.stage is not r.stage for r, plan in plans):
        return "A plan belongs to another dispute or another round."
    if any(plan.terms.action not in (Action.ACCEPT, Action.CHALLENGE) or not plan.terms.amount.is_finite()
           or not plan.terms.refund_amount.is_finite() for _, plan in plans):
        return "A plan's terms aren't usable."
    if any(plan.request_id != request_id_for(plan.terms) for _, plan in plans):
        return "A plan's request id doesn't come from its terms."
    keys = [booking.key for booking in record.bookings]
    if len(keys) != len(set(keys)):
        return "A cost is booked twice."
    if record.others < 0 or any(not entry.amount.is_finite() for booking in record.bookings for entry in booking.entries):
        return "A count or a booked amount isn't usable."
    if record.dispute_id != record.last.dispute.id or (record.closed is not None and record.closed not in FINAL) \
            or (record.history_only and record.closed is None):
        return "The record doesn't fit its last reading."
    return None
