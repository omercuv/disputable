"""The CE 3.0 check: every label in both answer keys, each rule on its own, and what a person reads."""

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import combinations, product

import pytest

from disputable.ce3 import ELEMENTS, NEWEST, OLDEST, READINGS, Outcome, check, consulted, evidence
from disputable.engine import Action, Evidence, Stage, decide
from disputable.fees import EvidenceClass, money
from disputable.seed.fernhill import ANSWERS, ENDING_2, ENDING_2_ANSWER, FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.seed.suite import ANSWERS as SUITE_ANSWERS, SUITE
from disputable.store import Case, Customer, Item, Order, Refund, Store

PASS, FAIL, UNCLEAR = Outcome.PASS, Outcome.FAIL, Outcome.UNCLEAR

# ----------------------------------------------------------------------------- every label in both answer keys
EVERY_CASE = ([("fernhill", FERNHILL, case_id, answer) for case_id, answer in ANSWERS.items()]
              + [("ending_2", ENDING_2, "headphones_round_2", ENDING_2_ANSWER)]
              + [("suite", SUITE, case_id, answer) for case_id, answer in SUITE_ANSWERS.items()])
LABELLED = [item for item in EVERY_CASE if item[3].ce3 is not None]
IDS = [f"{name}:{case_id}" for name, _, case_id, _ in LABELLED]
# Proof besides CE 3.0 settles these: a warranty record, a customer's claim the store can't check, or 3DS.
OTHER_PROOF = {("fernhill", "headphones_round_2"), ("suite", "ce3_customer_claims_a_fraud_report"),
               ("suite", "fraud_claim_that_passed_3ds")}


def checked(store, case_id, answer):
    return check(store, store.case(case_id), answer.reported_orders)


def now_for(case_id):
    return THREE_WEEKS_LATER if case_id == "headphones_round_2" else MONDAY


@pytest.mark.parametrize("name, store, case_id, answer", LABELLED, ids=IDS)
def test_the_check_reaches_every_label(name, store, case_id, answer):
    assert checked(store, case_id, answer).outcome.value == answer.ce3


@pytest.mark.parametrize("name, store, case_id, answer", LABELLED, ids=IDS)
def test_an_unclear_answer_carries_the_checks_own_words(name, store, case_id, answer):
    result = checked(store, case_id, answer)
    assert bool(result.conflicts) is (result.outcome is UNCLEAR)
    assert set(result.conflicts) <= set(answer.conflicts)
    if answer.ce3 == "unclear":
        assert result.conflicts == answer.conflicts


@pytest.mark.parametrize("name, store, case_id, answer", LABELLED, ids=IDS)
def test_a_ce3_package_cites_what_the_check_cites(name, store, case_id, answer):
    result = checked(store, case_id, answer)
    if answer.evidence_class is EvidenceClass.CE3_QUALIFIED:
        assert result.fact_ids <= answer.fact_ids
        # The headphones also cite the signed delivery: the PDF step's fact, not CE 3.0's.
        assert answer.fact_ids - result.fact_ids == ({"scan-1109-delivered"} if case_id == "headphones" else set())


@pytest.mark.parametrize("name, store, case_id, answer", LABELLED, ids=IDS)
def test_the_check_confirms_the_reports_the_answer_expects(name, store, case_id, answer):
    assert checked(store, case_id, answer).reported == answer.reported_orders


@pytest.mark.parametrize("name, store, case_id, answer", LABELLED, ids=IDS)
def test_the_engine_reaches_each_answer_from_the_check_alone(name, store, case_id, answer):
    result = checked(store, case_id, answer)
    if (name, case_id) in OTHER_PROOF:
        assert (answer.evidence_class not in (EvidenceClass.CE3_QUALIFIED, EvidenceClass.NONE)
                or set(answer.conflicts) != set(result.conflicts))
        return
    dispute = store.dispute(case_id, now_for(case_id))
    decision = decide(dispute, evidence(result, dispute, answer.file_hashes), now_for(case_id))
    assert (decision.action, decision.recommended) == (answer.final, answer.recommended)
    for conflict in answer.conflicts:
        assert conflict in decision.reasons


@pytest.mark.parametrize("name, store, case_id, answer", EVERY_CASE, ids=[f"{n}:{c}" for n, _, c, _ in EVERY_CASE])
def test_the_check_is_consulted_exactly_where_a_case_has_a_label(name, store, case_id, answer):
    dispute = store.dispute(case_id, now_for(case_id))
    first_call = decide(dispute, None, now_for(case_id)).action
    assert consulted(dispute, first_call, answer.reported_orders) is (answer.ce3 is not None)


@pytest.mark.parametrize("hours", [-12, -8, 5.5, 14])
def test_the_labels_hold_in_any_time_zone(hours):
    zone = timezone(timedelta(hours=hours))
    for name, store, case_id, answer in LABELLED:
        moved = replace(store, orders=tuple(replace(order, placed_at=order.placed_at.astimezone(zone))
                                            for order in store.orders),
                        cases=tuple(replace(case, opened_at=case.opened_at.astimezone(zone)) for case in store.cases))
        assert checked(moved, case_id, answer) == checked(store, case_id, answer), (name, case_id)


def test_the_check_reads_only_orders_and_the_case():
    for name, store, case_id, answer in LABELLED:
        bare = replace(store, emails=(), scans=(), warranty=())
        assert checked(bare, case_id, answer) == checked(store, case_id, answer), (name, case_id)


# ----------------------------------------------------------------------------- one rule at a time, on made-up stores
OPENED = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
HOLDER = Customer("customer-1", "Casey Sample", "casey.sample@example.com", "+1 4155550101",
                  "1 Sample Street, San Francisco, CA 94103, US")
STRANGER = Customer("customer-2", "Devon Demo", "devon.demo@example.com", "+1 4155550102",
                    "2 Demo Street, San Francisco, CA 94103, US")
USUAL = {"customer_id": HOLDER.id, "items": (Item("FH-HP-03", "Over-ear studio headphones", money(600)),),
         "card_fingerprint": "card-1", "card_last4": "0001", "cardholder_name": HOLDER.name,
         "cardholder_phone": HOLDER.phone, "account_id": "acct-1", "email": HOLDER.email, "device_id": "device-1",
         "ip_address": "198.51.100.1", "shipping_address": HOLDER.billing_address}
OTHER = {"account_id": "acct-2", "shipping_address": "9 Example Road, Oakland, CA 94607, US", "device_id": "device-2",
         "ip_address": "192.0.2.9"}
DISPUTED = "2000"
BANKS_WORDS = "Cardholder does not recognise this card-absent transaction."
FULL_REFUND = (Refund(money(600), OPENED - timedelta(days=190)),)
EVERYTHING = "account, delivery address, device and IP"


def differing(*elements):
    """Order fields that differ from the disputed order's, for the named elements."""
    return {ELEMENTS[element]: OTHER[ELEMENTS[element]] for element in elements}


def store_with(priors, *, gap=30, opened_at=OPENED, comment=BANKS_WORDS, disputed=None, stage=Stage.RFI):
    """A disputed order placed gap days before the dispute, and other orders as (days before the dispute, what differs)."""
    orders = [Order(number=DISPUTED, **{"placed_at": opened_at - timedelta(days=gap), **USUAL, **(disputed or {})})]
    orders += [Order(number=str(1001 + k), **{"placed_at": opened_at - timedelta(days=age), **USUAL, **changes})
               for k, (age, changes) in enumerate(priors)]
    return Store(customers=(HOLDER, STRANGER), orders=tuple(orders),
                 cases=(Case("case-1", DISPUTED, "10.4", stage, opened_at, timedelta(days=10), comment),))


def run(priors, *, reported=(), **options):
    store = store_with(priors, **options)
    return check(store, store.case("case-1"), reported)


def reported_by_the_bank(*numbers):
    return {"reported": numbers, "comment": " ".join(f"Order #{number} has a Visa fraud report (TC40) from May."
                                                     for number in numbers)}


