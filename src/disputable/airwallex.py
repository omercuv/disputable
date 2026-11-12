"""The Airwallex client: one Session on the sandbox's REST API, the AgentClient the agent may call, the exact bodies a
plan sends, and the map from an answer to what the state machine does next.

The Session logs in when it needs to and keeps the token in memory for 25 minutes. It leaves 0.25 s between calls, logs
in again once on a 401 and sends the same request once more, and retries a GET once after a 429, a 408, a 5xx or a lost
answer. It never retries a POST: the state machine's rule is to read the dispute again first, which only the next pass
does. It talks to the sandbox only, takes nothing from the process environment, and never raises an httpx error: a call
that got no answer is an Answer with no status. The AgentClient reads disputes and payments, uploads a plan's files and
sends the saved response. It has no path to the issuer's side of the sandbox, and every id it puts in a web address is
checked first.

A response sends exactly what the plan says: accept_body() and challenge_body(), checked by body_problem() before the
body is saved. There are no customer, delivery or order details, so nothing goes out that a person didn't approve.
Airwallex adds a summary file of its own to a challenge, and on the spike's RFI challenge it also filled the store's
profile from its own account and the order's number, currency and date from the payment (fixtures/spike/066, 073); the
approval screen says so. Amounts go out as exact JSON numbers (json_number()).

The send step uses it like this:
    body = accept_body(plan), or challenge_body(plan, [(document, file_id), ...]) after the uploads
    problem = body_problem(plan, body, files): any problem, and nothing is saved or sent
    post = PostRequest(plan.request_id, dispute_id, round_number, respond_path(plan), body_text(body)), saved with sending()
    sent = client.respond(post, retry=True when a try may already be with Airwallex: one is on file, or the case is at SENT)
    TAKEN: lifecycle.taken(); FAILED: lifecycle.send_failed(record, request_id, refusal(sent.detail), at); RETRY: nothing
        changes, and the next pass reads again before the same bytes go out; after a 429, on the POST or on the login it
        needs, none before not_before(at, sent)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

import httpx

from disputable import privacy
from disputable.config import NO_AIRWALLEX_KEYS, NOT_SANDBOX, Settings, SetupError, is_sandbox_url
from disputable.documents import MAX_NAME, MAX_PDF_BYTES, Document
from disputable.engine import Action, Stage
from disputable.fees import ZERO, AcceptReason, ChallengeReason, money
from disputable.lifecycle import ACTOR, Plan

LOGIN = "/api/v1/authentication/login"
DISPUTES = "/api/v1/pa/payment_disputes"
PAYMENTS = "/api/v1/pa/payment_intents"
UPLOAD = "/api/v1/files/upload"  # on the files host
TOKEN_REUSE_SECONDS = 25 * 60  # a token lasts 30 minutes
MIN_INTERVAL = 0.25  # one call at a time, about 4 a second (the sandbox allows 10 a second per endpoint)
TIMEOUT = httpx.Timeout(30.0, connect=10.0)  # uploads too: a plan's file is at most 2 MiB
GET_RETRY_WAIT, MAX_RETRY_AFTER = 2.0, 30.0
LIST_SIZE = 100  # far more disputes than one payment can carry
ID_TEXT = r"[A-Za-z0-9_]{1,64}"
ID = re.compile(ID_TEXT)  # every id that goes into a web address
FILE_ID = re.compile(r"[A-Za-z0-9+/=_-]{1,1000}")  # base64 of '<account id>,|,<region>,|,<file name>'
PATH = re.compile(r"/api/v1(?:/[A-Za-z0-9_]+)+")
AGENT_GETS = re.compile(rf"{re.escape(DISPUTES)}(?:/{ID_TEXT})?|{re.escape(PAYMENTS)}/{ID_TEXT}")
AGENT_POSTS = re.compile(rf"{re.escape(DISPUTES)}/{ID_TEXT}/(?:accept|challenge)")
LABEL = re.compile(r"[A-Za-z0-9_:.-]{1,100}")
ACTORS = frozenset({"agent", "bank"})  # whose call a recording says it is
USER_AGENT = "disputable/0.1"
MAX_TEXT, MAX_DETAIL = 2000, 200

REFUND_REASON = "REQUESTED_BY_CUSTOMER"  # the sandbox took it on the ear tips' accept
CHALLENGE_METHOD = "STANDARD"  # sent on every real challenge of the spike, never AI automation
DOCUMENT_TYPE = "PRIMARY"  # supporting_documents.documents[].type
PRODUCT_TYPE = "PHYSICAL_GOODS"  # the store sells goods; "required when the stage is CHARGEBACK"
ACCEPT_KEYS = frozenset({"request_id", "reason", "accepted_by", "refund"})
CHALLENGE_KEYS = frozenset({"request_id", "reason", "challenged_by", "challenge_method", "product_type",
                            "supporting_documents"})
ACCEPT_REASONS = frozenset(reason.value for reason in AcceptReason)
CHALLENGE_REASONS = frozenset(reason.value for reason in ChallengeReason)  # the API takes any text, so code checks it

TIMEOUTS = frozenset({"ReadTimeout", "WriteTimeout", "PoolTimeout"})
NO_CONNECTION = frozenset({"ConnectError", "ConnectTimeout"})
TOOK = "Airwallex took it"
HAD_IT = "Airwallex already had this request"
WAIT = "Airwallex asked this app to wait"
NO_ANSWER = "no answer in 30 seconds"
CONNECTION_FAILED = "the connection failed"
CONNECTION_BROKE = "the connection broke before Airwallex answered"
UNREADABLE = "an answer this app can't read"
NOT_LOGGED_IN = "this app couldn't log in to Airwallex ({why})"
RETRY_REFUSED = "Airwallex refused the retry: "
MAY_HAVE_LANDED = "The first try may have landed"
WAS_A_RETRY = "That was a retry, and the first try may have landed."  # refusal()'s words for it, after Airwallex's
CHECK_AIRWALLEX = "A person checks what Airwallex has."
A_FILE_ID = "(a file id)"  # what an Airwallex message shows in place of one, so no account id is kept or shown
AN_ID = "(an id)"  # in place of a UUID written plainly, such as an account id or a request id

BAD_ID = "Airwallex sent an id this app won't put in a web address, so it wasn't used."
NOT_THE_AGENTS = "The agent's client only reads disputes and payments, uploads files and responds to disputes."
NOT_AN_ACTOR = "A session is the agent's or the bank's."
TOO_BIG = f"The file is over the {MAX_PDF_BYTES:,} bytes Visa takes, so it isn't uploaded."
BOUNDARY_IN_FILE = "The file holds its own upload boundary, so it can't be sent as it is."
NOT_A_POST = "This isn't a request this app sends: {why}."

# body_problem()'s phrases. The send step puts one in "this app couldn't build the request ({problem})".
NOT_A_RESPONSE = "the plan neither accepts nor challenges"
DISPUTE_ID = "the dispute id isn't one this app puts in a web address"
REQUEST_ID = "the request id isn't the plan's"
EXTRA_FIELD = "the request holds a field the plan doesn't send"
REASON = "the reason isn't the plan's, or isn't one Airwallex lists for this response"
NOT_THIS_APP = "the request doesn't name this app as the one responding"
REFUND = "the refund isn't exactly the plan's"
REFUND_STAGE = "a refund goes out only with an accept at RFI"
ACCEPT_FILES = "an accept sends no file"
METHOD = "the challenge method isn't STANDARD"
PRODUCT = "the product type goes with a challenge at CHARGEBACK, and only there"
NOTES = f"a file's notes are over {MAX_NAME} characters"
FILES = "the files aren't exactly the plan's"
DOCUMENTS = "the supporting documents aren't one PRIMARY entry with one file id for each file"
NOT_EXACT = "the request isn't exactly what the plan sends"

_HALF_EMOJI = re.compile("[\ud800-\udfff]")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_=-]{48,}")


class BadId(ValueError):
    """An id that won't go into a web address. Nothing was sent."""


