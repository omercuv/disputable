"""The case state machine, run on a fake sandbox that answers the way the Airwallex sandbox did on 2026-10-30."""

from __future__ import annotations

import copy
import functools
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

from disputable import ce3, lifecycle
from disputable.documents import purchase_history
from disputable.engine import Action, Evidence, Stage, Status, decide
from disputable.fees import ZERO, EvidenceClass, money
from disputable.jsonable import Unloadable, canonical, dump
from disputable.ledger import Ledger
from disputable.lifecycle import (ACTOR, FRESH, NO_ORDER, ONE_CE3_ATTEMPT, Phase, Refused, Unreadable, approve, as_case,
                                  as_dispute, at_risk, blocker, book, cost, entries, from_json, keep, observe,
                                  person_reason, phase, poll, read, rejected, request_id_for, send_failed, sending, taken,
                                  to_json)
from disputable.seed.fernhill import (ANSWERS, ENDING_2, ENDING_2_ANSWER, FERNHILL, FIRST_PACKAGE_FACTS, FIRST_PACKAGE_FILE,
                                      MONDAY, REJECTION_COMMENT, SECOND_PACKAGE_FILE, THREE_WEEKS_LATER, WARRANTY_SIGN_UP)

RUN_DATE = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)  # the sandbox's created_at: when the run made the dispute
MINUTE = timedelta(minutes=1)
FIRST_PACKAGE = (FIRST_PACKAGE_FACTS, frozenset({FIRST_PACKAGE_FILE}))
NOTHING = (frozenset(), frozenset())
SOMEONE = "priya@fernhill.example"  # a person answering in Airwallex's web app
HELD = ("The payment showed a new refund while this app's response was being sent. It may be this response's own, so it "
        "isn't sent again.")
LATE_REFUND = "This app's RFI refund shows now, and this app booked it. A person checks that it isn't booked twice."
OTHER_REFUNDS = "The payment shows refunds this app didn't make. A person books them."
SENT_AND_CHANGED = "The refunds so far changed while a response was being sent. A person checks what Airwallex has."
REFUND_FELL = ("The payment shows less refunded than an earlier reading did, so this reading may be an old copy. A person "
               "checks it.")

# As in test_engine.py: GET /pa/payment_disputes/{id} after the simulated issuer rejected the first challenge.
RECORDED_DISPUTE = {
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
RECORDED_PAYMENT = {
    "id": "int_sgpvb7qw4hnpdl9vi5b",
    "amount": 1249,
    "currency": "USD",
    "status": "SUCCEEDED",
    "captured_amount": 1249,
    "latest_payment_attempt": {"captured_amount": 1249, "refunded_amount": 0},
}
RECORDED_FIRST_ROUND_KEYS = {"id", "stage", "status", "amount", "currency", "payment_intent_id", "reason", "accept_details",
                             "challenge_details", "due_at"}  # test_engine.py's RECORDED_EAR_TIPS


class Sandbox:
    """Answers the way the Airwallex sandbox did on 2026-10-30, as plain dicts, and plays the simulated issuer."""

    def __init__(self, store=FERNHILL):
        self.store, self.disputes, self.payments, self.opened, self.ticks = store, {}, {}, {}, 0

    def open(self, case_id, now=MONDAY, dispute_id=None, payment_id=None):
        case = self.store.case(case_id)
        order = self.store.order(case.order_number)
        dispute_id, payment_id = dispute_id or f"dst_{case_id}", payment_id or f"int_{case_id}"
        amount = int(order.amount)
        self.payments.setdefault(payment_id, {
            "id": payment_id, "amount": amount, "currency": "USD", "status": "SUCCEEDED", "captured_amount": amount,
            "created_at": RUN_DATE.isoformat(), "metadata": {"order_no": order.number},
            "latest_payment_attempt": {"captured_amount": amount, "refunded_amount": 0}})
        self.disputes[dispute_id] = {
            "id": dispute_id, "stage": case.stage.value, "status": "REQUIRES_RESPONSE", "amount": amount, "currency": "USD",
            "payment_intent_id": payment_id, "reason": {"original_code": case.reason_code}, "accept_details": [],
            "challenge_details": [], "due_at": (now + case.due_in).isoformat(), "created_at": RUN_DATE.isoformat(),
            "issuer_comment": case.issuer_comment, "refunds": None}
        self.opened[dispute_id] = case.opened_at  # what the simulated issuer knows and the sandbox doesn't
        self.change(dispute_id)
        return dispute_id

    def reads(self):
        return [(copy.deepcopy(d), copy.deepcopy(self.payments[d["payment_intent_id"]])) for d in self.disputes.values()]

    def change(self, dispute_id, **fields):
        """Anything Airwallex changes on a dispute. Every change moves updated_at."""
        self.ticks += 1
        self.disputes[dispute_id].update(fields, updated_at=(RUN_DATE + timedelta(seconds=self.ticks)).isoformat())

    def pay(self, dispute_id, refunded=None, order_no=None):
        payment = self.payments[self.disputes[dispute_id]["payment_intent_id"]]
        if refunded is not None:
            payment["latest_payment_attempt"]["refunded_amount"] = refunded
        if order_no is not None:
            payment["metadata"]["order_no"] = order_no

    def reply(self, dispute_id, action, by, reason, stage=None, **extra):
        d = self.disputes[dispute_id]
        key, who = ("accept_details", "accepted_by") if action is Action.ACCEPT else ("challenge_details", "challenged_by")
        d[key] = [*(d.get(key) or []), {"stage": stage or d["stage"], "reason": reason, who: by, **extra}]
        self.change(dispute_id)

    def post(self, record, show=True):
        """What /accept or /challenge did. That's the HTTP step's job: it's played here. show=False: the status moves, but
        the entry doesn't show yet."""
        plan, d = record.rounds[-1].plan, self.disputes[record.dispute_id]
        assert (d["stage"], d["status"]) == (plan.terms.stage.value, "REQUIRES_RESPONSE")
        if plan.terms.action is Action.ACCEPT:
            if plan.terms.refund_amount:
                self.payments[d["payment_intent_id"]]["latest_payment_attempt"]["refunded_amount"] += int(
                    plan.terms.refund_amount)
                d["refunds"] = [{"id": f"rfd_{record.dispute_id}"}]
            if show:
                self.reply(record.dispute_id, Action.ACCEPT, ACTOR, plan.terms.reason)
            self.change(record.dispute_id, status="ACCEPTED")
        else:
            if show:
                self.reply(record.dispute_id, Action.CHALLENGE, ACTOR, plan.terms.reason)
            # The POST answered CHARGEBACK/CHALLENGED, but the next GET showed the bank deciding.
            self.change(record.dispute_id, **({"stage": "PRE_ARBITRATION", "status": "PENDING_DECISION"}
                                              if d["stage"] == "CHARGEBACK" else {"status": "CHALLENGED"}))

    def escalate(self, dispute_id, case_id="headphones_round_2", now=THREE_WEEKS_LATER):
        """The simulated issuer turns the response down: a later stage, a new due date and the bank's comment."""
        case = self.store.case(case_id)
        self.change(dispute_id, stage=case.stage.value, status="REQUIRES_RESPONSE", due_at=(now + case.due_in).isoformat(),
                    issuer_comment=case.issuer_comment)

    def resolve(self, dispute_id, status):
        self.change(dispute_id, status=status.value)


class Story:
    """The app's passes over the queue: read, decide, approve, read again, send. Priya approves what code drafts."""

    def __init__(self, store=FERNHILL, cases=("ear_tips", "earbuds", "headphones"), accept_everything=False):
        self.store, self.sandbox, self.records, self.accept_everything = store, Sandbox(store), {}, accept_everything
        for case_id in cases:
            self.sandbox.open(case_id)

    def __getitem__(self, case_id):
        return self.records[f"dst_{case_id}"]

    def poll(self, now):
        self.records, problems = poll(self.records, self.sandbox.reads(), now, store=self.store, opened=self.sandbox.opened)
        for record in self.records.values():  # every step survives a restart
            assert to_json(from_json(to_json(record))) == to_json(record)
        return problems

    def evidence(self, dispute_id, record):
        if self.accept_everything:
            return Evidence(EvidenceClass.NONE)
        case_id = dispute_id.removeprefix("dst_")
        if case_id == "headphones" and len(record.rounds) > 1:
            return (ANSWERS["headphones_round_2"] if self.store.warranty else ENDING_2_ANSWER).evidence()
        return ANSWERS[case_id].evidence()

    def decide(self, now):
        for dispute_id, record in list(self.records.items()):
            if phase(record) is Phase.DECIDE:
                first = decide(as_dispute(record), None, now)
                evidence = self.evidence(dispute_id, record) if first.action is Action.GATHER_EVIDENCE else None
                self.records[dispute_id] = keep(record, evidence, now, round_number=len(record.rounds))

    def approve(self, now, by="Priya"):
        for dispute_id, record in list(self.records.items()):
            if phase(record) is Phase.APPROVE:
                self.records[dispute_id] = approve(record, record.rounds[-1].plan, by, now)

    def send(self, now, land=True):
        for dispute_id, record in list(self.records.items()):
            if blocker(record, now) is None:
                record = sending(record, now)
                if land:
                    self.sandbox.post(record)
                self.records[dispute_id] = taken(record, record.rounds[-1].plan.request_id, now)

    def run(self, now):
        self.poll(now)
        self.decide(now)
        self.approve(now)
        self.poll(now + MINUTE)
        self.send(now + MINUTE)
        self.poll(now + 2 * MINUTE)


def play(store=FERNHILL, outcome=Status.WON, accept_everything=False):
    story = Story(store, accept_everything=accept_everything)
    story.run(MONDAY)
    if story["headphones"].closed is None:
        story.sandbox.escalate("dst_headphones")
        story.run(THREE_WEEKS_LATER)
        if phase(story["headphones"]) is Phase.WAIT:
            story.sandbox.resolve("dst_headphones", outcome)
            story.run(THREE_WEEKS_LATER + timedelta(days=1))
    return story


def at_round_two(store=FERNHILL):
    story = Story(store)
    story.run(MONDAY)
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    return story


def one_case(case_id="headphones", now=MONDAY):
    story = Story(cases=(case_id,))
    story.poll(now)
    return story


def waiting_for_priya():
    story = one_case()
    story.decide(MONDAY)
    assert phase(story["headphones"]) is Phase.APPROVE
    return story


def being_sent(case_id="headphones", captured=None):
    """The plan's first POST try is saved: Airwallex hasn't answered yet. captured: the order's total, when the disputed
    item is only part of it."""
    story = Story(cases=(case_id,))
    if captured is not None:
        story.sandbox.payments[f"int_{case_id}"]["captured_amount"] = captured
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.approve(MONDAY)
    story.poll(MONDAY + MINUTE)
    story.records[f"dst_{case_id}"] = sending(story[case_id], MONDAY + MINUTE)
    return story


def round_two_being_sent():
    """Round two's warranty challenge: approved, and its first POST try saved."""
    story = at_round_two()
    story.decide(THREE_WEEKS_LATER)
    story.approve(THREE_WEEKS_LATER)
    story.poll(THREE_WEEKS_LATER + MINUTE)
    story.records["dst_headphones"] = sending(story["headphones"], THREE_WEEKS_LATER + MINUTE)
    return story


def nobody_answered_round_one():
    """Nobody answered the headphones RFI, the chargeback opened, and Priya approved round two's warranty challenge."""
    later = MONDAY + timedelta(days=7)
    story = one_case()
    story.sandbox.escalate("dst_headphones", now=later)
    story.poll(later)
    story.decide(later)
    story.approve(later)
    assert phase(story["headphones"]) is Phase.SEND
    return story, later


def keys(record):
    return [booking.key for booking in record.bookings]


def ledger_of(*steps):
    ledger = Ledger()
    for step, *amount in steps:
        getattr(ledger, step)(*map(money, amount))
    return tuple(ledger.entries)


def with_plan(record, **changes):
    current = record.rounds[-1]
    return replace(record, rounds=(*record.rounds[:-1], replace(current, plan=replace(current.plan, **changes))))


# ----------------------------------------------------------------------------- the fake sandbox
def test_the_fake_sandbox_speaks_like_the_recordings():
    sandbox = Sandbox()
    sandbox.open("headphones")
    dispute, payment = sandbox.reads()[0]
    assert set(RECORDED_DISPUTE) | RECORDED_FIRST_ROUND_KEYS <= set(dispute)
    assert set(RECORDED_PAYMENT) <= set(payment)
    assert dispute["created_at"] == RUN_DATE.isoformat()  # the run date, never the seed's


# ----------------------------------------------------------------------------- Priya's Monday and both endings
def test_monday_queue_is_three_cases_and_1450_at_risk():
    story = Story()
    assert not story.poll(MONDAY)
    assert sorted(story.records) == ["dst_ear_tips", "dst_earbuds", "dst_headphones"]  # round two is the same dispute
    assert sum(at_risk(record) for record in story.records.values()) == money(1450)
    assert {phase(record) for record in story.records.values()} == {Phase.DECIDE}
    assert keys(story["earbuds"]) == ["chargeback_fee"]


def test_only_the_ear_tips_warn_on_monday():
    story = Story()
    story.poll(MONDAY)
    warned = {dispute_id for dispute_id, record in story.records.items()
              if decide(as_dispute(record), None, MONDAY).deadline_warning}
    assert warned == {"dst_ear_tips"}


@pytest.mark.parametrize("store, outcome, accept_everything, total", [
    (FERNHILL, Status.WON, False, "279.30"),
    (FERNHILL, Status.LOST, False, "1778.30"),
    (ENDING_2, None, False, "1512.30"),  # approving code's own accept costs no review, as weigh() prices it
    (FERNHILL, None, True, "1465.60"),
], ids=["ending 1 won", "ending 1 lost", "ending 2", "accept everything"])
def test_the_queue_costs_what_the_story_says(store, outcome, accept_everything, total):
    story = play(store, outcome, accept_everything)
    assert {phase(record) for record in story.records.values()} == {Phase.CLOSED}
    assert sum(cost(record) for record in story.records.values()) == Decimal(total)
    assert sum(at_risk(record) for record in story.records.values()) == ZERO


def test_each_case_books_what_the_ledger_helpers_do():
    story = play()
    assert entries(story["ear_tips"]) == ledger_of(("accepted_at_rfi", 12))
    assert entries(story["earbuds"]) == ledger_of(("chargeback_opened",), ("reviewed",), ("accepted_at_chargeback", 189))
    assert entries(story["headphones"]) == ledger_of(("reviewed",), ("chargeback_opened",), ("reviewed",))


def test_losing_the_headphones_adds_the_amount_and_the_later_stage_fees():
    story = play(outcome=Status.LOST)
    assert entries(story["headphones"]) == ledger_of(("reviewed",), ("chargeback_opened",), ("reviewed",),
                                                     ("lost_after_fighting", 1249))


def test_the_earbuds_draft_costs_a_review_because_it_was_a_hand_off():
    earbuds = play()["earbuds"]
    plan = earbuds.rounds[0].plan
    assert plan.hand_off and plan.approved_by == "Priya"
    assert (plan.terms.action, plan.terms.reason, plan.terms.refund_amount) == (Action.ACCEPT, "VALID_CUSTOMER_DISPUTE",
                                                                                ZERO)
    assert keys(earbuds) == ["chargeback_fee", "review:CHARGEBACK", "outcome"]


def test_approving_codes_own_accept_costs_no_review():
    headphones = play(ENDING_2)["headphones"]
    plan = headphones.rounds[-1].plan
    assert not plan.hand_off and plan.needs_approval and plan.approved_by == "Priya"
    assert (plan.terms.action, plan.terms.reason, plan.terms.refund_amount) == (Action.ACCEPT, "OTHERS", ZERO)
    assert keys(headphones) == ["review:RFI", "chargeback_fee", "outcome"]


def test_accepting_everything_runs_through_the_same_approvals():
    story = play(accept_everything=True)
    assert [record.rounds[0].plan.approved_by for record in story.records.values()] == [None, "Priya", "Priya"]
    assert not any(key.startswith("review") for record in story.records.values() for key in keys(record))


# ----------------------------------------------------------------------------- three weeks later: the bank pushes back
def test_the_push_back_rejects_exactly_round_ones_package():
    record = at_round_two()["headphones"]
    seed = FERNHILL.case("headphones_round_2")
    assert rejected(record) == FIRST_PACKAGE == (seed.rejected_fact_ids, seed.rejected_file_hashes)
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK]
    assert record.rounds[-1].issuer_comment == REJECTION_COMMENT
    assert phase(record) is Phase.DECIDE
    assert keys(record) == ["review:RFI", "chargeback_fee"]


def test_round_two_doesnt_show_before_the_bank_pushes_back():
    story = Story()
    story.run(MONDAY)
    story.poll(THREE_WEEKS_LATER - timedelta(days=1))
    assert len(story["headphones"].rounds) == 1 and phase(story["headphones"]) is Phase.WAIT
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    assert sorted(story.records) == ["dst_ear_tips", "dst_earbuds", "dst_headphones"]
    assert len(story["headphones"].rounds) == 2


def test_round_two_reads_like_the_seeds_round_two():
    mine, seed = as_dispute(at_round_two()["headphones"]), FERNHILL.dispute("headphones_round_2", THREE_WEEKS_LATER)
    for field in ("stage", "status", "amount", "currency", "reason_code", "due_at", "captured_amount", "refunded_amount",
                  "prior_challenges", "rejected_fact_ids", "rejected_file_hashes"):
        assert getattr(mine, field) == getattr(seed, field), field


@pytest.mark.parametrize("store, answer, action, advantage", [
    (FERNHILL, ANSWERS["headphones_round_2"], Action.CHALLENGE, "408.55"),
    (ENDING_2, ENDING_2_ANSWER, Action.ACCEPT, "-41.15"),
    (FERNHILL, ENDING_2_ANSWER, Action.ACCEPT, None),  # round one's package again, with the warranty log there unread
], ids=["ending 1", "ending 2", "round one's package"])
def test_round_two_reaches_both_endings_through_the_record(store, answer, action, advantage):
    record = at_round_two(store)["headphones"]
    decision = decide(as_dispute(record), answer.evidence(), THREE_WEEKS_LATER)
    kept = keep(record, answer.evidence(), THREE_WEEKS_LATER, round_number=2)
    assert kept.rounds[-1].plan.terms.action is decision.action is action
    if advantage:
        assert decision.economics.fight_advantage == Decimal(advantage)
    else:
        assert "no new verified fact" in decision.reasons[0]


def test_reusing_priyas_approval_is_void_because_the_stage_changed():
    record = at_round_two()["headphones"]
    used = record.rounds[0].plan
    void = "^This approval is void: the stage changed from RFI to CHARGEBACK.$"
    with pytest.raises(Refused, match=void):
        approve(record, used, "Priya", THREE_WEEKS_LATER)
    planned = keep(record, ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER, round_number=2)
    with pytest.raises(Refused, match=void):
        approve(planned, used, "Priya", THREE_WEEKS_LATER)
    assert planned.bookings == record.bookings
    assert phase(planned) is Phase.APPROVE


def test_pending_decision_after_the_chargeback_challenge_is_not_a_rejection():
    story = at_round_two()
    story.run(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert (record.last.dispute.stage, record.last.dispute.status) == (Stage.PRE_ARBITRATION, Status.PENDING_DECISION)
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK]
    assert rejected(record) == FIRST_PACKAGE
    assert phase(record) is Phase.WAIT and person_reason(record) is None
    assert keys(record) == ["review:RFI", "chargeback_fee", "review:CHARGEBACK"]


