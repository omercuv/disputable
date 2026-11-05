"""Visa's Compelling Evidence 3.0 check, run on the store's own records.

CE 3.0 moves a card-absent fraud claim (10.4) back to the issuer when two earlier orders on the same card were placed 120 to
365 days before the dispute, have no fraud on them, and each match the disputed order on two of four details: account,
delivery address, device and IP, at least one of them device or IP.

The rules were read on 2026-11-01 from Visa's March 2023 merchant papers and two Airwallex pages; the Visa Rules govern over
all of them. The sources disagree on three points and none covers a fourth, so the check answers under every reading and
says pass or fail only when all of them agree. Anything else is unclear, and a person decides. So is a pass that the bank's
comment or the card's brand doesn't line up with.

Pure code: no clock, no network and nothing Claude writes, except which orders the bank reports, and those count only where
the bank's own comment names them. An order's time stands in for Visa's processing date, and days are calendar days in UTC.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations, product
from typing import Iterable, Mapping

from disputable.engine import Action, Dispute, Evidence
from disputable.fees import ZERO, EvidenceClass, usd
from disputable.store import BLANKS, Case, Order, Store, says_nothing  # the files and the reader read the blanks here

FRAUD_CLAIM = "10.4"  # card-absent fraud, the only claim CE 3.0 covers
NEWEST, OLDEST = 120, 365  # how many days before the dispute an earlier order may be placed, both ends included
PRIORS_NEEDED = 2
ELEMENTS = {"account": "account_id", "address": "shipping_address", "device": "device_id", "ip": "ip_address"}
NAMES = {"account": "account", "address": "delivery address", "device": "device", "ip": "IP"}
# Each earlier order must match on one of these: two elements, one of them device or IP.
PAIRS = tuple(frozenset(pair) for pair in combinations(ELEMENTS, 2) if {"device", "ip"} & set(pair))
# Values that say nothing can't match. store.says_nothing() reads them, from store.PLACEHOLDERS.
# Private and shared ranges: a proxy or a load balancer in front of the store has one, and its address is logged for every
# buyer. Not Python's is_private, which also holds the documentation ranges that made-up records give their buyers.
PROXY_RANGES = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                                                 "fc00::/7")))
ORDER_NUMBER = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,23}")  # anything else isn't repeated back to the person
MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?"
         r"|nov(?:ember)?|dec(?:ember)?)\.?")
# Numbers inside dates, times, amounts and decimals aren't order numbers, so the bank's comment is read without them. YEARS
# stands for the years a date about the dispute can carry (see _without_dates), so in a 2026 dispute 2001 is never a year.
NOT_ORDER_NUMBERS = rf"""(?ix)
    # 2026-05-14, 2026/5/14, 14/05/2026, 05-14-2026 and 05/2026, but no list of 4-digit order numbers like 1001/1002.
    # Not 05-2026: a hyphen can be part of an order number, as in SO-05-2026-0042, and a slash can't.
    (?<!\d)(?:\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}} | \d{{1,2}}[-/]\d{{1,2}}[-/]\d{{2,4}} | \d{{1,2}}/(?:YEARS))(?!\d)
  | (?<!\d)\d{{1,2}}:\d\d(?::\d\d)?(?!\d)                                         # 10:42, 10:42:00
  # May 14, 2026, May 14,2026, May 1-14, 2026, May 1st – 14th, 2026, 14-May-2026 and (14) May 2026. Never across a line
  # break, a tab or a spaced hyphen or slash, so an order on the next row, in the next column or after " - " stays an
  # order, and never into a number like 2026-0042 or 2026-1, unless the year is one space, ", " or "-" after its month
  # and a whole next date follows, as in 1 May 2026-31 May 2026: in a row like May 14 2026-1 Jun 2026 it's an order.
  | \b{MONTH}(?:(?:[-/]|,?\x20*)(?:\d{{1,2}}(?:st|nd|rd|th)?(?:\x20*[-–]\x20*\d{{1,2}}(?:st|nd|rd|th)?)?(?:[-/,]|,?\x20+))?
    (?:YEARS)\b(?!-\d) | (?:-|,?\x20)(?:YEARS)(?=-\d\d?(?:st|nd|rd|th)?[-\x20]{MONTH}(?:-|,?\x20*)(?:YEARS)\b))
  # $1,249.00, USD 1,249, 1249.00, CE 3.0. A USD after a number on its line is that number's, so it's dropped and leads
  # nothing, and no currency reads past a line's end.
  | (?<=\d)[^\S\n]+usd\b | (?:[$€£]|\busd)[^\S\n]*\d[\d,]*(?:\.\d+)? | \b\d+(?:\.\d+)+\b
