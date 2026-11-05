"""Evidence files: the PDFs the bank reads when this app challenges a dispute.

Code writes every line from the store's records, never Claude, so a file says only what the records hold. A file's hash
is the SHA-256 of its text, not of its bytes. The engine and the approval compare that hash, so the same evidence keeps it
when the file is built again or laid out anew. But the text follows the records and the wording, and both can change
between rounds, so a copy built in a later round could pass for a new file. So the purchase history, CE 3.0's one
attempt, is built in a case's first round only, and no file is built whose records the bank has all turned down. A
package takes its files through with_files(), which adds every record each one proves. Tests pin the story's two hashes,
so the wording changes only on purpose.

Pure code: no clock, no network and no randomness. The PDF is written by hand in Helvetica, a font every PDF reader has,
so no PDF library is needed and the same text always gives the same bytes.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import MAX_PREC, Decimal, localcontext
from typing import Iterable

from disputable.ce3 import BLANKS, ELEMENTS, NAMES, Ce3Result, Prior, ip_says_nothing, says_nothing
from disputable.engine import Evidence
from disputable.fees import EvidenceClass
from disputable.store import Case, Order, Scan, Store, WarrantyRegistration

MAX_PDF_BYTES = 2 * 1024 * 1024  # Visa takes a PDF of up to 2 MB per file
MAX_NAME = 50  # Airwallex's file service keeps 50 characters of a file's name, and of its notes
PAGE_WIDTH, PAGE_HEIGHT, MARGIN = 612, 792, 72  # US Letter, in points
TITLE, TEXT, FOOTER = (14, 20), (10, 14), 8  # font size and line height, in points
# Helvetica's widths in thousandths of the font size, for the printable ASCII characters (WinAnsiEncoding 32 to 126). Any
# other character counts as a full width, so a line never runs past the margin.
WIDTHS = dict(zip(range(32, 127), (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, *(556,) * 10, 278, 278, 584, 584, 584,
    556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667,
    944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556,
    556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)))
LABELS = {"account": "Store account", "address": "Delivery address", "device": "Device ID", "ip": "IP address"}
ITEM = "- "  # a paragraph that starts with it is a list item: its later rows line up after the dash
CANT_SHOW = "A record holds text this PDF can't show, so a person writes this file."
NOT_A_NUMBER = "An amount on record isn't a number this file can show, so a person writes this file."


class NoFileDue(ValueError):
    """No file of this kind is due: a later round, another order, nothing to cite, or a sign-up that can't go."""


class PersonWritesFile(ValueError):
    """The records hold something this file can't show (CANT_SHOW, NOT_A_NUMBER, over 2 MiB): a person writes it."""


# What carriers write as the signer when nobody signed, as far as the usual ones go. A value is read as its words, so
# case, spaces and punctuation don't hide a phrase ("Front Door / Porch", "FRONTDOOR", "Left at front door."), and a
# value with a NO_SIGNER word in it is a place, a release or nobody. Any other value is a signer's name, and so is a role
# a person signs as, such as "DOCK".
NO_SIGNATURE = frozenset({
    "driver release", "signature not required", "no signature required", "no signature", "not signed", "unsigned",
    "signature waived", "signature release", "signature release on file", "release on file", "contactless", "doorstep",
    "front door", "back door", "side door", "porch", "front porch", "front door/porch", "garage", "mailbox",
    "in/at mailbox", "parcel locker", "left at door", "left at front door", "front desk/reception/mail room",
    "neighbor as requested", "po box", "safe place"})
NO_SIGNER = frozenset({"left", "leave", "door", "doorstep", "porch", "mailbox", "locker", "garage", "release", "released",
                       "signature", "unsigned", "contactless", "covid", "covid19", "c19", "cv19"})