def test_round_two_counts_ce3_days_from_the_first_dispute():
    case = as_case(at_round_two()["headphones"])
    assert case.opened_at == FERNHILL.case("headphones").opened_at != RUN_DATE
    days = {prior.order.number: prior.days_before_dispute for prior in ce3.check(FERNHILL, case, ["1038"]).priors}
    seeds = {prior.order.number: prior.days_before_dispute
             for prior in ce3.check(FERNHILL, FERNHILL.case("headphones_round_2"), ["1038"]).priors}
    assert (days["1012"], seeds["1012"]) == (338, 364)
    assert ce3.evidence(ce3.check(FERNHILL, case, ["1038"]), case).evidence_class is EvidenceClass.NONE


def test_a_chargeback_after_an_rfi_nobody_answered_has_used_ce3():
    story = one_case()
    story.sandbox.escalate("dst_headphones", now=MONDAY + timedelta(days=7))
    story.poll(MONDAY + timedelta(days=7))
    record = story["headphones"]
    assert rejected(record) == NOTHING and as_dispute(record).prior_challenges == 0  # decide() sees no challenge
    case = as_case(record)
    assert case.prior_challenges == 1
    assert not ce3.consulted(case, Action.GATHER_EVIDENCE, [])
    assert ce3.evidence(ce3.check(FERNHILL, case, []), case).evidence_class is EvidenceClass.NONE
    package = Evidence(EvidenceClass.CE3_QUALIFIED, FIRST_PACKAGE_FACTS, frozenset({FIRST_PACKAGE_FILE}))
    with pytest.raises(Refused, match=ONE_CE3_ATTEMPT):
        keep(record, package, MONDAY + timedelta(days=7), round_number=2)


def test_round_ones_own_challenge_leaves_the_case_in_round_one():
    # From sending() on, round one is still round one: its file is built again for the upload, a retry after a restart or
    # a person after a refused POST, and gets the plan's own hash. Someone else's challenge still uses the attempt.
    story = being_sent()
    record = story["headphones"]
    plan = record.rounds[-1].plan
    failed = send_failed(record, plan.request_id, "invalid_argument: the file was refused.", MONDAY + MINUTE)
    for each in (record, failed):
        case = as_case(each)
        assert len(each.rounds) == 1 and case.prior_challenges == 0
        result = ce3.check(FERNHILL, case)
        assert result.reasons[0] == "CE 3.0 is met, as far as the store's records show."
        assert plan.terms.file_hashes == {purchase_history(FERNHILL, case, result).sha256} == {FIRST_PACKAGE_FILE}
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PURCHASE_HISTORY")
    story.poll(MONDAY + 2 * MINUTE)
    assert as_case(story["headphones"]).prior_challenges == 1  # someone else's, beside this app's own


def test_only_this_rounds_own_sent_challenge_is_left_out():
    # Someone else's challenge uses the attempt beside an accept this app sent: that's no challenge of its own to leave
    # out. (Beside a plan never sent, it can't miscount: poll() drops such a plan when someone else's challenge shows.)
    story = Story(cases=("headphones",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PURCHASE_HISTORY")
    story.poll(MONDAY + MINUTE)
    assert story["headphones"].rounds[-1].plan is None and as_case(story["headphones"]).prior_challenges == 1
    story = being_sent("ear_tips")
    assert story["ear_tips"].rounds[-1].plan.terms.action is Action.ACCEPT
    story.sandbox.reply("dst_ear_tips", Action.CHALLENGE, SOMEONE, "PRODUCT_NOT_RECEIVED")
    story.poll(MONDAY + 2 * MINUTE)
    assert as_case(story["ear_tips"]).prior_challenges == 1


def test_round_ones_challenge_uses_the_attempt_once_it_shows():
    # Once this app's challenge shows, the bank has the attempt, though the case is still in round one: a person who
    # handles the round, say when the bank asks again at the same stage, isn't told CE 3.0 can be sent or handed its file.
    story = being_sent()
    record = story["headphones"]
    story.sandbox.post(record)
    story.records["dst_headphones"] = taken(record, record.rounds[-1].plan.request_id, MONDAY + MINUTE)
    story.poll(MONDAY + 2 * MINUTE)
    shown = story["headphones"]
    story.sandbox.change("dst_headphones", status="REQUIRES_RESPONSE", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 3 * MINUTE)
    asked = story["headphones"]
    assert (phase(shown), phase(asked)) == (Phase.WAIT, Phase.PERSON)
    for each in (shown, asked):
        case = as_case(each)
        assert len(each.rounds) == 1 and each.rounds[-1].reply.by == ACTOR and case.prior_challenges == 1
        result = ce3.check(FERNHILL, case)
        assert "it can't be sent: Visa allows one CE 3.0 attempt" in result.reasons[0]
        assert not ce3.consulted(case, Action.GATHER_EVIDENCE, [])
        assert ce3.evidence(result, case).evidence_class is EvidenceClass.NONE
        with pytest.raises(ValueError, match="^Visa allows one CE 3.0 attempt"):
            purchase_history(FERNHILL, case, result)
    # Someone else's accept isn't this app's challenge showing, so the file can still be built for a person.
    story = being_sent()
    story.sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "OTHER")
    story.poll(MONDAY + 2 * MINUTE)
    assert story["headphones"].rounds[-1].reply.by == SOMEONE and as_case(story["headphones"]).prior_challenges == 0


def test_round_ones_challenge_uses_the_attempt_once_a_reading_shows_the_bank_deciding_on_it():
    # The POST landed, but its 2xx was lost and the entry lags: the next GET shows RFI/CHALLENGED. The bank has the attempt,
    # so a person who handles the round, here when the bank asks again after the deadline, isn't told CE 3.0 can be sent
    # or handed its file. A POST that may never have landed still leaves the round in round one, for the retry.
    story = being_sent()
    story.sandbox.post(story["headphones"], show=False)
    story.poll(MONDAY + 2 * MINUTE)
    landed = story["headphones"]
    later = MONDAY + timedelta(days=7)
    story.sandbox.change("dst_headphones", status="REQUIRES_RESPONSE", due_at=(later + timedelta(days=9)).isoformat())
    story.poll(later)
    asked = story["headphones"]
    assert (phase(landed), phase(asked)) == (Phase.SENT, Phase.PERSON)
    for each in (landed, asked):
        case = as_case(each)
        assert len(each.rounds) == 1 and not each.last.replies and not each.rounds[-1].plan.taken_at
        assert lifecycle.LANDED in each.rounds[-1].notes and case.prior_challenges == 1
        result = ce3.check(FERNHILL, case)
        assert "it can't be sent: Visa allows one CE 3.0 attempt" in result.reasons[0]
        assert not ce3.consulted(case, Action.GATHER_EVIDENCE, [])
        assert ce3.evidence(result, case).evidence_class is EvidenceClass.NONE
        with pytest.raises(ValueError, match="^Visa allows one CE 3.0 attempt"):
            purchase_history(FERNHILL, case, result)
    story = being_sent()
    story.poll(MONDAY + 2 * MINUTE)  # nothing moved
    assert as_case(story["headphones"]).prior_challenges == 0


def test_round_ones_challenge_uses_the_attempt_once_taken_or_shown_behind_another_reply():
    # Airwallex took it with a 2xx and it never showed, or it shows behind Priya's accept, which the round keeps as its
    # reply: either way the bank has the attempt, and nothing is sent again.
    story = being_sent()
    story.sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "OTHERS")
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY")
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.poll(MONDAY + 2 * MINUTE)
    behind = story["headphones"]
    assert behind.rounds[-1].reply.by == SOMEONE and any(reply.by == ACTOR for reply in behind.last.replies)
    story = being_sent()
    record = story["headphones"]
    story.records["dst_headphones"] = taken(record, record.rounds[-1].plan.request_id, MONDAY + MINUTE)
    story.poll(MONDAY + timedelta(days=7))
    took = story["headphones"]
    assert took.rounds[-1].plan.taken_at and not took.last.replies
    for each in (behind, took):
        case = as_case(each)
        assert len(each.rounds) == 1 and case.prior_challenges == 1
        result = ce3.check(FERNHILL, case)
        assert "it can't be sent: Visa allows one CE 3.0 attempt" in result.reasons[0]
        with pytest.raises(ValueError, match="^Visa allows one CE 3.0 attempt"):
            purchase_history(FERNHILL, case, result)


def test_rejected_sets_add_up_over_rounds():
    story = at_round_two()
    story.run(THREE_WEEKS_LATER)
    story.sandbox.change("dst_headphones", status="REQUIRES_RESPONSE",
                         due_at=(THREE_WEEKS_LATER + timedelta(days=10)).isoformat())
    later = THREE_WEEKS_LATER + timedelta(days=2)
    story.poll(later)
    record = story["headphones"]
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK, Stage.PRE_ARBITRATION]
    assert rejected(record) == (FIRST_PACKAGE_FACTS | {WARRANTY_SIGN_UP.id},
                                frozenset({FIRST_PACKAGE_FILE, SECOND_PACKAGE_FILE}))
    decision = decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), later)
    assert "no cost model for the PRE_ARBITRATION stage" in decision.reasons[0]
    assert phase(keep(record, ANSWERS["headphones_round_2"].evidence(), later, round_number=3)) is Phase.PERSON


# ----------------------------------------------------------------------------- rounds and whose replies
def test_a_round_opens_when_someone_answered_at_a_later_stage():
    story = one_case()
    story.sandbox.change("dst_headphones", stage="CHARGEBACK", status="CHALLENGED")
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PRODUCT_RECEIVED")
    story.poll(MONDAY + MINUTE)
    record = story["headphones"]
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK]
    assert record.rounds[-1].reply.by == SOMEONE
    assert record.others == 1 and phase(record) is Phase.WAIT
    assert keys(record) == ["chargeback_fee"]


def test_someone_elses_late_challenge_means_a_loss_costs_the_later_stage_fees():
    story = one_case("earbuds")
    story.sandbox.reply("dst_earbuds", Action.CHALLENGE, SOMEONE, "OTHER_REASONS")
    story.sandbox.change("dst_earbuds", stage="PRE_ARBITRATION", status="LOST")
    story.poll(MONDAY + MINUTE)
    assert entries(story["earbuds"]) == ledger_of(("chargeback_opened",), ("lost_after_fighting", 189))


@pytest.mark.parametrize("stage, why", [
    ("PRE_ARBITRATION", "The dispute moved to PRE_ARBITRATION with no response at CHARGEBACK."),
    ("CHARGEBACK", "Airwallex shows the case waiting on the bank, but no response is on record here."),
])
def test_waiting_with_no_response_on_record_goes_to_a_person(stage, why):
    story = one_case("earbuds")
    story.sandbox.change("dst_earbuds", stage=stage, status="PENDING_DECISION")
    story.poll(MONDAY + MINUTE)
    assert phase(story["earbuds"]) is Phase.PERSON and person_reason(story["earbuds"]).startswith(why)


def test_a_web_app_challenge_is_someone_elses():
    story = waiting_for_priya()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PURCHASE_HISTORY")
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.poll(MONDAY + MINUTE)
    record = story["headphones"]
    assert record.rounds[0].plan is None
    assert "Someone else responded first, so the plan was dropped." in record.rounds[0].notes
    assert record.others == 1
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert rejected(record) == NOTHING
    assert decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER).action is Action.HAND_OFF


def test_our_actor_without_a_plan_counts_as_someone_else():
    story = Story(cases=("headphones",))  # the record was lost, and the challenge on the dispute has no plan here
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY")
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.poll(MONDAY)
    assert story["headphones"].others == 1
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert rejected(record) == NOTHING
    assert decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER).action is Action.HAND_OFF


def test_a_reply_that_shows_before_this_app_first_looks_is_never_answered_again():
    story = Story(cases=("ear_tips",))
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, SOMEONE, "LOW_VALUE_TRANSACTION")  # the store accepted in the web app
    story.run(MONDAY)  # the entry shows, but the status still asks and the refund isn't on the payment yet
    record = story["ear_tips"]
    assert record.rounds[0].reply.by == SOMEONE and record.rounds[0].plan is None and phase(record) is Phase.WAIT
    assert [entry["accepted_by"] for entry in story.sandbox.disputes["dst_ear_tips"]["accept_details"]] == [SOMEONE]


def test_recorded_round_two_with_no_local_record_goes_to_a_person():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    records, problems = poll({}, [(RECORDED_DISPUTE, RECORDED_PAYMENT)], now, store=FERNHILL)
    record = records[RECORDED_DISPUTE["id"]]
    assert not problems and keys(record) == ["chargeback_fee"]
    assert record.others == 1  # the spike's own challenge: this app didn't send it
    assert rejected(record) == NOTHING and as_dispute(record).prior_challenges == 1
    assert record.last.issuer_comment == "" and record.last.customer_id is None
    assert person_reason(record) == NO_ORDER  # the recording's payment has no order number
    package = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, frozenset({"warranty-55821"}), frozenset({"pdf-2"}))
    assert decide(as_dispute(record), package, now).action is Action.HAND_OFF


def test_a_payment_with_no_order_in_the_store_goes_to_a_person():
    story = one_case()
    story.sandbox.pay("dst_headphones", order_no="9002")  # a spike probe the store doesn't have
    story.sandbox.change("dst_headphones")
    story.poll(MONDAY + MINUTE)
    record = story["headphones"]
    assert record.last.customer_id is None and person_reason(record) == NO_ORDER
    with pytest.raises(Refused, match="It is at PERSON."):
        keep(record, ANSWERS["headphones"].evidence(), MONDAY + MINUTE, round_number=1)


def test_an_ai_automation_challenge_is_never_followed_by_ours():
    story = Story(cases=("headphones",))
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, "Airwallex", "PURCHASE_HISTORY",
                        challenge_method="AI_DISPUTE_AUTOMATION")
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.poll(MONDAY)
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert phase(record) is Phase.DECIDE
    decision = decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER)
    assert decision.action is Action.HAND_OFF and "AI Dispute Automation" in decision.reasons[0]
    kept = keep(record, ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER, round_number=2)
    assert phase(kept) is Phase.PERSON


def test_a_reason_stored_another_way_is_noted():
    story = being_sent()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "OTHER_REASONS")
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.poll(MONDAY + 2 * MINUTE)
    record = story["headphones"]
    assert record.others == 0 and phase(record) is Phase.WAIT
    assert record.rounds[0].notes == ("Airwallex stored the reason as OTHER_REASONS, not PURCHASE_HISTORY.",)


def test_someone_elses_challenge_beside_ours_blanks_the_rejected_record():
    story = Story()
    story.run(MONDAY)
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PRODUCT_RECEIVED", stage="RFI")
    story.poll(MONDAY + timedelta(hours=1))
    assert person_reason(story["headphones"]) == "Airwallex shows a second response at RFI. A person checks it."
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert record.others == 1 and rejected(record) == NOTHING  # nobody here saw the other package
    assert decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER).action is Action.HAND_OFF


def test_an_alarm_on_an_earlier_round_reaches_the_queue():
    story = at_round_two()
    story.decide(THREE_WEEKS_LATER)
    story.approve(THREE_WEEKS_LATER)
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PRODUCT_RECEIVED", stage="RFI")
    story.poll(THREE_WEEKS_LATER + MINUTE)
    record = story["headphones"]
    why = "Airwallex shows a second response at RFI. A person checks it."
    assert why in record.rounds[0].notes and person_reason(record) == why
    with pytest.raises(Refused, match=f"^{re.escape(why)}$"):
        sending(record, THREE_WEEKS_LATER + MINUTE)


def test_an_alarm_a_person_saw_stays_off_the_next_round():
    story = Story()
    story.run(MONDAY)
    story.sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "OTHERS", stage="RFI")  # beside this app's challenge
    story.poll(MONDAY + timedelta(hours=1))
    assert person_reason(story["headphones"]) == "Airwallex shows a second response at RFI. A person checks it."
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert len(record.rounds) == 2 and phase(record) is Phase.DECIDE and rejected(record) == FIRST_PACKAGE


def test_a_case_a_person_closed_stays_closed_when_airwallex_touches_it():
    story = being_sent()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY", stage="CHARGEBACK")
    story.sandbox.change("dst_headphones", stage="PRE_ARBITRATION", status="PENDING_DECISION")
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.resolve("dst_headphones", Status.WON)
    story.poll(MONDAY + timedelta(days=3))
    assert person_reason(story["headphones"]) == "This app's response to RFI shows at CHARGEBACK. A person checks it."
    story.records["dst_headphones"] = book(story["headphones"], "Nothing more moved", ZERO, "Priya",
                                           MONDAY + timedelta(days=3, minutes=1))
    story.sandbox.change("dst_headphones")  # only updated_at moves
    story.poll(MONDAY + timedelta(days=4))
    assert phase(story["headphones"]) is Phase.CLOSED


def test_someone_elses_late_reply_to_a_round_in_flight_reaches_the_queue():
    story = being_sent()
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PRODUCT_RECEIVED", stage="RFI")
    story.poll(THREE_WEEKS_LATER + MINUTE)
    record = story["headphones"]
    assert person_reason(record) == "Someone else responded while this app was sending. A person checks it."
    assert record.others == 1 and rejected(record) == NOTHING


@pytest.mark.parametrize("refund_shows", [True, False], ids=["with its refund", "before its refund"])
def test_a_response_still_being_sent_when_the_bank_moves_on_goes_to_a_person(refund_shows):
    story = being_sent("ear_tips")  # code's $12 accept, refund included
    story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK]
    assert person_reason(record) == ("This app's response to RFI doesn't show, and the bank moved the case to "
                                     "CHARGEBACK. It may land there, so a person handles it.")
    with pytest.raises(Refused, match="It is at PERSON."):
        keep(record, None, MONDAY + 2 * MINUTE, round_number=2)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # it landed at RFI
    if refund_shows:
        story.sandbox.pay("dst_ear_tips", refunded=12)
    story.poll(MONDAY + 3 * MINUTE)
    record = story["ear_tips"]
    assert keys(record) == ["chargeback_fee", "refund"] and phase(record) is Phase.DECIDE
    assert at_risk(record) == money(12)  # the chargeback is open, so its $12 is at risk again
    assert "already refunded" in decide(as_dispute(record), None, MONDAY + 3 * MINUTE).reasons[0]  # never paid twice
    assert phase(keep(record, None, MONDAY + 3 * MINUTE, round_number=2)) is Phase.PERSON


def test_our_response_that_lands_at_the_next_stage_goes_to_a_person():
    story = being_sent()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY", stage="CHARGEBACK")
    story.sandbox.change("dst_headphones", stage="PRE_ARBITRATION", status="PENDING_DECISION")
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["headphones"]) == "This app's response to RFI shows at CHARGEBACK. A person checks it."


def test_a_response_that_shows_after_the_bank_moved_on_ends_the_wait_for_a_person():
    story = round_two_being_sent()
    story.sandbox.change("dst_headphones", stage="PRE_ARBITRATION", status="PENDING_DECISION")  # the entry lags
    story.poll(THREE_WEEKS_LATER + 2 * MINUTE)
    assert person_reason(story["headphones"]).startswith("This app's response to CHARGEBACK doesn't show, and the bank "
                                                         "moved the case to PRE_ARBITRATION.")
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PRODUCT_RECEIVED", stage="CHARGEBACK")
    story.poll(THREE_WEEKS_LATER + 3 * MINUTE)
    assert phase(story["headphones"]) is Phase.WAIT and person_reason(story["headphones"]) is None


