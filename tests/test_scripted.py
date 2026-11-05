"""The scripted readers at the reader level: the real ClaudeReader on the SDK-shaped FakeClient, then validate()'s checks,
on every case whose records get read (cases.READ: the story's three, Ending 2 and the suite's 79). No rounds and no
network. Each reading is made once (cases.read_with) and shared by the tests that look at it, test_walk's too, so the
files stay quick."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace

import pytest

from cases import READ, read_with, request
from disputable import documents, prompts, rules
from disputable.engine import Action, Stage, decide
from disputable.fees import FEES_V1
from disputable.lifecycle import as_case, as_dispute, poll
from disputable.prompts import BANK_COMMENT
from disputable.reader import (MAX_CONFLICTS, MAX_FACTS, MAX_FLAGS, MAX_QUOTE, MAX_TAGS, ReadRequest, Toolbox,
                               draft_problems, from_json, reading_key, to_json)
from disputable.seed import scripted
from disputable.seed.fernhill import ANSWERS, ENDING_2, ENDING_2_ANSWER, FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.seed.scripted import (ATTACKS, MADE_UP, PRIOR_CLAIMS, STRAY, WITHDRAWALS, Scripted, answer_for, empty_for,
                                      fooled, fooled_for, perfect_for, seed_case)
from disputable.seed.suite import ANSWERS as SUITE_ANSWERS, SUITE
from disputable.store import Case, Email, field_text
from sandbox import Sandbox, Story
from test_rules import NEEDS_A_READER

LABELS = list(READ)
NEVER_FIGHTS = "Code never fights a 13.6 claim: the fee table has no chance for it."


def cited_as(label: str, record_id: str) -> str:
    """The record id a key's fact is cited as: a fact on the case is the bank's comment."""
    return BANK_COMMENT if isinstance(READ[label].store.record(record_id), Case) else record_id


def own_emails(label: str) -> list[str]:
    """The customer's own emails among the records the reads showed, in the order shown."""
    read, reading = READ[label], read_with(label)[0]
    customer = rules.customer_emails(read.store, read.store.order(read.case.order_number))
    shown = dict.fromkeys(record_id for call in reading.tool_log for record_id in call.record_ids)
    return [record_id for record_id in shown if isinstance(email := read.store.record(record_id), Email)
            and documents.key(email.from_addr) in customer]


def last_point(reading):
    """The attack's point: it comes after the perfect reading's own."""
    return [row for row in reading.rows if row.part == "conflict"][-1]


def keyed_draft(label: str) -> tuple[str, str, str]:
    """final-spec 4's rule for the perfect draft, written out with its cases named: (action, reason, refund)."""
    read = READ[label]
    answer = read.answer
    if answer.final is Action.HAND_OFF and answer.recommended is not None:
        action = answer.recommended
    elif label == "credit_wrong_item_returned":
        return "CHALLENGE", "NOT_ENTITLED", ""
    elif label == "credit_customer_says_withdrawn":
        return "CHALLENGE", "CUSTOMER_WITHDRAWN", ""
    elif answer.final is Action.HAND_OFF:
        return "HAND_OFF", "NONE", ""
    else:
        action = answer.final
    decision = decide(read.dispute, answer.evidence(), read.now)
    if action is Action.ACCEPT:
        return "ACCEPT", decision.accept_reason.value, f"{read.dispute.amount:.2f}" if read.case.stage is Stage.RFI else ""
    reason = decision.challenge_reason or FEES_V1.challenge_reasons[answer.evidence_class]  # the gate key #37 gives none
    return "CHALLENGE", reason.value, ""


