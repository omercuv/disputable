"""The decision engine: accept, fight or ask a person, case by case and stage by stage.

Pure code: no network, no clock and no model. What Claude reads reaches it only as an Evidence
package of verified facts, so nothing a customer writes can change a fee, a limit or a chance.
Nothing here acts on a case either: the action is carried out later, and only after any approval.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping

from disputable.fees import FEES_V1, ONE, ZERO, AcceptReason, ChallengeReason, EvidenceClass, FeeTable, money, percent, usd


class Stage(Enum):
    RFI = "RFI"
    PRE_CHARGEBACK = "PRE_CHARGEBACK"
    CHARGEBACK = "CHARGEBACK"
    PRE_ARBITRATION = "PRE_ARBITRATION"
    ARBITRATION = "ARBITRATION"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> Stage:
        return cls.UNKNOWN


class Status(Enum):
    REQUIRES_RESPONSE = "REQUIRES_RESPONSE"
    CHALLENGED = "CHALLENGED"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    REVERSED = "REVERSED"
    PENDING_CLOSURE = "PENDING_CLOSURE"
    PENDING_DECISION = "PENDING_DECISION"
    WON = "WON"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> Status:
        return cls.UNKNOWN


class Action(Enum):
    ACCEPT = "ACCEPT"
    CHALLENGE = "CHALLENGE"
    HAND_OFF = "HAND_OFF"  # a person decides
    GATHER_EVIDENCE = "GATHER_EVIDENCE"  # worth reading the records, then calling decide again
    NO_ACTION = "NO_ACTION"  # nothing to respond to


@dataclass(frozen=True)
class Dispute:
    id: str
    stage: Stage
    status: Status
    amount: Decimal
    currency: str
    reason_code: str
    due_at: datetime
    captured_amount: Decimal
    refunded_amount: Decimal = ZERO  # NaN when the API's responses don't settle it
    prior_challenges: int = 0  # challenges Airwallex has on record, by anyone
    ai_automation_challenged: bool = False
    rejected_fact_ids: frozenset[str] = frozenset()  # what the bank has already turned down
    rejected_file_hashes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Evidence:
    """What code has verified and would submit. Facts without a quoted source never get this far."""
    evidence_class: EvidenceClass
    fact_ids: frozenset[str] = frozenset()
    file_hashes: frozenset[str] = frozenset()  # SHA-256 of each file's content, so a rebuilt file keeps its hash
    conflicts: tuple[str, ...] = ()  # unverifiable or conflicting facts: once records are read, these go to a person


@dataclass(frozen=True)
class Economics:
    """Expected cost of each option from now on. A fee already paid counts in neither."""
    stage: Stage
    chance: Decimal  # RFI: the chance the bank escalates. CHARGEBACK: the chance of winning
    accept_cost: Decimal
    fight_cost: Decimal
    sunk_fee: Decimal
    break_even: Decimal  # the chance at which both options cost the same

    @property
    def fight_advantage(self) -> Decimal:
        return self.accept_cost - self.fight_cost


@dataclass(frozen=True)
class Decision:
    action: Action
    reasons: tuple[str, ...]
    fee_table_version: str
    time_left: timedelta
    deadline_warning: bool = False
    needs_approval: bool = False
    accept_reason: AcceptReason | None = None
    challenge_reason: ChallengeReason | None = None
    refund_amount: Decimal = ZERO  # RFI: always send it (left out, the API refunds all it can). Chargeback: send none
    economics: Economics | None = None
    recommended: Action | None = None  # on a HAND_OFF: what the numbers favour, as a draft for the person


def weigh(dispute: Dispute, chance: Decimal, fees: FeeTable = FEES_V1) -> Economics:
    """Accepting against fighting, for a dispute at RFI or CHARGEBACK."""
    amount = dispute.amount
    if dispute.stage is Stage.RFI:
        # Worst case after an escalation: pay the fee and accept, so that's what the chance is multiplied by.
        accept_cost = amount + fees.refund_fee
        fight_cost = fees.review_cost + chance * (fees.chargeback_fee + amount)
        break_even = (accept_cost - fees.review_cost) / (fees.chargeback_fee + amount)
        sunk_fee = ZERO
    elif dispute.stage is Stage.CHARGEBACK:
        # The bank already took the amount back, so accepting means it stays gone.
        accept_cost = amount
        fight_cost = fees.review_cost + (ONE - chance) * (amount + fees.later_stage_fees)
        break_even = (fees.later_stage_fees + fees.review_cost) / (amount + fees.later_stage_fees)
        sunk_fee = fees.chargeback_fee
    else:
        raise ValueError(f"no cost model for stage {dispute.stage.value}")
    return Economics(dispute.stage, chance, accept_cost, fight_cost, sunk_fee, break_even)


def refund_problem(dispute: Dispute, amount: Decimal, currency: str, to_original_method: bool = True) -> str | None:
    """Why a refund must not happen, or None. For any refund, including one a drafted reply suggests."""
    if dispute.status is not Status.REQUIRES_RESPONSE:
        return f"The dispute is {dispute.status.value}, not waiting for a response, so a person decides on any refund."
    if dispute.stage is not Stage.RFI:
        return "The chargeback already returned this money to the customer, so a refund would pay them twice."
    if not to_original_method:
        return "Refunds only go back to the card that paid."
    if currency != dispute.currency:
        return f"The refund must be in {dispute.currency}, the currency of the dispute."
    if not amount.is_finite() or amount <= ZERO:
        return "A refund must be a positive amount."
    if amount > dispute.amount:
        return f"{usd(amount)} is more than the {usd(dispute.amount)} in dispute."
    if not dispute.refunded_amount.is_finite():
        return "It isn't clear what was already refunded on this payment."
    if dispute.refunded_amount + amount > dispute.captured_amount:
        return f"{usd(amount)} would take refunds past the {usd(dispute.captured_amount)} captured."
    return None


def decide(dispute: Dispute, evidence: Evidence | None, now: datetime, fees: FeeTable = FEES_V1) -> Decision:
    """The next step for one dispute. Call it with no evidence first: a clear case needs no model call."""
    time_left = dispute.due_at - now

    def result(action: Action, *reasons: str, **terms: Any) -> Decision:
        warn = action is not Action.NO_ACTION and time_left <= fees.deadline_warning
        return Decision(action, reasons, fees.version, time_left, deadline_warning=warn, **terms)

    if dispute.status is Status.UNKNOWN:
        return result(Action.HAND_OFF, "Airwallex reported a status this code doesn't know.")
    if dispute.status is not Status.REQUIRES_RESPONSE:
        return result(Action.NO_ACTION, f"The dispute is {dispute.status.value}: nothing to respond to.")
    problem = _outside_the_model(dispute, time_left, fees)
    if problem:
        return result(Action.HAND_OFF, problem)

    # A certain win is the best a fight can do, so this needs no evidence and no model call.
    best_case = weigh(dispute, ZERO if dispute.stage is Stage.RFI else ONE, fees)
    if best_case.fight_advantage <= ZERO:
        return result(
            Action.ACCEPT,
            f"Even a certain win costs {usd(best_case.fight_cost)}, at least as much as the "
            f"{usd(best_case.accept_cost)} it takes to accept.",
            economics=best_case,
            **_accept_terms(dispute, fees, AcceptReason.LOW_VALUE_TRANSACTION),
        )

    if dispute.reason_code not in fees.reason_codes:
        return result(Action.HAND_OFF, f"{fees.version} has no chances for reason code {dispute.reason_code or 'none'}.")
    if evidence is None:
        return result(Action.GATHER_EVIDENCE, "A fight could pay here, so the records are worth reading first.")

    decision = _judge(dispute, evidence, fees, result)
    if not evidence.conflicts:
        return decision
    return replace(
        decision,
        action=Action.HAND_OFF,
        reasons=(*evidence.conflicts, *decision.reasons),
        needs_approval=False,
        recommended=None if decision.action is Action.HAND_OFF else decision.action,
    )


def dispute_from_api(
    dispute: Mapping[str, Any],
    payment: Mapping[str, Any],
    *,  # both sets hold strings, so a swap could only be caught by naming them
    rejected_fact_ids: frozenset[str] = frozenset(),
    rejected_file_hashes: frozenset[str] = frozenset(),
) -> Dispute:
    """Build a Dispute from GET /pa/payment_disputes/{id} and GET /pa/payment_intents/{id}."""
    challenges = dispute.get("challenge_details") or []
    attempt = payment.get("latest_payment_attempt") or {}
    return Dispute(
        id=dispute["id"],
        stage=Stage(dispute.get("stage")),
        status=Status(dispute.get("status")),
        amount=money(dispute["amount"]),
        currency=dispute["currency"],
        reason_code=(dispute.get("reason") or {}).get("original_code") or "",
        due_at=datetime.fromisoformat(dispute["due_at"]),
        captured_amount=money(payment.get("captured_amount") or 0),
        refunded_amount=_refunded_so_far(dispute, attempt),
        prior_challenges=len(challenges),
        ai_automation_challenged=any(c.get("challenge_method") == "AI_DISPUTE_AUTOMATION" for c in challenges),
        rejected_fact_ids=rejected_fact_ids,
        rejected_file_hashes=rejected_file_hashes,
    )


def _refunded_so_far(dispute: Mapping[str, Any], attempt: Mapping[str, Any]) -> Decimal:
    """Not knowing has to stop a case, so neither gap here can count as nothing refunded."""
    refunded = attempt.get("refunded_amount")
    # The second gap: the dispute lists a refund that the payment doesn't show yet.
    if refunded is None or (refunded == 0 and dispute.get("refunds")):
        return Decimal("NaN")
    return money(refunded)


def _outside_the_model(dispute: Dispute, time_left: timedelta, fees: FeeTable) -> str | None:
    """Why the fee model can't safely answer for this dispute, or None."""
    if dispute.currency != fees.currency:
        return f"{fees.version} only has {fees.currency} fees; this dispute is in {dispute.currency}."
    if not (dispute.amount.is_finite() and dispute.captured_amount.is_finite()):
        return "The amounts Airwallex reported aren't usable numbers."
    if dispute.amount <= ZERO:
        return "The disputed amount isn't positive."
    if dispute.amount > dispute.captured_amount:
        return f"The {usd(dispute.amount)} in dispute is more than the {usd(dispute.captured_amount)} captured."
    if not dispute.refunded_amount.is_finite():
        return "It isn't clear what was already refunded on this payment, so accepting could pay the customer twice."
    if dispute.refunded_amount != ZERO:
        return (f"{usd(dispute.refunded_amount)} was already refunded on this payment, "
                "so accepting could pay the customer twice.")
    if time_left <= timedelta(0):
        return "The response deadline has passed."
    if dispute.ai_automation_challenged:
        return "AI Dispute Automation already fought this case, so the agent won't respond on its own."
    if dispute.stage not in (Stage.RFI, Stage.CHARGEBACK):
        return f"There's no cost model for the {dispute.stage.value} stage."
    return None


