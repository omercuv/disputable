"""Code's evidence rules: what the store's records prove for each claim, stage and round, with no model.

Code reads structured records and concludes the evidence class, the fact ids, the files and its own conflicts. Claude's
reading never becomes a fact id, a file or a reason here: the round orchestrator (rounds.py) merges what Claude found
after code's lines, and only toward a person. Pure code: no clock, no network and no model.

Every sentence here is a constant or a template filled from structured fields: days, counts, record ids and order
numbers. So no reason or finding holds text that a customer, carrier, warehouse or bank wrote, and the same records always
give the same reasons, which keep() needs: a changed reason makes a new plan and voids its approval. "Latest" is the
greatest (UTC time, id), so the order the records come in can't change anything. Values are read the way the evidence
files read them (documents.clean, key, address, device, ip, signer), so a rule and a file never disagree about a record.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Callable, Iterable, Sequence

from disputable import ce3, documents
from disputable.ce3 import Ce3Result
from disputable.documents import Document
from disputable.engine import Action, Evidence, Stage
from disputable.fees import EvidenceClass, FeeTable
from disputable.store import Case, Order, Scan, Store, WarrantyRegistration, mailbox  # the inbox reads senders so too

WAIT_DAYS = 15  # Visa 13.6: the cardholder waits 15 calendar days after the store got the return back
LABEL = "Claude found: "  # starts every reason that rests on Claude's reading
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # not strftime: locale
FRAUD, NOT_RECEIVED, CREDIT = "10.4", "13.1", "13.6"  # the claims fees-v1 covers, and the only ones with rules here
STAGES = (Stage.RFI, Stage.CHARGEBACK)  # the stages fees-v1 has a cost model for

# ----------------------------------------------------------------------------- conflicts: each sends the round to a person
# 13.6, credit not processed
RETURN_UNSCANNED = "The bank says the item came back, but the carrier has no return scan."  # R1
RETURN_ON_ITS_WAY = "The carrier has the return on its way, but no scan of it reaching the store."  # R2
RETURN_NOT_DELIVERED = "The warehouse logged the return, but the carrier has no scan of it reaching the store."  # R4
RETURN_TURNED_DOWN = "The warehouse turned the return down, so the store may not owe a credit."  # R7
RETURN_STATUS_UNKNOWN = "The warehouse logged the return with a status this code doesn't know."  # R8


def return_not_logged(arrived: datetime) -> str:  # R3
    return f"The carrier says the return arrived on {_day(arrived)}, but the warehouse has no scan of it."


def inside_the_wait(days: int, opened: str) -> str:  # R5
    return f"The return arrived {_days(days)} before the {opened}, inside Visa's {WAIT_DAYS}-day wait."


def after_it_opened(opened: str) -> str:  # R6
    return f"The return arrived after the {opened} opened, so Visa's {WAIT_DAYS}-day wait wasn't met."


# 13.1, not received
DELIVERED_ELSEWHERE = "The carrier delivered to a different address from the one on the order."  # D1
PLACE_UNKNOWN = "The records don't show where the parcel was meant to go or where it went."  # D2
PARCEL_RETURNED = ("The records show a return for this order. A person checks whether the parcel reached the "
                   "customer.")  # D3: a carrier's return scan or the warehouse's

# 10.4, card-absent fraud
PASSED_3DS = "The order passed 3DS, and the fee table has no chance for that."  # F1
OTHER_EMAIL_NO_SMS = ("The warranty sign-up used another email and no SMS check, so it can't be tied to the "
                      "cardholder.")  # W1
NO_SMS = "The warranty sign-up had no SMS check, so it can't be tied to the cardholder."  # W2
EMAIL_NOT_ON_FILE = ("The warranty sign-up's text went to the cardholder's phone, but it used an email that isn't on "
                     "file.")  # W3
# Phones are compared digit for digit, so "other" says only that the numbers differ: the same number written with and
# without its country code differs too, and a person compares them.
OTHER_PHONE = ("The warranty sign-up's text went to a number that doesn't match the cardholder's phone on the order, so "
               "it can't be tied to them.")  # W4
OTHER_EMAIL_AND_PHONE = ("The warranty sign-up used another email, and its text went to a number that doesn't match the "
                         "cardholder's phone on the order, so it can't be tied to them.")  # W5
NO_EMAIL_NO_SMS = "The warranty sign-up has no email and no SMS check, so it can't be tied to the cardholder."  # W8
NO_EMAIL = "The warranty sign-up's text went to the cardholder's phone, but it has no email."  # W9
NO_EMAIL_OTHER_PHONE = ("The warranty sign-up has no email, and its text went to a number that doesn't match the "
                        "cardholder's phone on the order, so it can't be tied to them.")  # W10
NO_NUMBER = ("The warranty sign-up's text, or the cardholder's phone on the order, shows no number, so the sign-up "
             "can't be tied to the cardholder.")  # W11
OTHER_EMAIL_NO_NUMBER = ("The warranty sign-up used another email, and its text, or the cardholder's phone on the "
                         "order, shows no number, so it can't be tied to them.")  # W12
NO_EMAIL_NO_NUMBER = ("The warranty sign-up has no email, and its text, or the cardholder's phone on the order, shows "
                      "no number, so it can't be tied to them.")  # W13
UNTIED = MappingProxyType({  # by (the email the sign-up used: see _emailed, where its text message went: see _texted)
    ("other", "none"): OTHER_EMAIL_NO_SMS, ("on_file", "none"): NO_SMS, ("other", "yes"): EMAIL_NOT_ON_FILE,
    ("on_file", "other"): OTHER_PHONE, ("other", "other"): OTHER_EMAIL_AND_PHONE, ("none", "none"): NO_EMAIL_NO_SMS,
    ("none", "yes"): NO_EMAIL, ("none", "other"): NO_EMAIL_OTHER_PHONE, ("on_file", "unknown"): NO_NUMBER,
    ("other", "unknown"): OTHER_EMAIL_NO_NUMBER, ("none", "unknown"): NO_EMAIL_NO_NUMBER})
REGISTERED_BEFORE_DELIVERY = ("The serial was registered for warranty before the carrier delivered the order, so the "
                              "sign-up can't show who received it.")  # W6
NO_DELIVERY_TO_COMPARE = ("The carrier has no delivery scan for the order, so the warranty sign-up can't be placed after "
                          "delivery.")  # W7
# X1 is the message of documents.PersonWritesFile: a record holds what the file can't show, so a person writes it.


# ----------------------------------------------------------------------------- findings: beside the plan, never in it
NO_SERIAL = "The disputed order has no serial number, so no warranty sign-up can show who has it."  # P0
NO_SIGN_UP = "No warranty sign-up is on file for the serial on the disputed order."  # P1


def sign_up_turned_down(sign_up_id: str) -> str:  # P2
    return f"Warranty sign-up {sign_up_id} was in the package the bank turned down, so it isn't new."


def sign_up_shares_a_source(sign_up_id: str, number: str) -> str:  # P3
    return (f"Warranty sign-up {sign_up_id} came from a device or an IP that order #{number} used, so it isn't "
            "independent proof.")


def sign_up_shows_no_source(sign_up_id: str) -> str:  # P4
    return (f"Warranty sign-up {sign_up_id}, or the disputed order, shows no device or IP to compare, so the sign-up "
            "isn't proof.")


def sign_up_is_proof(sign_up_id: str) -> str:  # P5
    return (f"Warranty sign-up {sign_up_id} is tied to the cardholder and came from a device and an IP no order in the "
            "history used.")


def no_delivery(number: str) -> str:  # N1
    return f"The carrier has no delivery scan for order #{number}."


def delivered_unsigned(number: str, delivered: datetime) -> str:  # N2
    return f"The carrier delivered order #{number} on {_day(delivered)} to the address on the order, with no signature."


def delivered_signed(number: str, delivered: datetime) -> str:  # N3
    return (f"The carrier delivered order #{number} on {_day(delivered)} to the address on the order, and someone "
            "signed for it.")


def return_delivered(arrived: datetime, days: int | None, opened: str) -> str:  # C1. days is None: after it opened
    when = f"after the {opened} opened" if days is None else f"{_days(days)} before the {opened}"
    return f"The carrier delivered the return to the store on {_day(arrived)}, {when}."


def return_received(logged: datetime) -> str:  # C2
    return f"The warehouse logged the return as received on {_day(logged)}."


def return_rejected(logged: datetime) -> str:  # C3
    return f"The warehouse turned the return down on {_day(logged)}."


def no_return_scan(number: str) -> str:  # C4
    return f"The carrier has no return scan for order #{number}."


class NoRules(ValueError):
    """A claim or stage this module has no rules for. decide()'s first call already hands every other one off."""