class Outcome(Enum):
    TAKEN = "taken"  # lifecycle.taken()
    FAILED = "failed"  # lifecycle.send_failed()
    RETRY = "retry"  # nothing changes: the next pass reads again, then sends the same bytes


@dataclass(frozen=True)
class Credentials:
    """The sandbox keys and addresses. The keys never show in its repr, so a traceback or a log line can't print them."""
    client_id: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str  # https://api.sandbox.airwallex.com
    files_url: str  # https://files.sandbox.airwallex.com


def credentials(settings: Settings) -> Credentials:
    """The Airwallex part of the settings. SetupError when the keys aren't set."""
    if not (settings.airwallex_client_id and settings.airwallex_api_key):
        raise SetupError(NO_AIRWALLEX_KEYS)
    return Credentials(settings.airwallex_client_id, settings.airwallex_api_key, settings.base_url, settings.files_url)


@dataclass(frozen=True)
class Answer:
    """What one call got back. It never holds the request."""
    status: int | None  # None: no HTTP answer (a timeout, the connection)
    data: Any  # the parsed JSON, or None
    text: str  # the body when it isn't JSON, cut to 2,000 characters
    error: str  # "" or the transport error's class: ReadTimeout, ConnectError, ...
    elapsed_ms: int
    retry_after: float | None  # a 429's Retry-After in seconds, at most 30; None when it gave none this app can read
    login_failed: bool = False  # the request never went out: this app couldn't log in first

    @property
    def ok(self) -> bool:
        """A 2xx."""
        return self.status is not None and 200 <= self.status < 300 and not self.login_failed

    @property
    def code(self) -> str:
        """The error body's "code", or "" on success."""
        return str(self.data.get("code") or "") if isinstance(self.data, dict) and not self.ok else ""

    @property
    def message(self) -> str:
        """The error body's "message", or "" on success."""
        return str(self.data.get("message") or "") if isinstance(self.data, dict) and not self.ok else ""


