"""Code's evidence rules, with no model: what code alone concludes on every case whose records get read.

Code alone is evaluate(store, case, []): no reader and no reported orders. It reaches 77 of the 83 keys. Four of the other
six need what only reading adds, and two keys were built only to reach engine gates that the pipeline never reaches.
"""

from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cases import READ, STORES, code_alone
from disputable import ce3, documents, rules
from disputable.documents import CANT_SHOW, NO_SIGNATURE, Document, purchase_history, warranty_registration
from disputable.engine import Action, Evidence, Stage, decide
from disputable.fees import FEES_V1, ZERO, ChallengeReason, EvidenceClass, money
from disputable.lifecycle import Phase, as_case, keep, phase
from disputable.rules import NoRules, check_table, customer_emails, evaluate, return_turned_down
from disputable.seed.fernhill import (ANSWERS, ENDING_2, FERNHILL, FIRST_PACKAGE, FIRST_PACKAGE_FACTS, FIRST_PACKAGE_FILE,
                                      MONDAY, SECOND_PACKAGE, SECOND_PACKAGE_FILE, THREE_WEEKS_LATER, WARRANTY_SIGN_UP)
from disputable.seed.suite import ANSWERS as SUITE_ANSWERS, OPENED_AT, SUITE
from disputable.store import Case, Customer, Email, Item, Order, Scan, Store, WarrantyRegistration
from sandbox import Story
from test_seed import WARRANTY_CASES

NONE, CE3, POSSESSION, UNPRICED = (EvidenceClass.NONE, EvidenceClass.CE3_QUALIFIED,
                                   EvidenceClass.INDEPENDENT_POSSESSION, EvidenceClass.UNPRICED_PROOF)
ACCEPT, CHALLENGE, HAND_OFF = Action.ACCEPT, Action.CHALLENGE, Action.HAND_OFF
KINDS ={Customer: "customers", Order: "orders", Scan: "scans", Email: "emails", WarrantyRegistration: "warranty",
         Case: "cases"}

# The design's sentences, word for word (merged §2.4), so a wording change shows here first.
R1 = "The bank says the item came back, but the carrier has no return scan."
R2 = "The carrier has the return on its way, but no scan of it reaching the store."
R4 = "The warehouse logged the return, but the carrier has no scan of it reaching the store."
R7 = "The warehouse turned the return down, so the store may not owe a credit."
R8 = "The warehouse logged the return with a status this code doesn't know."
D1 = "The carrier delivered to a different address from the one on the order."
D2 = "The records don't show where the parcel was meant to go or where it went."
D3 = "The records show a return for this order. A person checks whether the parcel reached the customer."  # any source
F1 = "The order passed 3DS, and the fee table has no chance for that."
W1 = "The warranty sign-up used another email and no SMS check, so it can't be tied to the cardholder."
W2 = "The warranty sign-up had no SMS check, so it can't be tied to the cardholder."
W3 = "The warranty sign-up's text went to the cardholder's phone, but it used an email that isn't on file."
# Phones are compared digit for digit, so W4, W5 and W10 say only that the numbers don't match (review 2, fixes-2).
W4 = ("The warranty sign-up's text went to a number that doesn't match the cardholder's phone on the order, so it can't be "
      "tied to them.")
W5 = ("The warranty sign-up used another email, and its text went to a number that doesn't match the cardholder's phone on "
      "the order, so it can't be tied to them.")
W6 = ("The serial was registered for warranty before the carrier delivered the order, so the sign-up can't show who "
      "received it.")
W7 = "The carrier has no delivery scan for the order, so the warranty sign-up can't be placed after delivery."
# Blank values get their own true lines (review 1, rules-3): no email is not "another email", and a text or a cardholder
# phone with no digits, or only zeros, is no number to compare, not "a number that doesn't match".
W8 = "The warranty sign-up has no email and no SMS check, so it can't be tied to the cardholder."
W9 = "The warranty sign-up's text went to the cardholder's phone, but it has no email."
W10 = ("The warranty sign-up has no email, and its text went to a number that doesn't match the cardholder's phone on the "
       "order, so it can't be tied to them.")
W11 = ("The warranty sign-up's text, or the cardholder's phone on the order, shows no number, so the sign-up can't be "
       "tied to the cardholder.")
W12 = ("The warranty sign-up used another email, and its text, or the cardholder's phone on the order, shows no number, "
       "so it can't be tied to them.")
W13 = ("The warranty sign-up has no email, and its text, or the cardholder's phone on the order, shows no number, so it "
       "can't be tied to them.")
P0 = "The disputed order has no serial number, so no warranty sign-up can show who has it."
P1 = "No warranty sign-up is on file for the serial on the disputed order."


def p5(sign_up_id: str) -> str:
    return (f"Warranty sign-up {sign_up_id} is tied to the cardholder and came from a device and an IP no order in the "
            "history used.")


def r3(day: str) -> str:
    return f"The carrier says the return arrived on {day}, but the warehouse has no scan of it."


def r5(days: str, opened: str = "chargeback") -> str:
    return f"The return arrived {days} before the {opened}, inside Visa's 15-day wait."


def r6(opened: str = "chargeback") -> str:
    return f"The return arrived after the {opened} opened, so Visa's 15-day wait wasn't met."


def test_every_sentence_has_the_designs_words_under_a_name():
    assert {"R1": rules.RETURN_UNSCANNED, "R2": rules.RETURN_ON_ITS_WAY, "R4": rules.RETURN_NOT_DELIVERED,
            "R7": rules.RETURN_TURNED_DOWN, "R8": rules.RETURN_STATUS_UNKNOWN, "D1": rules.DELIVERED_ELSEWHERE,
            "D2": rules.PLACE_UNKNOWN, "D3": rules.PARCEL_RETURNED, "F1": rules.PASSED_3DS, "W1": rules.OTHER_EMAIL_NO_SMS,
            "W2": rules.NO_SMS, "W3": rules.EMAIL_NOT_ON_FILE, "W4": rules.OTHER_PHONE, "W5": rules.OTHER_EMAIL_AND_PHONE,
            "W6": rules.REGISTERED_BEFORE_DELIVERY, "W7": rules.NO_DELIVERY_TO_COMPARE, "P0": rules.NO_SERIAL,
            "P1": rules.NO_SIGN_UP} == {"R1": R1, "R2": R2, "R4": R4, "R7": R7, "R8": R8, "D1": D1, "D2": D2, "D3": D3,
                                        "F1": F1, "W1": W1, "W2": W2, "W3": W3, "W4": W4, "W5": W5, "W6": W6, "W7": W7,
                                        "P0": P0, "P1": P1}
    assert rules.UNTIED == {("other", "none"): W1, ("on_file", "none"): W2, ("other", "yes"): W3,
                            ("on_file", "other"): W4, ("other", "other"): W5, ("none", "none"): W8, ("none", "yes"): W9,
                            ("none", "other"): W10, ("on_file", "unknown"): W11, ("other", "unknown"): W12,
                            ("none", "unknown"): W13}
    every = {(email, text) for email in ("on_file", "other", "none") for text in ("none", "yes", "other", "unknown")}
    assert set(rules.UNTIED) == every - {("on_file", "yes")}  # every state but the tie has its line
    evening = datetime(2026, 10, 5, 20, 0, tzinfo=timezone(timedelta(hours=-8)))  # Oct 6 in UTC: days are UTC days
    assert rules.return_not_logged(evening) == r3("Oct 6")
    assert (rules.inside_the_wait(1, "dispute"), rules.after_it_opened("chargeback")) == (r5("1 day", "dispute"), r6())
    assert (rules.LABEL, rules.WAIT_DAYS) == ("Claude found: ", 15)


# ----------------------------------------------------------------------------- the cases whose records get read
# READ (by label: the story's three, Ending 2 and the suite's 79) and code_alone() live in cases.py, shared with the
# scripted and walk tests.
def run(store: Store, case_id: str, now: datetime = MONDAY, reported=()):
    """Code alone on a case of a changed store."""
    conclusion = evaluate(store, store.case(case_id), list(reported))
    return conclusion, decide(store.dispute(case_id, now), conclusion.evidence, now)


def outcome(decision) -> tuple[Action, Action | None]:
    return decision.action, decision.recommended