@dataclass(frozen=True)
class Conclusion:
    evidence: Evidence  # class, fact ids, file hashes, and code's conflicts in their final order (ce3's first)
    files: tuple[Document, ...]  # exactly the documents behind evidence.file_hashes
    findings: tuple[str, ...]  # plain lines for the person: never in the reasons, never holding record text
    check: Ce3Result | None  # the CE 3.0 check, when ce3.consulted() said to run it: for the screen and the eval


def evaluate(store: Store, case: Case, reported: Sequence[str] = ()) -> Conclusion:
    """What the records prove for this round, by code alone. Call it only once decide()'s first call has said
    GATHER_EVIDENCE: that call hands off every claim and stage the fee table doesn't cover.

    case is lifecycle.as_case(record) in the app, so a later round counts as one and the days count from the first
    round's date. reported is a list or tuple of the order numbers Claude says the bank's comment reports, [] for none:
    CE 3.0 counts only those its comment names. Raises NoRules for any other claim or stage, and lets NoFileDue through:
    correct rules never ask for a file that isn't due, so it means a bug, and a person takes the round.
    """
    if isinstance(reported, str):
        raise TypeError("reported holds order numbers; one string would be read a character at a time")
    if not isinstance(reported, (list, tuple)):
        raise TypeError("reported is a list or tuple of order numbers")
    if case.reason_code not in (FRAUD, NOT_RECEIVED, CREDIT):
        raise NoRules(f"Code has no evidence rules for claim {case.reason_code or 'none'}.")
    if case.stage not in STAGES:
        raise NoRules(f"Code has no evidence rules for the {case.stage.value} stage.")
    order = store.order(case.order_number)
    if case.reason_code == NOT_RECEIVED:
        return _not_received(store, order)
    if case.reason_code == CREDIT:
        return _credit(store, case, order)
    return _fraud(store, case, order, list(reported))


