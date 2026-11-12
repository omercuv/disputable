"""Approvals: who must say yes before a plan goes out, a SHA-256 record of each yes, and the deadline alerts.

lifecycle already binds an approval to the exact plan the screen showed, voids it on any change of terms, stage or round,
books a review once a round and never takes the agent as an approver. This module adds what the screens and the HTTP step
need around that: a tier and a label for each round, written from the terms, never from a saved flag or from text that a
customer, carrier or bank wrote; an append-only record of each approval, bound by SHA-256 to the plan and its terms; a
check of that record before every POST try; and an alert for each open round whose deadline is near. Pure code: no clock,
no network and no model.

The approval screen, the HTTP step and the loop use it like this:
    record, approval = approve(record, jsonable.load(Plan, posted), "Priya", now, reading_key=key)
    save the approval, then the record, in one transaction
    before every POST try: lifecycle.blocker(record, now) is None and verify(record, the round's latest approval) is None
    verify() refuses at SEND, whatever it says: lifecycle.drop(record, "no approval on file matched it"), then decide
        again, and a person approves the new plan. At SENT nothing is dropped: the POST may have landed
    after each pass: alerts(records.values(), now)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Iterable

from disputable import lifecycle
from disputable.engine import Action, Stage, decide
from disputable.fees import CENT, FEES_V1, ZERO, FeeTable, usd
from disputable.jsonable import Unloadable, canonical, dump, load
from disputable.lifecycle import CaseRecord, Phase, Plan, Terms
from disputable.rules import MONTHS

FORMAT = 1
DAY, HOUR = timedelta(days=1), timedelta(hours=1)
WAITS = {  # what an alert says waits, by phase. WAIT never alerts: the bank is deciding, or a response is out
    Phase.DECIDE: "Code hasn't made a plan yet.",
    Phase.APPROVE: "It waits for a person's approval.",
    Phase.SEND: "It's approved and ready to send.",
    Phase.SENT: "It's being sent and doesn't show on the dispute yet.",
    Phase.PERSON: "A person handles this round.",
}
SENDS_ALONE = "It needs no approval and is ready to send."  # SEND, for code's own accept of $50 or less


class Tier(Enum):
    ALONE = "ALONE"  # code's own accept of $50 or less, with no conflict: nobody approves it
    ONE_APPROVAL = "ONE_APPROVAL"  # every challenge and every larger accept
    HAND_OFF = "HAND_OFF"  # a person's call on code's draft: approving it costs a review
    PERSON = "PERSON"  # a person handles the round, and nothing goes out from here
    NOTHING = "NOTHING"  # deciding, waiting on the bank, or closed


@dataclass(frozen=True)
class ApprovalRecord:
    """One approval, as the approvals table keeps it: append-only, saved through jsonable."""
    format: int
    dispute_id: str
    round_number: int
    stage: Stage
    tier: Tier
    plan_sha256: str
    terms_sha256: str
    plan: str  # canonical() of the plan as the screen showed it: the audit copy, which both digests come from
    request_id: str
    by: str
    at: datetime  # the plan's approved_at, in UTC
    reading_key: str  # the reading shown beside the plan, or "" when nothing was read


class Urgency(Enum):
    SOON = "SOON"  # due within the fee table's warning (72 hours)
    TODAY = "TODAY"  # due within 24 hours
    PASSED = "PASSED"


@dataclass(frozen=True)
class Alert:
    dispute_id: str
    order_number: str | None
    urgency: Urgency
    due_at: datetime  # in UTC
    phase: Phase
    message: str


# ----------------------------------------------------------------------------- tiers and labels
def tier(record: CaseRecord) -> Tier:
    """Who must approve the round's plan. Worked out from the terms by lifecycle.needs_approval(), so a saved flag can
    only add a person, never take one off."""
    step, plan = lifecycle.phase(record), record.rounds[-1].plan
    if step is Phase.PERSON:
        return Tier.PERSON
    if plan is None or step not in (Phase.APPROVE, Phase.SEND, Phase.SENT):
        return Tier.NOTHING
    if plan.hand_off:
        return Tier.HAND_OFF
    return Tier.ONE_APPROVAL if lifecycle.needs_approval(plan) else Tier.ALONE


def tier_label(record: CaseRecord) -> str:
    """The queue's line for the round. What the plan sends comes from its terms alone, never from decide()'s reasons, the
    notes or any record text: "Code sends this on its own: accept $12.00 and refund $12.00.", "One approval sends exactly
    this: challenge with 1 file. Any change voids it.", "A person decides. Code's draft: accept $189.00, no refund.
    Approving it costs a review." At PERSON it says what became of the plan, if there is one."""
    step, plan = tier(record), record.rounds[-1].plan
    if step is Tier.NOTHING:
        return "Nothing waits for an approval."
    if plan is None:
        return "A person handles this round. Code drafted nothing."
    sends, approved = _summary(plan.terms), f" {plan.approved_by} approved it." if plan.approved_by else ""
    if step is Tier.PERSON:
        if plan.sending_at is None:
            return f"A person handles this round. Code's draft is on hold: {sends}.{approved}"
        if plan.refused is not None:
            return f"A person handles this round. This app sent {sends}, and Airwallex refused it."
        return f"A person handles this round. This app sent {sends}."
    if step is Tier.ALONE:
        return f"Code sends this on its own: {sends}."
    if step is Tier.ONE_APPROVAL:
        return f"One approval sends exactly this: {sends}. Any change voids it.{approved}"
    return f"A person decides. Code's draft: {sends}. Approving it costs a review.{approved}"


