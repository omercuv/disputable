"""One pass of the agent: read this run's disputes, poll(), save, alert, send what is ready, then decide each round.

A pass reads the clock before any GET, so a reading is never dated after the GET it stands for. It lists the disputes on
each of the run's own payments, then reads each dispute found and each case already saved, and its payment once a pass.
A listed dispute that names another payment isn't read from that list, and a new dispute whose own read names a payment
outside the run's is read no further: the run's scope never rests on Airwallex's filter alone. A pair that answered goes
to lifecycle.poll(), which files it under the payment it names. When a list names its own payment for a dispute, new or
saved, and the dispute's own read names another of the run's payments (or its saved record does, when the read failed),
it counts on both, with a line: both payments' cases go to a person. A new dispute whose read failed still goes to
poll() as one that can't be read, so the one-payment-one-case rule counts it: on the payment its read named, else on the
first whose list showed it, and that payment's other cases can't go out. Nothing is sent on a pass for a case whose
payment's list didn't answer, or whose payment isn't one of the run's, since a second dispute on it could be unseen. A
saved case whose read failed stays as it was, and its reading ages. A case with a saved row that can't be read is left
out of everything, with a line on every pass, as the screens show it, and it counts on each of the run's payments whose
list shows it. Each changed record is saved in its own transaction, and one a screen changed meanwhile is left for the
next pass. The alerts come from the poll result. Sends run before any read, soonest due first, because a Claude read can
take minutes and a plan goes out only on a fresh reading. Then each round at DECIDE, soonest due first, through
rounds.decide_round(). One pass at a time per data folder: the PassLock, in this process and in any other.

The send step checks blocker() and verify() before every try. At SEND it builds the plan's files again with files_for()
before the upload, uploads each file once, and checks the body with body_problem(). At SENT it never builds anything: it
sends the saved bytes again with the same request id, after a fresh read. The body is saved with sending() in one
transaction, at a moment taken right before the POST, then one POST goes out, and its answer is saved as a try before it
is applied: taken(), send_failed(), or nothing until the next read. A try whose answer is on file is applied from there,
and nothing is sent again. A POST is a retry when a try may already be with Airwallex: one is on file, or the case was
saved at SENT and the answer was lost with the process. Every line a pass writes is for a person, in plain words, and
none holds a body or a file id.

The CLI, the story run and the demo use it like this:
    report = run_pass(service)     # the service's database, store, clock, reader, Airwallex client, fees and pass lock
    report.skipped: another pass held the lock, so nothing ran
    a ReplayMismatch always comes out, even from the reader: the demo stops at its beat and says so
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

from disputable import airwallex, approvals, lifecycle, rounds
from disputable.airwallex import Answer, Outcome, PostRequest
from disputable.approvals import Alert
from disputable.clock import ReplayMismatch
from disputable.db import APPROVAL_UNREADABLE, UPLOAD_UNREADABLE, Conflict, Database, Loaded, Try, Upload
from disputable.engine import Action
from disputable.jsonable import Unloadable
from disputable.lifecycle import CaseRecord, Phase, Refused
from disputable.reader import ReaderBusy

if TYPE_CHECKING:
    from disputable.service import Service

UNMATCHED = "no approval on file matched it"  # lifecycle.drop()'s why when verify() refuses at SEND
NOT_REBUILT = "its file couldn't be built again exactly as planned"  # its why when files_for() refuses

NO_PASS_HERE = ("This service has no Airwallex client or no reader, so it runs no pass. The pass and loop commands, the "
                "story run and the demo run passes.")
SKIPPED = "Another pass is running, so this one didn't start."

# The fetch's lines. {why} is airwallex.problem()'s phrase ("no answer in 30 seconds", "HTTP 503", "an answer this app
# can't read", ...), or a reason below for an answer this app won't use.
LIST_FAILED = ("This app couldn't read the list of disputes on order #{order}'s payment ({why}). The disputes this app "
               "knows are still read. Nothing is sent for this payment on this pass.")
BAD_SCOPE = "Order #{order}: the run's saved payment id won't go into a web address, so its disputes aren't listed."
NAMELESS = ("Airwallex listed a dispute on order #{order}'s payment with no id this app can use. It isn't read, but it "
            "counts as a dispute on that payment.")
OTHER_PAYMENTS = ("Airwallex listed disputes of other payments under order #{order}'s payment. This app doesn't read "
                  "them from this list.")
TWO_PAYMENTS = ("Airwallex names two of the run's payments for a dispute listed under order #{order}'s payment. Both "
                "payments' cases go to a person.")
READ_FAILED = "{who}: this app couldn't read the dispute on this pass ({why}). It reads it again on the next pass."
OTHER_DISPUTE = "the answer was for another dispute"
OTHER_PAYMENT = "the dispute names a payment that isn't in the run's list of payments"
BAD_PAYMENT = "the dispute names a payment id this app won't put in a web address"
BAD_ID = "its id won't go into a web address"
BROKEN = ("Dispute {id}: part of what this app saved for this case can't be read. {why} Nothing is sent for it. A person "
          "handles it in Airwallex.")  # {why}: db's sentences, as written
CONFLICT = "{who}: the case changed while this pass ran. It's read again on the next pass."
DECIDE_ERROR = "{who}: code hit an error while it decided this round. A person handles this round."
NO_ID = "A dispute with no id "  # how poll() names a dispute it got with no id: here only ever a stand-in

# The send step's lines.
NOT_LISTED = "{who}: the list of disputes on its payment couldn't be read, so nothing is sent for it on this pass."
NOT_IN_SCOPE = ("{who}: this app can't find its payment in the run's list of payments, so nothing is sent for it. A "
                "person checks it.")
DROPPED = "{who}: {why} The plan was dropped. Code decides the round again, and a person approves the new plan."
NOT_AT_SENT = "{who}: {why} The response may already be with Airwallex, so it isn't sent again. A person checks it."
UPLOAD_FAILED = "{who}: the evidence file may not have uploaded ({why}). It tries again on the next pass."
UPLOAD_LOST = "{who}: " + UPLOAD_UNREADABLE + " Nothing was sent. A person checks it."
CANT_BUILD = "{who}: this app couldn't build the request ({problem}). Nothing was sent. A person checks it."
NOT_SENT = "{who}: the response wasn't sent. {why}"  # {why}: blocker()'s sentence, as sending() refused it
NO_ANSWER = "{who}: Airwallex didn't answer the response ({detail}). The same request goes again after the next read."
WAIT = "{who}: Airwallex asked this app to wait. The same request goes again after {time} UTC."
BODY_MISSING = ("{who}: the saved request for this response is missing, so it isn't sent again. A person checks what "
                "Airwallex has.")
BODY_UNREADABLE = ("{who}: the saved request for this response can't be read, so it isn't sent again. A person checks "
                   "what Airwallex has.")
TRY_UNREADABLE = ("{who}: a saved try of this response can't be read, so it isn't sent again. A person checks what "
                  "Airwallex has.")
APPLIED = "{who}: Airwallex's answer to an earlier try is on file, so it's applied now and nothing is sent again."
MOVED_ON = "{who}: Airwallex answered this app's response, but the saved case had moved on. A person checks it."
SEND_ERROR = "{who}: this app hit an error while sending. Nothing more was sent. It tries again on the next pass."

Pair = tuple[Mapping[str, Any], Mapping[str, Any]]


@dataclass(frozen=True)
class PassReport:
    """What one pass did, for the CLI and the story run. The database keeps its lines and alerts too."""
    pass_id: int | None  # None when the pass didn't start
    started_at: datetime
    finished_at: datetime
    read: tuple[str, ...]  # the disputes whose dispute and payment both answered
    sent: tuple[tuple[str, Outcome], ...]  # each POST try of the pass: dispute id and what its answer meant
    decided: tuple[str, ...]  # the rounds decide_round() changed
    dropped: tuple[str, ...]  # the plans the send step dropped, to be decided and approved again
    problems: tuple[str, ...]
    alerts: tuple[Alert, ...]
    skipped: str = ""  # why the pass didn't start, or ""


@dataclass(frozen=True)
class Fetched:
    """What the GETs of a pass gave poll(), and what the send step needs to know of the lists."""
    reads: tuple[Pair, ...]  # the pairs for poll(), stand-ins for the disputes that couldn't be read among them
    read: tuple[str, ...]  # the disputes whose dispute and payment both answered
    stand_ins: frozenset[str]  # the disputes poll() got a stand-in for: the pass's own line says why, so poll()'s goes
    nameless: bool  # poll() got a stand-in with no id too: NAMELESS's or TWO_PAYMENTS' line says why
    listed: frozenset[str]  # the run's payments whose list answered: only their cases are sent on this pass
    scope: frozenset[str]  # the run's payments, as this pass could read them


def run_pass(service: Service) -> PassReport:
    """One pass, under the pass lock. When another pass holds it, here or in another process, nothing runs and the
    report says so. A ReplayMismatch comes out as it is, and so does anything else that stops the whole pass; the pass's
    row then stays unfinished, and the next pass loads everything again."""
    if service.client is None or service.reader is None:
        raise ValueError(NO_PASS_HERE)
    if not service.lock.acquire():
        at = service.clock.peek()  # never journaled: a pass that didn't start uses up no recorded moment
        return PassReport(None, at, at, (), (), (), (), (), (), SKIPPED)
    try:
        return _run(service)
    finally:
        service.lock.release()


def _run(service: Service) -> PassReport:
    db, clock = service.db, service.clock
    started = clock.now("pass")  # before any GET
    pass_id = db.begin_pass(started)
    loaded = db.load()  # every pass loads everything, so a restart is just the next pass
    problems: list[str] = []
    fetched = fetch(service, loaded, problems)
    records, lines = lifecycle.poll(loaded.records, fetched.reads, started, store=service.store, opened=db.opened(),
                                    fees=service.fees)
    problems += [line for line in lines if not _for_a_stand_in(line, fetched)]
    problems += [BROKEN.format(id=dispute_id, why=_sentence(why)) for dispute_id, why in sorted(loaded.broken.items())]
    problems += db.scope_problems()
    save_changed(db, records, loaded, problems)
    alerts = approvals.alerts(records.values(), started, service.fees)  # from the poll result, before any read
    dropped: list[str] = []
    sent = send_all(service, fetched, problems, dropped)  # before any read: a read can take minutes
    decided = decide_all(service, problems)
    finished = clock.now("pass:end")
    summary = {"read": len(fetched.read), "sent": len(sent), "decided": len(decided), "dropped": len(dropped),
               "problems": len(problems)}
    db.end_pass(pass_id, finished, problems, alerts, summary)
    return PassReport(pass_id, started, finished, fetched.read, tuple(sent), tuple(decided), tuple(dropped),
                      tuple(problems), alerts)


# ----------------------------------------------------------------------------- the GETs
def fetch(service: Service, loaded: Loaded, problems: list[str]) -> Fetched:
    """The pass's GETs: the list of disputes on each of the run's payments, by payment id; then each dispute found and
    each case already saved, sorted, but none with a saved row that can't be read; and each dispute's payment, once a
    pass.

    A listed dispute that names another payment isn't read from that list, and one line says so for the payment. A
    dispute and its payment that both answered with an object go to poll(), which files it under the payment it names,
    so a list that ignores its filter and doesn't say whose disputes it holds can't put one on the wrong payment. A new
    dispute whose dispute or payment read failed, or whose answer names a payment outside the run's, goes as
    ({"id": id, "payment_intent_id": its payment}, {}), so read() raises Unreadable and poll() counts it for the
    one-payment-one-case rule: its payment is the run's payment its own answer named, else the first whose list showed
    it. When a list's item named that list's own payment for a dispute, new or saved, and the dispute counts on another
    of the run's payments (the one its answer names, or its saved record's when a saved case's read failed), it counts
    on both, with the TWO_PAYMENTS line: as ({"payment_intent_id": the list's payment}, {}), and once more on its own
    payment, which may have no other case yet. These stand-ins have no id, so poll() marks no case as one it can't read,
    and every open case on either payment goes to a person. A dispute listed with no id this app can use goes as such a
    stand-in too, on the payment that lists it. A case with a saved row that can't be read goes as one pair with its id
    on each of the run's payments whose list shows it: this app can't read which one is its own. A saved case whose read
    failed stays as it was: its record already counts. Every failed read gets the pass's own line, and poll()'s line for
    a stand-in is left out, since its words would be false."""
    client = service.client
    found: dict[str, list[str]] = {}  # dispute id -> the run's payments whose list shows it, in the lists' order
    labelled: dict[str, set[str]] = {}  # dispute id -> the payments whose list's item names that same payment
    orders: dict[str, str] = {}  # the run's payments: payment id -> order number
    reads: list[Pair] = []
    listed: set[str] = set()
    nameless = False
    for payment in service.db.store_payments():  # by payment id
        orders[payment.payment_id] = payment.order_number
        if not airwallex.safe_id(payment.payment_id):
            problems.append(BAD_SCOPE.format(order=payment.order_number))
            continue
        answer = client.disputes_on(payment.payment_id)
        items = answer.data.get("items") if answer.ok and isinstance(answer.data, dict) else None
        if not isinstance(items, list):
            problems.append(LIST_FAILED.format(order=payment.order_number, why=airwallex.problem(answer)))
            continue
        listed.add(payment.payment_id)
        others = False
        for item in items:
            named = item.get("payment_intent_id") if isinstance(item, Mapping) else None
            if isinstance(named, str) and named and named != payment.payment_id:  # never read on Airwallex's filter alone
                others = True
                continue
            dispute_id = item.get("id") if isinstance(item, Mapping) else None
            if airwallex.safe_id(dispute_id):
                shown = found.setdefault(dispute_id, [])
                if payment.payment_id not in shown:  # once, if a list shows a dispute twice
                    shown.append(payment.payment_id)
                if named == payment.payment_id:
                    labelled.setdefault(dispute_id, set()).add(payment.payment_id)
            else:  # it can't be read, but it is a dispute on this payment: poll() counts it with no id
                problems.append(NAMELESS.format(order=payment.order_number))
                reads.append(({"payment_intent_id": payment.payment_id}, {}))
                nameless = True
        if others:
            problems.append(OTHER_PAYMENTS.format(order=payment.order_number))
    stand_ins = {dispute_id for dispute_id in loaded.broken if dispute_id in found}
    reads += [({"id": dispute_id, "payment_intent_id": payment_id}, {})  # never read: it counts on each list it's on
              for dispute_id in sorted(stand_ins) for payment_id in found[dispute_id]]
    run_payments = frozenset(orders)
    payments: dict[str, Answer] = {}
    read: list[str] = []
    for dispute_id in sorted((found.keys() | loaded.records.keys()) - loaded.broken.keys()):
        record = loaded.records.get(dispute_id)
        pair, why, its_payment = _read(client, dispute_id, payments, scope=None if record is not None else run_payments)
        if pair is not None:
            reads.append(pair)
            read.append(dispute_id)
        else:
            problems.append(READ_FAILED.format(who=who(record) if record is not None else f"Dispute {dispute_id}",
                                               why=why))
            if record is None:  # a new dispute: it still counts on its payment, so the payment's other case waits
                reads.append(({"id": dispute_id, "payment_intent_id": its_payment or found[dispute_id][0]}, {}))
                stand_ins.add(dispute_id)
        home = its_payment if record is None or pair is not None else record.last.payment_id  # where poll() counts it
        both = sorted(labelled.get(dispute_id, set()) - {home}) if home in run_payments else []
        for listed_on in both:  # a list named another of the run's payments for it: it counts there too
            problems.append(TWO_PAYMENTS.format(order=orders[listed_on]))
            reads.append(({"payment_intent_id": listed_on}, {}))
        if both:  # and once more on its own, which may have no other case yet: both payments' cases go to a person
            reads.append(({"payment_intent_id": home}, {}))
            nameless = True
    return Fetched(tuple(reads), tuple(read), frozenset(stand_ins), nameless, frozenset(listed), run_payments)


def _read(client: airwallex.AgentClient, dispute_id: str, payments: dict[str, Answer], *,
          scope: frozenset[str] | None = None) -> tuple[Pair | None, str, str | None]:
    """The dispute and its payment, or None and why not, as a phrase; and the payment the dispute's answer names, or
    None when it names none this app reads. scope: the run's payments, for a dispute this database has no record of.
    An answer that names a payment outside them is refused before that payment is read, so nothing outside the run's
    payments becomes a case of the run. One that names another of them than the list it was on is read: a list that
    ignores its filter can show a dispute under any of the run's payments. When the list's item named the list's own
    payment, fetch() counts the dispute there too."""
    if not airwallex.safe_id(dispute_id):
        return None, BAD_ID, None
    answer = client.dispute(dispute_id)
    dispute = answer.data if answer.ok and isinstance(answer.data, dict) else None
    if dispute is None:
        return None, airwallex.problem(answer), None
    if dispute.get("id") != dispute_id:  # poll() would file it under another case
        return None, OTHER_DISPUTE, None
    payment_id = dispute.get("payment_intent_id")
    if not airwallex.safe_id(payment_id):
        return None, BAD_PAYMENT, None
    if scope is not None and payment_id not in scope:
        return None, OTHER_PAYMENT, None
    if payment_id not in payments:  # once a pass: two disputes on one payment read it once
        payments[payment_id] = client.payment(payment_id)
    paid = payments[payment_id]
    if not (paid.ok and isinstance(paid.data, dict)):
        return None, airwallex.problem(paid), payment_id
    return (dispute, paid.data), "", payment_id


def _for_a_stand_in(line: str, fetched: Fetched) -> bool:
    """Whether poll() wrote the line for a stand-in this pass gave it, as in "The amount field is missing": the pass's
    own line says what happened."""
    if fetched.nameless and line.startswith(NO_ID):
        return True
    return any(line.startswith(f"Dispute {dispute_id} ") for dispute_id in fetched.stand_ins)


def save_changed(db: Database, records: Mapping[str, CaseRecord], loaded: Loaded, problems: list[str]) -> None:
    """Each record poll() changed, in its own transaction. One that a screen saved meanwhile is left as the screen saved
    it: the next pass reads it again. Nothing is ever overwritten."""
    for dispute_id in sorted(records):
        record, before = records[dispute_id], loaded.records.get(dispute_id)
        if before is not None and lifecycle.to_json(record) == lifecycle.to_json(before):
            continue
        try:
            db.save(record, loaded.versions.get(dispute_id), "poll")
        except Conflict:
            problems.append(CONFLICT.format(who=who(record)))


# ----------------------------------------------------------------------------- the send step
def send_all(service: Service, fetched: Fetched, problems: list[str], dropped: list[str]) -> list[tuple[str, Outcome]]:
    """Every case at SEND or SENT, as saved now, soonest due first, then by id. A case whose payment's list didn't
    answer on this pass, or whose payment isn't one of the run's, is held with a line before it takes a moment: a second
    dispute on that payment could be unseen, and the customer is never paid twice. An error in one case's send is a
    line, and the next case goes on; a ReplayMismatch comes out."""
    heads = service.db.load()
    ready = [(record, heads.versions[dispute_id]) for dispute_id, record in heads.records.items()
             if lifecycle.phase(record) in (Phase.SEND, Phase.SENT)]
    ready.sort(key=lambda pair: (lifecycle.as_dispute(pair[0]).due_at, pair[0].dispute_id))
    sent = []
    for record, version in ready:
        held = _hold(record, fetched)
        if held is not None:  # never a person's case for it: the next pass's list may answer
            problems.append(held)
            continue
        try:
            outcome = send_one(service, record, version, problems, dropped)
        except ReplayMismatch:
            raise
        except Exception:  # a bug, or the disk: never to the database or a recording
            traceback.print_exc(file=sys.stderr)
            problems.append(SEND_ERROR.format(who=who(record)))
            continue
        if outcome is not None:
            sent.append((record.dispute_id, outcome))
    return sent


def _hold(record: CaseRecord, fetched: Fetched) -> str | None:
    """Why nothing goes out for the case on this pass, or None: its payment's list of disputes didn't answer, or its
    payment isn't in the run's list of payments."""
    payment_id = record.last.payment_id
    if payment_id in fetched.listed:
        return None
    return (NOT_LISTED if payment_id in fetched.scope else NOT_IN_SCOPE).format(who=who(record))


def send_one(service: Service, record: CaseRecord, version: int, problems: list[str], dropped: list[str]) -> Outcome | None:
    """One case at SEND or SENT, at its saved version. Returns what the POST try's answer meant, or None when nothing
    was posted. dropped gets the case's id when its plan is dropped."""
    db, clock, client = service.db, service.clock, service.client
    dispute_id, name = record.dispute_id, who(record)
    t = clock.now(f"send:{dispute_id}")
    if lifecycle.blocker(record, t) is not None:  # HELD, an old reading, the deadline...: shown on the case, never sent
        return None
    step, plan, number = lifecycle.phase(record), record.rounds[-1].plan, len(record.rounds)
    # 1. the approval, before every try
    try:
        refusal = approvals.verify(record, db.latest_approval(dispute_id, number))
    except Unloadable:
        refusal = APPROVAL_UNREADABLE
    if refusal is not None:
        if step is Phase.SEND:
            _drop(db, record, version, UNMATCHED, DROPPED.format(who=name, why=refusal), problems, dropped)
        else:  # the POST may have landed: never dropped, never sent again
            problems.append(NOT_AT_SENT.format(who=name, why=refusal))
        return None
    # 2. what is on file for this request id
    try:
        tries = db.tries(plan.request_id)
    except Unloadable:
        problems.append(TRY_UNREADABLE.format(who=name))
        return None
    latest = tries[-1] if tries else None
    if step is Phase.SENT and latest is not None and latest.outcome is not Outcome.RETRY:
        # Its answer was saved, but the record wasn't updated: apply it, and send nothing.
        problems.append(APPLIED.format(who=name))
        _apply(db, record, plan.request_id, latest.outcome, latest.detail, latest.at, problems)
        return None
    if latest is not None and latest.not_before is not None and t < latest.not_before:
        problems.append(WAIT.format(who=name, time=f"{latest.not_before.astimezone(timezone.utc):%H:%M}"))
        return None
    # 3. the body: saved before the first try, then sent byte for byte on every try
    try:
        post = db.post_for(plan.request_id)
    except Unloadable:
        problems.append(BODY_UNREADABLE.format(who=name))
        return None
    if post is None:
        if step is Phase.SENT:  # sending() and the body are saved together, so the body row was lost: never rebuilt
            problems.append(BODY_MISSING.format(who=name))
            return None
        post = _build(service, record, version, t, problems, dropped)
        if post is None:
            return None
    # 4. a moment of its own for the POST, since the uploads may have taken a while
    now = clock.now(f"post:{dispute_id}")
    try:
        marked = lifecycle.sending(record, now)
    except Refused as refused:
        problems.append(NOT_SENT.format(who=name, why=refused))
        return None
    try:
        db.start_send(marked, version, post)  # the body and the SENT record, before the POST
    except Conflict:  # a screen saved first: nothing was sent, and the next pass reads again
        problems.append(CONFLICT.format(who=name))
        return None
    # 5. one POST, and nothing else until its answer: one pass at a time holds the pass lock. It is a retry when a try
    # may already be with Airwallex: one is on file, or the case was saved at SENT and its answer was lost (S3)
    answer = client.respond(post, retry=bool(tries) or step is Phase.SENT)
    at = clock.now(f"answer:{dispute_id}")
    db.put_try(Try(post.request_id, at, answer.outcome, answer.status, answer.detail, airwallex.not_before(at, answer)))
    if answer.outcome is Outcome.RETRY:
        later = airwallex.not_before(at, answer)
        problems.append(NO_ANSWER.format(who=name, detail=answer.detail) if later is None
                        else WAIT.format(who=name, time=f"{later.astimezone(timezone.utc):%H:%M}"))
    else:
        _apply(db, marked, post.request_id, answer.outcome, answer.detail, at, problems)
    return answer.outcome


def _build(service: Service, record: CaseRecord, version: int, at: datetime, problems: list[str],
           dropped: list[str]) -> PostRequest | None:
    """The plan's request at SEND: its files built again and uploaded once each, then the body, checked. None, with a
    line, when it can't be built now."""
    db, client, plan, number, name = service.db, service.client, record.rounds[-1].plan, len(record.rounds), who(record)
    try:
        documents = rounds.files_for(record, service.store, db.saved)  # before the upload, never after the first try
    except (rounds.FilesMoved, Unloadable) as moved:
        _drop(db, record, version, NOT_REBUILT, DROPPED.format(who=name, why=moved), problems, dropped)
        return None
    files = []
    for document in sorted(documents, key=lambda document: document.sha256):
        try:
            upload = db.upload_for(plan.request_id, document.sha256)  # a file uploaded for a plan is never uploaded again
        except Unloadable:
            problems.append(UPLOAD_LOST.format(who=name))
            return None
        if upload is None:
            result = client.upload(document)
            if result.file_id is None:  # still at SEND: the next pass tries again
                problems.append(UPLOAD_FAILED.format(who=name, why=airwallex.problem(result.answer)))
                return None
            upload = Upload(plan.request_id, document.sha256, record.dispute_id, number, result.file_id,
                            document.filename, result.pdf_sha256, result.size, at)
            db.put_upload(upload)
        files.append((document, upload.file_id))
    try:
        body = airwallex.accept_body(plan) if plan.terms.action is Action.ACCEPT else airwallex.challenge_body(plan, files)
        problem = airwallex.body_problem(plan, body, files)
        post = None if problem else PostRequest(plan.request_id, record.dispute_id, number, airwallex.respond_path(plan),
                                                airwallex.body_text(body))
    except (TypeError, ValueError) as error:  # an amount that isn't exact, say
        problem, post = _phrase(str(error)), None
    if post is None:  # stays at SEND: the deadline hands it to a person
        problems.append(CANT_BUILD.format(who=name, problem=problem))
    return post


def _apply(db: Database, record: CaseRecord, request_id: str, outcome: Outcome, detail: str, at: datetime,
           problems: list[str]) -> None:
    """A TAKEN or FAILED answer on the case: taken(), or send_failed() with Airwallex's words. A screen's write since is
    applied over, never lost."""
    if outcome is Outcome.TAKEN:
        def change(head: CaseRecord) -> CaseRecord:
            return lifecycle.taken(head, request_id, at)
        why = "taken"
    else:
        def change(head: CaseRecord) -> CaseRecord:
            return lifecycle.send_failed(head, request_id, airwallex.refusal(detail), at)
        why = "refused"
    try:
        db.update(record.dispute_id, change, why)
    except (Refused, Conflict):
        problems.append(MOVED_ON.format(who=who(record)))


def _drop(db: Database, record: CaseRecord, version: int, why: str, line: str, problems: list[str],
          dropped: list[str]) -> None:
    try:
        db.save(lifecycle.drop(record, why), version, "drop")
    except Conflict:  # a screen saved first: the next pass looks again
        problems.append(CONFLICT.format(who=who(record)))
        return
    problems.append(line)
    dropped.append(record.dispute_id)


# ----------------------------------------------------------------------------- the decide step
def decide_all(service: Service, problems: list[str]) -> list[str]:
    """Every round at DECIDE, as saved now, soonest due first, then by id, each reloaded just before. A plan the send
    step dropped is decided again here."""
    heads = service.db.load()
    due = sorted((record for record in heads.records.values() if lifecycle.phase(record) is Phase.DECIDE),
                 key=lambda record: (lifecycle.as_dispute(record).due_at, record.dispute_id))
    decided = []
    for candidate in due:
        try:
            record, version = service.db.load_one(candidate.dispute_id)
        except (KeyError, Unloadable):
            continue
        if lifecycle.phase(record) is Phase.DECIDE and decide_one(service, record, version, problems):
            decided.append(record.dispute_id)
    return decided


def decide_one(service: Service, record: CaseRecord, version: int, problems: list[str]) -> bool:
    """One round at DECIDE through decide_round(), the only way to keep(). Its errors (Refused, keep()'s, the saved
    readings') hold the round for a person, and the traceback goes to stderr. A round decide_round() held itself, for a
    bug in the reader, the checks or the rules, gets the same line and is saved as a hold too; a reader's traceback goes
    to stderr from _Loud. Whether the record changed and was saved."""
    name, reader = who(record), _Loud(service.reader)
    now = service.clock.now(f"decide:{record.dispute_id}")
    why = "decide"
    try:
        step = rounds.decide_round(record, service.store, reader, service.db.saved, now, service.fees)
    except ReplayMismatch:
        raise
    except Exception:
        reader.raise_mismatch()
        traceback.print_exc(file=sys.stderr)
        new, why = lifecycle.hold(record, rounds.HELD_FOR_ERROR), "hold"  # at DECIDE, so hold() can't refuse
        problems.append(DECIDE_ERROR.format(who=name))
    else:
        reader.raise_mismatch()
        new = step.record
        if step.note:  # BUSY_NOTE: still at DECIDE
            problems.append(f"{name}: {step.note}")
        elif new.rounds[-1].person == rounds.HELD_FOR_ERROR:  # at DECIDE the round had no person line: held just now
            why = "hold"
            problems.append(DECIDE_ERROR.format(who=name))
    if lifecycle.to_json(new) == lifecycle.to_json(record):
        return False
    try:
        service.db.save(new, version, why)
    except Conflict:  # the reading is saved, so the next pass reuses it with no API call
        problems.append(CONFLICT.format(who=name))
        return False
    return True


class _Loud:
    """The pass's reader as decide_round() sees it. decide_round() holds a round when its reader raises anything but
    ReaderBusy, so a ReplayMismatch from a replay reader is kept here and raised again once decide_round() is back: a
    replay that stops matching stops the demo, and is never held as a bug. Any other error is a bug in the reader: its
    traceback goes to stderr here, since decide_round() keeps nothing of it. Every other attribute is the reader's own,
    its profile and effort among them, so the reading key stays the same."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self.mismatch: ReplayMismatch | None = None

    def read(self, request: Any) -> Any:
        try:
            return self._reader.read(request)
        except ReplayMismatch as mismatch:
            self.mismatch = mismatch
            raise
        except ReaderBusy:  # not a bug: the round waits for the next pass
            raise
        except Exception:  # never to the database or a recording
            traceback.print_exc(file=sys.stderr)
            raise

    def raise_mismatch(self) -> None:
        if self.mismatch is not None:
            raise self.mismatch

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._reader, name)


# ----------------------------------------------------------------------------- words
def who(record: CaseRecord) -> str:
    """How a line names a case: "Order #1109", or "Dispute dst_..." when the payment names no order."""
    number = record.last.order_number
    return f"Order #{number}" if number else f"Dispute {record.dispute_id}"


def _phrase(sentence: str) -> str:
    """A sentence as a phrase inside a line: no full stop at the end, and a small first letter unless it starts a name
    such as CE 3.0 or HTTP."""
    text = sentence.strip().rstrip(".")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


def _sentence(text: str) -> str:
    """A reason as a sentence of its own inside a line: a capital first letter and a full stop at the end."""
    text = text.strip()
    if text[:1].islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."