@pytest.mark.parametrize("case_id, first, then", [
    ("earbuds", {"status": "WON"}, {"stage": "PRE_ARBITRATION", "status": "REQUIRES_RESPONSE"}),
    ("headphones", {"status": "LOST"}, {"stage": "CHARGEBACK", "status": "REQUIRES_RESPONSE"}),
    ("headphones", {"status": "ACCEPTED"}, {"stage": "CHARGEBACK", "status": "CHALLENGED"}),
], ids=["won, then pre-arbitration", "lost, then a chargeback", "accepted, then a chargeback someone answered"])
def test_a_dispute_first_seen_closed_that_reopens_goes_to_a_person(case_id, first, then):
    sandbox = Sandbox()
    sandbox.open(case_id)
    sandbox.change(f"dst_{case_id}", **first)
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    assert records[f"dst_{case_id}"].history_only
    sandbox.change(f"dst_{case_id}", due_at=(MONDAY + timedelta(days=9)).isoformat(), **then)
    records, problems = poll(records, sandbox.reads(), MONDAY + timedelta(days=1), store=FERNHILL)
    record = records[f"dst_{case_id}"]
    assert not record.history_only and not problems and phase(record) is Phase.PERSON
    assert person_reason(record).startswith("Airwallex reopened a dispute that was closed when this app first saw it.")
    assert at_risk(record) == FERNHILL.order(FERNHILL.case(case_id).order_number).amount
    assert "chargeback_fee" in keys(record)


# ----------------------------------------------------------------------------- approvals
@pytest.mark.parametrize("change, named", [
    (lambda sandbox: sandbox.change("dst_headphones", amount=1200), "the amount"),
    (lambda sandbox: sandbox.change("dst_headphones", currency="EUR"), "the currency"),
    (lambda sandbox: sandbox.change("dst_headphones", due_at=(MONDAY + timedelta(days=9)).isoformat()), "the due date"),
    (lambda sandbox: sandbox.pay("dst_headphones", refunded=5), "the refunds so far"),
    (lambda sandbox: sandbox.pay("dst_headphones", order_no="1121"), "the customer"),  # Jamie's order, not Alex's
], ids=["amount", "currency", "due date", "refunds", "customer"])
def test_a_reading_that_changes_a_bound_term_drops_the_plan(change, named):
    story = waiting_for_priya()
    story.approve(MONDAY)
    change(story.sandbox)
    story.poll(MONDAY + MINUTE)
    record = story["headphones"]
    assert record.rounds[0].plan is None
    assert f"The plan was dropped: {named} changed." in record.rounds[0].notes


@pytest.mark.parametrize("field, value, named", [
    ("reason", "PRODUCT_RECEIVED", "the reason"),
    ("action", Action.ACCEPT, "the action"),
    ("fact_ids", FIRST_PACKAGE_FACTS - {"order-1012"}, "the facts"),
    ("file_hashes", frozenset({"another-file"}), "the files"),
    ("amount", money(1), "the amount"),
    ("refund_amount", money(1249), "the refund"),
    ("due_at", MONDAY + timedelta(days=9), "the due date"),
    ("customer_id", "customer-jamie", "the customer"),
    ("fee_table", "fees-v2", "the fee table"),
])
def test_an_approval_for_other_terms_is_void_and_free(field, value, named):
    record = waiting_for_priya()["headphones"]
    plan = record.rounds[0].plan
    seen = replace(plan, terms=replace(plan.terms, **{field: value}))
    with pytest.raises(Refused, match=f"^This approval is void: {named} changed.$"):
        approve(record, seen, "Priya", MONDAY)
    assert record.bookings == ()


def test_a_second_approval_in_one_round_costs_one_review():
    story = waiting_for_priya()
    story.approve(MONDAY)
    story.sandbox.change("dst_headphones", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + MINUTE)
    story.decide(MONDAY + MINUTE)
    story.approve(MONDAY + MINUTE)
    record = story["headphones"]
    assert record.rounds[0].plan.approved_at == MONDAY + MINUTE and keys(record) == ["review:RFI"]


def test_keeping_the_same_terms_keeps_the_approval():
    story = waiting_for_priya()
    story.approve(MONDAY)
    record = story["headphones"]
    assert keep(record, ANSWERS["headphones"].evidence(), MONDAY + MINUTE, round_number=1) is record


def test_a_stricter_decision_on_the_same_terms_needs_a_person_again():
    story = Story(cases=("ear_tips",))
    story.sandbox.change("dst_ear_tips", amount=30)
    story.sandbox.payments["int_ear_tips"]["captured_amount"] = 30
    story.poll(MONDAY)
    clean = keep(story["ear_tips"], Evidence(EvidenceClass.NONE), MONDAY, round_number=1)
    assert phase(clean) is Phase.SEND  # $30: code's own accept, refund included
    gift_card = Evidence(EvidenceClass.NONE, conflicts=("The store already sent a $30 gift card.",))
    again = keep(clean, gift_card, MONDAY, round_number=1)
    plan = again.rounds[0].plan
    assert phase(again) is Phase.APPROVE and plan.hand_off and plan.reasons[0] == gift_card.conflicts[0]
    assert plan.request_id == clean.rounds[0].plan.request_id  # the terms are the same


def test_a_read_that_misses_a_conflict_keeps_the_person_on_the_same_terms():
    story = Story(cases=("ear_tips",))
    story.sandbox.change("dst_ear_tips", amount=30)
    story.sandbox.payments["int_ear_tips"]["captured_amount"] = 30
    story.poll(MONDAY)
    gift_card = Evidence(EvidenceClass.NONE, conflicts=("The store already sent a $30 gift card.",))
    held = keep(story["ear_tips"], gift_card, MONDAY, round_number=1)
    assert phase(held) is Phase.APPROVE
    clean = Evidence(EvidenceClass.NONE)  # a read of the records that misses the gift card
    assert keep(held, clean, MONDAY + MINUTE, round_number=1) is held  # $30 doesn't go out past it
    approved = approve(held, held.rounds[0].plan, "Priya", MONDAY + MINUTE)
    assert keep(approved, clean, MONDAY + 2 * MINUTE, round_number=1) is approved
    bad = with_plan(held, needs_approval=False)  # a saved flag gone wrong can't take the person off either
    assert keep(bad, clean, MONDAY + MINUTE, round_number=1) is bad


def test_a_read_that_misses_a_conflict_keeps_the_hand_off_over_50():
    held = the_earbuds_draft()  # $189: a person approves either way, but the conflict is why
    missed = replace(ANSWERS["earbuds"].evidence(), conflicts=())
    assert keep(held, missed, MONDAY + MINUTE, round_number=1) is held  # the conflict stays on Priya's screen
    approved = approve(held, held.rounds[0].plan, "Priya", MONDAY + MINUTE)
    assert keys(approved) == ["chargeback_fee", "review:CHARGEBACK"]  # a hand-off's approval costs a review
    assert keep(approved, missed, MONDAY + 2 * MINUTE, round_number=1) is approved


def test_a_read_that_misses_one_of_two_conflicts_keeps_the_hand_off():
    record = one_case("earbuds")["earbuds"]
    evidence = ANSWERS["earbuds"].evidence()  # the warehouse has no scan of the return
    both = replace(evidence, conflicts=(*evidence.conflicts, "Robin got a $189 gift card on Oct 20."))
    held = keep(record, both, MONDAY, round_number=1)
    assert keep(held, evidence, MONDAY + MINUTE, round_number=1) is held  # the gift card stays on Priya's screen
    assert keep(held, replace(both, conflicts=both.conflicts[::-1]), MONDAY + MINUTE, round_number=1) is held  # reordered
    approved = approve(held, held.rounds[0].plan, "Priya", MONDAY + MINUTE)
    assert keep(approved, evidence, MONDAY + 2 * MINUTE, round_number=1) is approved  # and her approval stands


def test_a_new_conflict_on_an_approved_draft_needs_a_person_again():
    record = the_earbuds_draft()
    shown = record.rounds[0].plan
    approved = approve(record, shown, "Priya", MONDAY)
    evidence = ANSWERS["earbuds"].evidence()
    gift_card = "Robin got a $189 gift card on Oct 20."
    again = keep(approved, replace(evidence, conflicts=(*evidence.conflicts, gift_card)), MONDAY + MINUTE, round_number=1)
    plan = again.rounds[0].plan
    assert phase(again) is Phase.APPROVE and gift_card in plan.reasons and plan.request_id == shown.request_id
    with pytest.raises(Refused, match="^This approval is void: the plan changed after the screen showed it.$"):
        approve(again, shown, "Priya", MONDAY + MINUTE)  # a click on the screen that still shows the old plan
    assert keys(approve(again, plan, "Priya", MONDAY + MINUTE)) == ["chargeback_fee", "review:CHARGEBACK"]


@pytest.mark.parametrize("by, extra, news", [
    (SOMEONE, {}, "someone else's challenge showed"),
    ("Airwallex", {"challenge_method": "AI_DISPUTE_AUTOMATION"},
     "someone else's challenge showed and AI Dispute Automation challenged the case"),
], ids=["a web-app entry", "an AI Dispute Automation entry"])
def test_an_approved_plan_is_dropped_when_a_late_challenge_shows(by, extra, news):
    story, later = nobody_answered_round_one()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, by, "PURCHASE_HISTORY", stage="RFI", **extra)
    story.poll(later + MINUTE)
    record = story["headphones"]
    assert f"The plan was dropped: {news}." in record.rounds[-1].notes and phase(record) is Phase.DECIDE
    assert decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), later + MINUTE).action is Action.HAND_OFF


@pytest.mark.parametrize("change, news", [
    (lambda sandbox: sandbox.change("dst_headphones", reason={"original_code": "13.1"}), "the reason code changed"),
    (lambda sandbox: sandbox.change("dst_headphones", issuer_comment="The cardholder also names order #1038."),
     "the bank's comment changed"),
    (lambda sandbox: sandbox.payments["int_headphones"].update(captured_amount=1000), "the captured amount changed"),
], ids=["reason code", "bank's comment", "captured amount"])
def test_an_approved_plan_is_dropped_when_what_decide_reads_changes(change, news):
    story = waiting_for_priya()
    story.approve(MONDAY)
    change(story.sandbox)
    story.poll(MONDAY + MINUTE)
    assert f"The plan was dropped: {news}." in story["headphones"].rounds[0].notes


def test_request_ids_come_from_the_terms():
    record = waiting_for_priya()["headphones"]
    plan = record.rounds[0].plan
    assert plan.request_id == request_id_for(plan.terms) == request_id_for(replace(plan.terms))
    assert request_id_for(replace(plan.terms, due_at=plan.terms.due_at + MINUTE)) != plan.request_id
    other = keep(record, Evidence(EvidenceClass.CE3_QUALIFIED, FIRST_PACKAGE_FACTS, frozenset({"rebuilt-file"})), MONDAY,
                 round_number=1)
    assert other.rounds[0].plan.request_id != plan.request_id


@pytest.mark.parametrize("by", ["", "   ", ACTOR, ACTOR.upper()])
def test_only_a_named_person_can_approve(by):
    record = waiting_for_priya()["headphones"]
    with pytest.raises(Refused, match="named person"):
        approve(record, record.rounds[0].plan, by, MONDAY)


def test_an_approval_is_refused_when_nothing_waits_for_one():
    kept = keep(one_case("ear_tips")["ear_tips"], None, MONDAY, round_number=1)
    assert phase(kept) is Phase.SEND  # $12: code accepts on its own
    with pytest.raises(Refused, match="Nothing waits for an approval. The case is at SEND."):
        approve(kept, kept.rounds[0].plan, "Priya", MONDAY)


def test_evidence_read_for_another_round_is_refused():
    record = at_round_two()["headphones"]
    with pytest.raises(Refused, match="read for round 1, but the case is in round 2"):
        keep(record, ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER, round_number=1)


# ----------------------------------------------------------------------------- sending
def test_nothing_is_sent_without_a_reading_after_the_approval():
    story = waiting_for_priya()
    story.approve(MONDAY + MINUTE)
    assert blocker(story["headphones"], MONDAY + MINUTE).startswith("Read the dispute again first.")
    story.poll(MONDAY + 2 * MINUTE)
    assert blocker(story["headphones"], MONDAY + 2 * MINUTE) is None


def test_nothing_is_sent_on_an_old_reading():
    record = keep(one_case("ear_tips")["ear_tips"], None, MONDAY, round_number=1)
    assert blocker(record, MONDAY) is None
    assert blocker(record, MONDAY + FRESH + MINUTE).startswith("Read the dispute again first.")


def test_a_reading_is_fresh_for_ten_minutes():
    assert FRESH == timedelta(minutes=10)
    record = keep(one_case("ear_tips")["ear_tips"], None, MONDAY, round_number=1)
    assert blocker(record, MONDAY + timedelta(minutes=10)) is None
    assert blocker(record, MONDAY + timedelta(minutes=10, seconds=1)) == ("Read the dispute again first. The last reading "
                                                                          "is more than 10 minutes old.")


def test_nothing_is_approved_or_sent_after_the_deadline():
    story = waiting_for_priya()
    plan = story["headphones"].rounds[0].plan
    terms = plan.terms
    with pytest.raises(Refused, match="The deadline has passed."):
        approve(story["headphones"], plan, "Priya", terms.due_at)
    story.approve(terms.due_at - 2 * MINUTE)
    story.poll(terms.due_at - MINUTE)
    assert blocker(story["headphones"], terms.due_at) == "The deadline has passed."
    story.poll(terms.due_at)
    record = story["headphones"]
    assert "The plan was dropped: the deadline passed before it was sent." in record.rounds[0].notes
    kept = keep(record, ANSWERS["headphones"].evidence(), terms.due_at, round_number=1)
    assert phase(kept) is Phase.PERSON and person_reason(kept) == "The response deadline has passed."


def test_a_retry_after_a_restart_reuses_the_request_id():
    story = being_sent("ear_tips")
    first = story["ear_tips"].rounds[0].plan
    story.records["dst_ear_tips"] = from_json(to_json(story["ear_tips"]))  # the app restarts: no answer was saved
    assert phase(story["ear_tips"]) is Phase.SENT
    assert blocker(story["ear_tips"], MONDAY + MINUTE) == "Read the dispute again first. It may already show this response."
    story.poll(MONDAY + 2 * MINUTE)  # still asking, so the POST didn't land
    retried = sending(story["ear_tips"], MONDAY + 2 * MINUTE).rounds[0].plan
    assert retried.request_id == first.request_id and retried.sending_at == MONDAY + 2 * MINUTE


def test_a_landed_post_is_confirmed_from_the_dispute_not_sent_again():
    story = being_sent("ear_tips")
    story.sandbox.post(story["ear_tips"])  # it landed, but the answer was lost
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert phase(record) is Phase.CLOSED and record.rounds[0].reply.by == ACTOR and cost(record) == Decimal("12.30")
    assert blocker(record, MONDAY + 2 * MINUTE) == "The case is closed."


def test_a_2xx_stops_every_resend():
    story = being_sent("ear_tips")
    story.records["dst_ear_tips"] = taken(story["ear_tips"], story["ear_tips"].rounds[0].plan.request_id, MONDAY + MINUTE)
    story.poll(MONDAY + 2 * MINUTE)  # Airwallex said yes, but the GET doesn't show it yet
    assert phase(story["ear_tips"]) is Phase.SENT
    assert blocker(story["ear_tips"], MONDAY + 2 * MINUTE).startswith("Airwallex took the response.")
    story.sandbox.post(story["ear_tips"])
    story.poll(MONDAY + 3 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.CLOSED and cost(story["ear_tips"]) == Decimal("12.30")


def test_an_answer_must_name_the_plan_in_flight():
    story = being_sent()
    with pytest.raises(Refused, match="No response with this request id is being sent."):
        taken(story["headphones"], "another-id", MONDAY + MINUTE)
    with pytest.raises(Refused, match="No response with this request id is being sent."):
        send_failed(waiting_for_priya()["headphones"], "any", "error", MONDAY)


@pytest.mark.parametrize("answered", [True, False], ids=["2xx", "no answer"])
def test_a_response_that_never_shows_goes_to_a_person_at_the_deadline(answered):
    story = being_sent()
    plan = story["headphones"].rounds[0].plan
    if answered:
        story.records["dst_headphones"] = taken(story["headphones"], plan.request_id, MONDAY + MINUTE)
    story.poll(plan.terms.due_at - MINUTE)
    assert phase(story["headphones"]) is Phase.SENT
    story.poll(plan.terms.due_at)
    assert person_reason(story["headphones"]).startswith("This app's response doesn't show on the dispute")


def test_a_plan_being_sent_is_never_replaced():
    story = being_sent()
    with pytest.raises(Refused, match="It is at SENT."):
        keep(story["headphones"], ANSWERS["headphones"].evidence(), MONDAY + MINUTE, round_number=1)
    story.sandbox.change("dst_headphones", amount=1200)
    story.poll(MONDAY + 2 * MINUTE)
    record = story["headphones"]
    assert phase(record) is Phase.PERSON and record.rounds[0].plan.sending_at == MONDAY + MINUTE
    assert person_reason(record) == "The amount changed while a response was being sent. A person checks what Airwallex has."


@pytest.mark.parametrize("captured, refunded", [(12, 5), (30, 15)], ids=["less than ours", "more than ours"])
def test_someone_elses_refund_while_our_accept_is_sent_goes_to_a_person(captured, refunded):
    story = being_sent("ear_tips", captured)  # code's own $12 accept: no answer came
    story.sandbox.pay("dst_ear_tips", refunded=refunded)  # the store refunded Jamie in the web app
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["ear_tips"]) == ("The refunds so far changed while a response was being sent. A person "
                                                "checks what Airwallex has.")
    with pytest.raises(Refused):
        sending(story["ear_tips"], MONDAY + 2 * MINUTE)  # a retry would refund Jamie another $12