# ----------------------------------------------------------------------------- the approval and its record
def plan_sha256(plan: Plan) -> str:
    """The SHA-256 of canonical() of the plan as the approval screen showed it: its terms (dispute, stage, action, amount,
    currency, customer, reason, refund, refunds so far, fact ids, file hashes, due date and fee table), request id,
    decide()'s reasons, reading time and flags. What approving and sending add is left out, so neither changes it."""
    shown = replace(plan, approved_by=None, approved_at=None, sending_at=None, taken_at=None, refused=None)
    return _sha256(canonical(shown))


def terms_sha256(terms: Terms) -> str:
    """The SHA-256 of what is sent, as the idea lists it."""
    return _sha256(canonical(terms))


def approve(record: CaseRecord, seen: Plan, by: str, now: datetime, *, reading_key: str = "",
            fees: FeeTable = FEES_V1) -> tuple[CaseRecord, ApprovalRecord]:
    """A person approves the plan their screen showed: lifecycle.approve(), and the record of it.

    seen is jsonable.load(Plan, what the screen posted), never rebuilt from shown values. lifecycle.approve() raises
    Refused for a void, stale or late approval, and then nothing is recorded. Save the ApprovalRecord first, then the
    record, in one transaction. A crash between the two leaves a row whose plan isn't approved: blocker() and verify()
    never send on it, and the person approves again.
    """
    if not isinstance(reading_key, str):
        raise TypeError("reading_key is the key of the reading shown beside the plan, or '' when nothing was read.")
    approved = lifecycle.approve(record, seen, by, now, fees)
    plan = approved.rounds[-1].plan  # the name and the time exactly as lifecycle keeps them
    return approved, ApprovalRecord(FORMAT, record.dispute_id, len(record.rounds), seen.terms.stage, tier(record),
                                    plan_sha256(seen), terms_sha256(seen.terms), canonical(seen), seen.request_id,
                                    plan.approved_by, plan.approved_at, reading_key)