def customer_emails(store: Store, order: Order) -> frozenset[str]:
    """The emails the store knows the order's customer by, as mailbox() reads them: the account's, and the one on each
    of the customer's own orders. An order on the same card by someone else isn't theirs, as the files say."""
    own = (other.email for other in store.orders if other.customer_id == order.customer_id)
    return frozenset(email for email in map(mailbox, (store.customer(order.customer_id).email, *own)) if email)


def return_turned_down(store: Store, case: Case) -> bool:
    """Whether the warehouse's latest scan of the return turned it down: the only proof a credit isn't owed."""
    logged = _logged_return(store, case.order_number)
    return logged is not None and documents.clean(logged.status) == "RETURN_REJECTED"


def check_table(fees: FeeTable) -> None:
    """Refuse a fee table that prices UNPRICED_PROOF. That class stands for proof no table has a chance for, so decide()
    hands it off with nothing drafted, and the Claude layer gives it to a checked withdrawal for that reason. A chance
    or a challenge reason for it would let a draft through."""
    priced = [name for name, table in (("rfi_escalation_chance", fees.rfi_escalation_chance),
                                       ("chargeback_win_chance", fees.chargeback_win_chance),
                                       ("challenge_reasons", fees.challenge_reasons))
              if EvidenceClass.UNPRICED_PROOF in table]
    if priced:
        raise ValueError(f"{fees.version} prices UNPRICED_PROOF in {' and '.join(priced)}, but that class must have no "
                         "chance and no challenge reason, so that nothing is drafted for it.")