def renumbered(store, old, new):
    """The same store, with order old under the number new."""
    return replace(store, orders=tuple(replace(order, number=new) if order.number == old else order
                                       for order in store.orders))


def window_line(number, dispute, order, decided=""):
    return (f"Order #{number} was placed {dispute} days before the dispute but {order} before the disputed order. Visa "
            f"counts the 120 to 365 days from the dispute, Airwallex's CE 3.0 page from the order.{decided}")


FROM_THE_DISPUTE = " Counted from the dispute, CE 3.0 is met; counted from the order, it isn't."
FROM_THE_ORDER = " Counted from the order, CE 3.0 is met; counted from the dispute, it isn't."
COUNTING_IT = " Counting it, CE 3.0 is met; without it, it isn't."
NON_FRAUD_LINE = ("Order #1001 had a dispute that wasn't for fraud. Visa's FAQ still counts it, but Airwallex's CE 3.0 page "
                  "asks for orders with no dispute.")
REFUND_LINE = ("Order #{} was refunded in full ($600.00), and neither Visa nor Airwallex says whether a refunded order "
               "still counts.")
UNREADABLE_LINE = ("Order #1001 has a dispute of a kind this check can't read. If it wasn't for fraud, Visa's FAQ still "
                   "counts the order, but Airwallex's CE 3.0 page asks for orders with no dispute.")
NOT_A_NUMBER = "Something flagged as reported isn't written as an order number, so the bank's comment can't confirm it."
NOT_MET = ("CE 3.0 isn't met. It needs two earlier orders on this card, placed 120 to 365 days before the dispute with no "
           "fraud on them, each matching the disputed order on device or IP and one more detail.")
TOO_LATE = "it can't be sent: Visa allows one CE 3.0 attempt, and this case is past its first round."
NO_EARLIER_ORDER = "CE 3.0 isn't met: the store's records show no earlier order on this card."


@pytest.mark.parametrize("age, outcome", [(NEWEST - 1, FAIL), (NEWEST, PASS), (OLDEST, PASS), (OLDEST + 1, FAIL)])
def test_the_window_includes_both_ends(age, outcome):
    assert run([(200, {}), (age, {})], gap=0).outcome is outcome


@pytest.mark.parametrize("age, outcome", [
    (125, UNCLEAR),  # 125 days before the dispute, 115 before the disputed order
    (130, PASS),  # 130 and 120
    (365, PASS),  # 365 and 355
    (370, UNCLEAR),  # 370 and 360
    (376, FAIL),  # 376 and 366
])
def test_the_window_is_counted_from_both_dates(age, outcome):
    assert run([(200, {}), (age, {})], gap=10).outcome is outcome


def test_days_are_calendar_days_not_hours():
    opened = datetime(2026, 10, 28, 0, 30, tzinfo=timezone.utc)
    late_evening = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    assert opened - late_evening < timedelta(days=NEWEST)
    result = run([(200, {}), (0, {"placed_at": late_evening})], gap=0, opened_at=opened)
    assert result.outcome is PASS
    assert result.priors[-1].days_before_dispute == NEWEST


def test_days_are_counted_in_utc():
    pacific = timezone(timedelta(hours=-8))
    evening = datetime(2026, 6, 30, 20, 0, tzinfo=pacific)  # July 1 in UTC: 119 days before the dispute, not 120
    result = run([(200, {}), (0, {"placed_at": evening})], gap=0)
    assert result.outcome is FAIL
    assert result.priors[-1].days_before_dispute == NEWEST - 1


@pytest.mark.parametrize("where", ["prior", "disputed order", "dispute"])
def test_a_date_without_a_time_zone_is_refused(where):
    naive = datetime(2026, 5, 1, 12, 0)
    aware = [(0, {"placed_at": OPENED - timedelta(days=age)}) for age in (200, 300)]
    options = {"prior": {"priors": [(200, {}), (0, {"placed_at": naive})]},
               "disputed order": {"priors": aware, "disputed": {"placed_at": naive}},
               "dispute": {"priors": aware, "disputed": {"placed_at": OPENED - timedelta(days=30)}, "opened_at": naive}}
    with pytest.raises(ValueError, match="time zone"):
        run(**options[where])


def test_a_dispute_before_its_own_order_is_refused():
    with pytest.raises(ValueError, match="before order 2000 was placed"):
        run([(200, {}), (300, {})], gap=-1)


def test_only_orders_on_the_same_card_count():
    other_card = {"card_fingerprint": "card-9", "card_last4": "0009"}
    result = run([(200, other_card), (300, other_card)])
    assert result.outcome is FAIL
    assert result.priors == ()
    assert result.reasons == (NO_EARLIER_ORDER,
                              "Orders #1002 and #1001 were paid with another card, so CE 3.0 can't use them.")


def test_the_card_decides_not_the_customer():
    stranger = {"customer_id": STRANGER.id, "account_id": "acct-9", "email": STRANGER.email,
                "shipping_address": STRANGER.billing_address}
    result = run([(200, stranger), (300, stranger)])  # same card, device and IP
    assert result.outcome is PASS
    assert result.reasons[-1] == ("Orders #1002 and #1001 were placed by another customer with this card. CE 3.0 finds "
                                  "earlier orders by card, not by customer.")


@pytest.mark.parametrize("card", ["", "  ", "unknown", "UNKNOWN", "N/A", "null", "-", "(not set)"])
def test_an_order_with_no_card_on_file_has_no_earlier_orders_on_it(card):
    result = run([(200, {"card_fingerprint": card}), (300, {"card_fingerprint": card})], disputed={"card_fingerprint": card})
    assert result.outcome is FAIL
    assert result.priors == ()
    # Nothing about the earlier orders' cards: with no card to compare, no other line could be true.
    assert result.reasons == ("CE 3.0 isn't met: the disputed order has no card on file, so no earlier order can be tied "
                              "to its card.",)


def test_earlier_orders_with_a_card_arent_called_another_card_when_the_disputed_order_has_none():
    result = run([(200, {}), (300, {})], disputed={"card_fingerprint": "unknown"})  # the earlier orders are on card-1
    assert result.reasons == ("CE 3.0 isn't met: the disputed order has no card on file, so no earlier order can be tied "
                              "to its card.",)


@pytest.mark.parametrize("card", ["", "unknown", "N/A"])
def test_an_earlier_order_with_no_card_on_file_isnt_on_this_card(card):
    result = run([(200, {}), (300, {}), (250, {"card_fingerprint": card}), (260, {"card_fingerprint": card})])
    assert [prior.order.number for prior in result.priors] == ["1002", "1001"]
    # One line for them after the two orders' own lines, and not "another card" too: nothing shows their card differs.
    assert result.reasons[3:] == ("Orders #1004 and #1003 have no card on file, so CE 3.0 can't tie them to this card.",)


def test_with_no_card_on_the_earlier_orders_the_summary_claims_only_what_the_records_show():
    result = run([(200, {"card_fingerprint": ""}), (300, {"card_fingerprint": "unknown"})])
    assert result.reasons == (NO_EARLIER_ORDER,
                              "Orders #1002 and #1001 have no card on file, so CE 3.0 can't tie them to this card.")


def test_one_order_reads_in_the_singular():
    result = run([(200, {}), (300, {"customer_id": STRANGER.id}), (250, {"card_fingerprint": "card-9"}),
                  (260, {"card_fingerprint": ""})])
    assert result.reasons[-3:] == (
        "Order #1003 was paid with another card, so CE 3.0 can't use it.",
        "Order #1004 has no card on file, so CE 3.0 can't tie it to this card.",
        "Order #1002 was placed by another customer with this card. CE 3.0 finds earlier orders by card, not by customer.",
    )


def test_spaces_around_a_card_fingerprint_dont_matter():
    result = run([(200, {"card_fingerprint": " card-1 "}), (300, {})])
    assert result.outcome is PASS
    assert not any("another card" in line for line in result.reasons)


