"""The M findings: what the inbox shows for a case's order, as counts and days, never what an email says."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from disputable import inbox
from disputable.inbox import M0, M2_NEVER, findings
from disputable.prompts import FERNHILL as PROFILE
from disputable.seed.build import SUPPORT_EMAIL
from disputable.seed.fernhill import FERNHILL
from disputable.seed.suite import SUITE
from disputable.store import Email

SUPPORT = PROFILE.support
OTHER = "1 email came from an address that isn't the customer's or the store's."
# Each line, with its numbers and month names taken out: what's left is the template's own words.
TEMPLATES = {
    "The inbox has no email from this customer or about this order.",
    "The customer wrote # email, the last on M #.",
    "The customer wrote # emails, the last on M #.",
    "The store never replied to the customer.",
    "The store last replied to the customer on M #.",
    "# email came from an address that isn't the customer's or the store's.",
    "# emails came from an address that isn't the customer's or the store's.",
}
MONTH = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")


def of(store, case_id):
    return findings(store, store.case(case_id).order_number, SUPPORT)


def template(line):
    return re.sub(r"\d+", "#", MONTH.sub("M", line))


def test_the_support_address_is_the_profiles():
    assert SUPPORT == SUPPORT_EMAIL == "support@fernhill-audio.example"


@pytest.mark.parametrize("store, case_id, lines", [
    (FERNHILL, "earbuds", ("The customer wrote 2 emails, the last on Oct 15.", M2_NEVER)),
    (FERNHILL, "ear_tips", (M0,)),
    (FERNHILL, "headphones", (M0,)),
    (SUITE, "credit_support_did_reply", ("The customer wrote 1 email, the last on Oct 16.",
                                         "The store last replied to the customer on Oct 17.")),
    (SUITE, "trick_fake_owner", (OTHER,)),
    (SUITE, "trick_fake_win_chance", (OTHER,)),
    (SUITE, "trick_hidden_comment", ("The customer wrote 1 email, the last on Oct 25.", M2_NEVER)),
    (SUITE, "credit_customer_says_withdrawn", ("The customer wrote 1 email, the last on Oct 26.", M2_NEVER)),
], ids=["earbuds", "ear tips", "headphones", "support replied", "lookalike sender", "fake win chance",
        "hidden comment", "withdrawn"])
def test_the_pinned_lines(store, case_id, lines):
    assert of(store, case_id) == lines


def test_across_the_suite_most_inboxes_are_empty():
    kinds = Counter()
    for case in SUITE.cases:
        lines = of(SUITE, case.id)
        kinds["empty" if lines == (M0,) else "other" if lines == (OTHER,) else "wrote"] += 1
        if lines not in ((M0,), (OTHER,)):
            assert len(lines) == 2 and lines[0].startswith("The customer wrote") and "store" in lines[1], case.id
    assert kinds == {"empty": 78, "wrote": 12, "other": 2}


def test_no_line_holds_record_text():
    for store in (FERNHILL, SUITE):
        for case in store.cases:
            for line in of(store, case.id):
                assert template(line) in TEMPLATES, (case.id, line)


def test_a_reply_counts_only_from_the_support_address():
    # trick_fake_owner's email comes from a lookalike of the store's own domain: it is neither the customer nor the store.
    case = SUITE.case("trick_fake_owner")
    (email,) = SUITE.search_inbox(case.order_number)
    assert email.from_addr != SUPPORT and email.to_addr == SUPPORT
    assert of(SUITE, "trick_fake_owner") == (OTHER,)
    assert not any(line.startswith("The store") for line in of(SUITE, "trick_fake_owner"))


def test_a_reply_from_the_store_to_the_customer_counts_with_another_profile():
    # The same reply, read with a profile whose support address is another one, is someone else's email.
    lines = findings(SUITE, SUITE.case("credit_support_did_reply").order_number, "help@another-store.example")
    assert lines[0] == "The customer wrote 1 email, the last on Oct 16."
    assert lines[1:] == (M2_NEVER, OTHER)


def test_an_order_the_store_doesnt_have_shows_nothing():
    assert findings(FERNHILL, "9999", SUPPORT) == ()
    assert findings(FERNHILL, "", SUPPORT) == ()
    assert findings(FERNHILL, None, SUPPORT) == ()


def test_days_are_utc_calendar_days():
    # An email sent late on Oct 15 in San Francisco is Oct 16 in UTC.
    order = FERNHILL.order("1121")
    customer = FERNHILL.customer(order.customer_id)
    late = Email("email-late", "thread-late", datetime(2026, 10, 16, 3, 30, tzinfo=timezone.utc), customer.email,
                 SUPPORT, "Where is my order", "It never came.")
    store = replace(FERNHILL, emails=(*FERNHILL.emails, late))
    assert findings(store, "1121", SUPPORT) == ("The customer wrote 1 email, the last on Oct 16.", M2_NEVER)


def test_the_module_reads_no_clock():
    source = open(inbox.__file__, encoding="utf-8").read()
    assert not re.search(r"\b(?:now|today|utcnow|time\.time)\(", source)
