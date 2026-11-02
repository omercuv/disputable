"""The money table: every fee, limit and chance the decision engine uses.

They sit in one versioned table, so each decision can say which numbers it used. The store's
costs and all the chances are assumptions (docs/idea/idea-submission-v2-detailed.md), not
measured data. A combination the table doesn't list goes to a person: code never guesses a chance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")


def money(value: Decimal | int | float | str) -> Decimal:
    """Exact Decimal for an amount. A float goes through str, so 12.3 stays 12.3."""
    return Decimal(str(value))


def usd(value: Decimal) -> str:
    rounded = value.quantize(CENT, ROUND_HALF_UP)
    return f"{'-' if rounded < 0 else ''}${abs(rounded):,.2f}"


def percent(chance: Decimal) -> str:
    shown = f"{(chance * 100).quantize(CENT, ROUND_HALF_UP):f}"
    return f"{shown.rstrip('0').rstrip('.')}%"


class EvidenceClass(Enum):
    NONE = "NONE"  # nothing verified supports a fight
    CE3_QUALIFIED = "CE3_QUALIFIED"  # Visa CE 3.0 is met by undisputed prior orders
    INDEPENDENT_POSSESSION = "INDEPENDENT_POSSESSION"  # verified possession proof, independent of the disputed device and IP
    RESUBMITTED = "RESUBMITTED"  # only evidence the bank already rejected


class AcceptReason(Enum):  # the accept reference's list; the API rejects anything else
    AGREEMENT_REACHED_WITH_CUSTOMER = "AGREEMENT_REACHED_WITH_CUSTOMER"
    CUSTOMER_RELATIONSHIP_MAINTENANCE = "CUSTOMER_RELATIONSHIP_MAINTENANCE"
    LOW_VALUE_TRANSACTION = "LOW_VALUE_TRANSACTION"
    VALID_CUSTOMER_DISPUTE = "VALID_CUSTOMER_DISPUTE"
    COLLABORATION_ACCEPTED_MANUAL = "COLLABORATION_ACCEPTED_MANUAL"
    OTHERS = "OTHERS"


class ChallengeReason(Enum):  # the challenge reference's list; the API accepts anything, so code must not
    CUSTOMER_WITHDRAWN = "CUSTOMER_WITHDRAWN"
    CUSTOMER_REFUNDED = "CUSTOMER_REFUNDED"
    PRODUCT_RECEIVED = "PRODUCT_RECEIVED"
    PURCHASE_HISTORY = "PURCHASE_HISTORY"
    NOT_ENTITLED = "NOT_ENTITLED"
    SEPARATE_PRODUCT = "SEPARATE_PRODUCT"
    AUTHENTIC_PRODUCT = "AUTHENTIC_PRODUCT"
    OTHER_REASONS = "OTHER_REASONS"


@dataclass(frozen=True)
class FeeTable:
    version: str
    currency: str
    chargeback_fee: Decimal  # charged when a chargeback opens, kept even on a win
    refund_fee: Decimal
    review_cost: Decimal  # a person's time per response
    later_stage_fees: Decimal  # if a fight at the chargeback stage fails
    auto_accept_limit: Decimal  # largest dispute accepted without a person
    deadline_warning: timedelta
    reason_codes: frozenset[str]  # the claims this table covers at all: with no proof, the answer is to accept
    fight_reason_codes: frozenset[str]  # the claims the chances for real proof were written for
    rfi_escalation_chance: Mapping[EvidenceClass, Decimal]
    chargeback_win_chance: Mapping[EvidenceClass, Decimal]
    challenge_reasons: Mapping[EvidenceClass, ChallengeReason]

    def __post_init__(self) -> None:
        for name in ("chargeback_fee", "refund_fee", "review_cost", "later_stage_fees", "auto_accept_limit"):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} can't be negative")
        for chances in (self.rfi_escalation_chance, self.chargeback_win_chance):
            for evidence_class, chance in chances.items():
                if not ZERO <= chance <= ONE:
                    raise ValueError(f"the chance for {evidence_class.name} must be between 0 and 1")


FEES_V1 = FeeTable(
    version="fees-v1",
    currency="USD",
    # Airwallex's US rate. Visa charges extra on fraud chargebacks without 3DS, but no amount is published: left out.
    chargeback_fee=money("15"),
    refund_fee=money("0.30"),
    review_cost=money("16"),
    # Assumed. Airwallex's pages conflict on whether Visa has a pre-arbitration fee; arbitration is capped at 500 USD.
    later_stage_fees=money("250"),
    auto_accept_limit=money("50"),
    deadline_warning=timedelta(hours=72),
    reason_codes=frozenset({"10.4", "13.1", "13.6"}),
    fight_reason_codes=frozenset({"10.4"}),
    rfi_escalation_chance=MappingProxyType({
        EvidenceClass.NONE: ONE,
        EvidenceClass.CE3_QUALIFIED: Decimal("0.15"),
    }),
    chargeback_win_chance=MappingProxyType({
        EvidenceClass.NONE: ZERO,
        EvidenceClass.INDEPENDENT_POSSESSION: Decimal("0.45"),
        EvidenceClass.RESUBMITTED: Decimal("0.15"),
    }),
    challenge_reasons=MappingProxyType({
        EvidenceClass.CE3_QUALIFIED: ChallengeReason.PURCHASE_HISTORY,
        EvidenceClass.INDEPENDENT_POSSESSION: ChallengeReason.PRODUCT_RECEIVED,
    }),
)