def test_the_disputed_order_is_never_its_own_prior():
    result = run([(300, {})], gap=150)  # the disputed order itself is 150 days old
    assert result.outcome is FAIL
    assert [prior.order.number for prior in result.priors] == ["1001"]


def test_an_order_placed_after_the_disputed_one_isnt_a_prior():
    result = run([(300, {}), (125, {})], gap=130)  # 125 days before the dispute, but after the disputed order
    assert result.outcome is FAIL
    assert [prior.order.number for prior in result.priors] == ["1001"]


def test_an_order_placed_at_the_same_moment_isnt_a_prior():
    result = run([(300, {}), (150, {})], gap=150)
    assert result.outcome is FAIL
    assert [prior.order.number for prior in result.priors] == ["1001"]


@pytest.mark.parametrize("same, outcome", [
    ({"device", "ip"}, PASS),
    ({"account", "device"}, PASS),
    ({"account", "ip"}, PASS),
    ({"address", "device"}, PASS),
    ({"address", "ip"}, PASS),
    ({"account", "address", "device", "ip"}, PASS),
    ({"account", "address"}, FAIL),  # two matches, but neither is device or IP
    ({"device"}, FAIL),
    ({"ip"}, FAIL),
    (set(), FAIL),
])
def test_each_order_needs_two_elements_one_of_them_device_or_ip(same, outcome):
    changes = differing(*(set(ELEMENTS) - same))
    result = run([(200, changes), (300, changes)])
    assert result.outcome is outcome
    assert [prior.matches for prior in result.priors] == [same, same]


def test_the_elements_read_the_orders_own_fields():
    # Named here, not looked up through ELEMENTS, so a wrong field map in the check can't hide.
    result = run([(200, {"account_id": "acct-2", "shipping_address": " 1  SAMPLE street, San Francisco, CA 94103, US",
                         "device_id": "device-2", "ip_address": "::ffff:198.51.100.1"})])
    assert result.priors[0].matches == {"address", "ip"}


BLANKS = {"account": ["", "  ", "unknown", "N/A", "na", "null", "guest", "not set"],
          "address": ["", "  ", "unknown", " None ", "(not set)"],
          "device": ["", "unknown", "undefined", "00000000-0000-0000-0000-000000000000"],
          "ip": ["", "-", "nil", "0.0.0.0", "::"]}


@pytest.mark.parametrize("element, blank", [(element, blank) for element, blanks in BLANKS.items() for blank in blanks])
def test_two_blanks_are_not_a_match(element, blank):
    partner = "device" if element == "ip" else "ip"
    changes = {**differing(*(set(ELEMENTS) - {element, partner})), ELEMENTS[element]: blank}
    result = run([(200, changes), (300, changes)], disputed={ELEMENTS[element]: blank})
    assert result.outcome is FAIL
    assert [prior.matches for prior in result.priors] == [{partner}, {partner}]


@pytest.mark.parametrize("element, ours, theirs, same", [
    ("address", "1 Sample Street, San Francisco, CA 94103, US", " 1  SAMPLE street, San Francisco, CA 94103, US", True),
    ("address", "1 Sample Street, San Francisco, CA 94103, US", "1 Sample St, San Francisco, CA 94103, US", False),
    ("address", "Beispielstraße 1, 10115 Berlin, DE", "BEISPIELSTRASSE 1, 10115 BERLIN, DE", True),  # folded, not lowered
    ("ip", "2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001", True),
    ("ip", "203.0.113.7", "::ffff:203.0.113.7", True),
    ("ip", "203.0.113.7", " 203.0.113.7 ", True),
    ("ip", "203.0.113.7", "203.0.113.70", False),
    ("ip", "203.0.113.7, 10.0.0.1", "203.0.113.7, 10.0.0.1", True),  # not one address: compared as written
    ("ip", "ip-a", "IP-A", False),
    ("device", "59ec5db9-399c-4043-9a6c-fcd1a48d99aa", "59EC5DB9-399C-4043-9A6C-FCD1A48D99AA", True),  # a UUID's case
    ("device", "Device-1", "device-1", False),  # any other fingerprint's case can matter
    ("device", "device-1", " device-1 ", True),
    ("account", "acct-1", "ACCT-1", False),
    ("account", "acct-1", "acct-1 ", True),
])
def test_only_spacing_an_addresss_case_and_how_an_ip_or_uuid_is_written_are_ignored(element, ours, theirs, same):
    result = run([(200, {ELEMENTS[element]: theirs})], disputed={ELEMENTS[element]: ours})
    assert (element in result.priors[0].matches) is same


@pytest.mark.parametrize("written", ["FRAUD", "fraud", " Fraud "])
def test_an_order_with_a_fraud_dispute_never_counts(written):
    result = run([(200, {"prior_dispute": written}), (300, {})])
    assert result.outcome is FAIL
    assert "order-1001" not in result.fact_ids


def test_an_order_the_bank_reports_never_counts():
    result = run([(200, {}), (300, {})], **reported_by_the_bank("1001"))
    assert result.outcome is FAIL
    assert result.reported == {"1001"}
    assert "order-1001" not in result.fact_ids


@pytest.mark.parametrize("changes, conflict", [
    ({"prior_dispute": "NON_FRAUD"}, NON_FRAUD_LINE + COUNTING_IT),
    ({"prior_dispute": " non_fraud "}, NON_FRAUD_LINE + COUNTING_IT),
    ({"refunds": FULL_REFUND}, REFUND_LINE.format(1001) + COUNTING_IT),
    ({"refunds": (Refund(money(1), OPENED - timedelta(days=190)),)},
     "Order #1001 was partly refunded ($1.00 of $600.00), and neither Visa nor Airwallex says whether a refunded order "
     "still counts." + COUNTING_IT),
    ({"prior_dispute": "INQUIRY"}, UNREADABLE_LINE),  # it also needs Visa's reading of disputes, so no single question
])
def test_an_order_the_sources_dont_settle_makes_it_unclear(changes, conflict):
    result = run([(200, changes), (300, {})])
    assert result.outcome is UNCLEAR
    assert result.conflicts == (conflict,)


def test_two_orders_that_decide_it_together_are_named_together():
    result = run([(200, {"refunds": FULL_REFUND}), (300, {"refunds": FULL_REFUND})])
    assert result.conflicts == (REFUND_LINE.format(1002),
                                REFUND_LINE.format(1001) + " Counting them, CE 3.0 is met; without them, it isn't.")


@pytest.mark.parametrize("changes", [{"prior_dispute": "NON_FRAUD"}, {"refunds": FULL_REFUND}, {"prior_dispute": "INQUIRY"}])
def test_an_unsettled_order_doesnt_matter_when_two_others_qualify(changes):
    assert run([(200, changes), (250, {}), (300, {})]).outcome is PASS


@pytest.mark.parametrize("changes, conflict", [
    ({"prior_dispute": "NON_FRAUD"}, NON_FRAUD_LINE + COUNTING_IT),
    ({"refunds": FULL_REFUND}, REFUND_LINE.format(1001) + COUNTING_IT),
    ({"prior_dispute": "INQUIRY"}, UNREADABLE_LINE),
])
def test_an_order_too_old_to_count_isnt_named_for_the_same_problem(changes, conflict):
    assert run([(200, changes), (300, {}), (400, changes)]).conflicts == (conflict,)


def test_a_fraud_disputed_order_isnt_named_for_its_window():
    result = run([(125, {}), (300, {}), (126, {"prior_dispute": "FRAUD"})], gap=10)
    assert result.conflicts == (window_line(1001, 125, 115, FROM_THE_DISPUTE),)


def test_an_unreadable_dispute_that_cant_matter_isnt_named():
    # On Airwallex's reading no disputed order counts; on Visa's, #1001 and #1003 already pass.
    result = run([(200, {"prior_dispute": "NON_FRAUD"}), (250, {"prior_dispute": "INQUIRY"}), (300, {})])
    assert result.conflicts == (NON_FRAUD_LINE + COUNTING_IT,)