def _accept_terms(dispute: Dispute, fees: FeeTable, reason: AcceptReason | None = None) -> dict[str, Any]:
    if reason is None:
        if dispute.amount <= fees.auto_accept_limit:
            reason = AcceptReason.LOW_VALUE_TRANSACTION
        elif dispute.reason_code.startswith("10."):
            reason = AcceptReason.OTHERS  # giving up on a fraud claim doesn't admit the fraud happened
        else:
            reason = AcceptReason.VALID_CUSTOMER_DISPUTE
    return {
        "accept_reason": reason,
        # At a chargeback the bank has already taken the money back: a refund would pay twice.
        "refund_amount": dispute.amount if dispute.stage is Stage.RFI else ZERO,
        "needs_approval": dispute.amount > fees.auto_accept_limit,
    }


def _chance(dispute: Dispute, evidence_class: EvidenceClass, fees: FeeTable) -> Decimal | None:
    """What the table says for this evidence on this claim, or None: code never guesses a chance."""
    if evidence_class is not EvidenceClass.NONE and dispute.reason_code not in fees.fight_reason_codes:
        return None
    chances = fees.rfi_escalation_chance if dispute.stage is Stage.RFI else fees.chargeback_win_chance
    return chances.get(evidence_class)


def _judge(dispute: Dispute, evidence: Evidence, fees: FeeTable, result: Callable[..., Decision]) -> Decision:
    whole_record = bool(dispute.rejected_fact_ids and dispute.rejected_file_hashes)

    if not whole_record and (dispute.prior_challenges or dispute.rejected_fact_ids or dispute.rejected_file_hashes):
        return result(Action.HAND_OFF, "There was an earlier challenge, but there's no full record here of its "
                                       "evidence, so nothing can count as new.")
    no_new_fact = whole_record and not evidence.fact_ids - dispute.rejected_fact_ids
    if evidence.evidence_class is EvidenceClass.RESUBMITTED or no_new_fact:
        return _accept_without_new_evidence(dispute, _chance(dispute, EvidenceClass.RESUBMITTED, fees), fees, result)
    if evidence.file_hashes & dispute.rejected_file_hashes:
        return result(Action.HAND_OFF, "There's a new fact, but the package still has a file the bank rejected. "
                                       "It must be rebuilt without that file.")

    chance = _chance(dispute, evidence.evidence_class, fees)
    if chance is None:
        return result(Action.HAND_OFF, f"{fees.version} has no chance for {evidence.evidence_class.value} evidence on "
                                       f"reason code {dispute.reason_code} at the {dispute.stage.value} stage.")
    economics = weigh(dispute, chance, fees)
    reasons = _explain(dispute, economics, fees)
    if economics.fight_advantage <= ZERO:
        return result(Action.ACCEPT, *reasons, economics=economics, **_accept_terms(dispute, fees))
    challenge_reason = fees.challenge_reasons.get(evidence.evidence_class)
    if challenge_reason is None or not evidence.fact_ids or not evidence.file_hashes:
        return result(Action.HAND_OFF, *reasons, "A fight would pay, but the package isn't complete: it needs a "
                                                 "challenge reason, a verified fact and a file.",
                      economics=economics, recommended=Action.CHALLENGE)
    return result(Action.CHALLENGE, *reasons, economics=economics, challenge_reason=challenge_reason,
                  needs_approval=True)