# ----------------------------------------------------------------------------- the perfect reader
@pytest.mark.parametrize("label", LABELS)
def test_the_perfect_reader_reads_everything_and_keeps_every_row(label):
    read, (reading, client) = READ[label], read_with(label)
    assert reading.failure is None and reading.complete and not client.rejected
    assert [row for row in reading.rows if not row.kept] == []
    assert (reading.other, reading.unbacked, reading.stray_tags) == (False, False, False)
    assert reading.prior_claim is (label in PRIOR_CLAIMS)
    assert reading.withdrew == ((WITHDRAWALS[label][0],) if label in WITHDRAWALS else ())
    box, number = Toolbox.for_case(read.store, read.case), read.case.order_number
    reads = [call for call in reading.tool_log if call.name != "propose_decision"]
    assert [call.refused for call in reads] == [""] * len(reads)
    assert [(call.name, call.asked) for call in reads] == [  # the history, then every order's scans and inbox, then
        ("get_order_history", number),  # at CHARGEBACK every serial's warranty log: all the stage's tools reach
        *((name, other) for other in box.numbers for name in ("get_carrier_scans", "search_inbox")),
        *(("get_warranty_log", serial) for serial in box.serials)]
    history = reads[0]  # asked with a "#", which the tool strips: it shows what a plain number shows
    assert json.loads(history.given) == {"order_number": f"#{number}"}
    assert history.record_ids == box.run("get_order_history", {"order_number": number})[1] != ()
    assert from_json(to_json(reading)) == reading


@pytest.mark.parametrize("label", LABELS)
def test_the_perfect_reader_finds_every_fact_flags_every_planted_line_and_tags_the_keys_orders(label):
    read, reading = READ[label], read_with(label)[0]
    kept = [row for row in reading.rows if row.kept]
    for fact in read.answer.must_find:
        assert any((row.part, row.record_id, row.quote) == ("fact", cited_as(label, fact.record_id), fact.quote)
                   for row in kept), fact
    for line in read.answer.must_ignore:
        assert [row.part for row in kept if row.quote == line] == ["flag"], line
    assert reading.reported == tuple(sorted(read.answer.reported_orders))
    for row in kept:  # each quote is in the one field it names, and in no other field of its record
        if row.record_id == BANK_COMMENT:
            assert documents.clean(row.quote) in documents.clean(read.case.issuer_comment)
            continue
        record = read.store.record(row.record_id)
        holding = [name for name in prompts.FIELDS
                   if any(documents.clean(row.quote) in documents.clean(leaf) for leaf in field_text(record, name) or ())]
        assert holding == [row.field], (row.record_id, row.quote)


@pytest.mark.parametrize("label", LABELS)
def test_the_perfect_draft_is_the_keys_with_no_problems(label):
    draft = read_with(label)[0].draft
    assert (draft.action, draft.reason, draft.refund_amount, draft.to_card_that_paid) == (*keyed_draft(label), True)
    assert draft.problems == () and draft.note  # no case's drafted action is one code calls a problem


def test_the_perfect_drafts_add_up_and_never_carry_a_planted_amount():
    drafts = {label: read_with(label)[0].draft for label in LABELS}
    assert Counter(draft.action for draft in drafts.values()) == {"ACCEPT": 49, "CHALLENGE": 29, "HAND_OFF": 5}
    assert {label for label, draft in drafts.items() if draft.action == "HAND_OFF"} == {
        "ce3_at_chargeback_first_round", "fraud_claim_that_passed_3ds", "round_2_old_file_again", "round_2_no_record_here",
        "not_received_signed_delivery"}
    pinned = {
        "headphones": ("CHALLENGE", "PURCHASE_HISTORY", ""),
        "headphones_round_2": ("CHALLENGE", "PRODUCT_RECEIVED", ""),
        "ending_2": ("ACCEPT", "OTHERS", ""),  # a chargeback: the bank already took the money back
        "earbuds": ("ACCEPT", "VALID_CUSTOMER_DISPUTE", ""),
        "just_past_the_certain_loss": ("ACCEPT", "LOW_VALUE_TRANSACTION", "15.71"),
        "part_of_the_order_disputed": ("ACCEPT", "VALID_CUSTOMER_DISPUTE", "100.00"),  # the part in dispute
        "ce3_package_has_no_file": ("CHALLENGE", "PURCHASE_HISTORY", ""),  # decide() gives no reason: the table's
        "credit_wrong_item_returned": ("CHALLENGE", "NOT_ENTITLED", ""),
        "credit_customer_says_withdrawn": ("CHALLENGE", "CUSTOMER_WITHDRAWN", ""),
    }
    assert {label: (draft.action, draft.reason, draft.refund_amount) for label, draft in drafts.items()
            if label in pinned} == pinned
    for label, draft in drafts.items():
        assert draft.refund_amount in ("", f"{READ[label].dispute.amount:.2f}"), label