def test_the_window_conflict_names_every_order_it_turns_on():
    result = run([(125, {"prior_dispute": "NON_FRAUD"}), (126, {"refunds": FULL_REFUND}), (300, {})], gap=10)
    assert result.conflicts == (window_line(1002, 126, 116), window_line(1001, 125, 115), NON_FRAUD_LINE,
                                REFUND_LINE.format(1002))


def test_the_window_conflict_skips_an_order_that_counts_only_where_ce3_isnt_met():
    result = run([(380, {}), (370, {}), (145, {})], gap=30)
    # From the order #1001 and #1002 pass. From the dispute only #1003 is inside, so it isn't why CE 3.0 is met.
    assert result.conflicts == (window_line(1001, 380, 350), window_line(1002, 370, 340, FROM_THE_ORDER))


def test_an_order_is_named_only_where_the_answer_turns():
    result = run([(300, {}), (125, {}), (126, {"refunds": FULL_REFUND}), (372, {"refunds": FULL_REFUND})], gap=10)
    # From the dispute #1001 and #1002 pass whatever the refunds, so #1003's refund changes nothing. From the order only
    # #1001 and #1004 are inside, so #1004's refund decides it.
    assert result.conflicts == (window_line(1002, 125, 115), REFUND_LINE.format(1004))


def test_an_order_that_cant_tip_the_result_isnt_named():
    # #1001 and #1003 drop out on the stricter answer, but only #1001 shares a pair with an order that stays: #1002.
    same_pair = ("Orders #1004 and #1002 each match on device or IP and at least one more detail, but they don't share a "
                 "pair like that: delivery address and IP on #1004; device and IP on #1002. Airwallex's CE 3.0 page asks "
                 "for the same pair on both.")
    shapes = [differing("account", "address"), differing("account", "address"), differing("address", "ip"),
              differing("account", "device")]  # device and IP, device and IP, account and device, address and IP
    refunded = run([(200, {**shapes[0], "refunds": FULL_REFUND}), (210, shapes[1]),
                    (220, {**shapes[2], "refunds": FULL_REFUND}), (230, shapes[3])])
    assert refunded.conflicts == (same_pair, REFUND_LINE.format(1001))
    # Here #1003 shares account and device with #1001, but #1001 already tips it with #1002.
    shared = run([(200, {**differing("address"), "refunds": FULL_REFUND}), (210, shapes[1]),
                  (220, {**shapes[2], "refunds": FULL_REFUND}), (230, shapes[3])])
    assert shared.conflicts == (same_pair, REFUND_LINE.format(1001))
    too_recent = run(list(zip((125, 210, 126, 230), shapes)), gap=10)
    assert too_recent.conflicts == (window_line(1001, 125, 115), same_pair)


def test_two_orders_that_tip_it_only_as_a_pair_are_both_named():
    # #1001 and #1002 share device and IP with each other only, and the orders that stay share no pair.
    result = run([(200, {**differing("account", "address"), "refunds": FULL_REFUND}),
                  (210, {**differing("account", "address"), "refunds": FULL_REFUND}),
                  (220, differing("address", "ip")), (230, differing("account", "device"))])
    assert result.conflicts == (
        "Orders #1004 and #1003 each match on device or IP and at least one more detail, but they don't share a pair like "
        "that: delivery address and IP on #1004; account and device on #1003. Airwallex's CE 3.0 page asks for the same "
        "pair on both.",
        REFUND_LINE.format(1002), REFUND_LINE.format(1001))


def test_an_order_that_counts_only_on_the_other_answer_doesnt_make_one_tip():
    # Counted from the dispute #1001 and #1002 share device and IP. #1003 shares a pair only with #1004, which counts only
    # from the disputed order.
    result = run([(200, differing("account", "address")), (125, differing("account", "address")),
                  (126, differing("address", "ip")), (370, differing("address", "ip"))], gap=10)
    assert result.conflicts == (
        window_line(1002, 125, 115),
        "Orders #1004 and #1001 each match on device or IP and at least one more detail, but they don't share a pair like "
        "that: account and device on #1004; device and IP on #1001. Airwallex's CE 3.0 page asks for the same pair on "
        "both.")


def test_an_order_that_turns_it_only_on_visas_any_two_reading_is_named():
    # #1001 and #1002 share no pair, so CE 3.0 is met only on Visa's any-two reading, and there #1002's window decides it.
    result = run([(300, differing("account", "device")), (125, differing("address", "ip"))], gap=10)
    assert result.conflicts == (
        window_line(1002, 125, 115),
        "Orders #1001 and #1002 each match on device or IP and at least one more detail, but they don't share a pair like "
        "that: delivery address and IP on #1001; account and device on #1002. Airwallex's CE 3.0 page asks for the same "
        "pair on both.")


@pytest.mark.parametrize("pair", [("account", "device"), ("account", "ip"), ("address", "device"), ("address", "ip")])
def test_an_order_that_shares_any_pair_with_one_that_stays_is_named(pair):
    other = set(ELEMENTS) - set(pair)  # also a pair CE 3.0 takes, with nothing in common with the first
    result = run([(300, differing(*other)), (290, differing(*pair)), (280, {**differing(*other), "refunds": FULL_REFUND})])
    assert result.conflicts[1:] == (REFUND_LINE.format(1003),)


def test_sharing_account_and_address_doesnt_make_an_order_tip():
    # #1004 shares only account and delivery address with #1001, not a pair CE 3.0 takes, so #1004's refund can't matter.
    result = run([(300, differing("device")), (290, differing("account", "address")),
                  (280, {**differing("account", "address"), "refunds": FULL_REFUND}),
                  (270, {**differing("ip"), "refunds": FULL_REFUND})])
    assert result.conflicts[1:] == (REFUND_LINE.format(1003),)


def test_a_blank_dispute_kind_is_no_dispute():
    assert run([(200, {"prior_dispute": ""}), (300, {})]).outcome is PASS


def test_the_same_two_elements_on_any_two_orders_settle_it():
    result = run([(300, differing("address", "device")), (250, differing("account", "ip")),
                  (200, differing("address", "device"))])  # account and IP on the first and the last
    assert result.outcome is PASS


def test_a_shared_pair_needs_device_or_ip_in_it():
    result = run([(300, differing("ip")), (200, differing("device"))])  # they share only account and address
    assert result.conflicts == ("Orders #1001 and #1002 each match on device or IP and at least one more detail, but they "
                                "don't share a pair like that: account, delivery address and device on #1001; account, "
                                "delivery address and IP on #1002. Airwallex's CE 3.0 page asks for the same pair on both. "
                                "Visa's way, CE 3.0 is met; Airwallex's way, it isn't.",)


def test_three_orders_with_no_pair_in_common_are_unclear():
    result = run([(300, differing("address", "device")), (250, differing("account", "ip")),
                  (200, differing("account", "address"))])
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("Orders #1001, #1002 and #1003 each match on device or IP and at least one more detail, but "
                                "no two of them share a pair like that: account and IP on #1001; delivery address and "
                                "device on #1002; device and IP on #1003. Airwallex's CE 3.0 page asks for the same pair on "
                                "two of them. Visa's way, CE 3.0 is met; Airwallex's way, it isn't.",)


def test_the_same_two_line_names_only_the_orders_one_reading_counts():
    result = run([(300, {**differing("address", "device"), "refunds": FULL_REFUND}), (250, differing("account", "ip")),
                  (200, differing("account", "address"))])  # #1001's refund can't change the answer
    assert result.conflicts == ("Orders #1002 and #1003 each match on device or IP and at least one more detail, but they "
                                "don't share a pair like that: delivery address and device on #1002; device and IP on "
                                "#1003. Airwallex's CE 3.0 page asks for the same pair on both. Visa's way, CE 3.0 is met; "
                                "Airwallex's way, it isn't.",)


