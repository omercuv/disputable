"""The bank: the story's simulated issuer and the customer's checkout. Never the agent.

The story run plays the bank with it. monday() makes each case's payment, pays it as the customer would, and opens its
dispute with the story's deadline (2, 4 and 6 days from now, and 15 minutes, so the agent's pass a moment later still
reads whole days), and it writes the run's scope: the payment rows the agent reads, and the day the story opened each
dispute, which the sandbox's created_at can't say. push_back() turns the headphones' RFI evidence down and opens the
chargeback, due in 7 days and 15 minutes, with the bank's comment. rule() ends a chargeback for the store or for the
customer. Each step reads the dispute first and refuses unless it is where the step belongs and this app's own response
at that stage shows on it.

The bank has its own Session (actor "bank"), and only it calls the sandbox's /simulation paths: the agent's client can't
reach them, and no core module imports this one. The customer pays in Airwallex.js's own card iframe, on a page this
module serves from 127.0.0.1: Airwallex's published test card goes into that iframe and nowhere else, and no card number
reaches a server of this app. The client secret stays in memory, and a recording redacts it.

preflight() checks, on three throwaway disputes outside any run, that the sandbox takes the app's exact challenge bodies
at RFI and at CHARGEBACK and its accept with a refund, what a repeated request id answers, and what Airwallex adds to a
challenge (a seller profile, the order's details, a file of its own). It prints statuses, error codes and yes or no,
never an id, an amount or a URL: its bank runs quiet, so a refused bank step says only the status and the error code,
never Airwallex's own words, which can repeat what was sent. A shape counts as refused only when Airwallex refused it;
when Airwallex gave no clear answer, or the file didn't upload, the line says the step couldn't check it.

The story run and the CLI use it like this:
    issuer = Issuer(bank_session, store, db, run=run, pay=pay_in_browser)
    opened = issuer.monday(); issuer.push_back(dispute_id); issuer.rule(dispute_id, "MERCHANT")
    disputable bank preflight: return preflight(settings, yes=args.yes)
"""

from __future__ import annotations

import http.server
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from disputable import airwallex
from disputable.airwallex import DISPUTES, PAYMENTS, AgentClient, Answer, Outcome, PostRequest, Session
from disputable.clock import wall
from disputable.config import Settings
from disputable.db import Database, IssuerDate, StorePayment
from disputable.documents import Document
from disputable.engine import Action, Stage
from disputable.fees import FEES_V1, ZERO, AcceptReason, ChallengeReason
from disputable.lifecycle import ACTOR, Plan, Terms, request_id_for
from disputable.privacy import LOCAL, Scrubber
from disputable.recording import RecordingTransport, Sink
from disputable.store import Store

CURRENCY = "USD"
DUE_AT_FORMAT = "%Y-%m-%dT%H:%M:%S+0000"  # the spike's, which the simulator took
TEST_CARD, TEST_CVC = "4035501000000008", "737"  # Airwallex's published test card: only its own iframe receives it
RETURN_URL = "https://example.com/checkout/return"
DESCRIPTOR = "FERNHILL AUDIO"
SIMULATION = "/api/v1/simulation/pa/payment_disputes"  # the issuer's side of the sandbox: the bank's session only
MONDAY_CASES = ("ear_tips", "earbuds", "headphones")
ROUND_TWO = "headphones_round_2"
DUE_GRACE = timedelta(minutes=15)  # past the story's whole days: the first pass, a moment later, reads "in 2 days"
WAITING = frozenset({"CHALLENGED", "PENDING_DECISION"})  # the bank is deciding on a response
DEAD_END = frozenset({"CANCELLED", "REQUIRES_CUSTOMER_ACTION"})
PAYMENT_TRIES, PAYMENT_WAIT = 5, 2.0  # a paid payment settles within seconds
READ_TRIES, READ_WAIT = 6, 1.5  # the sandbox can lag a moment after a change
LIST_TRIES, RETRY_WAIT = 3, 2.0
RULINGS: Mapping[str, tuple[str, frozenset[str]]] = {
    "MERCHANT": ("Issuer accepted the signed delivery and warranty registration; ruled for the merchant.",
                 frozenset({"WON", "REVERSED"})),
    "CUSTOMER": ("Issuer ruled for the cardholder.", frozenset({"LOST"})),
}