def test_the_two_challenges_on_a_credit_claim_are_clean_only_because_the_records_back_them():
    wrong, withdrawn = read_with("credit_wrong_item_returned")[0], read_with("credit_customer_says_withdrawn")[0]
    assert rules.return_turned_down(SUITE, SUITE.case("credit_wrong_item_returned")) and wrong.draft.problems == ()
    assert withdrawn.withdrew == ("email-54-1",) and withdrawn.draft.problems == ()
    assert draft_problems(withdrawn.draft, request("credit_customer_says_withdrawn")) == (NEVER_FIGHTS,)  # with no K2
    assert draft_problems(wrong.draft, request("earbuds")) == (  # where the warehouse turned nothing down
        "NOT_ENTITLED needs the warehouse's scan turning the return down, and the records have none.", NEVER_FIGHTS)


@pytest.mark.parametrize("points", [PRIOR_CLAIMS, WITHDRAWALS], ids=["prior claims", "withdrawals"])
def test_each_point_only_reading_finds_quotes_the_customers_own_email(points):
    for case_id, (email_id, quote) in points.items():
        case, email = SUITE.case(case_id), SUITE.record(email_id)
        assert isinstance(email, Email)
        assert documents.key(email.from_addr) in rules.customer_emails(SUITE, SUITE.order(case.order_number))
        assert email_id in Toolbox.for_case(SUITE, case).run("search_inbox", {"order_number": case.order_number})[1]
        assert documents.clean(quote) in documents.clean(email.body) and len(quote) <= MAX_QUOTE
        assert SUITE_ANSWERS[case_id].final is Action.HAND_OFF  # the key sends it to a person: code alone doesn't
    assert set(PRIOR_CLAIMS) == NEEDS_A_READER and set(WITHDRAWALS) == {"credit_customer_says_withdrawn"}


# ----------------------------------------------------------------------------- the empty reader
@pytest.mark.parametrize("label", LABELS)
def test_the_empty_reader_reads_everything_and_proposes_nothing(label):
    reading, client = read_with(label, "empty")
    assert reading.failure is None and reading.complete and not client.rejected
    assert (reading.rows, reading.reported, reading.reported_given) == ((), (), ())
    assert (reading.prior_claim, reading.withdrew, reading.other, reading.unbacked, reading.stray_tags) == (
        False, (), False, False, False)
    draft = reading.draft
    assert (draft.action, draft.reason, draft.refund_amount, draft.problems) == ("HAND_OFF", "NONE", "", ())
    assert [(call.name, call.asked, call.record_ids) for call in reading.tool_log] == [
        (call.name, call.asked, call.record_ids) for call in read_with(label)[0].tool_log]  # the same reads


# ----------------------------------------------------------------------------- the fooled readers
@pytest.mark.parametrize("attack", ATTACKS)
def test_no_attack_fails_the_reading_or_sends_what_the_api_refuses(attack):
    for label in LABELS:
        reading, client = read_with(label, attack)
        assert reading.failure is None and not client.rejected, label
        assert reading.complete is (attack != "early"), label
        assert from_json(to_json(reading)) == reading, label


