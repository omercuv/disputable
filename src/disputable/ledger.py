"""What a case has actually cost the store, entry by entry. For the money screen."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from disputable.fees import FEES_V1, ZERO, FeeTable


@dataclass(frozen=True)
class Entry:
    label: str
    amount: Decimal


@dataclass
class Ledger:
    fees: FeeTable = FEES_V1
    entries: list[Entry] = field(default_factory=list)

    def _add(self, label: str, amount: Decimal) -> None:
        self.entries.append(Entry(label, amount))

    def reviewed(self) -> None:
        self._add("A person's review", self.fees.review_cost)

    def chargeback_opened(self) -> None:
        self._add("Chargeback fee", self.fees.chargeback_fee)

    def accepted_at_rfi(self, amount: Decimal) -> None:
        self._add("Refund", amount)
        self._add("Refund fee", self.fees.refund_fee)

    def accepted_at_chargeback(self, amount: Decimal) -> None:
        self._add("Amount the bank took back", amount)

    def lost_after_fighting(self, amount: Decimal) -> None:
        self._add("Amount the bank took back", amount)
        self._add("Later-stage fees", self.fees.later_stage_fees)

    @property
    def total(self) -> Decimal:
        return sum((entry.amount for entry in self.entries), ZERO)