NOBODY = frozenset(re.sub(r"[\W_]+", "", phrase) for phrase in NO_SIGNATURE | NO_SIGNER)  # joined up: "frontdoorporch"
UNSURE = frozenset({"unknown", "undefined"})  # in a dispute field: the store doesn't know, which isn't "none"
# Every quotation mark WinAnsiEncoding has, straight, curly, low and angled, and the accents that look like one. In a
# record's words that the file quotes, each becomes ' and a run of them one, so the words can't end the quote early.
QUOTES = str.maketrans(dict.fromkeys('"`\u00b4\u2018\u2019\u201a\u201c\u201d\u201e\u00ab\u00bb\u2039\u203a', "'"))
# Card brands as a bank writes them, by how records name them: lower case, with "_" or a space between words.
BRANDS = {"visa": "Visa", "mastercard": "Mastercard", "amex": "American Express", "american_express": "American Express",
          "discover": "Discover", "jcb": "JCB", "unionpay": "UnionPay", "union_pay": "UnionPay", "diners": "Diners Club",
          "diners_club": "Diners Club"}


@dataclass(frozen=True)
class Document:
    """One evidence file as the bank reads it. fact_ids are the records it proves: with_files() puts them in the engine's
    Evidence with its hash. Its lines may restate other records, such as the order a warranty sign-up is measured
    against."""
    title: str
    lines: tuple[str, ...]  # one paragraph each; "" leaves a blank line
    fact_ids: frozenset[str]
    notes: str  # the file service's notes, at most 50 characters
    description: str  # what the challenge's supporting_documents entry says about the file

    def __post_init__(self) -> None:
        if not isinstance(self.lines, tuple) or not isinstance(self.fact_ids, frozenset):
            raise TypeError("lines is a tuple and fact_ids a frozenset, so a document can't change after it's built")
        for text in (self.title, *self.lines, self.notes, self.description):
            _check(text)
        if not self.title or not any(self.lines):
            raise ValueError("A document needs a title and at least one line.")
        if not self.lines[-1]:  # the PDF shows nothing for it, or a last page with only its number, but the hash counts it
            raise ValueError("A document can't end with a blank line.")
        if not self.fact_ids or not all(isinstance(fact, str) and fact.strip() for fact in self.fact_ids):
            raise ValueError("A document proves at least one record, named by its id.")
        if len(self.notes) > MAX_NAME:
            raise ValueError(f"The file service keeps only {MAX_NAME} characters of a file's notes.")

    @property
    def text(self) -> str:
        """What the bank reads: the title, then each paragraph."""
        return "\n".join((self.title, *self.lines))

    @property
    def sha256(self) -> str:
        """The file's hash, as the engine's Evidence and the approval take it. Only the text decides it."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def filename(self) -> str:
        """A name that holds nothing but the hash: Airwallex writes it into the file's id."""
        return f"evidence-{self.sha256[:16]}.pdf"

    def pdf(self) -> bytes:
        return _pdf(self.title, self.lines)


def with_files(evidence: Evidence, *files: Document) -> Evidence:
    """The package that sends these files: each one's hash goes in with every record it proves, so the round's plan keeps
    all the bank is shown, and a later round can't take one of them for new. When ce3.evidence(result, case) is
    CE3_QUALIFIED, round one is with_files(that, purchase_history(store, case, result)). Another kind of file goes with
    its own class, as round two's does: with_files(Evidence(EvidenceClass.INDEPENDENT_POSSESSION), warranty_registration(
    store, case, sign_up_id)). Evidence of no class sends nothing, so a file beside it is refused, not quietly dropped."""
    if evidence.evidence_class is EvidenceClass.NONE:
        if files:
            raise ValueError("Evidence of no class sends nothing, so no file can go with it.")
        return evidence
    return replace(evidence, fact_ids=evidence.fact_ids.union(*(file.fact_ids for file in files)),
                   file_hashes=evidence.file_hashes.union(file.sha256 for file in files))