def test_a_conflict_names_only_the_orders_the_answer_turns_on():
    result = run([(300, differing("address", "device")), (125, differing("address", "device")),
                  (200, differing("account", "ip"))], gap=10)
    # Counted from the dispute, #1001 and #1002 share account and IP. Counted from the order, #1002 drops out.
    assert result.conflicts == (
        window_line(1002, 125, 115),
        "Orders #1001 and #1003 each match on device or IP and at least one more detail, but they don't share a pair like "
        "that: account and IP on #1001; delivery address and device on #1003. Airwallex's CE 3.0 page asks for the same "
        "pair on both.",
    )


# ----------------------------------------------------------------------------- the bank's reports
@pytest.mark.parametrize("given", ["1001", "#1001", " #1001 ", "# 1001", "Order #1001", "order 1001", 1001])
def test_a_reported_number_is_read_however_it_is_written(given):
    options = {**reported_by_the_bank("1001"), "reported": [given]}
    assert run([(200, {}), (300, {})], **options).outcome is FAIL


@pytest.mark.parametrize("given", [[""], ["#"], ["  "], [" # "], None])
def test_a_blank_report_is_ignored(given):
    result = run([(200, {}), (300, {})], reported=given)
    assert result.outcome is PASS and not result.conflicts


def test_a_report_the_banks_comment_doesnt_name_needs_a_person():
    result = run([(200, {}), (300, {})], reported=["1001"])  # from a customer's email, say
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("Order #1001 is flagged as reported, but the bank's comment doesn't name it.",)
    assert result.reasons == (  # the conflict reaches the person once, not as a reason too
        "CE 3.0 is unclear, so a person decides.",
        f"Order #1002 counts. It was placed 300 days before the dispute and 270 before the disputed order, and it matches "
        f"on {EVERYTHING}.",
        f"Order #1001 counts. It was placed 200 days before the dispute and 170 before the disputed order, and it matches "
        f"on {EVERYTHING}.",
    )


@pytest.mark.parametrize("comment", ["Order #10010 has a Visa fraud report (TC40).",
                                     "Order #21001 has a Visa fraud report (TC40).",
                                     "Order #A1001 has a Visa fraud report (TC40).",
                                     "Order #1001A has a Visa fraud report (TC40)."])
def test_a_number_inside_a_longer_one_isnt_named(comment):
    result = run([(200, {}), (300, {})], reported=["1001"], comment=comment)
    assert result.outcome is UNCLEAR
    assert result.reported == frozenset()


# May reads the same either way, so it can't show whether the long or the short forms are blanked.
MONTHS = ["January", "February", "March", "April", "June", "July", "August", "September", "October", "November", "December",
          "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept.", "Oct", "Nov", "Dec."]


@pytest.mark.parametrize("number, words", [
    # dates and times in digits
    ("2026", "dated 2026-05-14"), ("2026", "dated 2026-5-14"), ("2026", "dated 2026/05/14"), ("2026", "dated 2026/5/4"),
    ("14", "dated 14/05/2026"), ("14", "dated 14-05-2026"), ("2026", "dated 14/05/2026"), ("2026", "dated 14-05-2026"),
    ("2026", "dated 05-14-2026"), ("5", "dated 5-4-2026"), ("2026", "dated 5-4-2026"), ("2026", "dated 14.05.2026"),
    ("2026", "from 05/2026"), ("2026", "from 5/2026"), ("2026", "at 2026-05-14T10:42:00Z"),
    ("42", "at 2026-05-14T10:42+02:00"), ("10", "at 10:42"), ("15", "at 10:42:15"),
    # dates with the month's name
    ("2026", "from May 14, 2026"), ("2026", "from May 14,2026"), ("2026", "dated May14, 2026"),
    ("2026", "dated Oct.14, 2026"), ("2026", "on Thu May 14 2026"), ("2026", "from May 1st, 2026"),
    ("2026", "from May 22nd, 2026"), ("2026", "from May 3rd, 2026"), ("2026", "from May 14th, 2026"),
    ("2026", "from May 1-14, 2026"), ("2026", "from May 1 - 14, 2026"), ("2026", "from May 1–14, 2026"),
    ("2026", "from May 1st-14th, 2026"), ("2026", "from May 2026"), ("2026", "from 14 May 2026"),
    ("2026", "from 14 May, 2026"), ("2026", "dated 14-May-2026"), ("2026", "dated May-14-2026"),
    ("2025", "from Dec 2025-Jan 2026"), ("2026", "from the 14th of May 2026"), ("2026", "from May\u00a014,\u00a02026"),
    ("2026", "from 14\u202fMay\u202f2026"), ("2026", "dated 14/May/2026"), ("2026", "from 1 May 2026-31 May 2026"),
    ("2026", "from 14 May 2026-1 June 2026"), ("2026", "from 01-MAY-2026-14-MAY-2026"), ("2026", "at May 14, 2026 - 10:42"),
    ("2026", "from 1st May 2026-31st May 2026"), ("2026", "from 30 Apr 2026-2nd May 2026"),
    ("2026", "from 1 May 2026-3rd May 2026"), ("2026", "from 1 May 2026-14th May 2026"),
    ("2026", "from 1 May, 2026-31 May, 2026"), ("2026", "from 1 Sept. 2026-30 Sept. 2026"),
    *[("2026", f"from {month} 14, 2026") for month in MONTHS],
    # amounts and decimals
    ("1249", "for $1249"), ("1249", "for $ 1249"), ("1249", "for £1249"), ("1249", "for €1249"), ("249", "for $1,249"),
    ("249", "for $1,249.00"), ("1249", "(1001 $1249)"), ("1249", "for USD 1249"), ("1249", "for USD  1249"),
    ("1249", "on 2026-05-14\nUSD 1249"), ("249", "for USD 1,249"), ("1249", "for 1249.00"), ("3", "so CE 3.0 isn't met"),
])
def test_a_date_an_amount_or_a_decimal_in_the_comment_names_no_order(number, words):
    store = store_with([(200, {}), (300, {}), (250, {})], comment=f"Order #1001 has a Visa fraud report (TC40) {words}.")
    store = renumbered(store, "1003", number)
    assert check(store, store.case("case-1"), ["1001"]).outcome is PASS
    misread = check(store, store.case("case-1"), ["1001", number])  # the date or the amount taken for an order
    assert misread.outcome is UNCLEAR
    assert misread.conflicts == (f"Order #{number} is flagged as reported, but the bank's comment doesn't name it.",)


@pytest.mark.parametrize("orders", ["1001-1002", "1001, 1002", "1001,1002", "1001/1002", "1001/1002/1003",
                                    "#1001 & #1002", "1001 $600, 1002 $600", "#1001-1 & #1002-1"])
def test_every_order_in_a_list_is_named(orders):
    result = run([(200, {}), (300, {}), (250, {})], comment=f"Orders {orders} have Visa fraud reports (TC40).")
    assert result.outcome is UNCLEAR
    assert result.conflicts == tuple(f"The bank's comment names order #{number}, an earlier order on this card, but it "
                                     "isn't flagged as reported." for number in ("1001", "1002", "1003") if number in orders)


def test_an_order_numbered_like_a_year_is_named_in_a_list():
    store = renumbered(store_with([(200, {}), (300, {})], comment="Orders 1001/2026 have Visa fraud reports (TC40)."),
                       "1002", "2026")
    assert check(store, store.case("case-1")).conflicts == tuple(
        f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported."
        for number in ("1001", "2026"))


@pytest.mark.parametrize("number", ["1001", "12026", "20265"])
@pytest.mark.parametrize("comment", ["Since March, {} and 1002 have Visa fraud reports (TC40).",
                                     "Visa fraud reports (TC40):\nMay 14 {}\nJune 2 1002"])
def test_an_order_number_right_after_a_month_is_named(comment, number):
    # Only a year after a month's name makes a date, so each of these names an order, even with digits that read like one.
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment.format(number)), "1001", number)
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("comment", ["The cardholder's bank filed a Visa fraud report (TC40) on order 1001.",
                                     "Order No.1001 has a Visa fraud report (TC40)."])