def verify(record: CaseRecord, approval: ApprovalRecord | None) -> str | None:
    """Whether approvals let the round's plan go out: None, or why not. Run it before every POST try, on top of
    lifecycle.blocker(). approval is the approvals table's latest row for this dispute and round, or None for no row.
    The row must name the plan's approver and request id and hold both digests. Its time isn't compared: two rows of the
    same plan by the same person, as from a double click, are the same approval."""
    plan = record.rounds[-1].plan
    if plan is None:
        return "There's no plan to send."
    if not lifecycle.needs_approval(plan):
        return None  # code's own accept of $50 or less: nobody approves it
    if approval is None:
        return "No approval is on file for this plan."
    if (approval.dispute_id, approval.round_number) != (record.dispute_id, len(record.rounds)):
        return "The approval on file is for another dispute or round."
    if (plan.approved_by, plan.request_id) != (approval.by, approval.request_id):
        return "The approval on file isn't the one on this plan."
    if plan_sha256(plan) != approval.plan_sha256 or terms_sha256(plan.terms) != approval.terms_sha256:
        return "The plan changed after it was approved."
    return None


def void_reason(record: CaseRecord, approval: ApprovalRecord) -> str | None:
    """Why an approval no longer stands, for the screen, or None while the round's plan still carries it. approval comes
    from approve() or from_json()."""
    if approval.dispute_id != record.dispute_id:
        return "This approval is void: it was for another dispute."
    if approval.round_number < len(record.rounds):
        return "This approval is void: the bank moved the case to a new round."
    if approval.round_number > len(record.rounds):
        return "This approval is void: it was for another round."
    plan = record.rounds[-1].plan
    if plan is None:
        return "This approval is void: the plan was dropped."
    old = load(Plan, json.loads(approval.plan), "the approved plan")
    changed = lifecycle.differences(old.terms, plan.terms)
    if changed:
        return f"This approval is void: {_listed(changed)} changed."
    if old.reasons != plan.reasons:
        return "This approval is void: the plan's reasons changed."
    if plan_sha256(plan) != approval.plan_sha256:
        return "This approval is void: code made the plan again."
    if plan.approved_by != approval.by:  # not the time: a second click on the same plan is the same approval, as verify()
        return "This approval is void: the case has no record of it."
    return None


# ----------------------------------------------------------------------------- deadline alerts
def alerts(records: Iterable[CaseRecord], now: datetime, fees: FeeTable = FEES_V1) -> tuple[Alert, ...]:
    """An alert for each open round that still owes its response, when decide() warns about its deadline: within
    fees.deadline_warning, or past it. A round whose response is out (a reply shows, or Airwallex took it) and a case the
    bank is deciding never alert. Soonest first."""
    now = _utc(now)
    found = []
    for record in records:
        current = record.rounds[-1]
        if record.closed is not None or record.history_only or current.reply is not None \
                or (current.plan is not None and current.plan.taken_at is not None):
            continue
        dispute = lifecycle.as_dispute(record)
        if not decide(dispute, None, now, fees).deadline_warning:
            continue
        due, step = dispute.due_at.astimezone(timezone.utc), lifecycle.phase(record)
        found.append(Alert(record.dispute_id, record.last.order_number, _urgency(due, now), due, step,
                           f"{_when(record, due, now)} {_what_waits(record, step)}"))
    return tuple(sorted(found, key=lambda alert: (alert.due_at, alert.dispute_id)))


# ----------------------------------------------------------------------------- saved state
def to_json(approval: ApprovalRecord) -> str:
    # ASCII only, as lifecycle.to_json() saves a record, so a name in any script can be stored, even half an emoji.
    return json.dumps({"format": FORMAT, "approval": dump(approval)}, sort_keys=True, ensure_ascii=True, allow_nan=False)