# ----------------------------------------------------------------------------- what each file says
def purchase_history(store: Store, case: Case, result: Ce3Result) -> Document:
    """CE 3.0's file: the disputed order, each earlier order the check cites and the delivery. Its facts are those orders
    and the delivery scan. Each earlier order shows its date wherever it falls in Visa's window: the bank reads the dates,
    and the check has already said what counts. Visa allows one CE 3.0 attempt, so it's built in a case's first round
    only: built later, from that round's check or from records that moved on, it could pass for a new file."""
    if case.prior_challenges or case.rejected_fact_ids or case.rejected_file_hashes:  # a later round, as ce3 reads one
        raise NoFileDue("Visa allows one CE 3.0 attempt, and this case is past its first round.")
    order = store.order(case.order_number)
    if order.id not in result.fact_ids:
        raise NoFileDue(f"The CE 3.0 check given is for another order than #{order.number}.")
    cited = [prior for prior in result.priors if prior.cited]
    if not cited:
        raise NoFileDue("The CE 3.0 check cites no earlier order, so there's no purchase history to show.")
    scan, delivery = _delivery(store, order)
    lines = [_disputed(order), *_details(order), "", "Earlier orders paid with the same card:", *map(_earlier, cited)]
    if delivery:
        lines += ["", *delivery]
    lines += ["", "All dates are calendar dates in UTC."]
    facts = {order.id, *(prior.order.id for prior in cited), *([scan.id] if scan else [])}  # each one the file shows
    return Document(f"Order #{_clean(order.number)}: earlier purchases on the same card", tuple(lines), frozenset(facts),
                    _notes(f"Earlier purchases, order {_clean(order.number)}"),
                    f"Earlier purchases on the same card as order #{_clean(order.number)}")


def warranty_registration(store: Store, case: Case, registration_id: str) -> Document:
    """Proof the item reached the cardholder: the warranty sign-up for its serial number. Its one fact is the sign-up. Its
    lines also restate the order and the delivery, which the sign-up is measured against, and the customer's own orders."""
    order = store.order(case.order_number)
    entry = store.record(registration_id)
    if not isinstance(entry, WarrantyRegistration):
        raise NoFileDue(f"{registration_id} isn't a warranty registration.")
    if entry.id in case.rejected_fact_ids:  # built again later, it could pass for a new file
        raise NoFileDue(f"The bank already turned down warranty registration {registration_id} in an earlier round.")
    serial = _clean(entry.serial)
    if not _says(serial) or serial not in {_clean(item.serial) for item in order.items if _says(item.serial)}:
        raise NoFileDue(f"Warranty registration {registration_id} is for a serial number that isn't on order "
                         f"#{_clean(order.number)}.")
    history = _orders_of(store, order)
    scan, delivery = _delivery(store, order)
    lines = [_disputed(order), *_cardholder(order), *delivery, ""]
    when = f"Serial {serial} was registered for warranty on {_day(entry.registered_at)}"
    lines.append(f"{when}, {_relative(scan.at, entry.registered_at)}:" if scan else f"{when}:")
    lines += [ITEM + _email(store, order, history, entry.email), ITEM + _texted(order, entry.sms_verified_phone),
              ITEM + _source(store, order, history, entry), "", "All dates are calendar dates in UTC."]
    return Document(f"Order #{_clean(order.number)}: warranty registration of serial {serial}", tuple(lines),
                    frozenset({entry.id}), _notes(f"Warranty registration, order {_clean(order.number)}"),
                    f"Warranty registration of serial {serial} from order #{_clean(order.number)}")


def _disputed(order: Order) -> str:
    return (f"Disputed order #{_clean(order.number)}, placed {_day(order.placed_at)}: {_items(order)}, {_amount(order)}, "
            f"paid with {_card(order)}.")


def _cardholder(order: Order) -> list[str]:
    return [_stop(f"Cardholder: {_clean(order.cardholder_name)}")] if _says(order.cardholder_name) else []


def _details(order: Order) -> list[str]:
    """The four details CE 3.0 compares, as the disputed order has them, after the cardholder's name. One that says
    nothing to the check, such as a proxy's IP, isn't shown."""
    reads = {"device": _device, "ip": _ip}
    shown = [(LABELS[element], getattr(order, field), reads.get(element, _key)) for element, field in ELEMENTS.items()]
    return _cardholder(order) + [_stop(f"{label}: {_clean(value)}") for label, value, read in shown if read(value)]


