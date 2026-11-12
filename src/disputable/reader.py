"""Claude's reading of one case: the read tools, the loop that asks Claude, and the checks on what it sends back.

Claude only reads. Each tool shows store records as tool_view() gives them, gated by the case's stage and capped in size,
and the loop ends at propose_decision. validate() then checks every quote against the one field it names and keeps a Row
for each item Claude sent, kept or dropped with a plain reason, so the eval sees everything. What reaches code is narrow:
the kept tags, a few checked kinds of point, and whether the read was complete. Claude's words never become a fact id, a
file or a reason. Pure code around one client call: NoReader and ReplayReader make no call at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

import anthropic
import httpx2  # the SDK's own HTTP library (anthropic depends on it), for claude_client's transport

from disputable import ce3, documents, prompts, rules
from disputable.engine import Dispute, Stage, refund_problem
from disputable.fees import FEES_V1, ZERO, AcceptReason, ChallengeReason, FeeTable, usd
from disputable.jsonable import Unloadable, dump, load
from disputable.prompts import BANK_COMMENT, EFFORT, FERNHILL, StoreProfile
from disputable.store import Case, Email, Order, Scan, Store, WarrantyRegistration, field_text, tool_view

FORMAT = 1
CHECKS_VERSION = 1  # bump when validation changes: rounds revalidates saved readings, with no API call
MAX_TURNS = 10
MAX_TOOL_CALLS = 40
MAX_NUDGES = 2
MAX_EARLY = 1
ATTEMPTS = 2
READ_SECONDS = 600  # a read starts no request after this. One request can take longer: the SDK's timeout and retries
MAX_QUOTE, MAX_NOTE = 300, 200
MIN_QUOTE = 4  # a shorter fragment proves nothing, unless it is a whole value
MAX_FACTS, MAX_FLAGS, MAX_CONFLICTS, MAX_TAGS = 40, 20, 10, 10
MAX_SENT = 400  # characters of what Claude sent, as a row keeps it
MAX_GIVEN = 200  # characters of a tool call's input, as the log keeps it
MAX_ECHO = 40  # characters of Claude's input that a refusal repeats
PROPOSE = "propose_decision"
READS = ("get_order_history", "get_carrier_scans", "search_inbox")
NO_CREDIT = "credit balance is too low"  # in the API's 400 for an account with no credit
_JSON = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": True}  # keys, digests and the proposal text


class Failure(Enum):
    NO_KEY = "NO_KEY"
    NOT_ALLOWED = "NOT_ALLOWED"
    ACCOUNT = "ACCOUNT"
    CAPPED = "CAPPED"  # this run hit a spend cap (its --cap, or the cap for one read) or the number of reads it was given
    BAD_REQUEST = "BAD_REQUEST"
    REFUSED = "REFUSED"
    CUT_OFF = "CUT_OFF"
    TOO_SLOW = "TOO_SLOW"
    NO_PROPOSAL = "NO_PROPOSAL"
    UNEXPECTED_STOP = "UNEXPECTED_STOP"
    MALFORMED = "MALFORMED"
    BUSY = "BUSY"
    NOT_RECORDED = "NOT_RECORDED"
    ERROR = "ERROR"


# The "{why}" in rounds' line: "Claude couldn't read this case's records ({why}), so a person reads them before ..."
WHY: Mapping[Failure, str] = MappingProxyType({
    Failure.NO_KEY: "this run doesn't use Claude",  # no key is set, or the run was told not to (--no-claude)
    Failure.NOT_ALLOWED: "the Claude API refused the key",
    Failure.ACCOUNT: "the Claude API account can't take more requests: a spend limit, billing or a hold",
    Failure.CAPPED: "this run hit a spend cap or the number of reads it was given",
    Failure.BAD_REQUEST: "the Claude API refused this app's request",
    Failure.REFUSED: "Claude declined to read it",
    Failure.CUT_OFF: "Claude's answer was cut off",
    Failure.TOO_SLOW: "Claude took too long to read it",
    Failure.NO_PROPOSAL: "Claude gave no answer",
    Failure.UNEXPECTED_STOP: "Claude stopped in a way this app doesn't expect",
    Failure.MALFORMED: "Claude's answer wasn't in the expected shape",
    Failure.BUSY: "Claude was busy each time this app asked",
    Failure.NOT_RECORDED: "no recorded reading matches this case",
    Failure.ERROR: "this app hit an error while Claude read",
})


class Kind(Enum):
    PRIOR_FRAUD_CLAIMED = "PRIOR_FRAUD_CLAIMED"
    CUSTOMER_WITHDREW = "CUSTOMER_WITHDREW"
    OTHER = "OTHER"


class ReaderBusy(Exception):
    """The Claude API was busy or out of reach (429, 5xx, 408, 409, a timeout or the connection) after the SDK's own
    retries, or the fallback model couldn't run. Nothing was read to the end, but the turns already made cost tokens, so
    the partial tool log and calls come with it for the round to log."""

    def __init__(self, why: str, log: Sequence[ToolCall] = (), calls: Sequence[Call] = ()) -> None:
        super().__init__(why)
        self.why, self.log, self.calls = why, tuple(log), tuple(calls)


@dataclass(frozen=True)
class Row:
    """One item Claude sent, kept or dropped. A kept row is shown with code's label, never Claude's wording."""
    part: str  # "fact", "conflict", "flag" or "tag"
    record_id: str  # as Claude sent it, cut to 400 characters, like field and quote
    field: str
    quote: str
    kept: bool
    why: str  # "" when kept, else a plain reason
    kind: str = ""  # conflicts only, as sent
    counted_as: str = ""  # conflicts only: "K1", "K2", "K5" or "U1"
    note: str = ""  # conflicts only: Claude's own sentence, at most 200 characters, never in a reason


@dataclass(frozen=True)
class Draft:
    """Claude's suggested response, shown collapsed beside code's plan with code's checks. Never passed to keep()."""
    action: str
    reason: str
    refund_amount: str  # raw, as sent: the eval looks for amounts taken from a planted line
    to_card_that_paid: bool
    note: str  # "" when dropped
    problems: tuple[str, ...]  # code's checks, in plain words