def test_our_own_refund_doesnt_count_as_a_change():
    story = being_sent("ear_tips")
    story.sandbox.pay("dst_ear_tips", refunded=12)  # the refund landed before the accept shows
    story.poll(MONDAY + 2 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.SENT


@pytest.mark.parametrize("ours", [True, False], ids=["this app's refund", "the store's refund"])
def test_a_refund_the_size_of_ours_while_our_accept_is_sent_holds_the_retry(ours):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    story.sandbox.pay("dst_ear_tips", refunded=12)  # this app's refund showing first, or the store's in the web app
    story.poll(MONDAY + 2 * MINUTE)  # still RFI/REQUIRES_RESPONSE, so code can't tell which
    record = story["ear_tips"]
    assert phase(record) is Phase.SENT and person_reason(record) is None and record.rounds[0].notes[-1] == HELD
    with pytest.raises(Refused, match=f"^{re.escape(HELD)}$"):
        sending(record, MONDAY + 2 * MINUTE)  # if the refund is the store's, a retry pays Jamie twice
    if ours:  # the rest of this app's accept shows
        story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")
        story.sandbox.change("dst_ear_tips", status="ACCEPTED", refunds=[{"id": "rfd_dst_ear_tips"}])
        story.poll(MONDAY + 3 * MINUTE)
        assert phase(story["ear_tips"]) is Phase.CLOSED and cost(story["ear_tips"]) == Decimal("12.30")
    else:  # it never shows: a person checks at the deadline
        story.poll(record.rounds[0].plan.terms.due_at)
        assert person_reason(story["ear_tips"]).startswith("This app's response doesn't show on the dispute")


@pytest.mark.parametrize("refunded, why", [(5, SENT_AND_CHANGED), (12, None)],
                         ids=["someone else's refund", "a refund the size of ours"])
def test_a_refund_behind_an_older_copy_while_our_accept_is_sent_holds_every_retry(refunded, why):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    older = copy.deepcopy(story.sandbox.disputes["dst_ear_tips"])
    story.sandbox.change("dst_ear_tips")  # only updated_at moves
    story.poll(MONDAY + 2 * MINUTE)
    before = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.sandbox.pay("dst_ear_tips", refunded=refunded)  # the store refunded Jamie in the web app
    payment = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.records, _ = poll(story.records, [(older, payment)], MONDAY + 3 * MINUTE, store=FERNHILL)
    assert story["ear_tips"].last.dispute.refunded_amount == refunded  # a refund never goes down: that payment is newer
    story.sandbox.payments["int_ear_tips"] = before  # the next GET is in order, but its payment lags: nothing refunded
    story.poll(MONDAY + 4 * MINUTE)
    assert person_reason(story["ear_tips"]) == REFUND_FELL  # so it reads as the old copy it is
    story.sandbox.payments["int_ear_tips"] = payment
    story.poll(MONDAY + 5 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked is None and person_reason(record) == why
    with pytest.raises(Refused, match=f"^{re.escape(why or HELD)}$"):
        sending(record, MONDAY + 5 * MINUTE)  # if the first try never landed, a retry refunds Jamie another $12


@pytest.mark.parametrize("shown", ["on the payment", "only as a listed refund"])
def test_a_refund_that_showed_while_our_accept_is_sent_holds_every_later_retry(shown):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    lagging = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    if shown == "on the payment":  # this app's refund, or the store's
        story.sandbox.pay("dst_ear_tips", refunded=12)
        story.poll(MONDAY + 2 * MINUTE)
        fresh = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
        del story.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]  # the payment doesn't say what was refunded
        story.poll(MONDAY + 3 * MINUTE)
        assert story["ear_tips"].last.dispute.refunded_amount == 12  # so the record keeps what it saw
        story.sandbox.payments["int_ear_tips"] = lagging  # then its GET lags
        story.poll(MONDAY + 4 * MINUTE)
        assert person_reason(story["ear_tips"]) == REFUND_FELL  # an old copy, until the payment's GET catches up
        story.sandbox.payments["int_ear_tips"] = fresh
    else:  # the payment doesn't show it yet
        story.sandbox.change("dst_ear_tips", refunds=[{"id": "rfd_someone"}])
        story.poll(MONDAY + 2 * MINUTE)
        story.poll(MONDAY + 3 * MINUTE)
        story.sandbox.disputes["dst_ear_tips"]["refunds"] = None  # a GET that lost the list, though nothing moved
        story.poll(MONDAY + 4 * MINUTE)
    story.poll(MONDAY + 5 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked is None and person_reason(record) is None
    with pytest.raises(Refused, match=f"^{re.escape(HELD)}$"):
        sending(record, MONDAY + 5 * MINUTE)  # if the $12 was the store's, a retry pays Jamie twice
    story.poll(record.rounds[0].plan.terms.due_at)
    assert person_reason(story["ear_tips"]).startswith("This app's response doesn't show on the dispute")


@pytest.mark.parametrize("shown", ["on the payment", "only as a listed refund"])
@pytest.mark.parametrize("waits", ["CHALLENGED", "PENDING_DECISION"])
def test_a_refund_that_showed_while_the_bank_decided_holds_the_retry_when_it_asks_again(waits, shown):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    lagging = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    if shown == "on the payment":  # the store refunded Jamie in its back office
        story.sandbox.pay("dst_ear_tips", refunded=12)
    fresh = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    listed = None if shown == "on the payment" else [{"id": "rfd_someone"}]  # or the payment doesn't show it yet
    story.sandbox.change("dst_ear_tips", status=waits, refunds=listed)  # and answered in the web app: no entry shows yet
    story.poll(MONDAY + 2 * MINUTE)
    assert story["ear_tips"].rounds[0].notes[-1] == HELD  # Airwallex doesn't wait for this response, but may ask again
    story.sandbox.payments["int_ear_tips"] = lagging  # then the payment's GET lags, and the dispute's loses the list
    story.sandbox.change("dst_ear_tips", status="REQUIRES_RESPONSE", refunds=None)  # as the bank asks again at RFI
    story.poll(MONDAY + 3 * MINUTE)
    if shown == "on the payment":  # less than an earlier reading showed: an old copy, until the payment catches up
        assert person_reason(story["ear_tips"]) == REFUND_FELL
        story.sandbox.payments["int_ear_tips"] = fresh
        story.poll(MONDAY + 4 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked is None and person_reason(record) is None
    with pytest.raises(Refused, match=f"^{re.escape(HELD)}$"):
        sending(record, MONDAY + 4 * MINUTE)  # if the first try never landed, a retry pays Jamie twice
    story.poll(record.rounds[0].plan.terms.due_at)
    assert person_reason(story["ear_tips"]).startswith("This app's response doesn't show on the dispute")


def test_a_listed_refund_beside_an_odd_dispute_get_while_our_accept_is_sent_holds_every_retry():
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    story.sandbox.change("dst_ear_tips", status="EXPIRED", refunds=[{"id": "rfd_someone"}])  # a glitch, beside a refund
    story.poll(MONDAY + 2 * MINUTE)  # the payment doesn't show the refund yet: unclear, so the record keeps no amount
    assert story["ear_tips"].blocked == "Airwallex marked the RFI stage expired after a response. A person checks it."
    story.sandbox.change("dst_ear_tips", status="REQUIRES_RESPONSE", refunds=None)  # right again, but the list is lost
    story.poll(MONDAY + 3 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked is None and person_reason(record) is None
    with pytest.raises(Refused, match=f"^{re.escape(HELD)}$"):
        sending(record, MONDAY + 3 * MINUTE)  # if the refund was the store's, a retry pays Jamie twice


@pytest.mark.parametrize("refunded, why", [(5, SENT_AND_CHANGED), (12, None)],
                         ids=["someone else's refund", "a refund the size of ours"])
def test_a_refund_beside_an_odd_dispute_get_while_our_accept_is_sent_holds_every_retry(refunded, why):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    lagging = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.sandbox.pay("dst_ear_tips", refunded=refunded)  # the store refunded Jamie in the web app
    fresh = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.sandbox.change("dst_ear_tips", status="EXPIRED")  # while the dispute's GET has a glitch
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked == "Airwallex marked the RFI stage expired after a response. A person checks it."
    assert (record.rounds[0].person, HELD in record.rounds[0].notes) == (why, why is None)  # its payment still counts
    assert record.last.dispute.refunded_amount == refunded  # a refund never goes down, so that payment GET is the newer one
    story.sandbox.change("dst_ear_tips", status="REQUIRES_RESPONSE")  # the dispute's GET is right again
    story.sandbox.payments["int_ear_tips"] = lagging  # but the payment's lags: nothing refunded
    story.poll(MONDAY + 3 * MINUTE)
    assert person_reason(story["ear_tips"]) == REFUND_FELL  # so it reads as the old copy it is
    story.sandbox.payments["int_ear_tips"] = fresh
    story.poll(MONDAY + 4 * MINUTE)
    record = story["ear_tips"]
    assert record.blocked is None and person_reason(record) == why
    with pytest.raises(Refused, match=f"^{re.escape(why or HELD)}$"):
        sending(record, MONDAY + 4 * MINUTE)  # if the first try never landed, a retry refunds Jamie another $12


@pytest.mark.parametrize("then", ["an older copy beside the fresh payment", "the payment still unclear"])
def test_someone_elses_refund_kept_through_an_unclear_payment_goes_to_a_person_when_the_bank_asks_again(then):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    story.sandbox.pay("dst_ear_tips", refunded=5)  # the store refunded Jamie $5 in its back office
    story.sandbox.change("dst_ear_tips", status="CHALLENGED")  # and someone challenged in the web app: no entry yet
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["ear_tips"]) is None  # Airwallex doesn't wait for this response
    older = copy.deepcopy(story.sandbox.disputes["dst_ear_tips"])
    fresh = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    del story.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]  # the payment doesn't say what was refunded
    story.sandbox.change("dst_ear_tips", status="REQUIRES_RESPONSE")  # as the bank asks again at RFI
    story.poll(MONDAY + 3 * MINUTE)
    record = story["ear_tips"]
    assert record.last.dispute.refunded_amount == 5 and HELD in record.rounds[0].notes  # the record keeps what it saw
    assert person_reason(record) == SENT_AND_CHANGED  # $5 can't be this app's $12, so no lucky GET has to show it again
    if then == "an older copy beside the fresh payment":
        story.records, _ = poll(story.records, [(older, fresh)], MONDAY + 4 * MINUTE, store=FERNHILL)
    else:
        story.poll(MONDAY + 4 * MINUTE)
    record = story["ear_tips"]
    assert person_reason(record) == SENT_AND_CHANGED
    with pytest.raises(Refused, match=f"^{re.escape(SENT_AND_CHANGED)}$"):
        sending(record, MONDAY + 4 * MINUTE)  # if the first try never landed, a retry refunds Jamie another $12


def test_an_unclear_refund_while_a_response_is_sent_goes_to_a_person():
    story = being_sent("earbuds")  # Priya's accept, with no refund of its own
    story.sandbox.change("dst_earbuds", refunds=[{"id": "rfd_someone"}])  # the payment doesn't show it yet
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["earbuds"]).startswith("The refunds so far changed while a response was being sent.")


def test_an_unclear_refund_holds_back_our_own_refund():
    story = being_sent("ear_tips")
    story.sandbox.change("dst_ear_tips", refunds=[{"id": "rfd_ear_tips"}])  # likely ours, but the payment doesn't say
    story.poll(MONDAY + 2 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.SENT
    assert blocker(story["ear_tips"], MONDAY + 2 * MINUTE) == "It isn't clear what was already refunded on this payment."


def test_a_change_decide_reads_while_a_response_is_sent_goes_to_a_person():
    story = being_sent()
    story.sandbox.change("dst_headphones", reason={"original_code": "13.1"})
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["headphones"]) == ("The reason code changed while a response was being sent. A person "
                                                  "checks what Airwallex has.")


def test_a_retry_stops_at_the_earlier_of_two_deadlines():
    story = being_sent()
    soon = MONDAY + 5 * MINUTE
    story.sandbox.change("dst_headphones", due_at=soon.isoformat())  # the bank moved the deadline up
    story.poll(MONDAY + 2 * MINUTE)
    assert phase(story["headphones"]) is Phase.SENT and blocker(story["headphones"], MONDAY + 2 * MINUTE) is None
    assert blocker(story["headphones"], soon) == "The deadline has passed."
    story.poll(soon)
    assert person_reason(story["headphones"]).startswith("This app's response doesn't show on the dispute, and the "
                                                         "deadline has passed.")
    story = being_sent()
    plan = story["headphones"].rounds[0].plan
    story.sandbox.change("dst_headphones", due_at=(plan.terms.due_at + timedelta(days=3)).isoformat())  # or put it off
    story.poll(plan.terms.due_at)
    assert person_reason(story["headphones"]).startswith("This app's response doesn't show on the dispute, and the "
                                                         "deadline has passed.")


def test_a_sent_plan_is_never_retried_once_airwallex_stops_waiting():
    story = being_sent()
    story.sandbox.change("dst_headphones", status="CHALLENGED")  # it landed, but the entry doesn't show yet
    story.poll(MONDAY + 2 * MINUTE)
    record = story["headphones"]
    assert phase(record) is Phase.SENT
    assert blocker(record, MONDAY + 2 * MINUTE) == "Airwallex no longer waits for this response."
    with pytest.raises(Refused, match="^Airwallex no longer waits for this response.$"):
        sending(record, MONDAY + 2 * MINUTE)
    assert blocker(with_plan(record, approved_by=None), MONDAY + 2 * MINUTE) == "It waits for a person's approval."


def test_a_late_reply_still_counts_as_ours():
    story = being_sent()
    story.sandbox.escalate("dst_headphones")  # the bank moved on before the challenge showed
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK]
    assert "counts as turned down" in record.rounds[0].notes[0]
    assert rejected(record) == FIRST_PACKAGE and as_dispute(record).prior_challenges == 1
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY", stage="RFI")
    story.poll(THREE_WEEKS_LATER + MINUTE)
    record = story["headphones"]
    assert record.rounds[0].reply.by == ACTOR and record.others == 0
    assert rejected(record) == FIRST_PACKAGE and phase(record) is Phase.DECIDE


def test_a_late_accept_entry_books_its_refund():
    story = being_sent("ear_tips")
    story.sandbox.post(story["ear_tips"], show=False)  # ACCEPTED shows before the accept entry
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    note = "When the case closed, this app's accept didn't show on the dispute yet. Its refund is booked when it shows."
    assert phase(record) is Phase.CLOSED and record.bookings == () and record.rounds[0].notes[-1] == note
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")
    story.poll(MONDAY + 3 * MINUTE)
    record = story["ear_tips"]
    assert keys(record) == ["refund"] and cost(record) == Decimal("12.30") and phase(record) is Phase.CLOSED
    assert record.rounds[0].notes[-1] == note  # still true once the accept shows


@pytest.mark.parametrize("at_close, between, at_entry", [
    (17, None, 17), (17, None, 12), (17, None, 0), (12, None, 17), (0, None, 17), (17, 12, 12), (17, 0, 0)], ids=[
    "a fresh payment", "its GET lags at 12", "its GET lags at 0", "the $5 shows with it", "the close's GET lags at 0",
    "two GETs lag at 12", "two GETs lag at 0"])
def test_someone_elses_refund_beside_our_late_accept_goes_to_a_person(at_close, between, at_entry):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order
    story.sandbox.post(story["ear_tips"], show=False)  # ACCEPTED shows before the accept entry
    story.sandbox.pay("dst_ear_tips", refunded=at_close)  # 17: this app's $12, and $5 the store refunded in the web app
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert record.closed is Status.ACCEPTED and record.bookings == ()  # this app's refund waits for its entry
    assert person_reason(record) == (OTHER_REFUNDS if at_close > 12 else None)  # more than it can refund goes now
    if between is not None:  # a closed case applies a GET whose payment lags
        story.sandbox.pay("dst_ear_tips", refunded=between)
        story.poll(MONDAY + 3 * MINUTE)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")
    story.sandbox.pay("dst_ear_tips", refunded=at_entry)  # any GET may lag: 0 with refunds listed reads as unclear
    story.poll(MONDAY + 4 * MINUTE)
    record = story["ear_tips"]
    assert keys(record) == ["refund"]
    assert person_reason(record) == OTHER_REFUNDS


@pytest.mark.parametrize("back_office", [5, 18], ids=["the store refunded $5", "the store refunded the rest"])
def test_a_close_that_shows_more_than_our_unshown_accept_can_refund_goes_to_a_person(back_office):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: the POST never reached Airwallex
    story.sandbox.pay("dst_ear_tips", refunded=back_office + 12)  # a back-office refund, then the store's web-app accept
    story.sandbox.change("dst_ear_tips", status="ACCEPTED", refunds=[{"id": "rfd_web_app"}])  # whose entry never shows
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert record.closed is Status.ACCEPTED and record.bookings == ()  # this app's refund waits for its entry
    assert person_reason(record) == OTHER_REFUNDS  # but this app's accept can't explain more than $12 of it
    story.poll(MONDAY + timedelta(days=30))
    assert person_reason(story["ear_tips"]) == OTHER_REFUNDS
    booked = book(story["ear_tips"], "Refunds to Jamie", money(back_office + 12), "Priya", MONDAY + timedelta(days=30))
    assert phase(booked) is Phase.CLOSED  # Priya books what moved


def test_someone_elses_refund_before_an_rfi_close_on_a_lagging_payment_goes_to_a_person():
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order
    plan = story["ear_tips"].rounds[0].plan
    story.records["dst_ear_tips"] = taken(story["ear_tips"], plan.request_id, MONDAY + MINUTE)  # a 2xx
    story.sandbox.pay("dst_ear_tips", refunded=12)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")  # the status still asks
    story.sandbox.change("dst_ear_tips", refunds=[{"id": "rfd_dst_ear_tips"}])
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=17)  # and $5 the store refunded in the web app
    story.poll(MONDAY + 3 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=0)  # the closing GET's payment lags, while the refunds are listed
    story.sandbox.resolve("dst_ear_tips", Status.ACCEPTED)
    story.poll(MONDAY + 4 * MINUTE)
    record = story["ear_tips"]
    assert record.last.dispute.refunded_amount == 17 and record.closed is Status.ACCEPTED  # unclear keeps what was seen
    assert person_reason(record) == OTHER_REFUNDS


@pytest.mark.parametrize("closing", [0, 12], ids=["the close lags at 0", "the close lags at 12"])
def test_two_lagging_payment_gets_before_an_rfi_close_still_flag_someone_elses_refund(closing):
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order
    plan = story["ear_tips"].rounds[0].plan
    story.records["dst_ear_tips"] = taken(story["ear_tips"], plan.request_id, MONDAY + MINUTE)  # a 2xx
    story.sandbox.pay("dst_ear_tips", refunded=12)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")  # the status still asks
    story.sandbox.change("dst_ear_tips", refunds=[{"id": "rfd_dst_ear_tips"}])
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=17)  # and $5 the store refunded in the web app
    story.poll(MONDAY + 3 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=0)  # two payment GETs lag: 0 with refunds listed reads as unclear
    story.poll(MONDAY + 4 * MINUTE)
    assert story["ear_tips"].last.dispute.refunded_amount == 17
    story.sandbox.pay("dst_ear_tips", refunded=closing)
    story.sandbox.resolve("dst_ear_tips", Status.ACCEPTED)
    story.poll(MONDAY + 5 * MINUTE)
    if closing:  # less than the record saw: an old copy, until the payment's GET catches up
        assert person_reason(story["ear_tips"]) == REFUND_FELL and story["ear_tips"].closed is None
        story.sandbox.pay("dst_ear_tips", refunded=17)
        story.poll(MONDAY + 6 * MINUTE)
    assert story["ear_tips"].closed is Status.ACCEPTED and person_reason(story["ear_tips"]) == OTHER_REFUNDS


@pytest.mark.parametrize("gap", ["no latest_payment_attempt", "refunded_amount null"])
def test_an_unclear_payment_never_hides_a_refund_from_the_close(gap):
    story = Story(cases=("headphones",))
    story.run(MONDAY)  # this app's RFI challenge shows: the bank decides
    story.sandbox.pay("dst_headphones", refunded=1249)  # the store refunds Alex in its back office
    story.poll(MONDAY + timedelta(hours=1))
    shown = copy.deepcopy(story.sandbox.payments["int_headphones"])
    if gap == "no latest_payment_attempt":
        del story.sandbox.payments["int_headphones"]["latest_payment_attempt"]
    else:
        story.sandbox.payments["int_headphones"]["latest_payment_attempt"]["refunded_amount"] = None
    story.poll(MONDAY + timedelta(hours=2))  # the payment doesn't say what was refunded
    story.sandbox.payments["int_headphones"] = copy.deepcopy(shown)
    story.sandbox.pay("dst_headphones", refunded=0)  # the closing GET's payment lags
    story.sandbox.resolve("dst_headphones", Status.WON)
    story.poll(MONDAY + timedelta(days=3))
    assert person_reason(story["headphones"]) == REFUND_FELL  # a refund never goes down
    story.sandbox.payments["int_headphones"] = shown
    story.poll(MONDAY + timedelta(days=4))
    record = story["headphones"]
    assert record.closed is Status.WON and person_reason(record) == OTHER_REFUNDS  # the store's $1,249 is for a person