def _earlier(prior: Prior) -> str:
    order = prior.order
    same = _join(NAMES[element] for element in ELEMENTS if element in prior.matches)
    line = (f"{ITEM}Order #{_clean(order.number)}, placed {_day(order.placed_at)}, {_days(prior.days_before_order)} before "
            f"the disputed order: {_items(order)}, {_amount(order)}. Same {same} as the disputed order. ")
    kind = _clean(order.prior_dispute).upper() if _says(order.prior_dispute) else ""  # "none" says nothing, as "" does
    # A blank with a question mark or an unsure part, split as ce3.says_nothing splits one: "Unknown.", "?", "None?" and
    # "Unknown / N/A" don't say "none". "FRAUD?" isn't a blank: it's quoted.
    parts = map(_words, re.split(r"[\s,;]+", _clean(order.prior_dispute)))
    if not kind and ("?" in _clean(order.prior_dispute) or not UNSURE.isdisjoint(parts)):
        line += "The store's records don't show whether it had a dispute."
    elif not kind:
        line += "The store's records show no dispute or fraud report on it."
    elif kind == "NON_FRAUD":
        line += "The store's records show a dispute on it that wasn't for fraud."
    else:  # a value this file can't read is quoted, not taken for a dispute: "No dispute" isn't one
        line += f'The store\'s dispute field for it reads "{_quoted(order.prior_dispute)}".'
    # Written before they're sorted: a NaN can't be compared, and a tie in time is then ordered by what the bank reads.
    refunds = sorted((_utc(refund.at), _money(order.currency, refund.amount)) for refund in order.refunds)
    return line + "".join(f" {amount} of it was refunded on {at.date().isoformat()}." for at, amount in refunds)


def _delivery(store: Store, order: Order) -> tuple[Scan | None, list[str]]:
    """The carrier's last delivery of the order, or None and no lines when it has none."""
    scans = [scan for scan in store.get_carrier_scans(order.number)
             if scan.source == "carrier" and scan.direction == "outbound" and scan.status == "DELIVERED"]
    if not scans:
        return None, []
    scan = max(scans, key=lambda scan: (_utc(scan.at), scan.id))  # the same scan, in whatever order the records come
    where = f" to {_clean(scan.location)}" if _says(scan.location) else ""
    tracking = f" (carrier tracking {_clean(scan.tracking_number)})" if _says(scan.tracking_number) else ""
    lines = [_stop(f"Order #{_clean(order.number)} was delivered on {_day(scan.at)}{where}{tracking}")]
    # To a bank, "the billing address" is the cardholder's. The store keeps only its customer's, so that's the one meant
    # only when the cardholder is the customer, and no order on this card, the customer's own or not, names someone else.
    customer = store.customer(order.customer_id)
    # Here a name is blank only when the whole of it is one placeholder: "Na Na" reads as no name elsewhere, but it may
    # be a person's, and then the card may be theirs.
    names = {"" if _words(other.cardholder_name) in BLANKS else _clean(other.cardholder_name).casefold()
             for other in _on_card(store, order)}
    at_billing = (_address(scan.location) == _address(customer.billing_address) != ""
                  and _key(order.cardholder_name) == _key(customer.name) != "" and names <= {"", _key(customer.name)})
    if _says(scan.signed_by) and not _nobody(scan.signed_by):
        signed = _stop(f"Delivered and signed for at the billing address. Signed by: {_clean(scan.signed_by)}" if at_billing
                       else f"Signed for on delivery by {_clean(scan.signed_by)}")
    else:
        signed = "Delivered to the billing address, with no signature." if at_billing else "Delivered with no signature."
    lines.append(signed + (f' Carrier note: "{_quoted(scan.note)}"' if _says(scan.note) else ""))  # the carrier's words
    return scan, lines


def _relative(delivered: datetime, registered: datetime) -> str:
    days = (_utc(registered).date() - _utc(delivered).date()).days
    if days == 0:
        return "on the day of delivery"
    return f"{_days(abs(days))} {'after' if days > 0 else 'before'} delivery"


def _orders_of(store: Store, order: Order) -> list[Order]:
    """The customer's own orders, oldest first, the disputed one included. An order on the same card by someone else isn't
    theirs, so a sign-up is never tied to the customer through it: _strangers() has those, and the lines name them apart."""
    return _oldest_first(other for other in store.orders if other.customer_id == order.customer_id)


def _on_card(store: Store, order: Order) -> list[Order]:
    """Every order paid with the disputed order's card, oldest first, the disputed one included. A card with no
    fingerprint on record, however the blank is written, is no card, so it ties nothing."""
    card = _clean(order.card_fingerprint)
    return _oldest_first(other for other in store.orders if _says(card) and _clean(other.card_fingerprint) == card)


