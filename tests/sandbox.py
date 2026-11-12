"""The fake Airwallex sandbox and the story's passes, shared by the lifecycle and rounds tests."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from disputable.airwallex import json_number
from disputable.engine import Action, Evidence, decide
from disputable.fees import EvidenceClass, money
from disputable.lifecycle import (ACTOR, Phase, approve, as_dispute, blocker, from_json, keep, phase, poll, sending, taken,
                                  to_json)
from disputable.seed.fernhill import ANSWERS, ENDING_2_ANSWER, FERNHILL, MONDAY, THREE_WEEKS_LATER

RUN_DATE = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)  # the sandbox's created_at: when the run made the dispute
MINUTE = timedelta(minutes=1)


class Sandbox:
    """Answers the way the Airwallex sandbox did on 2026-10-30, as plain dicts, and plays the simulated issuer."""

    def __init__(self, store=FERNHILL):
        self.store, self.disputes, self.payments, self.opened, self.ticks = store, {}, {}, {}, 0

    def open(self, case_id, now=MONDAY, dispute_id=None, payment_id=None):
        """The case's dispute on its order's payment, with exact amounts as JSON numbers: the case's amount, else the
        order's total, so $15.71 stays 15.71 and a $100 part of a $600 order stays $100."""
        case = self.store.case(case_id)
        order = self.store.order(case.order_number)
        dispute_id, payment_id = dispute_id or f"dst_{case_id}", payment_id or f"int_{case_id}"
        total = json_number(order.amount)
        amount = json_number(order.amount if case.amount is None else case.amount)
        self.payments.setdefault(payment_id, {
            "id": payment_id, "amount": total, "currency": "USD", "status": "SUCCEEDED", "captured_amount": total,
            "created_at": RUN_DATE.isoformat(), "metadata": {"order_no": order.number},
            "latest_payment_attempt": {"captured_amount": total, "refunded_amount": 0}})
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
                attempt = self.payments[d["payment_intent_id"]]["latest_payment_attempt"]
                attempt["refunded_amount"] = json_number(money(attempt["refunded_amount"]) + plan.terms.refund_amount)
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