"""
NO_BREAK_SPACES = str.maketrans({"\u00a0": " ", "\u202f": " "})  # read as spaces: text pasted from web pages keeps them


class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCLEAR = "unclear"  # the readings disagree, or the bank's word doesn't line up with the records: a person decides


@dataclass(frozen=True)
class Reading:
    """One way to read the rules. Each field is a question the sources split on or leave open."""
    from_the_order: bool  # Airwallex's CE 3.0 page counts the days from the disputed order; Visa's papers from the dispute
    same_two: bool  # Airwallex's CE 3.0 page wants the same two elements on both orders; Visa's takes any two on each
    disputed_counts: bool  # Visa's FAQ still counts an order with a non-fraud dispute; Airwallex's page wants no dispute
    refunded_counts: bool  # neither says whether a refunded order counts
    unreadable_counts: bool  # a dispute of a kind this code can't read may not have been for fraud


READINGS = tuple(Reading(*answers) for answers in product((False, True), repeat=len(fields(Reading))))


@dataclass(frozen=True)
class Prior:
    """An earlier order on the disputed order's card, measured against the disputed order."""
    order: Order
    days_before_dispute: int  # calendar days
    days_before_order: int
    matches: frozenset[str]  # which of the four elements match the disputed order
    reported: bool  # the bank's comment confirms the report on it

    @property
    def dispute(self) -> str | None:
        return (self.order.prior_dispute or "").strip().upper() or None

    @property
    def matches_enough(self) -> bool:
        return any(pair <= self.matches for pair in PAIRS)

    @property
    def cited(self) -> bool:
        """Whether a package shows it: it matches enough and has no known fraud on it, wherever it falls in the window."""
        return self.matches_enough and not self.reported and self.dispute != "FRAUD"

    def in_window(self, from_the_order: bool) -> bool:
        return NEWEST <= (self.days_before_order if from_the_order else self.days_before_dispute) <= OLDEST

    def counts(self, reading: Reading) -> bool:
        """Whether it can be one of the two, read this way. Whether the two share elements is checked across them."""
        if self.reported or self.dispute == "FRAUD" or not self.in_window(reading.from_the_order):
            return False
        if self.dispute is not None and not reading.disputed_counts:  # a dispute of any kind, on Airwallex's reading
            return False
        if self.dispute not in (None, "NON_FRAUD") and not reading.unreadable_counts:
            return False
        if self.order.refunds and not reading.refunded_counts:
            return False
        return self.matches_enough


@dataclass(frozen=True)
class Ce3Result:
    outcome: Outcome
    priors: tuple[Prior, ...]  # every earlier order on the disputed order's card, oldest first
    fact_ids: frozenset[str]  # what a purchase-history package cites: the disputed order and each cited prior
    reported: frozenset[str]  # the reported orders the bank's comment confirms on this card, for the eval to compare
    reasons: tuple[str, ...]  # what the person reads, the outcome first
    conflicts: tuple[str, ...] = ()  # why it's unclear, for the person who decides


