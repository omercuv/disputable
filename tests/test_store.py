"""The store's records and the four read-only queries Claude's tools will use."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from disputable.engine import Stage, Status
from disputable.fees import ZERO, money
from disputable.store import Case, Customer, Email, Item, Order, Refund, Scan, Store, WarrantyRegistration, tool_view

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
ANN = Customer("customer-ann", "Ann Sample", "ann@example.com", "+1 4155550101", "1 Sample Street")
BEA = Customer("customer-bea", "Bea Demo", "bea@example.com", "+1 4155550102", "2 Demo Street")


def order(number="100", customer=ANN, days_ago=10, price="50", card="card-ann", **changes):
    return Order(number=number, customer_id=customer.id, placed_at=NOW - timedelta(days=days_ago),
                 items=(Item("SKU", "A thing", money(price), serial=f"serial-{number}"),), card_fingerprint=card,
                 card_last4="0001", cardholder_name=customer.name, cardholder_phone=customer.phone, account_id="acct",
                 email=customer.email, device_id="device", ip_address="192.0.2.1", shipping_address=customer.billing_address,
                 **changes)


def scan(scan_id, order_number="100", days_ago=5, source="carrier"):
    return Scan(scan_id, order_number, source, "outbound", NOW - timedelta(days=days_ago), "DELIVERED", "1 Sample Street")


def email(email_id, from_addr, subject="Hello", body="Some words", days_ago=3, to_addr="support@shop.example"):
    return Email(email_id, "thread", NOW - timedelta(days=days_ago), from_addr, to_addr, subject, body)


def warranty(entry_id, serial="serial-100", days_ago=4):
    return WarrantyRegistration(entry_id, serial, NOW - timedelta(days=days_ago), "ann@example.com", "+1 4155550101",
                                "device-2", "192.0.2.2")


def case(case_id="case", order_number="100", **changes):
    return Case(case_id, order_number, "13.1", Stage.RFI, NOW - timedelta(days=1), timedelta(days=2), "A comment", **changes)


def store(**changes):
    fields = {"customers": (ANN, BEA), "orders": (order(),)}
    return Store(**{**fields, **changes})


# ----------------------------------------------------------------------------- records that don't add up
@pytest.mark.parametrize("changes, words", [
    ({"orders": (order(), order())}, "used twice"),
    ({"scans": (scan("x"), scan("x"))}, "used twice"),
    ({"emails": (email("order-100", "ann@example.com"),)}, "used twice"),
    ({"orders": (order(customer=Customer("nobody", "N", "n@example.com", "", "")),)}, "unknown customer"),
    ({"scans": (scan("x", order_number="999"),)}, "unknown order"),
    ({"cases": (case(order_number="999"),)}, "unknown order"),
])
def test_records_that_dont_add_up_are_refused(changes, words):
    with pytest.raises(ValueError, match=words):
        store(**changes)


@pytest.mark.parametrize("record", [
    ANN, Item("A", "One", money(1)), Refund(money(1), NOW), order(), scan("x"), email("e", "ann@example.com"), warranty("w"),
    case(), store(),
], ids=lambda record: type(record).__name__)
def test_every_kind_of_record_is_frozen(record):
    with pytest.raises(FrozenInstanceError):
        setattr(record, fields(record)[0].name, "changed")


# ----------------------------------------------------------------------------- finding a record
def test_an_order_total_adds_up_its_items():
    items = (Item("A", "One", money("10.50"), quantity=2), Item("B", "Two", money("1.25")))
    assert replace(order(), items=items).amount == Decimal("22.25")


def test_every_kind_of_record_can_be_found_by_id():
    records = store(scans=(scan("scan-1"),), emails=(email("email-1", "ann@example.com"),),
                    warranty=(warranty("warranty-1"),), cases=(case(),))
    for record_id, kind in (("customer-ann", Customer), ("order-100", Order), ("scan-1", Scan), ("email-1", Email),
                            ("warranty-1", WarrantyRegistration), ("case", Case)):
        assert isinstance(records.record(record_id), kind)
    with pytest.raises(KeyError):
        records.record("nothing")


@pytest.mark.parametrize("quote, found", [
    ("Some words", True),
    ("Some", True),
    ("some words", False),  # verbatim means verbatim
    ("Some  words", False),
    ("", False),
    (" ", False),
    ("Words nobody wrote", False),
])
def test_a_quote_has_to_be_word_for_word(quote, found):
    records = store(emails=(email("email-1", "ann@example.com"),))
    assert records.quote_is_in("email-1", quote) is found


def test_a_quote_can_come_from_any_field_of_a_record():
    records = store()
    assert records.quote_is_in("order-100", "A thing")  # inside an item
    assert records.quote_is_in("order-100", "serial-100")
    assert records.quote_is_in("order-100", "192.0.2.1")
    assert records.quote_is_in("order-100", NOW.date().replace(day=23, month=10).isoformat())  # a date, as ISO text
    assert records.quote_is_in("order-100", (NOW - timedelta(days=10)).isoformat())  # a time, as ISO text
    assert records.quote_is_in("order-100", "50")  # a price is text too


@pytest.mark.parametrize("quote", ["", " ", "   "])
def test_a_blank_quote_is_no_quote_even_where_a_field_is_blank(quote):
    assert not store(scans=(replace(scan("scan-1"), note=quote),)).quote_is_in("scan-1", quote)


def test_a_quote_stays_inside_one_real_value():
    records = store()
    assert not records.quote_is_in("order-100", "None")  # an empty field holds no words
    assert not records.quote_is_in("order-100", "Item(")  # an item's repr isn't its text
    assert not records.quote_is_in("order-100", "SKU\nA thing")  # a quote can't span two fields
    assert not records.quote_is_in("order-100", " A thing")  # the spaces are part of the quote


def test_a_fact_about_a_record_that_doesnt_exist_is_dropped():
    assert store().quote_is_in("email-nobody-wrote", "anything") is False
    with pytest.raises(KeyError):
        store().record("email-nobody-wrote")  # asking for the record itself stays an error


@pytest.mark.parametrize("quote, found", [("1", True), ("5", False), ("SK", False), ("thi", True), ("0", False)])
def test_a_fragment_under_3_characters_must_be_a_whole_value(quote, found):
    assert store().quote_is_in("order-100", quote) is found  # 1 is the quantity, and "thi" is 3 characters of "A thing"


def test_a_records_own_id_isnt_something_it_says():
    assert not store(scans=(scan("scan-1"),)).quote_is_in("scan-1", "scan-1")


# ----------------------------------------------------------------------------- what a tool shows
def test_a_tool_shows_the_id_a_fact_must_cite_and_text_it_can_quote():
    refund = Refund(money("2.50"), NOW)
    records = store(orders=(order(refunds=(refund,)),))
    view = tool_view(records.order("100"))
    assert view["id"] == "order-100"
    assert view["number"] == "100"
    assert view["placed_at"] == (NOW - timedelta(days=10)).isoformat()
    assert view["three_ds"] == "False"
    assert view["tracking_number"] is None
    assert view["items"][0]["unit_price"] == "50"
    assert view["refunds"] == [{"amount": "2.50", "at": NOW.isoformat()}]
    for value in (view["placed_at"], view["three_ds"], view["items"][0]["unit_price"], view["refunds"][0]["at"]):
        assert records.quote_is_in("order-100", value)


# ----------------------------------------------------------------------------- the dispute the engine sees
def test_a_case_becomes_the_dispute_the_engine_reads():
    rejected = frozenset({"order-100"})
    dispute = store(cases=(case(prior_challenges=1, ai_automation_challenged=True, rejected_fact_ids=rejected,
                                rejected_file_hashes=frozenset({"file"})),)).dispute("case", NOW)
    assert dispute.id == "case"
    assert dispute.stage is Stage.RFI
    assert dispute.status is Status.REQUIRES_RESPONSE
    assert dispute.amount == money(50)
    assert dispute.currency == "USD"
    assert dispute.reason_code == "13.1"
    assert dispute.due_at == NOW + timedelta(days=2)
    assert dispute.captured_amount == money(50)
    assert dispute.refunded_amount == ZERO
    assert dispute.prior_challenges == 1
    assert dispute.ai_automation_challenged
    assert dispute.rejected_fact_ids == rejected
    assert dispute.rejected_file_hashes == frozenset({"file"})


def test_a_partial_dispute_keeps_the_whole_capture():
    dispute = store(cases=(case(amount=money(20)),)).dispute("case", NOW)
    assert dispute.amount == money(20)
    assert dispute.captured_amount == money(50)


def test_refunds_on_the_order_reach_the_engine():
    refunds = (Refund(money(5), NOW), Refund(money("2.50"), NOW))
    dispute = store(orders=(order(refunds=refunds),), cases=(case(),)).dispute("case", NOW)
    assert dispute.refunded_amount == Decimal("7.50")


def test_due_dates_follow_the_clock_they_are_given():
    later = NOW + timedelta(days=30)
    assert store(cases=(case(),)).dispute("case", later).due_at == later + timedelta(days=2)


# ----------------------------------------------------------------------------- what Claude's tools can read
def test_order_history_finds_the_customer_or_the_card_oldest_first():
    orders = (order("100", days_ago=10), order("101", days_ago=30), order("102", BEA, days_ago=20, card="card-bea"),
              order("103", BEA, days_ago=40, card="card-ann"))
    assert [o.number for o in store(orders=orders).get_order_history("100")] == ["103", "101", "100"]
    assert [o.number for o in store(orders=orders).get_order_history("102")] == ["103", "102"]


def test_order_history_holds_orders_after_this_one_too():
    orders = (order("100", days_ago=10), order("101", days_ago=30))
    assert [o.number for o in store(orders=orders).get_order_history("101")] == ["101", "100"]


def test_scans_are_for_that_order_oldest_first_from_both_sources():
    orders = (order("100"), order("101"))
    scans = (scan("late", days_ago=1), scan("early", days_ago=9, source="warehouse"), scan("other", "101"))
    assert [s.id for s in store(orders=orders, scans=scans).get_carrier_scans("100")] == ["early", "late"]


def test_scans_want_the_whole_order_number():
    assert store(orders=(order("100"), order("1000")), scans=(scan("x", "1000"),)).get_carrier_scans("100") == ()


def test_the_warranty_log_is_by_serial_oldest_first():
    log = (warranty("late", days_ago=1), warranty("early", days_ago=9), warranty("other", serial="serial-200"))
    assert [e.id for e in store(warranty=log).get_warranty_log("serial-100")] == ["early", "late"]
    assert store(warranty=log).get_warranty_log("serial-300") == ()


def test_the_warranty_log_wants_the_whole_serial():
    assert store(warranty=(warranty("w"),)).get_warranty_log("serial-1") == ()


@pytest.mark.parametrize("message, found", [
    (dict(from_addr="ann@example.com"), True),
    (dict(from_addr="ANN@example.com"), True),
    (dict(from_addr="support@shop.example", to_addr="ann@example.com"), True),
    (dict(from_addr="stranger@example.net", subject="About #100"), True),
    (dict(from_addr="stranger@example.net", body="Order 100 is mine"), True),
    (dict(from_addr="bea@example.com"), False),
    (dict(from_addr="stranger@example.net", body="Order 1000 is mine"), False),
    (dict(from_addr="stranger@example.net", body="Order 2100 is mine"), False),
])
def test_the_inbox_finds_the_customer_or_a_mention_of_the_order(message, found):
    records = store(emails=(email("email-1", **message),))
    assert (len(records.search_inbox("100")) == 1) is found


def test_the_inbox_also_finds_the_address_on_the_order():
    records = store(orders=(replace(order(), email="other@example.com"),), emails=(email("email-1", "other@example.com"),))
    assert len(records.search_inbox("100")) == 1


def test_the_inbox_finds_the_customers_own_address_when_the_order_used_another():
    records = store(orders=(replace(order(), email="other@example.com"),), emails=(email("email-1", "ann@example.com"),))
    assert len(records.search_inbox("100")) == 1


def test_address_case_doesnt_matter_on_any_side():
    shouting = Customer("customer-ann", "Ann Sample", "ANN@example.com", "+1 4155550101", "1 Sample Street")
    used_another = (replace(order(), email="other@example.com"),)
    assert store(customers=(shouting, BEA), orders=used_another, emails=(email("e", "ann@example.com"),)).search_inbox("100")
    shouting_order = (replace(order(), email="OTHER@example.com"),)
    assert store(orders=shouting_order, emails=(email("e", "other@example.com"),)).search_inbox("100")
    assert store(emails=(email("e", "support@shop.example", to_addr="ANN@example.com"),)).search_inbox("100")


def test_a_search_word_narrows_the_inbox_ignoring_case():
    mails = (email("email-1", "ann@example.com", subject="My RETURN"), email("email-2", "ann@example.com", body="a return"),
             email("email-3", "ann@example.com", body="something else"))
    assert [e.id for e in store(emails=mails).search_inbox("100", "return")] == ["email-1", "email-2"]
    assert [e.id for e in store(emails=mails).search_inbox("100", "RETURN")] == ["email-1", "email-2"]


def test_a_search_phrase_stays_a_phrase():
    mails = (email("email-1", "ann@example.com", body="my return"), email("email-2", "ann@example.com", body="my order"))
    assert [e.id for e in store(emails=mails).search_inbox("100", "my return")] == ["email-1"]


def test_the_inbox_is_oldest_first():
    mails = (email("late", "ann@example.com", days_ago=1), email("early", "ann@example.com", days_ago=9))
    assert [e.id for e in store(emails=mails).search_inbox("100")] == ["early", "late"]


@pytest.mark.parametrize("query", ["get_order_history", "get_carrier_scans", "search_inbox"])
def test_the_tools_refuse_an_order_that_doesnt_exist(query):
    with pytest.raises(KeyError):
        getattr(store(), query)("999")


def test_the_second_ending_differs_only_in_the_warranty_log():
    records = store(scans=(scan("x"),), emails=(email("e", "ann@example.com"),), warranty=(warranty("w"),), cases=(case(),))
    assert records.without_warranty().warranty == ()
    assert replace(records.without_warranty(), warranty=records.warranty) == records
    assert len(records.warranty) == 1