def edited(store: Store, *records: object, drop: tuple[str, ...] = ()) -> Store:
    """The store with these records in place of the ones that share their ids, new ones added, and drop's ids gone."""
    changes = {}
    for kind, name in KINDS.items():
        mine = {record.id: record for record in records if type(record) is kind}
        kept = tuple(mine.pop(record.id, record) for record in getattr(store, name) if record.id not in drop)
        changes[name] = (*kept, *mine.values())
    return replace(store, **changes)


def changed(store: Store, record_id: str, **fields: object):
    """A record of the store with some fields changed."""
    return replace(store.record(record_id), **fields)


# ----------------------------------------------------------------------------- the walk: code alone against every key
EXPECTED_CODE_ONLY = {  # what only reading adds: code alone gives this, and a reader brings the key
    "ce3_bank_reports_one_of_three": (HAND_OFF, CHALLENGE),  # ce3 sees the comment name #3899, untagged
    "ce3_bank_reports_the_only_good_pair": (HAND_OFF, CHALLENGE),  # and #3908
    "ce3_customer_claims_a_fraud_report": (CHALLENGE, None),  # only a reader sees the customer's email (K1)
    "credit_customer_says_withdrawn": (HAND_OFF, ACCEPT),  # only a reader sees the withdrawal (K2)
}
GATE_ONLY = {  # keys built to reach an engine gate: the pipeline always builds the file and never sends a rejected one
    "ce3_package_has_no_file": (CHALLENGE, None),
    "round_2_old_file_again": (CHALLENGE, None),
}


def test_the_walk_covers_the_suite_the_story_and_ending_2():
    assert len(READ) == 83
    assert {"earbuds", "headphones", "headphones_round_2", "ending_2"} <= set(READ)
    assert "ear_tips" not in READ  # the first call accepts it, so nothing is read
    assert sum(read.store_name == "suite" for read in READ.values()) == 79


@pytest.mark.parametrize("label", READ)
def test_every_read_case_reaches_its_key_with_code_alone_but_six(label):
    key = (READ[label].answer.final, READ[label].answer.recommended)
    expected = {**EXPECTED_CODE_ONLY, **GATE_ONLY}.get(label, key)
    assert outcome(code_alone(label)[1]) == expected
    assert (expected == key) is (label not in {**EXPECTED_CODE_ONLY, **GATE_ONLY})


def test_code_alone_matches_77_of_the_83_keys():
    misses = {label for label, read in READ.items()
              if outcome(code_alone(label)[1]) != (read.answer.final, read.answer.recommended)}
    assert misses == set(EXPECTED_CODE_ONLY) | set(GATE_ONLY)
    assert len(READ) - len(misses) == 77


def test_the_six_cases_code_alone_cant_reach_give_what_the_design_says():
    for label, number in (("ce3_bank_reports_one_of_three", "3899"), ("ce3_bank_reports_the_only_good_pair", "3908")):
        conclusion, decision = code_alone(label)
        assert conclusion.evidence.evidence_class is CE3
        assert decision.reasons[0] == (f"The bank's comment names order #{number}, an earlier order on this card, but it "
                                       "isn't flagged as reported.")
    conclusion, decision = code_alone("ce3_customer_claims_a_fraud_report")
    assert conclusion.evidence.conflicts == () and decision.challenge_reason is ChallengeReason.PURCHASE_HISTORY
    conclusion, decision = code_alone("credit_customer_says_withdrawn")
    assert conclusion.evidence.evidence_class is NONE and decision.reasons[0] == R1
    # The gates: the file is always built, so the package is complete...
    case = SUITE.case("ce3_package_has_no_file")
    conclusion, decision = code_alone("ce3_package_has_no_file")
    assert conclusion.evidence.file_hashes == {purchase_history(SUITE, case, ce3.check(SUITE, case)).sha256}
    assert decision.challenge_reason is ChallengeReason.PURCHASE_HISTORY
    # ...and the bank's rejected file never goes again: the new sign-up's file goes alone, and the fight pays.
    case = SUITE.case("round_2_old_file_again")
    conclusion, decision = code_alone("round_2_old_file_again")
    assert "old-package-file" in case.rejected_file_hashes and "old-package-file" not in conclusion.evidence.file_hashes
    assert conclusion.evidence.file_hashes == {warranty_registration(SUITE, case, "warranty-33").sha256}
    assert decision.economics.fight_advantage == Decimal("206.50")


CODES_OWN_WORDS = {  # the key's wording is illustrative; code writes its own line, with the same decision
    "credit_return_not_in_the_warehouse": r3("Oct 8"),  # the date added, as the earbuds key has it
    "credit_no_return_scan": R1,  # the bank's word, not the customer's
    "credit_wrong_item_returned": R7,  # the warehouse's note reaches Priya only as Claude's quote
    "credit_customer_says_withdrawn": R1,  # the withdrawal itself needs a reader (K2)
}
NEEDS_A_READER = {"ce3_customer_claims_a_fraud_report"}  # K1, so code alone has no line


@pytest.mark.parametrize("label", [label for label, read in READ.items() if read.answer.conflicts])
def test_key_conflicts_appear_word_for_word_but_the_listed_ones(label):
    conclusion, decision = code_alone(label)
    for conflict in READ[label].answer.conflicts:
        if label in CODES_OWN_WORDS:
            assert conflict not in decision.reasons and decision.reasons[0] == CODES_OWN_WORDS[label]
        elif label in NEEDS_A_READER:
            assert conclusion.evidence.conflicts == ()
        else:
            assert conflict in decision.reasons


def test_twenty_of_the_25_keyed_conflicts_are_codes_own_words():
    keyed = [(label, conflict) for label, read in READ.items() for conflict in read.answer.conflicts]
    exact = [label for label, conflict in keyed if conflict in code_alone(label)[1].reasons]
    assert (len(keyed), len(exact)) == (25, 20)
    assert {label for label, _ in keyed} - set(exact) == set(CODES_OWN_WORDS) | NEEDS_A_READER


# ----------------------------------------------------------------------------- the story
def test_the_story_packages_keep_their_pinned_hashes_and_facts():
    first = evaluate(FERNHILL, FERNHILL.case("headphones"), [])
    assert first.evidence == Evidence(CE3, FIRST_PACKAGE_FACTS, frozenset({FIRST_PACKAGE_FILE}))
    assert first.files == (FIRST_PACKAGE,)
    for reported in ([], ["1038"]):
        second = evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), reported)
        assert second.evidence == Evidence(POSSESSION, frozenset({WARRANTY_SIGN_UP.id}), frozenset({SECOND_PACKAGE_FILE}))
        assert second.files == (SECOND_PACKAGE,)
        ending_2 = evaluate(ENDING_2, ENDING_2.case("headphones_round_2"), reported)
        assert ending_2.evidence == Evidence(NONE) and ending_2.files == ()
    assert (FIRST_PACKAGE_FILE[:8], SECOND_PACKAGE_FILE[:8]) == ("64631fdc", "2d8e9d0a")  # test_documents pins them whole


@pytest.mark.parametrize("store, second", [
    (FERNHILL, Evidence(POSSESSION, frozenset({WARRANTY_SIGN_UP.id}), frozenset({SECOND_PACKAGE_FILE}))),
    (ENDING_2, Evidence(NONE)),
], ids=["ending 1", "ending 2"])
def test_as_case_reads_the_story_as_the_seed_cases_do(store, second):
    # In the app the case is lifecycle.as_case(record): round two counts the sent challenge as the bank's rejection.
    story = Story(store, cases=("headphones",))
    story.poll(MONDAY)
    first = evaluate(store, as_case(story["headphones"]), [])
    assert first.evidence == Evidence(CE3, FIRST_PACKAGE_FACTS, frozenset({FIRST_PACKAGE_FILE}))
    story.run(MONDAY)
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    case = as_case(story["headphones"])
    assert ce3.later_round(case) and case.rejected_file_hashes == {FIRST_PACKAGE_FILE}
    for reported in ([], ["1038"]):
        assert evaluate(store, case, reported).evidence == second


def test_the_earbuds_conflict_is_the_storys_sentence_and_needs_no_reader():
    conclusion, decision = code_alone("earbuds")
    assert ANSWERS["earbuds"].conflicts == (r3("Oct 5"),)
    assert conclusion.evidence == Evidence(NONE, conflicts=ANSWERS["earbuds"].conflicts)
    assert decision.reasons[0] == r3("Oct 5")
    assert (decision.action, decision.recommended, decision.refund_amount) == (HAND_OFF, ACCEPT, ZERO)
    assert conclusion.findings == ("The carrier delivered the return to the store on Oct 5, 17 days before the chargeback.",)