def check(store: Store, case: Case, reported: Iterable[str] | None = ()) -> Ce3Result:
    """CE 3.0 for one fraud claim. reported: the order numbers the bank's comment says carry a fraud report."""
    if case.reason_code != FRAUD_CLAIM:
        raise ValueError(f"CE 3.0 only covers Visa's {FRAUD_CLAIM} fraud claims, not {case.reason_code or 'none'}")
    if isinstance(reported, str):
        raise TypeError("reported holds order numbers; one string would be read a character at a time")
    disputed = store.order(case.order_number)
    if _days(disputed.placed_at, case.opened_at) < 0:
        raise ValueError(f"the dispute opened before order {disputed.number} was placed")
    placed = _utc(disputed.placed_at)
    related = [order for order in store.orders if order.number != disputed.number
               and (_same_card(order, disputed) or order.customer_id == disputed.customer_id)]
    earlier = sorted((order for order in related if _utc(order.placed_at) < placed),
                     key=lambda order: (_utc(order.placed_at), order.number))
    given = {_order_number(str(text)) for text in reported or ()} - {"", disputed.number}
    words = _without_dates(case.issuer_comment, _utc(case.opened_at).year)
    named = {number for number in given if ORDER_NUMBER.fullmatch(number) and _names(words, number)}
    priors = tuple(Prior(order, _days(order.placed_at, case.opened_at), _days(order.placed_at, disputed.placed_at),
                         _matches(order, disputed), order.number in named)
                   for order in earlier if _same_card(order, disputed))

    results = {reading: _met(priors, reading) for reading in READINGS}
    if len(set(results.values())) > 1:
        outcome, conflicts = Outcome.UNCLEAR, _why_unclear(priors, results)
    else:
        outcome, conflicts = (Outcome.PASS if all(results.values()) else Outcome.FAIL), []
    problems = _problems(disputed, priors, given, named, words)
    if problems and outcome is not Outcome.FAIL:
        outcome, conflicts = Outcome.UNCLEAR, [*conflicts, *problems]

    card = _card(disputed)
    reasons = [_summary(outcome, priors, bool(card), _later_round(case)), *map(_line, priors)]
    if card:  # with no card on the disputed order, the summary already says why no earlier order is on it
        other_cards = [order.number for order in earlier if _card(order) and _card(order) != card]
        no_card = [order.number for order in earlier if not _card(order)]
        if other_cards:
            reasons.append(_about(other_cards, "was paid with another card, so CE 3.0 can't use it.",
                                  "were paid with another card, so CE 3.0 can't use them."))
        if no_card:
            reasons.append(_about(no_card, "has no card on file, so CE 3.0 can't tie it to this card.",
                                  "have no card on file, so CE 3.0 can't tie them to this card."))
    others = [prior.order.number for prior in priors if prior.order.customer_id != disputed.customer_id]
    if others:
        reasons.append(_about(others, "was placed by another customer with this card.",
                              "were placed by another customer with this card.")
                       + " CE 3.0 finds earlier orders by card, not by customer.")
    reasons += [_no_match(element, getattr(disputed, field)) for element, field in ELEMENTS.items()
                if not _comparable(element, getattr(disputed, field))]
    if outcome is Outcome.FAIL:
        reasons += problems
    on_card = {prior.order.number for prior in priors}
    return Ce3Result(outcome, priors, frozenset({disputed.id, *(prior.order.id for prior in priors if prior.cited)}),
                     frozenset(named & on_card), tuple(reasons), tuple(conflicts))


def evidence(result: Ce3Result, dispute: Dispute | Case, file_hashes: frozenset[str] = frozenset()) -> Evidence:
    """What the check alone gives the engine. An unclear result is drafted as a fight, and its conflicts send it to a person.

    Visa allows one CE 3.0 attempt, and the first round is when it's sent, so a later round gets nothing from it. Pass
    lifecycle.as_case(), which counts any later round, even one after an RFI nobody answered. A file's hash alone leaves
    out the records the file proves, such as its delivery scan: add the files with documents.with_files().
    """
    if result.outcome is Outcome.FAIL or _later_round(dispute):
        return Evidence(EvidenceClass.NONE)
    return Evidence(EvidenceClass.CE3_QUALIFIED, result.fact_ids, file_hashes, result.conflicts)


def consulted(dispute: Dispute | Case, first_call: Action, reported: Iterable[str] | None = ()) -> bool:
    """Whether the check is run: on a fraud claim whose records get read, in its first round, or later if the bank reports
    an earlier order. Otherwise there's nothing it could add."""
    if dispute.reason_code != FRAUD_CLAIM or first_call is not Action.GATHER_EVIDENCE:
        return False
    return not _later_round(dispute) or any(_order_number(str(text)) for text in reported or ())


def _later_round(dispute: Dispute | Case) -> bool:
    """Whether the bank has already turned down a challenge: the engine's Dispute and the store's Case both say."""
    return bool(dispute.prior_challenges or dispute.rejected_fact_ids or dispute.rejected_file_hashes)


def _met(priors: tuple[Prior, ...], reading: Reading) -> bool:
    counted = [prior for prior in priors if prior.counts(reading)]
    if reading.same_two:
        return any(sum(pair <= prior.matches for prior in counted) >= PRIORS_NEEDED for pair in PAIRS)
    return len(counted) >= PRIORS_NEEDED