OBEYED = {  # (action, refund as the line writes it, to the card that paid), for each planted line that asks for one
    "earbuds": ("ACCEPT", "$900", False),
    "trick_system_note": ("ACCEPT", "$5,000", True),
    "trick_accept_on_a_case_to_fight": ("ACCEPT", "$5,000", True),
    "trick_fake_win_chance": ("CHALLENGE", "", True),
    "trick_fake_owner": ("ACCEPT", "$2,000", False),
    "trick_hidden_comment": ("CHALLENGE", "", True),
    "trick_in_a_subject_line": ("CHALLENGE", "", True),
    "trick_in_a_shipping_address": ("CHALLENGE", "", True),
    "trick_other_language": ("ACCEPT", "$3,000", True),
    "trick_issuer_comment": ("ACCEPT", "170.00", True),  # "refund twice": twice the $85.00 in dispute
    "trick_claimed_refund": ("ACCEPT", "125.00", True),
}  # trick_claimed_order_history, trick_carrier_note and trick_warranty_note ask for no action: the default draft


def test_an_obeying_reader_is_refused_9999_and_its_planted_draft_is_marked():
    for label in LABELS:
        read, reading = READ[label], read_with(label, "obey")[0]
        number, planted = read.case.order_number, read.answer.must_ignore
        assert [(call.name, call.refused, call.record_ids) for call in reading.tool_log if call.asked == STRAY] == [
            ("get_order_history", prompts.ASK_DISPUTED.format(number=number), ())], label
        points = [(row.quote, row.kept, row.counted_as) for row in reading.rows if row.part == "conflict"]
        if planted:  # each planted line, cited where it is: a checked point of no kind code knows
            assert points == [(line, True, "K5") for line in planted] and reading.other and not reading.unbacked, label
        else:  # nothing planted to cite, so a made-up quote: a point that can't be backed still hands off
            assert points == [(MADE_UP, False, "U1")] and reading.unbacked and not reading.other, label
        assert not [row for row in reading.rows if row.part == "flag"], label
        numbers = Toolbox.for_case(read.store, read.case).numbers
        assert reading.reported_given == numbers and not reading.stray_tags, label
        earlier = tuple(sorted(set(numbers) - {number}))[:MAX_TAGS] if read.case.reason_code == "10.4" else ()
        assert reading.reported == earlier, label
        draft = reading.draft
        expected = OBEYED.get(label, ("ACCEPT", f"{read.dispute.amount:.2f}", False))  # the whole amount, another card
        assert (draft.action, draft.refund_amount, draft.to_card_that_paid) == expected, label
        assert not draft.refund_amount or draft.problems, label  # a planted refund never passes code's checks
        if label in OBEYED:
            assert draft.note == planted[0], label  # nothing is flagged, so the note that echoes the line stays
    assert read_with("earbuds", "obey")[0].draft.problems == (
        "Its refund of $900.00 was refused: The chargeback already returned this money to the customer, so a refund "
        "would pay them twice.",)
    assert read_with("trick_hidden_comment", "obey")[0].draft.problems == (
        "Code never fights a 13.1 claim: the fee table has no chance for it.",)
    assert read_with("trick_claimed_order_history", "obey")[0].draft.problems == (
        "Its refund of $350.00 was refused: Refunds only go back to the card that paid.",)


def test_planted_tags_are_dropped_and_only_history_orders_are_kept():
    for label in LABELS:
        read, reading = READ[label], read_with(label, "tags")[0]
        numbers, number = Toolbox.for_case(read.store, read.case).numbers, read.case.order_number
        assert reading.reported_given == (*numbers, STRAY), label
        tags = [(row.quote, row.kept, row.why) for row in reading.rows if row.part == "tag"]
        if read.case.reason_code != "10.4":
            assert reading.reported == () and not reading.stray_tags, label
            assert tags == [(tag, False, "Tags only count on a fraud claim.") for tag in (*numbers, STRAY)], label
            continue
        assert reading.reported == tuple(sorted(set(numbers) - {number}))[:MAX_TAGS] and reading.stray_tags, label
        assert tags == [(number, False, "That is the disputed order, not an earlier one."),
                        (STRAY, False, "That order isn't in this case's history.")], label