@dataclass(frozen=True)
class Uploaded:
    file_id: str | None  # from "file_id", else "id"; None when the file didn't upload
    pdf_sha256: str  # of the bytes sent
    size: int
    answer: Answer


@dataclass(frozen=True)
class Sent:
    """What a POST try's answer means for the state machine."""
    outcome: Outcome
    status: int | None
    detail: str  # one plain phrase with no full stop, at most 200 characters
    retry_after: float | None  # after any 429, on the POST or its login: the seconds before the next try may go. Else None


@dataclass(frozen=True)
class PostRequest:
    """The exact request a plan sends, saved before its first try: every try sends these bytes, with the same id."""
    request_id: str  # plan.request_id
    dispute_id: str
    round_number: int
    path: str  # respond_path(plan)
    body: str  # body_text(body): sorted keys, no spaces, ASCII

    def __post_init__(self) -> None:
        if not (isinstance(self.request_id, str) and self.request_id):
            raise ValueError(NOT_A_POST.format(why="it has no request id"))
        if not (isinstance(self.dispute_id, str) and ID.fullmatch(self.dispute_id)):
            raise ValueError(NOT_A_POST.format(why="its dispute id won't go into a web address"))
        if type(self.round_number) is not int or self.round_number < 1:
            raise ValueError(NOT_A_POST.format(why="it doesn't name a round"))
        if self.path not in (f"{DISPUTES}/{self.dispute_id}/accept", f"{DISPUTES}/{self.dispute_id}/challenge"):
            raise ValueError(NOT_A_POST.format(why="its path isn't this dispute's accept or challenge"))
        try:
            data = json.loads(self.body)
        except (TypeError, ValueError, RecursionError):
            raise ValueError(NOT_A_POST.format(why="its body isn't JSON")) from None
        if not isinstance(data, dict) or body_text(data) != self.body or data.get("request_id") != self.request_id:
            raise ValueError(NOT_A_POST.format(why="its body isn't saved as this app writes one, with its request id"))


