"""The decision engine: Priya's three cases, both endings, the gates and the guards."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import product

import pytest

from disputable.engine import Action, Dispute, Evidence, Stage, Status, decide, dispute_from_api, refund_problem, weigh
from disputable.fees import FEES_V1, ZERO, AcceptReason, ChallengeReason, EvidenceClass, money, percent

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
PRIOR_ORDERS = frozenset({"order-1012", "order-1038", "order-1077"})
PRIOR_PDF = frozenset({"sha256-prior-purchase-pdf"})
WARRANTY_PDF = frozenset({"sha256-warranty-pdf"})
REJECTED = dict(rejected_fact_ids=PRIOR_ORDERS, rejected_file_hashes=PRIOR_PDF)

EAR_TIPS = Dispute(id="dst_ear_tips", stage=Stage.RFI, status=Status.REQUIRES_RESPONSE, amount=money(12), currency="USD",
                   reason_code="13.1", due_at=NOW + timedelta(days=2), captured_amount=money(12))
EARBUDS = Dispute(id="dst_earbuds", stage=Stage.CHARGEBACK, status=Status.REQUIRES_RESPONSE, amount=money(189),
                  currency="USD", reason_code="13.6", due_at=NOW + timedelta(days=4), captured_amount=money(189))
HEADPHONES = Dispute(id="dst_headphones", stage=Stage.RFI, status=Status.REQUIRES_RESPONSE, amount=money(1249),
                     currency="USD", reason_code="10.4", due_at=NOW + timedelta(days=6), captured_amount=money(1249))
HEADPHONES_REJECTED = replace(HEADPHONES, stage=Stage.CHARGEBACK, prior_challenges=1, **REJECTED)
CE3_PACKAGE = Evidence(EvidenceClass.CE3_QUALIFIED, PRIOR_ORDERS, PRIOR_PDF)
WARRANTY_PACKAGE = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, frozenset({"warranty-registration"}), WARRANTY_PDF)
OLD_PACKAGE = Evidence(EvidenceClass.RESUBMITTED, PRIOR_ORDERS, PRIOR_PDF)
OLD_FACTS_NEW_FILE = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, PRIOR_ORDERS, WARRANTY_PDF)
NEW_FACT_OLD_FILE = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, frozenset({"warranty-registration"}),
                             PRIOR_PDF | WARRANTY_PDF)
NO_PROOF = Evidence(EvidenceClass.NONE)


def cents(value):
    return value.quantize(Decimal("0.01"))


# ----------------------------------------------------------------------------- Priya's Monday
def test_ear_tips_are_accepted_alone_before_any_model_call():
    decision = decide(EAR_TIPS, None, NOW)
    assert decision.action is Action.ACCEPT
    assert not decision.needs_approval
    assert decision.accept_reason is AcceptReason.LOW_VALUE_TRANSACTION
    assert decision.refund_amount == money(12)
    assert decision.economics.accept_cost == Decimal("12.30")
    assert decision.economics.fight_cost == Decimal("16")


def test_ear_tips_raise_a_deadline_warning():
    assert decide(EAR_TIPS, None, NOW).deadline_warning


def test_earbuds_need_reading_first():
    assert decide(EARBUDS, None, NOW).action is Action.GATHER_EVIDENCE


def test_earbuds_conflict_goes_to_priya_with_a_drafted_accept_and_no_second_refund():
    conflict = "The carrier says the return arrived, but the warehouse has no scan."
    decision = decide(EARBUDS, Evidence(EvidenceClass.NONE, conflicts=(conflict,)), NOW)
    assert decision.action is Action.HAND_OFF
    assert decision.reasons[0] == conflict
    assert decision.recommended is Action.ACCEPT
    assert decision.accept_reason is AcceptReason.VALID_CUSTOMER_DISPUTE
    assert decision.refund_amount == ZERO
    assert not decision.needs_approval


def test_earbuds_fee_is_sunk():
    economics = decide(EARBUDS, NO_PROOF, NOW).economics
    assert economics.sunk_fee == money(15)
    assert economics.accept_cost == money(189)


def test_email_asking_for_900_to_another_card_is_blocked():
    assert "pay them twice" in refund_problem(EARBUDS, money(900), "USD", to_original_method=False)
    at_rfi = replace(EARBUDS, stage=Stage.RFI)
    assert "card that paid" in refund_problem(at_rfi, money(189), "USD", to_original_method=False)
    assert "more than" in refund_problem(at_rfi, money(900), "USD")


def test_headphones_need_reading_first():
    assert decide(HEADPHONES, None, NOW).action is Action.GATHER_EVIDENCE


def test_headphones_are_fought_with_priyas_approval():
    decision = decide(HEADPHONES, CE3_PACKAGE, NOW)
    assert decision.action is Action.CHALLENGE
    assert decision.needs_approval
    assert decision.challenge_reason is ChallengeReason.PURCHASE_HISTORY
    assert decision.refund_amount == ZERO
    assert decision.economics.fight_cost == Decimal("205.60")
    assert decision.economics.accept_cost == Decimal("1249.30")


def test_the_first_fight_shows_how_its_cost_is_built():
    assert decide(HEADPHONES, CE3_PACKAGE, NOW).reasons == (
        "Fighting costs $205.60 ($16.00 review + 15% escalation chance x ($15.00 fee + $1,249.00)). "
        "Refunding costs $1,249.30.",)


# ----------------------------------------------------------------------------- three weeks later
def test_ending_1_new_proof_fights_again_with_a_new_approval():
    decision = decide(HEADPHONES_REJECTED, WARRANTY_PACKAGE, NOW)
    assert decision.action is Action.CHALLENGE
    assert decision.needs_approval
    assert decision.challenge_reason is ChallengeReason.PRODUCT_RECEIVED
    assert decision.economics.sunk_fee == money(15)
    assert decision.economics.fight_advantage == Decimal("408.55")
    assert percent(decision.economics.break_even) == "17.75%"


def test_ending_1_tells_priya_the_right_numbers():
    assert decide(HEADPHONES_REJECTED, WARRANTY_PACKAGE, NOW).reasons == (
        "The $15.00 chargeback fee is already paid, so it no longer counts.",
        "At a 45% win chance, fighting is worth $408.55 more than accepting. It needs a win chance above 17.75%.",
    )


def test_ending_2_no_new_proof_accepts_and_refunds_nothing():
    decision = decide(HEADPHONES_REJECTED, OLD_PACKAGE, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.needs_approval
    assert decision.accept_reason is AcceptReason.OTHERS
    assert decision.refund_amount == ZERO
    assert decision.economics.fight_advantage == Decimal("-41.15")


def test_ending_2_explains_itself():
    assert decide(HEADPHONES_REJECTED, OLD_PACKAGE, NOW).reasons == (
        "There's no new verified fact since the bank's rejection, so the old evidence can't be sent again.",
        "At a 15% win chance it would lose $41.15 against accepting anyway.",
        "Without the $250.00 later-stage risk it would pay $171.35: the next stage's cost flips the answer.",
    )


# ----------------------------------------------------------------------------- the novelty gate
def test_rejected_evidence_is_never_sent_twice_even_when_the_numbers_favour_it():
    big = replace(HEADPHONES_REJECTED, amount=money(50000), captured_amount=money(50000))
    decision = decide(big, OLD_PACKAGE, NOW)
    assert decision.economics.fight_advantage > ZERO
    assert decision.action is Action.ACCEPT


def test_a_package_labelled_new_with_only_old_facts_is_blocked():
    assert decide(HEADPHONES_REJECTED, OLD_FACTS_NEW_FILE, NOW).action is Action.ACCEPT


def test_a_package_with_no_facts_is_blocked_after_a_rejection():
    empty = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, frozenset(), WARRANTY_PDF)
    assert decide(HEADPHONES_REJECTED, empty, NOW).action is Action.ACCEPT


def test_a_resubmitted_label_is_blocked_whatever_facts_come_with_it():
    fresh_label = Evidence(EvidenceClass.RESUBMITTED, frozenset({"something-new"}), WARRANTY_PDF)
    assert decide(HEADPHONES_REJECTED, fresh_label, NOW).action is Action.ACCEPT
    never_rejected = replace(HEADPHONES_REJECTED, prior_challenges=0, rejected_fact_ids=frozenset(),
                             rejected_file_hashes=frozenset(), amount=money(50000), captured_amount=money(50000))
    assert decide(never_rejected, fresh_label, NOW).action is Action.ACCEPT


def test_a_new_fact_with_a_rejected_file_must_be_rebuilt():
    decision = decide(HEADPHONES_REJECTED, NEW_FACT_OLD_FILE, NOW)
    assert decision.action is Action.HAND_OFF
    assert "rebuilt" in decision.reasons[0]


def test_an_earlier_challenge_with_no_local_record_goes_to_a_person():
    unknown = replace(HEADPHONES_REJECTED, rejected_fact_ids=frozenset(), rejected_file_hashes=frozenset())
    decision = decide(unknown, WARRANTY_PACKAGE, NOW)
    assert decision.action is Action.HAND_OFF
    assert "earlier challenge" in decision.reasons[0]


@pytest.mark.parametrize("prior_challenges", [0, 1])
@pytest.mark.parametrize("facts, files", [(PRIOR_ORDERS, frozenset()), (frozenset(), PRIOR_PDF)])
@pytest.mark.parametrize("package", [WARRANTY_PACKAGE, OLD_FACTS_NEW_FILE, NEW_FACT_OLD_FILE])
def test_half_a_rejection_record_goes_to_a_person(prior_challenges, facts, files, package):
    dispute = replace(HEADPHONES_REJECTED, prior_challenges=prior_challenges,
                      rejected_fact_ids=facts, rejected_file_hashes=files)
    decision = decide(dispute, package, NOW)
    assert decision.action is Action.HAND_OFF
    assert "earlier challenge" in decision.reasons[0]


def test_new_proof_still_has_to_pay_for_itself():
    small = replace(HEADPHONES_REJECTED, amount=money(100), captured_amount=money(100))
    decision = decide(small, WARRANTY_PACKAGE, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.economics.fight_advantage == Decimal("-108.50")


# ----------------------------------------------------------------------------- limits and boundaries
@pytest.mark.parametrize("amount, needs_approval", [("50", False), ("50.01", True), ("189", True)])
def test_accepts_above_50_need_approval(amount, needs_approval):
    dispute = replace(EARBUDS, amount=money(amount), captured_amount=money(amount))
    decision = decide(dispute, NO_PROOF, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.needs_approval is needs_approval


def test_small_fight_still_needs_approval():
    dispute = replace(HEADPHONES, amount=money(40), captured_amount=money(40))
    decision = decide(dispute, CE3_PACKAGE, NOW)
    assert decision.action is Action.CHALLENGE
    assert decision.needs_approval


@pytest.mark.parametrize("amount, action", [("15.70", Action.ACCEPT), ("15.71", Action.GATHER_EVIDENCE)])
def test_certain_win_rule_at_rfi(amount, action):
    dispute = replace(EAR_TIPS, amount=money(amount), captured_amount=money(amount))
    assert decide(dispute, None, NOW).action is action


@pytest.mark.parametrize("amount, action", [("16", Action.ACCEPT), ("16.01", Action.GATHER_EVIDENCE)])
def test_certain_win_rule_at_chargeback(amount, action):
    dispute = replace(EARBUDS, amount=money(amount), captured_amount=money(amount))
    assert decide(dispute, None, NOW).action is action


def test_certain_win_rule_at_chargeback_refunds_nothing():
    dispute = replace(EARBUDS, amount=money(12), captured_amount=money(12))
    decision = decide(dispute, None, NOW)
    assert decision.accept_reason is AcceptReason.LOW_VALUE_TRANSACTION
    assert decision.refund_amount == ZERO


def test_a_certain_loss_is_accepted_whatever_the_reason_code():
    decision = decide(replace(EAR_TIPS, reason_code="4837"), None, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.accept_reason is AcceptReason.LOW_VALUE_TRANSACTION


def test_a_conflict_cant_stop_a_certain_loss():
    # These cases are accepted before any records are read, and a person's review would cost more than the case.
    decision = decide(EAR_TIPS, Evidence(EvidenceClass.NONE, conflicts=("The carrier and warehouse disagree.",)), NOW)
    assert decision.action is Action.ACCEPT


@pytest.mark.parametrize("base, package, amount, action", [
    (HEADPHONES, CE3_PACKAGE, "21.11", Action.ACCEPT),
    (HEADPHONES, CE3_PACKAGE, "21.12", Action.CHALLENGE),
    (HEADPHONES_REJECTED, WARRANTY_PACKAGE, "341", Action.ACCEPT),
    (HEADPHONES_REJECTED, WARRANTY_PACKAGE, "342", Action.CHALLENGE),
])
def test_the_fight_starts_exactly_where_it_pays(base, package, amount, action):
    dispute = replace(base, amount=money(amount), captured_amount=money(amount))
    assert decide(dispute, package, NOW).action is action


def test_an_even_fight_is_not_taken():
    # Fighting costs $100 + 50% of $200 = $200, the same as accepting, so the store doesn't take the risk.
    fees = replace(FEES_V1, chargeback_win_chance={EvidenceClass.INDEPENDENT_POSSESSION: Decimal("0.5")},
                   later_stage_fees=ZERO, review_cost=money(100))
    dispute = replace(HEADPHONES_REJECTED, amount=money(200), captured_amount=money(200))
    decision = decide(dispute, WARRANTY_PACKAGE, NOW, fees)
    assert decision.economics.fight_advantage == ZERO
    assert decision.action is Action.ACCEPT


@pytest.mark.parametrize("dispute, refund, advantage", [
    (HEADPHONES, money(1249), Decimal("-30.70")),
    (replace(HEADPHONES, stage=Stage.CHARGEBACK), ZERO, Decimal("-266")),
])
def test_no_proof_is_accepted_at_any_size(dispute, refund, advantage):
    decision = decide(dispute, NO_PROOF, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.needs_approval
    assert decision.refund_amount == refund
    assert decision.economics.fight_advantage == advantage


def test_an_accept_on_the_numbers_explains_itself():
    assert decide(EARBUDS, NO_PROOF, NOW).reasons == (
        "The $15.00 chargeback fee is already paid, so it no longer counts.",
        "At a 0% win chance, fighting is worth $266.00 less than accepting. It needs a win chance above 60.59%.",
    )


@pytest.mark.parametrize("amount, code, reason", [
    ("40", "13.6", AcceptReason.LOW_VALUE_TRANSACTION),
    ("50", "10.4", AcceptReason.LOW_VALUE_TRANSACTION),
    ("50.01", "10.4", AcceptReason.OTHERS),
    ("50.01", "13.6", AcceptReason.VALID_CUSTOMER_DISPUTE),
])
def test_accept_reason_by_size_and_claim(amount, code, reason):
    dispute = replace(EARBUDS, amount=money(amount), captured_amount=money(amount), reason_code=code)
    assert decide(dispute, NO_PROOF, NOW).accept_reason is reason


def test_giving_up_on_a_non_fraud_claim_says_so():
    decision = decide(replace(HEADPHONES_REJECTED, reason_code="13.6"), OLD_PACKAGE, NOW)
    assert decision.accept_reason is AcceptReason.VALID_CUSTOMER_DISPUTE


def test_partial_dispute_refunds_only_the_disputed_amount():
    dispute = replace(EAR_TIPS, amount=money(10), captured_amount=money(100))
    assert decide(dispute, None, NOW).refund_amount == money(10)


def test_decision_says_which_table_it_used():
    assert decide(EAR_TIPS, None, NOW).fee_table_version == "fees-v1"


# ----------------------------------------------------------------------------- what a person must see
@pytest.mark.parametrize("change, words", [
    ({"currency": "EUR"}, "USD fees"),
    ({"amount": money(0)}, "isn't positive"),
    ({"amount": money(-5)}, "isn't positive"),
    ({"amount": Decimal("NaN")}, "usable numbers"),
    ({"amount": money(1300)}, "more than"),
    ({"refunded_amount": money(1)}, "already refunded"),
    ({"refunded_amount": Decimal("NaN")}, "isn't clear"),
    ({"due_at": NOW}, "deadline has passed"),
    ({"due_at": NOW - timedelta(days=1)}, "deadline has passed"),
    ({"ai_automation_challenged": True}, "AI Dispute Automation"),
    ({"stage": Stage.PRE_ARBITRATION}, "PRE_ARBITRATION"),
    ({"stage": Stage.PRE_CHARGEBACK}, "PRE_CHARGEBACK"),
    ({"stage": Stage.UNKNOWN}, "UNKNOWN"),
    ({"reason_code": "4837"}, "4837"),
    ({"reason_code": ""}, "none"),
    ({"status": Status.UNKNOWN}, "doesn't know"),
])
def test_hand_offs(change, words):
    decision = decide(replace(HEADPHONES, **change), CE3_PACKAGE, NOW)
    assert decision.action is Action.HAND_OFF
    assert words in decision.reasons[0]
    assert not decision.needs_approval


@pytest.mark.parametrize("change, words", [
    ({"currency": "EUR"}, "USD fees"),
    ({"refunded_amount": money(1)}, "already refunded"),
    ({"refunded_amount": Decimal("NaN")}, "isn't clear"),
    ({"captured_amount": money(0)}, "more than"),
    ({"due_at": NOW}, "deadline has passed"),
    ({"ai_automation_challenged": True}, "AI Dispute Automation"),
])
def test_a_small_case_goes_through_the_same_gates(change, words):
    decision = decide(replace(EAR_TIPS, **change), None, NOW)
    assert decision.action is Action.HAND_OFF
    assert words in decision.reasons[0]
    assert decision.refund_amount == ZERO
    assert decision.accept_reason is None


@pytest.mark.parametrize("status", [Status.CHALLENGED, Status.ACCEPTED, Status.EXPIRED, Status.REVERSED,
                                    Status.PENDING_CLOSURE, Status.PENDING_DECISION, Status.WON, Status.LOST])
def test_nothing_to_respond_to(status):
    decision = decide(replace(HEADPHONES, status=status, ai_automation_challenged=True), CE3_PACKAGE, NOW)
    assert decision.action is Action.NO_ACTION
    assert not decision.deadline_warning


@pytest.mark.parametrize("status", [Status.WON, Status.CHALLENGED])
def test_a_closed_case_near_its_old_deadline_raises_no_warning(status):
    decision = decide(replace(HEADPHONES, status=status, due_at=NOW + timedelta(hours=1)), CE3_PACKAGE, NOW)
    assert decision.action is Action.NO_ACTION
    assert not decision.deadline_warning


@pytest.mark.parametrize("stage, evidence_class", [
    (Stage.RFI, EvidenceClass.INDEPENDENT_POSSESSION),
    (Stage.CHARGEBACK, EvidenceClass.CE3_QUALIFIED),
])
def test_code_never_guesses_a_chance_the_table_doesnt_hold(stage, evidence_class):
    package = Evidence(evidence_class, frozenset({"fact"}), frozenset({"file"}))
    decision = decide(replace(HEADPHONES, stage=stage), package, NOW)
    assert decision.action is Action.HAND_OFF
    assert "no chance" in decision.reasons[0]


@pytest.mark.parametrize("code", ["13.1", "13.6"])
@pytest.mark.parametrize("stage, package", [(Stage.RFI, CE3_PACKAGE), (Stage.CHARGEBACK, WARRANTY_PACKAGE)])
def test_chances_written_for_a_fraud_claim_arent_used_on_other_claims(code, stage, package):
    decision = decide(replace(HEADPHONES, stage=stage, reason_code=code), package, NOW)
    assert decision.action is Action.HAND_OFF
    assert "no chance" in decision.reasons[0] and code in decision.reasons[0]
    assert decision.economics is None


def test_old_evidence_on_another_claim_is_refused_without_made_up_numbers():
    decision = decide(replace(HEADPHONES_REJECTED, reason_code="13.1"), OLD_PACKAGE, NOW)
    assert decision.action is Action.ACCEPT
    assert decision.economics is None
    assert len(decision.reasons) == 1


@pytest.mark.parametrize("package", [
    Evidence(EvidenceClass.CE3_QUALIFIED, frozenset(), PRIOR_PDF),
    Evidence(EvidenceClass.CE3_QUALIFIED, PRIOR_ORDERS, frozenset()),
])
def test_a_fight_without_facts_or_files_goes_to_a_person(package):
    decision = decide(HEADPHONES, package, NOW)
    assert decision.action is Action.HAND_OFF
    assert decision.recommended is Action.CHALLENGE
    assert decision.challenge_reason is None


def test_conflict_overrides_a_fight_but_keeps_the_numbers():
    package = replace(CE3_PACKAGE, conflicts=("The signature doesn't match.",))
    decision = decide(HEADPHONES, package, NOW)
    assert decision.action is Action.HAND_OFF
    assert decision.recommended is Action.CHALLENGE
    assert not decision.needs_approval
    assert decision.economics.fight_cost == Decimal("205.60")


def test_conflict_on_a_hand_off_recommends_nothing_but_still_shows():
    package = replace(CE3_PACKAGE, conflicts=("The signature doesn't match.",))
    decision = decide(replace(HEADPHONES, stage=Stage.CHARGEBACK), package, NOW)
    assert decision.action is Action.HAND_OFF
    assert decision.recommended is None
    assert decision.reasons[0] == "The signature doesn't match."


# ----------------------------------------------------------------------------- refund guard
@pytest.mark.parametrize("dispute, amount, currency, original, words", [
    (EAR_TIPS, money(12), "USD", True, None),
    (EAR_TIPS, money(5), "USD", True, None),
    (EAR_TIPS, money(13), "USD", True, "more than"),
    (EAR_TIPS, money(0), "USD", True, "positive"),
    (EAR_TIPS, money(-1), "USD", True, "positive"),
    (EAR_TIPS, Decimal("NaN"), "USD", True, "positive"),
    (EAR_TIPS, money(12), "EUR", True, "currency"),
    (EAR_TIPS, money(12), "USD", False, "card that paid"),
    (replace(EAR_TIPS, refunded_amount=money(1)), money(12), "USD", True, "past"),
    (replace(EAR_TIPS, refunded_amount=Decimal("NaN")), money(12), "USD", True, "isn't clear"),
    (EARBUDS, money(189), "USD", True, "pay them twice"),
    (replace(HEADPHONES, stage=Stage.PRE_ARBITRATION), money(1), "USD", True, "pay them twice"),
])
def test_refund_guard(dispute, amount, currency, original, words):
    problem = refund_problem(dispute, amount, currency, original)
    assert problem is None if words is None else words in problem


@pytest.mark.parametrize("status", [status for status in Status if status is not Status.REQUIRES_RESPONSE])
@pytest.mark.parametrize("stage", [Stage.RFI, Stage.CHARGEBACK])
def test_a_refund_needs_a_dispute_waiting_for_a_response(status, stage):
    assert status.value in refund_problem(replace(EAR_TIPS, status=status, stage=stage), money(12), "USD")


# ----------------------------------------------------------------------------- deadlines and the cost model
@pytest.mark.parametrize("hours, warned", [(1, True), (72, True), (73, False)])
def test_deadline_warning(hours, warned):
    dispute = replace(HEADPHONES, due_at=NOW + timedelta(hours=hours))
    decision = decide(dispute, CE3_PACKAGE, NOW)
    assert decision.deadline_warning is warned
    assert decision.time_left == timedelta(hours=hours)


@pytest.mark.parametrize("change, package, action", [
    ({}, None, Action.GATHER_EVIDENCE),
    ({"reason_code": "4837"}, None, Action.HAND_OFF),
    ({}, replace(CE3_PACKAGE, conflicts=("A conflict.",)), Action.HAND_OFF),
])
def test_a_case_a_person_or_the_model_still_has_to_act_on_warns_too(change, package, action):
    decision = decide(replace(HEADPHONES, due_at=NOW + timedelta(hours=1), **change), package, NOW)
    assert decision.action is action
    assert decision.deadline_warning


def test_naive_times_are_refused():
    with pytest.raises(TypeError):
        decide(EAR_TIPS, None, datetime(2026, 11, 2))


def test_weigh_refuses_a_stage_it_has_no_model_for():
    with pytest.raises(ValueError):
        weigh(replace(HEADPHONES, stage=Stage.PRE_ARBITRATION), ZERO)


def test_break_even_is_where_the_options_cost_the_same():
    for dispute in (HEADPHONES, HEADPHONES_REJECTED):
        at_break_even = weigh(dispute, weigh(dispute, ZERO).break_even)
        assert cents(at_break_even.fight_advantage) == ZERO


def test_later_stage_risk_is_what_flips_the_old_evidence():
    without_risk = weigh(HEADPHONES_REJECTED, Decimal("0.15"), replace(FEES_V1, later_stage_fees=ZERO))
    assert without_risk.fight_advantage == Decimal("171.35")


# ----------------------------------------------------------------------------- rules that hold for every case
AMOUNTS = ["0.01", "12", "15.70", "16", "16.01", "49.99", "50", "50.01", "189", "341", "342", "1249", "50000"]
PACKAGES = [None, NO_PROOF, CE3_PACKAGE, WARRANTY_PACKAGE, OLD_PACKAGE,
            replace(WARRANTY_PACKAGE, conflicts=("A conflict.",)),
            Evidence(EvidenceClass.INDEPENDENT_POSSESSION, PRIOR_ORDERS, PRIOR_PDF | WARRANTY_PDF),
            NEW_FACT_OLD_FILE, OLD_FACTS_NEW_FILE]
HISTORIES = [dict(prior_challenges=0), dict(prior_challenges=1), dict(prior_challenges=1, **REJECTED)]


@pytest.mark.parametrize("amount, stage, package, history", list(product(
    AMOUNTS, [Stage.RFI, Stage.CHARGEBACK], PACKAGES, HISTORIES)))
def test_rules_that_always_hold(amount, stage, package, history):
    dispute = replace(HEADPHONES, amount=money(amount), captured_amount=money(amount), stage=stage, **history)
    decision = decide(dispute, package, NOW)

    assert decision.action is not Action.NO_ACTION
    if decision.action is Action.CHALLENGE:
        assert decision.needs_approval
        assert decision.challenge_reason is not None
        assert decision.refund_amount == ZERO
        assert decision.economics.fight_advantage > ZERO
        assert package.fact_ids and package.file_hashes and not package.conflicts
        assert not package.file_hashes & dispute.rejected_file_hashes
        if dispute.rejected_fact_ids:
            assert package.fact_ids - dispute.rejected_fact_ids
    if decision.action is Action.ACCEPT:
        assert decision.needs_approval is (dispute.amount > FEES_V1.auto_accept_limit)
        assert decision.accept_reason is not None
    if decision.action in (Action.HAND_OFF, Action.GATHER_EVIDENCE):
        assert not decision.needs_approval
    if decision.action is Action.GATHER_EVIDENCE:
        assert package is None
    if decision.action is Action.ACCEPT or decision.recommended is Action.ACCEPT:
        assert decision.refund_amount == (dispute.amount if stage is Stage.RFI else ZERO)
        if decision.refund_amount:
            assert refund_problem(dispute, decision.refund_amount, dispute.currency) is None


# ----------------------------------------------------------------------------- real sandbox responses
RECORDED_DISPUTE = {  # GET /pa/payment_disputes/{id} after the simulated issuer rejected the first challenge
    "id": "dst_sgpvfc6s8hnpdllyz1y",
    "stage": "CHARGEBACK",
    "status": "REQUIRES_RESPONSE",
    "amount": 1249,
    "currency": "USD",
    "payment_intent_id": "int_sgpvb7qw4hnpdl9vi5b",
    "reason": {"original_code": "10.4", "description": "Other Fraud-Card Absent Environment", "type": "FRAUDULENT"},
    "challenge_details": [{"stage": "RFI", "reason": "PURCHASE_HISTORY", "challenged_by": "disputable-spike"}],
    "due_at": "2026-11-13T20:51:30+00:00",
}
RECORDED_PAYMENT = {  # GET /pa/payment_intents/{id}. Read with a chargeback open too: it leaves both figures alone
    "id": "int_sgpvb7qw4hnpdl9vi5b",
    "amount": 1249,
    "currency": "USD",
    "status": "SUCCEEDED",
    "captured_amount": 1249,
    "latest_payment_attempt": {"captured_amount": 1249, "refunded_amount": 0},
}
RECORDED_EAR_TIPS = {  # a first-round response: fixtures/spike/017
    "id": "dst_sgpvfc6s8hnpdljxs0s",
    "stage": "RFI",
    "status": "REQUIRES_RESPONSE",
    "amount": 12,
    "currency": "USD",
    "payment_intent_id": "int_sgpvzhfh9hnpdkrj23r",
    "reason": {"original_code": "13.1", "description": "Merchandise/ Services Not Received",
               "type": "PRODUCT_NOT_RECEIVED"},
    "accept_details": [],
    "challenge_details": [],
    "due_at": "2026-11-13T20:51:14+00:00",
}
RECORDED_EAR_TIPS_PAYMENT = {  # fixtures/spike/003
    "id": "int_sgpvzhfh9hnpdkrj23r",
    "amount": 12,
    "currency": "USD",
    "status": "SUCCEEDED",
    "captured_amount": 12,
    "latest_payment_attempt": {"captured_amount": 12, "refunded_amount": 0},
}
RECORDED_EAR_TIPS_PAYMENT_REFUNDED = {  # the same payment, read after the accept's refund went through
    **RECORDED_EAR_TIPS_PAYMENT,
    "latest_payment_attempt": {"captured_amount": 12, "refunded_amount": 12},
}


def test_recorded_responses_become_a_dispute():
    dispute = dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT, **REJECTED)
    assert dispute.stage is Stage.CHARGEBACK
    assert dispute.status is Status.REQUIRES_RESPONSE
    assert dispute.amount == money(1249)
    assert dispute.captured_amount == money(1249)
    assert dispute.refunded_amount == ZERO
    assert dispute.reason_code == "10.4"
    assert dispute.prior_challenges == 1
    assert not dispute.ai_automation_challenged
    assert dispute.due_at == datetime(2026, 11, 13, 20, 51, 30, tzinfo=timezone.utc)


def test_the_dispute_id_is_the_disputes_own():
    assert dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT).id == "dst_sgpvfc6s8hnpdllyz1y"


def test_the_disputed_amount_comes_from_the_dispute_not_the_payment():
    partial = {**RECORDED_DISPUTE, "stage": "RFI", "challenge_details": [], "amount": 100}
    dispute = dispute_from_api(partial, RECORDED_PAYMENT)
    assert dispute.amount == money(100)
    assert dispute.captured_amount == money(1249)
    assert decide(dispute, NO_PROOF, NOW).refund_amount == money(100)


def test_a_dispute_in_another_currency_goes_to_a_person_after_parsing():
    dispute = dispute_from_api({**RECORDED_DISPUTE, "currency": "EUR"}, RECORDED_PAYMENT, **REJECTED)
    assert dispute.currency == "EUR"
    assert decide(dispute, WARRANTY_PACKAGE, NOW).action is Action.HAND_OFF


def test_cents_survive_parsing():
    payment = {"id": "int_x", "captured_amount": 189.99, "latest_payment_attempt": {"refunded_amount": 0.1}}
    dispute = dispute_from_api({**RECORDED_DISPUTE, "amount": 189.99}, payment)
    assert dispute.amount == Decimal("189.99")
    assert dispute.captured_amount == Decimal("189.99")
    assert dispute.refunded_amount == Decimal("0.1")


def test_recorded_responses_reach_ending_1():
    dispute = dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT, **REJECTED)
    assert decide(dispute, WARRANTY_PACKAGE, NOW).action is Action.CHALLENGE


def test_recorded_responses_with_no_local_history_go_to_a_person():
    dispute = dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT)
    assert decide(dispute, WARRANTY_PACKAGE, NOW).action is Action.HAND_OFF


def test_the_banks_rejection_reaches_both_novelty_gates_after_parsing():
    dispute = dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT, **REJECTED)
    assert dispute.rejected_fact_ids == PRIOR_ORDERS
    assert dispute.rejected_file_hashes == PRIOR_PDF
    assert decide(dispute, OLD_FACTS_NEW_FILE, NOW).action is Action.ACCEPT
    assert decide(dispute, NEW_FACT_OLD_FILE, NOW).action is Action.HAND_OFF


def test_the_rejected_sets_must_be_named():
    with pytest.raises(TypeError):
        dispute_from_api(RECORDED_DISPUTE, RECORDED_PAYMENT, PRIOR_PDF, PRIOR_ORDERS)


def test_first_round_response_is_auto_accepted_end_to_end():
    dispute = dispute_from_api(RECORDED_EAR_TIPS, RECORDED_EAR_TIPS_PAYMENT)
    assert dispute.id == "dst_sgpvfc6s8hnpdljxs0s"
    assert dispute.stage is Stage.RFI
    assert dispute.prior_challenges == 0
    assert not dispute.ai_automation_challenged
    decision = decide(dispute, None, NOW)
    assert decision.action is Action.ACCEPT
    assert not decision.needs_approval
    assert decision.refund_amount == money(12)


def test_a_refund_the_payment_already_shows_goes_to_a_person():
    dispute = dispute_from_api(RECORDED_EAR_TIPS, RECORDED_EAR_TIPS_PAYMENT_REFUNDED)
    assert dispute.refunded_amount == money(12)
    decision = decide(dispute, None, NOW)
    assert decision.action is Action.HAND_OFF
    assert "already refunded" in decision.reasons[0]
    assert "past" in refund_problem(dispute, money(12), "USD")


@pytest.mark.parametrize("change, payment", [
    ({}, {**RECORDED_EAR_TIPS_PAYMENT, "latest_payment_attempt": {"captured_amount": 12}}),
    ({}, {"id": "int_x", "captured_amount": 12}),
    ({"refunds": [{"id": "rfd_x"}]}, RECORDED_EAR_TIPS_PAYMENT),
])
def test_an_unsettled_refund_figure_goes_to_a_person(change, payment):
    dispute = dispute_from_api({**RECORDED_EAR_TIPS, **change}, payment)
    decision = decide(dispute, None, NOW)
    assert decision.action is Action.HAND_OFF
    assert "isn't clear" in decision.reasons[0]
    assert "isn't clear" in refund_problem(dispute, money(12), "USD")


@pytest.mark.parametrize("payment", [RECORDED_EAR_TIPS_PAYMENT, RECORDED_EAR_TIPS_PAYMENT_REFUNDED])
def test_accepted_ear_tips_need_nothing_more_whichever_payment_read_is_used(payment):
    accepted = {**RECORDED_EAR_TIPS, "status": "ACCEPTED", "refunds": [{"id": "rfd_sgpvb7qw4hnpdlp670a_krj23r"}]}
    dispute = dispute_from_api(accepted, payment)
    assert decide(dispute, None, NOW).action is Action.NO_ACTION
    assert "ACCEPTED" in refund_problem(dispute, money(12), "USD")


@pytest.mark.parametrize("refunds", [None, []])
def test_a_dispute_with_no_refund_listed_is_fine(refunds):
    # Live reads show null until a refund exists, then a list of ids.
    dispute = dispute_from_api({**RECORDED_EAR_TIPS, "refunds": refunds}, RECORDED_EAR_TIPS_PAYMENT)
    assert dispute.refunded_amount == ZERO


def test_a_due_date_in_the_format_the_simulator_uses_parses():
    dispute = dispute_from_api({**RECORDED_DISPUTE, "due_at": "2026-11-13T20:51:14.000+00:00"}, RECORDED_PAYMENT)
    assert dispute.due_at == datetime(2026, 11, 13, 20, 51, 14, tzinfo=timezone.utc)


def test_a_challenge_by_ai_dispute_automation_is_noticed():
    # The docs' shape: the sandbox has never reported challenge_method on a challenge.
    details = [{"stage": "CHARGEBACK", "challenge_method": "AI_DISPUTE_AUTOMATION"}]
    dispute = dispute_from_api({**RECORDED_DISPUTE, "challenge_details": details}, RECORDED_PAYMENT)
    assert dispute.ai_automation_challenged


@pytest.mark.parametrize("field, value", [("stage", "SOMETHING_NEW"), ("status", "SOMETHING_NEW")])
def test_values_this_code_has_never_seen_go_to_a_person(field, value):
    dispute = dispute_from_api({**RECORDED_DISPUTE, field: value}, RECORDED_PAYMENT, **REJECTED)
    assert decide(dispute, WARRANTY_PACKAGE, NOW).action is Action.HAND_OFF


@pytest.mark.parametrize("wire", ["RFI", "PRE_CHARGEBACK", "CHARGEBACK", "PRE_ARBITRATION", "ARBITRATION"])
def test_every_documented_stage_is_known(wire):
    assert Stage(wire).name == wire


@pytest.mark.parametrize("wire", ["REQUIRES_RESPONSE", "CHALLENGED", "ACCEPTED", "REVERSED", "WON", "LOST",
                                  "PENDING_CLOSURE", "EXPIRED", "PENDING_DECISION"])
def test_every_documented_status_is_known(wire):
    assert Status(wire).name == wire


def test_a_missing_payment_capture_goes_to_a_person():
    dispute = dispute_from_api(RECORDED_DISPUTE, {"id": "int_x"}, **REJECTED)
    decision = decide(dispute, WARRANTY_PACKAGE, NOW)
    assert decision.action is Action.HAND_OFF
    assert "captured" in decision.reasons[0]