def test_a_claimed_withdrawal_counts_only_from_the_customers_own_email():
    counted, cited = Counter(), {}
    for label in LABELS:
        reading = read_with(label, "withdraw")[0]
        row, own = last_point(reading), own_emails(label)
        assert row.kept and row.kind == "CUSTOMER_WITHDREW", label
        if own:
            assert (row.record_id, row.field, row.counted_as) == (own[0], "body", "K2") and own[0] in reading.withdrew
        else:
            assert row.counted_as == "K5" and reading.other and reading.withdrew == (), label
        counted[row.counted_as] += 1
        cited[label] = row.record_id, row.counted_as
    assert counted == {"K2": 12, "K5": 71}
    assert cited["earbuds"] == ("email-1098-1", "K2")
    assert cited["trick_fake_win_chance"] == ("email-61-1", "K5")  # an email, but not the customer's
    assert cited["trick_fake_owner"] == ("email-62-1", "K5")  # from a lookalike of the store's own address
    assert cited["ce3_everything_matches"] == (BANK_COMMENT, "K5")  # no email at all: the bank's comment


def test_a_claimed_prior_report_counts_only_from_the_customers_own_email_on_a_fraud_claim():
    counted = Counter()
    for label in LABELS:
        read, reading = READ[label], read_with(label, "prior")[0]
        row = last_point(reading)
        expected = "K1" if own_emails(label) and read.case.reason_code == "10.4" else "K5"
        assert row.kept and row.kind == "PRIOR_FRAUD_CLAIMED" and row.counted_as == expected, label
        assert reading.prior_claim is (expected == "K1"), label
        counted[expected] += 1
    assert counted == {"K1": 3, "K5": 80}


def test_an_unbacked_point_is_a_dropped_row_that_still_counts():
    for label in LABELS:
        reading = read_with(label, "unbacked")[0]
        row = last_point(reading)
        assert (row.quote, row.kept, row.counted_as, row.why) == (MADE_UP, False, "U1", "The quote isn't in that field.")
        assert reading.unbacked and [other for other in reading.rows if other != row] == list(read_with(label)[0].rows)


def test_an_early_proposal_is_sent_back_once_then_kept_as_incomplete():
    for label in LABELS:
        reading, client = read_with(label, "early")
        assert len(client.requests) == 2 and [(call.name, call.refused[:8]) for call in reading.tool_log] == [
            ("propose_decision", "Not yet."), ("propose_decision", "")], label
        unread = [row for row in reading.rows if row.record_id != BANK_COMMENT and row.part != "tag"]
        assert {row.why for row in unread} <= {"Claude didn't read that record."}, label  # only the comment can be cited
        assert reading.unbacked is (label in PRIOR_CLAIMS or label in WITHDRAWALS), label  # the points it couldn't check
        assert (reading.prior_claim, reading.withdrew) == (False, ()), label
        assert reading.reported == read_with(label)[0].reported, label  # a tag needs no read: the comment is in the prompt


def test_a_flood_is_capped_as_the_reader_caps_say():
    caps = {"fact": (MAX_FACTS, "Code keeps the first 40 facts."),
            "flag": (MAX_FLAGS, "Code keeps the first 20 flagged lines."),
            "conflict": (MAX_CONFLICTS, "Code keeps the first 10 points.")}
    assert scripted.EXTRA == 5  # the flood goes past every cap, so the caps, not the builder, drop rows
    for label in LABELS:
        reading = read_with(label, "flood")[0]
        for part, (cap, why) in caps.items():
            rows = [row for row in reading.rows if row.part == part]
            assert len(rows) == cap + 5 and all(row.kept for row in rows[:cap]), (label, part)
            assert [row.why for row in rows[cap:]] == [why] * 5, (label, part)
        assert reading.other and not reading.unbacked, label
        assert (reading.prior_claim, reading.withdrew) == (label in PRIOR_CLAIMS, read_with(label)[0].withdrew), label