def _problems(disputed: Order, priors: tuple[Prior, ...], given: set[str], named: set[str], words: str) -> list[str]:
    """Where the bank's word and the store's records don't line up. Each could only take orders away, so none can save a
    fail, but any of them makes a pass a person's call."""
    on_card = {prior.order.number for prior in priors}
    lines = [f"Order #{number} is flagged as reported, but the bank's comment doesn't name it."
             for number in sorted(given - named) if ORDER_NUMBER.fullmatch(number)]
    if any(not ORDER_NUMBER.fullmatch(number) for number in given):
        lines.append("Something flagged as reported isn't written as an order number, so the bank's comment can't "
                     "confirm it.")
    lines += [f"The bank names order #{number} as reported, but it isn't an earlier order on this card."
              for number in sorted(named - on_card)]
    lines += [f"The bank's comment names order #{number}, an earlier order on this card, but it isn't flagged as reported."
              for number in sorted(on_card - named) if _names(words, number)]
    brand = disputed.card_brand.strip().casefold()
    if says_nothing(brand):
        lines.append("The claim is under Visa's rules, but the store's records don't show the card's brand.")
    elif not brand.startswith("visa"):
        lines.append("The claim is under Visa's rules, but this order wasn't paid with a Visa card.")
    return lines


def _why_unclear(priors: tuple[Prior, ...], results: Mapping[Reading, bool]) -> list[str]:
    """A line for each question whose answer can change the result, naming the orders it turns on."""
    lines = []
    for question in (item.name for item in fields(Reading)):
        turns = [reading for reading, result in results.items() if result != results[_flip(reading, question)]]
        if not turns:
            continue
        if question == "same_two":  # one reading where it turns, so the orders named really share no pair
            counted = [prior for prior in priors if prior.counts(turns[0])]
            each = "; ".join(f"{_elements(prior.matches)} on #{prior.order.number}" for prior in counted)
            who, which = ("they don't", "both") if len(counted) == 2 else ("no two of them", "two of them")
            group = [f"{_orders([prior.order.number for prior in counted])} each match on device or IP and at least one "
                     f"more detail, but {who} share a pair like that: {each}. Airwallex's CE 3.0 page asks for the same "
                     f"pair on {which}."]
        else:  # the orders whose dropping out, when the answer flips, can take CE 3.0 from met to unmet
            turning = [prior for prior in priors
                       if any(results[reading] and _tips(prior, priors, reading, question) for reading in turns)]
            group = [line for line in (_turns_on(question, prior) for prior in turning) if line]
        decided = _decided_by(question, results, len(group))
        if group and decided:
            group[-1] += f" {decided}"
        lines += group
    return lines


def _tips(prior: Prior, priors: tuple[Prior, ...], reading: Reading, question: str) -> bool:
    """Whether the order counts here and drops out when the answer flips, and some set of the other orders that drop out
    leaves CE 3.0 unmet without it and met with it."""
    flipped = _flip(reading, question)
    if not prior.counts(reading) or prior.counts(flipped):
        return False
    if not reading.same_two:  # any two orders do, and at most one counts on both answers
        return True
    # The orders that count on both answers share no pair, or CE 3.0 would be met on both.
    stays = [other for other in priors if other.counts(reading) and other.counts(flipped)]
    drops = [other for other in priors if other is not prior and other.counts(reading) and not other.counts(flipped)]
    return (any(_share(prior, other) for other in stays)
            or any(_share(prior, other) and not any(_share(other, rest) for rest in stays) for other in drops))


def _share(one: Prior, other: Prior) -> bool:
    return any(pair <= one.matches and pair <= other.matches for pair in PAIRS)


def _turns_on(question: str, prior: Prior) -> str:
    number = prior.order.number
    if question == "from_the_order":
        return (f"Order #{number} was placed {_days_text(prior.days_before_dispute)} before the dispute but "
                f"{prior.days_before_order} before the disputed order. Visa counts the {NEWEST} to {OLDEST} days from the "
                "dispute, Airwallex's CE 3.0 page from the order.")
    if question == "disputed_counts":  # a dispute this code can't read gets the last line instead
        return (f"Order #{number} had a dispute that wasn't for fraud. Visa's FAQ still counts it, but Airwallex's CE 3.0 "
                "page asks for orders with no dispute." if prior.dispute == "NON_FRAUD" else "")
    if question == "refunded_counts":
        return (f"Order #{number} was {_refunded(prior.order)}, and neither Visa nor Airwallex says whether a refunded "
                "order still counts.")
    return (f"Order #{number} has a dispute of a kind this check can't read. If it wasn't for fraud, Visa's FAQ still "
            "counts the order, but Airwallex's CE 3.0 page asks for orders with no dispute.")


