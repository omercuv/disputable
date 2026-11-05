"""The store's own records: what Claude's read-only tools and the CE 3.0 check get to read.

Plain frozen data, with no clock and no network. The four query methods are all the tools can reach, and nothing here
can act on a case. A record's id is what a verified fact points at, so facts are built from records, not from wording.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator

from disputable.engine import Dispute, Stage, Status
from disputable.fees import ZERO

# Values that say nothing, so they can't match: what stores and scripts write when a field is empty ("0" for a guest's
# account; "localhost", and the other names Linux hosts files give it, for the server's own name). A value is read as its
# letters and digits, so case and punctuation don't hide one ("N/A.", "(none)"), a value with none ("--") is blank, and so
# are placeholders joined ("None, None"). The CE 3.0 check, the files and the order history all read blanks this way.
PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "none", "null", "nil", "undefined", "not set", "(not set)", "guest", "-",
                          "0", "localhost", "localhost.localdomain", "localhost4", "localhost4.localdomain4", "localhost6",
                          "localhost6.localdomain6", "ip6-localhost", "ip6-loopback"})
BLANKS = frozenset(re.sub(r"[\W_]+", "", phrase) for phrase in PLACEHOLDERS)  # joined up: "notset", and "-" is ""


def says_nothing(value: str | None) -> bool:
    """Whether a value is a blank or a placeholder, however it's written: "N/A.", "(none)" and "--" say nothing, and so
    does a value whose every part is one, such as "None, None, None" (empty address fields joined)."""
    text = (value or "").casefold()
    return _joined(text) in BLANKS or all(_joined(part) in BLANKS for part in re.split(r"[\s,;]+", text))


def _joined(text: str) -> str:
    return "".join(re.findall(r"[^\W_]+", text))


def mailbox(value: object) -> str:
    """An email address as a sender check reads it: its own characters, composed (NFC) with spaces trimmed, and only the
    ASCII letters in lower case, or "" when it says nothing. So a lookalike such as "straße" for "strasse" never passes
    for the customer's address, and a customer's own non-ASCII address still matches itself."""
    text = " ".join(unicodedata.normalize("NFC", "" if value is None else str(value)).split())
    return "" if says_nothing(text) else "".join(char.lower() if char.isascii() else char for char in text)


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    email: str
    phone: str
    billing_address: str


@dataclass(frozen=True)
class Item:
    sku: str
    description: str
    unit_price: Decimal
    quantity: int = 1
    serial: str | None = None


@dataclass(frozen=True)
class Refund:
    amount: Decimal
    at: datetime


@dataclass(frozen=True)
class Order:
    number: str
    customer_id: str
    placed_at: datetime
    items: tuple[Item, ...]
    card_fingerprint: str  # the same card gives the same value; card numbers are never stored
    card_last4: str
    cardholder_name: str
    cardholder_phone: str
    account_id: str  # the store login that placed it
    email: str
    device_id: str
    ip_address: str
    shipping_address: str
    currency: str = "USD"
    card_brand: str = "visa"
    three_ds: bool = False
    tracking_number: str | None = None
    prior_dispute: str | None = None  # "FRAUD" or "NON_FRAUD". A fraud report the bank never told the store about isn't here
    refunds: tuple[Refund, ...] = ()

    @property
    def id(self) -> str:
        return f"order-{self.number}"

    @property
    def amount(self) -> Decimal:
        return sum((item.unit_price * item.quantity for item in self.items), ZERO)


@dataclass(frozen=True)
class Scan:
    id: str
    order_number: str
    source: str  # "carrier" or "warehouse"
    direction: str  # "outbound" or "return"
    at: datetime
    status: str
    location: str
    signed_by: str | None = None
    tracking_number: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Email:
    id: str
    thread_id: str
    sent_at: datetime
    from_addr: str
    to_addr: str
    subject: str
    body: str


@dataclass(frozen=True)
class WarrantyRegistration:
    id: str
    serial: str
    registered_at: datetime
    email: str
    sms_verified_phone: str | None
    device_id: str
    ip_address: str
    note: str = ""


@dataclass(frozen=True)
class Case:
    """A dispute as the simulated issuer opens it. Due dates count from the run's clock, not from a fixed date."""
    id: str
    order_number: str
    reason_code: str
    stage: Stage
    opened_at: datetime
    due_in: timedelta
    issuer_comment: str
    amount: Decimal | None = None  # the order's total, unless the dispute is for part of it
    status: Status = Status.REQUIRES_RESPONSE
    prior_challenges: int = 0
    ai_automation_challenged: bool = False
    rejected_fact_ids: frozenset[str] = frozenset()
    rejected_file_hashes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Store:
    customers: tuple[Customer, ...] = ()
    orders: tuple[Order, ...] = ()
    scans: tuple[Scan, ...] = ()
    emails: tuple[Email, ...] = ()
    warranty: tuple[WarrantyRegistration, ...] = ()
    cases: tuple[Case, ...] = ()

    def __post_init__(self) -> None:
        repeated = [record_id for record_id, count in Counter(record.id for record in self._records()).items() if count > 1]
        if repeated:
            raise ValueError(f"record ids used twice: {sorted(repeated)}")
        customers = {customer.id for customer in self.customers}
        numbers = {order.number for order in self.orders}
        for order in self.orders:
            if order.customer_id not in customers:
                raise ValueError(f"order {order.number} names an unknown customer")
        for record in (*self.scans, *self.cases):
            if record.order_number not in numbers:
                raise ValueError(f"{record.id} names an unknown order")

    def _records(self) -> Iterator[Any]:
        yield from self.customers
        yield from self.orders
        yield from self.scans
        yield from self.emails
        yield from self.warranty
        yield from self.cases

    def record(self, record_id: str) -> Any:
        for record in self._records():
            if record.id == record_id:
                return record
        raise KeyError(record_id)

    def quote_is_in(self, record_id: str, quote: str) -> bool:
        """Whether this exact text is in one of the record's fields. A fact without a true quote gets dropped.

        So does a fact about a record that doesn't exist. Under 3 characters a fragment proves nothing: it must be a whole
        value.
        """
        try:
            record = self.record(record_id)
        except KeyError:
            return False
        fragment_ok = len(quote) >= 3
        return bool(quote.strip()) and any(quote in text if fragment_ok else quote == text for text in _text_of(record))

    def order(self, number: str) -> Order:
        return self.record(f"order-{number}")

    def customer(self, customer_id: str) -> Customer:
        return self.record(customer_id)

    def case(self, case_id: str) -> Case:
        return self.record(case_id)

    def dispute(self, case_id: str, now: datetime) -> Dispute:
        """The case as the decision engine sees it, without going through the API."""
        case = self.case(case_id)
        order = self.order(case.order_number)
        return Dispute(
            id=case.id,
            stage=case.stage,
            status=case.status,
            amount=order.amount if case.amount is None else case.amount,
            currency=order.currency,
            reason_code=case.reason_code,
            due_at=now + case.due_in,
            captured_amount=order.amount,
            refunded_amount=sum((refund.amount for refund in order.refunds), ZERO),
            prior_challenges=case.prior_challenges,
            ai_automation_challenged=case.ai_automation_challenged,
            rejected_fact_ids=case.rejected_fact_ids,
            rejected_file_hashes=case.rejected_file_hashes,
        )

    def without_warranty(self) -> Store:
        """The same store with no warranty log: the second ending."""
        return replace(self, warranty=())

    # ------------------------------------------------------------------------- what Claude's tools can read
    def get_order_history(self, order_number: str) -> tuple[Order, ...]:
        """Every order by the same customer or on the same card, oldest first, this one included. A card with no
        fingerprint on record, however the blank is written, is no card, so it ties no stranger's order to this one."""
        order = self.order(order_number)
        card = _card(order)
        history = [other for other in self.orders
                   if other.customer_id == order.customer_id or (card and _card(other) == card)]
        return tuple(sorted(history, key=lambda other: other.placed_at))

    def get_carrier_scans(self, order_number: str) -> tuple[Scan, ...]:
        """The carrier's and the warehouse's scans for this order, both ways, oldest first."""
        self.order(order_number)
        return tuple(sorted((scan for scan in self.scans if scan.order_number == order_number), key=lambda scan: scan.at))

    def get_warranty_log(self, serial: str) -> tuple[WarrantyRegistration, ...]:
        return tuple(sorted((entry for entry in self.warranty if entry.serial == serial),
                            key=lambda entry: entry.registered_at))

    def search_inbox(self, order_number: str, query: str = "") -> tuple[Email, ...]:
        """Emails to or from this order's customer, or naming the order, oldest first. query narrows them, ignoring case.
        An address is the customer's when it is as the sender check reads it (mailbox) or in full lower case, so the
        inbox shows at least every email the sender check could call the customer's."""
        order = self.order(order_number)
        addresses = _inbox_keys(order.email) | _inbox_keys(self.customer(order.customer_id).email)
        names_order = re.compile(rf"(?<!\d){re.escape(order_number)}(?!\d)")
        found = [email for email in self.emails
                 if _inbox_keys(email.from_addr) & addresses or _inbox_keys(email.to_addr) & addresses
                 or names_order.search(f"{email.subject} {email.body}")]
        if query:
            found = [email for email in found if query.lower() in f"{email.subject} {email.body}".lower()]
        return tuple(sorted(found, key=lambda email: email.sent_at))