# ----------------------------------------------------------------------------- the session
class Session:
    """One login and one connection pool on the sandbox, for the agent or for the bank script, one call at a time."""

    def __init__(self, credentials: Credentials, *, actor: str, transport: httpx.BaseTransport | None = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        for name, url in (("AWX_BASE_URL", credentials.base_url), ("AWX_FILES_URL", credentials.files_url)):
            if not is_sandbox_url(url):
                raise SetupError(NOT_SANDBOX.format(name=name))
        if not all(isinstance(key, str) and key for key in (credentials.client_id, credentials.api_key)):
            raise SetupError(NO_AIRWALLEX_KEYS)
        if actor not in ACTORS:
            raise ValueError(NOT_AN_ACTOR)
        self.actor = actor
        self.logins = 0  # how many times it logged in
        self._credentials = credentials
        self._base, self._files = credentials.base_url.rstrip("/"), credentials.files_url.rstrip("/")
        # trust_env=False: no proxy, certificate or .netrc setting comes from the environment, as no key does.
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT, trust_env=False, follow_redirects=False,
                                    headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        self._monotonic, self._sleep = monotonic, sleep
        self._token: str | None = None
        self._token_at = 0.0
        self._last_call: float | None = None

    def get(self, path: str, *, params: Mapping[str, str | int] | None = None, label: str) -> Answer:
        """A GET on the API host, retried once after a 429, a 408, a 5xx or a lost answer."""
        self._check(path, AGENT_GETS)
        return self._call("GET", self._base + path, label=label, retry=True, params=dict(params) if params else None)

    def post_json(self, path: str, body: str, *, label: str) -> Answer:
        """A POST of these exact bytes on the API host. Never retried here, but a 401 logs in again and sends it once more:
        a 401 means it wasn't processed."""
        self._check(path, AGENT_POSTS)
        if not isinstance(body, str):
            raise TypeError("The body is the saved JSON text, sent as it is.")
        return self._call("POST", self._base + path, label=label, content=body.encode("utf-8"),
                          headers={"Content-Type": "application/json"})

    def post_file(self, *, filename: str, content: bytes, notes: str, boundary: str, label: str) -> Answer:
        """A file upload on the files host, as multipart form data with this boundary, so the same file gives the same
        bytes on any machine. It carries no request id, so it's never retried here."""
        self._check(UPLOAD, re.compile(re.escape(UPLOAD)))
        return self._call("POST", self._files + UPLOAD, label=label,
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                          files={"file": (filename, content, "application/pdf")}, data={"notes": notes})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _check(self, path: str, agent_may: re.Pattern[str]) -> None:
        if not (isinstance(path, str) and PATH.fullmatch(path)):
            raise ValueError("A path is /api/v1/ and plain names, with no query in it.")
        if self.actor == "agent" and not agent_may.fullmatch(path):
            raise ValueError(NOT_THE_AGENTS)

    def _call(self, method: str, url: str, *, label: str, retry: bool = False, **request: Any) -> Answer:
        if not (isinstance(label, str) and LABEL.fullmatch(label)):
            raise ValueError("A label is a short name of letters, digits and _ : . -")
        headers = request.pop("headers", {})
        relogged = retried = False
        while True:
            token = self._login()
            if isinstance(token, Answer):
                return token  # the request never went out
            answer = self._attempt(method, url, label=label, headers={**headers, "Authorization": f"Bearer {token}"},
                                   **request)
            if answer.status == 401 and not relogged:
                relogged, self._token = True, None
                continue
            if retry and not retried and _worth_a_retry(answer):
                retried = True
                self._sleep(_wait(answer))
                continue
            return answer

    def _login(self) -> str | Answer:
        """The token, logging in when there is none or it is 25 minutes old. A failed login is its own answer."""
        if self._token is not None and self._monotonic() - self._token_at < TOKEN_REUSE_SECONDS:
            return self._token
        self._token = None
        headers = {"x-client-id": self._credentials.client_id, "x-api-key": self._credentials.api_key}
        answer = self._attempt("POST", self._base + LOGIN, label="login", headers=headers)
        if _worth_a_retry(answer):  # a login changes nothing, so a second try is safe
            self._sleep(_wait(answer))
            answer = self._attempt("POST", self._base + LOGIN, label="login", headers=headers)
        token = answer.data.get("token") if answer.ok and isinstance(answer.data, dict) else None
        if not (isinstance(token, str) and token):
            return replace(answer, login_failed=True)
        self._token, self._token_at = token, self._monotonic()
        self.logins += 1
        return token

    def _attempt(self, method: str, url: str, *, label: str, headers: Mapping[str, str], **request: Any) -> Answer:
        self._throttle()
        started = self._monotonic()
        extensions = {"disputable": {"actor": self.actor, "label": label}}  # for the recording, never a header
        try:
            response = self._client.request(method, url, headers=headers, extensions=extensions, **request)
        except httpx.HTTPError as error:
            return Answer(None, None, "", type(error).__name__, _ms(self._monotonic() - started), None)
        return _answer(response, _ms(self._monotonic() - started))

    def _throttle(self) -> None:
        if self._last_call is not None:
            wait = MIN_INTERVAL - (self._monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._monotonic()


def _answer(response: httpx.Response, elapsed_ms: int) -> Answer:
    data, text = None, ""
    if response.content:
        try:
            data = response.json()
        except (ValueError, RecursionError):  # not JSON, or not text
            text = response.text[:MAX_TEXT]
    headers = response.headers
    retry_after = _retry_after(headers.get("retry-after"), headers.get("date")) if response.status_code == 429 else None
    return Answer(response.status_code, data, text, "", elapsed_ms, retry_after)


def _worth_a_retry(answer: Answer) -> bool:
    return answer.status is None or answer.status in (408, 429) or answer.status >= 500


def _wait(answer: Answer) -> float:
    if answer.status == 429 and answer.retry_after is not None:
        return min(answer.retry_after, MAX_RETRY_AFTER)
    return GET_RETRY_WAIT


def _retry_after(value: str | None, date: str | None) -> float | None:
    """A 429's Retry-After in seconds, at most 30: as seconds, or as an HTTP date read against the answer's own Date, where
    a date already past is 0. None when there is none, or none this app can read."""
    text = (value or "").strip()
    try:
        seconds: float | None = float(text)
    except ValueError:  # an HTTP date, or nothing
        seconds = _seconds_until(text, date)
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


def _seconds_until(text: str, date: str | None) -> float | None:
    """How long after the answer's Date the HTTP date is, and never less than 0. None when either won't parse: a date
    alone can't say how long to wait, since this computer's clock may differ from Airwallex's."""
    try:
        moments = [parsedate_to_datetime(found) for found in (text, date or "")]
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    then, now = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc) for moment in moments)
    return max((then - now).total_seconds(), 0.0)


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