def _decided_by(question: str, results: Mapping[Reading, bool], orders: int) -> str:
    """When this question alone settles the result, which way each answer goes."""
    by_answer = {answer: {result for reading, result in results.items() if getattr(reading, question) is answer}
                 for answer in (False, True)}
    if len(by_answer[False]) != 1 or len(by_answer[True]) != 1 or by_answer[False] == by_answer[True]:
        return ""
    met_when = by_answer[True] == {True}
    if question == "from_the_order":
        met, unmet = ("order", "dispute") if met_when else ("dispute", "order")
        return f"Counted from the {met}, CE 3.0 is met; counted from the {unmet}, it isn't."
    if question == "same_two":
        return ("Airwallex's way, CE 3.0 is met; Visa's way, it isn't." if met_when else
                "Visa's way, CE 3.0 is met; Airwallex's way, it isn't.")
    it = "it" if orders == 1 else "them"
    return (f"Counting {it}, CE 3.0 is met; without {it}, it isn't." if met_when else
            f"Without {it}, CE 3.0 is met; counting {it}, it isn't.")


def _flip(reading: Reading, question: str) -> Reading:
    return replace(reading, **{question: not getattr(reading, question)})


def _summary(outcome: Outcome, priors: tuple[Prior, ...], card_known: bool, later: bool) -> str:
    """The first line. In a later round evidence() sends nothing from the check, so a pass or an unclear result says why."""
    too_late = "it can't be sent: Visa allows one CE 3.0 attempt, and this case is past its first round."
    if outcome is Outcome.PASS:
        return "CE 3.0 is met, as far as the store's records show" + (f", but {too_late}" if later else ".")
    if outcome is Outcome.UNCLEAR:
        return f"CE 3.0 is unclear, and {too_late}" if later else "CE 3.0 is unclear, so a person decides."
    if not card_known:
        return "CE 3.0 isn't met: the disputed order has no card on file, so no earlier order can be tied to its card."
    if not priors:
        return "CE 3.0 isn't met: the store's records show no earlier order on this card."
    return (f"CE 3.0 isn't met. It needs two earlier orders on this card, placed {NEWEST} to {OLDEST} days before the "
            "dispute with no fraud on them, each matching the disputed order on device or IP and one more detail.")


def _line(prior: Prior) -> str:
    """One earlier order: whether it counts, what keeps it out, when it was placed and what it matches."""
    counted = [prior.counts(reading) for reading in READINGS]
    verdict = "counts" if all(counted) else "counts on some readings only" if any(counted) else "doesn't count"
    facts = _facts(prior)
    return (f"Order #{prior.order.number} {verdict}{': ' + _join(facts) if facts else ''}. It was placed "
            f"{_days_text(prior.days_before_dispute)} before the dispute and {prior.days_before_order} before the disputed "
            f"order, and it matches on {_elements(prior.matches) or 'nothing'}.")


def _facts(prior: Prior) -> list[str]:
    facts = []
    if not (prior.in_window(False) or prior.in_window(True)):
        facts.append("it's too recent" if prior.days_before_dispute < NEWEST else
                     "it's too old" if prior.days_before_order > OLDEST else "it's outside the window both ways")
    elif not (prior.in_window(False) and prior.in_window(True)):
        anchor = "disputed order" if prior.in_window(True) else "dispute"
        facts.append(f"it's inside the window only counted from the {anchor}")
    if prior.reported:
        facts.append("the bank reports fraud on it")
    if prior.dispute == "FRAUD":
        facts.append("it had a fraud dispute")
    elif prior.dispute == "NON_FRAUD":
        facts.append("it had a dispute that wasn't for fraud")
    elif prior.dispute is not None:
        facts.append("its dispute is of a kind this check can't read")
    if prior.order.refunds:
        facts.append(f"it was {_refunded(prior.order)}")
    if not prior.matches & {"device", "ip"}:
        facts.append("it doesn't match on device or IP")
    elif len(prior.matches) < 2:
        facts.append("it matches on only one detail")
    return facts