@dataclass(frozen=True)
class ToolCall:
    attempt: int
    turn: int
    name: str
    given: str  # the input as sorted JSON, cut to 200 characters
    refused: str  # "" when it ran and was shown
    record_ids: tuple[str, ...]  # what it showed
    asked: str = ""  # the order number or serial it asked for, cleaned as the tool reads it
    left_out: int = 0  # records the result left out past a cap: the read counts, but isn't complete


@dataclass(frozen=True)
class Iteration:
    """One entry of usage.iterations: each model attempt inside one request, the declined one included."""
    type: str  # "message", or "fallback_message" for the attempt that served after a decline
    model: str  # "" when the API names none
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int


@dataclass(frozen=True)
class Call:
    """One request to the API. Optional SDK numbers come in as 0, so every reading loads back. The counts are the
    serving attempt's. A turn that fell back is billed for every attempt in iterations, at each one's model's rates."""
    model: str  # message.model: the model that served the turn
    fell_back: bool
    stop_reason: str
    max_tokens: int
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    iterations: tuple[Iteration, ...] = ()  # usage.iterations, when the API sends it


@dataclass(frozen=True)
class Reading:
    format: int
    key: str  # reading_key(): what it was read from
    failure: Failure | None
    complete: bool  # the proposing attempt read the history, scans and inbox (and, on 10.4 at CB, the warranty log),
    # and no result it was shown left records out
    proposal: str  # propose_decision's input as canonical JSON, "" when none
    tool_log: tuple[ToolCall, ...]  # every call of every attempt
    rows: tuple[Row, ...]
    reported_given: tuple[str, ...]  # the tags as sent
    reported: tuple[str, ...]  # the kept tags: history orders, sorted
    draft: Draft | None
    calls: tuple[Call, ...]
    checks: int  # CHECKS_VERSION it was validated with
    prior_claim: bool  # a checked K1
    withdrew: tuple[str, ...]  # the customer's own emails with a checked withdrawal (K2), sorted
    other: bool  # a checked K5
    unbacked: bool  # a point whose quote failed (U1)
    stray_tags: bool  # a tag that isn't an order of this case's history


@dataclass(frozen=True)
class ReadRequest:
    store: Store
    case: Case  # lifecycle.as_case(record) in the app
    dispute: Dispute  # lifecycle.as_dispute(record) in the app: the draft's refund is checked against it
    key: str  # reading_key(store, case)


@dataclass(frozen=True)
class Toolbox:
    """The read tools for one case, gated by its stage. They show only orders of the case's own history and what hangs
    on them, so a planted "call it for 9999" is refused and logged."""
    store: Store
    case: Case
    numbers: tuple[str, ...]  # get_order_history(case.order_number), oldest first
    serials: Mapping[str, str]  # cleaned -> raw, items of history orders (CHARGEBACK only, else empty)

    @classmethod
    def for_case(cls, store: Store, case: Case) -> Toolbox:
        history = store.get_order_history(case.order_number)
        serials: dict[str, str] = {}
        if case.stage is Stage.CHARGEBACK:
            for order in history:
                for item in order.items:
                    if documents.says(item.serial):
                        serials.setdefault(documents.clean(item.serial), item.serial)
        return cls(store, case, tuple(order.number for order in history), MappingProxyType(serials))

    def asked(self, name: str, given: object) -> str:
        """What a call asks for, cleaned as the tool reads it: "Order #1109" is 1109. "" for anything else."""
        key = "serial" if name == "get_warranty_log" else "order_number" if name in READS else ""
        value = given.get(key) if key and isinstance(given, Mapping) else None
        if not isinstance(value, str):
            return ""
        return documents.clean(value) if key == "serial" else ce3.order_number(value)

    def run(self, name: str, given: object) -> tuple[str, tuple[str, ...], str]:
        """One call: the text shown, the ids shown and a refusal ("" when it ran). Never raises on Claude's input."""
        shown, ids, refusal, _ = self.call(name, given)
        return shown, ids, refusal

    def call(self, name: str, given: object) -> tuple[str, tuple[str, ...], str, int]:
        """run(), and how many records the result left out past a cap."""
        asked = self.asked(name, given)
        if name == "get_order_history":
            if asked != self.case.order_number:  # a prior's own history could reach records outside the case
                return "", (), prompts.ASK_DISPUTED.format(number=self.case.order_number), 0
            return self._show(self.store.get_order_history(asked), first=self.store.order(asked).id)
        if name in ("get_carrier_scans", "search_inbox"):
            if asked not in self.numbers:
                return "", (), prompts.NOT_IN_HISTORY.format(number=_echo(asked), numbers=", ".join(self.numbers)), 0
            found = self.store.get_carrier_scans(asked) if name == "get_carrier_scans" else self.store.search_inbox(asked)
            return self._show(found)
        if name == "get_warranty_log":
            if self.case.stage is not Stage.CHARGEBACK:
                return "", (), prompts.NO_WARRANTY_HERE, 0
            if asked not in self.serials:
                return "", (), prompts.NOT_A_SERIAL.format(serial=_echo(asked)), 0
            return self._show(self.store.get_warranty_log(self.serials[asked]))
        return "", (), prompts.NO_TOOL.format(name=_echo(name)), 0

    def complete(self, log: Sequence[ToolCall]) -> bool:
        """Whether one attempt's calls read all it owed (see missing()) and saw every record they asked for. A result
        that left records out past a cap counts as a read, so it isn't owed again, but the reading isn't complete."""
        return not self.missing(log) and not any(call.left_out for call in log if not call.refused)

    def missing(self, log: Sequence[ToolCall]) -> tuple[str, ...]:
        """The reads owed before a proposal counts: the disputed order's history, scans and inbox, and on a fraud claim
        at CHARGEBACK the warranty log of each serial on it. Pass one attempt's calls: another conversation's reads
        aren't in this one's context."""
        done = {(call.name, call.asked) for call in log if not call.refused}
        number = self.case.order_number
        owed = [(name, f"order {number}", number) for name in READS]
        if self.case.reason_code == ce3.FRAUD_CLAIM and self.case.stage is Stage.CHARGEBACK:
            order = self.store.order(number)
            serials = dict.fromkeys(documents.clean(item.serial) for item in order.items if documents.says(item.serial))
            owed += [("get_warranty_log", f"serial {serial}", serial) for serial in serials]
        return tuple(f"{name} for {what}" for name, what, asked in owed if (name, asked) not in done)

    def digest(self) -> str:
        """SHA-256 of every record the tools can reach, as tool_view() shows it, by id: a change to any of them reads the
        case again. The warranty log counts only at CHARGEBACK, where the tool is offered."""
        reach = {order.id: order for order in self.store.get_order_history(self.case.order_number)}
        for number in self.numbers:
            reach.update({scan.id: scan for scan in self.store.get_carrier_scans(number)})
            reach.update({email.id: email for email in self.store.search_inbox(number)})
        for serial in self.serials.values():
            reach.update({entry.id: entry for entry in self.store.get_warranty_log(serial)})
        return _sha256(json.dumps({record_id: tool_view(record) for record_id, record in reach.items()}, **_JSON))

    def _show(self, records: Sequence[Any], first: str = "") -> tuple[str, tuple[str, ...], str, int]:
        """The records as JSON, in the store's order, oldest first. Long text is cut. Past either cap, the record named
        first (the disputed order, in its history) and then the newest that fit are shown: one too big for the room left
        is skipped, so neither old mail nor one huge record pushes the latest out. The rest are counted in "not_shown",
        so no outsider sets the size of a result, and the reading that saw it isn't complete."""
        views = [_capped(tool_view(record)) for record in records]
        size = len(json.dumps({"records": [], "not_shown": len(views)}, ensure_ascii=False))
        chosen: set[int] = set()
        for index in sorted(range(len(views)), key=lambda index: (records[index].id != first, -index)):
            if len(chosen) == prompts.MAX_RECORDS:
                break
            extra = len(json.dumps(views[index], ensure_ascii=False)) + (2 if chosen else 0)  # ", " before all but one
            if size + extra <= prompts.MAX_RESULT_CHARS:
                size, chosen = size + extra, chosen | {index}
        kept = sorted(chosen)
        left = len(views) - len(kept)
        shown = json.dumps({"records": [views[index] for index in kept], "not_shown": left}, ensure_ascii=False)
        return shown, tuple(records[index].id for index in kept), "", left