# ----------------------------------------------------------------------------- 10.4: CE 3.0 in round one
def test_round_one_fact_ids_are_ce3s_plus_the_delivery_scan_the_file_proves():
    packages = {label: code_alone(label)[0] for label in READ if code_alone(label)[0].evidence.evidence_class is CE3}
    assert len(packages) == 28
    for label, conclusion in packages.items():
        read, check = READ[label], conclusion.check
        delivery = documents.delivery_scan(read.store, read.store.order(read.case.order_number))
        assert conclusion.evidence.fact_ids == check.fact_ids | {delivery.id}, label
        assert conclusion.files == (purchase_history(read.store, read.case, check),), label
        assert conclusion.evidence.file_hashes == {conclusion.files[0].sha256}, label


def test_round_one_never_reads_or_sends_a_warranty_sign_up(monkeypatch):
    # Monday's headphones: the sign-up is already on file, but CE 3.0 is all round one sends, so round two has it new.
    assert FERNHILL.get_warranty_log(WARRANTY_SIGN_UP.serial) == (WARRANTY_SIGN_UP,)
    monkeypatch.setattr(Store, "get_warranty_log", lambda self, serial: pytest.fail("the warranty log was read at RFI"))
    conclusion = evaluate(FERNHILL, FERNHILL.case("headphones"), [])
    assert WARRANTY_SIGN_UP.id not in conclusion.evidence.fact_ids
    assert [file.title for file in conclusion.files] == ["Order #1109: earlier purchases on the same card"]
    assert conclusion.findings == conclusion.check.reasons


def test_fraud_findings_start_with_ce3s_reasons_when_it_ran():
    for label in READ:
        conclusion = code_alone(label)[0]
        if conclusion.check is not None:
            assert conclusion.findings[:len(conclusion.check.reasons)] == conclusion.check.reasons, label
    chargeback = code_alone("ce3_at_chargeback_first_round")[0]
    assert chargeback.findings == (*chargeback.check.reasons, P0)


def test_ce3_runs_only_when_consulted(monkeypatch):
    for label, read in READ.items():
        assert (code_alone(label)[0].check is not None) is ce3.consulted(read.case, Action.GATHER_EVIDENCE, []), label
    calls = []
    check = ce3.check
    monkeypatch.setattr(ce3, "check", lambda store, case, reported=(): calls.append(case.id) or check(store, case, reported))
    evaluate(SUITE, SUITE.case("credit_wrong_item_returned"), ["3529"])  # a credit claim: tags change nothing
    evaluate(SUITE, SUITE.case("round_2_new_warranty_proof"), [])  # a later round with nothing reported
    assert calls == []
    later = evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), ["1038"])  # reported: the check runs, for findings
    assert calls == ["headphones_round_2"] and later.check.outcome is ce3.Outcome.FAIL
    assert later.findings == (*later.check.reasons, p5(WARRANTY_SIGN_UP.id))


def test_reported_reaches_ce3_as_a_list(monkeypatch):
    seen = []
    check, consulted = ce3.check, ce3.consulted
    monkeypatch.setattr(ce3, "consulted", lambda case, first_call, reported=(): seen.append(("consulted", first_call,
                                                                                             reported))
                        or consulted(case, first_call, reported))
    monkeypatch.setattr(ce3, "check", lambda store, case, reported=(): seen.append(("check", reported))
                        or check(store, case, reported))
    evaluate(FERNHILL, FERNHILL.case("headphones"))
    evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), ("1038",))
    assert seen == [("consulted", Action.GATHER_EVIDENCE, []), ("check", []),
                    ("consulted", Action.GATHER_EVIDENCE, ["1038"]), ("check", ["1038"])]
    assert all(type(call[-1]) is list for call in seen)
    with pytest.raises(TypeError, match="one string would be read a character at a time"):
        evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), "1038")
    with pytest.raises(TypeError, match="one string"):
        evaluate(SUITE, SUITE.case("credit_wrong_item_returned"), "3529")  # whatever the claim
    for wrong in ({"1038"}, None):
        with pytest.raises(TypeError, match="a list or tuple"):
            evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), wrong)


def test_ce3_conflicts_come_first():
    # An unclear CE 3.0 on a 3DS order whose file a person must write: ce3's line, then the file's, then 3DS.
    store = edited(SUITE, changed(SUITE, "order-3210", three_ds=True, cardholder_name="Łukasz Sample"))
    conclusion, decision = run(store, "ce3_prior_other_dispute")
    assert conclusion.check.conflicts == SUITE_ANSWERS["ce3_prior_other_dispute"].conflicts
    assert conclusion.evidence.conflicts == (*conclusion.check.conflicts, CANT_SHOW, F1)
    assert decision.reasons[:3] == conclusion.evidence.conflicts


# ----------------------------------------------------------------------------- 10.4: the warranty sign-up at CHARGEBACK
def test_round_two_package_holds_only_the_sign_up():
    packages = {label: code_alone(label)[0] for label in READ
                if code_alone(label)[0].evidence.evidence_class is POSSESSION}
    assert set(packages) == {"headphones_round_2", "round_2_new_warranty_proof", "round_2_proof_not_worth_it",
                             "round_2_old_file_again", "round_2_no_record_here"}
    for label, conclusion in packages.items():
        read = READ[label]
        sign_up_id, = conclusion.evidence.fact_ids
        assert isinstance(read.store.record(sign_up_id), WarrantyRegistration)
        assert conclusion.files == (warranty_registration(read.store, read.case, sign_up_id),)
        assert conclusion.evidence.file_hashes == {conclusion.files[0].sha256}
        assert conclusion.findings[-1] == p5(sign_up_id)


UNTIED_LINES = {"round_2_warranty_other_email": W3, "round_2_warranty_other_phone": W4, "round_2_warranty_no_sms": W2,
                "round_2_warranty_unverified": W1}


@pytest.mark.parametrize("case_id, conditions", WARRANTY_CASES)
def test_each_warranty_condition_alone(case_id, conditions):
    conclusion, decision = code_alone(case_id)
    tied, independent = all(conditions[:2]), all(conditions[2:])
    if not tied:
        assert conclusion.evidence == Evidence(NONE, conflicts=(UNTIED_LINES[case_id],))
        assert outcome(decision) == (HAND_OFF, ACCEPT)
    elif not independent:  # tied, but not proof: no conflict, just a finding
        assert conclusion.evidence == Evidence(NONE)
        assert conclusion.findings[-1].endswith("so it isn't independent proof.")
        assert decision.action is ACCEPT
    else:
        assert conclusion.evidence.evidence_class is POSSESSION and conclusion.evidence.conflicts == ()


@pytest.mark.parametrize("email, phone, line", [
    ("someone.else.34@example.net", None, W1),
    (None, None, W2),
    ("someone.else.34@example.net", "keep", W3),
    (None, "+1 4155550100", W4),
    ("someone.else.34@example.net", "+1 4155550100", W5),
])
def test_an_untied_sign_up_goes_to_a_person_even_from_the_disputed_device(email, phone, line):
    # warranty-34 came from the disputed order's own device and IP. Untied, it's a person's call, not "not proof".
    entry = SUITE.record("warranty-34")
    store = edited(SUITE, replace(entry, email=email or entry.email,
                                  sms_verified_phone=entry.sms_verified_phone if phone == "keep" else phone))
    conclusion, decision = run(store, "round_2_warranty_on_the_disputed_device")
    assert conclusion.evidence == Evidence(NONE, conflicts=(line,))
    assert conclusion.findings == ()  # independence is never weighed for it
    assert outcome(decision) == (HAND_OFF, ACCEPT)


@pytest.mark.parametrize("email, phone, cardholder, line", [
    ("", None, "keep", W8),
    ("N/A", "keep", "keep", W9),
    ("", "+1 4155550100", "keep", W10),
    ("keep", "verified", "keep", W11),
    ("keep", "keep", "", W11),
    ("keep", "verified", "", W11),
    ("someone.else.34@example.net", "verified", "keep", W12),
    ("", "verified", "", W13),
], ids=["no email, no SMS", "no email, the cardholder's phone", "no email, another phone", "a text with no number",
        "no cardholder phone", "neither shows a number", "another email, no number", "no email, no number"])