# ----------------------------------------------------------------------------- what the agent may call
class AgentClient:
    """Reads disputes and payments, uploads a plan's files and sends the saved response, and nothing else. Only the bank
    script plays the issuer, with a session of its own."""

    def __init__(self, session: Session) -> None:
        if session.actor != "agent":
            raise ValueError("The agent's client runs on the agent's own session.")
        self._session = session

    def disputes_on(self, payment_id: str) -> Answer:
        """GET the disputes on one payment: {"items": [...]}, with no paging keys."""
        _safe(payment_id)
        return self._session.get(DISPUTES, params={"payment_intent_id": payment_id, "size": LIST_SIZE},
                                 label=f"list-{payment_id}")

    def dispute(self, dispute_id: str) -> Answer:
        _safe(dispute_id)
        return self._session.get(f"{DISPUTES}/{dispute_id}", label=f"dispute-{dispute_id}")

    def payment(self, payment_id: str) -> Answer:
        _safe(payment_id)
        return self._session.get(f"{PAYMENTS}/{payment_id}", label=f"payment-{payment_id}")

    def upload(self, document: Document) -> Uploaded:
        """The document's PDF on the files host, named by its hash with its own notes. Airwallex writes the name into the
        file id, so it holds nothing but the hash. ValueError for a file over 2 MiB or one that holds its boundary."""
        content = document.pdf()
        if len(content) > MAX_PDF_BYTES:
            raise ValueError(TOO_BIG)
        boundary = boundary_for(document)
        if boundary.encode("ascii") in content:
            raise ValueError(BOUNDARY_IN_FILE)
        answer = self._session.post_file(filename=document.filename, content=content, notes=document.notes,
                                         boundary=boundary, label=f"upload-{document.sha256[:16]}")
        return Uploaded(_file_id(answer), hashlib.sha256(content).hexdigest(), len(content), answer)

    def respond(self, post: PostRequest, *, retry: bool) -> Sent:
        """POST the saved body as it is. retry: a try may already be with Airwallex for this request id, since one is on
        file or the case was saved at SENT, whose answer may have been lost with the process."""
        if not isinstance(post, PostRequest):
            raise TypeError("respond() sends a saved PostRequest.")
        action = post.path.rsplit("/", 1)[-1]
        return outcome_of(self._session.post_json(post.path, post.body, label=f"{action}-{post.dispute_id}"), retry=retry)