# Airwallex.js's Card element on a page served from 127.0.0.1: an http origin, so the SDK runs. The intent and its
# client secret are passed in by the browser driver, never in the URL, so they reach no log or history.
AIRWALLEX_JS_URL = "https://static.airwallex.com/components/sdk/v1/index.js"
BROWSER_CHANNELS = ("msedge", "chrome", None)  # None: Playwright's own Chromium, if installed
BROWSER_TIMEOUT_MS = 90_000
CHECKOUT_HTML = f"""<!doctype html><html><head><meta charset="utf-8"><title>Disputable test checkout</title>
<script src="{AIRWALLEX_JS_URL}"></script></head><body>
<div id="card" style="width:420px;height:60px"></div><button id="submit" type="button">Pay</button>
<script>
window.__state = 'idle';
window.startCheckout = async (cfg) => {{
  try {{
    window.__state = 'loading';
    await window.AirwallexComponentsSDK.init({{ env: 'demo', enabledElements: ['payments'] }});
    const card = await window.AirwallexComponentsSDK.createElement('card', cfg);
    card.mount('card');
    card.on('ready', () => {{ window.__state = 'ready'; }});
    document.getElementById('submit').addEventListener('click', () => {{
      window.__state = 'confirming';
      card.confirm({{ intent_id: cfg.intent_id, client_secret: cfg.client_secret }})
        .then(() => {{ window.__state = 'done'; }})
        .catch(e => {{ window.__error = (e && (e.message || e.code)) || String(e); window.__state = 'error'; }});
    }});
  }} catch (e) {{ window.__error = String(e); window.__state = 'error'; }}
}};
</script></body></html>"""

NOT_THE_BANKS = "The bank runs on its own session, never the agent's."
NO_STORE = "This step needs the story's store."
NOT_A_STAGE = "The bank opens disputes at RFI or CHARGEBACK only, never at {stage}: a pre-chargeback accepts itself."
NO_PAYMENT = "The bank couldn't make order #{order}'s payment ({why}). Nothing else was done for it."
LOST_PAYMENT = ("Airwallex already had order #{order}'s payment request, but its first answer was lost, so the bank "
                "stops. Run the story again.")
NO_SECRET = "Airwallex gave order #{order}'s payment no client secret, so it can't be paid."
NOT_PAID = "Order #{order}'s payment ended at {status}, not SUCCEEDED, so the bank stops."
NO_LIST = "The bank couldn't list the disputes on order #{order}'s payment ({why}), so it stops."
ALREADY_DISPUTED = "Order #{order}'s new payment already has a dispute, so the bank stops."
NO_DISPUTE = "The bank couldn't open order #{order}'s dispute ({why})."
LOST_DISPUTE = ("The bank's request to open order #{order}'s dispute got no clear answer ({why}), and no dispute shows "
                "on the payment. Running the story again is safe.")
NOT_OPEN = "Order #{order}'s dispute doesn't show as waiting for a response, so the bank stops."
NOT_LISTED = "Order #{order}'s dispute doesn't show in its payment's list, where the agent looks, so the bank stops."
BAD_ID = "Airwallex sent an id the bank won't put in a web address, so it stops."
UNREAD = "The bank couldn't read the dispute ({why}), so it stops."
NOT_YET_PUSHED = ("Order #{order} can't be pushed back yet: the dispute is {stage}/{status}, and this app's challenge "
                  "doesn't show.")
NOT_PUSHED = "The bank's push back didn't open a chargeback that waits for a response ({why})."
NOT_YET_RULED = ("The dispute can't be ruled on yet: it is {stage}/{status}, and this app's chargeback challenge doesn't "
                 "show.")
NOT_RULED = "The bank's ruling didn't close the dispute ({why})."
DIDNT_MOVE = "the dispute didn't move"
NO_PLAYWRIGHT = "Playwright isn't installed, so the test payment can't be made. Run uv sync."
NO_BROWSER = "No browser was found for the test payment. Install Microsoft Edge or Google Chrome."
CARD_FAILED = "The card form didn't take the test payment ({why})."
BROWSER_FAILED = "The browser payment failed ({why})."


class IssuerError(Exception):
    """A step of the bank can't run now. The message says why; the CLI prints it, with no traceback."""


@dataclass(frozen=True)
class Opened:
    """One case the bank opened on Monday: its payment (the agent's scope) and the day the story opened its dispute."""
    case_id: str
    payment: StorePayment
    date: IssuerDate

    @property
    def dispute_id(self) -> str:
        return self.date.dispute_id