def _inbox_keys(address: str) -> set[str]:
    """How the inbox knows an address: as mailbox() reads it, and in full lower case. A blank is nobody's."""
    key = mailbox(address)
    return {key, address.lower()} if key else set()


def _card(order: Order) -> str:
    """The card's fingerprint, or "" when it says nothing, as ce3 reads it."""
    card = order.card_fingerprint.strip()
    return "" if says_nothing(card) else card


def tool_view(record: Any) -> dict[str, Any]:
    """A record as Claude's tools show it: the id a fact must cite, and each value as the text the quote check holds."""
    return {"id": record.id, **_shown(record)}


def field_text(record: Any, field: str) -> tuple[str, ...] | None:
    """One field's text, as quote_is_in() reads it, or None when the record has no such field. The id isn't a field."""
    if field == "id" or not is_dataclass(record) or field not in {item.name for item in fields(record)}:
        return None
    return tuple(_text_of(getattr(record, field)))


def _shown(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _shown(getattr(value, item.name)) for item in fields(value) if item.name != "id"}
    if isinstance(value, (tuple, frozenset)):
        return [_shown(item) for item in value]
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _text_of(value: Any) -> Iterator[str]:
    if is_dataclass(value):
        for item in fields(value):
            if item.name != "id":  # the record's label in this store, which no source wrote
                yield from _text_of(getattr(value, item.name))
    elif isinstance(value, (tuple, frozenset)):
        for item in value:
            yield from _text_of(item)
    elif isinstance(value, datetime):
        yield value.isoformat()
    elif value is not None:
        yield str(value)