def safe_id(value: object) -> bool:
    """Whether an id may go into a web address: letters, digits and _, at most 64."""
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _safe(value: object) -> None:
    if not safe_id(value):
        raise BadId(BAD_ID)


def boundary_for(document: Document) -> str:
    return f"disputable-{document.sha256[:32]}"


def _file_id(answer: Answer) -> str | None:
    if not answer.ok or not isinstance(answer.data, dict):
        return None
    value = answer.data.get("file_id") or answer.data.get("id")
    return value if isinstance(value, str) and FILE_ID.fullmatch(value) else None


# ----------------------------------------------------------------------------- answers to outcomes
def outcome_of(answer: Answer, *, retry: bool) -> Sent:
    """What a POST's answer means. retry: a try may already be with Airwallex for this request id (one is on file, or
    the case was saved at SENT), so a refusal may follow a first try that landed. A failed login is RETRY, since the
    POST never went out. Every other 4xx but 429 is FAILED, 408 too, since only a 2xx or duplicate_request says the
    request landed; anything else is RETRY. A 429 always sets a wait, on the POST or on the login it needs: its
    Retry-After, at most 30 seconds, or 30 seconds when it gave none this app can read."""
    status = answer.status
    if answer.login_failed:  # the POST never went out
        return Sent(Outcome.RETRY, status, problem(answer), _asked_to_wait(answer))
    if answer.ok:
        return Sent(Outcome.TAKEN, status, TOOK, None)
    if answer.code.startswith("duplicate_request"):  # at any status: the state machine's rule, until the preflight
        return Sent(Outcome.TAKEN, status, HAD_IT, None)
    if status == 429:
        return Sent(Outcome.RETRY, status, WAIT, _asked_to_wait(answer))
    if status is not None and 400 <= status < 500:
        why = problem(answer)
        if retry:
            room = MAX_DETAIL - len(RETRY_REFUSED) - len(". ") - len(MAY_HAVE_LANDED)
            why = f"{RETRY_REFUSED}{_cut(why, room)}. {MAY_HAVE_LANDED}"
        return Sent(Outcome.FAILED, status, why, None)
    return Sent(Outcome.RETRY, status, problem(answer), None)


def _asked_to_wait(answer: Answer) -> float | None:
    """After a 429, the seconds before the next try may go: its Retry-After, at most 30, or 30 when it gave none this
    app can read. None after any other answer."""
    if answer.status != 429:
        return None
    return MAX_RETRY_AFTER if answer.retry_after is None else min(answer.retry_after, MAX_RETRY_AFTER)


def problem(answer: Answer) -> str:
    """One plain phrase with no full stop for an answer that isn't a usable 2xx, at most 200 characters: "no answer in
    30 seconds", "the connection failed", "HTTP 503", "Airwallex asked this app to wait", "HTTP 400, validation_error:
    Dispute transition is not supported". Airwallex's own words are cut to one line. A file id or a UUID in them (an
    account id among them) shows as "(a file id)" or "(an id)"; other ids, such as dispute and payment ids, stay as they
    came."""
    status = answer.status
    if status is None:
        why = NO_ANSWER if answer.error in TIMEOUTS else CONNECTION_FAILED if answer.error in NO_CONNECTION \
            else CONNECTION_BROKE
    elif 200 <= status < 300:
        why = UNREADABLE
    elif status == 429:
        why = WAIT
    elif 400 <= status < 500 and isinstance(answer.data, dict):
        code, message = _one_line(str(answer.data.get("code") or "")), _one_line(str(answer.data.get("message") or ""))
        why = f"HTTP {status}" + (f", {code}" if code else "") + (f": {message}" if message else "")
    else:
        why = f"HTTP {status}"
    return _cut(NOT_LOGGED_IN.format(why=why) if answer.login_failed else why, MAX_DETAIL)