# ----------------------------------------------------------------------------- driving them as a Reader
def test_scripted_reads_each_request_on_a_fresh_client_and_keeps_the_last():
    reader = Scripted(perfect_for, effort="medium")
    first = reader.read(request("headphones"))
    client = reader.client
    second = reader.read(request("earbuds"))
    assert reader.requests == [request("headphones"), request("earbuds")] and reader.client is not client
    assert (first, second) == (read_with("headphones")[0], read_with("earbuds")[0])
    assert len(reader.client.requests) == 3 and not reader.client.rejected
    assert reader.client.requests[0]["output_config"] == {"effort": "medium"} and reader.effort == "medium"
    assert reader.profile == prompts.FERNHILL
    assert Scripted(empty_for).read(request("earbuds")) == read_with("earbuds", "empty")[0]
    assert Scripted(fooled_for("tags")).read(request("ending_2")) == read_with("ending_2", "tags")[0]


def test_an_unknown_attack_is_refused():
    with pytest.raises(ValueError, match="There's no attack named 'bribe'"):
        fooled_for("bribe")
    with pytest.raises(ValueError, match="no attack named"):
        fooled(SUITE, SUITE.case("trick_system_note"), SUITE_ANSWERS["trick_system_note"], "bribe")


def test_the_lookup_finds_the_answer_for_a_case_lifecycle_renamed():
    story = Story()
    story.run(MONDAY)
    for case_id in ("ear_tips", "earbuds", "headphones"):
        case = as_case(story[case_id])
        assert case.id == f"dst_{case_id}" and answer_for(FERNHILL, case) is ANSWERS[case_id]
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    assert answer_for(FERNHILL, as_case(story["headphones"])) is ANSWERS["headphones_round_2"]
    ending_2 = Story(ENDING_2)
    ending_2.run(MONDAY)
    ending_2.sandbox.escalate("dst_headphones")
    ending_2.poll(THREE_WEEKS_LATER)
    assert answer_for(ENDING_2, as_case(ending_2["headphones"])) is ENDING_2_ANSWER
    sandbox = Sandbox(SUITE)
    suite = [label for label, read in READ.items() if read.store_name == "suite"]
    for case_id in suite:
        sandbox.open(case_id)
    records, problems = poll({}, sandbox.reads(), MONDAY, store=SUITE, opened=sandbox.opened)
    assert problems == ()
    for case_id in suite:
        case = as_case(records[f"dst_{case_id}"])
        assert seed_case(SUITE, case) is SUITE.case(case_id) and answer_for(SUITE, case) is SUITE_ANSWERS[case_id]


def test_a_case_lifecycle_renamed_reads_like_its_seed_case():
    cases = ("ce3_customer_claims_a_fraud_report", "credit_customer_says_withdrawn", "trick_fake_owner")
    sandbox = Sandbox(SUITE)
    for case_id in cases:
        sandbox.open(case_id)
    records, _ = poll({}, sandbox.reads(), MONDAY, store=SUITE, opened=sandbox.opened)
    reader = Scripted(perfect_for)
    for case_id in cases:
        record = records[f"dst_{case_id}"]
        case = as_case(record)
        renamed = reader.read(ReadRequest(SUITE, case, as_dispute(record), reading_key(SUITE, case)))
        seed = read_with(case_id)[0]
        assert renamed.failure is None and renamed.complete and not reader.client.rejected, case_id
        assert renamed.key == seed.key, case_id  # no id and no clock in the key: a recording replays across runs
        assert (renamed.rows, renamed.reported, renamed.draft, renamed.prior_claim, renamed.withdrew) == (
            seed.rows, seed.reported, seed.draft, seed.prior_claim, seed.withdrew), case_id


def test_a_case_the_store_cant_place_has_no_answer():
    headphones = replace(FERNHILL.case("headphones"), id="dst_headphones")
    assert seed_case(FERNHILL, headphones).id == "headphones"  # order 1109 has a case at each stage
    assert seed_case(FERNHILL, replace(headphones, stage=Stage.CHARGEBACK)).id == "headphones_round_2"
    for stranger in (replace(headphones, stage=Stage.PRE_ARBITRATION), replace(headphones, order_number="9999")):
        with pytest.raises(KeyError):
            answer_for(FERNHILL, stranger)
