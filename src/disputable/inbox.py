"""What the inbox shows for a case's order: counts and days, never what an email says (the M findings).

The inbox is store.search_inbox(order_number), as Claude's tool shows it. The customer's addresses are
rules.customer_emails(), and every address is read as store.mailbox() reads it. A reply counts only when it comes from
the store's support address and goes to one of the customer's addresses, so a lookalike sender is never the store: it
counts as an address that is neither the customer's nor the store's. Days are UTC calendar days, written "Oct 5". Pure
code: no clock, no network and no model.
"""

from __future__ import annotations

from datetime import datetime

from disputable import documents, rules
from disputable.store import Store, mailbox

M0 = "The inbox has no email from this customer or about this order."
M1 = "The customer wrote {emails}, the last on {day}."
M2_NEVER = "The store never replied to the customer."
M2_LAST = "The store last replied to the customer on {day}."
M3 = "{emails} came from an address that isn't the customer's or the store's."


def findings(store: Store, order_number: str | None, support: str) -> tuple[str, ...]:
    """The M lines for the order, in a fixed order: M1, then M2, then M3, or M0 alone for an empty inbox. support is the
    store's support address (board.reader.profile.support). () when the order isn't in the store."""
    if not order_number:
        return ()
    try:
        order = store.order(order_number)
    except KeyError:
        return ()
    mail = store.search_inbox(order_number)
    if not mail:
        return (M0,)
    customer, box = rules.customer_emails(store, order), mailbox(support)
    own = [email for email in mail if mailbox(email.from_addr) in customer]
    replies = [email for email in mail
               if box and mailbox(email.from_addr) == box and mailbox(email.to_addr) in customer]
    others = [email for email in mail if mailbox(email.from_addr) not in customer and mailbox(email.from_addr) != box]
    lines = []
    if own:
        lines.append(M1.format(emails=_count(len(own)), day=_day(max(email.sent_at for email in own))))
    if replies:
        lines.append(M2_LAST.format(day=_day(max(email.sent_at for email in replies))))
    elif own:
        lines.append(M2_NEVER)
    if others:
        lines.append(M3.format(emails=_count(len(others))))
    return tuple(lines)


def _count(number: int) -> str:
    return f"{number} email{'' if number == 1 else 's'}"


def _day(moment: datetime) -> str:
    day = documents.utc(moment)
    return f"{rules.MONTHS[day.month - 1]} {day.day}"