def test_a_blank_email_or_phone_gets_its_own_true_line(email, phone, cardholder, line):
    # Two blank phones are never the same phone, and no email isn't "another email".
    entry, case = SUITE.record("warranty-34"), SUITE.case("round_2_warranty_on_the_disputed_device")
    order = SUITE.order(case.order_number)
    store = edited(SUITE, replace(entry, email=entry.email if email == "keep" else email,
                                  sms_verified_phone=entry.sms_verified_phone if phone == "keep" else phone),
                   replace(order, cardholder_phone=order.cardholder_phone if cardholder == "keep" else cardholder))
    conclusion, decision = run(store, case.id)
    assert conclusion.evidence == Evidence(NONE, conflicts=(line,))
    assert conclusion.findings == () and outcome(decision) == (HAND_OFF, ACCEPT)


def test_phones_are_compared_digit_for_digit_and_zeros_are_no_number():
    entry, case = SUITE.record("warranty-34"), SUITE.case("round_2_warranty_on_the_disputed_device")
    order = SUITE.order(case.order_number)
    assert documents.digits(entry.sms_verified_phone) == documents.digits(order.cardholder_phone)  # tied as seeded
    national = order.cardholder_phone.split(" ", 1)[1]  # the same number without its country code
    for cardholder, sms, line in ((national, entry.sms_verified_phone, W4),  # it doesn't match, and W4 says only that
                                  ("000-000-0000", entry.sms_verified_phone, W11),  # a placeholder is no number
                                  ("000-000-0000", "000-000-0000", W11),  # two placeholders are never the same phone
                                  ("+1 0000000000", "+1 000-000-0000", W11),  # nor with a country code, as stored
                                  ("０００-０００-００００", "０００-０００-００００", W11),  # nor in fullwidth digits
                                  (order.cardholder_phone, "00000", W11)):  # ("0" alone says nothing: no SMS check)
        store = edited(SUITE, replace(entry, sms_verified_phone=sms), replace(order, cardholder_phone=cardholder))
        conclusion, decision = run(store, case.id)
        assert conclusion.evidence == Evidence(NONE, conflicts=(line,)), (cardholder, sms)
        assert outcome(decision) == (HAND_OFF, ACCEPT)


def owned_by(email):
    """The suite with warranty-30's customer, account and orders, at this email."""
    owner = SUITE.order(SUITE.case("round_2_new_warranty_proof").order_number).customer_id
    moved = [replace(order, email=email) for order in SUITE.orders if order.customer_id == owner]
    return edited(SUITE, replace(SUITE.customer(owner), email=email), *moved)


def test_a_lookalike_of_the_customers_email_is_not_on_file():
    # casefold() would read "straße" as "strasse": a separate domain someone else can register. Only the case of ASCII
    # letters is forgiven.
    entry, case = SUITE.record("warranty-30"), SUITE.case("round_2_new_warranty_proof")
    store = owned_by("Kai@Strasse-Audio.example")
    assert rules.mailbox(" Kai@Strasse-Audio.example ") == "kai@strasse-audio.example"
    for lookalike in ("kai@straße-audio.example", "kai@strasσe-audio.example", "ｋａｉ@strasse-audio.example"):
        assert rules.mailbox(lookalike) not in ("", "kai@strasse-audio.example"), lookalike
    assert rules.mailbox("N/A") == rules.mailbox("") == rules.mailbox(None) == ""
    conclusion, decision = run(edited(store, replace(entry, email="kai@straße-audio.example")), case.id)
    assert conclusion.evidence == Evidence(NONE, conflicts=(W3,)) and outcome(decision) == (HAND_OFF, ACCEPT)
    conclusion, decision = run(edited(store, replace(entry, email="KAI@strasse-audio.example")), case.id)
    assert conclusion.evidence.fact_ids == {"warranty-30"} and decision.action is CHALLENGE  # ASCII case is forgiven


def test_a_customers_own_non_ascii_email_matches_itself_and_nothing_like_it():
    entry, case = SUITE.record("warranty-30"), SUITE.case("round_2_new_warranty_proof")
    store = owned_by("jörg@müller-audio.example")  # composed (NFC)
    decomposed = "jörg@müller-audio.example"  # the same address, decomposed (NFD): one address
    for email in ("jörg@müller-audio.example", decomposed, "JöRG@MüLLER-AUDIO.EXAMPLE"):  # ASCII case is forgiven
        conclusion, decision = run(edited(store, replace(entry, email=email)), case.id)
        assert conclusion.evidence.fact_ids == {"warranty-30"} and decision.action is CHALLENGE, email
    # The other way round: the customer is at straße, and the ASCII "strasse" is another address.
    store = owned_by("kai@straße-audio.example")
    conclusion, decision = run(edited(store, replace(entry, email="kai@strasse-audio.example")), case.id)
    assert conclusion.evidence == Evidence(NONE, conflicts=(W3,)) and outcome(decision) == (HAND_OFF, ACCEPT)
    conclusion, _ = run(edited(store, replace(entry, email="KAI@straße-audio.example")), case.id)
    assert conclusion.evidence.fact_ids == {"warranty-30"}


def test_a_sign_up_the_bank_turned_down_is_never_new(monkeypatch):
    monkeypatch.setattr(documents, "warranty_registration", lambda *args: pytest.fail("built the rejected sign-up's file"))
    case = SUITE.case("round_2_new_warranty_proof")
    store = edited(SUITE, replace(case, rejected_fact_ids=case.rejected_fact_ids | {"warranty-30"}))
    conclusion, decision = run(store, "round_2_new_warranty_proof")
    assert conclusion.evidence == Evidence(NONE)
    assert conclusion.findings == ("Warranty sign-up warranty-30 was in the package the bank turned down, so it isn't new.",)
    assert decision.action is ACCEPT


def test_a_sign_up_before_delivery_or_with_no_delivery_goes_to_a_person():
    delivered = SUITE.record("scan-3300-delivered").at  # Sep 2, 10:30 UTC
    for entry, gone, line in (
        (changed(SUITE, "warranty-30", registered_at=delivered - timedelta(days=1)), (), W6),
        (changed(SUITE, "warranty-30", registered_at=delivered.replace(hour=0, minute=5)), (), None),  # that day is after
        (changed(SUITE, "warranty-30", registered_at=datetime(2026, 9, 1, 20, tzinfo=timezone(timedelta(hours=-8)))), (),
         None),  # Sep 2 in UTC, though Sep 1 where it was made
        (SUITE.record("warranty-30"), ("scan-3300-delivered",), W7),
    ):
        conclusion, decision = run(edited(SUITE, entry, drop=gone), "round_2_new_warranty_proof")
        if line:
            assert conclusion.evidence == Evidence(NONE, conflicts=(line,))
            assert outcome(decision) == (HAND_OFF, ACCEPT)
        else:
            assert conclusion.evidence.fact_ids == {"warranty-30"} and conclusion.evidence.conflicts == ()
            assert decision.action is CHALLENGE


def test_a_sign_up_for_another_serial_is_never_a_candidate():
    conclusion, decision = code_alone("round_2_warranty_for_another_serial")
    assert conclusion.evidence == Evidence(NONE) and conclusion.findings == (P1,)
    assert decision.action is ACCEPT
    # Untied, it still raises nothing: it's a sign-up for another order's serial.
    store = edited(SUITE, changed(SUITE, "warranty-35", email="someone.else.35@example.net", sms_verified_phone=None))
    assert evaluate(store, store.case("round_2_warranty_for_another_serial"), []) == conclusion


def test_an_untied_second_sign_up_beside_proof_hands_off_with_the_fight_drafted(monkeypatch):
    proof = SUITE.record("warranty-30")
    other = replace(proof, id="warranty-30-b", registered_at=proof.registered_at - timedelta(days=1),
                    email="someone.else.30@example.net", sms_verified_phone=None)
    conclusion, decision = run(edited(SUITE, other), "round_2_new_warranty_proof")
    assert conclusion.evidence.fact_ids == {"warranty-30"} and conclusion.evidence.conflicts == (W1,)
    assert outcome(decision) == (HAND_OFF, CHALLENGE)
    # If the proof's own file is one a person must write, that line comes after the other sign-up's.
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 1000)
    conclusion, decision = run(edited(SUITE, other), "round_2_new_warranty_proof")
    untied, refusal = conclusion.evidence.conflicts
    assert untied == W1 and refusal.startswith("The file would be") and outcome(decision) == (HAND_OFF, None)