def _strangers(store: Store, order: Order) -> list[Order]:
    """The orders paid with the disputed order's card from another customer's account, oldest first."""
    return [other for other in _on_card(store, order) if other.customer_id != order.customer_id]


def _oldest_first(orders: Iterable[Order]) -> list[Order]:
    return sorted(orders, key=lambda other: (_utc(other.placed_at), _clean(other.number)))  # a tie in time can't reorder


def _email(store: Store, order: Order, history: list[Order], email: str) -> str:
    """Whether the sign-up's email is one the store had for this customer: on which of their orders and when, or on their
    account. "Since" an order only when every order from that one on has it. Other customers' orders on this card with the
    same email are named apart, and so is a fraud dispute on any order the line names."""
    if not _says(email):
        return "It has no email."
    said = f"It used {_clean(email)}"
    used = [index for index, other in enumerate(history) if _key(other.email) == _key(email)]
    tied = [history[index] for index in used]
    if len(used) > 1 and used == list(range(used[0], len(history))):
        line = f"{said}, the email on the customer's orders since order {_dated(tied[:1])}."
    elif used:
        line = f"{said}, the email on the customer's {'orders' if len(used) > 1 else 'order'} {_dated(tied)}."
    elif _key(store.customer(order.customer_id).email) == _key(email):
        line = f"{said}, the email on the customer's account."
    else:
        line = f"{said}, which isn't on the customer's account or orders."
    others = [other for other in _strangers(store, order) if _key(other.email) == _key(email)]
    on_card = f" Orders on this card by other customers, with the same email: {_dated(others)}."
    return line + _fraud(order, tied) + (on_card + _fraud(order, others) if others else "")


def _texted(order: Order, phone: str | None) -> str:
    if not _says(phone):
        return "It wasn't confirmed by a text message."
    if _digits(phone) and _digits(phone) == _digits(order.cardholder_phone):
        return (f"A code sent by text message to {_clean(phone)}, the cardholder's phone on order "
                f"#{_clean(order.number)}, confirmed it.")
    return f"A code sent by text message to {_clean(phone)} confirmed it."


def _source(store: Store, order: Order, history: list[Order], entry: WarrantyRegistration) -> str:
    """Where the sign-up came from, against the customer's orders, then other customers' orders on this card. A device or
    an IP is the one an order has however either is written, as the CE 3.0 check reads them. "None came from it" is said
    only of the orders that show such a detail, and those that show none are named, the disputed one above all: the
    sign-up can't be shown independent of a device that isn't on record. A fraud dispute on any order the line names is
    said too."""
    device, ip = _device(entry.device_id), _ip(entry.ip_address)
    if not (device or ip):
        return "The registration doesn't show its device or IP address."
    said = _join(part for part in (f"device {_clean(entry.device_id)}" if device else "",
                                   f"IP address {_clean(entry.ip_address)}" if ip else "") if part)
    line = _stop(f"It came from {said}")
    by_device, by_ip, tied = _same(history, device, ip)
    mentioned: list[Order] = []  # the orders named for showing no device or IP
    if tied:
        line += f" The customer's {_from_same(by_device, by_ip, 'orders ')}."
    else:  # each detail by the orders that show none of it: no order, some of them, or all
        groups: dict[tuple[int, ...], list[tuple[str, str]]] = {}  # by place in history: an order may not hash
        for that, none, has, read, field in (("that device", "device", device, _device, "device_id"),
                                             ("that IP address", "buyer's IP address", ip, _ip, "ip_address")):
            if has:
                lacking = tuple(index for index, other in enumerate(history) if not read(getattr(other, field)))
                groups.setdefault(lacking, []).append((that, none))
        for lacking, parts in sorted(groups.items(), key=lambda group: len(group[0])):
            that, none = (" or ".join(names) for names in zip(*parts))
            named = [history[index] for index in lacking]
            if not lacking:
                line += f" None of the customer's orders came from {that}."
            elif len(lacking) < len(history):
                shows = f"Orders {_dated(named)} show" if len(named) > 1 else f"Order {_dated(named)} shows"
                line += f" {shows} no {none}, and none of the customer's other orders came from {that}."
                mentioned += named
            else:
                line += f" The customer's orders show no {none} to compare it with."
    by_device, by_ip, others = _same(_strangers(store, order), device, ip)
    on_card = f" Orders on this card by other customers, {_from_same(by_device, by_ip, '')}."
    return line + _fraud(order, [*tied, *mentioned]) + (on_card + _fraud(order, others) if others else "")