def test_an_unclear_payment_never_lets_code_accept_a_chargeback_after_a_refund():
    story = Story(cases=("ear_tips",))
    story.sandbox.payments["int_ear_tips"]["captured_amount"] = 30  # $12 of a $30 order
    story.sandbox.pay("dst_ear_tips", refunded=12)  # the store refunded Jamie before this app looked
    story.poll(MONDAY)
    story.decide(MONDAY)  # "$12.00 was already refunded": a person handles round one
    attempt = story.sandbox.payments["int_ear_tips"].pop("latest_payment_attempt")
    story.poll(MONDAY + timedelta(days=1))  # the payment doesn't say what was refunded
    story.sandbox.payments["int_ear_tips"]["latest_payment_attempt"] = {**attempt, "refunded_amount": 0}  # then it lags
    later = MONDAY + timedelta(days=2)  # and the bank opens a chargeback anyway
    story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(later + timedelta(days=7)).isoformat())
    story.poll(later)
    record = story["ear_tips"]
    assert person_reason(record) == REFUND_FELL and blocker(record, later) == REFUND_FELL
    with pytest.raises(Refused, match="It is at PERSON."):
        keep(record, None, later, round_number=len(record.rounds))  # code's $12 accept at the chargeback never goes
    story.sandbox.payments["int_ear_tips"]["latest_payment_attempt"] = attempt  # once the payment's GET catches up
    story.poll(later + MINUTE)
    assert len(story["ear_tips"].rounds) == 2
    assert "already refunded" in decide(as_dispute(story["ear_tips"]), None, later + MINUTE).reasons[0]


def test_a_refund_beside_an_older_copy_counts_when_the_last_reading_was_unclear():
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order
    plan = story["ear_tips"].rounds[0].plan
    story.records["dst_ear_tips"] = taken(story["ear_tips"], plan.request_id, MONDAY + MINUTE)  # a 2xx
    unlisted = copy.deepcopy(story.sandbox.reads())
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")  # the RFI still asks
    story.sandbox.change("dst_ear_tips", refunds=[{"id": "rfd_dst_ear_tips"}])  # the payment still says 0
    story.poll(MONDAY + 2 * MINUTE)
    assert story["ear_tips"].last.dispute.refunded_amount.is_nan()
    older = copy.deepcopy(story.sandbox.disputes["dst_ear_tips"])
    story.sandbox.change("dst_ear_tips")
    story.poll(MONDAY + 3 * MINUTE)
    story.records, _ = poll(story.records, unlisted, MONDAY + 3 * MINUTE, store=FERNHILL)  # an old copy of both
    assert story["ear_tips"].last.dispute.refunded_amount.is_nan()  # nothing refunded there isn't news: it stays unclear
    story.sandbox.pay("dst_ear_tips", refunded=24)  # this app's $12 and $12 the store refunded in its back office
    payment = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.records, _ = poll(story.records, [(older, payment)], MONDAY + 4 * MINUTE, store=FERNHILL)
    assert story["ear_tips"].last.dispute.refunded_amount == 24  # a refund never goes down: that payment is newer
    story.sandbox.pay("dst_ear_tips", refunded=0)  # the closing GET's payment lags, with the refunds listed
    story.sandbox.resolve("dst_ear_tips", Status.ACCEPTED)
    story.poll(MONDAY + 5 * MINUTE)
    assert person_reason(story["ear_tips"]) == OTHER_REFUNDS


@pytest.mark.parametrize("bank", ["chargeback expired", "RFI lost"])
def test_our_late_refund_on_a_case_a_person_booked_goes_back_to_them(bank):
    story = being_sent("ear_tips")  # code's $12 accept, refund included
    if bank == "chargeback expired":  # the bank moved on while the accept was being sent, and it landed at RFI
        story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(MONDAY + timedelta(days=9)).isoformat())
        story.poll(MONDAY + 2 * MINUTE)
        story.sandbox.pay("dst_ear_tips", refunded=12)  # its refund shows, its entry lags
        story.sandbox.resolve("dst_ear_tips", Status.EXPIRED)
        booked, later = money(12), MONDAY + timedelta(days=10)
    else:
        story.sandbox.pay("dst_ear_tips", refunded=12)
        story.sandbox.change("dst_ear_tips", status="LOST")  # no entry shows yet
        booked, later = money("12.30"), MONDAY + 2 * MINUTE
    story.poll(later)
    assert phase(story["ear_tips"]) is Phase.PERSON  # the close asks a person to book what moved
    story.records["dst_ear_tips"] = book(story["ear_tips"], "Refund to Jamie", booked, "Priya", later + MINUTE)
    assert phase(story["ear_tips"]) is Phase.CLOSED
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # then it shows
    story.poll(later + 2 * MINUTE)
    record = story["ear_tips"]
    assert keys(record)[-2:] == ["person:1", "refund"]  # this app's refund, booked on top of Priya's
    assert person_reason(record) == LATE_REFUND


@pytest.mark.parametrize("closes", [False, True], ids=["chargeback still open", "it shows with the close"])
def test_our_late_refund_after_a_person_booked_the_open_case_goes_back_to_them(closes):
    story = being_sent("ear_tips")  # code's $12 accept, refund included
    story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=12)  # it landed at RFI: its refund shows, its entry lags
    story.poll(MONDAY + 3 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.PERSON and story["ear_tips"].closed is None  # it may still land
    story.records["dst_ear_tips"] = book(story["ear_tips"], "Refund to Jamie", money(12), "Priya", MONDAY + 4 * MINUTE)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # then it shows
    if closes:  # on the GET that closes the chargeback too
        story.sandbox.resolve("dst_ear_tips", Status.EXPIRED)
    story.poll(MONDAY + timedelta(days=10) if closes else MONDAY + 5 * MINUTE)
    record = story["ear_tips"]
    assert keys(record)[1:3] == ["person:1", "refund"]  # this app's refund, booked on top of Priya's
    assert person_reason(record) == LATE_REFUND


@pytest.mark.parametrize("answer, refunded", [(None, 12), (Action.ACCEPT, 12), (None, 17)],
                         ids=["chargeback expired", "accepted in the web app", "and the store refunded $5"])
def test_our_late_refund_after_the_close_asked_a_person_to_book_it_corrects_the_line(answer, refunded):
    story = being_sent("ear_tips", captured=30)  # code's $12 accept on a $30 order
    story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=refunded)  # it landed at RFI: its refund shows, its entry lags
    if answer:
        story.sandbox.reply("dst_ear_tips", answer, SOMEONE, "VALID_CUSTOMER_DISPUTE")
    story.sandbox.resolve("dst_ear_tips", Status.ACCEPTED if answer else Status.EXPIRED)
    later = MONDAY + timedelta(days=10)
    story.poll(later)
    assert person_reason(story["ear_tips"]) == OTHER_REFUNDS  # true as far as the record knows
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # then it shows
    story.poll(later + MINUTE)
    record = story["ear_tips"]
    assert keys(record)[-1] == "refund" and cost(record) == Decimal("39.30") and record.rounds[-1].notes[-1] == LATE_REFUND
    # Priya sees this before she books: the line is wrong now only if this app's refund was all the payment showed.
    assert person_reason(record) == (OTHER_REFUNDS if refunded > 12 else LATE_REFUND)
    booked = book(record, "Nothing more moved", ZERO, "Priya", later + 2 * MINUTE)
    assert phase(booked) is Phase.CLOSED and cost(booked) == Decimal("39.30")


@pytest.mark.parametrize("at_close, between, at_entry", [(17, 12, 12), (17, 0, 0), (12, None, 17)],
                         ids=["two GETs lag at 12", "two GETs lag at 0", "the store's $5 shows with the entry"])
def test_someone_elses_refund_keeps_the_close_line_when_our_late_refund_shows(at_close, between, at_entry):
    story = being_sent("ear_tips", captured=30)  # code's $12 accept on a $30 order
    story.sandbox.change("dst_ear_tips", stage="CHARGEBACK", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=at_close)  # it landed at RFI: its refund shows, its entry lags
    story.sandbox.resolve("dst_ear_tips", Status.EXPIRED)
    later = MONDAY + timedelta(days=10)
    story.poll(later)
    assert person_reason(story["ear_tips"]) == OTHER_REFUNDS
    if between is not None:  # a closed case applies a GET whose payment lags
        story.sandbox.pay("dst_ear_tips", refunded=between)
        story.poll(later + MINUTE)
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # then it shows
    story.sandbox.pay("dst_ear_tips", refunded=at_entry)
    story.poll(later + 2 * MINUTE)
    record = story["ear_tips"]
    assert keys(record)[-1] == "refund"
    assert person_reason(record) == OTHER_REFUNDS  # the store's $5 is still for a person to book


@pytest.mark.parametrize("stage, status", [("RFI", "LOST"), ("PRE_CHARGEBACK", "ACCEPTED")],
                         ids=["RFI lost", "pre-chargeback accepted in the web app"])
def test_our_late_refund_after_a_close_asked_a_person_to_book_tells_them_its_booked(stage, status):
    story = being_sent("ear_tips")  # code's $12 accept, refund included
    if stage != "RFI":  # the bank moved on while the accept was being sent
        story.sandbox.change("dst_ear_tips", stage=stage, due_at=(MONDAY + timedelta(days=9)).isoformat())
        story.poll(MONDAY + 2 * MINUTE)
        story.sandbox.reply("dst_ear_tips", Action.ACCEPT, SOMEONE, "VALID_CUSTOMER_DISPUTE")  # Priya, in the web app
    story.sandbox.pay("dst_ear_tips", refunded=12)  # it landed at RFI: its refund shows, its entry lags
    story.sandbox.change("dst_ear_tips", status=status)
    later = MONDAY + timedelta(days=1)
    story.poll(later)
    assert person_reason(story["ear_tips"]).endswith("Code can't tell what money moved, so a person books it.")
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION", stage="RFI")  # then it shows
    story.poll(later + MINUTE)
    record = story["ear_tips"]
    assert keys(record) == ["refund"] and cost(record) == Decimal("12.30")
    # Before Priya books what moved, the round she handles says this app booked its refund.
    assert phase(record) is Phase.PERSON and LATE_REFUND in record.rounds[-1].notes
    booked = book(record, "Nothing more moved", ZERO, "Priya", later + 2 * MINUTE)
    assert phase(booked) is Phase.CLOSED and cost(booked) == Decimal("12.30")


def test_a_refused_send_goes_to_a_person_and_keeps_the_plan():
    story = being_sent("ear_tips")
    plan = story["ear_tips"].rounds[0].plan
    failed = send_failed(story["ear_tips"], plan.request_id, "validation_error: refund.reason.", MONDAY + MINUTE)
    assert phase(failed) is Phase.PERSON and failed.rounds[0].plan.refused == "validation_error: refund.reason."
    assert blocker(failed, MONDAY + MINUTE).startswith("Airwallex refused the response")
    with pytest.raises(Refused):
        send_failed(failed, plan.request_id, "again", MONDAY + MINUTE)


def test_a_refund_is_checked_again_when_it_goes():
    record = keep(one_case("ear_tips")["ear_tips"], None, MONDAY, round_number=1)
    tampered = with_plan(record, terms=replace(record.rounds[0].plan.terms, refund_amount=money(13)))
    assert blocker(tampered, MONDAY) == "$13.00 is more than the $12.00 in dispute."


def codes_accept_at_round_two():
    record = at_round_two(ENDING_2)["headphones"]
    return keep(record, ENDING_2_ANSWER.evidence(), THREE_WEEKS_LATER, round_number=2)


def the_earbuds_draft():
    story = one_case("earbuds")
    story.decide(MONDAY)
    return story["earbuds"]


@pytest.mark.parametrize("planned, changes", [
    (lambda: waiting_for_priya()["headphones"], {"amount": money(30)}),  # a challenge needs a person at any amount
    (codes_accept_at_round_two, {}),  # code's own accept of more than $50
    (the_earbuds_draft, {"amount": money(30)}),  # a hand-off draft, even under $50
], ids=["challenge", "accept over $50", "hand-off draft"])
def test_a_saved_flag_cant_skip_a_person(planned, changes):
    record = planned()
    plan = record.rounds[-1].plan
    tampered = with_plan(record, needs_approval=False, terms=replace(plan.terms, **changes))
    assert phase(tampered) is Phase.APPROVE and blocker(tampered, record.last.at) == "It waits for a person's approval."


def test_only_final_answers_are_kept():
    with pytest.raises(Refused, match="The engine says GATHER_EVIDENCE."):
        keep(one_case()["headphones"], None, MONDAY, round_number=1)


def test_a_hand_off_without_a_draft_stays_with_a_person_for_the_round():
    story = one_case()
    story.sandbox.change("dst_headphones", currency="EUR")
    story.poll(MONDAY + MINUTE)
    record = keep(story["headphones"], ANSWERS["headphones"].evidence(), MONDAY + MINUTE, round_number=1)
    assert phase(record) is Phase.PERSON and person_reason(record).startswith("fees-v1 only has USD fees")
    with pytest.raises(Refused, match="It is at PERSON."):
        keep(record, ANSWERS["headphones"].evidence(), MONDAY + MINUTE, round_number=1)
    story.records["dst_headphones"] = record
    story.sandbox.escalate("dst_headphones")
    story.sandbox.change("dst_headphones", currency="USD")
    story.poll(THREE_WEEKS_LATER)
    assert phase(story["headphones"]) is Phase.DECIDE  # the next round is the agent's again


def test_two_disputes_on_one_payment_go_to_a_person():
    story = Story(cases=("ear_tips",))
    story.sandbox.open("ear_tips", dispute_id="dst_ear_tips_again", payment_id="int_ear_tips")
    story.poll(MONDAY)
    for record in story.records.values():
        assert phase(record) is Phase.PERSON and person_reason(record).startswith("Another dispute is on this payment.")
        with pytest.raises(Refused, match="It is at PERSON."):
            keep(record, None, MONDAY, round_number=1)


def test_a_second_dispute_on_the_payment_counts_even_when_it_cant_be_read():
    story = Story(cases=("ear_tips",))
    story.sandbox.open("ear_tips", dispute_id="dst_chargeback_on_ear_tips", payment_id="int_ear_tips")
    story.sandbox.change("dst_chargeback_on_ear_tips", stage="CHARGEBACK", status="SOMETHING_NEW")
    problems = story.poll(MONDAY)
    assert len(problems) == 1 and sorted(story.records) == ["dst_ear_tips"]
    assert person_reason(story["ear_tips"]).startswith("Another dispute is on this payment.")


@pytest.mark.parametrize("spoil", [
    lambda d, p: (d, {}),  # the payment GET failed: only the dispute names the payment
    lambda d, p: ({k: v for k, v in d.items() if k != "payment_intent_id"}, p),  # only the payment does
], ids=["the dispute names it", "the payment names it"])
def test_an_unreadable_second_dispute_counts_whichever_response_names_the_payment(spoil):
    sandbox = Sandbox()
    sandbox.open("ear_tips")
    sandbox.open("ear_tips", dispute_id="dst_chargeback_on_ear_tips", payment_id="int_ear_tips")
    reads = sandbox.reads()
    reads[1] = spoil(*reads[1])
    records, problems = poll({}, reads, MONDAY, store=FERNHILL)
    assert len(problems) == 1 and sorted(records) == ["dst_ear_tips"]
    assert person_reason(records["dst_ear_tips"]).startswith("Another dispute is on this payment.")


def test_an_old_closed_dispute_on_the_payment_counts_too():
    story = Story(cases=("headphones",))
    story.sandbox.open("headphones", dispute_id="dst_old_chargeback", payment_id="int_headphones")
    story.sandbox.change("dst_old_chargeback", stage="CHARGEBACK", status="LOST")  # a chargeback leaves the refunds alone
    story.poll(MONDAY)
    old = story.records["dst_old_chargeback"]
    assert old.history_only and phase(old) is Phase.CLOSED and cost(old) == at_risk(old) == ZERO
    assert person_reason(story["headphones"]).startswith("Another dispute is on this payment.")


# ----------------------------------------------------------------------------- readings code can't place
def test_a_stage_going_back_blocks_until_a_good_reading():
    story = at_round_two()
    before = story["headphones"]
    story.sandbox.change("dst_headphones", stage="RFI")
    story.poll(THREE_WEEKS_LATER + MINUTE)
    blocked = story["headphones"]
    assert blocked.blocked == "The stage went back from CHARGEBACK to RFI. A person checks it."
    assert phase(blocked) is Phase.PERSON and blocked.rounds == before.rounds and blocked.bookings == before.bookings
    story.sandbox.change("dst_headphones", stage="CHARGEBACK")
    story.poll(THREE_WEEKS_LATER + 2 * MINUTE)
    assert story["headphones"].blocked is None and phase(story["headphones"]) is Phase.DECIDE


def test_an_ask_at_the_same_stage_after_a_response_blocks():
    story = Story()
    story.run(MONDAY)
    story.sandbox.change("dst_headphones", status="REQUIRES_RESPONSE")
    story.poll(MONDAY + timedelta(hours=1))
    assert story["headphones"].blocked == "The bank asks again at the same stage after a response. A person checks it."


def test_an_ask_after_an_accept_blocks():
    story = one_case("earbuds")
    story.sandbox.reply("dst_earbuds", Action.ACCEPT, SOMEONE, "VALID_CUSTOMER_DISPUTE")
    story.poll(MONDAY + MINUTE)  # the accept shows before the status moves: someone answered, so the case waits
    assert phase(story["earbuds"]) is Phase.WAIT
    story.sandbox.change("dst_earbuds", due_at=(MONDAY + timedelta(days=9)).isoformat())
    story.poll(MONDAY + 2 * MINUTE)
    assert story["earbuds"].blocked == "The bank asks for a response after the case was accepted. A person checks it."