def test_an_untied_sign_up_after_the_proof_still_hands_off():
    # Two people on one serial: every sign-up is weighed, not only those before the first proof.
    proof = SUITE.record("warranty-30")
    later = replace(proof, id="warranty-30-c", registered_at=proof.registered_at + timedelta(days=1),
                    email="someone.else.30@example.net", sms_verified_phone=None)
    conclusion, decision = run(edited(SUITE, later), "round_2_new_warranty_proof")
    assert conclusion.evidence.fact_ids == {"warranty-30"} and conclusion.evidence.conflicts == (W1,)
    assert outcome(decision) == (HAND_OFF, CHALLENGE)


def test_a_sign_up_counts_from_the_latest_delivery():
    # Registered after a first delivery but before the latest one, so it can't show who received the order.
    delivered = SUITE.record("scan-3300-delivered")
    first = replace(delivered, id="scan-3300-delivered-first", at=delivered.at - timedelta(days=7))
    between = changed(SUITE, "warranty-30", registered_at=delivered.at - timedelta(days=3))
    conclusion, decision = run(edited(SUITE, first, between), "round_2_new_warranty_proof")
    assert conclusion.evidence == Evidence(NONE, conflicts=(W6,)) and outcome(decision) == (HAND_OFF, ACCEPT)


@pytest.mark.parametrize("record", [
    changed(SUITE, "warranty-30", device_id=""),
    changed(SUITE, "warranty-30", device_id="00000000-0000-0000-0000-000000000000"),
    changed(SUITE, "warranty-30", ip_address="unknown"),
    changed(SUITE, "warranty-30", ip_address="10.0.0.5"),  # a proxy's address is every buyer's
    changed(SUITE, "order-3300", device_id=""),
    changed(SUITE, "order-3300", ip_address="N/A"),
], ids=["sign-up device blank", "sign-up device zero", "sign-up IP unknown", "sign-up IP a proxy's",
        "order device blank", "order IP blank"])
def test_independence_needs_a_device_and_an_ip_on_the_sign_up_and_the_disputed_order(record):
    conclusion, decision = run(edited(SUITE, record), "round_2_new_warranty_proof")
    assert conclusion.evidence == Evidence(NONE)
    assert conclusion.findings == ("Warranty sign-up warranty-30, or the disputed order, shows no device or IP to compare, "
                                   "so the sign-up isn't proof.",)
    assert decision.action is ACCEPT


def test_an_order_that_shows_no_device_or_ip_never_blocks_a_sign_up():
    disputed = SUITE.order("3300")
    older = replace(disputed, number="3305", placed_at=disputed.placed_at - timedelta(days=90), device_id="",
                    ip_address="", items=(Item("FH-EP-01", "Replacement ear pads", money(89)),), tracking_number=None)
    store = edited(SUITE, older)
    assert older in store.get_order_history("3300")
    conclusion, decision = run(store, "round_2_new_warranty_proof")
    assert conclusion.evidence.fact_ids == {"warranty-30"} and decision.action is CHALLENGE


def test_3ds_adds_its_conflict_and_is_unpriced_only_without_other_proof():
    conclusion, decision = code_alone("fraud_claim_that_passed_3ds")
    assert conclusion.evidence == Evidence(UNPRICED, frozenset({"order-3260"}), conflicts=(F1,))
    assert outcome(decision) == (HAND_OFF, None)
    for case_id, order_id in (("ce3_everything_matches", "order-3100"), ("round_2_new_warranty_proof", "order-3300")):
        with_3ds, decision = run(edited(SUITE, changed(SUITE, order_id, three_ds=True)), case_id)
        assert with_3ds.evidence == replace(code_alone(case_id)[0].evidence, conflicts=(F1,))  # the class is kept
        assert outcome(decision) == (HAND_OFF, CHALLENGE)
    conclusion, decision = run(edited(SUITE, changed(SUITE, "order-3320", three_ds=True)), "round_2_old_facts_only")
    assert conclusion.evidence == Evidence(UNPRICED, frozenset({"order-3320"}), conflicts=(F1,))
    assert outcome(decision) == (HAND_OFF, ACCEPT)  # the bank has seen the order, so it's no new fact


def test_at_chargeback_round_one_a_sign_up_beats_ce3():
    # The table prices possession at CHARGEBACK, not CE 3.0, so the sign-up's package replaces CE 3.0's.
    for store, expected, result in (
        (FERNHILL, Evidence(POSSESSION, frozenset({WARRANTY_SIGN_UP.id}), frozenset({SECOND_PACKAGE_FILE})), CHALLENGE),
        (ENDING_2, Evidence(CE3, FIRST_PACKAGE_FACTS, frozenset({FIRST_PACKAGE_FILE})), HAND_OFF),
    ):
        moved = edited(store, replace(store.case("headphones"), stage=Stage.CHARGEBACK))
        conclusion, decision = run(moved, "headphones")
        assert conclusion.evidence == expected and conclusion.check.outcome is ce3.Outcome.PASS
        assert decision.action is result
    # An unclear CE 3.0's conflicts drop out with its package.
    order = SUITE.order("3210")
    serial = "FH-HP-03-00021"
    sign_up = WarrantyRegistration("warranty-21", serial, SUITE.record("scan-3210-delivered").at + timedelta(days=3),
                                   order.email, order.cardholder_phone, "d0000021-0000-4000-8000-000000000003",
                                   "203.0.113.21")
    case = replace(SUITE.case("ce3_prior_other_dispute"), stage=Stage.CHARGEBACK)
    store = edited(SUITE, replace(order, items=(replace(order.items[0], serial=serial),)), sign_up, case)
    conclusion, decision = run(store, case.id)
    assert conclusion.check.outcome is ce3.Outcome.UNCLEAR and conclusion.check.conflicts
    assert conclusion.evidence.fact_ids == {"warranty-21"} and conclusion.evidence.conflicts == ()
    assert decision.action is CHALLENGE
    conclusion, decision = run(edited(SUITE, case), case.id)  # with no sign-up, CE 3.0 is all there is
    assert conclusion.evidence.conflicts == conclusion.check.conflicts and outcome(decision) == (HAND_OFF, None)


# ----------------------------------------------------------------------------- 13.1, not received
def delivery_of(order_number: str, name: str, when: datetime, **fields: object) -> Scan:
    order = SUITE.order(order_number)
    return Scan(f"scan-{order_number}-{name}", order_number, fields.pop("source", "carrier"),
                fields.pop("direction", "outbound"), when, fields.pop("status", "DELIVERED"),
                fields.pop("location", order.shipping_address), **fields)


AUTO_LIMIT = SUITE.record("scan-3440-delivered")  # not_received_at_the_auto_limit: $50, unsigned at the address
ELSEWHERE = "44 Example Road, Oakland, CA 94607, US"


@pytest.mark.parametrize("case_id, records, gone, expected, decided", [
    ("not_received_no_delivery_scan", (), (), Evidence(NONE), (ACCEPT, None)),
    ("not_received_delivered_elsewhere", (), (), Evidence(NONE, conflicts=(D1,)), (HAND_OFF, ACCEPT)),
    ("not_received_signed_delivery", (), (), Evidence(UNPRICED, frozenset({"scan-3420-delivered"})), (HAND_OFF, None)),
    ("not_received_at_the_auto_limit", (), (), Evidence(NONE), (ACCEPT, None)),
    ("not_received_at_the_auto_limit", (replace(AUTO_LIMIT, location=""),), (), Evidence(NONE, conflicts=(D2,)),
     (HAND_OFF, ACCEPT)),
    ("not_received_at_the_auto_limit", (changed(SUITE, "order-3440", shipping_address="N/A"),), (),
     Evidence(NONE, conflicts=(D2,)), (HAND_OFF, ACCEPT)),
    ("not_received_at_the_auto_limit",
     (delivery_of("3440", "return", AUTO_LIMIT.at + timedelta(days=3), direction="return", status="PICKED_UP"),), (),
     Evidence(NONE, conflicts=(D3,)), (HAND_OFF, ACCEPT)),
    ("not_received_no_delivery_scan",
     (delivery_of("3400", "return", OPENED_AT - timedelta(days=5), direction="return", status="PICKED_UP"),), (),
     Evidence(NONE, conflicts=(D3,)), (HAND_OFF, ACCEPT)),
    ("not_received_at_the_auto_limit",
     (delivery_of("3440", "first", AUTO_LIMIT.at - timedelta(days=2), location=ELSEWHERE),
      delivery_of("3440", "second", AUTO_LIMIT.at - timedelta(days=1), location="")), (),
     Evidence(NONE, conflicts=(D1, D2)), (HAND_OFF, ACCEPT)),
    ("not_received_signed_delivery",
     (delivery_of("3420", "again", SUITE.record("scan-3420-delivered").at + timedelta(days=1)),), (),
     Evidence(NONE), (ACCEPT, None)),
    ("not_received_at_the_auto_limit",  # only the warehouse logged a return: D3 names no carrier
     (delivery_of("3440", "wh-return", AUTO_LIMIT.at + timedelta(days=5), source="warehouse", direction="return",
                  status="RETURN_RECEIVED", location="Fernhill warehouse"),), (),
     Evidence(NONE, conflicts=(D3,)), (HAND_OFF, ACCEPT)),
    ("not_received_at_the_auto_limit",  # a blank place never equals a blank address, signed or not
     (changed(SUITE, "order-3440", shipping_address=""), replace(AUTO_LIMIT, location="", signed_by="R. Lee")), (),
     Evidence(NONE, conflicts=(D2,)), (HAND_OFF, ACCEPT)),
], ids=["no delivery scan", "elsewhere", "signed at the address", "unsigned at the address", "blank place",
        "no address on the order", "a return scan", "a return and no delivery", "elsewhere and a blank place",
        "signed, then a later delivery unsigned", "a warehouse return only", "signed to a blank place, no address"])