def _same(orders: list[Order], device: str, ip: str) -> tuple[list[Order], list[Order], list[Order]]:
    """Of these orders: those from the device, those from the IP, and those from either."""
    by_device = [other for other in orders if device and _device(other.device_id) == device]
    by_ip = [other for other in orders if ip and _ip(other.ip_address) == ip]
    numbers = {other.number for other in (*by_device, *by_ip)}
    return by_device, by_ip, [other for other in orders if other.number in numbers]


def _from_same(by_device: list[Order], by_ip: list[Order], lead: str) -> str:
    parts = [f"{lead}from the same device: {_dated(by_device)}" if by_device else "",
             f"{lead}from the same IP address: {_dated(by_ip)}" if by_ip else ""]
    return "; ".join(part for part in parts if part)


def _fraud(order: Order, tied: Iterable[Order]) -> str:
    """A fraud dispute the store's records show on an order a line names, read as the CE 3.0 check reads one. To the bank
    such an order is someone else's, so CE 3.0 never cites one, and a tie through it says so. The disputed order's own
    dispute is this case, so it isn't named."""
    marked = list({other.number: other for other in tied if other.number != order.number
                   and (other.prior_dispute or "").strip().upper() == "FRAUD"}.values())  # each once, dated
    if len(marked) > 1:
        return f" The store's records show fraud disputes on orders {_dated(marked)}."
    return f" The store's records show a fraud dispute on order {_dated(marked)}." if marked else ""


# ----------------------------------------------------------------------------- how values are written
def _items(order: Order) -> str:
    """The order's items in a fixed order, so the same items in another order on record give the same text."""
    def one(item) -> str:
        text = _clean(item.description) + (f", serial {_clean(item.serial)}" if _says(item.serial) else "")
        return text if item.quantity == 1 else f"{item.quantity} x {text}"
    items = sorted(order.items, key=lambda item: (_clean(item.sku), _clean(item.description), _clean(item.serial),
                                                  item.quantity, str(item.unit_price)))
    return "; ".join(map(one, items)) or "no items"


def _amount(order: Order) -> str:
    """The order's total, added up exactly, so the order its items come back in can't round it another way."""
    try:
        with localcontext() as context:
            context.prec = MAX_PREC  # Order.amount adds up in the current context
            amount = order.amount
    except ArithmeticError:  # a signalling NaN in a price can't even be added up
        raise PersonWritesFile(NOT_A_NUMBER) from None
    return _money(order.currency, amount)


def _money(currency: object, amount: Decimal) -> str:
    """Every decimal the record has, and at least two: KWD 380.125 isn't rounded to 380.12."""
    try:
        places = max(2, -amount.normalize().as_tuple().exponent) if amount.is_finite() else None
    except ArithmeticError:  # past decimal's range
        places = None
    if places is None:
        raise PersonWritesFile(NOT_A_NUMBER)
    return f"{_clean(currency)} {amount:,.{places}f}"


def _card(order: Order) -> str:
    key = _key(order.card_brand).replace(" ", "_")
    brand = BRANDS.get(key, _clean(order.card_brand).replace("_", " ").title()) if key else "a card"
    return f"{brand} ending {_clean(order.card_last4)}" if _says(order.card_last4) else brand


def _day(moment: datetime) -> str:
    return _utc(moment).date().isoformat()