def _accept_without_new_evidence(dispute: Dispute, chance: Decimal | None, fees: FeeTable,
                                 result: Callable[..., Decision]) -> Decision:
    reasons = ["There's no new verified fact since the bank's rejection, so the old evidence can't be sent again."]
    economics = None if chance is None else weigh(dispute, chance, fees)
    if economics is not None:
        if economics.fight_advantage < ZERO:
            reasons.append(f"At a {percent(chance)} win chance it would lose {usd(-economics.fight_advantage)} "
                           "against accepting anyway.")
            without_risk = weigh(dispute, chance, replace(fees, later_stage_fees=ZERO))
            if dispute.stage is Stage.CHARGEBACK and without_risk.fight_advantage > ZERO:
                reasons.append(f"Without the {usd(fees.later_stage_fees)} later-stage risk it would pay "
                               f"{usd(without_risk.fight_advantage)}: the next stage's cost flips the answer.")
        else:
            reasons.append(f"The numbers alone favour sending it by {usd(economics.fight_advantage)}, but "
                           "evidence the bank rejected isn't sent twice.")
    return result(Action.ACCEPT, *reasons, economics=economics, **_accept_terms(dispute, fees))


def _explain(dispute: Dispute, economics: Economics, fees: FeeTable) -> tuple[str, ...]:
    if dispute.stage is Stage.RFI:
        return (f"Fighting costs {usd(economics.fight_cost)} ({usd(fees.review_cost)} review + "
                f"{percent(economics.chance)} escalation chance x ({usd(fees.chargeback_fee)} fee + "
                f"{usd(dispute.amount)})). Refunding costs {usd(economics.accept_cost)}.",)
    advantage = economics.fight_advantage
    return (f"The {usd(economics.sunk_fee)} chargeback fee is already paid, so it no longer counts.",
            f"At a {percent(economics.chance)} win chance, fighting is worth {usd(abs(advantage))} "
            f"{'more' if advantage > ZERO else 'less'} than accepting. It needs a win chance above "
            f"{percent(economics.break_even)}.")