class Issuer:
    """The simulated issuer, on its own session. store: the story's records, for monday() and push_back(). db: where the
    run's scope is written, or None (the preflight writes none). pay(payment_id, client_secret) pays one payment as the
    customer: pay_in_browser live, the fake sandbox's pay() in the tests. quiet: a refusal names only the status and the
    error code of Airwallex's answer, never its words, which can hold an id or an amount (the preflight's)."""

    def __init__(self, session: Session, store: Store | None, db: Database | None, *, run: str,
                 pay: Callable[[str, str], None], clock: Callable[[], datetime] = wall,
                 sleep: Callable[[float], None] = time.sleep, quiet: bool = False) -> None:
        if session.actor != "bank":
            raise ValueError(NOT_THE_BANKS)
        self.session, self.store, self.db, self.run = session, store, db, run
        self.pay, self.clock, self.sleep, self.quiet = pay, clock, sleep, quiet

    # ------------------------------------------------------------------------- the story's steps
    def monday(self, case_ids: Sequence[str] = MONDAY_CASES) -> tuple[Opened, ...]:
        """Each case, in order: its payment, paid, in the run's scope; then its dispute, due in the case's days from now
        and DUE_GRACE, and the day the story opened it."""
        store = self._store()
        opened = []
        for case_id in case_ids:
            case = store.case(case_id)
            order = store.order(case.order_number)
            if case.stage not in (Stage.RFI, Stage.CHARGEBACK):
                raise IssuerError(NOT_A_STAGE.format(stage=case.stage.value))
            payment_id, merchant_order_id = self.pay_order(order.number, order.amount, label=case_id)
            payment = StorePayment(payment_id, order.number, merchant_order_id, _utc(self.clock()))
            if self.db is not None:
                self.db.put_store_payment(payment)
            dispute = self.open_dispute(payment_id, order=order.number, reason_code=case.reason_code, stage=case.stage,
                                        amount=order.amount if case.amount is None else case.amount,
                                        comment=case.issuer_comment, due_in=case.due_in + DUE_GRACE, label=case_id)
            day = IssuerDate(dispute["id"], case_id, case.opened_at)
            if self.db is not None:
                self.db.put_issuer_date(day)
            opened.append(Opened(case_id, payment, day))
        return tuple(opened)

    def push_back(self, dispute_id: str, case_id: str = ROUND_TWO) -> dict[str, Any]:
        """The bank turns the RFI evidence down: a chargeback, due in the round's days from now and DUE_GRACE, with its
        comment. Only once this app's RFI challenge shows and the bank is deciding on it."""
        case = self._store().case(case_id)
        dispute = self._read(dispute_id, case_id)
        if not (dispute.get("stage") == "RFI" and dispute.get("status") in WAITING and _ours(dispute, "RFI")):
            raise IssuerError(NOT_YET_PUSHED.format(order=case.order_number, stage=dispute.get("stage"),
                                                    status=dispute.get("status")))
        due = _utc(self.clock()) + case.due_in + DUE_GRACE
        body = {"due_at": due.strftime(DUE_AT_FORMAT), "comment": case.issuer_comment}
        answer = self._post(f"{SIMULATION}/{dispute_id}/escalate", body, f"sim-escalate-{case_id}")
        if not answer.ok and not _unsettled(answer):
            raise IssuerError(NOT_PUSHED.format(why=self._why(answer)))
        after = self._wait(dispute_id, case_id, lambda d: (d.get("stage"), d.get("status")) == ("CHARGEBACK",
                                                                                             "REQUIRES_RESPONSE"))
        if after is None:
            raise IssuerError(NOT_PUSHED.format(why=DIDNT_MOVE))
        return after

    def rule(self, dispute_id: str, in_favor_of: str) -> dict[str, Any]:
        """The bank rules on a chargeback this app challenged: "MERCHANT" (WON) or "CUSTOMER" (LOST)."""
        if in_favor_of not in RULINGS:
            raise ValueError("The bank rules for the MERCHANT or the CUSTOMER.")
        comment, ended = RULINGS[in_favor_of]
        dispute = self._read(dispute_id, "rule")
        if not (dispute.get("stage") in ("CHARGEBACK", "PRE_ARBITRATION") and dispute.get("status") in WAITING
                and _ours(dispute, "CHARGEBACK")):
            raise IssuerError(NOT_YET_RULED.format(stage=dispute.get("stage"), status=dispute.get("status")))
        answer = self._post(f"{SIMULATION}/{dispute_id}/resolve", {"in_favor_of": in_favor_of, "comment": comment},
                            "sim-resolve")
        if not answer.ok and not _unsettled(answer):
            raise IssuerError(NOT_RULED.format(why=self._why(answer)))
        after = self._wait(dispute_id, "rule", lambda d: d.get("status") in ended)
        if after is None:
            raise IssuerError(NOT_RULED.format(why=DIDNT_MOVE))
        return after

    # ------------------------------------------------------------------------- the parts
    def pay_order(self, order_number: str, amount: Decimal, *, label: str) -> tuple[str, str]:
        """A payment for the order, paid by the customer and SUCCEEDED: its id and merchant order id. Its metadata names
        the order and the run, so the agent's reading ties the dispute to the store's order."""
        merchant_order_id = f"FH-{order_number}-{self.run}-{secrets.token_hex(2)}"[:64]
        body = {"request_id": str(uuid.uuid4()), "amount": airwallex.json_number(amount), "currency": CURRENCY,
                "merchant_order_id": merchant_order_id, "descriptor": DESCRIPTOR, "return_url": RETURN_URL,
                "metadata": {"order_no": order_number, "run": self.run}}
        answer = self._post(f"{PAYMENTS}/create", body, f"pi-create-{label}")
        if not answer.ok and _unsettled(answer):  # the same request id once more: Airwallex makes it once
            self.sleep(RETRY_WAIT)
            answer = self._post(f"{PAYMENTS}/create", body, f"pi-create-{label}")
            if answer.code.startswith("duplicate_request"):
                raise IssuerError(LOST_PAYMENT.format(order=order_number))
        created = answer.data if answer.ok and isinstance(answer.data, dict) else None
        if created is None:
            raise IssuerError(NO_PAYMENT.format(order=order_number, why=self._why(answer)))
        payment_id = created.get("id")
        if not airwallex.safe_id(payment_id):
            raise IssuerError(BAD_ID)
        status = str(created.get("status") or "")
        if status == "REQUIRES_PAYMENT_METHOD":
            secret = created.get("client_secret")
            if not (isinstance(secret, str) and secret):
                raise IssuerError(NO_SECRET.format(order=order_number))
            self.pay(payment_id, secret)
        for attempt in range(PAYMENT_TRIES):
            read = self.session.get(f"{PAYMENTS}/{payment_id}", label=f"pi-get-{label}")
            if read.ok and isinstance(read.data, dict):
                status = str(read.data.get("status") or "")
            if status == "SUCCEEDED":
                return payment_id, merchant_order_id
            if status in DEAD_END:
                break
            if attempt + 1 < PAYMENT_TRIES:
                self.sleep(PAYMENT_WAIT)
        raise IssuerError(NOT_PAID.format(order=order_number, status=status or "an unknown status"))

    def open_dispute(self, payment_id: str, *, order: str, reason_code: str, stage: Stage, amount: Decimal,
                     comment: str, due_in: timedelta, label: str) -> dict[str, Any]:
        """A dispute on the payment, as the issuer opens one, once it reads as waiting for a response and shows in the
        payment's list, where the agent looks. The simulator takes no request id, so an answer that got lost is found on
        the payment instead of opening a second dispute."""
        if self._listed(payment_id, order, label):
            raise IssuerError(ALREADY_DISPUTED.format(order=order))
        body = {"payment_intent_id": payment_id, "reason_code": reason_code, "stage": stage.value,
                "amount": airwallex.json_number(amount), "comment": comment,
                "due_at": (_utc(self.clock()) + due_in).strftime(DUE_AT_FORMAT)}
        answer = self._post(f"{SIMULATION}/create", body, f"sim-create-{label}")
        dispute = answer.data if answer.ok and isinstance(answer.data, dict) and answer.data.get("id") else None
        if dispute is None:
            if not _unsettled(answer):
                raise IssuerError(NO_DISPUTE.format(order=order, why=self._why(answer)))
            found = self._listed(payment_id, order, label)
            if len(found) != 1:
                raise IssuerError(LOST_DISPUTE.format(order=order, why=self._why(answer)))
            dispute = found[0]
        dispute_id = dispute.get("id")
        if not airwallex.safe_id(dispute_id):
            raise IssuerError(BAD_ID)
        shown = self._wait(dispute_id, label, lambda d: d.get("status") == "REQUIRES_RESPONSE")
        if shown is None:
            raise IssuerError(NOT_OPEN.format(order=order))
        for attempt in range(LIST_TRIES):
            if any(item.get("id") == dispute_id for item in self._listed(payment_id, order, label)):
                return shown
            if attempt + 1 < LIST_TRIES:
                self.sleep(READ_WAIT)
        raise IssuerError(NOT_LISTED.format(order=order))

    def read(self, dispute_id: str, label: str) -> dict[str, Any] | None:
        """The dispute as the bank reads it, or None when Airwallex didn't answer with one."""
        if not airwallex.safe_id(dispute_id):
            raise IssuerError(BAD_ID)
        answer = self.session.get(f"{DISPUTES}/{dispute_id}", label=f"bank-dispute-{label}")
        return answer.data if answer.ok and isinstance(answer.data, dict) else None

    def _read(self, dispute_id: str, label: str) -> dict[str, Any]:
        if not airwallex.safe_id(dispute_id):
            raise IssuerError(BAD_ID)
        answer = self.session.get(f"{DISPUTES}/{dispute_id}", label=f"bank-dispute-{label}")
        if not (answer.ok and isinstance(answer.data, dict)):
            raise IssuerError(UNREAD.format(why=self._why(answer)))
        return answer.data

    def _wait(self, dispute_id: str, label: str, until: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        """The dispute once until() holds, read up to 6 times 1.5 s apart, or None."""
        for attempt in range(READ_TRIES):
            dispute = self.read(dispute_id, label)
            if dispute is not None and until(dispute):
                return dispute
            if attempt + 1 < READ_TRIES:
                self.sleep(READ_WAIT)
        return None

    def _listed(self, payment_id: str, order: str, label: str) -> list[dict[str, Any]]:
        answer = self.session.get(DISPUTES, params={"payment_intent_id": payment_id, "size": airwallex.LIST_SIZE},
                                  label=f"bank-list-{label}")
        items = answer.data.get("items") if answer.ok and isinstance(answer.data, dict) else None
        if not isinstance(items, list):
            raise IssuerError(NO_LIST.format(order=order, why=self._why(answer)))
        return [item for item in items if isinstance(item, dict) and item.get("payment_intent_id") == payment_id]

    def _post(self, path: str, body: Mapping[str, Any], label: str) -> Answer:
        return self.session.post_json(path, airwallex.body_text(body), label=label)

    def _why(self, answer: Answer) -> str:
        """Why a step refuses, from Airwallex's answer: airwallex.problem()'s phrase, with Airwallex's own words, or, when
        quiet, only the status and the error code."""
        return _status(answer) if self.quiet else airwallex.problem(answer)

    def _store(self) -> Store:
        if self.store is None:
            raise ValueError(NO_STORE)
        return self.store


def _ours(dispute: Mapping[str, Any], stage: str) -> bool:
    """Whether this app's challenge at that stage shows on the dispute."""
    return any(isinstance(entry, Mapping) and entry.get("challenged_by") == ACTOR and entry.get("stage") == stage
               for entry in dispute.get("challenge_details") or ())


def _unsettled(answer: Answer) -> bool:
    """No clear answer: none at all, a 429 or a 5xx. Anything else says what happened."""
    return answer.status is None or answer.status == 429 or answer.status >= 500 or answer.login_failed


def _utc(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("A moment needs a time zone.")
    return moment.astimezone(timezone.utc)


# ----------------------------------------------------------------------------- the customer's checkout
class CheckoutPage(http.server.BaseHTTPRequestHandler):
    """Serves the checkout page. Airwallex.js needs an http origin, so the page is served from 127.0.0.1."""

    def do_GET(self) -> None:
        body = CHECKOUT_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def checkout_server() -> http.server.ThreadingHTTPServer:
    """The checkout page's server, on 127.0.0.1 and a free port. Not started: the caller serves and shuts it."""
    return http.server.ThreadingHTTPServer((LOCAL, 0), CheckoutPage)


def launch_browser(playwright: Any, error_type: type[Exception]) -> Any:
    for channel in BROWSER_CHANNELS:
        try:
            return playwright.chromium.launch(channel=channel, headless=True)
        except error_type:
            continue
    raise IssuerError(NO_BROWSER)


def pay_in_browser(intent_id: str, client_secret: str) -> None:
    """Pays with the test card in Airwallex.js's Card element, on the local page, in a headless Edge or Chrome. Only
    Airwallex's iframe ever receives the card data. The intent and its secret are handed to the page's script, never put
    in a URL."""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise IssuerError(NO_PLAYWRIGHT) from None
    server = checkout_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright, PlaywrightError)
            try:
                page = browser.new_page()
                page.goto(f"http://{LOCAL}:{server.server_address[1]}/")
                page.evaluate("cfg => window.startCheckout(cfg)",
                              {"intent_id": intent_id, "client_secret": client_secret, "currency": CURRENCY})
                page.wait_for_function("['ready', 'error'].includes(window.__state)", timeout=BROWSER_TIMEOUT_MS)
                if page.evaluate("window.__state") == "ready":
                    fields = page.frame_locator("#card iframe").locator("input")
                    fields.nth(0).fill(TEST_CARD)
                    fields.nth(1).fill(f"12/{(datetime.now(timezone.utc).year + 3) % 100:02d}")
                    fields.nth(2).fill(TEST_CVC)
                    time.sleep(0.5)  # the element checks its fields before it takes a confirm
                    page.click("#submit")
                    page.wait_for_function("['done', 'error'].includes(window.__state)", timeout=BROWSER_TIMEOUT_MS)
                if page.evaluate("window.__state") == "error":
                    raise IssuerError(CARD_FAILED.format(why=str(page.evaluate("window.__error"))[:200]))
            finally:
                browser.close()
    except PlaywrightError as error:
        raise IssuerError(BROWSER_FAILED.format(why=str(error).splitlines()[0][:200] if str(error) else "no detail")
                          ) from None
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------------- bank preflight
@dataclass(frozen=True)
class Check:
    """One throwaway dispute of the preflight, and the response the app sends to it."""
    n: int
    what: str
    order: str  # outside every store and every run's scope
    stage: Stage
    reason_code: str
    action: Action
    reason: str
    refund: Decimal = ZERO


CHECKS = (
    Check(1, "a challenge at RFI", "9101", Stage.RFI, "10.4", Action.CHALLENGE, ChallengeReason.PURCHASE_HISTORY.value),
    Check(2, "a challenge at CHARGEBACK", "9102", Stage.CHARGEBACK, "10.4", Action.CHALLENGE,
          ChallengeReason.PRODUCT_RECEIVED.value),
    Check(3, "an accept at RFI with a refund", "9103", Stage.RFI, "13.1", Action.ACCEPT,
          AcceptReason.LOW_VALUE_TRANSACTION.value, refund=Decimal(25)),
)
PREFLIGHT_AMOUNT = Decimal(25)
PREFLIGHT_DUE = timedelta(days=14)
PREFLIGHT_COMMENT = "A check of the request shape. Not part of the story."
PREFLIGHT_FILE = Document("Not evidence", ("Not evidence. A check of the request shape.",), frozenset({"preflight"}),
                          "A check of the request shape", "Not evidence. A check of the request shape.")
PREFLIGHT_QUESTION = "This makes 3 test payments in the sandbox with Airwallex's test card. Type yes to go on: "
PREFLIGHT_STOPPED = "Stopped. Nothing was paid."
STEP = "Step {n}: {what}."
FIRST_POST = "  The first POST: {status}. {meaning}"
SAME_AGAIN = "  The same request id again: {status}. {meaning}"
SHOWS = "  The dispute shows this app's response: {answer}."
ADDED = ("  Airwallex filled in a seller profile: {profile}. It filled in the order's details: {order}. It added a file "
         "of its own: {file}.")
CLOSED = "  The bank closed the dispute: {status}."
STAYS_OPEN = "  The dispute stays open in the sandbox: the simulator rules on chargebacks only."
OPEN_UNANSWERED = "  The dispute stays open in the sandbox: the bank rules only on a response that shows."
NOT_UPLOADED = "  The file didn't upload ({why}), so nothing was sent."
NOT_BUILT = "  The app couldn't build the request ({why}), so nothing was sent."
MEANINGS = {Outcome.TAKEN: "The app reads this as taken.", Outcome.FAILED: "The app reads this as refused.",
            Outcome.RETRY: "The app would read the dispute again and send the same request."}
BOTH_TAKEN = "Both challenge shapes were taken."
SHAPE_REFUSED = "A challenge shape was refused. Fix the request bodies before the story is recorded."
NOT_CHECKED = "Step {n} couldn't check the challenge shape: {why}. Run the preflight again later."
BODY_NOT_BUILT = "The app couldn't build a challenge body. Fix the request bodies before the story is recorded."
NO_UPLOAD = "the file didn't upload"
EACH_TIME = "Airwallex answered HTTP {status} each time"
NO_ANSWER_EITHER = "Airwallex didn't answer either POST"
UNCLEAR = "Airwallex gave no clear answer"
WRITTEN = "The scrubbed calls are in {path}."
TAKEN, REFUSED, UNBUILT, UNCHECKED = "taken", "refused", "unbuilt", "unchecked"  # what a challenge check found


def preflight(settings: Settings, *, yes: bool = False, ask: Callable[[str], str] = input,
              say: Callable[[str], object] = print, transport: httpx.BaseTransport | None = None,
              pay: Callable[[str, str], None] = pay_in_browser, clock: Callable[[], datetime] = wall,
              sleep: Callable[[float], None] = time.sleep, run: str | None = None) -> int:
    """disputable bank preflight: three throwaway $25 disputes, each answered with the app's own body, twice with the
    same request id. 0 when both challenge shapes were taken, else 1, with a line for each shape that was refused or
    couldn't be checked. The scrubbed calls go to <data>/preflight-<run>/exchanges.jsonl, never the repo. SetupError
    when the sandbox keys aren't set; IssuerError, with no words of Airwallex's, when a bank step refuses."""
    keys = airwallex.credentials(settings)
    if not yes:
        try:
            answer = ask(PREFLIGHT_QUESTION)
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not (isinstance(answer, str) and answer.strip().casefold() == "yes"):
            say(PREFLIGHT_STOPPED)
            return 1
    run = run or secrets.token_hex(4)
    folder = Path(settings.data_dir) / f"preflight-{run}"
    sink = Sink(folder / "exchanges.jsonl", Scrubber([keys.client_id, keys.api_key], frozenset({LOCAL})), clock=clock)
    inner = transport if transport is not None else httpx.HTTPTransport(trust_env=False)
    bank = Session(keys, actor="bank", transport=RecordingTransport(inner, sink), sleep=sleep)
    agent = Session(keys, actor="agent", transport=RecordingTransport(inner, sink), sleep=sleep)
    issuer = Issuer(bank, None, None, run=run, pay=pay, clock=clock, sleep=sleep, quiet=True)
    shapes: list[Shape] = []
    try:
        for check in CHECKS:
            sink.beat = check.n
            say(STEP.format(n=check.n, what=check.what))
            shape = _check(check, issuer, agent, say)
            if check.action is Action.CHALLENGE:
                shapes.append(shape)
    finally:
        for session in (bank, agent):
            session.close()
        if transport is None:
            inner.close()
    say(WRITTEN.format(path=folder))
    return _verdict(shapes, say)


@dataclass(frozen=True)
class Shape:
    """What one check found of the request shape: taken, refused by Airwallex, not built by the app, or not checked."""
    n: int  # the step
    outcome: str  # TAKEN, REFUSED, UNBUILT or UNCHECKED
    why: str = ""  # UNCHECKED: why the step couldn't tell


def _verdict(shapes: Sequence[Shape], say: Callable[[str], object]) -> int:
    """The preflight's last lines and its exit code: 0 only when both challenge shapes were taken. A shape the step
    couldn't check says so, and blames no request body."""
    for shape in shapes:
        if shape.outcome == UNCHECKED:
            say(NOT_CHECKED.format(n=shape.n, why=shape.why))
    if any(shape.outcome == UNBUILT for shape in shapes):
        say(BODY_NOT_BUILT)
    if any(shape.outcome == REFUSED for shape in shapes):
        say(SHAPE_REFUSED)
    if all(shape.outcome == TAKEN for shape in shapes):
        say(BOTH_TAKEN)
        return 0
    return 1


def _check(check: Check, issuer: Issuer, agent: Session, say: Callable[[str], object]) -> Shape:
    """One throwaway dispute: paid, opened, answered twice with one request id, read after each POST, and ruled on when
    the simulator can. What it found of the shape."""
    payment_id, _ = issuer.pay_order(check.order, PREFLIGHT_AMOUNT, label=check.order)
    dispute = issuer.open_dispute(payment_id, order=check.order, reason_code=check.reason_code, stage=check.stage,
                                  amount=PREFLIGHT_AMOUNT, comment=PREFLIGHT_COMMENT, due_in=PREFLIGHT_DUE,
                                  label=check.order)
    dispute_id = dispute["id"]
    challenge = check.action is Action.CHALLENGE
    files: list[tuple[Document, str]] = []
    if challenge:
        uploaded = AgentClient(agent).upload(PREFLIGHT_FILE)
        if uploaded.file_id is None:
            say(NOT_UPLOADED.format(why=_status(uploaded.answer)))
            return Shape(check.n, UNCHECKED, NO_UPLOAD)
        files.append((PREFLIGHT_FILE, uploaded.file_id))
    plan = _plan(check, dispute, dispute_id, _utc(issuer.clock()))
    try:
        body = airwallex.challenge_body(plan, files) if challenge else airwallex.accept_body(plan)
        problem = airwallex.body_problem(plan, body, files)
        post = None if problem else PostRequest(plan.request_id, dispute_id, 1, airwallex.respond_path(plan),
                                                airwallex.body_text(body))
    except (TypeError, ValueError) as error:
        problem, post = str(error), None
    if post is None:
        say(NOT_BUILT.format(why=problem))
        return Shape(check.n, UNBUILT)
    label = f"{check.action.value.lower()}-{dispute_id}"
    first = agent.post_json(post.path, post.body, label=label)
    say(FIRST_POST.format(status=_status(first), meaning=MEANINGS[airwallex.outcome_of(first, retry=False).outcome]))
    after = issuer.read(dispute_id, check.order) or {}
    again = agent.post_json(post.path, post.body, label=label)
    say(SAME_AGAIN.format(status=_status(again), meaning=MEANINGS[airwallex.outcome_of(again, retry=True).outcome]))
    after = issuer.read(dispute_id, check.order) or after
    entry = _own_reply(after, check)
    say(SHOWS.format(answer="yes" if entry is not None else "no"))
    if challenge:
        if entry is not None:
            documents = entry.get("supporting_documents") or {}
            say(ADDED.format(profile=_yes(bool(entry.get("seller_info"))), order=_yes(bool(entry.get("order_info"))),
                             file=_yes(bool(documents.get("generated_files") if isinstance(documents, Mapping)
                                            else False))))
        if after.get("stage") in ("CHARGEBACK", "PRE_ARBITRATION") and after.get("status") in WAITING:
            ruled = issuer.rule(dispute_id, "MERCHANT")
            say(CLOSED.format(status=ruled.get("status")))
        else:
            say(STAYS_OPEN if check.stage is Stage.RFI else OPEN_UNANSWERED)
    return _shape(check.n, first, again, shows=entry is not None)


def _shape(n: int, first: Answer, again: Answer, *, shows: bool) -> Shape:
    """Taken: a 2xx, or no clear first answer and then the response shows or the repeat is taken (the first may have
    landed). Refused: a 4xx on the first POST, or on the repeat of one that got no clear answer while nothing shows.
    Anything else (no answer, a 429 or a 5xx each time) didn't check the shape."""
    if first.ok:
        return Shape(n, TAKEN)
    if airwallex.outcome_of(first, retry=False).outcome is Outcome.FAILED:
        return Shape(n, REFUSED)
    if shows or again.ok:
        return Shape(n, TAKEN)
    if airwallex.outcome_of(again, retry=True).outcome is Outcome.FAILED:
        return Shape(n, REFUSED)
    if first.status is not None and first.status == again.status:
        return Shape(n, UNCHECKED, EACH_TIME.format(status=first.status))
    return Shape(n, UNCHECKED, NO_ANSWER_EITHER if first.status is None and again.status is None else UNCLEAR)


def _plan(check: Check, dispute: Mapping[str, Any], dispute_id: str, now: datetime) -> Plan:
    """The plan the app would keep for this dispute, so its body is built exactly as the app builds one."""
    challenge = check.action is Action.CHALLENGE
    due = datetime.fromisoformat(str(dispute.get("due_at")))
    terms = Terms(dispute_id, check.stage, check.action, PREFLIGHT_AMOUNT, CURRENCY, "preflight", check.reason,
                  check.refund, ZERO, PREFLIGHT_FILE.fact_ids if challenge else frozenset(),
                  frozenset({PREFLIGHT_FILE.sha256}) if challenge else frozenset(), due, FEES_V1.version)
    return Plan(terms, request_id_for(terms), ("A preflight check of the request shape.",), now, True)


def _own_reply(dispute: Mapping[str, Any], check: Check) -> Mapping[str, Any] | None:
    key, by = ("challenge_details", "challenged_by") if check.action is Action.CHALLENGE else ("accept_details",
                                                                                              "accepted_by")
    found = [entry for entry in dispute.get(key) or () if isinstance(entry, Mapping) and entry.get(by) == ACTOR
             and entry.get("stage") == check.stage.value]
    return found[-1] if found else None


def _status(answer: Answer) -> str:
    """An answer as the preflight prints it: the status and the error code, never Airwallex's words or an id."""
    if answer.status is None:
        return f"no answer ({answer.error or 'no detail'})"
    code = answer.code
    return f"HTTP {answer.status}" + (f", {code}" if code and airwallex.ID.fullmatch(code) else "")


def _yes(value: bool) -> str:
    return "yes" if value else "no"