def _utc(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("Every date a file shows needs a time zone.")
    return moment.astimezone(timezone.utc)


def _days(days: int) -> str:
    return f"{days} day{'' if days == 1 else 's'}"


def _join(parts: Iterable[str]) -> str:
    parts = list(parts)
    return ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else "".join(parts)


def _numbers(numbers: list[str]) -> str:
    return _join(f"#{_clean(number)}" for number in numbers)


def _dated(orders: list[Order]) -> str:
    """Orders with the day each was placed, so the bank sees which came before what."""
    return _join(f"#{_clean(other.number)} ({_day(other.placed_at)})" for other in orders)


def _notes(text: str) -> str:
    return text[:MAX_NAME].rstrip()


def _clean(value: object) -> str:
    """A record's value as one line: the same characters in their composed form, every run of spaces or breaks one space."""
    return " ".join(unicodedata.normalize("NFC", "" if value is None else str(value)).split())


def _says(value: object) -> bool:
    """Whether a value says anything: blanks and what stores write for nothing don't, however they're written. The CE 3.0
    check reads them the same way (ce3.says_nothing)."""
    return not says_nothing(_clean(value))


def _words(value: object) -> str:
    """A value's letters and digits, joined up in lower case: "Unknown." is "unknown"."""
    return "".join(re.findall(r"[^\W_]+", _clean(value).casefold()))


def _quoted(value: object) -> str:
    """A record's own words as the file quotes them: one line, with every quotation mark in them a single one."""
    return re.sub("'{2,}", "'", _clean(value).translate(QUOTES))


def _stop(text: str) -> str:
    """A sentence that ends in a record's value: a full stop, unless the value brings its own."""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _key(value: object) -> str:
    return _clean(value).casefold() if _says(value) else ""


def _address(value: object) -> str:
    return _key(value)


def _digits(value: object) -> str:
    return re.sub(r"\D", "", _clean(value))


def _nobody(signer: object) -> bool:
    """Whether a signer's field holds a carrier's words for nobody: a listed phrase however it's written, or a value with
    a NO_SIGNER word. Words are runs of letters and digits, so "C-19" is c and 19, and joined up, c19."""
    words = re.findall(r"[^\W_]+", _key(signer))
    return "".join(words) in NOBODY or not NO_SIGNER.isdisjoint(words)


def _device(value: object) -> str:
    """A device id as it's compared, or "" when it says nothing. A UUID is the same however it's written, as the CE 3.0
    check reads it, and any other id ignores letter case, so the file never misses a device the check would match."""
    try:
        device = uuid.UUID(_clean(value).rstrip(".!?"))  # a mark left after it isn't part of it, as the check reads it
    except ValueError:
        return _key(value)
    return "" if device.int == 0 else str(device)


def _ip(value: object) -> str:
    """An IP address as it's compared, or "" when it says nothing, however it's written, as the CE 3.0 check reads it. An
    address that's no buyer's, such as a proxy's (ce3.ip_says_nothing), says nothing either."""
    try:
        address = ipaddress.ip_address(_clean(value).rstrip(".!?"))  # "203.0.113.7." is that IP, as the check reads it
    except ValueError:
        return _key(value)
    address = getattr(address, "ipv4_mapped", None) or address
    return "" if ip_says_nothing(address) else str(address)


def _check(text: object) -> None:
    """Text a file can hold: one line, spaced once, in characters Helvetica's WinAnsiEncoding can show."""
    if not isinstance(text, str):
        raise TypeError("A document holds text only.")
    if text != _clean(text) or any(unicodedata.category(char).startswith("C") for char in text):
        raise PersonWritesFile(CANT_SHOW)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        raise PersonWritesFile(CANT_SHOW) from None


# ----------------------------------------------------------------------------- the PDF
def _pdf(title: str, lines: tuple[str, ...]) -> bytes:
    """US Letter pages: the title, then each paragraph wrapped to the margins, and a page number at the foot."""
    characters = len(title) + sum(map(len, lines))  # each takes a byte of the file at least, and each row takes more
    if characters > MAX_PDF_BYTES:  # so it can't fit, and laying it out first would only take time
        raise PersonWritesFile(f"The file would be more than {characters:,} bytes, over the {MAX_PDF_BYTES:,} Visa takes.")
    width = PAGE_WIDTH - 2 * MARGIN
    rows = [(TITLE, 0.0, row) for row in _wrap(title, TITLE[0], width)] + [(TEXT, 0.0, "")]
    for line in lines:
        item = line.startswith(ITEM)
        indent = _width(ITEM, TEXT[0]) if item else 0.0
        wrapped = _wrap(line[len(ITEM):] if item else line, TEXT[0], width - indent) or [""]
        rows += [(TEXT, indent if i else 0.0, ITEM + row if item and not i else row) for i, row in enumerate(wrapped)]
    pages: list[list[tuple[int, float, int, str]]] = [[]]
    top = y = PAGE_HEIGHT - MARGIN
    for (size, height), x, row in rows:
        if y - height < MARGIN:
            pages.append([])
            y = top
        if row or y != top:  # a page never starts with a blank line
            y -= height
            if row:
                pages[-1].append((size, x, y, row))
    count = len(pages)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(count)),
                                                             count),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for number, page in enumerate(pages, start=1):
        shown = [*page, (FOOTER, 0.0, MARGIN // 2, f"Page {number} of {count}")]
        stream = b"\n".join(b"BT /F1 %d Tf %s %d Td (%s) Tj ET" % (size, _point(MARGIN + x), y, _escaped(row))
                            for size, x, y, row in shown)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R >> >> "
                       b"/Contents %d 0 R >>" % (PAGE_WIDTH, PAGE_HEIGHT, len(objects) + 2))
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # the second line marks the file as binary for tools that check
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)  # each entry is exactly 20 bytes
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    if len(out) > MAX_PDF_BYTES:
        raise PersonWritesFile(f"The file would be {len(out):,} bytes, over the {MAX_PDF_BYTES:,} Visa takes.")
    return bytes(out)


