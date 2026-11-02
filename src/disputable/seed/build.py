"""Small builders the seed modules share."""

from __future__ import annotations

from datetime import datetime, timedelta

from disputable.store import Order, Scan

RETURNS_DOCK = "1 Demo Street, San Francisco, CA 94103, US"
SUPPORT_EMAIL = "support@fernhill-audio.example"


def tracking(order_number: str, leg: str = "0") -> str:
    """Shaped like UPS's published sample number, with the check digit always wrong, so it can't be a real parcel."""
    body = f"999AA1{leg}{order_number:0>8}"
    digits = [int(c) if c.isdigit() else (ord(c) - 63) % 10 for c in body]
    check = (10 - (sum(digits[0::2]) + 2 * sum(digits[1::2])) % 10) % 10
    return f"1Z{body}{(check + 1) % 10}"


def delivered(order: Order, at: datetime, signed_by: str | None = None, location: str | None = None,
              note: str = "") -> tuple[Scan, Scan, Scan]:
    """The warehouse packing the order, and the carrier taking it to someone."""
    picked_up = at - timedelta(days=3)
    common = {"order_number": order.number, "source": "carrier", "direction": "outbound",
              "tracking_number": order.tracking_number}
    return (
        Scan(id=f"scan-{order.number}-packed", order_number=order.number, source="warehouse", direction="outbound",
             at=picked_up - timedelta(hours=4), status="PACKED", location=RETURNS_DOCK),
        Scan(id=f"scan-{order.number}-picked-up", at=picked_up, status="PICKED_UP", location=RETURNS_DOCK, **common),
        Scan(id=f"scan-{order.number}-delivered", at=at, status="DELIVERED", location=location or order.shipping_address,
             signed_by=signed_by, note=note, **common),
    )


def returned(order: Order, at: datetime, signed_by: str | None = None) -> tuple[Scan, Scan]:
    """The customer's return reaching the store's dock. That doesn't mean the warehouse logged it."""
    common = {"order_number": order.number, "source": "carrier", "direction": "return",
              "tracking_number": tracking(order.number, "9")}
    return (
        Scan(id=f"scan-{order.number}-return-picked-up", at=at - timedelta(days=4), status="PICKED_UP",
             location=order.shipping_address, **common),
        Scan(id=f"scan-{order.number}-return-delivered", at=at, status="DELIVERED", location=RETURNS_DOCK,
             signed_by=signed_by, **common),
    )