def refusal(detail: str) -> str:
    """The error lifecycle.send_failed() takes for a FAILED try, from the try's saved detail: Airwallex's words and a
    full stop. After a refused retry, Airwallex's words, then that it was a retry and the first try may have landed, and
    "A person checks what Airwallex has.", so the person line, which starts "Airwallex refused the response", says
    "refused" once. The try keeps its detail as it was saved."""
    if detail.startswith(RETRY_REFUSED):
        words = detail.removeprefix(RETRY_REFUSED).removesuffix(f". {MAY_HAVE_LANDED}")
        return f"{words}. {WAS_A_RETRY} {CHECK_AIRWALLEX}"
    return f"{detail}."


def not_before(at: datetime, sent: Sent) -> datetime | None:
    """When the next try of the request may go, after Airwallex asked this app to wait: no try before it. Else None."""
    if sent.outcome is not Outcome.RETRY or sent.retry_after is None:
        return None
    return at + timedelta(seconds=sent.retry_after)


def _one_line(text: str) -> str:
    """Airwallex's words as one plain line: no control characters, no half emoji, no file id, no UUID (an account id
    written plainly would reach the pages), no full stop at the end."""
    text = _HALF_EMOJI.sub("\ufffd", text)
    text = "".join(" " if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") else char for char in text)
    text = _BASE64_RUN.sub(_hide_file_id, text)
    text = privacy.UUID.sub(AN_ID, text)
    return " ".join(text.split()).rstrip(" .")


def _hide_file_id(found: re.Match[str]) -> str:
    run = found.group()
    decoded = (privacy.decode_base64(token) or "" for token in privacy.base64_tokens(run))
    return A_FILE_ID if any(privacy.SEPARATOR in text for text in decoded) else run


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip(" .,;:-")


# ----------------------------------------------------------------------------- the bodies, exactly as the plan says
def respond_path(plan: Plan) -> str:
    """f"{DISPUTES}/{dispute_id}/accept" or ".../challenge". BadId for an id that won't go into a web address."""
    terms = plan.terms
    _safe(terms.dispute_id)
    action = {Action.ACCEPT: "accept", Action.CHALLENGE: "challenge"}.get(terms.action)
    if action is None:
        raise ValueError("Only an accept or a challenge is sent.")
    return f"{DISPUTES}/{terms.dispute_id}/{action}"


def accept_body(plan: Plan) -> dict[str, Any]:
    """The request id, the plan's reason and this app as the one accepting. A refund object only when the plan refunds
    more than nothing: left out at RFI, Airwallex refunds all it can. No description."""
    terms = plan.terms
    if terms.action is not Action.ACCEPT:
        raise ValueError("accept_body() builds an accept, and this plan doesn't accept.")
    body: dict[str, Any] = {"request_id": plan.request_id, "reason": terms.reason, "accepted_by": ACTOR}
    if terms.refund_amount > ZERO:
        body["refund"] = {"amount": json_number(terms.refund_amount), "reason": REFUND_REASON}
    return body


def challenge_body(plan: Plan, files: Sequence[tuple[Document, str]]) -> dict[str, Any]:
    """The request id, the plan's reason, this app as the one challenging, the STANDARD method and one PRIMARY document
    entry per file, in the order of the files' text hashes, so the same files always give the same bytes. product_type
    only at CHARGEBACK. No evidence object and no customer, delivery or order details: nothing a person didn't approve."""
    terms = plan.terms
    if terms.action is not Action.CHALLENGE:
        raise ValueError("challenge_body() builds a challenge, and this plan doesn't challenge.")
    body: dict[str, Any] = {"request_id": plan.request_id, "reason": terms.reason, "challenged_by": ACTOR,
                            "challenge_method": CHALLENGE_METHOD, "supporting_documents": {"documents": _entries(files)}}
    if terms.stage is Stage.CHARGEBACK:
        body["product_type"] = PRODUCT_TYPE
    return body


def _entries(files: Sequence[tuple[Document, str]]) -> list[dict[str, Any]]:
    return [{"type": DOCUMENT_TYPE, "file_ids": [file_id], "description": document.description}
            for document, file_id in sorted(files, key=lambda pair: (pair[0].sha256, pair[1]))]


def body_text(body: Mapping[str, Any]) -> str:
    """The bytes every try sends: sorted keys, no spaces, ASCII."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def json_number(amount: Decimal) -> int | float:
    """An amount as an exact JSON number: a whole one as an int (12), any other as the float whose shortest text is the
    amount (15.71). ValueError unless that is exact, so money never goes out rounded."""
    if not isinstance(amount, Decimal):
        raise TypeError("An amount is a Decimal, so it is exact before it goes out.")
    if not amount.is_finite():
        raise ValueError("An amount must be a finite number.")
    if amount == amount.to_integral_value():
        return int(amount)
    number = float(amount)
    if Decimal(repr(number)) != amount:
        raise ValueError(f"{amount} can't go out as an exact JSON number.")
    return number


def body_problem(plan: Plan, body: Mapping[str, Any], files: Sequence[tuple[Document, str]]) -> str | None:
    """The last guard before a body is saved: a plain phrase saying what's wrong, or None. The request id and the action
    are the plan's, and its dispute id can go into a web address. The reason is the plan's and one Airwallex lists (it
    stores an unknown challenge reason as OTHER_REASONS with a 200, so code checks it, not the API). This app is the one
    responding. A refund object is there exactly when the plan refunds, only at RFI, with the plan's exact amount. A
    challenge's files: one PRIMARY entry and one file id each, and their text hashes are exactly the plan's, with notes
    of at most 50 characters. product_type exactly at CHARGEBACK, and no field beyond these."""
    terms = plan.terms
    if terms.action not in (Action.ACCEPT, Action.CHALLENGE):
        return NOT_A_RESPONSE
    if not safe_id(terms.dispute_id):
        return DISPUTE_ID
    if not isinstance(body, Mapping):
        return NOT_EXACT
    accept = terms.action is Action.ACCEPT
    if set(body) - (ACCEPT_KEYS if accept else CHALLENGE_KEYS):
        return EXTRA_FIELD
    if body.get("request_id") != plan.request_id:
        return REQUEST_ID
    if body.get("reason") != terms.reason or terms.reason not in (ACCEPT_REASONS if accept else CHALLENGE_REASONS):
        return REASON
    if body.get("accepted_by" if accept else "challenged_by") != ACTOR:
        return NOT_THIS_APP
    if accept:
        problem_found = _accept_problem(terms, body, files)
    else:
        problem_found = _challenge_problem(terms, body, files)
    if problem_found:
        return problem_found
    try:
        exact = body_text(body) == body_text(accept_body(plan) if accept else challenge_body(plan, files))
    except (TypeError, ValueError):
        exact = False
    return None if exact else NOT_EXACT


def _accept_problem(terms: Any, body: Mapping[str, Any], files: Sequence[tuple[Document, str]]) -> str | None:
    if files:
        return ACCEPT_FILES
    if terms.refund_amount > ZERO:
        if terms.stage is not Stage.RFI:
            return REFUND_STAGE
        refund = body.get("refund")
        if not (isinstance(refund, Mapping) and set(refund) == {"amount", "reason"}
                and refund["reason"] == REFUND_REASON and _same_amount(refund["amount"], terms.refund_amount)):
            return REFUND
    elif "refund" in body:
        return REFUND
    return None


def _challenge_problem(terms: Any, body: Mapping[str, Any], files: Sequence[tuple[Document, str]]) -> str | None:
    if body.get("challenge_method") != CHALLENGE_METHOD:
        return METHOD
    if terms.stage is Stage.CHARGEBACK:
        if body.get("product_type") != PRODUCT_TYPE:
            return PRODUCT
    elif "product_type" in body:
        return PRODUCT
    if any(len(document.notes) > MAX_NAME for document, _ in files):
        return NOTES
    hashes = [document.sha256 for document, _ in files]
    if len(hashes) != len(set(hashes)) or set(hashes) != set(terms.file_hashes):
        return FILES
    ids = [file_id for _, file_id in files]
    if not all(isinstance(file_id, str) and FILE_ID.fullmatch(file_id) for file_id in ids) or len(set(ids)) != len(ids):
        return DOCUMENTS
    if body.get("supporting_documents") != {"documents": _entries(files)}:
        return DOCUMENTS
    return None


def _same_amount(value: Any, amount: Decimal) -> bool:
    if type(value) not in (int, float):  # never a bool or a string
        return False
    try:
        return money(value) == amount
    except ArithmeticError:
        return False