def _wrap(text: str, size: int, width: float) -> list[str]:
    """Rows that fit the width, broken at spaces. A word wider than a row is cut where it runs out. A row's width is added
    up as it grows, so each word is measured once, not the whole row again for every word."""
    rows: list[str] = []
    row, used = "", 0  # used: the row's width, as _units counts it
    for word in text.split(" "):
        start = 0
        while (end := _fits(word, start, size, width)) < len(word):  # too wide: what fits goes on a row of its own
            if row:
                rows.append(row)
                row, used = "", 0
            rows.append(word[start:end])
            start = end
        word = word[start:]
        wide = _units(word)
        if row and (used + WIDTHS[32] + wide) * size / 1000 <= width:  # what _width(f"{row} {word}", size) says
            row, used = f"{row} {word}", used + WIDTHS[32] + wide
        else:
            if row:
                rows.append(row)
            row, used = word, wide
    return rows + [row] if row else rows


def _fits(word: str, start: int, size: int, width: float) -> int:
    """Where a row of the word that begins at start ends: after all that fits, and at least one character. Each
    character is read once, so a long word costs its length."""
    total = 0
    for end in range(start, len(word)):
        total += WIDTHS.get(word[end].encode("cp1252")[0], 1000)  # one byte per character in cp1252
        if total * size / 1000 > width:  # the sum _width makes, so a row fits exactly when _width says it does
            return max(end, start + 1)
    return len(word)


def _width(text: str, size: int) -> float:
    return _units(text) * size / 1000


def _units(text: str) -> int:
    """A text's width in thousandths of the font size: a whole number, so adding up parts gives what the whole gives."""
    return sum(WIDTHS.get(byte, 1000) for byte in text.encode("cp1252"))


def _point(value: float) -> bytes:
    return f"{value:.2f}".rstrip("0").rstrip(".").encode("ascii")


def _escaped(text: str) -> bytes:
    """A PDF string's inside: brackets and backslashes escaped, any byte outside printable ASCII as octal."""
    out = bytearray()
    for byte in text.encode("cp1252"):
        if byte in b"()\\":
            out += b"\\%c" % byte
        elif 32 <= byte < 127:
            out.append(byte)
        else:
            out += b"\\%03o" % byte
    return bytes(out)


# ----------------------------------------------------------------------------- read the same way elsewhere
# The Claude layer's rules read a value as the files do, so a rule and a file never disagree about what a record says.
clean, says, key, address, digits = _clean, _says, _key, _address, _digits
device, ip, utc, nobody = _device, _ip, _utc, _nobody


def delivery_scan(store: Store, order: Order) -> Scan | None:
    """The carrier's last delivery of the order, the one the files show, or None when it has none."""
    return _delivery(store, order)[0]


def signer(value: object) -> str | None:
    """Who signed, as the files show it, or None: a blank, a placeholder and a carrier's words for nobody name no one."""
    return _clean(value) if _says(value) and not _nobody(value) else None