class Reader(Protocol):
    def read(self, request: ReadRequest) -> Reading:
        """May raise ReaderBusy. Every other failure is a Reading with its failure set."""
        ...


class ClaudeReader:
    """Asks Claude to read one case through the stage's tools, in a loop this code runs, and checks the answer."""

    def __init__(self, client: Any, *, profile: StoreProfile = FERNHILL, effort: str = EFFORT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.profile, self.effort, self.clock = client, profile, effort, clock

    def read(self, request: ReadRequest) -> Reading:
        read = _Read(self, request)
        try:
            for attempt in range(1, ATTEMPTS + 1):  # no answer: one fresh conversation
                reading = read.attempt(attempt)
                if reading is not None:
                    return reading
            return failed(request.key, Failure.NO_PROPOSAL, read.log, read.calls)
        except _Stop as stop:
            return failed(request.key, stop.failure, read.log, read.calls)
        except _Busy as busy:
            raise ReaderBusy(busy.why, read.log, read.calls) from None


class NoReader:
    """A run that doesn't use Claude: no key is set, or the run was told not to (--no-claude). No client is built and no
    call is made, so every case that reads records goes to a person."""

    def read(self, request: ReadRequest) -> Reading:
        return failed(request.key, Failure.NO_KEY)


class ReplayReader:
    """make demo: readings recorded earlier, by key. The key holds no Airwallex id and no clock, so it replays."""

    def __init__(self, saved: Mapping[str, Reading]) -> None:
        self.saved = dict(saved)

    def read(self, request: ReadRequest) -> Reading:
        reading = self.saved.get(request.key)
        return reading if reading is not None else failed(request.key, Failure.NOT_RECORDED)


CLAUDE_API = "https://api.anthropic.com"
CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"  # the SDK adds these to every request when the shell sets them


def claude_client(api_key: str) -> anthropic.Anthropic:
    """The one Claude client this app builds, for a key the caller read. It takes nothing from the shell: it names the
    address, so ANTHROPIC_BASE_URL can't send the records elsewhere; it builds the client while ANTHROPIC_CUSTOM_HEADERS
    is out of the environment, so no header from the shell rides along (the SDK reads it only while it builds a client);
    and its HTTP client has a transport of this app's own that doesn't trust the environment. The SDK's default client
    mounts the shell's proxies whatever trust_env says, unless it is given a transport, so this one has no proxy and
    takes no certificate or .netrc setting from the shell. Callers build it once, at the start of a command."""
    saved = os.environ.pop(CUSTOM_HEADERS, None)
    try:
        return anthropic.Anthropic(api_key=api_key.strip(), base_url=CLAUDE_API, max_retries=2,
                                   http_client=anthropic.DefaultHttpxClient(trust_env=False, transport=_transport()))
    finally:
        if saved is not None:
            os.environ[CUSTOM_HEADERS] = saved


def _transport() -> httpx2.HTTPTransport:
    """The SDK's own default transport (its connection limits and TCP keepalive, so a read of several minutes keeps
    its connection), with no proxy and nothing from the environment."""
    options: list[tuple[int, int, int | bool]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)]
    for name, value in (("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 5), ("TCP_KEEPIDLE", 60)):
        number = getattr(socket, name, None)
        if number is not None:
            options.append((socket.IPPROTO_TCP, number, value))
    return httpx2.HTTPTransport(trust_env=False, limits=anthropic.DEFAULT_CONNECTION_LIMITS, socket_options=options)


def live_reader(api_key: str | None) -> Reader:
    """The reader for a key the caller read. This never reads .env, and its client takes nothing from the shell
    (claude_client)."""
    if api_key is None or not api_key.strip():
        return NoReader()
    return ClaudeReader(claude_client(api_key))


def reading_key(store: Store, case: Case, *, profile: StoreProfile = FERNHILL, effort: str = EFFORT) -> str:
    """What a reading was read from. A saved reading is reused only while this is the same: the same records the tools
    reach, the same case prompt, and the same model, prompts, tools, caps and limits. No clock and no Airwallex id."""
    limits = {"max_leaf": prompts.MAX_LEAF, "max_records": prompts.MAX_RECORDS,
              "max_result_chars": prompts.MAX_RESULT_CHARS, "max_read_chars": prompts.MAX_READ_CHARS,
              "max_turns": MAX_TURNS, "max_tool_calls": MAX_TOOL_CALLS, "max_nudges": MAX_NUDGES,
              "max_early": MAX_EARLY, "attempts": ATTEMPTS}
    return _sha256(json.dumps({
        "version": prompts.PROMPT_VERSION, "model": prompts.MODEL, "effort": effort,
        "max_tokens": [prompts.MAX_TOKENS, prompts.RETRY_MAX_TOKENS], "betas": list(prompts.BETAS),
        "fallbacks": prompts.FALLBACKS, "limits": limits,
        "system": _sha256(prompts.system_prompt(profile)),
        "tools": _sha256(json.dumps(prompts.tools_for(case.stage), **_JSON)),
        "turns": _sha256(json.dumps(prompts.TURN_TEXTS, **_JSON)),
        "prompt": prompts.case_prompt(store, case),
        "records": Toolbox.for_case(store, case).digest(),
    }, **_JSON))


def echo_blocks(content: Sequence[Any]) -> list[Any]:
    """The blocks of a turn that go back to the API, in their order, as the fallback guide says. Before the turn's last
    fallback block only text goes back: thinking and tool calls there came from a model that declined, and a call may be
    cut off, so it never runs. The fallback block and everything after it go back unchanged; so does a turn with none."""
    blocks = list(content)
    seams = [index for index, block in enumerate(blocks) if block.type == "fallback"]
    if not seams:
        return blocks
    return [block for block in blocks[:seams[-1]] if block.type == "text"] + blocks[seams[-1]:]


def failed(key: str, failure: Failure, log: Sequence[ToolCall] = (), calls: Sequence[Call] = ()) -> Reading:
    return Reading(FORMAT, key, failure, False, "", tuple(log), (), (), (), None, tuple(calls), CHECKS_VERSION,
                   False, (), False, False, False)


def validate(raw: object, request: ReadRequest, log: Sequence[ToolCall], *, calls: Sequence[Call] = (),
             complete: bool = True, attempt: int | None = None) -> Reading:
    """Check propose_decision's input. Pure and never raises: a misshapen answer is MALFORMED, and a bug is ERROR, both
    with the raw JSON kept. attempt is the conversation that proposed: only the records its own calls showed can be cited
    (None: the attempt of the last accepted proposal in the log)."""
    log, calls = tuple(log), tuple(calls)
    try:
        text = json.dumps(raw, **_JSON, allow_nan=False)
    except (TypeError, ValueError):
        text = ""
    try:
        proposal = _load(raw)
    except (Unloadable, TypeError, ValueError):
        return replace(failed(request.key, Failure.MALFORMED, log, calls), proposal=text)
    try:
        return _checked(proposal, text, request, log, calls, complete, _proposing(log) if attempt is None else attempt)
    except Exception:  # a bug in a check: the read failed, so a person reads the records
        return replace(failed(request.key, Failure.ERROR, log, calls), proposal=text)


def revalidate(reading: Reading, request: ReadRequest) -> Reading:
    """The same checks again on the saved proposal and tool log, with no API call: after a change to the checks. A failed
    reading is checked again only when the checks themselves failed it, with Claude's answer kept: MALFORMED, or ERROR
    from a bug in a check."""
    if reading.failure is not None and not (reading.failure in (Failure.MALFORMED, Failure.ERROR) and reading.proposal):
        return reading
    try:
        raw = json.loads(reading.proposal)
    except ValueError:
        return replace(failed(request.key, Failure.MALFORMED, reading.tool_log, reading.calls), proposal=reading.proposal)
    attempt = _proposing(reading.tool_log)
    mine = [call for call in reading.tool_log if call.attempt == attempt]  # the loop's rule: Toolbox.complete()
    complete = Toolbox.for_case(request.store, request.case).complete(mine)
    return validate(raw, request, reading.tool_log, calls=reading.calls, complete=complete, attempt=attempt)


def draft_problems(draft: Draft, request: ReadRequest, fees: FeeTable = FEES_V1, *,
                   withdrew: Sequence[str] = ()) -> tuple[str, ...]:
    """Code's checks on Claude's suggestion, in plain words. withdrew: the reading's checked K2 emails. A challenge on a
    claim the table never fights is marked, unless it is backed: NOT_ENTITLED by the warehouse turning the return down,
    or CUSTOMER_WITHDRAWN by the customer's own email."""
    problems = []
    fits = {"ACCEPT": {reason.value for reason in AcceptReason}, "CHALLENGE": {reason.value for reason in ChallengeReason},
            "HAND_OFF": {"NONE"}}
    if draft.reason not in fits.get(draft.action, ()):
        problems.append("The draft's reason doesn't go with its action.")
    turned_down = rules.return_turned_down(request.store, request.case)
    if draft.reason == ChallengeReason.NOT_ENTITLED.value and not turned_down:
        problems.append("NOT_ENTITLED needs the warehouse's scan turning the return down, and the records have none.")
    backed = ((draft.reason == ChallengeReason.NOT_ENTITLED.value and turned_down)
              or (draft.reason == ChallengeReason.CUSTOMER_WITHDRAWN.value and bool(withdrew)))
    code = request.case.reason_code
    if draft.action == "CHALLENGE" and code not in fees.fight_reason_codes and not backed:
        problems.append(f"Code never fights a {code or 'blank'} claim: the fee table has no chance for it.")
    refund = re.sub(r"[\s$,]", "", draft.refund_amount)
    if refund and not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", refund):
        problems.append("The draft's refund isn't an amount.")
    elif refund and Decimal(refund) > ZERO:  # a refund of 0 means none
        amount = Decimal(refund)
        if draft.action != "ACCEPT":
            problems.append("Only an accept sends a refund.")
        else:
            problem = refund_problem(request.dispute, amount, request.dispute.currency, draft.to_card_that_paid)
            if problem:
                problems.append(f"Its refund of {usd(amount)} was refused: {problem}")
    return tuple(problems)


# A field's plain name for code's label. Fixed, so the label never holds Claude's wording.
FIELD_NAMES: Mapping[str, str] = MappingProxyType({
    "account_id": "store account", "at": "time", "body": "text", "card_brand": "card brand",
    "card_fingerprint": "card fingerprint", "card_last4": "card's last four digits", "cardholder_name": "cardholder's name",
    "cardholder_phone": "cardholder's phone", "currency": "currency", "customer_id": "customer", "device_id": "device",
    "direction": "direction", "email": "email", "from_addr": "sender", "ip_address": "IP address",
    "issuer_comment": "comment", "items": "items", "location": "place", "note": "note", "number": "order number",
    "order_number": "order number", "placed_at": "time placed", "prior_dispute": "earlier dispute", "refunds": "refunds",
    "registered_at": "time registered", "sent_at": "time sent", "serial": "serial number",
    "shipping_address": "shipping address", "signed_by": "signed by", "sms_verified_phone": "phone a text confirmed",
    "source": "source", "status": "status", "subject": "subject", "thread_id": "thread", "three_ds": "3DS",
    "to_addr": "recipient", "tracking_number": "tracking number",
})
_RECORDS = {Email: "Email", WarrantyRegistration: "Warranty sign-up"}
_SCANS = {"carrier": "Carrier scan", "warehouse": "Warehouse scan"}


def describe(row: Row, store: Store) -> str:
    """Code's label for a row, from the record and the field, as the case page heads the record: "Email email-1098-1,
    text", "Order #1109, cardholder's name". It holds a store id or an order number at most, never what Claude wrote."""
    if row.part == "tag":
        return "A reported order"
    if row.record_id.strip() == BANK_COMMENT:
        return "The bank's comment"
    try:
        record = store.record(row.record_id.strip())
    except KeyError:
        return "A record the store doesn't have"
    if isinstance(record, Order):
        name = f"Order #{record.number}"
    elif isinstance(record, Scan):
        name = f"{_SCANS.get(documents.key(record.source), 'Scan')} {record.id}"
    else:
        name = f"{_RECORDS.get(type(record), 'Record')} {record.id}"
    return f"{name}, {FIELD_NAMES.get(row.field.strip(), 'a field it does not have')}"


def to_json(reading: Reading) -> str:
    # ASCII only, as lifecycle saves records, so any text Claude sent can be stored, even half of an emoji.
    return json.dumps({"format": FORMAT, "reading": dump(reading)}, sort_keys=True, ensure_ascii=True, allow_nan=False)


def from_json(text: str) -> Reading:
    """Strict: anything that doesn't fit a Reading exactly is Unloadable."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise Unloadable("The saved reading isn't valid JSON.") from None
    if not isinstance(data, dict) or set(data) != {"format", "reading"} or type(data["format"]) is not int \
            or data["format"] != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} reading.")
    reading = load(Reading, data["reading"], "the reading")
    if reading.format != FORMAT:
        raise Unloadable(f"This isn't a format {FORMAT} reading.")
    return reading


# ----------------------------------------------------------------------------- the loop
class _Stop(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class _Busy(Exception):
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


@dataclass
class _Spend:
    """What one attempt has used of its tool budget."""
    calls: int = 0
    chars: int = 0
    full: bool = False  # a result didn't fit in MAX_READ_CHARS: every later call gets BUDGET

    def used_up(self) -> bool:
        """No read can run any more, so a proposal has nothing to wait for."""
        return self.full or self.calls >= MAX_TOOL_CALLS


class _Read:
    """One read: the toolbox, the request parts that never change mid-read, and every attempt's calls so far."""

    def __init__(self, reader: ClaudeReader, request: ReadRequest) -> None:
        self.reader, self.request = reader, request
        self.box = Toolbox.for_case(request.store, request.case)
        self.tools = prompts.tools_for(request.case.stage)
        self.system = [{"type": "text", "text": prompts.system_prompt(reader.profile),
                        "cache_control": {"type": "ephemeral"}}]  # tools and system: the prefix every case shares
        self.first = {"role": "user", "content": prompts.case_prompt(request.store, request.case)}
        self.log: list[ToolCall] = []
        self.calls: list[Call] = []
        self.start = reader.clock()

    def attempt(self, number: int) -> Reading | None:
        """One conversation from the case prompt. None when it ends with no proposal. messages only grows."""
        messages: list[dict[str, Any]] = [self.first]
        nudges = early = 0
        spend = _Spend()
        for turn in range(1, MAX_TURNS + 1):
            message = self.create(messages, prompts.MAX_TOKENS)
            if message.stop_reason == "max_tokens":  # the cut turn is dropped, so its tool calls never run
                message = self.create(messages, prompts.RETRY_MAX_TOKENS)
            _check_stop(message)
            kept = echo_blocks(message.content or [])
            uses = [block for block in kept if block.type == "tool_use"]
            proposals = [block for block in uses if block.name == PROPOSE]
            mine = self.mine(number)
            if proposals and (not self.box.missing(mine) or turn == MAX_TURNS or spend.used_up()  # nothing to wait for
                              or (early >= MAX_EARLY and not self.covers(mine, uses, spend))):
                return self.propose(number, turn, uses, proposals[0], complete=self.box.complete(mine))
            if any(block.type != "fallback" for block in kept):  # never an empty turn: the API refuses one
                messages.append({"role": "assistant", "content": [_param(block) for block in kept]})
            if not uses:
                nudges += 1
                if nudges > MAX_NUDGES:
                    return None
                messages.append({"role": "user", "content": [_text(prompts.NUDGE)]})
                continue
            results, bounced = self.answer(number, turn, uses, proposals, spend, early)
            early += bounced
            note = prompts.LAST_TURN if turn == MAX_TURNS - 1 else prompts.REMINDER
            messages.append({"role": "user", "content": [*results, _text(note)]})
        return None

    def create(self, messages: list[dict[str, Any]], max_tokens: int) -> Any:
        """One request. The SDK has already retried 408, 409, 429, 5xx and connection errors; the rest go most specific
        first."""
        if self.reader.clock() - self.start > READ_SECONDS:
            raise _Stop(Failure.TOO_SLOW)
        try:
            message = self.reader.client.beta.messages.create(
                model=prompts.MODEL, max_tokens=max_tokens, system=self.system, tools=self.tools,
                tool_choice={"type": "auto"},  # "any" and "tool" are a 400 on this model
                messages=messages, output_config={"effort": self.reader.effort},
                cache_control={"type": "ephemeral"},  # the automatic breakpoint on the growing conversation
                betas=list(prompts.BETAS), fallbacks=prompts.FALLBACKS)
        except anthropic.APIStatusError as error:
            raise _status(error) from None
        except anthropic.APIConnectionError:  # a timeout is one
            raise _Busy("This app couldn't reach the Claude API.") from None
        except (anthropic.AnthropicError, ValueError):  # ValueError: the SDK's guard against a long non-streaming call
            raise _Stop(Failure.ERROR) from None
        self.calls.append(_call(message, max_tokens))
        return message

    def mine(self, attempt: int) -> list[ToolCall]:
        return [call for call in self.log if call.attempt == attempt]

    def covers(self, mine: list[ToolCall], uses: list[Any], spend: _Spend) -> bool:
        """Whether the reads beside a proposal would make every read the attempt still owes, if the budget lets them run.
        Then the proposal waits for their results (READ_FIRST), even after an early one."""
        reads = [block for block in uses if block.name != PROPOSE]
        if spend.used_up() or spend.calls + len(reads) > MAX_TOOL_CALLS:
            return False
        planned = [ToolCall(0, 0, block.name, "", "", (), self.box.asked(block.name, block.input)) for block in reads]
        return not self.box.missing([*mine, *planned])

    def propose(self, attempt: int, turn: int, uses: list[Any], proposal: Any, complete: bool) -> Reading:
        """The proposal ends the read: nothing beside it runs. It is logged too, so the log shows which attempt answered."""
        self.log.extend(ToolCall(attempt, turn, block.name, _given(block), "" if block is proposal else prompts.NOT_RUN,
                                 (), self.box.asked(block.name, block.input)) for block in uses)
        return validate(proposal.input, self.request, self.log, calls=self.calls, complete=complete, attempt=attempt)

    def answer(self, attempt: int, turn: int, uses: list[Any], proposals: list[Any], spend: _Spend,
               early: int) -> tuple[list[dict[str, Any]], int]:
        """A result for every call of the turn, in call order, and 1 when the proposal came early. A proposal waits until
        this attempt has read what it owes; if this turn's own calls read it, it isn't early. Once the attempt has used
        its bounce, or this turn's calls used up the budget, a proposal here only waits for this turn's results
        (READ_FIRST), never a NOT_YET it can't follow: the next one is kept."""
        answers: dict[int, tuple[ToolCall, dict[str, Any]]] = {}  # by the call's place in the turn
        for index, block in enumerate(uses):
            if block.name != PROPOSE:
                refusal, shown, ids, left = self.run(block, spend)
                asked = self.box.asked(block.name, block.input)
                call = ToolCall(attempt, turn, block.name, _given(block), refusal, ids, asked, left)
                answers[index] = call, _result(block, refusal or shown, error=bool(refusal))
        bounced = 0
        if proposals:
            still = self.box.missing([*self.mine(attempt), *(call for call, _ in answers.values())])
            bounced = int(bool(still) and early < MAX_EARLY and not spend.used_up())
            reply = prompts.NOT_YET.format(missing=", ".join(still)) if bounced else prompts.READ_FIRST
            for index, block in enumerate(uses):
                if block.name == PROPOSE:
                    first = block is proposals[0]  # any other is answered once and never run, at no cost
                    call = ToolCall(attempt, turn, PROPOSE, _given(block), reply if first else prompts.NOT_RUN, ())
                    answers[index] = call, _result(block, reply if first else prompts.ONCE, error=True)
        ordered = [answers[index] for index in range(len(uses))]
        self.log.extend(call for call, _ in ordered)
        return [result for _, result in ordered], bounced

    def run(self, block: Any, spend: _Spend) -> tuple[str, str, tuple[str, ...], int]:
        """One read call, unless this attempt's budget is used up: (refusal, text shown, ids shown, records left out)."""
        if spend.calls >= MAX_TOOL_CALLS or spend.full:
            return prompts.BUDGET, "", (), 0
        spend.calls += 1
        shown, ids, refusal, left = self.box.call(block.name, block.input)
        if refusal:
            return refusal, "", (), 0
        if spend.chars + len(shown) > prompts.MAX_READ_CHARS:
            spend.full = True
            return prompts.BUDGET, "", (), 0
        spend.chars += len(shown)
        return "", shown, ids, left


def _check_stop(message: Any) -> None:
    """Only tool_use and end_turn go on. A refusal whose fallback model couldn't run (rate-limited or overloaded) is a
    busy API, not a decline."""
    stop = message.stop_reason
    if stop == "refusal":
        if getattr(message.stop_details, "recommended_model", None) is not None:
            raise _Busy("Claude declined, and its fallback model was busy.")
        raise _Stop(Failure.REFUSED)
    if stop in ("max_tokens", "model_context_window_exceeded"):
        raise _Stop(Failure.CUT_OFF)
    if stop not in ("tool_use", "end_turn"):  # pause_turn, compaction, stop_sequence or anything new
        raise _Stop(Failure.UNEXPECTED_STOP)


def _status(error: anthropic.APIStatusError) -> Exception:
    """An error status, most specific first: the account, the key, a busy API, then this app's own request. An account
    with no credit gets a 400, not a 402 (seen live on Nov 11, 2026), so that 400 is the account's too."""
    account = (anthropic.EnforcedSpendLimitReachedError, anthropic.OrganizationOnHoldError)
    if isinstance(error, account) or error.status_code == 402 or _no_credit(error):
        return _Stop(Failure.ACCOUNT)
    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return _Stop(Failure.NOT_ALLOWED)
    if isinstance(error, (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.RequestTimeoutError,
                          anthropic.ConflictError)):
        return _Busy(f"The Claude API answered {error.status_code}.")
    return _Stop(Failure.BAD_REQUEST)


def _no_credit(error: anthropic.APIStatusError) -> bool:
    """The API's 400 for an account with no credit: "Your credit balance is too low to access the Anthropic API. ..."."""
    body = error.body if isinstance(error.body, dict) else {}
    inner = body.get("error")
    message = inner.get("message") if isinstance(inner, dict) else None
    return error.status_code == 400 and isinstance(message, str) and NO_CREDIT in message.casefold()


def _call(message: Any, max_tokens: int) -> Call:
    usage = message.usage
    iterations = getattr(usage, "iterations", None) or ()
    fell_back = any(getattr(item, "type", None) == "fallback_message" for item in iterations) or any(
        block.type == "fallback" for block in message.content or ())
    return Call(str(message.model or ""), fell_back, str(message.stop_reason or ""), max_tokens,
                int(usage.input_tokens or 0), int(usage.output_tokens or 0), int(usage.cache_read_input_tokens or 0),
                int(usage.cache_creation_input_tokens or 0), tuple(_iteration(item) for item in iterations))


def _iteration(item: Any) -> Iteration:
    """One usage.iterations entry. Every count is read with a default, so an entry of a new type still loads."""
    def count(name: str) -> int:
        return int(getattr(item, name, 0) or 0)
    return Iteration(str(getattr(item, "type", "") or ""), str(getattr(item, "model", "") or ""), count("input_tokens"),
                     count("output_tokens"), count("cache_read_input_tokens"), count("cache_creation_input_tokens"))


def _param(block: Any) -> dict[str, Any]:
    """A block as the API takes it back: the SDK's own dump, so from_ goes out as from. Never built by hand."""
    return block.model_dump(by_alias=True, exclude_none=True)


def _result(block: Any, content: str, *, error: bool) -> dict[str, Any]:
    result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
    return {**result, "is_error": True} if error else result


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _given(block: Any) -> str:
    try:
        return json.dumps(block.input, **_JSON)[:MAX_GIVEN]
    except (TypeError, ValueError):
        return str(block.input)[:MAX_GIVEN]


def _echo(text: str) -> str:
    """Claude's input as a refusal repeats it: one line, cut short."""
    return " ".join(str(text).split())[:MAX_ECHO] or '""'


def _capped(value: Any) -> Any:
    """A tool view with each text value cut to MAX_LEAF. Validation still reads the whole field."""
    if isinstance(value, str):
        extra = len(value) - prompts.MAX_LEAF
        return value if extra <= 0 else value[:prompts.MAX_LEAF] + prompts.CUT.format(count=extra)
    if isinstance(value, dict):
        return {key: _capped(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_capped(item) for item in value]
    return value


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- the checks
_PARTS = frozenset({"facts", "conflicts", "reported_orders", "flagged", "draft"})


@dataclass(frozen=True)
class _Cite:
    record_id: str
    field: str
    quote: str


@dataclass(frozen=True)
class _Point:
    kind: str
    record_id: str
    field: str
    quote: str
    note: str


@dataclass(frozen=True)
class _Suggestion:
    action: str
    reason: str
    refund_amount: str
    refund_to_card_that_paid: bool
    note: str


@dataclass(frozen=True)
class _Proposal:
    facts: tuple[_Cite, ...]
    conflicts: tuple[_Point, ...]
    reported_orders: tuple[object, ...]  # any JSON: a tag that isn't text is a dropped row, not a misshapen answer
    flagged: tuple[_Cite, ...]
    draft: _Suggestion


def _load(raw: object) -> _Proposal:
    """The proposal as the schema shapes it, loaded with jsonable's rules: a missing or extra key, a wrong type or a value
    outside an enum raises. Only a tag's own type is checked later."""
    if not isinstance(raw, dict) or set(raw) != _PARTS or not isinstance(raw["reported_orders"], list):
        raise Unloadable("The proposal doesn't have the schema's parts.")
    proposal = _Proposal(load(tuple[_Cite, ...], raw["facts"], "facts"),
                         load(tuple[_Point, ...], raw["conflicts"], "conflicts"), tuple(raw["reported_orders"]),
                         load(tuple[_Cite, ...], raw["flagged"], "flagged"), load(_Suggestion, raw["draft"], "draft"))
    cites = (*proposal.facts, *proposal.conflicts, *proposal.flagged)
    if any(cite.field not in prompts.FIELDS for cite in cites) or any(
            point.kind not in prompts.KINDS for point in proposal.conflicts) or proposal.draft.action not in \
            prompts.ACTIONS or proposal.draft.reason not in prompts.REASONS:
        raise Unloadable("A value in the proposal isn't one the schema allows.")
    return proposal


def _checked(proposal: _Proposal, text: str, request: ReadRequest, log: tuple[ToolCall, ...],
             calls: tuple[Call, ...], complete: bool, attempt: int) -> Reading:
    store, case = request.store, request.case
    shown = frozenset(record_id for call in log if call.attempt == attempt and not call.refused
                      for record_id in call.record_ids)
    facts = _cited(proposal.facts, "fact", store, case, shown, MAX_FACTS, "Code keeps the first 40 facts.")
    flags = _cited(proposal.flagged, "flag", store, case, shown, MAX_FLAGS, "Code keeps the first 20 flagged lines.")
    points = _points(proposal.conflicts, store, case, shown)
    tags, reported, given, stray = _tags(proposal.reported_orders, store, case)
    # The kinds come from every point sent, before any cap, so the order of rows can't decide what reaches code.
    withdrew = tuple(sorted({row.record_id.strip() for row in points if row.counted_as == "K2"}))
    flagged = [item.quote for item, row in zip(proposal.flagged, flags) if row.kept]
    draft = _draft(proposal.draft, request, flagged, withdrew)
    return Reading(FORMAT, request.key, None, complete, text, log, (*facts, *points, *flags, *tags), given, reported,
                   draft, calls, CHECKS_VERSION, any(row.counted_as == "K1" for row in points), withdrew,
                   any(row.counted_as == "K5" for row in points), any(row.counted_as == "U1" for row in points), stray)


def _problem(record_id: str, field: str, quote: str, store: Store, case: Case, shown: frozenset[str]) -> str:
    """Why a citation fails, or "" when it passes. The first failure is the reason."""
    record_id, field = record_id.strip(), field.strip()
    if record_id == BANK_COMMENT:
        leaves = (case.issuer_comment or "",) if field == "issuer_comment" else None
    elif record_id not in shown:  # this conversation never saw it, whether it exists or not
        return "Claude didn't read that record."
    else:
        leaves = field_text(store.record(record_id), field)
    if leaves is None:
        return "That record has no such field."
    said = documents.clean(quote)  # only composition and spacing are forgiven, on both sides
    if not said or ce3.says_nothing(said):
        return "The quote is empty or says nothing."
    values = [documents.clean(leaf) for leaf in leaves]  # items and refunds: one leaf each
    if said in values:
        return ""
    if len(said) < MIN_QUOTE:
        return "The quote is too short to show anything."
    if len(said) > MAX_QUOTE:
        return "The quote is too long. A short phrase or one sentence is enough."
    return "" if any(said in value for value in values) else "The quote isn't in that field."


def _cited(items: Sequence[_Cite], part: str, store: Store, case: Case, shown: frozenset[str], cap: int,
           capped: str) -> tuple[Row, ...]:
    rows, seen = [], set()
    for item in items:
        why = _problem(item.record_id, item.field, item.quote, store, case, shown)
        key = (item.record_id.strip(), item.field.strip(), documents.clean(item.quote))
        if not why and key in seen:
            why = "Claude sent this twice."
        elif not why and len(seen) >= cap:
            why = capped
        if not why:
            seen.add(key)
        rows.append(Row(part, _sent(item.record_id), _sent(item.field), _sent(item.quote), not why, why))
    return tuple(rows)


def _points(items: Sequence[_Point], store: Store, case: Case, shown: frozenset[str]) -> tuple[Row, ...]:
    """Each point counts as one of code's kinds: K1 and K2 only from the customer's own email, K5 for anything else whose
    quote holds, and U1 when it doesn't. Deduplicated on the kind and the citation, so a second kind isn't lost."""
    customer = rules.customer_emails(store, store.order(case.order_number))
    rows, seen = [], set()
    for item in items:
        why = _problem(item.record_id, item.field, item.quote, store, case, shown)
        counted = "U1" if why else _kind(item, store, case, customer)
        key = (item.kind, item.record_id.strip(), item.field.strip(), documents.clean(item.quote))
        if not why and key in seen:
            why = "Claude sent this point twice."
        elif not why and len(seen) >= MAX_CONFLICTS:
            why = "Code keeps the first 10 points."
        if not why:
            seen.add(key)
        rows.append(Row("conflict", _sent(item.record_id), _sent(item.field), _sent(item.quote), not why, why,
                        _sent(item.kind), counted, _note(item.note)))
    return tuple(rows)


def _kind(item: _Point, store: Store, case: Case, customer: frozenset[str]) -> str:
    """Code's own check of a kind: the record type and the sender, never the meaning."""
    record_id, field = item.record_id.strip(), item.field.strip()
    record = None if record_id == BANK_COMMENT else store.record(record_id)
    own = isinstance(record, Email) and field in ("subject", "body") and rules.mailbox(record.from_addr) in customer
    if item.kind == Kind.CUSTOMER_WITHDREW.value and own:
        return "K2"
    if item.kind == Kind.PRIOR_FRAUD_CLAIMED.value and own and case.reason_code == ce3.FRAUD_CLAIM:
        return "K1"
    return "K5"  # OTHER, a lookalike sender, support's own reply, or a fraud claim on another claim


def _tags(values: Sequence[object], store: Store, case: Case) -> tuple[tuple[Row, ...], tuple[str, ...],
                                                                         tuple[str, ...], bool]:
    """The tags code passes to ce3: orders of this case's history other than the disputed one, sorted, at most
    MAX_TAGS, so ce3 only ever writes a store number. Every other tag is a dropped row. A dropped one that names some
    order (not one that isn't text, is blank or is the disputed order) is stray, and sends a person to the bank's
    comment. On another claim than fraud, tags count for nothing."""
    given = tuple(_as_sent(value) for value in values)
    if case.reason_code != ce3.FRAUD_CLAIM:
        ignored = tuple(Row("tag", "", "", sent, False, "Tags only count on a fraud claim.") for sent in given)
        return ignored, (), given, False
    numbers = {order.number for order in store.get_order_history(case.order_number)}
    rows: list[Row] = []
    found: dict[str, str] = {}  # a kept number, and the tag it was first sent as
    stray = False
    for value, sent in zip(values, given):
        if not isinstance(value, str):
            why = "A reported order must be written as text."
        elif not (number := ce3.order_number(value)):
            why = "The reported order is blank."
        elif number == case.order_number:
            why = "That is the disputed order, not an earlier one."
        elif number not in numbers:
            why, stray = "That order isn't in this case's history.", True
        else:
            found.setdefault(number, sent)
            continue
        rows.append(Row("tag", "", "", sent, False, why))
    kept = sorted(found)
    rows += [Row("tag", "", "", found[number], False, "Code keeps the first 10 reported orders.")
             for number in kept[MAX_TAGS:]]
    return tuple(rows), tuple(kept[:MAX_TAGS]), given, stray or len(kept) > MAX_TAGS


def _draft(suggestion: _Suggestion, request: ReadRequest, flagged: Sequence[str], withdrew: tuple[str, ...]) -> Draft:
    note = _note(suggestion.note)
    if _repeats(note, flagged):  # a suggestion that echoes a planted line isn't shown
        note = ""
    draft = Draft(suggestion.action, suggestion.reason, _sent(suggestion.refund_amount),
                  suggestion.refund_to_card_that_paid, note, ())
    return replace(draft, problems=draft_problems(draft, request, withdrew=withdrew))


def _repeats(note: str, quotes: Sequence[str]) -> bool:
    """Whether a run of 6 words in the note is in one of the quotes, ignoring case and punctuation."""
    def runs(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"[^\W_]+", documents.clean(text).casefold())
        return {tuple(words[index:index + 6]) for index in range(len(words) - 5)}
    mine = runs(note)
    return bool(mine) and any(mine & runs(quote) for quote in quotes)


def _note(text: str) -> str:
    """Claude's own sentence: one line, no control or format characters, at most MAX_NOTE characters."""
    shown = "".join(char for char in " ".join(text.split()) if not unicodedata.category(char).startswith("C"))
    return " ".join(shown.split())[:MAX_NOTE].rstrip()


def _sent(value: str) -> str:
    return value[:MAX_SENT]


def _as_sent(value: object) -> str:
    """A tag as Claude sent it, as text: a value that isn't text is shown as its JSON."""
    if isinstance(value, str):
        return _sent(value)
    try:
        return _sent(json.dumps(value, **_JSON))
    except (TypeError, ValueError):
        return _sent(str(value))


def _proposing(log: Sequence[ToolCall]) -> int:
    """The attempt that proposed: the last accepted propose_decision in the log, else the last attempt that called."""
    accepted = [call.attempt for call in log if call.name == PROPOSE and not call.refused]
    return accepted[-1] if accepted else max((call.attempt for call in log), default=1)


proposing = _proposing
