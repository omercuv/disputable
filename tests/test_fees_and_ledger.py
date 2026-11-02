"""The fee table's checks, and the totals the idea submission states in public."""

from dataclasses import replace
from decimal import Decimal

import pytest

from disputable.fees import FEES_V1, AcceptReason, ChallengeReason, EvidenceClass, money, percent, usd
from disputable.ledger import Ledger


@pytest.mark.parametrize("value, shown", [
    (Decimal("1249.3"), "$1,249.30"),
    (Decimal("-41.15"), "-$41.15"),
    (Decimal("0"), "$0.00"),
    (Decimal("2.675"), "$2.68"),
])
def test_usd(value, shown):
    assert usd(value) == shown


@pytest.mark.parametrize("value, shown", [
    (Decimal("0.15"), "15%"),
    (Decimal("0.45"), "45%"),
    (Decimal("266") / Decimal("1499"), "17.75%"),
    (Decimal("1"), "100%"),
])
def test_percent(value, shown):
    assert percent(value) == shown


def test_money_keeps_the_decimal_a_float_was_written_as():
    assert money(12.3) == Decimal("12.3")


def test_accept_reasons_are_exactly_the_references_list():
    assert {reason.value for reason in AcceptReason} == {
        "AGREEMENT_REACHED_WITH_CUSTOMER", "CUSTOMER_RELATIONSHIP_MAINTENANCE", "LOW_VALUE_TRANSACTION",
        "VALID_CUSTOMER_DISPUTE", "COLLABORATION_ACCEPTED_MANUAL", "OTHERS"}


def test_challenge_reasons_are_exactly_the_references_list():
    assert {reason.value for reason in ChallengeReason} == {
        "CUSTOMER_WITHDRAWN", "CUSTOMER_REFUNDED", "PRODUCT_RECEIVED", "PURCHASE_HISTORY", "NOT_ENTITLED",
        "SEPARATE_PRODUCT", "AUTHENTIC_PRODUCT", "OTHER_REASONS"}


def test_table_refuses_a_negative_fee():
    with pytest.raises(ValueError):
        replace(FEES_V1, chargeback_fee=Decimal("-1"))


def test_table_refuses_a_chance_above_one():
    with pytest.raises(ValueError):
        replace(FEES_V1, chargeback_win_chance={EvidenceClass.NONE: Decimal("1.01")})


def test_table_cant_be_edited_in_place():
    with pytest.raises(TypeError):
        FEES_V1.chargeback_win_chance[EvidenceClass.NONE] = Decimal("1")


def ear_tips():
    ledger = Ledger()
    ledger.accepted_at_rfi(money(12))
    return ledger


def earbuds_after_priya_reviews():
    ledger = Ledger()
    ledger.chargeback_opened()
    ledger.reviewed()
    ledger.accepted_at_chargeback(money(189))
    return ledger


def headphones_fought_twice():
    ledger = Ledger()
    ledger.reviewed()
    ledger.chargeback_opened()
    ledger.reviewed()
    return ledger


def test_cost_of_each_case():
    assert ear_tips().total == Decimal("12.30")
    assert earbuds_after_priya_reviews().total == Decimal("220")
    assert headphones_fought_twice().total == Decimal("47")


def test_queue_cost_when_the_store_wins_the_headphones():
    total = ear_tips().total + earbuds_after_priya_reviews().total + headphones_fought_twice().total
    assert total == Decimal("279.30")


def test_queue_cost_when_the_store_loses_the_headphones():
    headphones = headphones_fought_twice()
    headphones.lost_after_fighting(money(1249))
    assert ear_tips().total + earbuds_after_priya_reviews().total + headphones.total == Decimal("1778.30")


def test_queue_cost_of_accepting_everything():
    earbuds = Ledger()
    earbuds.chargeback_opened()
    earbuds.accepted_at_chargeback(money(189))
    headphones = Ledger()
    headphones.accepted_at_rfi(money(1249))
    assert ear_tips().total + earbuds.total + headphones.total == Decimal("1465.60")