def test_a_reply_that_shows_before_the_status_moves_isnt_read_as_a_new_ask():
    story = being_sent("ear_tips")
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, ACTOR, "LOW_VALUE_TRANSACTION")  # the status still asks
    for minutes in (2, 3):  # the same reading twice
        story.poll(MONDAY + minutes * MINUTE)
        assert phase(story["ear_tips"]) is Phase.WAIT and story["ear_tips"].blocked is None
    assert cost(story["ear_tips"]) == Decimal("12.30") and at_risk(story["ear_tips"]) == ZERO  # counted once
    story.sandbox.change("dst_ear_tips")  # only updated_at moves
    story.poll(MONDAY + 4 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.WAIT and story["ear_tips"].blocked is None


def test_fewer_replies_than_before_blocks():
    story = Story()
    story.run(MONDAY)
    story.sandbox.escalate("dst_headphones")
    story.sandbox.change("dst_headphones", challenge_details=[])  # round one's challenge is gone from the list
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert record.blocked == "Airwallex now shows fewer responses on this dispute than before. A person checks it."
    assert len(record.rounds) == 1 and keys(record) == ["review:RFI"]


def test_an_expired_rfi_after_a_response_blocks():
    story = Story()
    story.run(MONDAY)
    story.sandbox.change("dst_headphones", status="EXPIRED")
    story.poll(MONDAY + timedelta(days=7))
    assert story["headphones"].blocked == "Airwallex marked the RFI stage expired after a response. A person checks it."


def test_an_expired_rfi_after_a_response_that_doesnt_show_blocks():
    story = being_sent()
    record = story["headphones"]
    story.records["dst_headphones"] = taken(record, record.rounds[0].plan.request_id, MONDAY + MINUTE)
    story.sandbox.change("dst_headphones", status="EXPIRED")
    story.poll(MONDAY + timedelta(days=1))
    assert story["headphones"].blocked == "Airwallex marked the RFI stage expired after a response. A person checks it."


def test_a_closed_case_that_changes_stays_blocked_and_its_ledger_stays():
    story = play()
    before = story["headphones"]
    story.sandbox.resolve("dst_headphones", Status.LOST)
    story.poll(THREE_WEEKS_LATER + timedelta(days=2))
    record = story["headphones"]
    assert record.blocked.startswith("Airwallex changed a closed case from PRE_ARBITRATION/WON to PRE_ARBITRATION/LOST.")
    assert record.bookings == before.bookings and phase(record) is Phase.PERSON


def dispute_with(change):
    sandbox = Sandbox()
    sandbox.open("headphones")
    dispute, payment = sandbox.reads()[0]
    return change(dispute, payment)


@pytest.mark.parametrize("change", [
    lambda d, p: ({k: v for k, v in d.items() if k != "id"}, p),
    lambda d, p: ({k: v for k, v in d.items() if k != "amount"}, p),
    lambda d, p: ({k: v for k, v in d.items() if k != "currency"}, p),
    lambda d, p: ({k: v for k, v in d.items() if k != "due_at"}, p),
    lambda d, p: ({**d, "id": None}, p),
    lambda d, p: ({**d, "currency": None}, p),
    lambda d, p: ({**d, "due_at": None}, p),
    lambda d, p: ({**d, "due_at": "2026-11-08T09:00:00"}, p),
    lambda d, p: ({**d, "due_at": "next week"}, p),
    lambda d, p: ({**d, "amount": "abc"}, p),
    lambda d, p: ({**d, "currency": 840}, p),
    lambda d, p: ({**d, "reason": "10.4"}, p),
    lambda d, p: ({**d, "stage": "SOMETHING_NEW"}, p),
    lambda d, p: ({**d, "status": "SOMETHING_NEW"}, p),
    lambda d, p: ({**d, "challenge_details": ["PURCHASE_HISTORY"]}, p),
    lambda d, p: ({**d, "challenge_details": [{"stage": "LATER", "challenged_by": ACTOR}]}, p),
    lambda d, p: ({**d, "accept_details": 7}, p),
    lambda d, p: ({**d, "updated_at": "2026-11-02T17:00:00"}, p),
    lambda d, p: ({**d, "issuer_comment": ["no"]}, p),
    lambda d, p: (d, {**p, "latest_payment_attempt": ["x"]}),
    lambda d, p: (d, {k: v for k, v in p.items() if k != "id"}),
    lambda d, p: (d, {**p, "id": "int_someone_else"}),
    lambda d, p: (["not", "a", "dispute"], p),
    lambda d, p: ({**d, "amount": "NaN"}, p),
    lambda d, p: ({**d, "amount": -1249}, p),
    lambda d, p: ({**d, "amount": 0}, p),
    lambda d, p: (d, {**p, "captured_amount": "Infinity"}),
    lambda d, p: (d, {**p, "latest_payment_attempt": {"refunded_amount": -5}}),
    lambda d, p: (d, {**p, "latest_payment_attempt": {"refunded_amount": "sNaN"}}),
    lambda d, p: ({**d, "id": "dst_\ud83d"}, p),
])
def test_one_bad_dispute_doesnt_stop_the_queue(change):
    sandbox = Sandbox()
    for case_id in ("ear_tips", "earbuds"):
        sandbox.open(case_id)
    bad = dispute_with(change)
    records, problems = poll({}, [*sandbox.reads(), bad], MONDAY, store=FERNHILL)
    assert sorted(records) == ["dst_ear_tips", "dst_earbuds"]
    assert len(problems) == 1 and problems[0].endswith("A person checks it.")
    named = isinstance(bad[0], dict) and isinstance(bad[0].get("id"), str)
    start = f"Dispute {bad[0]['id'].replace(chr(0xD83D), chr(0xFFFD))}" if named else "A dispute with no id"
    assert problems[0].startswith(f"{start} can't be read: ")
    assert not re.search(r"Error|Exception|Operation|<class|object has no|argument", problems[0])  # plain words only


def test_a_dispute_this_app_cant_handle_doesnt_stop_the_queue(monkeypatch):
    start = lifecycle._start

    def broken(reading, fees):
        if reading.dispute.id == "dst_earbuds":
            raise RuntimeError("a bug")
        return start(reading, fees)

    monkeypatch.setattr(lifecycle, "_start", broken)
    story = Story()
    problems = story.poll(MONDAY)
    assert problems == ("Dispute dst_earbuds couldn't be handled, because this app hit an error. A person checks it.",)
    assert sorted(story.records) == ["dst_ear_tips", "dst_headphones"]


def test_half_an_emoji_can_still_be_saved():
    dispute, payment = dispute_with(lambda d, p: ({**d, "issuer_comment": "Never came \ud83d", "challenge_details": [
        {"stage": "RFI", "reason": "PURCHASE_HISTORY \ud83d", "challenged_by": "Priya \ud83d"}]},
        {**p, "metadata": {"order_no": "1012 \ud83d"}}))
    reading = read(dispute, payment, MONDAY, store=FERNHILL)
    assert reading.issuer_comment == "Never came �" and reading.replies[0].by == "Priya �"
    assert reading.replies[0].reason == "PURCHASE_HISTORY �" and reading.order_number == "1012 �"
    assert to_json(observe(None, reading)).encode("utf-8")
    with pytest.raises(Unreadable, match="isn't valid text"):
        read({**dispute, "id": "dst_\ud83d"}, payment, MONDAY)


def test_text_that_skips_read_can_still_be_saved():
    story = being_sent("ear_tips")
    record = story["ear_tips"]
    error = json.loads('"validation_error: file \\ud83d"')  # Airwallex's message, cut in the middle of an emoji
    failed = send_failed(record, record.rounds[0].plan.request_id, error, MONDAY + MINUTE)
    booked = book(failed, json.loads('"Gift card \\ud83c"'), money(5), "Priya", MONDAY + 2 * MINUTE)
    text = to_json(booked)
    assert text.isascii() and from_json(text) == booked


def test_a_record_that_cant_be_read_any_more_is_blocked_until_it_can():
    story = one_case()
    story.sandbox.change("dst_headphones", status="SOMETHING_NEW")
    problems = story.poll(MONDAY + MINUTE)
    assert story["headphones"].blocked == problems[0]
    assert "'SOMETHING_NEW'" in problems[0] and phase(story["headphones"]) is Phase.PERSON
    story.sandbox.change("dst_headphones", status="REQUIRES_RESPONSE")
    story.poll(MONDAY + 2 * MINUTE)
    assert story["headphones"].blocked is None and phase(story["headphones"]) is Phase.DECIDE


def test_an_older_reading_is_ignored():
    story = one_case()
    stale = story.sandbox.reads()
    story.sandbox.change("dst_headphones", status="CHALLENGED")
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PURCHASE_HISTORY")
    story.poll(MONDAY + MINUTE)
    record = story["headphones"]
    records, _ = poll(story.records, stale, MONDAY + 2 * MINUTE, store=FERNHILL)
    assert records["dst_headphones"] == record
    records, _ = poll(story.records, story.sandbox.reads(), MONDAY, store=FERNHILL)  # read before the last one
    assert records["dst_headphones"] == record


@pytest.mark.parametrize("lags", [False, True], ids=["then a fresh reading", "then the payment's GET lags"])
@pytest.mark.parametrize("late, why", [
    (MINUTE, "The dispute and its payment came back out of step, so one of them may be an old copy. A person checks it."),
    (FRESH + MINUTE, "Airwallex returned an older copy of this dispute than one this app read before. A person checks it."),
    (None, "This app's clock went back, but Airwallex's dates say this reading is newer. A person checks it."),
], ids=["a minute late", "eleven minutes late", "this app's clock went back"])
def test_a_fresh_payment_behind_an_older_copy_of_the_dispute_drops_the_plan(late, why, lags):
    story = Story(cases=("earbuds",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.approve(MONDAY)  # Priya approves code's accept draft
    older = copy.deepcopy(story.sandbox.disputes["dst_earbuds"])
    story.sandbox.change("dst_earbuds")  # only updated_at moves
    story.poll(MONDAY + MINUTE)
    assert blocker(story["earbuds"], MONDAY + MINUTE) is None
    before = copy.deepcopy(story.sandbox.payments["int_earbuds"])
    story.sandbox.pay("dst_earbuds", refunded=189)  # the store refunded Robin in its back office
    payment = copy.deepcopy(story.sandbox.payments["int_earbuds"])
    if late is None:  # Airwallex's dates move on, but this app's clock went back
        story.sandbox.change("dst_earbuds")
        now, dispute = MONDAY, copy.deepcopy(story.sandbox.disputes["dst_earbuds"])
    else:  # the dispute's GET lags
        now, dispute = MONDAY + MINUTE + late, older
    story.records, _ = poll(story.records, [(dispute, payment)], now, store=FERNHILL)
    record = story["earbuds"]
    assert blocker(record, now) == why and "The plan was dropped: the refunds so far changed." in record.rounds[0].notes
    assert record.rounds[0].plan is None  # Priya's approval went with it: nothing goes out
    assert record.last.dispute.refunded_amount == 189  # a refund never goes down, so that payment GET is the newer one
    if lags:  # the dispute's GET is fresh again, but the payment's lags: it says nothing was refunded
        story.sandbox.payments["int_earbuds"] = before
    story.poll(max(now, MONDAY + MINUTE) + MINUTE)
    record = story["earbuds"]
    assert record.rounds[0].plan is None and record.blocked == (REFUND_FELL if lags else None)


@pytest.mark.parametrize("change, named", [
    (lambda sandbox: sandbox.payments["int_earbuds"].update(captured_amount=150), "the captured amount changed"),
    (lambda sandbox: sandbox.pay("dst_earbuds", order_no=FERNHILL.case("ear_tips").order_number), "the customer changed"),
], ids=["captured amount", "customer"])
def test_any_payment_change_behind_an_older_copy_of_the_dispute_drops_the_plan(change, named):
    story = Story(cases=("earbuds",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.approve(MONDAY)
    older = copy.deepcopy(story.sandbox.disputes["dst_earbuds"])
    story.sandbox.change("dst_earbuds")
    story.poll(MONDAY + MINUTE)
    before = copy.deepcopy(story.sandbox.payments["int_earbuds"])
    change(story.sandbox)
    payment = copy.deepcopy(story.sandbox.payments["int_earbuds"])
    story.records, _ = poll(story.records, [(older, payment)], MONDAY + 2 * MINUTE, store=FERNHILL)
    story.sandbox.payments["int_earbuds"] = before  # the next GET's payment lags
    story.poll(MONDAY + 3 * MINUTE)
    record = story["earbuds"]
    assert f"The plan was dropped: {named}." in record.rounds[0].notes and record.rounds[0].plan is None


@pytest.mark.parametrize("lags", [False, True], ids=["then a fresh reading", "then the payment's GET lags"])
def test_a_fresh_payment_beside_a_stage_that_went_back_drops_the_plan(lags):
    story = Story(cases=("earbuds",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.approve(MONDAY)  # Priya approves code's accept draft
    before = copy.deepcopy(story.sandbox.payments["int_earbuds"])
    story.sandbox.pay("dst_earbuds", refunded=189)  # the store refunded Robin in its back office
    story.sandbox.change("dst_earbuds", stage="RFI")  # while the dispute's GET has a glitch
    story.poll(MONDAY + MINUTE)
    record = story["earbuds"]
    assert record.blocked == "The stage went back from CHARGEBACK to RFI. A person checks it."
    assert "The plan was dropped: the refunds so far changed." in record.rounds[0].notes
    assert record.rounds[0].plan is None  # Priya's approval went with it: nothing goes out
    assert record.last.dispute.refunded_amount == 189 and record.last.dispute.stage is Stage.CHARGEBACK  # only the payment
    story.sandbox.change("dst_earbuds", stage="CHARGEBACK")
    if lags:  # the dispute's GET is right again, but the payment's lags: it says nothing was refunded
        story.sandbox.payments["int_earbuds"] = before
    story.poll(MONDAY + 2 * MINUTE)
    record = story["earbuds"]
    assert record.rounds[0].plan is None and record.blocked == (REFUND_FELL if lags else None)


@pytest.mark.parametrize("lags", [False, True], ids=["then a fresh reading", "then the payment's GET lags"])
def test_a_fresh_payment_beside_fewer_replies_drops_the_plan(lags):
    story = at_round_two(ENDING_2)
    story.decide(THREE_WEEKS_LATER)
    story.approve(THREE_WEEKS_LATER)  # Priya approves code's $1,249 accept
    before = copy.deepcopy(story.sandbox.payments["int_headphones"])
    story.sandbox.pay("dst_headphones", refunded=1249)  # the store refunded Alex in its back office
    shown = story.sandbox.disputes["dst_headphones"]["challenge_details"]
    story.sandbox.change("dst_headphones", challenge_details=[])  # while the dispute's GET drops round one's challenge
    story.poll(THREE_WEEKS_LATER + MINUTE)
    record = story["headphones"]
    assert record.blocked == "Airwallex now shows fewer responses on this dispute than before. A person checks it."
    assert "The plan was dropped: the refunds so far changed." in record.rounds[-1].notes
    assert record.rounds[-1].plan is None  # Priya's approval went with it: nothing goes out
    assert record.last.dispute.refunded_amount == 1249 and len(record.last.replies) == 1  # only the payment counts
    story.sandbox.change("dst_headphones", challenge_details=shown)
    if lags:  # the dispute's GET is right again, but the payment's lags: it says nothing was refunded
        story.sandbox.payments["int_headphones"] = before
    story.poll(THREE_WEEKS_LATER + 2 * MINUTE)
    record = story["headphones"]
    assert record.rounds[-1].plan is None and record.blocked == (REFUND_FELL if lags else None)


def test_a_refund_seen_beside_an_older_copy_still_counts_for_the_next_plan():
    story = Story(cases=("ear_tips",))
    story.sandbox.payments["int_ear_tips"]["captured_amount"] = 30  # $12 of a $30 order
    older = copy.deepcopy(story.sandbox.disputes["dst_ear_tips"])
    story.sandbox.change("dst_ear_tips")  # only updated_at moves
    story.poll(MONDAY)
    story.decide(MONDAY)  # code's own $12 accept, not sent yet
    lagging = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.sandbox.pay("dst_ear_tips", refunded=12)  # the store refunds Jamie in its back office
    fresh = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.records, _ = poll(story.records, [(older, fresh)], MONDAY + MINUTE, store=FERNHILL)
    assert story["ear_tips"].rounds[0].plan is None  # dropped: the refunds so far changed
    story.records, _ = poll(story.records, [(older, copy.deepcopy(lagging))], MONDAY + MINUTE, store=FERNHILL)
    assert story["ear_tips"].last.dispute.refunded_amount == 12  # an old copy of both can't lower what was seen
    story.sandbox.payments["int_ear_tips"] = lagging  # the dispute's GET is fresh again, but the payment's lags
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story["ear_tips"]) == REFUND_FELL  # so no new plan is made on it
    story.sandbox.payments["int_ear_tips"] = fresh
    story.poll(MONDAY + 3 * MINUTE)
    assert "already refunded" in decide(as_dispute(story["ear_tips"]), None, MONDAY + 3 * MINUTE).reasons[0]


def test_a_payment_that_shows_less_refunded_than_before_blocks_until_a_good_reading():
    story = being_sent("ear_tips", captured=30)  # code's own $12 accept on a $30 order: no answer came
    story.sandbox.pay("dst_ear_tips", refunded=12)  # this app's refund, or the store's
    story.poll(MONDAY + 2 * MINUTE)
    shown = copy.deepcopy(story.sandbox.payments["int_ear_tips"])
    story.sandbox.pay("dst_ear_tips", refunded=0)  # the payment's next GET lags
    story.sandbox.change("dst_ear_tips")  # while the dispute's moves on
    story.poll(MONDAY + 3 * MINUTE)
    record = story["ear_tips"]
    assert person_reason(record) == REFUND_FELL
    with pytest.raises(Refused):
        sending(record, MONDAY + 3 * MINUTE)  # if the $12 was the store's, a retry pays Jamie twice
    story.sandbox.payments["int_ear_tips"] = shown
    story.poll(MONDAY + 4 * MINUTE)
    assert story["ear_tips"].blocked is None and phase(story["ear_tips"]) is Phase.SENT


def test_readings_that_stay_older_go_to_a_person():
    story = Story()
    story.run(MONDAY)
    story.sandbox.change("dst_headphones")
    story.sandbox.disputes["dst_headphones"]["updated_at"] = "2062-11-02T17:00:30+00:00"  # a slip in Airwallex's date
    story.poll(MONDAY + timedelta(hours=1))
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert len(record.rounds) == 1 and record.blocked == ("Airwallex returned an older copy of this dispute than one this "
                                                          "app read before. A person checks it.")
    # The copy that reads as older is Airwallex's chargeback: the slip is what a person needs to know.
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert person_reason(record) == future == blocker(record, THREE_WEEKS_LATER)


def test_readings_that_stay_older_go_to_a_person_after_ten_minutes():
    story = Story()
    story.run(MONDAY)
    story.sandbox.change("dst_headphones")
    story.sandbox.disputes["dst_headphones"]["updated_at"] = "2062-11-02T17:00:30+00:00"
    slip = MONDAY + timedelta(hours=1)
    story.poll(slip)
    story.sandbox.escalate("dst_headphones", now=slip)
    story.poll(slip + FRESH)  # ten minutes on: a GET that lagged, so it's ignored
    assert story["headphones"].blocked is None
    story.poll(slip + FRESH + timedelta(seconds=1))
    assert story["headphones"].blocked.startswith("Airwallex returned an older copy of this dispute")


@pytest.mark.parametrize("planned", [False, True], ids=["before a plan", "after code's own accept"])
def test_a_date_slip_goes_to_a_person_so_nothing_goes_out_on_the_slipped_copy(planned):
    story = Story(cases=("ear_tips",))
    if planned:
        story.poll(MONDAY - MINUTE)
        story.records["dst_ear_tips"] = keep(story["ear_tips"], None, MONDAY - MINUTE, round_number=1)
        story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = "2062-11-02T17:00:30+00:00"  # a slip in Airwallex's date
    slipped = story.sandbox.reads()
    story.poll(MONDAY)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert person_reason(story["ear_tips"]) == future == blocker(story["ear_tips"], MONDAY)
    with pytest.raises(Refused, match="It is at PERSON."):
        keep(story["ear_tips"], None, MONDAY, round_number=1)
    story.sandbox.escalate("dst_ear_tips", now=MONDAY)  # the bank opens a chargeback: its copy reads as the older one
    story.poll(MONDAY + MINUTE)
    story.records, _ = poll(story.records, slipped, MONDAY + 2 * MINUTE, store=FERNHILL)  # a replica serves it again
    assert person_reason(story["ear_tips"]) == future == blocker(story["ear_tips"], MONDAY + 2 * MINUTE)
    story.sandbox.pay("dst_ear_tips", refunded=12)  # and the store refunds Jamie
    story.poll(MONDAY + 3 * MINUTE)  # the chargeback's copy, beside a payment that moved
    assert story["ear_tips"].blocked.startswith("The dispute and its payment came back out of step")
    assert person_reason(story["ear_tips"]) == future  # the copy isn't old: the slip is why it reads so
    story.poll(MONDAY + 3 * MINUTE + FRESH)  # ten minutes on, it reads as an old copy of the dispute
    assert story["ear_tips"].blocked.startswith("Airwallex returned an older copy of this dispute")
    assert person_reason(story["ear_tips"]) == future == blocker(story["ear_tips"], MONDAY + 3 * MINUTE + FRESH)
    story.sandbox.disputes["dst_ear_tips"]["status"] = "PAUSED"  # a status this code doesn't know
    story.poll(MONDAY + 4 * MINUTE + FRESH)
    assert person_reason(story["ear_tips"]).startswith("Dispute dst_ear_tips can't be read")  # its own line, not the slip


@pytest.mark.parametrize("ahead, caught", [(timedelta(days=2), False), (timedelta(days=2, seconds=1), True)],
                         ids=["two days ahead", "a second more"])
def test_a_date_slip_of_more_than_two_days_goes_to_a_person(ahead, caught):
    assert lifecycle.SLIPPED == timedelta(days=2)  # as FRESH is pinned: the app's clock must be within two days
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY - MINUTE)
    story.records["dst_ear_tips"] = keep(story["ear_tips"], None, MONDAY - MINUTE, round_number=1)  # code's own $12 accept
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = (MONDAY + ahead).isoformat()  # Airwallex's date runs ahead
    story.poll(MONDAY)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert (blocker(story["ear_tips"], MONDAY) == future) is caught  # past two days, nothing goes out on the slipped copy


def test_a_date_slip_on_a_closed_case_goes_to_a_person_so_a_reopen_isnt_missed():
    story = being_sent("ear_tips")  # code's own $12 accept
    story.sandbox.post(story["ear_tips"])  # it lands, and the case closes
    story.poll(MONDAY + 2 * MINUTE)
    assert phase(story["ear_tips"]) is Phase.CLOSED and cost(story["ear_tips"]) == Decimal("12.30")
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = "2062-11-02T17:00:30+00:00"  # a slip in Airwallex's date
    slipped = story.sandbox.reads()
    story.poll(MONDAY + 3 * MINUTE)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert person_reason(story["ear_tips"]) == future  # any newer copy would read as an old one, even a reopen
    story.sandbox.escalate("dst_ear_tips", now=MONDAY)  # the bank opens a chargeback: its copy reads as the older one
    story.poll(MONDAY + 4 * MINUTE)
    story.records, _ = poll(story.records, slipped, MONDAY + 5 * MINUTE, store=FERNHILL)  # a replica serves it again
    assert person_reason(story["ear_tips"]) == future and cost(story["ear_tips"]) == Decimal("12.30")


def test_a_date_slip_still_goes_to_a_person_when_a_replica_serves_the_slipped_copy_a_day_later():
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY - MINUTE)
    story.records["dst_ear_tips"] = keep(story["ear_tips"], None, MONDAY - MINUTE, round_number=1)  # code's own $12 accept
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = (MONDAY + timedelta(days=2, hours=12)).isoformat()  # a slip
    slipped = story.sandbox.reads()
    story.poll(MONDAY)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert blocker(story["ear_tips"], MONDAY) == future
    story.sandbox.escalate("dst_ear_tips", now=MONDAY + timedelta(hours=1))  # the bank opens a chargeback: it reads as older
    story.poll(MONDAY + timedelta(hours=1))
    later = MONDAY + timedelta(hours=12, minutes=10)  # past the slip, less two days
    story.records, _ = poll(story.records, slipped, later, store=FERNHILL)  # a replica serves the slipped copy again
    # Read again, the copy is no newer than it was: else the RFI accept and its $12 refund go out on a chargeback.
    assert blocker(story["ear_tips"], later) == future
    after = MONDAY + timedelta(days=2, hours=13)  # Airwallex's clock passes the slipped date
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = after.isoformat()  # so its chargeback reads as newer at last
    story.poll(after)
    record = story["ear_tips"]
    assert len(record.rounds) == 2 and phase(record) is Phase.DECIDE  # the slip is over, and the RFI's plan was dropped


@pytest.mark.parametrize("undate", [lambda d: d.pop("updated_at"), lambda d: d.update(updated_at=None)],
                         ids=["no updated_at", "updated_at is null"])
def test_a_date_slip_still_goes_to_a_person_when_a_copy_has_no_date(undate):
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY - MINUTE)
    story.records["dst_ear_tips"] = keep(story["ear_tips"], None, MONDAY - MINUTE, round_number=1)  # code's own $12 accept
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = (MONDAY + timedelta(days=3)).isoformat()  # a slip
    slipped = story.sandbox.reads()
    story.poll(MONDAY)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    story.sandbox.escalate("dst_ear_tips", now=MONDAY + timedelta(hours=1))  # the bank opens a chargeback: it reads as older
    story.poll(MONDAY + timedelta(hours=1))
    assert person_reason(story["ear_tips"]) == future
    undate(slipped[0][0])  # a GET serves the slipped copy without its date
    later = MONDAY + timedelta(hours=2)
    story.records, _ = poll(story.records, slipped, later, store=FERNHILL)
    # No date can show that the slip is over: else the RFI accept and its $12 refund go out on a chargeback.
    assert person_reason(story["ear_tips"]) == future == blocker(story["ear_tips"], later)
    with pytest.raises(Refused, match="Airwallex dated this dispute after this app read it"):
        sending(story["ear_tips"], later)
    after = MONDAY + timedelta(days=3, hours=1)  # Airwallex's clock passes the slipped date
    story.sandbox.change("dst_ear_tips")
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = after.isoformat()  # so its chargeback reads as newer at last
    story.poll(after)
    record = story["ear_tips"]
    assert len(record.rounds) == 2 and phase(record) is Phase.DECIDE  # the slip is over, and the RFI's plan was dropped


def test_a_copy_with_no_date_is_read_as_new_without_a_date_slip():
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY - MINUTE)
    story.sandbox.escalate("dst_ear_tips", now=MONDAY)  # the bank opens a chargeback
    del story.sandbox.disputes["dst_ear_tips"]["updated_at"]  # and its copy comes without updated_at
    story.poll(MONDAY)
    record = story["ear_tips"]
    assert len(record.rounds) == 2 and phase(record) is Phase.DECIDE  # only after a slip is a copy with no date an old one


@pytest.mark.parametrize("lags", [False, True], ids=["the same copy", "beside a payment GET that lags"])
def test_a_date_slip_on_a_closed_case_still_goes_to_a_person_when_a_replica_serves_the_slipped_copy_a_day_later(lags):
    story = being_sent("ear_tips")  # code's own $12 accept
    story.sandbox.post(story["ear_tips"])  # it lands, and the case closes
    story.sandbox.disputes["dst_ear_tips"]["updated_at"] = (MONDAY + timedelta(days=3)).isoformat()  # a slip
    slipped = story.sandbox.reads()
    story.poll(MONDAY + 2 * MINUTE)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert person_reason(story["ear_tips"]) == future
    story.sandbox.escalate("dst_ear_tips", now=MONDAY + timedelta(hours=1))  # the bank opens a chargeback: it reads as older
    if lags:
        slipped[0][1]["latest_payment_attempt"]["refunded_amount"] = 0  # the payment's GET lags: the refund isn't there yet
    later = MONDAY + timedelta(days=1, minutes=5)  # past the slip, less two days
    story.records, _ = poll(story.records, slipped, later, store=FERNHILL)  # a replica serves the slipped copy again
    assert person_reason(story["ear_tips"]) == future
    story.poll(later + 5 * MINUTE)  # Airwallex's chargeback, five minutes after the replica's copy
    assert person_reason(story["ear_tips"]) == future and cost(story["ear_tips"]) == Decimal("12.30")


def test_a_clock_that_goes_back_goes_to_a_person():
    story = one_case()
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PURCHASE_HISTORY")
    story.poll(MONDAY - timedelta(hours=1))  # this app's clock went back an hour, while Airwallex's dates moved on
    assert story["headphones"].blocked == ("This app's clock went back, but Airwallex's dates say this reading is newer. "
                                           "A person checks it.")
    record = keep(one_case("ear_tips")["ear_tips"], None, MONDAY, round_number=1)
    assert blocker(record, MONDAY - MINUTE) == "Read the dispute again first. This app's clock is behind the last reading."


@pytest.mark.parametrize("closed", [False, True], ids=["open case", "closed case"])
def test_a_clock_that_goes_back_behind_a_date_slip_still_says_so(closed):
    case_id = "ear_tips" if closed else "headphones"
    if closed:
        story = being_sent("ear_tips")  # code's own $12 accept
        story.sandbox.post(story["ear_tips"])  # it lands, and the case closes
        story.poll(MONDAY + 2 * MINUTE)
        assert phase(story["ear_tips"]) is Phase.CLOSED
    else:
        story = one_case()
    dispute = story.sandbox.disputes[f"dst_{case_id}"]
    story.sandbox.change(f"dst_{case_id}")
    dispute["updated_at"] = (MONDAY + timedelta(days=3)).isoformat()  # Airwallex's dates run three days ahead
    story.poll(MONDAY + 3 * MINUTE)
    future = "Airwallex dated this dispute after this app read it, so newer copies would look older. A person checks it."
    assert person_reason(story[case_id]) == future
    story.sandbox.escalate(f"dst_{case_id}", now=MONDAY)  # the bank opens a chargeback
    dispute["updated_at"] = (MONDAY + timedelta(days=3, minutes=4)).isoformat()  # a newer copy, by Airwallex's dates
    back = MONDAY + 4 * MINUTE - timedelta(hours=1)
    story.poll(back)  # this app's clock went back an hour
    clock_back = "This app's clock went back, but Airwallex's dates say this reading is newer. A person checks it."
    assert story[case_id].blocked == clock_back
    assert person_reason(story[case_id]) == clock_back == blocker(story[case_id], back)  # the slip didn't cause this one
    story.poll(MONDAY + 4 * MINUTE)  # the clock caught up, so the chargeback's copy counts
    record = story[case_id]
    if closed:
        assert person_reason(record) == ("Airwallex changed a closed case from RFI/ACCEPTED to CHARGEBACK/"
                                         "REQUIRES_RESPONSE. A person checks it. The ledger stays as it was.")
    else:
        assert len(record.rounds) == 2 and person_reason(record) == future  # the chargeback's copy is slipped too


class FallBack(tzinfo):
    """San Francisco on Nov 1, 2026: clocks go back at 2:00 PDT, so 1:00 to 2:00 happens twice. fold=1 is the second."""

    def utcoffset(self, dt):
        naive = dt.replace(tzinfo=None)
        second = datetime(2026, 11, 1, 1) <= naive < datetime(2026, 11, 1, 2) and dt.fold
        return timedelta(hours=-8 if naive >= datetime(2026, 11, 1, 2) or second else -7)

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


def test_times_compare_by_the_instant_whatever_the_zone():
    store_clock = FallBack()
    first = datetime(2026, 11, 1, 1, 50, tzinfo=store_clock)  # PDT
    later = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=store_clock)  # PST: 40 minutes later, though it reads 1:30
    assert later < first  # Python compares two times in one zone by the wall clock
    story = one_case("ear_tips", now=first)
    story.records["dst_ear_tips"] = keep(story["ear_tips"], None, first, round_number=1)  # code's own $12 accept
    story.sandbox.reply("dst_ear_tips", Action.ACCEPT, SOMEONE, "LOW_VALUE_TRANSACTION")
    story.sandbox.change("dst_ear_tips", status="ACCEPTED")  # Priya accepted in the web app meanwhile
    story.poll(later)
    record = story["ear_tips"]
    assert record.last.at == later.astimezone(timezone.utc)  # a time in the fold never equals one in another zone
    assert record.rounds[0].plan is None and record.closed is Status.ACCEPTED


def test_the_same_reading_twice_changes_only_the_read_time():
    story = play()
    before = story.records
    story.poll(THREE_WEEKS_LATER + timedelta(days=3))
    for dispute_id, record in story.records.items():
        assert record.last.at == THREE_WEEKS_LATER + timedelta(days=3)
        assert canonical(replace(record, last=before[dispute_id].last)) == canonical(before[dispute_id])


def test_reading_without_a_seed_date_uses_created_at_then_the_read_time():
    dispute, payment = dispute_with(lambda d, p: (d, p))
    assert read(dispute, payment, MONDAY).opened_at == RUN_DATE == read(dispute, payment, MONDAY).created_at
    del dispute["created_at"]
    assert read(dispute, payment, MONDAY).opened_at == MONDAY
    with pytest.raises(ValueError):
        read(dispute, payment, datetime(2026, 11, 2, 9, 0))
    with pytest.raises(ValueError):
        read(dispute, payment, MONDAY, opened_at=datetime(2026, 10, 28, 8, 0))  # a seed date with no time zone


def test_the_customer_comes_from_the_stores_order():
    dispute, payment = dispute_with(lambda d, p: (d, p))
    assert read(dispute, payment, MONDAY, store=FERNHILL).customer_id == "customer-alex"
    assert read(dispute, payment, MONDAY).customer_id is None
    payment["metadata"]["order_no"] = "9002"
    assert read(dispute, payment, MONDAY, store=FERNHILL).customer_id is None


# ----------------------------------------------------------------------------- closing
def closed_as(case_id, status, stage=None, replies=()):
    story = one_case(case_id)
    for action, by, reason in replies:
        story.sandbox.reply(f"dst_{case_id}", action, by, reason)
    story.sandbox.change(f"dst_{case_id}", status=status.value, **({"stage": stage.value} if stage else {}))
    story.poll(MONDAY + MINUTE)
    return story[case_id]


@pytest.mark.parametrize("case_id, status, stage, replies, booked", [
    ("earbuds", Status.WON, None, (), ["chargeback_opened"]),
    ("earbuds", Status.REVERSED, None, (), ["chargeback_opened"]),
    ("earbuds", Status.EXPIRED, None, (), ["chargeback_opened", "accepted_at_chargeback"]),
    ("earbuds", Status.LOST, None, (), ["chargeback_opened", "accepted_at_chargeback"]),
    ("earbuds", Status.ACCEPTED, None, ((Action.ACCEPT, SOMEONE, "VALID_CUSTOMER_DISPUTE"),),
     ["chargeback_opened", "accepted_at_chargeback"]),
    ("earbuds", Status.LOST, Stage.PRE_ARBITRATION, ((Action.CHALLENGE, SOMEONE, "OTHER_REASONS"),),
     ["chargeback_opened", "lost_after_fighting"]),
    ("headphones", Status.LOST, Stage.CHARGEBACK, (), ["chargeback_opened", "accepted_at_chargeback"]),
])
def test_closing_costs(case_id, status, stage, replies, booked):
    record = closed_as(case_id, status, stage, replies)
    amount = FERNHILL.order(FERNHILL.case(case_id).order_number).amount
    expected = Ledger()
    for step in booked:
        getattr(expected, step)(*([] if step == "chargeback_opened" else [amount]))
    assert record.closed is status and phase(record) is Phase.CLOSED
    assert entries(record) == tuple(expected.entries)


@pytest.mark.parametrize("status, replies", [
    (Status.LOST, ()),
    (Status.ACCEPTED, ((Action.ACCEPT, SOMEONE, "LOW_VALUE_TRANSACTION"),)),
])
def test_an_rfi_close_code_cant_price_goes_to_a_person_who_books_it(status, replies):
    record = closed_as("ear_tips", status, replies=replies)
    assert record.closed is status and phase(record) is Phase.PERSON and record.bookings == ()
    assert person_reason(record).endswith("Code can't tell what money moved, so a person books it.")
    for by in ("", ACTOR):
        with pytest.raises(Refused):
            book(record, "Refund", money(12), by, MONDAY)
    for typed in ("NaN", "Infinity"):  # a typed "nan" or "inf": the saved record would no longer load
        with pytest.raises(Refused):
            book(record, "Refund", Decimal(typed), "Priya", MONDAY)
    booked = book(record, "Refund", money(12), "Priya", MONDAY)
    assert phase(booked) is Phase.CLOSED and cost(booked) == money(12)
    assert entries(booked)[-1].label == "Refund (booked by Priya)" and keys(booked) == ["person:1"]
    touched = observe(booked, replace(booked.last, at=MONDAY + 2 * MINUTE, updated_at=booked.last.updated_at + MINUTE))
    assert phase(touched) is Phase.CLOSED  # the booking closed it for good: a touch from Airwallex asks nothing again


@pytest.mark.parametrize("refused, ending, persons", [
    (True, Status.ACCEPTED, True),
    (True, Status.EXPIRED, True),
    (True, Status.LOST, True),
    (False, Status.ACCEPTED, True),
    (False, Status.EXPIRED, False),
], ids=["refused, then accepted in the web app", "refused, then expired", "refused, then lost",
        "no answer, then accepted in the web app", "no answer, then expired"])
def test_a_challenge_that_never_reached_the_bank_isnt_booked_as_a_fight(refused, ending, persons):
    story = round_two_being_sent()
    if refused:
        record = story["headphones"]
        story.records["dst_headphones"] = send_failed(record, record.rounds[-1].plan.request_id,
                                                      "validation_error: file too large.", THREE_WEEKS_LATER + MINUTE)
    if ending is Status.ACCEPTED:
        story.sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "VALID_CUSTOMER_DISPUTE")
    story.sandbox.resolve("dst_headphones", ending)
    story.poll(THREE_WEEKS_LATER + timedelta(days=12))
    record = story["headphones"]
    assert entries(record) == ledger_of(("reviewed",), ("chargeback_opened",), ("reviewed",),
                                        ("accepted_at_chargeback", 1249))
    assert record.closed is ending and (phase(record) is Phase.PERSON) is persons