def test_not_received_rules(case_id, records, gone, expected, decided):
    conclusion, decision = run(edited(SUITE, *records, drop=gone), case_id)
    assert conclusion.evidence == expected
    assert conclusion.files == () and conclusion.check is None  # no 13.1 file exists
    assert outcome(decision) == decided


def test_a_misdelivery_then_a_signed_delivery_is_unpriced_proof_with_its_conflict():
    signed = SUITE.record("scan-3420-delivered")
    misdelivered = replace(signed, id="scan-3420-misdelivered", at=signed.at - timedelta(days=1),
                           location="42 Example Road, Oakland, CA 94607, US", signed_by=None)
    conclusion, decision = run(edited(SUITE, misdelivered), "not_received_signed_delivery")
    assert conclusion.evidence == Evidence(UNPRICED, frozenset({signed.id}), conflicts=(D1,))
    assert outcome(decision) == (HAND_OFF, None)
    # The other way round, the latest delivery went elsewhere, and the earlier signature proves nothing.
    conclusion, decision = run(edited(SUITE, replace(misdelivered, at=signed.at + timedelta(days=1))),
                               "not_received_signed_delivery")
    assert conclusion.evidence == Evidence(NONE, conflicts=(D1,))
    assert outcome(decision) == (HAND_OFF, ACCEPT)


@pytest.mark.parametrize("signer", [*sorted(NO_SIGNATURE), "Front Door / Porch", "LEFT AT FRONT DOOR.", "N/A", "", None])
def test_a_carriers_words_for_nobody_are_never_a_signature(signer):
    store = edited(SUITE, changed(SUITE, "scan-3420-delivered", signed_by=signer))
    conclusion, decision = run(store, "not_received_signed_delivery")
    assert conclusion.evidence == Evidence(NONE) and decision.action is ACCEPT


def test_a_role_someone_signs_as_is_a_signature():
    store = edited(SUITE, changed(SUITE, "scan-3420-delivered", signed_by="DOCK"))
    assert run(store, "not_received_signed_delivery")[0].evidence.evidence_class is UNPRICED


def test_a_note_that_claims_a_signature_isnt_one():
    scan = SUITE.record("scan-3650-delivered")
    assert scan.signed_by is None and "signed" in scan.note  # the trap
    conclusion, decision = code_alone("trick_carrier_note")
    assert conclusion.evidence == Evidence(NONE) and decision.action is ACCEPT
    noted = edited(SUITE, changed(SUITE, "scan-3420-delivered", signed_by=None, note="Signed for by the cardholder."))
    assert run(noted, "not_received_signed_delivery")[0].evidence == Evidence(NONE)


# ----------------------------------------------------------------------------- 13.6, credit not processed
@pytest.mark.parametrize("label", [label for label, read in READ.items() if read.case.reason_code == "13.6"])
def test_a_credit_claim_is_a_plain_accept_only_when_carrier_and_warehouse_show_the_return_past_15_days(label):
    read = READ[label]
    back = [scan for scan in read.store.get_carrier_scans(read.case.order_number) if scan.direction == "return"]
    arrived = [scan for scan in back if scan.source == "carrier" and scan.status == "DELIVERED"]
    logged = [scan for scan in back if scan.source == "warehouse" and scan.status == "RETURN_RECEIVED"]
    plain = bool(arrived and logged) and (read.case.opened_at.date() - arrived[-1].at.date()).days > 15
    conclusion, decision = code_alone(label)
    assert (conclusion.evidence == Evidence(NONE)) is plain
    assert (decision.action is ACCEPT) is plain


LOGGED = "credit_warehouse_logged_the_return"  # order 3460: the return came Oct 8, logged received Oct 10


@pytest.mark.parametrize("arrived, line", [
    (datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc), r5("15 days")),  # day 15 goes to a person
    (datetime(2026, 10, 12, 23, 59, tzinfo=timezone.utc), None),  # day 16, though it's under 16 x 24 hours
    (datetime(2026, 10, 12, 20, 0, tzinfo=timezone(timedelta(hours=-8))), r5("15 days")),  # Oct 13 in UTC
    (OPENED_AT - timedelta(days=1), r5("1 day")),
    (OPENED_AT, r5("0 days")),
    (OPENED_AT + timedelta(minutes=30), r6()),  # the same day, but after the chargeback opened
    (OPENED_AT + timedelta(days=2), r6()),
], ids=["day 15", "day 16", "day 15 in UTC", "one day", "the moment it opened", "later that day", "two days after"])
def test_the_15_day_wait_counts_utc_calendar_days_to_the_first_rounds_opened_date(arrived, line):
    store = edited(SUITE, changed(SUITE, "scan-3460-return-delivered", at=arrived),
                   changed(SUITE, "scan-3460-warehouse", at=arrived + timedelta(days=2)))
    conclusion, decision = run(store, LOGGED)
    assert conclusion.evidence == Evidence(NONE, conflicts=(line,) if line else ())
    assert outcome(decision) == ((HAND_OFF, ACCEPT) if line else (ACCEPT, None))


def test_a_later_round_counts_the_days_from_the_first_rounds_date_and_says_dispute():
    # An RFI nobody answered, then the chargeback: as_case() makes it a later round, still dated from the RFI.
    rfi = replace(SUITE.case("credit_inside_the_15_day_wait"), id="credit_inside_the_wait_at_rfi", stage=Stage.RFI)
    store = edited(SUITE, rfi)
    story = Story(store, cases=(rfi.id,))
    story.poll(MONDAY)
    later = MONDAY + timedelta(days=11)
    story.sandbox.escalate(f"dst_{rfi.id}", case_id="credit_inside_the_15_day_wait", now=later)
    story.poll(later)
    case = as_case(story[rfi.id])
    assert case.stage is Stage.CHARGEBACK and ce3.later_round(case) and case.opened_at == OPENED_AT
    conclusion = evaluate(store, case, [])
    assert conclusion.evidence == Evidence(NONE, conflicts=(r5("9 days", "dispute"),))
    assert conclusion.findings[0] == "The carrier delivered the return to the store on Oct 19, 9 days before the dispute."
    assert code_alone("credit_inside_the_15_day_wait")[0].evidence.conflicts == (r5("9 days"),)  # round one's word