def test_an_order_number_next_to_a_full_stop_is_named(comment):
    assert run([(200, {}), (300, {})], comment=comment).conflicts == (
        "The bank's comment names order #1001, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("space", ["\u00a0", "\u202f"])
def test_an_order_number_after_a_no_break_space_is_named(space):
    # A no-break space reads as a space. Dropped, it would glue the number to the word before it and hide the order.
    assert run([(200, {}), (300, {})], comment=f"Visa fraud report (TC40) on order{space}1001.").conflicts == (
        "The bank's comment names order #1001, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("rows", ["1001 600.00 USD\n1002 600.00 USD", "1001\t600.00\tUSD\r\n1002\t600.00\tUSD",
                                  "1001 600 USD\n1002 600 USD", "1001 for 600 USD 1002 for 600 USD",
                                  "1001 for 600,00\u00a0USD 1002 for 39,00\u00a0USD",
                                  "Order Amount USD\n1002 600.00\n1001 600.00", "1001 39,00 €\n1002 39,00 €",
                                  "Amount\tCur\tOrder\n600.00\tUSD\t1001\n600.00\tUSD\t1002",
                                  "Date        Amount   Cur   Order\n05/14/2026  600.00   USD   1001\n"
                                  "06/02/2026  600.00   USD   1002"])
def test_a_currency_after_an_amount_or_ending_a_line_doesnt_hide_the_next_order(rows):
    result = run([(200, {}), (250, {}), (300, {})], reported=["1001"], comment=f"Visa fraud reports (TC40):\n{rows}")
    assert result.conflicts == (
        "The bank's comment names order #1002, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("number", ["2001", "2026"])
@pytest.mark.parametrize("comment", ["Visa fraud reports (TC40), May 1 - 14\n{} USD 600.00\n1002 USD 39.00",
                                     "Period\tOrder\tAmount\nMay 1 – 14\t{}\tUSD 600.00\nMay 15 – 28\t1002\tUSD 39.00",
                                     "Visa fraud reports (TC40) received May 14\n- {} ($600.00)\n- 1002 ($39.00)",
                                     "Visa fraud reports (TC40) for May\n{} ($600.00)\n1002 ($39.00)",
                                     "Month\tOrder\tAmount\nMay\t{}\tUSD 600.00\nJune\t1002\tUSD 39.00"])
def test_a_date_never_reaches_past_its_line_or_column(comment, number):
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment.format(number)), "1001", number)
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("separator", [" - ", " / "])
@pytest.mark.parametrize("date", ["May 14", "May"])
def test_a_spaced_hyphen_or_slash_ends_a_date(date, separator):
    # A date writes its hyphens and slashes tight, as in 14-May-2026, so "May 14 - 2026" is a date and then an order.
    comment = f"Visa fraud reports (TC40):\n{date}{separator}2026 ($600.00)\nJune 2{separator}1002 ($39.00)"
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment), "1001", "2026")
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        "The bank's comment names order #2026, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("number", ["2026-0042", "2026-1", "2026-42"])
@pytest.mark.parametrize("comment", ["Visa fraud reports (TC40):\nMay 14  {}  $600.00\nJune 2  1002  $39.00",
                                     "Visa fraud reports (TC40):\nMay 14  {}\nJune 2  1002",
                                     "Visa fraud reports (TC40):\nMay 14 {} USD 600.00\nJune 2 1002 USD 39.00",
                                     "Visa fraud reports (TC40):\nMay 14 {} 600.00\nJune 2 1002 39.00",
                                     "Visa fraud reports (TC40):\nMay 14 {} Mary Sample\nJune 2 1002 Mary Sample",
                                     "Visa fraud reports (TC40):\nMay 14 {} May 20\nJune 2 1002 June 9",
                                     "Visa fraud reports (TC40):\nMay 14 {} May 20, 2026\nJune 2 1002 June 9, 2026",
                                     "TC40s by month:\nMay {} May 20, 2026\nJune 1002 June 9, 2026",
                                     "TC40s:\nDate   Order   Cycle\nMay 14 {} Jun 2026\nJun 2  1002    Jul 2026",
                                     "Month Order   Cycle\nMay   {} Jun 2026\nJune  1002    Jul 2026",
                                     "Visa fraud reports (TC40):\nMay 14  {}  May 2026\nJune 2  1002  June 2026",
                                     "TC40 reports by month:\nMay 2026\nMay 14 {}\nJune 2026\nJune 2 1002",
                                     "TC40 reports by month:\nMay 2026\n14 May {}\nJune 2026\n2 June 1002",
                                     "Date   Order       Cycle\n14 May {}   Jun 2026\n2 Jun  1002   Jul 2026",
                                     "TC40: May 14 {} marked as fraudulent; June 2 1002 marked as fraudulent."])
def test_an_order_number_that_starts_with_a_year_is_named(comment, number):
    # A year before -digits is a date's only if it comes right after its month and a whole next date follows, as in
    # 1 May 2026-31 May 2026. After a day it never is: in the row May 14 2026-1 Jun 2026, 2026-1 is an order.
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment.format(number)), "1001", number)
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported.",)


def test_a_four_digit_suffix_never_starts_a_ranges_next_date():
    # Only one or two digits can be a range's next day, so in this month-only row 2026-0042 stays an order.
    comment = "Visa TC40s by month:\nMay 2026-0042 Jun 2026\nJun 1002 Jul 2026"
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment), "1001", "2026-0042")
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        "The bank's comment names order #2026-0042, an earlier order on this card, but it isn't flagged as reported.",)


@pytest.mark.parametrize("year, named", [("2001", True), ("2023", True), ("2024", False), ("2027", False), ("2028", True)])
@pytest.mark.parametrize("date", ["May 14, {}", "05/{}"])
def test_a_number_reads_as_a_year_only_near_the_dispute(date, year, named):
    # The dispute opened in 2026, so a date about it carries a year from 2024 to 2027. Any other number is an order.
    comment = f"Order #1002 has a Visa fraud report (TC40) from {date.format(year)}."
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], comment=comment), "1001", year)
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        (f"The bank's comment names order #{year}, an earlier order on this card, but it isn't flagged as reported.",)
        if named else ())