@pytest.mark.parametrize("steps", [
    [{"status": "LOST"}],
    [{"stage": "PRE_ARBITRATION", "status": "LOST"}],
    [{"stage": "PRE_ARBITRATION", "status": "REQUIRES_RESPONSE", "due_at": "2026-12-20T00:00:00+00:00"},
     {"status": "EXPIRED"}],
], ids=["chargeback lost", "pre-arbitration lost", "pre-arbitration expired"])
def test_a_challenge_airwallex_took_that_never_shows_is_booked_as_a_fight(steps):
    story = round_two_being_sent()
    record = story["headphones"]
    story.records["dst_headphones"] = taken(record, record.rounds[-1].plan.request_id, THREE_WEEKS_LATER + MINUTE)
    for number, change in enumerate(steps):
        story.sandbox.change("dst_headphones", **change)
        story.poll(THREE_WEEKS_LATER + timedelta(days=2 + number))
    record = story["headphones"]
    assert entries(record) == ledger_of(("reviewed",), ("chargeback_opened",), ("reviewed",),
                                        ("lost_after_fighting", 1249))
    assert phase(record) is Phase.CLOSED


def test_a_loss_after_a_challenge_nobody_confirmed_goes_to_a_person():
    story = round_two_being_sent()  # no answer came back, and the challenge never shows
    story.sandbox.resolve("dst_headphones", Status.LOST)
    story.poll(THREE_WEEKS_LATER + timedelta(days=2))
    record = story["headphones"]
    assert entries(record) == ledger_of(("reviewed",), ("chargeback_opened",), ("reviewed",),
                                        ("lost_after_fighting", 1249))
    assert person_reason(record) == ("This app's challenge never showed on the dispute, and Airwallex never answered it. "
                                     "The later-stage fees are booked as if it reached the bank. A person checks it.")


def test_a_challenge_entry_at_a_stage_this_app_never_saw_counts_as_a_fight():
    story = one_case()
    story.sandbox.change("dst_headphones", stage="CHARGEBACK")
    story.sandbox.reply("dst_headphones", Action.CHALLENGE, SOMEONE, "PRODUCT_RECEIVED")
    story.sandbox.change("dst_headphones", stage="PRE_ARBITRATION", status="LOST")
    story.poll(MONDAY + MINUTE)
    assert entries(story["headphones"]) == ledger_of(("chargeback_opened",), ("lost_after_fighting", 1249))


@pytest.mark.parametrize("case_id, status", [("headphones", "ACCEPTED"), ("ear_tips", "LOST")],
                         ids=["our challenge, then accepted in the web app", "our accept, then lost"])
def test_an_rfi_close_after_a_response_that_doesnt_show_goes_to_a_person(case_id, status):
    story = being_sent(case_id)
    story.sandbox.change(f"dst_{case_id}", status=status)  # no entry shows
    story.poll(MONDAY + 2 * MINUTE)
    assert person_reason(story[case_id]) == (f"The case closed as {status} at the RFI stage. Code can't tell what money "
                                             "moved, so a person books it.")