@pytest.mark.parametrize("case_id, records, gone, conflicts", [
    ("credit_no_return_scan", (), (), (R1,)),
    (LOGGED, (), ("scan-3460-return-delivered", "scan-3460-warehouse"), (R2,)),
    (LOGGED, (), ("scan-3460-warehouse",), (r3("Oct 8"),)),
    (LOGGED, (), ("scan-3460-return-delivered",), (R4,)),
    ("credit_inside_the_15_day_wait", (), (), (r5("9 days"),)),
    (LOGGED, (changed(SUITE, "scan-3460-return-delivered", at=OPENED_AT + timedelta(days=1)),), (), (r6(),)),
    ("credit_wrong_item_returned", (), (), (R7,)),
    (LOGGED, (changed(SUITE, "scan-3460-warehouse", status="RETURN_INSPECTING"),), (), (R8,)),
    ("credit_wrong_item_returned", (), ("scan-3530-return-delivered",), (R7, R4)),
    ("credit_wrong_item_returned", (changed(SUITE, "scan-3530-return-delivered", at=OPENED_AT - timedelta(days=3)),), (),
     (R7, r5("3 days"))),
    (LOGGED, (changed(SUITE, "scan-3460-warehouse", status="RETURN_INSPECTING"),
              changed(SUITE, "scan-3460-return-delivered", at=OPENED_AT - timedelta(days=4))), (), (R8, r5("4 days"))),
    (LOGGED, (replace(SUITE.record("scan-3460-return-delivered"), id="scan-3460-return-delivered-early",
                      at=OPENED_AT - timedelta(days=30)),  # the wait counts from the latest return delivery
              changed(SUITE, "scan-3460-return-delivered", at=OPENED_AT - timedelta(days=9)),
              changed(SUITE, "scan-3460-warehouse", at=OPENED_AT - timedelta(days=8))), (), (r5("9 days"),)),
], ids=["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R7 and R4", "R7 and R5", "R8 and R5", "R5 from the latest"])
def test_each_credit_conflict_sentence(case_id, records, gone, conflicts):
    conclusion, decision = run(edited(SUITE, *records, drop=gone), case_id)
    assert conclusion.evidence.conflicts == conflicts
    assert outcome(decision) == ((HAND_OFF, None) if R7 in conflicts else (HAND_OFF, ACCEPT))


def test_the_latest_return_delivery_dates_the_finding_too():
    store = edited(SUITE, replace(SUITE.record("scan-3460-return-delivered"), id="scan-3460-return-delivered-early",
                                  at=OPENED_AT - timedelta(days=30)),
                   changed(SUITE, "scan-3460-return-delivered", at=OPENED_AT - timedelta(days=9)),
                   changed(SUITE, "scan-3460-warehouse", at=OPENED_AT - timedelta(days=8)))
    conclusion, _ = run(store, LOGGED)
    assert conclusion.findings[0] == "The carrier delivered the return to the store on Oct 19, 9 days before the chargeback."


def test_a_return_on_its_way_is_not_reported_as_no_return_scan():
    conclusion, _ = run(edited(SUITE, drop=("scan-3460-return-delivered", "scan-3460-warehouse")), LOGGED)
    assert conclusion.evidence.conflicts == (R2,)
    assert conclusion.findings == ()  # the carrier has a return scan: it's on its way


def test_a_return_inside_the_wait_with_no_warehouse_scan_gives_both_lines():
    conclusion, decision = run(edited(SUITE, drop=("scan-3510-warehouse",)), "credit_inside_the_15_day_wait")
    assert conclusion.evidence == Evidence(NONE, conflicts=(r3("Oct 19"), r5("9 days")))
    assert decision.reasons[:2] == (r3("Oct 19"), r5("9 days"))


def test_a_turned_down_return_is_unpriced_proof_with_its_scan():
    conclusion, decision = code_alone("credit_wrong_item_returned")
    assert conclusion.evidence == Evidence(UNPRICED, frozenset({"scan-3530-warehouse"}), conflicts=(R7,))
    assert outcome(decision) == (HAND_OFF, None)  # nothing drafted: no table prices the proof
    assert return_turned_down(SUITE, SUITE.case("credit_wrong_item_returned"))
    assert not return_turned_down(SUITE, SUITE.case(LOGGED))
    assert not return_turned_down(SUITE, SUITE.case("credit_no_return_scan"))
    # Only the warehouse's latest scan counts, in either order.
    rejected = SUITE.record("scan-3530-warehouse")
    received = replace(rejected, id="scan-3530-warehouse-again", at=rejected.at + timedelta(days=1),
                       status="RETURN_RECEIVED", note="")
    store = edited(SUITE, received)
    assert not return_turned_down(store, store.case("credit_wrong_item_returned"))
    assert run(store, "credit_wrong_item_returned")[0].evidence == Evidence(NONE)
    store = edited(SUITE, replace(received, at=rejected.at - timedelta(days=1)))
    assert return_turned_down(store, store.case("credit_wrong_item_returned"))


# ----------------------------------------------------------------------------- findings
@pytest.mark.parametrize("label, findings", [
    ("not_received_no_delivery_scan", ("The carrier has no delivery scan for order #3400.",)),
    ("not_received_at_the_auto_limit",
     ("The carrier delivered order #3440 on Sep 22 to the address on the order, with no signature.",)),
    ("not_received_signed_delivery",
     ("The carrier delivered order #3420 on Oct 2 to the address on the order, and someone signed for it.",)),
    ("not_received_delivered_elsewhere", ()),
    (LOGGED, ("The carrier delivered the return to the store on Oct 8, 20 days before the chargeback.",
              "The warehouse logged the return as received on Oct 10.")),
    ("credit_wrong_item_returned", ("The carrier delivered the return to the store on Oct 8, 20 days before the chargeback.",
                                    "The warehouse turned the return down on Oct 10.")),
    ("credit_no_return_scan", ("The carrier has no return scan for order #3520.",)),
    ("round_2_on_a_credit_claim", ("The carrier delivered the return to the store on Oct 8, 20 days before the dispute.",
                                   "The warehouse logged the return as received on Oct 10.")),
    ("round_2_old_facts_only", (P1,)),
    ("round_2_warranty_on_the_disputed_device",
     ("Warranty sign-up warranty-34 came from a device or an IP that order #3340 used, so it isn't independent proof.",)),
    ("headphones_round_2", (p5("warranty-55821"),)),
    ("ending_2", (P1,)),
])
def test_findings_say_what_code_saw_in_its_own_words(label, findings):
    assert code_alone(label)[0].findings == findings


def test_a_return_after_the_dispute_opened_is_dated_without_a_count():
    store = edited(SUITE, changed(SUITE, "scan-3460-return-delivered", at=OPENED_AT + timedelta(days=1)))
    assert run(store, LOGGED)[0].findings[0] == ("The carrier delivered the return to the store on Oct 29, after the "
                                                 "chargeback opened.")


def test_the_files_are_exactly_those_behind_the_hashes():
    for label, read in READ.items():
        conclusion = code_alone(label)[0]
        assert {file.sha256 for file in conclusion.files} == conclusion.evidence.file_hashes, label
        assert all(file.fact_ids <= conclusion.evidence.fact_ids for file in conclusion.files), label
        if read.case.reason_code != "10.4":
            assert conclusion.files == () and conclusion.check is None, label


# ----------------------------------------------------------------------------- files a person writes, and bugs
def test_a_file_a_person_must_write_goes_to_a_person_with_nothing_drafted(monkeypatch):
    # A character the PDF can't show, in the cardholder's name the first file shows.
    store = edited(FERNHILL, changed(FERNHILL, "order-1109", cardholder_name="Łukasz Sample"))
    conclusion, decision = run(store, "headphones")
    assert conclusion.evidence == Evidence(CE3, conclusion.check.fact_ids, conflicts=(CANT_SHOW,))
    assert conclusion.files == ()
    assert outcome(decision) == (HAND_OFF, None) and decision.reasons[0] == CANT_SHOW
    story = Story(store, cases=("headphones",))
    story.poll(MONDAY)
    record = story["headphones"]
    kept = keep(record, evaluate(store, as_case(record), []).evidence, MONDAY, round_number=1)
    assert phase(kept) is Phase.PERSON and kept.rounds[-1].plan is None
    # A file over Visa's size limit is caught when the package is made, not at upload.
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 1000)
    for case_id, now, facts in (("headphones", MONDAY, None), ("headphones_round_2", THREE_WEEKS_LATER, {"warranty-55821"})):
        conclusion, decision = run(FERNHILL, case_id, now)
        refusal, = conclusion.evidence.conflicts
        assert re.fullmatch(r"The file would be (more than )?[\d,]+ bytes, over the 1,000 Visa takes\.", refusal)
        assert conclusion.evidence.file_hashes == frozenset() and conclusion.files == ()
        assert conclusion.evidence.fact_ids == (facts or conclusion.check.fact_ids)
        assert outcome(decision) == (HAND_OFF, None)


def test_each_file_is_laid_out_once_when_its_package_is_made(monkeypatch):
    laid_out = []
    pdf = Document.pdf
    monkeypatch.setattr(Document, "pdf", lambda self: laid_out.append(self.title) or pdf(self))
    evaluate(FERNHILL, FERNHILL.case("headphones"), [])
    evaluate(FERNHILL, FERNHILL.case("headphones_round_2"), [])
    evaluate(FERNHILL, FERNHILL.case("earbuds"), [])
    assert laid_out == [FIRST_PACKAGE.title, SECOND_PACKAGE.title]