# ----------------------------------------------------------------------------- 13.1, not received
def _not_received(store: Store, order: Order) -> Conclusion:
    """The class comes from the latest delivery alone: at the order's address and signed for, it's proof the table can't
    price. Each doubt about where the parcel went is a conflict beside the class, whatever the latest delivery shows."""
    scans = store.get_carrier_scans(order.number)
    deliveries = [scan for scan in scans if _delivered(scan, "outbound")]
    to = documents.address(order.shipping_address)
    number = documents.clean(order.number)
    evidence, lines, findings = Evidence(EvidenceClass.NONE), [], []
    if deliveries:
        places = [documents.address(scan.location) for scan in deliveries]
        if to and any(place and place != to for place in places):
            lines.append(DELIVERED_ELSEWHERE)
        if not (to and all(places)):
            lines.append(PLACE_UNKNOWN)
        latest = documents.delivery_scan(store, order)  # the same scan, by the same test, as the files show
        if to and documents.address(latest.location) == to:
            if documents.signer(latest.signed_by) is not None:  # only signed_by shows a signature, never a note
                evidence = Evidence(EvidenceClass.UNPRICED_PROOF, frozenset({latest.id}))
                findings.append(delivered_signed(number, latest.at))
            else:
                findings.append(delivered_unsigned(number, latest.at))
    else:
        findings.append(no_delivery(number))
    if any(scan.direction == "return" for scan in scans):  # with or without a delivery: a person sees it
        lines.append(PARCEL_RETURNED)
    return Conclusion(replace(evidence, conflicts=tuple(lines)), (), tuple(findings), None)


# ----------------------------------------------------------------------------- 13.6, credit not processed
def _credit(store: Store, case: Case, order: Order) -> Conclusion:
    """A plain accept only when the carrier delivered the return, the warehouse logged it as received, and Visa's wait
    is over. Otherwise the lines stack in a fixed order: a turned-down return, where the return is, when it arrived.
    Days are UTC calendar days from the return's delivery to the day the dispute opened, the first round's in a later
    round. A return delivered after that moment didn't wait at all."""
    returns = [scan for scan in store.get_carrier_scans(order.number) if scan.direction == "return"]
    carrier = [scan for scan in returns if scan.source == "carrier"]
    arrived = [scan for scan in carrier if _delivered(scan, "return")]
    warehouse = [scan for scan in returns if scan.source == "warehouse"]
    logged = _logged_return(store, order.number)
    status = documents.clean(logged.status) if logged else ""
    opened = _opened(case)
    evidence, lines, findings = Evidence(EvidenceClass.NONE), [], []
    if status == "RETURN_REJECTED":  # the same test as return_turned_down()
        evidence = Evidence(EvidenceClass.UNPRICED_PROOF, frozenset({logged.id}))
        lines.append(RETURN_TURNED_DOWN)
    if not arrived:
        lines.append(RETURN_NOT_DELIVERED if warehouse else RETURN_ON_ITS_WAY if carrier else RETURN_UNSCANNED)
        if not carrier:
            findings.append(no_return_scan(documents.clean(order.number)))
    else:
        came = _latest(arrived)
        if not warehouse:
            lines.append(return_not_logged(came.at))
        elif status not in ("RETURN_RECEIVED", "RETURN_REJECTED"):
            lines.append(RETURN_STATUS_UNKNOWN)
        if documents.utc(came.at) > documents.utc(case.opened_at):  # it didn't wait at all
            lines.append(after_it_opened(opened))
            findings.append(return_delivered(came.at, None, opened))
        else:
            days = (documents.utc(case.opened_at).date() - documents.utc(came.at).date()).days
            if days <= WAIT_DAYS:
                lines.append(inside_the_wait(days, opened))
            findings.append(return_delivered(came.at, days, opened))
    if status == "RETURN_RECEIVED":
        findings.append(return_received(logged.at))
    elif status == "RETURN_REJECTED":
        findings.append(return_rejected(logged.at))
    return Conclusion(replace(evidence, conflicts=tuple(lines)), (), tuple(findings), None)