def test_closing_keeps_a_round_a_person_handles():
    story = being_sent("earbuds")
    story.sandbox.pay("dst_earbuds", refunded=189)  # the store refunded Robin while Priya's accept was being sent
    story.poll(MONDAY + 2 * MINUTE)
    why = person_reason(story["earbuds"])
    assert why.startswith("The refunds so far changed while a response was being sent.")
    story.sandbox.post(story["earbuds"])  # the accept had landed
    story.poll(MONDAY + 3 * MINUTE)
    record = story["earbuds"]
    assert record.closed is Status.ACCEPTED and person_reason(record) == why and cost(record) == money(220)
    booked = book(record, "Refund to Robin", money(189), "Priya", MONDAY + timedelta(hours=1))
    assert phase(booked) is Phase.CLOSED and cost(booked) == money(409)
    touched = observe(booked, replace(booked.last, at=MONDAY + timedelta(hours=2),
                                      updated_at=booked.last.updated_at + MINUTE))  # only updated_at moves
    assert phase(touched) is Phase.CLOSED


def test_a_booking_on_an_open_case_leaves_the_round_with_the_person():
    story = being_sent()
    story.sandbox.change("dst_headphones", reason={"original_code": "13.1"})
    story.poll(MONDAY + 2 * MINUTE)
    why = person_reason(story["headphones"])
    booked = book(story["headphones"], "Called the bank", ZERO, "Priya", MONDAY + 3 * MINUTE)
    assert phase(booked) is Phase.PERSON and person_reason(booked) == why == blocker(booked, MONDAY + 3 * MINUTE)


def test_a_correction_gets_its_own_booking_and_the_record_still_loads():
    record = closed_as("ear_tips", Status.LOST)
    record = book(record, "Refund", money(12), "Priya", MONDAY + 2 * MINUTE)
    record = book(record, "Correction", money(-2), "Priya", MONDAY + 3 * MINUTE)
    assert keys(record) == ["person:1", "person:2"] and cost(record) == money(10)
    assert from_json(to_json(record)) == record


def test_a_refund_nobody_booked_goes_to_a_person_at_the_close():
    story = Story()
    story.run(MONDAY)
    story.sandbox.pay("dst_headphones", refunded=1249)  # the store refunded Alex while the challenge was pending
    story.sandbox.resolve("dst_headphones", Status.WON)
    story.poll(MONDAY + timedelta(days=3))
    record = story["headphones"]
    assert record.closed is Status.WON
    assert person_reason(record) == "The payment shows refunds this app didn't make. A person books them."
    assert phase(story["ear_tips"]) is Phase.CLOSED  # that refund is this app's own


@pytest.mark.parametrize("ending, later", [(Status.WON, False), (Status.LOST, True)],
                         ids=["RFI won", "pre-arbitration lost"])
def test_a_refund_airwallex_lists_but_the_payment_doesnt_show_goes_to_a_person_at_the_close(ending, later):
    story = Story(cases=("headphones",))
    story.run(MONDAY)  # the challenge shows: RFI/CHALLENGED
    now = MONDAY
    if later:
        story.sandbox.escalate("dst_headphones")
        story.run(THREE_WEEKS_LATER)  # the warranty challenge shows: PRE_ARBITRATION/PENDING_DECISION
        now = THREE_WEEKS_LATER
    story.sandbox.change("dst_headphones", refunds=[{"id": "rfd_web_app"}])  # the payment still says 0
    story.poll(now + timedelta(hours=1))
    story.sandbox.resolve("dst_headphones", ending)
    story.poll(now + timedelta(days=3))
    record = story["headphones"]
    assert record.last.dispute.refunded_amount.is_nan() and record.closed is ending
    assert person_reason(record) == "The payment shows refunds this app didn't make. A person books them."


def test_our_own_refund_the_payment_doesnt_show_yet_closes_quietly():
    story = being_sent("ear_tips")
    story.sandbox.post(story["ear_tips"])
    story.sandbox.pay("dst_ear_tips", refunded=0)  # the refund is listed, but the payment hasn't caught up
    story.poll(MONDAY + 2 * MINUTE)
    record = story["ear_tips"]
    assert record.last.dispute.refunded_amount.is_nan()
    assert phase(record) is Phase.CLOSED and cost(record) == Decimal("12.30")


@pytest.mark.parametrize("refunded, why", [
    (0, None),
    (1249, "The payment shows refunds this app didn't make. A person books them."),
], ids=["nothing refunded", "the store refunded Alex"])
def test_an_unclear_refund_on_the_first_reading_flags_only_a_refund_made_since(refunded, why):
    story = Story(cases=("headphones",))
    attempt = story.sandbox.payments["int_headphones"].pop("latest_payment_attempt")
    story.poll(MONDAY - MINUTE)  # the payment doesn't say what was refunded yet
    assert story["headphones"].refunded_at_start.is_nan()
    story.sandbox.payments["int_headphones"]["latest_payment_attempt"] = attempt
    story.run(MONDAY)
    story.sandbox.pay("dst_headphones", refunded=refunded)  # the store may refund Alex in its back office
    story.sandbox.resolve("dst_headphones", Status.WON)
    story.poll(MONDAY + timedelta(days=3))
    assert story["headphones"].closed is Status.WON and person_reason(story["headphones"]) == why


def test_an_expired_rfi_isnt_over():
    story = one_case()
    story.sandbox.change("dst_headphones", status="EXPIRED")
    story.poll(MONDAY + timedelta(days=7))
    record = story["headphones"]
    assert record.closed is None and phase(record) is Phase.WAIT and record.bookings == ()
    story.sandbox.escalate("dst_headphones")
    story.poll(THREE_WEEKS_LATER)
    record = story["headphones"]
    assert [r.stage for r in record.rounds] == [Stage.RFI, Stage.CHARGEBACK] and keys(record) == ["chargeback_fee"]
    assert rejected(record) == NOTHING and phase(record) is Phase.DECIDE
    assert decide(as_dispute(record), ANSWERS["headphones_round_2"].evidence(), THREE_WEEKS_LATER).action is Action.CHALLENGE


def test_a_dispute_closed_before_the_app_saw_it_is_history_only():
    sandbox = Sandbox()
    sandbox.open("earbuds")
    sandbox.change("dst_earbuds", status="WON")
    records, problems = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    record = records["dst_earbuds"]
    assert record.history_only and problems == ()
    assert phase(record) is Phase.CLOSED and person_reason(record) is None
    assert record.bookings == () and at_risk(record) == ZERO
    sandbox.change("dst_earbuds", status="LOST")
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    assert records["dst_earbuds"].bookings == () and records["dst_earbuds"].blocked is None


def test_a_closed_dispute_that_reopens_behind_a_date_slip_still_reaches_a_person():
    sandbox = Sandbox()
    sandbox.open("headphones")
    sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "OTHERS")
    sandbox.change("dst_headphones", status="ACCEPTED")  # accepted in the web app before this app looked
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    sandbox.change("dst_headphones")
    sandbox.disputes["dst_headphones"]["updated_at"] = "2062-11-02T17:00:30+00:00"  # a slip in Airwallex's date
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    sandbox.escalate("dst_headphones", now=MONDAY + timedelta(days=1))  # the bank opens a chargeback
    records, _ = poll(records, sandbox.reads(), MONDAY + timedelta(days=1), store=FERNHILL)
    record = records["dst_headphones"]
    assert record.history_only and phase(record) is Phase.PERSON
    assert person_reason(record).startswith("Airwallex returned an older copy of this dispute")


def test_a_closed_dispute_that_reopens_behind_a_date_slip_stays_with_a_person_when_a_replica_serves_the_slipped_copy():
    sandbox = Sandbox()
    sandbox.open("headphones")
    sandbox.reply("dst_headphones", Action.ACCEPT, SOMEONE, "OTHERS")
    sandbox.change("dst_headphones", status="ACCEPTED")  # accepted in the web app before this app looked
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    sandbox.change("dst_headphones")
    sandbox.disputes["dst_headphones"]["updated_at"] = (MONDAY + timedelta(days=3)).isoformat()  # a slip
    slipped = sandbox.reads()
    for minutes in (1, 56):  # read at once, and again before the bank acts: until then it's Airwallex's current copy
        records, _ = poll(records, slipped, MONDAY + minutes * MINUTE, store=FERNHILL)
    sandbox.escalate("dst_headphones", now=MONDAY + timedelta(hours=1))  # a chargeback, dated right: it reads as older
    for step in range(6):  # a GET every 5 minutes, and a replica answers every other one with the slipped copy
        reads = slipped if step % 2 else sandbox.reads()
        records, _ = poll(records, reads, MONDAY + timedelta(hours=1, minutes=1 + 5 * step), store=FERNHILL)
        record = records["dst_headphones"]
        assert record.history_only and phase(record) is Phase.PERSON  # the slipped copy read again hides nothing
        assert person_reason(record).startswith("Airwallex returned an older copy of this dispute")
    after = MONDAY + timedelta(days=3, minutes=1)  # Airwallex's clock passes the slipped date
    sandbox.change("dst_headphones")
    sandbox.disputes["dst_headphones"]["updated_at"] = after.isoformat()  # so its chargeback reads as newer at last
    records, _ = poll(records, sandbox.reads(), after, store=FERNHILL)
    record = records["dst_headphones"]
    assert not record.history_only and phase(record) is Phase.PERSON
    assert person_reason(record).startswith("Airwallex reopened a dispute that was closed when this app first saw it.")


def test_a_lagging_copy_of_an_old_closed_dispute_blocks_only_until_a_good_reading():
    sandbox = Sandbox()
    sandbox.open("earbuds")
    sandbox.change("dst_earbuds", status="WON")
    stale = sandbox.reads()
    records, _ = poll({}, stale, MONDAY, store=FERNHILL)
    sandbox.change("dst_earbuds")  # only updated_at moves
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    records, _ = poll(records, stale, MONDAY + timedelta(hours=1), store=FERNHILL)
    assert phase(records["dst_earbuds"]) is Phase.PERSON
    records, _ = poll(records, sandbox.reads(), MONDAY + timedelta(hours=1, minutes=1), store=FERNHILL)
    assert phase(records["dst_earbuds"]) is Phase.CLOSED and records["dst_earbuds"].blocked is None


def test_a_lagging_copy_of_an_old_closed_dispute_behind_a_date_slip_blocks_until_a_copy_dated_after_it():
    sandbox = Sandbox()
    sandbox.open("earbuds")
    sandbox.change("dst_earbuds", status="WON")
    stale = sandbox.reads()
    records, _ = poll({}, stale, MONDAY, store=FERNHILL)
    sandbox.change("dst_earbuds")
    sandbox.disputes["dst_earbuds"]["updated_at"] = (MONDAY + timedelta(days=3)).isoformat()  # a slip
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    records, _ = poll(records, stale, MONDAY + timedelta(hours=1), store=FERNHILL)
    assert phase(records["dst_earbuds"]) is Phase.PERSON
    records, _ = poll(records, sandbox.reads(), MONDAY + timedelta(hours=1, minutes=1), store=FERNHILL)
    assert phase(records["dst_earbuds"]) is Phase.PERSON  # after a slip, a copy that reads as older may be the current one
    after = MONDAY + timedelta(days=3, minutes=1)
    sandbox.change("dst_earbuds")
    sandbox.disputes["dst_earbuds"]["updated_at"] = after.isoformat()  # a copy dated after the slipped one
    records, _ = poll(records, sandbox.reads(), after, store=FERNHILL)
    assert phase(records["dst_earbuds"]) is Phase.CLOSED and records["dst_earbuds"].blocked is None


def test_an_old_closed_dispute_that_reads_an_earlier_stage_keeps_its_last_reading():
    sandbox = Sandbox()
    sandbox.open("earbuds")
    sandbox.change("dst_earbuds", status="WON")
    records, _ = poll({}, sandbox.reads(), MONDAY, store=FERNHILL)
    before = records["dst_earbuds"]
    sandbox.change("dst_earbuds", stage="RFI")  # a closed dispute doesn't go back a stage
    records, _ = poll(records, sandbox.reads(), MONDAY + MINUTE, store=FERNHILL)
    assert records["dst_earbuds"] == before and from_json(to_json(before)) == before


# ----------------------------------------------------------------------------- saved state
def test_a_saved_record_loads_back_the_same():
    for record in play(outcome=Status.LOST).records.values():
        assert from_json(to_json(record)) == record


@pytest.mark.parametrize("change, message", [
    (lambda data: data.update(format=2), "isn't a format 1 case record"),
    (lambda data: data["record"]["rounds"].reverse(), "one per stage, in order"),
    (lambda data: data["record"]["rounds"][0]["plan"].update(request_id="another-id"), "doesn't come from its terms"),
    (lambda data: data["record"]["rounds"][0].update(stage="SOMETHING_NEW"), "isn't a Stage"),
    (lambda data: data["record"]["rounds"][0]["plan"].update(sending_at=None), "never sent"),
    (lambda data: data["record"]["bookings"].append(data["record"]["bookings"][0]), "booked twice"),
    (lambda data: data["record"].update(closed="CHALLENGED"), "doesn't fit its last reading"),
    (lambda data: data["record"].update(opened_at="2026-10-28T08:00:00"), "has no time zone"),
    (lambda data: data["record"].update(extra=1), "not expected"),
    (lambda data: data["record"]["last"]["dispute"].update(stage="UNKNOWN"), "doesn't know"),
    (lambda data: data["record"]["rounds"][0]["reply"].update(action="HAND_OFF"), "doesn't know"),
    (lambda data: data["record"]["last"]["dispute"].update(stage="RFI"), "doesn't fit the rounds"),
    (lambda data: data["record"]["rounds"][0]["plan"]["terms"].update(action="NO_ACTION"), "terms aren't usable"),
    (lambda data: data["record"].update(others=-3), "isn't usable"),
    (lambda data: data["record"]["bookings"][0]["entries"][0].update(amount="NaN"), "isn't usable"),
    (lambda data: data["record"]["bookings"][0]["entries"][0].update(amount="sNaN"), "isn't a number"),
    (lambda data: data["record"]["rounds"][0]["plan"]["terms"].update(customer_id="customer-\ud83d"),
     "text that isn't valid"),  # a row edited by hand: no request id can come from these terms
])
def test_a_broken_saved_record_is_refused(change, message):
    data = json.loads(to_json(play()["headphones"]))
    change(data)
    with pytest.raises(Unloadable, match=message):
        from_json(json.dumps(data))


def test_a_cut_or_mislabelled_save_is_refused():
    text = to_json(play()["headphones"])
    with pytest.raises(Unloadable, match="isn't valid JSON"):
        from_json(text[:-5])
    with pytest.raises(Unloadable, match="isn't a format 1 case record"):
        from_json(text.replace('"format": 1', '"format": true'))


def test_the_module_never_names_a_simulation_call():
    assert "simulation" not in Path(lifecycle.__file__).read_text(encoding="utf-8").lower()


# ----------------------------------------------------------------------------- every short run of readings
def wire(stage, status, *replies, due_in_days=6):
    sandbox = Sandbox()
    sandbox.open("headphones")
    dispute, payment = sandbox.reads()[0]
    dispute.update(stage=stage.value, status=status.value, due_at=(MONDAY + timedelta(days=due_in_days)).isoformat())
    for action, reply_stage, by in replies:
        key, who = ("accept_details", "accepted_by") if action is Action.ACCEPT else ("challenge_details", "challenged_by")
        dispute[key].append({"stage": reply_stage.value, "reason": "PURCHASE_HISTORY", who: by})
    return dispute, payment


OURS_AT_RFI = (Action.CHALLENGE, Stage.RFI, ACTOR)
READINGS = [
    wire(Stage.RFI, Status.REQUIRES_RESPONSE),
    wire(Stage.RFI, Status.REQUIRES_RESPONSE, due_in_days=9),
    wire(Stage.RFI, Status.REQUIRES_RESPONSE, OURS_AT_RFI),  # the reply shows before the status moves
    wire(Stage.RFI, Status.CHALLENGED, OURS_AT_RFI),
    wire(Stage.RFI, Status.CHALLENGED, (Action.CHALLENGE, Stage.RFI, SOMEONE)),
    wire(Stage.CHARGEBACK, Status.REQUIRES_RESPONSE),  # the bank moved on with nothing on record
    wire(Stage.CHARGEBACK, Status.REQUIRES_RESPONSE, OURS_AT_RFI),
    wire(Stage.CHARGEBACK, Status.REQUIRES_RESPONSE, (Action.CHALLENGE, Stage.RFI, SOMEONE)),
    wire(Stage.PRE_ARBITRATION, Status.PENDING_DECISION, OURS_AT_RFI, (Action.CHALLENGE, Stage.CHARGEBACK, ACTOR)),
    wire(Stage.PRE_ARBITRATION, Status.LOST, OURS_AT_RFI),
    wire(Stage.RFI, Status.EXPIRED),
]


def starting_points():
    yield "nothing yet", None
    record = one_case()["headphones"]
    yield "deciding", record
    planned = keep(record, ANSWERS["headphones"].evidence(), MONDAY, round_number=1)
    approved = approve(planned, planned.rounds[0].plan, "Priya", MONDAY)
    yield "approved", approved
    tried = sending(observe(approved, read(*READINGS[0], MONDAY + MINUTE, store=FERNHILL)), MONDAY + MINUTE)
    yield "sending", tried
    yield "taken", taken(tried, tried.rounds[0].plan.request_id, MONDAY + MINUTE)


@functools.cache
def reading_at(index, step):
    """READINGS[index] as a run's step-th GET: each step reads a minute later, and Airwallex's date moves on too."""
    dispute, payment = copy.deepcopy(READINGS[index])
    dispute["updated_at"] = (RUN_DATE + timedelta(hours=step + 1)).isoformat()
    return read(dispute, payment, MONDAY + (step + 2) * MINUTE, store=FERNHILL,
                opened_at=FERNHILL.case("headphones").opened_at)


@pytest.mark.parametrize("name, start", list(starting_points()), ids=[name for name, _ in starting_points()])
def test_every_short_run_of_readings_keeps_the_rules(name, start):
    def walk(before, run, sent):  # every run of up to three readings, each prefix checked once
        if len(run) == 3:
            return
        for index in range(len(READINGS)):
            path, reading = (*run, index), reading_at(index, len(run))
            record = observe(before, reading)
            labels = keys(record)
            assert len(labels) == len(set(labels)), path
            assert before is None or record.bookings[:len(before.bookings)] == before.bookings, path  # append-only
            if record.blocked and before is not None:
                assert record.rounds == before.rounds and record.bookings == before.bookings, path
            if before is not None and len(record.rounds) == len(before.rounds) and before.rounds[-1].person:
                assert record.rounds[-1].person == before.rounds[-1].person, path  # only a person ends their round
            raised = [number for number, here in enumerate(record.rounds[:-1]) if here.person and (
                before is None or number >= len(before.rounds) or not before.rounds[number].person)]
            assert not raised or person_reason(record), path  # an earlier round's alarm reaches the queue
            again = observe(record, replace(reading, at=reading.at + MINUTE))
            assert dump(replace(again, last=record.last)) == dump(record), path  # a duplicate changes nothing
            facts, files = rejected(record)
            assert bool(facts) == bool(files), path  # both or neither
            tried = sent | {number for number, here in enumerate(record.rounds) if _tried(here)}
            assert all(record.rounds[number].plan is not None for number in tried), path  # what was sent stays on record
            if blocker(record, reading.at) is None:
                plan = record.rounds[-1].plan
                assert plan.terms.stage is record.last.dispute.stage and reading.at < plan.terms.due_at, path
                assert not plan.taken_at and phase(record) in (Phase.SEND, Phase.SENT), path
            assert to_json(from_json(to_json(record))) == to_json(record), path
            walk(record, path, tried)

    walk(start, (), frozenset())


def _tried(here):
    return here.plan is not None and here.plan.sending_at is not None