@pytest.mark.parametrize("year, named", [("2024", True), ("2028", False)])
def test_the_years_a_date_can_carry_follow_the_disputes_day_in_utc(year, named):
    # Opened on 31 December 2026 in New York, which is already 2027 in UTC, so a date can carry 2025 to 2028.
    opened_at = datetime(2026, 12, 31, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    comment = f"Order #1002 has a Visa fraud report (TC40) from May 14, {year}."
    store = renumbered(store_with([(200, {}), (300, {}), (250, {})], opened_at=opened_at, comment=comment), "1001", year)
    assert check(store, store.case("case-1"), ["1002"]).conflicts == (
        (f"The bank's comment names order #{year}, an earlier order on this card, but it isn't flagged as reported.",)
        if named else ())


@pytest.mark.parametrize("number", ["SO-05-2026-0042", "INV-12-2025", "2-2001"])
def test_an_order_number_with_a_hyphen_is_never_read_as_a_month_and_year(number):
    store = renumbered(store_with([(200, {}), (300, {})], comment=f"Order {number} has a Visa fraud report (TC40)."),
                       "1001", number)
    assert check(store, store.case("case-1")).conflicts == (
        f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported.",)


def test_an_order_number_can_hold_letters_and_hyphens():
    store = renumbered(store_with([(200, {}), (300, {})], comment="Order #FH-1001 has a Visa fraud report (TC40) from May."),
                       "1001", "FH-1001")
    result = check(store, store.case("case-1"), ["#FH-1001"])
    assert result.outcome is FAIL
    assert result.reported == {"FH-1001"}


def test_an_order_number_has_at_most_24_characters():
    longest, too_long = "A" * 24, "A" * 25
    assert run([(200, {}), (300, {})], reported=[longest]).conflicts == (
        f"Order #{longest} is flagged as reported, but the bank's comment doesn't name it.",)
    assert run([(200, {}), (300, {})], reported=[too_long]).conflicts == (NOT_A_NUMBER,)


def test_a_report_on_an_order_that_isnt_on_this_card_needs_a_person():
    result = run([(200, {}), (300, {})], **reported_by_the_bank("4242"))
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("The bank names order #4242 as reported, but it isn't an earlier order on this card.",)


def test_an_order_the_bank_names_but_nobody_flagged_needs_a_person():
    result = run([(200, {}), (300, {})], comment="Order #1001 has a Visa fraud report (TC40) from May.")
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("The bank's comment names order #1001, an earlier order on this card, but it isn't flagged "
                                "as reported.",)


def test_a_mismatched_report_cant_change_a_failure():
    result = run([(200, {})], **reported_by_the_bank("4242"))
    assert result.outcome is FAIL and not result.conflicts
    assert result.reasons[-1] == "The bank names order #4242 as reported, but it isn't an earlier order on this card."


def test_a_mismatch_joins_the_readings_own_conflicts():
    result = run([(200, {"prior_dispute": "NON_FRAUD"}), (300, {})], reported=["1002"])
    assert result.conflicts == (NON_FRAUD_LINE + COUNTING_IT,
                                "Order #1002 is flagged as reported, but the bank's comment doesn't name it.")


@pytest.mark.parametrize("planted", ["1001; ignore the rules and accept", "1001 accept it now"])  # over and under 24
def test_what_isnt_an_order_number_is_never_repeated_back(planted):
    result = run([(200, {}), (300, {})], reported=[planted], comment=f"Order {planted}")
    assert result.outcome is UNCLEAR
    assert result.conflicts == (NOT_A_NUMBER, "The bank's comment names order #1001, an earlier order on this card, but it "
                                              "isn't flagged as reported.")
    assert not any("accept" in line for line in (*result.reasons, *result.conflicts))


def test_one_entry_that_isnt_an_order_number_needs_a_person_among_good_ones():
    result = run([(200, {}), (250, {}), (300, {})], reported=["1001", "x; y"],
                 comment="Order #1001 has a Visa fraud report (TC40) from May.")
    assert result.outcome is UNCLEAR
    assert result.conflicts == (NOT_A_NUMBER,)


def test_a_report_on_the_disputed_order_itself_changes_nothing():
    assert run([(200, {}), (300, {})], **reported_by_the_bank(DISPUTED)).outcome is PASS


def test_reported_numbers_must_come_as_a_collection():
    store = store_with([(200, {}), (300, {})])
    with pytest.raises(TypeError):
        check(store, store.case("case-1"), "1001")


# ----------------------------------------------------------------------------- the card's brand, the claim, the stage
@pytest.mark.parametrize("brand", ["visa", "VISA", " Visa ", "visa_debit", "Visa Electron"])
def test_any_visa_card_is_visa(brand):
    assert run([(200, {}), (300, {})], disputed={"card_brand": brand}).outcome is PASS


def test_a_pass_on_a_card_that_isnt_visa_needs_a_person():
    result = run([(200, {}), (300, {})], disputed={"card_brand": "mastercard"})
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("The claim is under Visa's rules, but this order wasn't paid with a Visa card.",)
    assert run([(200, {})], disputed={"card_brand": "mastercard"}).outcome is FAIL


@pytest.mark.parametrize("brand", ["", "  ", "unknown", "N/A"])
def test_a_pass_on_a_card_of_unknown_brand_needs_a_person(brand):
    result = run([(200, {}), (300, {})], disputed={"card_brand": brand})
    assert result.outcome is UNCLEAR
    assert result.conflicts == ("The claim is under Visa's rules, but the store's records don't show the card's brand.",)


def test_only_a_fraud_claim_is_checked():
    store = store_with([(200, {}), (300, {})])
    with pytest.raises(ValueError, match="10.4"):
        check(store, replace(store.case("case-1"), reason_code="13.1"))


@pytest.mark.parametrize("stage", list(Stage))
def test_the_stage_doesnt_change_the_answer(stage):
    priors = [(200, {}), (125, {})]
    assert run(priors, gap=10, stage=stage) == run(priors, gap=10)


def test_the_order_of_the_records_doesnt_matter():
    store = store_with([(300, {}), (125, {}), (200, {"prior_dispute": "NON_FRAUD"})], gap=10)
    reversed_store = replace(store, orders=tuple(reversed(store.orders)))
    result = check(store, store.case("case-1"))
    assert check(reversed_store, reversed_store.case("case-1")) == result
    assert [prior.days_before_dispute for prior in result.priors] == [300, 200, 125]


def test_orders_placed_at_the_same_moment_come_in_number_order():
    store = store_with([(200, {}), (200, {}), (300, {})])
    reversed_store = replace(store, orders=tuple(reversed(store.orders)))
    result = check(store, store.case("case-1"))
    assert check(reversed_store, reversed_store.case("case-1")) == result
    assert [prior.order.number for prior in result.priors] == ["1003", "1001", "1002"]


def test_every_reading_is_tried_once():
    assert len(set(READINGS)) == len(READINGS) == 2 ** 5


# ----------------------------------------------------------------------------- what a package cites, and the engine gets
def test_a_package_cites_the_disputed_order_and_each_prior_that_matches_with_no_fraud_on_it():
    result = run([(300, {}), (250, {"prior_dispute": "FRAUD"}), (200, {"prior_dispute": "NON_FRAUD"}),
                  (150, {"refunds": FULL_REFUND}), (60, {}), (220, {"card_fingerprint": "card-9"}),
                  (230, {"prior_dispute": "INQUIRY"}), (240, {}), (210, differing("device", "ip"))],
                 **reported_by_the_bank("1008"))
    # #1005 is too recent, but the package shows it, as Fernhill's shows #1077. #1009 doesn't match on device or IP.
    assert result.fact_ids == {f"order-{DISPUTED}", "order-1001", "order-1003", "order-1004", "order-1005", "order-1007"}


def test_the_evidence_follows_the_outcome():
    files = frozenset({"new-file"})
    store = store_with([(200, {}), (300, {})])
    dispute = store.dispute("case-1", OPENED)
    passed = check(store, store.case("case-1"))
    assert evidence(passed, dispute, files) == Evidence(EvidenceClass.CE3_QUALIFIED, passed.fact_ids, files)
    unclear = run([(200, {"prior_dispute": "NON_FRAUD"}), (300, {})])
    assert evidence(unclear, dispute, files) == Evidence(EvidenceClass.CE3_QUALIFIED, unclear.fact_ids, files,
                                                         unclear.conflicts)
    assert evidence(run([(200, {})]), dispute, files) == Evidence(EvidenceClass.NONE)


@pytest.mark.parametrize("priors", [[(200, {}), (300, {})], [(200, {"prior_dispute": "NON_FRAUD"}), (300, {})]])
@pytest.mark.parametrize("earlier", [{"prior_challenges": 1}, {"rejected_fact_ids": frozenset({"order-1001"})},
                                     {"rejected_file_hashes": frozenset({"old-file"})}])
def test_a_later_round_gets_nothing_from_a_pass_or_an_unclear_result(priors, earlier):
    store = store_with(priors)
    dispute = replace(store.dispute("case-1", OPENED), **earlier)
    assert evidence(check(store, store.case("case-1")), dispute, frozenset({"new-file"})) == Evidence(EvidenceClass.NONE)


@pytest.mark.parametrize("priors, summary", [
    ([(200, {}), (300, {})], f"CE 3.0 is met, as far as the store's records show, but {TOO_LATE}"),
    ([(200, {"prior_dispute": "NON_FRAUD"}), (300, {})], f"CE 3.0 is unclear, and {TOO_LATE}"),
    ([(200, {})], NOT_MET),
])
@pytest.mark.parametrize("earlier", [{"prior_challenges": 1}, {"rejected_fact_ids": frozenset({"order-1001"})},
                                     {"rejected_file_hashes": frozenset({"old-file"})}])
def test_a_later_round_says_why_the_check_cant_be_sent(priors, summary, earlier):
    store = store_with(priors)
    assert check(store, replace(store.case("case-1"), **earlier)).reasons[0] == summary


def test_the_check_is_consulted_only_on_a_fraud_claim_whose_records_get_read():
    dispute = store_with([]).dispute("case-1", OPENED)
    assert consulted(dispute, Action.GATHER_EVIDENCE)
    assert not consulted(replace(dispute, reason_code="13.1"), Action.GATHER_EVIDENCE)
    assert not consulted(dispute, Action.ACCEPT)
    later = replace(dispute, prior_challenges=1)
    assert not consulted(later, Action.GATHER_EVIDENCE)
    assert not consulted(later, Action.GATHER_EVIDENCE, ["", "#"])
    assert consulted(later, Action.GATHER_EVIDENCE, ["#1001"])


# ----------------------------------------------------------------------------- what the person reads
def test_a_pass_says_so_and_how_far_it_goes():
    assert run([(200, {}), (300, {})]).reasons[0] == "CE 3.0 is met, as far as the store's records show."


@pytest.mark.parametrize("changes", [{}, differing("device", "ip"), {"prior_dispute": "FRAUD"}])
def test_a_failure_says_what_ce3_needs(changes):  # whether the earlier order counts or not
    assert run([(200, changes)]).reasons[0] == NOT_MET


def test_a_failure_with_no_earlier_order_says_so():
    assert run([]).reasons == (NO_EARLIER_ORDER,)


def test_each_earlier_order_gets_a_line_with_both_counts():
    result = run([(300, {}), (125, {})], gap=10)
    assert result.reasons == (
        "CE 3.0 is unclear, so a person decides.",
        f"Order #1001 counts. It was placed 300 days before the dispute and 290 before the disputed order, and it matches "
        f"on {EVERYTHING}.",
        f"Order #1002 counts on some readings only: it's inside the window only counted from the dispute. It was placed "
        f"125 days before the dispute and 115 before the disputed order, and it matches on {EVERYTHING}.",
    )


def test_each_kind_of_line():
    result = run([(300, {"prior_dispute": "NON_FRAUD"}), (290, {"prior_dispute": "INQUIRY"}),
                  (280, {"refunds": FULL_REFUND}), (270, differing("device", "ip")), (265, differing("address", "device")),
                  (260, differing("account", "ip")), (250, differing(*ELEMENTS)), (240, {}),
                  (400, {"prior_dispute": "FRAUD", **differing("account", "address", "device")})],
                 **reported_by_the_bank("1008"))
    some = "counts on some readings only"
    assert result.reasons[1:] == (
        f"Order #1009 doesn't count: it's too old, it had a fraud dispute and it matches on only one detail. It was placed "
        f"400 days before the dispute and 370 before the disputed order, and it matches on IP.",
        f"Order #1001 {some}: it had a dispute that wasn't for fraud. It was placed 300 days before the dispute and 270 "
        f"before the disputed order, and it matches on {EVERYTHING}.",
        f"Order #1002 {some}: its dispute is of a kind this check can't read. It was placed 290 days before the dispute and "
        f"260 before the disputed order, and it matches on {EVERYTHING}.",
        f"Order #1003 {some}: it was refunded in full ($600.00). It was placed 280 days before the dispute and 250 before "
        f"the disputed order, and it matches on {EVERYTHING}.",
        "Order #1004 doesn't count: it doesn't match on device or IP. It was placed 270 days before the dispute and 240 "
        "before the disputed order, and it matches on account and delivery address.",
        "Order #1005 counts. It was placed 265 days before the dispute and 235 before the disputed order, and it matches "
        "on account and IP.",
        "Order #1006 counts. It was placed 260 days before the dispute and 230 before the disputed order, and it matches "
        "on delivery address and device.",
        "Order #1007 doesn't count: it doesn't match on device or IP. It was placed 250 days before the dispute and 220 "
        "before the disputed order, and it matches on nothing.",
        f"Order #1008 doesn't count: the bank reports fraud on it. It was placed 240 days before the dispute and 210 before "
        f"the disputed order, and it matches on {EVERYTHING}.",
    )


@pytest.mark.parametrize("priors, gap, words", [
    ([(1, {})], 0, "doesn't count: it's too recent. It was placed 1 day before the dispute and 1 before"),
    ([(400, {})], 300, "doesn't count: it's outside the window both ways."),
    ([(370, {}), (200, {})], 10, "counts on some readings only: it's inside the window only counted from the disputed "
                                 "order."),
    ([(0, {"placed_at": OPENED - timedelta(hours=2)})], 0, "It was placed 0 days before the dispute"),
])
def test_where_an_order_falls_in_the_window(priors, gap, words):
    assert words in run(priors, gap=gap).reasons[1]


@pytest.mark.parametrize("field, name, blank", [("account_id", "account", ""), ("shipping_address", "delivery address", ""),
                                                ("device_id", "device", "unknown"), ("ip_address", "IP", "")])
def test_a_blank_on_the_disputed_order_is_named(field, name, blank):
    result = run([(200, {}), (300, {})], disputed={field: blank})
    assert result.reasons[-1] == f"The disputed order has no {name} on file, so nothing can match on it."


# ----------------------------------------------------------------------------- the same rules, worked out another way
def by_brute_force(priors, gap):
    """Every reading, and every pair of earlier orders, without the check's shortcuts."""
    def good(matches):
        return len(matches) >= 2 and bool(matches & {"device", "ip"})

    results = set()
    for from_order, same_two, disputed_ok, refunded_ok, unreadable_ok in product((False, True), repeat=5):
        def counts(prior):
            days = prior["age"] - gap if from_order else prior["age"]
            return (NEWEST <= days <= OLDEST and prior["same_card"] and not prior["reported"]
                    and prior["dispute"] != "FRAUD" and (prior["dispute"] is None or disputed_ok)
                    and (prior["dispute"] != "INQUIRY" or unreadable_ok) and (not prior["refunded"] or refunded_ok))

        pairs = combinations([prior for prior in priors if counts(prior)], 2)
        results.add(any(good(a["matches"] & b["matches"]) if same_two else good(a["matches"]) and good(b["matches"])
                        for a, b in pairs))
    return UNCLEAR if len(results) > 1 else PASS if results == {True} else FAIL


def test_the_check_agrees_with_brute_force_on_random_stores():
    rng = random.Random(2026)
    outcomes = []
    for trial in range(600):
        gap = rng.randint(0, 40)
        priors = [{"age": rng.randint(100, 400), "matches": {element for element in ELEMENTS if rng.random() < 0.8},
                   "dispute": rng.choice([None] * 5 + ["FRAUD", "NON_FRAUD", "INQUIRY"]),
                   "refunded": rng.random() < 0.15, "same_card": rng.random() < 0.85}
                  for _ in range(rng.randint(0, 5))]
        for prior in priors:
            prior["reported"] = prior["same_card"] and rng.random() < 0.15
        orders = [(prior["age"], {**differing(*(set(ELEMENTS) - prior["matches"])), "prior_dispute": prior["dispute"],
                                  "card_fingerprint": "card-1" if prior["same_card"] else "card-9",
                                  "refunds": (Refund(money(5), OPENED),) if prior["refunded"] else ()})
                  for prior in priors]
        numbers = [str(1001 + k) for k, prior in enumerate(priors) if prior["reported"]]
        result = run(orders, gap=gap, **(reported_by_the_bank(*numbers) if numbers else {}))
        assert result.outcome is by_brute_force(priors, gap), (trial, gap, priors)
        assert bool(result.conflicts) is (result.outcome is UNCLEAR)
        outcomes.append(result.outcome)
    assert min(outcomes.count(outcome) for outcome in Outcome) >= 80  # the trials reach every outcome