# ----------------------------------------------------------------------------- 10.4, card-absent fraud
def _fraud(store: Store, case: Case, order: Order, reported: list[str]) -> Conclusion:
    """CE 3.0 in the first round, a warranty sign-up at CHARGEBACK, and 3DS in any round.

    The table prices possession at CHARGEBACK and not CE 3.0, so a qualifying sign-up replaces CE 3.0's package there,
    and CE 3.0's conflicts drop out with it. In a later round ce3.evidence() gives nothing, and the check runs only when
    the bank reports an order, for the findings. 3DS gives the fee table nothing to price, so its proof counts only
    where nothing else does, and a priced package keeps its class and goes to a person."""
    check, findings = None, []
    if ce3.consulted(case, Action.GATHER_EVIDENCE, reported):
        check = ce3.check(store, case, reported)
        findings += check.reasons
    sign_up, untied = None, []
    if case.stage is Stage.CHARGEBACK:
        sign_up, untied, notes = _possession(store, case, order)
        findings += notes
    package = ce3.evidence(check, case) if check is not None else Evidence(EvidenceClass.NONE)
    evidence, files, first = Evidence(EvidenceClass.NONE), (), ()
    if sign_up is not None:
        evidence, files, refusal = _with_file(
            Evidence(EvidenceClass.INDEPENDENT_POSSESSION, frozenset({sign_up.id})),
            lambda: documents.warranty_registration(store, case, sign_up.id))
        lines = [*untied, *refusal]
    elif package.evidence_class is EvidenceClass.CE3_QUALIFIED:
        first = package.conflicts  # ce3's own lines come first, unchanged
        evidence, files, refusal = _with_file(replace(package, conflicts=()),
                                              lambda: documents.purchase_history(store, case, check))
        lines = [*refusal, *untied]
    else:
        lines = list(untied)
    if order.three_ds:
        lines.append(PASSED_3DS)
        if evidence.evidence_class is EvidenceClass.NONE:
            evidence = Evidence(EvidenceClass.UNPRICED_PROOF, frozenset({order.id}))
    return Conclusion(replace(evidence, conflicts=tuple(dict.fromkeys((*first, *lines)))), files, tuple(findings), check)


def _possession(store: Store, case: Case, order: Order) -> tuple[WarrantyRegistration | None, list[str], list[str]]:
    """The possession rule: the first warranty sign-up that is new, tied to the cardholder, placed after the delivery and
    independent of every order in the history. The candidates are the sign-ups for the serials on the disputed order,
    oldest first: a serial elsewhere in the history is never one. For each, the first test it fails decides. The tie
    comes before independence, so an untied sign-up goes to a person whatever its device. One that is tied but not
    independent simply isn't proof. Returns the sign-up or None, the conflicts (W1 to W13) and the findings (P0 to P5)."""
    serials = sorted({item.serial for item in order.items if documents.says(item.serial)})
    if not serials:
        return None, [], [NO_SERIAL]
    entries = {entry.id: entry for serial in serials for entry in store.get_warranty_log(serial)}
    if not entries:
        return None, [], [NO_SIGN_UP]
    emails, delivery = customer_emails(store, order), documents.delivery_scan(store, order)
    history = store.get_order_history(order.number)
    chosen, conflicts, findings = None, [], []
    for entry in sorted(entries.values(), key=lambda entry: (documents.utc(entry.registered_at), entry.id)):
        if entry.id in case.rejected_fact_ids:  # built again, its file could pass for a new one
            findings.append(sign_up_turned_down(entry.id))
            continue
        tie = (_emailed(entry, emails), _texted(entry, order))
        if tie != ("on_file", "yes"):
            conflicts.append(UNTIED[tie])
            continue
        if delivery is None:
            conflicts.append(NO_DELIVERY_TO_COMPARE)
            continue
        if documents.utc(entry.registered_at).date() < documents.utc(delivery.at).date():  # the same day counts as after
            conflicts.append(REGISTERED_BEFORE_DELIVERY)
            continue
        device, ip = documents.device(entry.device_id), documents.ip(entry.ip_address)
        if not (device and ip and documents.device(order.device_id) and documents.ip(order.ip_address)):
            findings.append(sign_up_shows_no_source(entry.id))
            continue
        shared = [other for other in history
                  if documents.device(other.device_id) == device or documents.ip(other.ip_address) == ip]
        if shared:  # an order that shows no device or IP never blocks: "" equals neither
            first = min(shared, key=lambda other: (documents.utc(other.placed_at), documents.clean(other.number)))
            findings.append(sign_up_shares_a_source(entry.id, documents.clean(first.number)))
            continue
        findings.append(sign_up_is_proof(entry.id))
        chosen = chosen or entry
    return chosen, conflicts, findings