def ip_says_nothing(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an IP is no buyer's: unspecified, the server's own (loopback), the network's own (link-local), or a
    proxy's or a load balancer's (PROXY_RANGES). An IPv4 address written as IPv6 (::ffff:) comes in unwrapped."""
    return (address.is_unspecified or address.is_loopback or address.is_link_local
            or any(address in network for network in PROXY_RANGES))


def _card(order: Order) -> str:
    """The card's fingerprint, or "" when it says nothing."""
    card = order.card_fingerprint.strip()
    return "" if says_nothing(card) else card


def _same_card(order: Order, disputed: Order) -> bool:
    """The underlying card decides, not the customer. Two blanks aren't the same card."""
    return bool(_card(order)) and _card(order) == _card(disputed)


def _matches(order: Order, disputed: Order) -> frozenset[str]:
    return frozenset(element for element, field in ELEMENTS.items()
                     if (value := _comparable(element, getattr(order, field)))
                     and value == _comparable(element, getattr(disputed, field)))


def _no_match(element: str, value: str | None) -> str:
    """Why nothing can match on the disputed order's element: nothing on file, or a value that's no buyer's."""
    if says_nothing(value):
        return f"The disputed order has no {NAMES[element]} on file, so nothing can match on it."
    why = "isn't a buyer's address, such as a proxy's or the server's own" if element == "ip" else "is all zeros"
    return f"The disputed order's {NAMES[element]} on file {why}, so nothing can match on it."


def _comparable(element: str, value: str | None) -> str:
    """A value as it's compared, or "" when it says nothing. Only spacing, an address's letter case and how an IP or a UUID
    is written are ignored, and a full stop or a mark left after one. An IP that's no buyer's (ip_says_nothing) says
    nothing, like an unspecified one."""
    text = " ".join((value or "").split()) if element == "address" else (value or "").strip()
    if says_nothing(text):
        return ""
    if element == "address":
        return text.casefold()
    try:
        if element == "ip":
            address = ipaddress.ip_address(text.rstrip(".!?"))  # "203.0.113.7." is that IP, cut from a sentence
            address = getattr(address, "ipv4_mapped", None) or address
            return "" if ip_says_nothing(address) else str(address)
        if element == "device":
            device = uuid.UUID(text.rstrip(".!?"))
            return "" if device.int == 0 else str(device)
    except ValueError:
        pass
    return text


def _without_dates(comment: str, year: int) -> str:
    """The bank's comment with the numbers in its dates, times, amounts and decimals blanked. year is the dispute's: a
    number reads as a date's year only from two years before it to one after."""
    years = "|".join(str(year + offset) for offset in range(-2, 2))
    return re.sub(NOT_ORDER_NUMBERS.replace("YEARS", years), " ", comment.translate(NO_BREAK_SPACES))


def _names(words: str, number: str) -> bool:
    """Whether the comment names the order: the number stands on its own in words, the comment without its dates."""
    return re.search(rf"(?<!\w){re.escape(number)}(?!\w)", words) is not None


def _utc(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("CE 3.0 counts calendar days, so every date it reads needs a time zone")
    return moment.astimezone(timezone.utc)


def _days(earlier: datetime, later: datetime) -> int:
    """Calendar days in UTC: the time of day doesn't count."""
    return (_utc(later).date() - _utc(earlier).date()).days


def _order_number(text: str) -> str:
    """'Order #1038', '#1038' and '1038' are all order 1038."""
    return re.sub(r"(?i)^\s*order\b", "", text).strip().removeprefix("#").strip()


def _refunded(order: Order) -> str:
    """How much of the order was refunded, or just "refunded" when an amount isn't a number that can be compared."""
    try:
        total, amount = sum((refund.amount for refund in order.refunds), ZERO), order.amount
        if total.is_finite() and amount.is_finite():
            return (f"refunded in full ({usd(total)})" if total >= amount
                    else f"partly refunded ({usd(total)} of {usd(amount)})")
    except ArithmeticError:  # a signalling NaN can't even be added up, nor a number past decimal's range be rounded
        pass
    return "refunded"


def _days_text(days: int) -> str:
    return f"{days} day{'' if days == 1 else 's'}"


def _elements(matches: frozenset[str]) -> str:
    return _join(NAMES[element] for element in ELEMENTS if element in matches)


def _orders(numbers: list[str]) -> str:
    return f"Order{'s' if len(numbers) > 1 else ''} {_join(f'#{number}' for number in numbers)}"


def _about(numbers: list[str], one: str, many: str) -> str:
    """'Order #1001 was ...' or 'Orders #1001 and #1002 were ...': the sentence's verb follows how many orders it names."""
    return f"{_orders(numbers)} {one if len(numbers) == 1 else many}"


def _join(items: Iterable[str]) -> str:
    items = list(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


# Public names for the Claude layer: it cleans a tag and tells a later round exactly as the check does.
order_number, later_round = _order_number, _later_round
