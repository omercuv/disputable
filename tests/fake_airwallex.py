"""The fake Airwallex sandbox: tests/sandbox.py's Sandbox behind Airwallex's REST paths, for httpx.MockTransport.

It answers the way the sandbox did on 2026-10-30 (fixtures/spike). A login gives a made-up token, the list by payment
answers {"items": [...]}, an unknown id answers 404, an upload answers a file id shaped like Airwallex's, and an accept or
a challenge is applied to the sandbox and answered with the dispute. A request id it already applied answers 400
duplicate_request (the code's assumption, until the bank preflight checks what a repeat really returns). A dispute that
no longer asks answers 400 validation_error "Dispute transition is not supported", an unknown accept reason answers 400,
and an unknown challenge reason is stored as OTHER_REASONS with a 200. Each challenge it applies gets a summary file of
its own, a seller profile whose store URL holds the made-up account id, and the order's number, currency and date from
the payment, as the sandbox adds them (066, 073), so the privacy tests meet them. An upload's answer carries the files
host's cf-ray and cookie, and a payment's answer the paying browser's device data (031, 039, 064 and 003, 005, 007), so
a recording must hide them. fail() injects faults. Payment creation, pay() and the issuer's routes are there for the
bank's tests.

Every id, key, token, cookie and address here is made up. ACCOUNT_ID isn't privacy's stand-in, so a test can tell that a
recording hid it.

The tests use it like this:
    fake = FakeAirwallex(Sandbox()); fake.sandbox.open("ear_tips")
    client = AgentClient(fake.session())     # or Session(fake_airwallex.credentials(), transport=fake.transport(), ...)
    fake.fail("GET", "/api/v1/pa/payment_disputes/dst_ear_tips", timeout=True)
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import httpx

from disputable.airwallex import (DISPUTES, LOGIN, PAYMENTS, UPLOAD, AgentClient, Credentials, Session,
                                  json_number)
from disputable.engine import Action
from disputable.fees import AcceptReason, ChallengeReason, money
from sandbox import RUN_DATE, Sandbox

ACCOUNT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"  # made up: the fake's file ids and its store URL carry it
REGION = "sg"
CLIENT_ID = "fake-client-id-0123"
API_KEY = "fake-api-key-0123456789"
API_HOST, FILES_HOST = "api.sandbox.airwallex.com", "files.sandbox.airwallex.com"
SIMULATION = "/api/v1/simulation/pa/payment_disputes"
PROFILE = {"store_name": "Sandbox Business", "store_physical_address": "1 Example Road, Example City",
           "store_url": f"https://github.com/{ACCOUNT_ID}"}  # the shape Airwallex fills in when a body has no seller_info
FIRST_MILLIS = 1793393507092  # file ids end in the upload's time, in milliseconds
FILES_HEADERS = {"cf-ray": "0123456789abcdef-XYZ",  # the shape of Cloudflare's: a ray id and the site that answered
                 "set-cookie": "__cf_bm=fake-cookie-0123456789; path=/; domain=.airwallex.com; HttpOnly; Secure"}
DEVICE_DATA = {"device_id": "dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb", "ip_address": "203.0.113.10",
               "browser": {"user_agent": "Mozilla/5.0 (fake browser)", "javascript_enabled": True},
               "language": "en-US", "screen_height": 900, "screen_width": 1440, "timezone": "0"}
NOT_SUPPORTED = "Dispute transition is not supported"
ACCEPT_REASONS = frozenset(reason.value for reason in AcceptReason)
CHALLENGE_REASONS = frozenset(reason.value for reason in ChallengeReason)
CODES = {400: "validation_error", 401: "unauthorized", 403: "forbidden", 404: "resource_not_found",
         408: "request_timeout", 409: "conflict", 429: "too_many_requests"}
ID = "(?P<id>[^/]+)"
ROUTES = (
    ("GET", re.compile(re.escape(DISPUTES)), "_list"),
    ("GET", re.compile(rf"{re.escape(DISPUTES)}/{ID}"), "_dispute"),
    ("POST", re.compile(rf"{re.escape(DISPUTES)}/{ID}/accept"), "_accept"),
    ("POST", re.compile(rf"{re.escape(DISPUTES)}/{ID}/challenge"), "_challenge"),
    ("POST", re.compile(rf"{re.escape(PAYMENTS)}/create"), "_create_payment"),
    ("GET", re.compile(rf"{re.escape(PAYMENTS)}/{ID}"), "_payment"),
    ("POST", re.compile(rf"{re.escape(SIMULATION)}/create"), "_open_dispute"),
    ("POST", re.compile(rf"{re.escape(SIMULATION)}/{ID}/escalate"), "_escalate"),
    ("POST", re.compile(rf"{re.escape(SIMULATION)}/{ID}/resolve"), "_resolve"),
)


def credentials() -> Credentials:
    return Credentials(CLIENT_ID, API_KEY, f"https://{API_HOST}", f"https://{FILES_HOST}")


def file_id(name: str, millis: int, account_id: str = ACCOUNT_ID) -> str:
    """A file id as Airwallex makes one: base64 of '<account id>,|,<region>,|,<file name>_<millis>', with no padding."""
    return base64.b64encode(f"{account_id},|,{REGION},|,{name}_{millis}".encode()).decode().rstrip("=")


@dataclass
class Fault:
    method: str
    path: str
    status: int | None
    timeout: bool
    connect: bool
    lost: bool
    body: Any
    retry_after: float | str | None
    left: int
    headers: dict[str, str]


class FakeAirwallex:
    """tests/sandbox.py's Sandbox behind Airwallex's REST paths, for httpx.MockTransport. It keeps every request in
    calls, dedups request ids and plays the issuer's side for the bank's tests."""

    def __init__(self, sandbox: Sandbox, *, account_id: str = ACCOUNT_ID) -> None:
        self.sandbox, self.account_id = sandbox, account_id
        self.calls: list[httpx.Request] = []
        self.files: dict[str, dict[str, Any]] = {}  # file id -> what was uploaded
        self.request_ids: set[str] = set()  # applied ones only: a refused request doesn't use its id
        self.tokens: set[str] = set()  # valid now
        self.logins = 0
        self.faults: list[Fault] = []
        self._made = 0  # payments and disputes this fake made, for their ids

    # ------------------------------------------------------------------------- for the tests
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def session(self, actor: str = "agent", **options: Any) -> Session:
        """A Session on this fake that never waits."""
        options.setdefault("sleep", lambda seconds: None)
        return Session(credentials(), actor=actor, transport=self.transport(), **options)

    def client(self, **options: Any) -> AgentClient:
        return AgentClient(self.session("agent", **options))

    def fail(self, method: str, path: str, *, status: int | None = None, timeout: bool = False, lost: bool = False,
             times: int = 1, body: Any = None, retry_after: float | str | None = None, connect: bool = False,
             headers: dict[str, str] | None = None) -> None:
        """The next times requests to method and path fail: with status (and body, a dict or a text, else Airwallex's
        usual error body, and a Retry-After when given, in seconds or as the header's own text, and any other headers,
        such as the answer's Date), with no answer in time (timeout), with no connection (connect), or applied with
        their answer lost (lost: the request changes the sandbox, then httpx.ReadTimeout)."""
        if sum((status is not None, timeout, connect, lost)) != 1:
            raise ValueError("A fault is one of: a status, a timeout, a connection error or a lost answer.")
        self.faults.append(Fault(method, path, status, timeout, connect, lost, body, retry_after, times,
                                 dict(headers or {})))

    def expire_tokens(self) -> None:
        """Every token so far answers 401 from now on, as an expired one does."""
        self.tokens.clear()

    def pay(self, payment_id: str) -> None:
        """What the customer's card form does in the browser checkout, which can't run here: the payment succeeds."""
        payment = self.sandbox.payments[payment_id]
        payment.update(status="SUCCEEDED", captured_amount=payment["amount"],
                       latest_payment_attempt={"captured_amount": payment["amount"], "refunded_amount": 0})

    def seen(self, method: str | None = None) -> list[str]:
        """The path of every request, in order, of one method or all."""
        return [request.url.path for request in self.calls if method in (None, request.method)]

    # ------------------------------------------------------------------------- the transport
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        fault = self._fault(request)
        if fault is not None and not fault.lost:
            if fault.timeout:
                raise httpx.ReadTimeout("The fake sandbox didn't answer in time.", request=request)
            if fault.connect:
                raise httpx.ConnectError("The fake sandbox refused the connection.", request=request)
            after = fault.retry_after
            headers = {**fault.headers,
                       **({} if after is None else {"Retry-After": after if isinstance(after, str) else f"{after:g}"})}
            if isinstance(fault.body, str):
                return httpx.Response(fault.status, text=fault.body, headers=headers)
            return httpx.Response(fault.status, json=fault.body if fault.body is not None else _error(fault.status),
                                  headers=headers)
        response = self._route(request)
        if fault is not None:  # lost: applied, then the answer never arrived
            raise httpx.ReadTimeout("The fake sandbox's answer was lost.", request=request)
        return response

    def _fault(self, request: httpx.Request) -> Fault | None:
        for fault in self.faults:
            if fault.left > 0 and (fault.method, fault.path) == (request.method, request.url.path):
                fault.left -= 1
                return fault
        return None

    def _route(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == FILES_HOST:
            if (request.method, path) != ("POST", UPLOAD):
                return _answer(404, _error(404))
            return self._refused(request) or self._upload(request)
        if host != API_HOST:
            return _answer(404, _error(404))
        if (request.method, path) == ("POST", LOGIN):
            return self._login(request)
        refused = self._refused(request)
        if refused is not None:
            return refused
        for method, pattern, name in ROUTES:
            found = pattern.fullmatch(path)
            if method == request.method and found:
                handler: Callable[..., httpx.Response] = getattr(self, name)
                return handler(request, **found.groupdict())
        return _answer(404, _error(404))

    # ------------------------------------------------------------------------- logins
    def _login(self, request: httpx.Request) -> httpx.Response:
        if (request.headers.get("x-client-id"), request.headers.get("x-api-key")) != (CLIENT_ID, API_KEY):
            return _answer(401, {"code": "credentials_invalid", "message": "The client id or the API key is wrong."})
        self.logins += 1
        token = f"fake-token-{self.logins:04d}-{hashlib.sha256(str(self.logins).encode()).hexdigest()[:24]}"
        self.tokens.add(token)
        return _answer(201, {"expires_at": "2026-11-02T17:30:00+0000", "token": token})

    def _refused(self, request: httpx.Request) -> httpx.Response | None:
        given = request.headers.get("authorization", "")
        if given.startswith("Bearer ") and given.removeprefix("Bearer ") in self.tokens:
            return None
        return _answer(401, {"code": "unauthorized", "message": "Access token is invalid or expired."})

    # ------------------------------------------------------------------------- reads
    def _list(self, request: httpx.Request) -> httpx.Response:
        payment_id = request.url.params.get("payment_intent_id")
        items = [copy.deepcopy(dispute) for dispute in self.sandbox.disputes.values()
                 if payment_id is None or dispute["payment_intent_id"] == payment_id]
        return _answer(200, {"items": items})

    def _dispute(self, request: httpx.Request, id: str) -> httpx.Response:
        dispute = self.sandbox.disputes.get(id)
        return _answer(404, _error(404)) if dispute is None else _answer(200, copy.deepcopy(dispute))

    def _payment(self, request: httpx.Request, id: str) -> httpx.Response:
        """The payment, with the paying browser's device data on its latest attempt, as the sandbox answers."""
        payment = self.sandbox.payments.get(id)
        if payment is None:
            return _answer(404, _error(404))
        answer = copy.deepcopy(payment)
        if isinstance(answer.get("latest_payment_attempt"), dict):
            answer["latest_payment_attempt"]["device_data"] = copy.deepcopy(DEVICE_DATA)
        return _answer(200, answer)

    # ------------------------------------------------------------------------- the agent's writes
    def _upload(self, request: httpx.Request) -> httpx.Response:
        fields = _multipart(request)
        name, content_type, content = fields.get("file", (None, None, b""))
        if not name or content_type != "application/pdf":
            return _answer(400, {"code": "validation_error", "message": "A PDF file is required."})
        notes = fields["notes"][2].decode("utf-8") if "notes" in fields else ""
        millis = FIRST_MILLIS + len(self.files)
        found = file_id(name, millis, self.account_id)
        self.files[found] = {"filename": name, "notes": notes, "sha256": hashlib.sha256(content).hexdigest(),
                             "size": len(content)}
        return httpx.Response(201, json={"created": millis // 1000, "file_id": found, "filename": name, "notes": notes,
                                         "object_type": "file", "size": len(content)}, headers=FILES_HEADERS)

    def _accept(self, request: httpx.Request, id: str) -> httpx.Response:
        dispute, body, refused = self._respondable(request, id)
        if refused is not None:
            return refused
        reason = body.get("reason")
        if reason not in ACCEPT_REASONS:
            return _answer(400, {"code": "validation_error", "message": f"Invalid accept_reason {reason}"})
        attempt = self.sandbox.payments[dispute["payment_intent_id"]].setdefault("latest_payment_attempt", {})
        refunded = money(attempt.get("refunded_amount") or 0)
        refund = body.get("refund") if isinstance(body.get("refund"), dict) else None
        if refund is not None and "amount" in refund:
            amount = money(refund["amount"])
        elif dispute["stage"] == "RFI":  # left out at RFI, the sandbox refunded all it could (fixtures/spike/071)
            amount = money(attempt.get("captured_amount") or 0) - refunded
        else:
            amount = Decimal(0)
        shown: dict[str, Any] = {}
        if amount > 0:
            attempt["refunded_amount"] = json_number(refunded + amount)
            dispute["refunds"] = [*(dispute.get("refunds") or []), {"id": f"rfd_{id}_{len(self.request_ids)}"}]
            shown = {"amount": json_number(amount)}
            if refund is not None and "reason" in refund:
                shown["reason"] = refund["reason"]
        self.sandbox.reply(id, Action.ACCEPT, body.get("accepted_by", ""), reason, refund=shown, accepted_at=self._now())
        self.sandbox.change(id, status="ACCEPTED")
        self._applied(body)
        return _answer(200, copy.deepcopy(dispute))

    def _challenge(self, request: httpx.Request, id: str) -> httpx.Response:
        dispute, body, refused = self._respondable(request, id)
        if refused is not None:
            return refused
        documents = (body.get("supporting_documents") or {}).get("documents") or []
        sent = [given for entry in documents if isinstance(entry, dict) for given in entry.get("file_ids") or []]
        if any(given not in self.files for given in sent):
            return _answer(400, {"code": "validation_error", "message": "A file id in the request isn't known."})
        reason = body.get("reason")
        generated = file_id(f"Evidence_Provided_Form{len(self.files)}{len(self.request_ids)}.pdf",
                            FIRST_MILLIS + 500_000 + len(self.request_ids), self.account_id)
        extra: dict[str, Any] = {
            "challenged_at": self._now(), "seller_info": dict(PROFILE), "customer_info": {}, "delivery_info": {},
            "order_info": self._order_info(self.sandbox.payments.get(dispute["payment_intent_id"]) or {}),
            "supporting_documents": {"generated_files": [generated], "other_documents": sent,
                                     "documents": [*copy.deepcopy(documents),
                                                   *([{"type": "OTHER", "file_ids": sent[:1]}] if sent else [])]}}
        if "product_type" in body:
            extra["product_type"] = body["product_type"]
        self.sandbox.reply(id, Action.CHALLENGE, body.get("challenged_by", ""),
                           reason if reason in CHALLENGE_REASONS else "OTHER_REASONS", **extra)
        answer = {**copy.deepcopy(dispute), "status": "CHALLENGED"}  # the POST's own answer: the same stage, challenged
        # The next GET shows the bank deciding, as the sandbox's did after the chargeback challenge.
        self.sandbox.change(id, **({"stage": "PRE_ARBITRATION", "status": "PENDING_DECISION"}
                                   if dispute["stage"] == "CHARGEBACK" else {"status": "CHALLENGED"}))
        self._applied(body)
        return _answer(200, answer)

    def _respondable(self, request: httpx.Request, id: str) -> tuple[dict, dict, httpx.Response | None]:
        dispute = self.sandbox.disputes.get(id)
        if dispute is None:
            return {}, {}, _answer(404, _error(404))
        try:
            body = json.loads(request.content)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return dispute, {}, _answer(400, {"code": "validation_error", "message": "The body isn't a JSON object."})
        if body.get("request_id") in self.request_ids:
            return dispute, body, _answer(400, {"code": "duplicate_request", "message": "The request_id was used before."})
        if dispute["status"] != "REQUIRES_RESPONSE":
            return dispute, body, _answer(400, {"code": "validation_error", "message": NOT_SUPPORTED})
        return dispute, body, None

    def _applied(self, body: dict) -> None:
        if isinstance(body.get("request_id"), str):
            self.request_ids.add(body["request_id"])

    @staticmethod
    def _order_info(payment: dict) -> dict[str, Any]:
        """What the sandbox filled from the payment on a challenge with no order_info: the merchant order id, its
        currency and when it was made. A test payment of the Sandbox has no merchant order id, so one is made up."""
        number = (payment.get("metadata") or {}).get("order_no")
        return {"id": payment.get("merchant_order_id") or f"FH-{number}-fake", "total_currency": payment.get("currency"),
                "created_at": payment.get("created_at")}

    # ------------------------------------------------------------------------- the bank's side
    def _create_payment(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("request_id") in self.request_ids:
            return _answer(400, {"code": "duplicate_request", "message": "The request_id was used before."})
        self._made += 1
        payment_id = f"int_fake{self._made:04d}"
        payment = {"id": payment_id, "request_id": body.get("request_id"), "amount": body["amount"],
                   "currency": body.get("currency", "USD"), "merchant_order_id": body.get("merchant_order_id"),
                   "descriptor": body.get("descriptor"), "metadata": body.get("metadata") or {},
                   "status": "REQUIRES_PAYMENT_METHOD", "captured_amount": 0, "created_at": self._now(),
                   "client_secret": f"fake-client-secret-{self._made:04d}"}
        self.sandbox.payments[payment_id] = payment
        self._applied(body)
        return _answer(201, copy.deepcopy(payment))

    def _open_dispute(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payment = self.sandbox.payments.get(body.get("payment_intent_id"))
        if payment is None:
            return _answer(404, _error(404))
        if body.get("stage") not in ("RFI", "CHARGEBACK"):
            return _answer(400, {"code": "validation_error", "message": "stage should be RFI or CHARGEBACK"})
        try:
            due = datetime.strptime(body["due_at"], "%Y-%m-%dT%H:%M:%S%z").isoformat()
        except (KeyError, TypeError, ValueError):
            return _answer(400, {"code": "validation_error", "message": "due_at isn't a date and time."})
        self._made += 1
        dispute_id = f"dst_fake{self._made:04d}"
        self.sandbox.disputes[dispute_id] = {
            "id": dispute_id, "stage": body["stage"], "status": "REQUIRES_RESPONSE", "amount": body.get("amount"),
            "currency": payment["currency"], "payment_intent_id": payment["id"],
            "merchant_order_id": payment.get("merchant_order_id"), "reason": {"original_code": body.get("reason_code")},
            "accept_details": [], "challenge_details": [], "due_at": due, "created_at": self._now(),
            "issuer_comment": body.get("comment") or "", "refunds": None}
        self.sandbox.change(dispute_id)
        return _answer(201, copy.deepcopy(self.sandbox.disputes[dispute_id]))

    def _escalate(self, request: httpx.Request, id: str) -> httpx.Response:
        dispute = self.sandbox.disputes.get(id)
        if dispute is None:
            return _answer(404, _error(404))
        body = json.loads(request.content)
        if (dispute["stage"], dispute["status"]) not in (("RFI", "CHALLENGED"), ("RFI", "PENDING_DECISION")):
            return _answer(400, {"code": "validation_error", "message": NOT_SUPPORTED})
        due = datetime.strptime(body["due_at"], "%Y-%m-%dT%H:%M:%S%z").isoformat()
        self.sandbox.change(id, stage="CHARGEBACK", status="REQUIRES_RESPONSE", due_at=due,
                            issuer_comment=body.get("comment") or "")
        return _answer(200, copy.deepcopy(dispute))

    def _resolve(self, request: httpx.Request, id: str) -> httpx.Response:
        dispute = self.sandbox.disputes.get(id)
        if dispute is None:
            return _answer(404, _error(404))
        outcome = {"MERCHANT": "WON", "CUSTOMER": "LOST"}.get(json.loads(request.content).get("in_favor_of"))
        if outcome is None or dispute["stage"] not in ("CHARGEBACK", "PRE_ARBITRATION") \
                or dispute["status"] not in ("CHALLENGED", "PENDING_DECISION"):
            return _answer(400, {"code": "validation_error", "message": NOT_SUPPORTED})
        self.sandbox.change(id, status=outcome)
        return _answer(200, copy.deepcopy(dispute))

    def _now(self) -> str:
        return (RUN_DATE + timedelta(seconds=self.sandbox.ticks)).isoformat()


def _answer(status: int, data: Any) -> httpx.Response:
    return httpx.Response(status, json=data)


def _error(status: int) -> dict[str, str]:
    return {"code": CODES.get(status, "internal_error"), "message": f"The fake sandbox answered {status}."}


def _multipart(request: httpx.Request) -> dict[str, tuple[str | None, str | None, bytes]]:
    """name -> (file name, content type, content) of each part of a multipart form."""
    found = re.search(r"boundary=([^;\s]+)", request.headers.get("content-type", ""))
    if found is None:
        return {}
    fields = {}
    for part in request.content.split(b"--" + found.group(1).strip('"').encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, content = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        content = content.removesuffix(b"\r\n")
        name = re.search(rb';\s*name="([^"]*)"', head)
        filename = re.search(rb';\s*filename="([^"]*)"', head)
        kind = re.search(rb"(?im)^content-type:\s*([^\s;]+)", head)
        if name is not None:
            fields[name.group(1).decode()] = (filename.group(1).decode() if filename else None,
                                              kind.group(1).decode() if kind else None, content)
    return fields