def _emailed(entry: WarrantyRegistration, emails: frozenset[str]) -> str:
    """The email the sign-up used: "on_file" (one the store knows the customer by), "other", or "none" when it has none."""
    if not documents.says(entry.email):
        return "none"
    return "on_file" if mailbox(entry.email) in emails else "other"


def _texted(entry: WarrantyRegistration, order: Order) -> str:
    """Where the sign-up's text message went: "none" (no SMS check), "yes" (the cardholder's phone on the disputed
    order, digit for digit), "other" (another number), or "unknown" when there's no pair of numbers to compare: the text
    or the cardholder's phone on the order shows no digits, or only zeros ("000-000-0000" is a placeholder). Two blanks
    are never the same phone."""
    if not documents.says(entry.sms_verified_phone):
        return "none"
    phone, cardholder = _number(entry.sms_verified_phone), _number(order.cardholder_phone)
    if not (phone and cardholder):
        return "unknown"
    return "yes" if phone == cardholder else "other"


def _number(phone: str | None) -> str:
    """A phone's digits, or "" when its last seven are all zeros, in any script: "000-000-0000" and "+1 000-000-0000" are
    placeholders, and no real number ends in seven zeros."""
    digits = documents.digits(phone)
    return digits if any(unicodedata.digit(char, 0) for char in digits[-7:]) else ""


def _with_file(evidence: Evidence,
               build: Callable[[], Document]) -> tuple[Evidence, tuple[Document, ...], tuple[str, ...]]:
    """The package with its file, or with no file and the reason a person writes it. pdf() runs here, once, so Visa's
    size limit is checked at DECIDE, not at upload. NoFileDue isn't caught: correct rules never meet it, so it's a bug."""
    try:
        document = build()
        document.pdf()
    except documents.PersonWritesFile as refusal:
        return evidence, (), (str(refusal),)  # X1
    return documents.with_files(evidence, document), (document,), ()


# ----------------------------------------------------------------------------- how records and days are read
def _delivered(scan: Scan, direction: str) -> bool:
    """A carrier's delivery, by the test documents makes for the files' delivery line."""
    return scan.source == "carrier" and scan.direction == direction and scan.status == "DELIVERED"


def _logged_return(store: Store, order_number: str) -> Scan | None:
    """The warehouse's latest scan of the return, or None when it logged none."""
    logged = [scan for scan in store.get_carrier_scans(order_number)
              if scan.source == "warehouse" and scan.direction == "return"]
    return _latest(logged) if logged else None


def _latest(scans: Iterable[Scan]) -> Scan:
    return max(scans, key=lambda scan: (documents.utc(scan.at), scan.id))


def _opened(case: Case) -> str:
    """What opened, as the 15-day lines name it: the chargeback only in a first round at that stage. Later, the days
    count from the first round's date, which was the dispute's, not the chargeback's."""
    return "chargeback" if case.stage is Stage.CHARGEBACK and not ce3.later_round(case) else "dispute"


def _day(moment: datetime) -> str:
    """A UTC calendar day, written "Oct 5"."""
    day = documents.utc(moment)
    return f"{MONTHS[day.month - 1]} {day.day}"


def _days(days: int) -> str:
    return f"{days} day{'' if days == 1 else 's'}"