def from_json(text: str) -> ApprovalRecord:
    """Strict, as jsonable loads: a missing or extra key, a wrong type, or digests that don't fit the saved plan."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise Unloadable("The saved approval isn't valid JSON.") from None
    if not isinstance(data, dict) or set(data) != {"format", "approval"} or type(data["format"]) is not int \
            or data["format"] != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} approval record.")
    approval = load(ApprovalRecord, data["approval"], "the approval")
    try:
        plan = load(Plan, json.loads(approval.plan), "the approved plan")
    except json.JSONDecodeError:
        raise Unloadable("The approved plan isn't valid JSON.") from None
    problem = _broken(approval, plan)
    if problem:
        raise Unloadable(problem)
    return approval


# ----------------------------------------------------------------------------- inside
def _summary(terms: Terms) -> str:
    """What a plan sends, from its terms alone: "challenge with 1 file", "accept $189.00, no refund"."""
    if terms.action is Action.CHALLENGE:
        return f"challenge with {_count(len(terms.file_hashes), 'file')}"
    amount = _money(terms.amount, terms.currency)
    if terms.refund_amount > ZERO:
        return f"accept {amount} and refund {_money(terms.refund_amount, terms.currency)}"
    return f"accept {amount}, no refund"


def _money(amount: Decimal, currency: str) -> str:
    # fees-v1 only plans in USD. Any other currency is named, so an amount never reads as dollars by mistake.
    return usd(amount) if currency == "USD" else f"{amount.quantize(CENT, ROUND_HALF_UP):,.2f} {currency}"


def _count(number: int, unit: str) -> str:
    return f"{number} {unit}{'' if number == 1 else 's'}"


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _sha256(text: str) -> str:
    # surrogatepass: half an emoji hashes as itself instead of stopping the approval. Valid text is plain UTF-8.
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _utc(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("A time needs a time zone.")
    return moment.astimezone(timezone.utc)


def _urgency(due: datetime, now: datetime) -> Urgency:
    if due <= now:
        return Urgency.PASSED
    return Urgency.TODAY if due - now <= DAY else Urgency.SOON


def _when(record: CaseRecord, due: datetime, now: datetime) -> str:
    who = f"Order #{record.last.order_number}" if record.last.order_number else f"Dispute {record.dispute_id}"
    if due <= now:
        return f"{who} passed its deadline {_span(now - due)} ago."
    return f"{who} is due in {_span(due - now)}, on {MONTHS[due.month - 1]} {due.day} at {due:%H:%M} UTC."


def _span(length: timedelta) -> str:
    """Rounded down, so the time left never reads as more than there is. Up to 24 hours it is counted in hours."""
    if length > DAY:
        return _count(length // DAY, "day")
    if length >= HOUR:
        return _count(length // HOUR, "hour")
    return "under an hour"


def _what_waits(record: CaseRecord, step: Phase) -> str:
    if step is Phase.SEND and not lifecycle.needs_approval(record.rounds[-1].plan):
        return SENDS_ALONE
    return WAITS.get(step, "Airwallex still asks for a response.")  # WAIT can't get here: decide() warned, so it asks


def _broken(approval: ApprovalRecord, plan: Plan) -> str | None:
    """What makes a loaded approval impossible, or None. plan is its audit copy, loaded: the plan as the screen showed it,
    with nothing that approving or sending adds, so both digests come from it."""
    if approval.format != FORMAT:
        return f"This isn't a format {FORMAT} approval record."
    if approval.round_number < 1 or not approval.by.strip() or approval.by != approval.by.strip():
        return "The approval doesn't name a round and a person."
    if canonical(plan) != approval.plan or _sha256(approval.plan) != approval.plan_sha256 \
            or plan_sha256(plan) != approval.plan_sha256 or terms_sha256(plan.terms) != approval.terms_sha256:
        return "The approval's digests don't fit the plan it saved."
    if (plan.terms.dispute_id, plan.terms.stage, plan.request_id) != (approval.dispute_id, approval.stage,
                                                                      approval.request_id):
        return "The approval's plan is for another dispute, stage or request."
    return None


summary = _summary  # a public name for the screens: what a plan sends, from its terms alone
