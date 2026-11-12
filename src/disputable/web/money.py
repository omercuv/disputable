"""The money screen's sums: what the queue cost, what is still at risk, and what accepting everything at once would
have cost instead. Pure code over the records, with the ledger's own methods, so the labels and the amounts are the
ones lifecycle books. History-only records are left out, as lifecycle says: never in the queue or the totals. So is a
dispute in another currency than the fee table's: its amount and the table's fees can't be added up, so the sums count
it apart and the screens say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from disputable import lifecycle
from disputable.engine import Stage
from disputable.fees import FEES_V1, ZERO, FeeTable
from disputable.ledger import Entry, Ledger
from disputable.lifecycle import CaseRecord


@dataclass(frozen=True)
class Totals:
    cost: Decimal  # what the handled records in the fee table's currency booked: lifecycle.cost()
    at_risk: Decimal  # lifecycle.at_risk() of each of them
    accept_everything: Decimal  # accept_everything() of each of them it can price
    open: int  # handled records not yet closed, in any currency
    unpriced: tuple[Stage, ...]  # the first stage of each record accept_everything() can't price, in record order
    other_currency: int = 0  # handled records in another currency: in none of the sums

    @property
    def final(self) -> bool:
        """Every handled case is closed, so the comparison is final."""
        return self.open == 0


def handled(records: Iterable[CaseRecord]) -> list[CaseRecord]:
    """The records this app handles: every one but those first seen closed."""
    return [record for record in records if not record.history_only]


def summed(record: CaseRecord, fees: FeeTable = FEES_V1) -> bool:
    """Whether the record's money adds up with the fee table's: its dispute is in the table's currency."""
    return record.last.dispute.currency == fees.currency


def accept_everything(record: CaseRecord, fees: FeeTable = FEES_V1) -> Decimal | None:
    """What accepting the case at once, in its first round, would have cost: at RFI the refund and its fee, at a
    chargeback the fee and the amount the bank took back. No review: approving code's own accept costs none. None when
    the first round was at another stage, which this table can't price."""
    entries = accepting_entries(record, fees)
    return None if entries is None else sum((entry.amount for entry in entries), ZERO)


def accepting_entries(record: CaseRecord, fees: FeeTable = FEES_V1) -> tuple[Entry, ...] | None:
    """The entries accepting at once would have booked, through the ledger's own methods, or None (see
    accept_everything())."""
    stage, amount = record.rounds[0].stage, record.last.dispute.amount
    ledger = Ledger(fees)
    if stage is Stage.RFI:
        ledger.accepted_at_rfi(amount)
    elif stage is Stage.CHARGEBACK:
        ledger.chargeback_opened()
        ledger.accepted_at_chargeback(amount)
    else:
        return None
    return tuple(ledger.entries)


def totals(records: Iterable[CaseRecord], fees: FeeTable = FEES_V1) -> Totals:
    mine = handled(records)
    added = [record for record in mine if summed(record, fees)]
    priced = [accept_everything(record, fees) for record in added]
    return Totals(sum((lifecycle.cost(record) for record in added), ZERO),
                  sum((lifecycle.at_risk(record) for record in added), ZERO),
                  sum((amount for amount in priced if amount is not None), ZERO),
                  sum(record.closed is None for record in mine),
                  tuple(record.rounds[0].stage for record, amount in zip(added, priced) if amount is None),
                  len(mine) - len(added))