def test_a_file_that_isnt_due_is_a_bug_not_a_conflict(monkeypatch):
    def not_due(*args):
        raise documents.NoFileDue("No file of this kind is due.")

    monkeypatch.setattr(documents, "purchase_history", not_due)
    monkeypatch.setattr(documents, "warranty_registration", not_due)
    for case_id in ("headphones", "headphones_round_2"):
        with pytest.raises(documents.NoFileDue):
            evaluate(FERNHILL, FERNHILL.case(case_id), [])


# ----------------------------------------------------------------------------- no record text, and the same in any order
RUN = 12  # characters: a run this long of a record's words in a reason would be a copy
FREE_TEXT = {Email: ("from_addr", "to_addr", "subject", "body"), Scan: ("note", "tracking_number"),
             WarrantyRegistration: ("note",), Case: ("issuer_comment",)}  # words no rule compares
COMPARED = {Customer: ("name", "email", "phone", "billing_address"),
            Order: ("cardholder_name", "cardholder_phone", "email", "shipping_address", "prior_dispute"),
            Scan: ("status", "location", "signed_by"),
            WarrantyRegistration: ("email", "sms_verified_phone", "device_id", "ip_address")}  # values rules compare


def runs(text: str) -> set[str]:
    return {text[start:start + RUN] for start in range(len(text) - RUN + 1)}


def records_of(store: Store):
    return (record for name in KINDS.values() for record in getattr(store, name))


def canary(*parts: str) -> str:
    """Letters unique to one field: no sentence of code's can hold 12 of them in a row unless it copied them."""
    return "".join(chr(ord("a") + int(digit, 16)) for digit in hashlib.sha256("/".join(parts).encode()).hexdigest())


@functools.cache
def canaried(store_name: str) -> Store:
    def swapped(record):
        return replace(record, **{name: canary(record.id, name) for name in FREE_TEXT.get(type(record), ())})
    store = STORES[store_name]
    return replace(store, **{name: tuple(map(swapped, getattr(store, name))) for name in KINDS.values()})


@functools.cache
def record_runs(store_name: str, canaries: bool) -> frozenset[str]:
    store, fields = (canaried(store_name), FREE_TEXT) if canaries else (STORES[store_name], COMPARED)
    return frozenset().union(*(runs(getattr(record, name) or "") for record in records_of(store)
                               for name in fields.get(type(record), ())))


@pytest.mark.parametrize("label", READ)
def test_codes_sentences_and_findings_hold_no_text_a_customer_carrier_or_bank_wrote(label):
    # Values the rules compare (addresses, signers, emails, phones) are checked as they are. Free text is replaced by
    # canaries first: code writes "the cardholder" and "independent proof" itself, and so do some records.
    read = READ[label]
    conclusion, decision = code_alone(label)
    assert not set().union(*map(runs, (*decision.reasons, *conclusion.findings))) & record_runs(read.store_name, False)
    store = canaried(read.store_name)
    conclusion, decision = run(store, read.case_id, read.now)
    assert not set().union(*map(runs, (*decision.reasons, *conclusion.findings))) & record_runs(read.store_name, True)


def shuffled(store: Store, how: str) -> Store:
    def moved(records: tuple) -> tuple:
        return records[::-1] if how == "reversed" else records[len(records) // 2:] + records[:len(records) // 2]
    return replace(store, **{name: moved(getattr(store, name)) for name in KINDS.values()})


@pytest.mark.parametrize("how", ["reversed", "rotated"])
def test_the_same_records_give_the_same_conclusion_in_any_order(how):
    stores = {name: shuffled(store, how) for name, store in STORES.items()}
    assert all(stores[name].scans != store.scans for name, store in STORES.items())
    for label, read in READ.items():
        store = stores[read.store_name]
        assert evaluate(store, store.case(read.case_id), []) == code_alone(label)[0], label
    for store_name, case_id, reported in (("fernhill", "headphones_round_2", ["1038"]),
                                          ("suite", "ce3_bank_reports_one_of_three", ["3899"])):
        store = stores[store_name]
        assert (evaluate(store, store.case(case_id), reported)
                == evaluate(STORES[store_name], STORES[store_name].case(case_id), reported))


def test_a_tie_in_time_is_broken_by_id_so_the_order_records_come_in_cant_matter():
    # The seed has no two such records at the same moment, so these make some. "Latest" is the greatest (UTC time, id).
    signed, proof, rejected = (SUITE.record(record_id) for record_id in ("scan-3420-delivered", "warranty-30",
                                                                        "scan-3530-warehouse"))
    store = edited(SUITE, replace(signed, id="scan-3420-delivered-b", location=ELSEWHERE, signed_by=None),
                   replace(proof, id="warranty-30-b", device_id="d0000030-0000-4000-8000-000000000009",
                           ip_address="203.0.113.130"),
                   replace(rejected, id="scan-3530-warehouse-b", status="RETURN_RECEIVED", note=""))
    for moved in (store, shuffled(store, "reversed"), shuffled(store, "rotated")):
        assert run(moved, "not_received_signed_delivery")[0].evidence == Evidence(NONE, conflicts=(D1,))
        assert run(moved, "round_2_new_warranty_proof")[0].evidence.fact_ids == {"warranty-30"}  # both qualify
        assert run(moved, "credit_wrong_item_returned")[0].evidence == Evidence(NONE)


# ----------------------------------------------------------------------------- the table, other claims and stages
def test_a_table_that_prices_unpriced_proof_is_refused():
    check_table(FEES_V1)
    for field, value in (("rfi_escalation_chance", Decimal("0.5")), ("chargeback_win_chance", Decimal("0.5")),
                         ("challenge_reasons", ChallengeReason.NOT_ENTITLED)):
        priced = replace(FEES_V1, **{field: {**getattr(FEES_V1, field), UNPRICED: value}})
        with pytest.raises(ValueError, match=f"^fees-v1 prices UNPRICED_PROOF in {field},"):
            check_table(priced)


@pytest.mark.parametrize("reason_code", ["12.6", "10.5", "13.2", ""])
def test_other_claims_have_no_rules(reason_code):
    case = replace(SUITE.case("not_received_at_the_auto_limit"), reason_code=reason_code)
    with pytest.raises(NoRules, match=f"^Code has no evidence rules for claim {reason_code or 'none'}.$"):
        evaluate(SUITE, case, [])
    with pytest.raises(NoRules):
        evaluate(SUITE, SUITE.case("unknown_reason_code"), [])
    assert issubclass(NoRules, ValueError)


@pytest.mark.parametrize("stage", [stage for stage in Stage if stage not in (Stage.RFI, Stage.CHARGEBACK)])
def test_other_stages_have_no_rules(stage):
    for case_id in ("not_received_at_the_auto_limit", "credit_wrong_item_returned", "ce3_everything_matches"):
        with pytest.raises(NoRules, match=f"^Code has no evidence rules for the {stage.value} stage.$"):
            evaluate(SUITE, replace(SUITE.case(case_id), stage=stage), [])
    with pytest.raises(NoRules):
        evaluate(SUITE, SUITE.case("pre_arbitration_stage"), [])


# ----------------------------------------------------------------------------- the customer's emails
def test_customer_emails_are_the_accounts_and_the_customers_own_orders():
    assert customer_emails(FERNHILL, FERNHILL.order("1109")) == {"alex.sample@example.com"}
    store = edited(FERNHILL, changed(FERNHILL, "order-1077", email="Alex.Sample+Old@Example.com"),
                   changed(FERNHILL, "order-1038", email="N/A"))
    assert customer_emails(store, store.order("1109")) == {"alex.sample@example.com", "alex.sample+old@example.com"}
    # The priors on this card are another customer's, so their emails aren't the disputed order's customer's.
    disputed = SUITE.order("3880")
    others = {order.email for order in SUITE.get_order_history("3880") if order.customer_id != disputed.customer_id}
    assert others and customer_emails(SUITE, disputed) == {SUITE.customer(disputed.customer_id).email}
    # The account's own email counts even when no order uses it.
    customer = FERNHILL.customer(FERNHILL.order("1109").customer_id)
    moved = edited(FERNHILL, replace(customer, email="Alex.New@Example.com"))
    assert customer_emails(moved, moved.order("1109")) == {"alex.new@example.com", "alex.sample@example.com"}
