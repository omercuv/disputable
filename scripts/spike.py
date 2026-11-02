#!/usr/bin/env python3
"""Disputable sandbox spike: the Airwallex dispute lifecycle, end to end.

Kit 4 (Dispute Response Agent), Airwallex Agentic Banking Hackathon.
Talks ONLY to the Airwallex sandbox. Every HTTP attempt is recorded, with
secrets redacted, as a numbered JSON fixture in fixtures/spike/ so the demo
can replay it. The SPIKE REPORT answers the open API questions from what was
recorded.

Setup
    uv sync                      (or: pip install httpx python-dotenv playwright)
    The payments step needs Microsoft Edge or Google Chrome installed (otherwise: playwright install chromium).
    Create a file named .env in the project root with:
        AWX_CLIENT_ID=...
        AWX_API_KEY=...
        AWX_BASE_URL=https://api.sandbox.airwallex.com      (optional)
        AWX_FILES_URL=https://files.sandbox.airwallex.com   (optional)
    Values in .env win over variables already set in the shell.

Usage
    python scripts/spike.py all                    every step in order (probe last), then the report
    python scripts/spike.py all --no-probe         the same without the probe step
    python scripts/spike.py <step>                 run one step
    python scripts/spike.py <step> --force         redo a step that state.json marks as done. Recreating a
                                                   payment or dispute also clears the steps that depend on it
    python scripts/spike.py payments --force --case earbuds
                                                   recreate one case only (--case limits payments, disputes,
                                                   read, refunds and the case-specific steps)
    python scripts/spike.py report [--all-runs]    print the SPIKE REPORT from the recordings (no network);
                                                   by default only the current run's recordings
    python scripts/spike.py all --reset            archive old recordings and state, start a fresh run

Steps, in 'all' order
    login              get a bearer token (kept in memory, never written to disk)
    payments           3 USD payment intents, paid with test card 4035501000000008 in the Airwallex.js
                       Card element, on a local page in a headless browser (this sandbox account is not
                       enabled for server-side card confirm, so card data stays in Airwallex's iframe)
    disputes           simulated disputes: ear tips RFI 13.1, earbuds CHARGEBACK 13.6,
                       headphones RFI 10.4 (never PRE_CHARGEBACK: it auto-accepts)
    read               retrieve and list disputes, plus read-only probes of list filters
    accept-small       accept the ear tips at RFI (LOW_VALUE_TRANSACTION, refund object; falls back to none)
    upload             upload evidence PDF #1 (prior purchases), generated in code
    challenge-1        challenge the headphones at RFI with PDF #1 (PURCHASE_HISTORY)
    escalate           simulated issuer rejects the evidence (to CHARGEBACK), then re-read
    challenge-2        challenge again at CHARGEBACK (PRODUCT_RECEIVED) with a NEW PDF #2
    resolve            simulated issuer rules for the merchant
    accept-chargeback  accept the earbuds at CHARGEBACK (VALID_CUSTOMER_DISPUTE, no refund)
    refunds            list refunds per payment intent and for the account
    probe              two throwaway USD 25 RFI disputes get deliberately invalid accept / challenge
                       calls (the errors show the enum lists and whether request_id is required),
                       then are closed with a plain accept. Skip it with --no-probe.
Extra step
    report             print the SPIKE REPORT

Safety rails
    - Irreversible steps read the dispute first and are SKIPPED when their input step has not
      happened: accept/challenge need REQUIRES_RESPONSE at the expected stage, escalate needs a
      challenged RFI case, resolve needs a challenged CHARGEBACK case. --force calls anyway.
    - Calls that carry a request_id (payment intents, accept, challenge) retry once with the SAME
      request_id on timeout/5xx/429. Calls without one (simulator create/escalate/resolve, file
      upload) retry only when nothing reached the server (429, connect errors); otherwise the script
      re-reads the dispute to see whether the call took effect ("reconciled" in the output).
    - This project lives under OneDrive: writes to state.json retry if the sync client holds the
      file. Excluding fixtures/ from OneDrive sync avoids the problem entirely.

Files written
    fixtures/spike/NNN-<label>.json   one file per HTTP attempt (secrets redacted)
    fixtures/spike/state.json         ids created so far, so steps re-run without duplicates
    fixtures/spike/evidence_*.pdf|jpg copies of the uploaded evidence files

Guesses for fields the docs leave UNCONFIRMED sit in one block of constants
below. Every call that relies on one is tagged with its name, and the report
prints what the API said about it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

# ----------------------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent  # this file lives in <root>/scripts/
ENV_PATH = ROOT / ".env"
FIXTURES = ROOT / "fixtures" / "spike"
STATE_PATH = FIXTURES / "state.json"

# ----------------------------------------------------------------------------- confirmed facts
# Builder guide:  https://www.airwallexdev.com/guide
# API reference:  https://www.airwallex.com/docs/api (payments/payment_intents, payments/payment_disputes,
#                 simulation/payments, supporting_services/file_service, payments/refunds)
DEFAULT_BASE_URL = "https://api.sandbox.airwallex.com"
DEFAULT_FILES_URL = "https://files.sandbox.airwallex.com"
LOGIN_PATH = "/api/v1/authentication/login"
CURRENCY = "USD"
TEST_CARD = "4035501000000008"  # Visa debit US, always succeeds; any future expiry and CVC
TEST_CVC = "737"
RETURN_URL = "https://example.com/checkout/return"
# Airwallex.js Card element. env 'demo' is the sandbox value that builders have recorded working.
AIRWALLEX_JS_URL = "https://static.airwallex.com/components/sdk/v1/index.js"
BROWSER_CHANNELS = ("msedge", "chrome", None)  # None = Playwright's own Chromium, if installed
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
ACTOR = "disputable-spike"  # accepted_by / challenged_by are free-form strings, echoed back in *_details
TOKEN_REUSE_S = 25 * 60  # tokens last 30 minutes; refresh a little early
MIN_INTERVAL_S = 0.25  # one call at a time, about 4 req/s (sandbox: 10 req/s per endpoint)
RETRY_BACKOFF_S = 2.0
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_EVIDENCE_PDF_BYTES = 2 * 1024 * 1024  # Visa evidence: PDF <= 2 MB per file
MAX_EVIDENCE_JPEG_BYTES = 10 * 1024 * 1024  # challenge evidence fields and Visa JPEG: <= 10 MB
MAX_NAME_LEN = 50  # file service caps filename and notes at 50 characters
FINAL_STATUSES = {"ACCEPTED", "WON", "LOST", "REVERSED", "EXPIRED"}
RESOLVED_STATUSES = {"MERCHANT": {"WON", "REVERSED"}, "CUSTOMER": {"LOST"}}  # simulate resolve outcomes
DEAD_END_PI = {"CANCELLED", "REQUIRES_CUSTOMER_ACTION"}  # payment intents a resume cannot finish
STATE_ERROR_CODES = {"operation_not_supported", "resource_not_found", "not_found", "invalid_status_for_operation"}
# Wrong-state errors can arrive as code validation_error ('Dispute transition is not supported').
STATE_ERROR_RE = re.compile(
    r"transition|operation \S+ is not supported|current (status|stage)|requires_response"
    r"|(status|stage) (is )?not (allowed|supported|valid)|cannot be (accepted|challenged|escalated|resolved)")
NOTHING_SENT_ERRORS = {"ConnectError", "ConnectTimeout", "PoolTimeout"}  # the request never left this machine
DOC_ACCEPT_REASONS = (
    "AGREEMENT_REACHED_WITH_CUSTOMER", "CUSTOMER_RELATIONSHIP_MAINTENANCE", "LOW_VALUE_TRANSACTION",
    "VALID_CUSTOMER_DISPUTE", "COLLABORATION_ACCEPTED_MANUAL", "OTHERS",
)
DOC_CHALLENGE_REASONS = (
    "CUSTOMER_WITHDRAWN", "CUSTOMER_REFUNDED", "PRODUCT_RECEIVED", "PURCHASE_HISTORY",
    "NOT_ENTITLED", "SEPARATE_PRODUCT", "AUTHENTIC_PRODUCT", "OTHER_REASONS",
)
# Challenge fields compared against challenge_details after a successful challenge (silent-drop check).
CHECKED_FIELDS = ("order_info", "seller_info", "delivery_info", "customer_info", "product_type", "product_description")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ============================================================================= #
# UNCONFIRMED GUESSES: change them here, in one place.                          #
# Each value is the best-supported reading of the docs. Calls that rely on a    #
# guess are tagged with its name; the SPIKE REPORT (section 13) shows what the  #
# API answered, including the validation error when a guess is wrong.           #
# ============================================================================= #

# Simulation create/escalate due_at. Only '+0000' appears in the docs example.
DUE_AT_FORMAT = "%Y-%m-%dT%H:%M:%S+0000"
DUE_IN_DAYS = 14
# Visa reason codes as dotted strings (builder guide); the API reference gives no format.
VISA_REASON_CODES = {"ear_tips": "13.1", "earbuds": "13.6", "headphones": "10.4",
                     "probe_accept": "13.1", "probe_challenge": "13.1"}
# Accept reasons from the accept reference enum.
ACCEPT_REASON_LOW_VALUE = "LOW_VALUE_TRANSACTION"
ACCEPT_REASON_VALID = "VALID_CUSTOMER_DISPUTE"
# Refund object on accept: "applicable when accepting a dispute at the RFI stage".
# amount is filled in from the payment. None = never send a refund object.
RFI_ACCEPT_REFUND: dict | None = {"reason": "REQUESTED_BY_CUSTOMER"}
# Challenge reasons from the challenge reference enum (PURCHASE_HISTORY was disputed earlier).
CHALLENGE_1_REASON = "PURCHASE_HISTORY"
CHALLENGE_2_REASON = "PRODUCT_RECEIVED"
# Used only when an error names the reason, so the report shows which reason the API refused.
CHALLENGE_FALLBACK_REASON = "OTHER_REASONS"
CHALLENGE_METHOD = "STANDARD"
# Switch to AI_DISPUTE_AUTOMATION at CHARGEBACK when the dispute reports it AVAILABLE.
USE_AI_METHOD_WHEN_AVAILABLE = False
# evidence{} is documented as required for AI_DISPUTE_AUTOMATION. We try it on a STANDARD
# challenge first (the brief puts PDF #1 in the previous-purchase field); the report flags it
# as "ACCEPTED BUT DROPPED" if the API takes the call but does not store it.
SEND_EVIDENCE_OBJECT_ON_STANDARD = True
CHALLENGE_1_EVIDENCE_FIELD = "previous_purchase_similar_product_file_ids"
CHALLENGE_2_EVIDENCE_FIELD = "order_fulfillment_file_ids"  # delivery proof; no proof_of_delivery_file_ids exists
# The reference lists JPG/PNG/WEBP for evidence.*_file_ids and the guide rejects PNG, so JPG is the
# one format both allow. If a PDF id in evidence{} is refused, retry with a fresh JPEG made in code.
EVIDENCE_OBJECT_JPEG_FALLBACK = True
CHALLENGE_DOC_TYPE = "PRIMARY"  # supporting_documents.documents[].type: PRIMARY | ORDER | CUSTOMER | OTHER
# Fields the docs label "applicable when the stage is RFI", sent on challenge-1.
RFI_CHALLENGE_EXTRAS = ("order_info", "seller_info", "delivery_info")
# product_type is "required when the stage is CHARGEBACK"; unknown whether RFI accepts it.
CHARGEBACK_PRODUCT_TYPE = "PHYSICAL_GOODS"
SEND_PRODUCT_TYPE_AT_RFI = False
# delivery_info values: the docs only give examples, not closed enums.
SHIPPING_COMPANY = "ups"
DELIVERY_STATUS = "DELIVERED"
# Escalate simulator: the reference and example say due_at, the guide prose says due_date.
ESCALATE_DUE_FIELD = "due_at"
# Resolve simulator: MERCHANT gives WON or REVERSED, CUSTOMER gives LOST.
RESOLVE_IN_FAVOR_OF = "MERCHANT"
# File service path (the builder guide writes '/files/upload') and the id field in its response.
FILE_UPLOAD_PATH = "/api/v1/files/upload"
FILE_ID_KEYS = ("file_id", "id")
# After a 400 validation error, try the next (simpler) payload variant, each with a fresh request_id.
FALLBACK_VARIANTS = True

# Guess name -> words an error would contain if that guess were the problem (empty = any error).
GUESSES: dict[str, tuple[str, ...]] = {
    "DUE_AT_FORMAT": ("due",),
    "VISA_REASON_CODES": ("reason_code", "reason code"),
    "ACCEPT_REASON_LOW_VALUE": ("reason",),
    "ACCEPT_REASON_VALID": ("reason",),
    "RFI_ACCEPT_REFUND": ("refund",),
    "CHALLENGE_1_REASON": ("reason",),
    "CHALLENGE_2_REASON": ("reason",),
    "CHALLENGE_FALLBACK_REASON": ("reason",),
    "CHALLENGE_METHOD": ("challenge_method", "method"),
    "USE_AI_METHOD_WHEN_AVAILABLE": ("challenge_method", "ai_dispute"),
    "SEND_EVIDENCE_OBJECT_ON_STANDARD": ("evidence", "file_ids"),
    "CHALLENGE_1_EVIDENCE_FIELD": ("evidence", "file_ids"),
    "CHALLENGE_2_EVIDENCE_FIELD": ("evidence", "file_ids"),
    "EVIDENCE_OBJECT_JPEG_FALLBACK": ("evidence", "file", "format"),
    "CHALLENGE_DOC_TYPE": ("documents",),
    "RFI_CHALLENGE_EXTRAS": ("_info",),
    "CHARGEBACK_PRODUCT_TYPE": ("product_type",),
    "SEND_PRODUCT_TYPE_AT_RFI": ("product_type",),
    "SHIPPING_COMPANY": ("shipping_company", "shipping"),
    "DELIVERY_STATUS": ("delivery_info",),
    "ESCALATE_DUE_FIELD": ("due",),
    "RESOLVE_IN_FAVOR_OF": ("in_favor_of",),
    "FILE_UPLOAD_PATH": (),
    "FILE_ID_KEYS": (),
}
# Guess name -> fields that, if missing from challenge_details after a 2xx, mean the guess was dropped.
DROP_HINTS: dict[str, tuple[str, ...]] = {
    "SEND_EVIDENCE_OBJECT_ON_STANDARD": ("evidence.",),
    "CHALLENGE_1_EVIDENCE_FIELD": ("evidence.",),
    "CHALLENGE_2_EVIDENCE_FIELD": ("evidence.",),
    "EVIDENCE_OBJECT_JPEG_FALLBACK": ("evidence.",),
    "RFI_CHALLENGE_EXTRAS": ("order_info", "seller_info", "delivery_info"),
    "CHARGEBACK_PRODUCT_TYPE": ("product_type",),
    "SEND_PRODUCT_TYPE_AT_RFI": ("product_type",),
}

# ----------------------------------------------------------------------------- scenario (synthetic)
STORE = {
    "name": "Fernhill Audio Ltd",
    "store_name": "Fernhill Audio",
    "store_url": "https://example.com/fernhill-audio",
    "store_physical_address": "1 Demo Street, San Francisco, CA 94103, US",
}
CUSTOMER = {
    "first_name": "Alex",
    "last_name": "Sample",
    "email": "alex.sample@example.com",
    "phone_number": "+1 4155550123",
    "address": {"country_code": "US", "state": "CA", "city": "San Francisco",
                "street": "2 Sample Street", "postcode": "94103"},
}
CUSTOMER_ADDRESS_LINE = "2 Sample Street, San Francisco, CA 94103, US"
CUSTOMER_IP = "203.0.113.7"  # documentation-only IP range
CUSTOMER_DEVICE_ID = "59ec5db9-399c-4043-9a6c-fcd1a48d99aa"
TRACKING_NUMBER = "1Z999AA10123456784"

CASES = {
    "ear_tips": {"amount": 12, "order_no": "1121", "product": "Silicone ear tips, 3 sizes", "sku": "FH-ET-01",
                 "stage": "RFI", "comment": "Cardholder says the ear tips never arrived."},
    # Kit 4's "escalate a credit-not-processed case" beat. "Escalate" here means routing the case to a
    # human; the API side is an accept at CHARGEBACK. The simulator escalate is never called on this
    # case: escalating from CHARGEBACK fails ('Dispute transition is not supported'). If a
    # simulator-escalate beat is ever wanted for it, change its stage to "RFI" (never PRE_CHARGEBACK).
    "earbuds": {"amount": 189, "order_no": "1098", "product": "Wireless earbuds", "sku": "FH-EB-02",
                "stage": "CHARGEBACK", "comment": "Cardholder returned the earbuds; no credit was processed."},
    "headphones": {"amount": 1249, "order_no": "1109", "product": "Over-ear studio headphones", "sku": "FH-HP-03",
                   "stage": "RFI", "comment": "Cardholder does not recognise this card-absent transaction."},
    "probe_accept": {"amount": 25, "order_no": "9001", "product": "Spike probe item A", "sku": "FH-PROBE-A",
                     "stage": "RFI", "comment": "Spike accept probe (not part of the demo)."},
    "probe_challenge": {"amount": 25, "order_no": "9002", "product": "Spike probe item C", "sku": "FH-PROBE-C",
                        "stage": "RFI", "comment": "Spike challenge probe (not part of the demo)."},
}
MAIN_CASES = ("ear_tips", "earbuds", "headphones")
PROBE_CASES = ("probe_accept", "probe_challenge")
# Steps whose result belongs to one case: cleared when that case's payment or dispute is recreated.
CASE_STEPS = {
    "ear_tips": ("accept-small",),
    "earbuds": ("accept-chargeback",),
    "headphones": ("challenge-1", "escalate", "challenge-2", "resolve"),
    "probe_accept": ("probe",),
    "probe_challenge": ("probe",),
}
STEP_CASE = {"accept-small": "ear_tips", "accept-chargeback": "earbuds", "upload": "headphones",
             "challenge-1": "headphones", "escalate": "headphones", "challenge-2": "headphones",
             "resolve": "headphones"}

ESCALATE_COMMENT = "Order #1038 has a Visa fraud report (TC40) from May; CE 3.0 not met."
ESCALATE_MARKER = "TC40"  # used to find the comment again on the dispute object
RESOLVE_COMMENT = "Issuer accepted the signed delivery and warranty registration; ruled for the merchant."

EVIDENCE_1 = {
    "title": "Fernhill Audio - prior purchase history",
    "notes": "Prior purchase history, Visa 0008",
    "lines": [
        "Disputed order #1109: over-ear studio headphones, USD 1,249.00, Visa ending 0008.",
        "Prior undisputed orders by the same cardholder:",
        "  #1012   302 days before the disputed order   same card, device ID and IP",
        "  #1038   188 days before the disputed order   same card, device ID and IP",
        "  #1077    41 days before the disputed order   same card, device ID and IP",
        f"Device ID {CUSTOMER_DEVICE_ID}   IP {CUSTOMER_IP}",
    ],
}
EVIDENCE_2 = {
    "title": "Fernhill Audio - proof of possession",
    "notes": "Signed delivery and warranty registration",
    "lines": [
        f"Order #1109 shipped by UPS, tracking {TRACKING_NUMBER}.",
        "Delivered and signed for at the billing address.",
        "Serial FH-HP-03-55821 was warranty-registered 3 days after delivery,",
        "from the email on file since order #1012, SMS-verified to the cardholder's phone,",
        "on a device and IP not linked to the disputed order.",
    ],
}
EVIDENCE_PROBE = {
    "title": "Spike probe file",
    "notes": "Spike probe, not evidence",
    "lines": ["Not evidence. Used only to provoke validation errors in the sandbox."],
}

# ----------------------------------------------------------------------------- redaction
REDACTED = "<redacted>"
SECRET_HEADERS = {"authorization", "x-api-key", "x-client-id", "cookie", "set-cookie",
                  "cf-ray"}  # cf-ray ends in the code of the Cloudflare site that answered, so it shows the rough location
SECRET_KEYS = {"token", "access_token", "refresh_token", "client_secret", "api_key",
               "device_data"}  # device_data holds the paying browser's real IP address
ADDRESS = re.compile(r"(?<![\w.:])(?:(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9A-Fa-f]{0,4}:){3,7}[0-9A-Fa-f]{0,4})(?![\w.:])")
OWN_ADDRESSES = {"127.0.0.1", CUSTOMER_IP}  # the local checkout page and the made-up customer
ACCOUNT_ID_STAND_IN = "00000000-0000-4000-8000-000000000000"
_SECRET_VALUES: set[str] = set()


def add_secret(value: Any) -> None:
    if isinstance(value, str) and len(value) >= 6:
        _SECRET_VALUES.add(value)


def scrub(text: str) -> str:
    """Last line of defence: remove every known secret value and any address that isn't the script's own.
    An error page can print the caller's IP in a field no rule above knows about."""
    for value in sorted(_SECRET_VALUES, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return ADDRESS.sub(lambda found: found.group() if found.group() in OWN_ADDRESSES else REDACTED, text)


def _base64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def hide_account_id(text: str) -> str:
    """For recordings only. A file id is base64 of '<account id>,|,<region>,|,<file name>', and Airwallex also fills in
    a store URL from the id, so both forms get a stand-in. state.json keeps the real file ids, which a resume needs."""
    found = set(re.findall(r'github\.com/([0-9a-f-]{36})"', text))
    for token in re.findall(r'"([A-Za-z0-9+/]{48,})"', text):
        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode()
        except ValueError:  # not base64 or not text
            continue
        if ",|," in decoded:
            found.add(decoded.split(",|,")[0])
    for account_id in found:
        if len(account_id) == len(ACCOUNT_ID_STAND_IN):  # 36 bytes, so its base64 is a clean prefix of each file id
            text = text.replace(account_id, ACCOUNT_ID_STAND_IN).replace(_base64(account_id), _base64(ACCOUNT_ID_STAND_IN))
    return text


def redact_headers(headers: dict | None) -> dict:
    return {k: (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in (headers or {}).items()}


def redact(obj: Any) -> Any:
    """Copy of obj with secret fields redacted and card numbers masked."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS and value not in (None, ""):
                out[key] = REDACTED
            elif lowered == "card" and isinstance(value, dict):
                out[key] = _mask_card(redact(value))
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _mask_card(card: dict) -> dict:
    number = card.get("number")
    if isinstance(number, str) and len(number) > 10:
        card["number"] = number[:6] + "*" * (len(number) - 10) + number[-4:]
    if card.get("cvc"):
        card["cvc"] = "***"
    return card


# ----------------------------------------------------------------------------- small helpers
def log(msg: str = "") -> None:
    print(scrub(msg), flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def due_at(days: int = DUE_IN_DAYS) -> str:
    return (utcnow() + timedelta(days=days)).strftime(DUE_AT_FORMAT)


def new_request_id() -> str:
    return str(uuid.uuid4())


def ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def parse_ts(value: Any) -> datetime | None:
    """Tolerant ISO 8601 parser: 'Z', '+00:00' and '+0000' offsets all appear in the docs."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_dispute(obj: Any) -> bool:
    """A PaymentDispute object: ids look like dst_..., and only disputes carry a stage."""
    if not isinstance(obj, dict) or not obj.get("id"):
        return False
    return str(obj["id"]).startswith("dst_") or "stage" in obj


def stage_of(d: dict | None) -> str | None:
    return (d or {}).get("stage")


def status_of(d: dict | None) -> str | None:
    return (d or {}).get("status")


def customer_name() -> str:
    return f"{CUSTOMER['first_name']} {CUSTOMER['last_name']}"


def find_text_paths(obj: Any, needle: str, path: str = "") -> list[str]:
    """JSON paths of every string value that contains needle (case-insensitive)."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            hits += find_text_paths(value, needle, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            hits += find_text_paths(value, needle, f"{path}[{i}]")
    elif isinstance(obj, str) and needle.lower() in obj.lower():
        hits.append(path or "$")
    return hits


def collect_file_ids(obj: Any) -> set[str]:
    """Every id under a 'file_ids' or '*_file_ids' key, e.g. inside challenge_details."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).endswith("file_ids") and isinstance(value, list):
                found |= {v for v in value if isinstance(v, str)}
            else:
                found |= collect_file_ids(value)
    elif isinstance(obj, list):
        for value in obj:
            found |= collect_file_ids(value)
    return found


def dispute_objects(data: Any) -> Iterator[dict]:
    """Dispute objects in a response body: the object itself or the items of a list page."""
    if is_dispute(data):
        yield data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        yield from (item for item in data["items"] if is_dispute(item))


def _filled(value: Any) -> bool:
    """True for a non-empty value; a dict counts only if one of its values is non-empty."""
    if isinstance(value, dict):
        return any(_filled(v) for v in value.values())
    return bool(value)


def own_detail(d: dict | None, key: str, stage: str) -> dict | None:
    """Our own accept_details / challenge_details entry for a stage (matched on accepted_by / challenged_by)."""
    actor_key = "accepted_by" if key == "accept_details" else "challenged_by"
    for entry in (d or {}).get(key) or []:
        if isinstance(entry, dict) and entry.get("stage") == stage and entry.get(actor_key) == ACTOR:
            return entry
    return None


def challenge_entry(d: dict | None, stage: str | None) -> dict | None:
    """The latest challenge_details entry for a stage (by challenged_at, then list order)."""
    entries = [(i, c) for i, c in enumerate((d or {}).get("challenge_details") or [])
               if isinstance(c, dict) and (stage is None or c.get("stage") == stage)]
    if not entries:
        return None
    return max(entries, key=lambda ic: (parse_ts(ic[1].get("challenged_at")) or EPOCH, ic[0]))[1]


def persisted_check(sent: dict, d: dict | None, stage: str | None) -> dict:
    """Compare what a challenge sent with what challenge_details kept (catches silently dropped fields)."""
    entry = challenge_entry(d, stage) or {}
    sent_ev = {k for k, v in (sent.get("evidence") or {}).items() if _filled(v)}
    kept_raw = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
    kept_ev = {k for k, v in kept_raw.items() if _filled(v)}
    sent_ids, kept_ids = collect_file_ids(sent), collect_file_ids(entry)
    dropped = [f"evidence.{k}" for k in sorted(sent_ev - kept_ev)]
    dropped += [f"file_id {i}" for i in sorted(sent_ids - kept_ids)]
    dropped += [k for k in CHECKED_FIELDS if k in sent and _filled(sent[k]) and not _filled(entry.get(k))]
    return {"stage": stage, "entry_found": bool(entry), "reason_sent": sent.get("reason"),
            "reason_kept": entry.get("reason"), "evidence_sent": sorted(sent_ev), "evidence_kept": sorted(kept_ev),
            "file_ids_sent": sorted(sent_ids), "file_ids_kept": sorted(kept_ids), "dropped": dropped}


def describe_check(check: dict) -> str:
    if not check.get("entry_found"):
        return f"ACCEPTED BUT NOT FOUND: no challenge_details entry for stage {check.get('stage')} afterwards"
    if check.get("dropped"):
        return "ACCEPTED BUT DROPPED: " + ", ".join(check["dropped"])
    note = "" if check.get("reason_kept") in (None, check.get("reason_sent")) else \
        f" (reason stored as {check.get('reason_kept')!r}, sent {check.get('reason_sent')!r})"
    return "persisted: every evidence key, file id and info object sent is in challenge_details" + note


# ----------------------------------------------------------------------------- evidence files
def make_pdf(title: str, lines: list[str]) -> bytes:
    """A minimal one-page PDF (Helvetica text) written by hand, so no PDF library is needed."""

    def pdf_text(s: str) -> str:
        s = s.encode("ascii", "replace").decode("ascii")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 15 Tf", "72 740 Td", f"({pdf_text(title)}) Tj", "/F1 10 Tf"]
    for i, line in enumerate(lines):
        ops += ["0 -26 Td" if i == 0 else "0 -15 Td", f"({pdf_text(line)}) Tj"]
    ops.append("ET")
    stream = "\n".join(ops).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset  # each xref entry is exactly 20 bytes
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_at)
    return bytes(out)


def check_pdf(pdf: bytes) -> None:
    if not pdf.startswith(b"%PDF-"):
        raise StepFail("generated evidence does not start with %PDF")
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", pdf)
    if not m:
        raise StepFail("generated evidence has no startxref / %%EOF trailer")
    if pdf[int(m.group(1)):int(m.group(1)) + 4] != b"xref":
        raise StepFail("generated evidence has a startxref offset that does not point at its xref table")
    if len(pdf) > MAX_EVIDENCE_PDF_BYTES:
        raise StepFail(f"generated evidence is {len(pdf)} bytes, over the {MAX_EVIDENCE_PDF_BYTES}-byte limit")


def make_jpeg(comment: str, size: int = 64) -> bytes:
    """A minimal baseline JPEG written by hand: a flat grey size x size image with the text in a COM
    segment (so every file differs). Each 8x8 block is 'DC difference 0, end of block', coded with
    one-symbol Huffman tables, so the scan is only zero bits and needs no byte stuffing."""

    def segment(marker: int, payload: bytes) -> bytes:
        return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload

    if size % 8 or not 8 <= size <= 4096:
        raise ValueError("size must be a multiple of 8 between 8 and 4096")
    one_symbol = b"\x01" + b"\x00" * 15 + b"\x00"  # one 1-bit code ('0') for symbol 0x00
    out = bytearray(b"\xff\xd8")  # SOI
    out += segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")  # APP0 JFIF 1.1, 1:1 aspect
    out += segment(0xFE, comment.encode("ascii", "replace")[:60000])  # COM
    out += segment(0xDB, b"\x00" + b"\x01" * 64)  # DQT: table 0, all ones
    out += segment(0xC0, b"\x08" + size.to_bytes(2, "big") * 2 + b"\x01\x01\x11\x00")  # SOF0: 8-bit greyscale
    out += segment(0xC4, b"\x00" + one_symbol)  # DHT DC 0: category 0 (difference 0)
    out += segment(0xC4, b"\x10" + one_symbol)  # DHT AC 0: 0x00 = end of block
    out += segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")  # SOS: 1 component, tables 0/0, Ss=0, Se=63
    bits = "00" * (size // 8) ** 2  # per block: DC code '0', then EOB code '0'
    bits += "1" * (-len(bits) % 8)  # pad the last byte with 1-bits
    out += int(bits, 2).to_bytes(len(bits) // 8, "big")
    out += b"\xff\xd9"  # EOI
    return bytes(out)


def check_jpeg(jpg: bytes) -> None:
    if not (jpg.startswith(b"\xff\xd8\xff") and jpg.endswith(b"\xff\xd9")):
        raise StepFail("generated JPEG lacks the SOI/EOI markers")
    if len(jpg) > MAX_EVIDENCE_JPEG_BYTES:
        raise StepFail(f"generated JPEG is {len(jpg)} bytes, over the {MAX_EVIDENCE_JPEG_BYTES}-byte limit")


def save_copy(name: str, data: bytes) -> None:
    """Keep a copy of each uploaded file next to the recordings, for the demo."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_bytes(data)


# ----------------------------------------------------------------------------- core types
class StepSkip(Exception):
    """The step's inputs are missing or its work is already done."""


class StepFail(Exception):
    """The step ran and the API (or a check) said no."""


@dataclass
class Config:
    client_id: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str = ""
    files_url: str = ""


@dataclass
class Resp:
    status: int | None  # None = no HTTP response (timeout, connection error)
    data: Any = None  # parsed JSON body
    text: str = ""  # raw body when it is not JSON
    headers: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None  # transport error
    error_kind: str | None = None  # transport exception class, e.g. ConnectError or ReadTimeout
    attempts: int = 1  # HTTP attempts behind this response (retries and re-login included)
    record: str = ""  # fixture file name

    @classmethod
    def from_httpx(cls, r: httpx.Response, elapsed_ms: int) -> Resp:
        data, text = None, ""
        if r.content:
            try:
                data = r.json()
            except ValueError:
                text = r.text[:20000]
        headers = {k.lower(): v for k, v in r.headers.items()}
        return cls(status=r.status_code, data=data, text=text, headers=headers, elapsed_ms=elapsed_ms)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500

    @property
    def settled(self) -> bool:
        """The outcome is known (success or a definite rejection), so the request must not be replayed."""
        return self.ok or (self.status is not None and self.status < 500 and self.status != 429)

    @property
    def code(self) -> str:
        return str(self.data.get("code") or "") if isinstance(self.data, dict) and not self.ok else ""

    def retry_after(self) -> float | None:
        try:
            return min(float(self.headers.get("retry-after") or ""), 30.0)
        except (TypeError, ValueError):
            return None

    def blob(self) -> str:
        """Everything the error said, lowercased, for keyword matching."""
        body = json.dumps(self.data) if self.data is not None else self.text
        return f"{body} {self.error or ''}".lower()

    def error_text(self, limit: int = 300) -> str:
        if self.error:
            return self.error
        if isinstance(self.data, dict):
            parts = [self.data.get(k) for k in ("code", "source", "message")]
            text = " | ".join(str(p) for p in parts if p) or json.dumps(self.data)[:limit]
            if self.data.get("details"):
                text += f" | details={json.dumps(self.data['details'])[:limit]}"
            return text
        return (self.text or "")[:limit]

    def summary(self) -> str:
        return f"HTTP {self.status}" if self.ok else f"HTTP {self.status or 'none'}: {self.error_text()}"


def is_sandbox_url(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme == "https" and (parts.hostname or "").lower().endswith(".sandbox.airwallex.com")


def load_config() -> Config:
    """Read .env from the project root; refuse anything that is not the sandbox."""
    # .env wins over the shell; utf-8-sig tolerates the BOM Windows Notepad may add.
    load_dotenv(ENV_PATH, override=True, encoding="utf-8-sig")
    urls = {}
    for name, default in (("AWX_BASE_URL", DEFAULT_BASE_URL), ("AWX_FILES_URL", DEFAULT_FILES_URL)):
        url = (os.getenv(name) or default).strip().rstrip("/").removesuffix("/api/v1")
        if not is_sandbox_url(url):
            raise SystemExit(f"Refusing to run: {name}={url!r} is not an https://*.sandbox.airwallex.com URL. "
                             "This spike only talks to the sandbox.")
        urls[name] = url
    client_id = (os.getenv("AWX_CLIENT_ID") or "").strip()
    api_key = (os.getenv("AWX_API_KEY") or "").strip()
    if not client_id or not api_key:
        raise SystemExit(f"AWX_CLIENT_ID and AWX_API_KEY must be set in {ENV_PATH} (see docs/getting-api-keys.md).")
    add_secret(client_id)
    add_secret(api_key)
    return Config(client_id, api_key, urls["AWX_BASE_URL"], urls["AWX_FILES_URL"])


class Recorder:
    """Writes one numbered JSON file per HTTP attempt, e.g. 001-login.json."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for p in folder.glob("*.json") if (m := re.match(r"(\d+)-", p.name))]
        self.seq = max(numbers, default=0)  # re-runs append, never overwrite

    def write(self, label: str, record: dict) -> str:
        self.seq += 1
        slug = re.sub(r"[^a-z0-9_.-]+", "-", label.lower()).strip("-")[:70] or "call"
        path = self.folder / f"{self.seq:03d}-{slug}.json"
        text = json.dumps(redact({"seq": self.seq, **record}), indent=2, ensure_ascii=False, default=str)
        path.write_text(hide_account_id(scrub(text)), encoding="utf-8", newline="\n")  # same bytes on Windows and macOS
        return path.name


class State:
    """Run state in fixtures/spike/state.json: ids and statuses only, never secrets."""

    def __init__(self, path: Path):
        self.path = path
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path} is not valid JSON ({exc}). Fix or delete it, or use --reset.")
        data.setdefault("run_id", uuid.uuid4().hex[:8])
        data.setdefault("started_at", iso(utcnow()))
        for key in ("payments", "disputes", "files", "steps", "pending"):
            data.setdefault(key, {})
        data.setdefault("file_history", [])
        data.setdefault("history", [])  # retired payments and disputes (replaced with --force)
        self.data = data

    run_id = property(lambda self: self.data["run_id"])
    payments = property(lambda self: self.data["payments"])
    disputes = property(lambda self: self.data["disputes"])
    files = property(lambda self: self.data["files"])
    steps = property(lambda self: self.data["steps"])

    def all_files(self) -> list[dict]:
        return list(self.files.values()) + list(self.data["file_history"])

    def save(self) -> None:
        """Atomic write. On Windows, OneDrive or antivirus can hold the file for a moment: retry."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = scrub(json.dumps(self.data, indent=2, ensure_ascii=False))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        for i in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if i == 5:
                    raise
                time.sleep(0.25 * (i + 1))

    def mark(self, step: str, ok: bool, detail: str) -> None:
        self.steps[step] = {"ok": ok, "at": iso(utcnow()), "detail": scrub(detail)[:500]}
        self.save()

    def done(self, step: str) -> bool:
        return bool(self.steps.get(step, {}).get("ok"))


class Api:
    """Sequential HTTP client: bearer auth, throttling, one safe retry, and a recording per attempt."""

    def __init__(self, cfg: Config, recorder: Recorder, state: State, transport: httpx.BaseTransport | None = None):
        self.cfg, self.rec, self.state = cfg, recorder, state
        self.http = httpx.Client(timeout=HTTP_TIMEOUT, transport=transport,
                                 headers={"Accept": "application/json", "User-Agent": "disputable-spike/0.2"})
        self.step = "-"
        self._token: str | None = None
        self._token_at = 0.0
        self._last_call = 0.0
        add_secret(cfg.client_id)
        add_secret(cfg.api_key)

    def close(self) -> None:
        self.http.close()

    def login(self, force: bool = False) -> Resp | None:
        """Get a token, or reuse the one we have while it is fresh."""
        if self._token and not force and time.monotonic() - self._token_at < TOKEN_REUSE_S:
            return None
        resp = self.request("POST", LOGIN_PATH, label="login", auth=False,
                            extra_headers={"x-client-id": self.cfg.client_id, "x-api-key": self.cfg.api_key},
                            meta={"kind": "login"})
        token = resp.data.get("token") if resp.ok and isinstance(resp.data, dict) else None
        if not token:
            raise StepFail(f"login failed: {resp.summary()}")
        self._token, self._token_at = token, time.monotonic()
        return resp

    def _throttle(self) -> None:
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _may_retry(resp: Resp, idempotent: bool) -> bool:
        if resp.status == 429:
            return True  # rate limited: the server did not process it
        if idempotent:
            return resp.retryable  # same request_id, so the server can deduplicate
        return resp.status is None and resp.error_kind in NOTHING_SENT_ERRORS

    def request(self, method: str, path: str, *, label: str, host: str = "api", json_body: dict | None = None,
                params: dict | None = None, files: dict | None = None, form: dict | None = None, auth: bool = True,
                extra_headers: dict | None = None, record_body: Any = None, meta: dict | None = None,
                idempotent: bool = True, retry: bool = True) -> Resp:
        """One logical call, every attempt recorded. idempotent=True (a request_id in the body, or a
        GET): one retry with the same body on timeout/5xx/429. idempotent=False (no request_id): retry
        only when nothing reached the server. 401: log in again once."""
        url = (self.cfg.files_url if host == "files" else self.cfg.base_url) + path
        shown_url = str(httpx.URL(url, params=params)) if params else url
        relogged = retried = False
        attempt = 0
        while True:
            attempt += 1
            if auth:
                self.login()  # no-op while the token is fresh
            headers = dict(extra_headers or {})
            if auth:
                headers["Authorization"] = f"Bearer {self._token}"
            sent_headers = {**dict(self.http.headers.items()), **headers}
            self._throttle()
            started = time.perf_counter()
            try:
                r = self.http.request(method, url, headers=headers, params=params, json=json_body,
                                      files=files, data=form)
                resp = Resp.from_httpx(r, ms_since(started))
                sent_headers = dict(r.request.headers.items())
            except httpx.HTTPError as exc:  # timeout, connection reset, DNS failure ...
                resp = Resp(status=None, error=f"{type(exc).__name__}: {exc}", error_kind=type(exc).__name__,
                            elapsed_ms=ms_since(started))
            resp.attempts = attempt
            if isinstance(resp.data, dict):
                for key in SECRET_KEYS:  # e.g. the login token: scrub it from every later line
                    add_secret(resp.data.get(key))
            resp.record = self.rec.write(label, {
                "run_id": self.state.run_id,
                "step": self.step,
                "label": label,
                "attempt": attempt,
                "idempotent": idempotent,
                "recorded_at": iso(utcnow()),
                "meta": meta or {},
                "request": {"method": method, "url": shown_url, "headers": redact_headers(sent_headers),
                            "body": record_body if record_body is not None else json_body},
                "response": {"status": resp.status, "elapsed_ms": resp.elapsed_ms,
                             "headers": redact_headers(resp.headers), "json": resp.data,
                             "text": resp.text or None, "transport_error": resp.error},
            })
            note = f" (attempt {attempt})" if attempt > 1 else ""
            log(f"    {method} {path} -> {resp.status or 'no response'} in {resp.elapsed_ms} ms{note}  [{resp.record}]")
            if not resp.ok:
                log(f"      {resp.error_text()}")
            if auth and resp.status == 401 and not relogged:
                relogged = True
                log("    401: logging in again, then retrying once")
                self.login(force=True)
                continue
            if retry and not retried and self._may_retry(resp, idempotent):
                retried = True
                delay = resp.retry_after() or RETRY_BACKOFF_S
                how = "with the same body and request_id" if idempotent else "(the server did not process it)"
                log(f"    retrying once in {delay:.1f}s {how}")
                time.sleep(delay)
                continue
            if not resp.ok and not resp.settled and not idempotent:
                log("      no request_id on this call, so no blind retry: the step re-reads state instead")
            return resp


@dataclass
class Ctx:
    api: Api
    state: State
    force: bool = False
    only_case: str | None = None
    client_secrets: dict[str, str] = field(default_factory=dict)  # by payment intent id, memory only

    def cases(self, pool: tuple[str, ...] = MAIN_CASES) -> tuple[str, ...]:
        return tuple(c for c in pool if self.only_case in (None, c))


@dataclass
class Variant:
    """One payload shape for a call. body may be a callable (built lazily, None = skip)."""
    name: str
    body: Any
    guesses: list[str]
    when: Callable[[Resp], bool] | None = None  # tried only if the previous error passes this test


def is_state_error(resp: Resp) -> bool:
    return resp.code in STATE_ERROR_CODES or bool(STATE_ERROR_RE.search(resp.blob()))


def is_validation_error(resp: Resp) -> bool:
    """A 400/422 about the payload (worth trying a simpler variant), not about the dispute's state."""
    return (resp.status in (400, 422) and not resp.code.startswith("duplicate_request")
            and not is_state_error(resp))


def names_reason(resp: Resp) -> bool:
    return bool(re.search(r"\breason\b", resp.blob()))


def not_reason_error(resp: Resp) -> bool:
    return not names_reason(resp)


def names_file_problem(resp: Resp) -> bool:
    return not names_reason(resp) and bool(re.search(r"evidence|file|format|pdf|extension|mime", resp.blob()))


def try_variants(ctx: Ctx, path: str, variants: list[Variant], *, label: str, meta: dict, idempotent: bool = True,
                 fallback_if: Callable[[Resp], bool] = is_validation_error,
                 still_valid: Callable[[], bool] | None = None) -> tuple[str, dict, Resp, list[tuple[str, dict, Resp]]]:
    """POST payload variants in order until one is accepted. Moves on only after a payload-type
    validation error, and (with still_valid) only while the dispute is still in the expected state.
    Each variant is a different operation, so it gets a fresh request_id."""
    attempts: list[tuple[str, dict, Resp]] = []
    for variant in variants:
        if attempts:
            prev = attempts[-1][2]
            if prev.ok or not FALLBACK_VARIANTS or not fallback_if(prev):
                break
            if variant.when is not None and not variant.when(prev):
                continue
            if still_valid is not None and not still_valid():
                log("    the dispute is no longer in the expected state; not trying more variants")
                break
            log(f"    variant '{attempts[-1][0]}' was rejected; trying '{variant.name}'")
        body = variant.body() if callable(variant.body) else variant.body
        if body is None:
            log(f"    variant '{variant.name}' could not be built; skipping it")
            continue
        if "request_id" in body:
            body = {**body, "request_id": new_request_id()}
        resp = ctx.api.request("POST", path, label=f"{label}-{variant.name}", json_body=body, idempotent=idempotent,
                               meta={**meta, "variant": variant.name, "guesses": variant.guesses})
        attempts.append((variant.name, body, resp))
    if not attempts:
        raise StepFail("no payload variant could be built")
    name, body, resp = attempts[-1]
    return name, body, resp, attempts


def attempts_summary(attempts: list[tuple[str, dict, Resp]]) -> str:
    return "; ".join(f"[{name}] {resp.summary()}" for name, _, resp in attempts)


def applied(resp: Resp, after: dict | None, expect: Callable[[dict], bool]) -> str | None:
    """'ok' on success; 'reconciled' when the response was lost (or a retry came back as
    duplicate_request) but the dispute shows the change; None when it did not happen."""
    if resp.ok:
        return "ok"
    lost = resp.status is None or resp.status >= 500 or resp.status == 429
    dup = resp.code.startswith("duplicate_request") and resp.attempts > 1
    if (lost or dup) and after is not None and expect(after):
        return ("reconciled: duplicate_request on the retry, applied on the first attempt" if dup
                else "reconciled: no clean response, but the dispute shows the change")
    return None


# ----------------------------------------------------------------------------- state helpers
def pending_get(ctx: Ctx, key: str, build: Callable[[], dict]) -> dict:
    """A request saved before sending, so a re-run after a lost response resends the same request_id."""
    pend = ctx.state.data.setdefault("pending", {})
    if key in pend:
        log(f"    reusing the saved {key} request (same request_id): an earlier attempt's outcome is unknown")
    else:
        pend[key] = build()
        ctx.state.save()
    return pend[key]


def pending_clear(ctx: Ctx, key: str) -> None:
    if ctx.state.data.setdefault("pending", {}).pop(key, None) is not None:
        ctx.state.save()


def retire(ctx: Ctx, case: str, what: str, why: str) -> None:
    """Move a case's payment (what='payment') and/or dispute to history and clear its dependent steps."""
    hist = ctx.state.data.setdefault("history", [])
    now = iso(utcnow())
    if what == "payment" and case in ctx.state.payments:
        hist.append({"kind": "payment", "case": case, "retired_at": now, "why": why, **ctx.state.payments.pop(case)})
    if case in ctx.state.disputes:  # a dispute belongs to its payment, so it goes either way
        hist.append({"kind": "dispute", "case": case, "retired_at": now, "why": why, **ctx.state.disputes.pop(case)})
    cleared = [s for s in CASE_STEPS.get(case, ()) if ctx.state.steps.pop(s, None)]
    ctx.state.save()
    log(f"  {case}: retired the old {what} ({why})" + (f"; cleared steps {', '.join(cleared)}" if cleared else ""))


def known_dispute_ids(ctx: Ctx) -> set[str]:
    ids = {d.get("id") for d in ctx.state.disputes.values()}
    ids |= {h.get("id") for h in ctx.state.data.get("history", []) if h.get("kind") == "dispute"}
    return {i for i in ids if i}


def mark_used(ctx: Ctx, obj: Any, step: str) -> None:
    """Record which uploaded files a successful challenge carried (they must never be sent again)."""
    ids = collect_file_ids(obj)
    for entry in ctx.state.all_files():
        if entry.get("file_id") in ids and step not in entry.setdefault("used_in", []):
            entry["used_in"].append(step)
    ctx.state.save()


def skip_if_done(ctx: Ctx, step: str) -> None:
    if not ctx.force and ctx.state.done(step):
        raise StepSkip(f"already done ({ctx.state.steps[step]['detail']}); use --force to redo")


def require(ctx: Ctx, d: dict | None, stages: set[str], statuses: set[str], what: str) -> None:
    """Guard for irreversible calls: skip unless the dispute is where the call belongs (--force overrides)."""
    want = f"{'|'.join(sorted(stages))}/{'|'.join(sorted(statuses))}"
    here = "unreadable" if d is None else f"{d.get('stage')}/{d.get('status')}"
    if d is not None and d.get("stage") in stages and d.get("status") in statuses:
        return
    if ctx.force:
        log(f"  WARNING: {what} expects {want}, dispute is {here}; --force: calling anyway")
        return
    raise StepSkip(f"{what} needs the dispute at {want}, it is {here} (use --force to call anyway)")


# ----------------------------------------------------------------------------- dispute helpers
def dispute_id(ctx: Ctx, case: str) -> str:
    rec = ctx.state.disputes.get(case) or {}
    if not rec.get("id"):
        raise StepSkip(f"no {case} dispute in state; run 'disputes' first")
    return rec["id"]


def remember_dispute(ctx: Ctx, case: str, d: dict) -> None:
    rec = ctx.state.disputes.setdefault(case, {})
    rec.update({"id": d.get("id") or rec.get("id"), "status": d.get("status"), "stage": d.get("stage"),
                "refund_ids": [r.get("id") for r in d.get("refunds") or [] if isinstance(r, dict) and r.get("id")]})
    ctx.state.save()


def get_dispute(ctx: Ctx, case: str, phase: str) -> dict | None:
    did = dispute_id(ctx, case)
    resp = ctx.api.request("GET", f"/api/v1/pa/payment_disputes/{did}", label=f"dispute-get-{case}-{phase}",
                           meta={"kind": "dispute_get", "case": case, "phase": phase})
    if resp.ok and is_dispute(resp.data):
        remember_dispute(ctx, case, resp.data)
        log(f"    {case}: stage={resp.data.get('stage')} status={resp.data.get('status')}")
        return resp.data
    return None


def wait_dispute(ctx: Ctx, case: str, phase: str, until: Callable[[dict], bool], tries: int = 5,
                 delay: float = 1.5) -> dict | None:
    """Re-read the dispute until `until` holds (sandbox transitions can lag a moment)."""
    d = None
    for i in range(tries):
        d = get_dispute(ctx, case, phase if i == 0 else f"{phase}-poll{i}") or d
        if d is not None and until(d):
            break
        if i + 1 < tries:
            time.sleep(delay)
    return d


def still_open(ctx: Ctx, case: str, stage: str) -> Callable[[], bool]:
    """For try_variants: re-read the dispute before a fallback variant; go on only while it is actionable."""
    def check() -> bool:
        d = get_dispute(ctx, case, "pre-variant")
        return d is not None and d.get("stage") == stage and d.get("status") == "REQUIRES_RESPONSE"
    return check


def actionable_or_final(d: dict) -> bool:
    return d.get("status") == "REQUIRES_RESPONSE" or d.get("status") in FINAL_STATUSES


def list_disputes(ctx: Ctx, name: str, params: dict, case: str | None = None, probe: str | None = None) -> list | None:
    """Items of one list page; None when the call failed."""
    resp = ctx.api.request("GET", "/api/v1/pa/payment_disputes", params=params, label=f"dispute-list-{name}",
                           meta={"kind": "dispute_list", "case": case, "probe": probe, "params": params})
    if not resp.ok:
        return None
    items = resp.data.get("items") if isinstance(resp.data, dict) else None
    return items if isinstance(items, list) else []


def list_refunds(ctx: Ctx, case: str, phase: str, tries: int = 1, delay: float = 2.0) -> list | None:
    """Refunds on the case's disputed payment; None when every call failed."""
    pay = ctx.state.payments.get(case) or {}
    pid = (ctx.state.disputes.get(case) or {}).get("payment_intent_id") or pay.get("id")
    if not pid:
        return []
    items: list | None = None
    for i in range(tries):
        resp = ctx.api.request("GET", "/api/v1/pa/refunds",
                               params={"payment_intent_id": pid, "page_num": 0, "page_size": 100},
                               label=f"refunds-{case}-{phase}" + (f"-poll{i}" if i else ""),
                               meta={"kind": "refunds_list", "case": case, "phase": phase})
        if resp.ok:
            found = resp.data.get("items") if isinstance(resp.data, dict) else None
            items = found if isinstance(found, list) else []
        if items or i + 1 == tries:
            break
        time.sleep(delay)
    if items is None:
        return None
    if pay:
        pay["refunds"] = [{"id": r.get("id"), "amount": r.get("amount"), "status": r.get("status")}
                          for r in items if isinstance(r, dict)]
        ctx.state.save()
    log(f"    refunds on {case}: " + (", ".join(f"{r.get('id')} {r.get('amount')} {r.get('status')}"
                                              for r in items if isinstance(r, dict)) or "none"))
    return items


# ----------------------------------------------------------------------------- payment helpers
def get_payment(ctx: Ctx, case: str, phase: str) -> dict | None:
    pid = ctx.state.payments[case]["id"]
    resp = ctx.api.request("GET", f"/api/v1/pa/payment_intents/{pid}", label=f"pi-get-{case}-{phase}",
                           meta={"kind": "pi_get", "case": case})
    return resp.data if resp.ok and isinstance(resp.data, dict) else None


def create_payment(ctx: Ctx, case: str) -> str:
    spec = CASES[case]
    key = f"pi_create:{case}"

    def build() -> dict:
        return {
            "request_id": new_request_id(),
            "amount": spec["amount"],  # major units: 12 = USD 12.00
            "currency": CURRENCY,
            "merchant_order_id": f"FH-{spec['order_no']}-{ctx.state.run_id}-{uuid.uuid4().hex[:4]}",  # max 64
            "descriptor": "FERNHILL AUDIO",
            "return_url": RETURN_URL,
            "metadata": {"spike_run": ctx.state.run_id, "case": case, "order_no": spec["order_no"]},
        }

    body = pending_get(ctx, key, build)  # saved first: a re-run after a lost response reuses its request_id
    resp = ctx.api.request("POST", "/api/v1/pa/payment_intents/create", label=f"pi-create-{case}",
                           json_body=body, meta={"kind": "pi_create", "case": case})
    pi = resp.data if isinstance(resp.data, dict) else {}
    if resp.ok and pi.get("id"):
        ctx.state.payments[case] = {"id": pi["id"], "status": pi.get("status"), "amount": spec["amount"],
                                    "currency": CURRENCY, "merchant_order_id": body["merchant_order_id"],
                                    "created_at": pi.get("created_at") or iso(utcnow())}
        if pi.get("client_secret"):
            ctx.client_secrets[pi["id"]] = pi["client_secret"]
        pending_clear(ctx, key)
        return pi.get("status") or "REQUIRES_PAYMENT_METHOD"
    if resp.settled:
        pending_clear(ctx, key)
    hint = "" if resp.settled else "; re-running 'payments' resends the same request_id"
    raise StepFail(f"{case}: create failed: {resp.summary()}{hint}")


class CheckoutPage(http.server.BaseHTTPRequestHandler):
    """Airwallex.js needs an http origin, so the page is served from 127.0.0.1."""

    def do_GET(self) -> None:
        body = CHECKOUT_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def launch_browser(playwright: Any, error_type: type[Exception]) -> Any:
    for channel in BROWSER_CHANNELS:
        try:
            return playwright.chromium.launch(channel=channel, headless=True)
        except error_type:
            continue
    raise StepFail("no browser found: install Microsoft Edge or Google Chrome, or run 'playwright install chromium'")


def pay_in_browser(intent_id: str, client_secret: str) -> None:
    """Pay with the test card in the Card element. Only Airwallex's iframe ever receives the card data."""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise StepFail("playwright is not installed: run 'uv sync'") from exc
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CheckoutPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright, PlaywrightError)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                page.evaluate("cfg => window.startCheckout(cfg)",  # not in the URL: it would reach logs and history
                              {"intent_id": intent_id, "client_secret": client_secret, "currency": CURRENCY})
                page.wait_for_function("['ready', 'error'].includes(window.__state)", timeout=BROWSER_TIMEOUT_MS)
                if page.evaluate("window.__state") == "ready":
                    fields = page.frame_locator("#card iframe").locator("input")
                    fields.nth(0).fill(TEST_CARD)
                    fields.nth(1).fill(f"12/{(utcnow().year + 3) % 100:02d}")
                    fields.nth(2).fill(TEST_CVC)
                    time.sleep(0.5)  # the element validates its fields before it accepts a confirm
                    page.click("#submit")
                    page.wait_for_function("['done', 'error'].includes(window.__state)", timeout=BROWSER_TIMEOUT_MS)
                if page.evaluate("window.__state") == "error":
                    raise StepFail(f"card element: {page.evaluate('window.__error')}")
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise StepFail(f"browser payment failed: {exc}") from exc
    finally:
        server.shutdown()
        server.server_close()


def pay_payment(ctx: Ctx, case: str) -> str:
    pay = ctx.state.payments[case]
    log(f"  {case}: paying {pay['id']} in the card element")
    try:
        pay_in_browser(pay["id"], ctx.client_secrets[pay["id"]])
    except StepFail as exc:
        raise StepFail(f"{case}: {exc}") from exc
    pi = get_payment(ctx, case, "after-pay") or {}
    pay.update({"status": pi.get("status"), "payment_attempt_id": (pi.get("latest_payment_attempt") or {}).get("id")})
    ctx.state.save()
    return pi.get("status") or ""


def capture_payment(ctx: Ctx, case: str) -> str:
    pay = ctx.state.payments[case]
    resp = ctx.api.request("POST", f"/api/v1/pa/payment_intents/{pay['id']}/capture", label=f"pi-capture-{case}",
                           json_body={"request_id": new_request_id()}, meta={"kind": "pi_capture", "case": case})
    if not resp.ok:
        raise StepFail(f"{case}: capture failed: {resp.summary()}")
    return (resp.data or {}).get("status") or ""


def ensure_payment(ctx: Ctx, case: str) -> bool:
    """Make sure `case` has a SUCCEEDED payment intent. Returns True when something changed."""
    pay = ctx.state.payments.get(case)
    if pay and pay.get("id") and ctx.force:
        retire(ctx, case, "payment", "--force")
        pay = None
    status = ""
    if pay and pay.get("id"):
        if pay.get("status") == "SUCCEEDED":
            log(f"  {case}: {pay['id']} already SUCCEEDED (skip)")
            return False
        current = get_payment(ctx, case, "resume") or {}
        status = current.get("status", "")
        if current.get("client_secret"):
            ctx.client_secrets[pay["id"]] = current["client_secret"]
        log(f"  {case}: resuming {pay['id']} in status {status or 'unknown'}")
        if status in DEAD_END_PI:
            retire(ctx, case, "payment", f"stuck in {status}")
            pay = None
        elif status == "REQUIRES_PAYMENT_METHOD" and pay["id"] not in ctx.client_secrets:
            retire(ctx, case, "payment", "unpaid, and its client_secret is gone")
            pay = None
    if not (pay and pay.get("id")):
        log(f"  {case}: creating a USD {CASES[case]['amount']} payment intent")
        status = create_payment(ctx, case)
    if status == "REQUIRES_PAYMENT_METHOD":
        status = pay_payment(ctx, case)
    if status == "REQUIRES_CAPTURE":  # only if auto_capture was off
        status = capture_payment(ctx, case)
    for i in range(5):  # PENDING / PENDING_REVIEW should settle within seconds
        if status in ("SUCCEEDED", "CANCELLED", "REQUIRES_CUSTOMER_ACTION", "REQUIRES_PAYMENT_METHOD"):
            break
        time.sleep(2)
        status = (get_payment(ctx, case, f"poll{i + 1}") or {}).get("status", status)
    ctx.state.payments[case]["status"] = status
    ctx.state.save()
    if status != "SUCCEEDED":
        hint = " (3DS was requested)" if status == "REQUIRES_CUSTOMER_ACTION" else ""
        raise StepFail(f"{case}: payment {ctx.state.payments[case]['id']} ended in {status or 'unknown'}{hint}")
    log(f"  {case}: {ctx.state.payments[case]['id']} SUCCEEDED")
    return True


def find_orphan_dispute(ctx: Ctx, case: str, pi_id: str, phase: str) -> dict | None:
    """A dispute on this payment that state does not know about (a create whose response was lost)."""
    items = list_disputes(ctx, f"{phase}-{case}", {"payment_intent_id": pi_id, "size": 100}, case=case) or []
    known = known_dispute_ids(ctx)
    fresh = [d for d in items if is_dispute(d) and d.get("payment_intent_id") == pi_id and d["id"] not in known]
    if not fresh:
        return None
    return max(fresh, key=lambda d: parse_ts(d.get("created_at")) or EPOCH)


def store_dispute(ctx: Ctx, case: str, d: dict, pay: dict, reason_code: str, how: str) -> None:
    spec = CASES[case]
    ctx.state.disputes[case] = {"id": d["id"], "payment_intent_id": pay["id"], "reason_code": reason_code,
                                "created_stage": spec["stage"], "created_status": d.get("status"), "how": how}
    remember_dispute(ctx, case, d)
    if d.get("stage") not in (None, spec["stage"]) and how != "created":
        log(f"  WARNING: adopted dispute is at {d.get('stage')}, the scenario expects {spec['stage']}")
    log(f"  {case}: {d['id']} {how} at {spec['stage']} (status in that response: {d.get('status')})")


def create_dispute(ctx: Ctx, case: str) -> dict:
    spec = CASES[case]
    pay = ctx.state.payments.get(case) or {}
    if pay.get("status") != "SUCCEEDED":
        raise StepSkip(f"{case}: no SUCCEEDED payment in state; run 'payments' first")
    if spec["stage"] == "PRE_CHARGEBACK":
        raise StepFail("never create PRE_CHARGEBACK disputes: they auto-accept in the same second")
    if (ctx.state.disputes.get(case) or {}).get("id"):  # only reached with --force
        retire(ctx, case, "dispute", "--force")
    code = VISA_REASON_CODES[case]
    # The simulator takes no request_id: adopt a dispute a lost response left behind instead of a second one.
    orphan = find_orphan_dispute(ctx, case, pay["id"], "precheck")
    if orphan:
        store_dispute(ctx, case, orphan, pay, code, "adopted (found on the payment before creating)")
    else:
        body = {"payment_intent_id": pay["id"], "reason_code": code, "stage": spec["stage"],
                "due_at": due_at(), "amount": pay["amount"], "comment": spec["comment"]}
        resp = ctx.api.request("POST", "/api/v1/simulation/pa/payment_disputes/create",
                               label=f"sim-dispute-create-{case}", json_body=body, idempotent=False,
                               meta={"kind": "sim_create", "case": case,
                                     "guesses": ["VISA_REASON_CODES", "DUE_AT_FORMAT"]})
        if resp.ok and is_dispute(resp.data):
            store_dispute(ctx, case, resp.data, pay, code, "created")
        elif not resp.settled:
            orphan = find_orphan_dispute(ctx, case, pay["id"], "recover")
            if not orphan:
                raise StepFail(f"{case}: simulated create failed: {resp.summary()}; no dispute appeared on the "
                               "payment, so re-running 'disputes' is safe")
            store_dispute(ctx, case, orphan, pay, code, "reconciled (create response lost, dispute found)")
        else:
            raise StepFail(f"{case}: simulated create failed: {resp.summary()}")
    return wait_dispute(ctx, case, "after-create", actionable_or_final) or {}


def rfi_extras(ctx: Ctx, case: str) -> dict:
    """order_info / seller_info / delivery_info, which the docs label as RFI-stage fields."""
    pay, spec = ctx.state.payments.get(case) or {}, CASES[case]
    shipped, delivered = delivery_times(pay)
    created = parse_ts(pay.get("created_at")) or utcnow()
    extras = {
        "order_info": {"id": pay.get("merchant_order_id") or spec["order_no"], "invoice_number": f"INV-{spec['order_no']}",
                       "created_at": iso(created), "total_amount": spec["amount"], "total_currency": CURRENCY,
                       "products": [{"name": spec["product"], "sku": spec["sku"], "quantity": 1,
                                     "unit_price": spec["amount"], "type": "physical_good"}]},
        "seller_info": dict(STORE),
        "delivery_info": {"name": customer_name(), "address": CUSTOMER_ADDRESS_LINE,
                          "shipping_company": SHIPPING_COMPANY, "tracking_number": TRACKING_NUMBER,
                          "shipped_at": shipped, "delivered_at": delivered, "status": DELIVERY_STATUS},
    }
    return {k: v for k, v in extras.items() if k in RFI_CHALLENGE_EXTRAS}


def delivery_times(pay: dict) -> tuple[str, str]:
    """Shipped and delivered shortly after the (sandbox) payment, never in the future."""
    now = utcnow()
    base = parse_ts(pay.get("created_at")) or now - timedelta(minutes=5)
    shipped = min(base + timedelta(seconds=30), now - timedelta(seconds=2))
    delivered = min(base + timedelta(seconds=60), now - timedelta(seconds=1))
    return iso(shipped), iso(delivered)


def content_hash(spec: dict, fmt: str) -> str:
    """Hash of the evidence content itself (without the 'Generated ...' line), to tell documents apart."""
    text = f"{fmt}\n{spec['title']}\n" + "\n".join(spec["lines"])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upload_evidence(ctx: Ctx, key: str, spec: dict, fmt: str = "pdf") -> dict:
    """Generate a small PDF (or JPEG), check it, upload it to the files host, and record its file_id.
    File-id tracking (used_in, challenge_details) is what stops a file from being sent twice."""
    lines = spec["lines"] + ["", f"Generated {iso(utcnow())} for spike run {ctx.state.run_id} ({key})."]
    if fmt == "pdf":
        data, ext, ctype = make_pdf(spec["title"], lines), "pdf", "application/pdf"
        check_pdf(data)
    else:
        data, ext, ctype = make_jpeg(spec["title"] + "\n" + "\n".join(lines)), "jpg", "image/jpeg"
        check_jpeg(data)
    digest = hashlib.sha256(data).hexdigest()
    filename = f"{key}_{ctx.state.run_id}_{uuid.uuid4().hex[:4]}.{ext}"
    notes = spec["notes"][:MAX_NAME_LEN]
    if len(filename) > MAX_NAME_LEN:
        raise StepFail(f"filename {filename!r} is over {MAX_NAME_LEN} characters")
    save_copy(filename, data)
    resp = ctx.api.request(
        "POST", FILE_UPLOAD_PATH, host="files", label=f"file-upload-{key}",
        files={"file": (filename, data, ctype)}, form={"notes": notes}, idempotent=False,
        record_body={"multipart": {"file": {"filename": filename, "content_type": ctype,
                                            "bytes": len(data), "sha256": digest,
                                            "copy": f"fixtures/spike/{filename}"},
                                   "notes": notes}},
        meta={"kind": "upload", "file_key": key, "format": fmt, "guesses": ["FILE_UPLOAD_PATH", "FILE_ID_KEYS"]},
    )  # no request_id exists for uploads, so a timeout is not retried blindly (it could create a second file)
    body = resp.data if isinstance(resp.data, dict) else {}
    file_id = next((body[k] for k in FILE_ID_KEYS if body.get(k)), None)
    if not resp.ok or not file_id:
        raise StepFail(f"upload of {key} failed: {resp.summary()}")
    if key in ctx.state.files:
        ctx.state.data["file_history"].append(ctx.state.files[key])
    entry = {"file_id": file_id, "filename": filename, "format": fmt, "bytes": len(data), "sha256": digest,
             "content_sha256": content_hash(spec, fmt), "uploaded_at": iso(utcnow()), "used_in": []}
    ctx.state.files[key] = entry
    ctx.state.save()
    log(f"  {key}: uploaded {filename} ({len(data)} bytes) -> file_id {file_id}")
    return entry


def files_already_sent(ctx: Ctx, dispute: dict | None) -> set[str]:
    """File ids that must not go into round 2: anything in challenge_details, used, or from round 1."""
    sent = collect_file_ids((dispute or {}).get("challenge_details"))
    sent |= {f["file_id"] for f in ctx.state.all_files() if f.get("used_in") and f.get("file_id")}
    for key in ("evidence_1", "evidence_1_jpg"):
        fid = (ctx.state.files.get(key) or {}).get("file_id")
        if fid:
            sent.add(fid)
    return sent


def fresh_evidence(ctx: Ctx, key: str, spec: dict, fmt: str, avoid: set[str]) -> dict:
    """A file for `key` that was never sent before; uploads a new one when needed."""
    entry = ctx.state.files.get(key)
    if not entry or entry.get("file_id") in avoid or entry.get("used_in"):
        entry = upload_evidence(ctx, key, spec, fmt)
    if entry["file_id"] in avoid:
        raise StepFail(f"{key} matches a file already sent; rejected evidence must not be resent")
    if key.startswith("evidence_2"):
        first = {(ctx.state.files.get(k) or {}).get("content_sha256") for k in ("evidence_1", "evidence_1_jpg")}
        if entry.get("content_sha256") in first - {None}:
            raise StepFail(f"{key} has the same content as the round-1 evidence; round 2 needs new evidence")
    return entry


def jpeg_evidence_body(ctx: Ctx, base: dict, key: str, spec: dict, field_name: str, avoid: set[str]) -> dict | None:
    """Lazy body for the 'evidence-jpg' variant: uploads a JPEG only when that variant is reached."""
    try:
        entry = fresh_evidence(ctx, key, spec, "jpg", avoid)
    except StepFail as exc:
        log(f"    could not prepare a JPEG for the evidence object: {exc}")
        return None
    return {**base, "evidence": {field_name: [entry["file_id"]]}}


def record_check(ctx: Ctx, case: str, step: str, check: dict) -> None:
    text = describe_check(check)
    log(("  WARNING: " if not text.startswith("persisted") else "  ") + text)
    rec = ctx.state.disputes.setdefault(case, {})
    rec.setdefault("challenge_checks", {})[step] = check
    ctx.state.save()


# ----------------------------------------------------------------------------- steps
def step_login(ctx: Ctx) -> str:
    resp = ctx.api.login(force=True)
    expires = resp.data.get("expires_at") if resp and isinstance(resp.data, dict) else None
    return f"token received (memory only), expires_at={expires}"


def step_payments(ctx: Ctx) -> str:
    changed, failures = [], []
    cases = ctx.cases()
    for case in cases:
        try:
            if ensure_payment(ctx, case):
                changed.append(case)
        except StepFail as exc:
            failures.append(str(exc))
            log(f"  FAILED {exc}")
    if failures:
        raise StepFail("; ".join(failures))
    if not changed:
        raise StepSkip("the payments already SUCCEEDED in state (use --force for new ones)")
    return "SUCCEEDED: " + ", ".join(f"{c}={ctx.state.payments[c]['id']}" for c in cases)


def step_disputes(ctx: Ctx) -> str:
    created, skipped, failures = [], [], []
    for case in ctx.cases():
        existing = (ctx.state.disputes.get(case) or {}).get("id")
        if existing and not ctx.force:
            log(f"  {case}: dispute {existing} already in state (skip)")
            skipped.append(case)
            continue
        try:
            create_dispute(ctx, case)
            created.append(case)
        except StepSkip as exc:
            skipped.append(case)
            log(f"  SKIP {exc}")
        except StepFail as exc:
            failures.append(str(exc))
            log(f"  FAILED {exc}")
    if failures:
        raise StepFail("; ".join(failures))
    if not created:
        raise StepSkip("no new disputes (already in state, or payments missing)")
    return "created " + ", ".join(f"{c}={ctx.state.disputes[c]['id']}" for c in created) + (
        f"; skipped {', '.join(skipped)}" if skipped else "")


def step_read(ctx: Ctx) -> str:
    cases = [c for c in ctx.cases() if (ctx.state.disputes.get(c) or {}).get("id")]
    if not cases:
        raise StepSkip("no disputes in state; run 'disputes' first")
    got = sum(1 for case in cases if get_dispute(ctx, case, "read") is not None)
    queue = list_disputes(ctx, "queue", {"status": "REQUIRES_RESPONSE", "size": 100})
    log(f"  queue: {len(queue) if queue is not None else '?'} dispute(s) in REQUIRES_RESPONSE")
    for case in cases:
        list_disputes(ctx, f"by-pi-{case}", {"payment_intent_id": ctx.state.disputes[case]["payment_intent_id"]},
                      case=case)
    # Read-only probes of list-filter behaviour the docs leave open (allowed to fail).
    list_disputes(ctx, "probe-comma-status", {"status": "REQUIRES_RESPONSE,CHALLENGED", "size": 100},
                  probe="comma_separated_status")
    list_disputes(ctx, "probe-reason-code", {"reason_code": VISA_REASON_CODES["headphones"], "size": 100},
                  probe="reason_code_filter")
    if "headphones" in cases:  # prior purchases for the fraud case (FRAUDULENT / DUPLICATE_CHARGE only)
        ctx.api.request("GET", f"/api/v1/pa/payment_disputes/{dispute_id(ctx, 'headphones')}/related_payment_intents",
                        params={"page_size": 100}, label="dispute-related-pis-headphones",
                        meta={"kind": "related", "case": "headphones"})
    if got == 0 and queue is None:
        raise StepFail("every core read failed (dispute retrieve and the queue list)")
    return f"read {got}/{len(cases)} dispute(s); queue holds {len(queue) if queue is not None else '?'}"


def step_accept_small(ctx: Ctx) -> str:
    case = "ear_tips"
    skip_if_done(ctx, "accept-small")
    did = dispute_id(ctx, case)
    before = wait_dispute(ctx, case, "before-accept", actionable_or_final, tries=3)
    if not ctx.force and own_detail(before, "accept_details", "RFI"):
        return "reconciled: the dispute already holds our RFI accept (a previous response was lost)"
    require(ctx, before, {"RFI"}, {"REQUIRES_RESPONSE"}, "accept-small")
    amount = (ctx.state.payments.get(case) or {}).get("amount", CASES[case]["amount"])
    base = {"request_id": "", "reason": ACCEPT_REASON_LOW_VALUE, "accepted_by": ACTOR,
            "description": "USD 12 at risk is below the USD 15 chargeback fee; delivery scan is unsigned."}
    variants: list[Variant] = []
    if RFI_ACCEPT_REFUND is not None:
        variants.append(Variant("with-refund", {**base, "refund": {"amount": amount, **RFI_ACCEPT_REFUND}},
                                ["ACCEPT_REASON_LOW_VALUE", "RFI_ACCEPT_REFUND"]))
    variants.append(Variant("no-refund", base, ["ACCEPT_REASON_LOW_VALUE"]))
    name, _, resp, attempts = try_variants(
        ctx, f"/api/v1/pa/payment_disputes/{did}/accept", variants, label=f"dispute-accept-{case}",
        meta={"kind": "accept", "case": case, "stage_before": stage_of(before)}, still_valid=still_open(ctx, case, "RFI"))
    after = wait_dispute(ctx, case, "after-accept", lambda d: d.get("status") != "REQUIRES_RESPONSE", tries=3)
    refunds = list_refunds(ctx, case, "after-accept-small", tries=4)  # does an RFI accept create a refund?
    how = applied(resp, after, lambda d: d.get("status") == "ACCEPTED" or own_detail(d, "accept_details", "RFI") is not None)
    if not how:
        raise StepFail(f"accept failed: {attempts_summary(attempts)}")
    return (f"{how}; variant '{name}': {stage_of(after)}/{status_of(after)}; "
            f"refunds on the payment: {len(refunds) if refunds is not None else '?'}")


def step_upload(ctx: Ctx) -> str:
    existing = ctx.state.files.get("evidence_1")
    if existing and not ctx.force:
        raise StepSkip(f"evidence_1 already uploaded ({existing['file_id']}); use --force for a new file")
    entry = upload_evidence(ctx, "evidence_1", EVIDENCE_1)
    return f"evidence_1 file_id={entry['file_id']} ({entry['bytes']} bytes)"


def step_challenge_1(ctx: Ctx) -> str:
    case = "headphones"
    skip_if_done(ctx, "challenge-1")
    did = dispute_id(ctx, case)
    first = ctx.state.files.get("evidence_1")
    if not first:
        raise StepSkip("no evidence_1 file in state; run 'upload' first")
    before = wait_dispute(ctx, case, "before-challenge-1", actionable_or_final, tries=3)
    prior = own_detail(before, "challenge_details", "RFI")
    if prior and not ctx.force:
        mark_used(ctx, prior, "challenge-1")
        return "reconciled: the dispute already holds our RFI challenge (a previous response was lost)"
    require(ctx, before, {"RFI"}, {"REQUIRES_RESPONSE"}, "challenge-1")
    fid = first["file_id"]
    docs = {"documents": [{"type": CHALLENGE_DOC_TYPE, "file_ids": [fid],
                           "description": "Orders #1012, #1038, #1077: same card, device ID and IP, never disputed"}]}
    full = {"request_id": "", "reason": CHALLENGE_1_REASON, "challenge_method": CHALLENGE_METHOD,
            "challenged_by": ACTOR, **rfi_extras(ctx, case), "supporting_documents": docs}
    guesses = ["CHALLENGE_1_REASON", "CHALLENGE_METHOD", "CHALLENGE_DOC_TYPE", "RFI_CHALLENGE_EXTRAS",
               "SHIPPING_COMPANY", "DELIVERY_STATUS"]
    if SEND_PRODUCT_TYPE_AT_RFI:
        full["product_type"] = CHARGEBACK_PRODUCT_TYPE
        guesses.append("SEND_PRODUCT_TYPE_AT_RFI")
    minimal = {"request_id": "", "reason": CHALLENGE_1_REASON, "challenged_by": ACTOR, "supporting_documents": docs}
    ev_guesses = guesses + ["SEND_EVIDENCE_OBJECT_ON_STANDARD", "CHALLENGE_1_EVIDENCE_FIELD"]
    variants: list[Variant] = []
    if SEND_EVIDENCE_OBJECT_ON_STANDARD or CHALLENGE_METHOD == "AI_DISPUTE_AUTOMATION":
        variants.append(Variant("evidence-pdf", {**full, "evidence": {CHALLENGE_1_EVIDENCE_FIELD: [fid]}}, ev_guesses))
        if EVIDENCE_OBJECT_JPEG_FALLBACK:
            variants.append(Variant(
                "evidence-jpg",
                lambda: jpeg_evidence_body(ctx, full, "evidence_1_jpg", EVIDENCE_1, CHALLENGE_1_EVIDENCE_FIELD, set()),
                ev_guesses + ["EVIDENCE_OBJECT_JPEG_FALLBACK"], when=names_file_problem))
    variants.append(Variant("documents-only", full, guesses, when=not_reason_error))
    variants.append(Variant("minimal", minimal, ["CHALLENGE_1_REASON", "CHALLENGE_DOC_TYPE"], when=not_reason_error))
    if CHALLENGE_FALLBACK_REASON != CHALLENGE_1_REASON:
        variants.append(Variant("reason-fallback", {**minimal, "reason": CHALLENGE_FALLBACK_REASON},
                                ["CHALLENGE_1_REASON", "CHALLENGE_FALLBACK_REASON"], when=names_reason))
    name, body, resp, attempts = try_variants(
        ctx, f"/api/v1/pa/payment_disputes/{did}/challenge", variants, label=f"dispute-challenge1-{case}",
        meta={"kind": "challenge", "case": case, "round": 1, "stage_before": stage_of(before)},
        still_valid=still_open(ctx, case, "RFI"))
    after = wait_dispute(ctx, case, "after-challenge-1", lambda d: d.get("status") != "REQUIRES_RESPONSE", tries=3)
    how = applied(resp, after, lambda d: own_detail(d, "challenge_details", "RFI") is not None)
    if not how:
        raise StepFail(f"challenge-1 failed: {attempts_summary(attempts)}")
    record_check(ctx, case, "challenge-1", persisted_check(body, after or resp.data, "RFI"))
    mark_used(ctx, body, "challenge-1")
    return f"{how}; variant '{name}' reason={body.get('reason')}: {stage_of(after)}/{status_of(after)}"


def step_escalate(ctx: Ctx) -> str:
    case = "headphones"
    skip_if_done(ctx, "escalate")
    did = dispute_id(ctx, case)
    before = get_dispute(ctx, case, "before-escalate")
    created_stage = (ctx.state.disputes.get(case) or {}).get("created_stage")
    if before and before.get("stage") == "CHARGEBACK" and created_stage == "RFI" and not ctx.force:
        return "reconciled: the dispute is already at CHARGEBACK (escalated earlier)"
    # Escalating an unchallenged RFI case would burn the RFI round for good.
    require(ctx, before, {"RFI"}, {"CHALLENGED", "PENDING_DECISION"}, "escalate")
    variants = [
        Variant(ESCALATE_DUE_FIELD, {ESCALATE_DUE_FIELD: due_at(), "comment": ESCALATE_COMMENT},
                ["ESCALATE_DUE_FIELD", "DUE_AT_FORMAT"]),
        Variant("due_at+due_date", {"due_at": due_at(), "due_date": due_at(), "comment": ESCALATE_COMMENT},
                ["DUE_AT_FORMAT"], when=lambda r: "due" in r.blob()),
    ]
    name, _, resp, attempts = try_variants(
        ctx, f"/api/v1/simulation/pa/payment_disputes/{did}/escalate", variants, label=f"sim-dispute-escalate-{case}",
        idempotent=False,
        meta={"kind": "sim_escalate", "case": case, "marker": ESCALATE_MARKER, "stage_before": stage_of(before),
              "status_before": status_of(before)})
    if resp.ok and is_dispute(resp.data):
        remember_dispute(ctx, case, resp.data)
    after = wait_dispute(ctx, case, "after-escalate",
                         lambda d: d.get("stage") == "CHARGEBACK" and d.get("status") == "REQUIRES_RESPONSE", tries=6)
    moved = after is not None and stage_of(after) != stage_of(before)
    if resp.ok:
        how = "ok"
        if not moved:
            log(f"  WARNING: the simulator answered {resp.status} but the dispute is still {stage_of(after)}/{status_of(after)}")
    elif moved and not resp.settled:
        how = "reconciled: no clean response, but the dispute moved"
    else:
        raise StepFail(f"escalate failed: {attempts_summary(attempts)}")
    paths = find_text_paths(after, ESCALATE_MARKER) if after else []
    log(f"  escalate comment found at: {', '.join(paths) or 'nowhere on the dispute'}")
    return f"{how}; field '{name}': {stage_of(after)}/{status_of(after)}; comment at {', '.join(paths) or 'not found'}"


def step_challenge_2(ctx: Ctx) -> str:
    case = "headphones"
    skip_if_done(ctx, "challenge-2")
    dispute_id(ctx, case)
    before = wait_dispute(ctx, case, "before-challenge-2", actionable_or_final, tries=3)
    prior = own_detail(before, "challenge_details", "CHARGEBACK")
    if prior and not ctx.force:
        mark_used(ctx, prior, "challenge-2")
        return "reconciled: the dispute already holds our CHARGEBACK challenge (a previous response was lost)"
    require(ctx, before, {"CHARGEBACK"}, {"REQUIRES_RESPONSE"}, "challenge-2 (run 'escalate' first)")
    did = (before or {}).get("id") or dispute_id(ctx, case)
    avoid = files_already_sent(ctx, before)
    second = fresh_evidence(ctx, "evidence_2", EVIDENCE_2, "pdf", avoid)
    fid = second["file_id"]
    shipped, delivered = delivery_times(ctx.state.payments.get(case) or {})
    ai = (before or {}).get("ai_dispute_automation")
    ai = ai if isinstance(ai, dict) else {}
    method = CHALLENGE_METHOD
    if USE_AI_METHOD_WHEN_AVAILABLE and ai.get("status") == "AVAILABLE":
        method = "AI_DISPUTE_AUTOMATION"
    docs = {"documents": [{"type": CHALLENGE_DOC_TYPE, "file_ids": [fid],
                           "description": "UPS signed delivery and warranty registration of the shipped serial"}]}
    description = "Over-ear studio headphones; serial warranty-registered by the cardholder after signed delivery"
    full = {
        "request_id": "", "reason": CHALLENGE_2_REASON, "challenge_method": method, "challenged_by": ACTOR,
        "product_type": CHARGEBACK_PRODUCT_TYPE,  # "required when the stage is CHARGEBACK"
        "product_description": description,
        "customer_info": {"name": customer_name(), "email": CUSTOMER["email"], "phone_number": CUSTOMER["phone_number"],
                          "billing_address": CUSTOMER_ADDRESS_LINE, "ip": CUSTOMER_IP, "device_id": CUSTOMER_DEVICE_ID},
        "delivery_info": {"shipping_company": SHIPPING_COMPANY, "tracking_number": TRACKING_NUMBER,
                          "shipped_at": shipped, "delivered_at": delivered},
        "supporting_documents": docs,
    }
    minimal = {"request_id": "", "reason": CHALLENGE_2_REASON, "challenged_by": ACTOR,
               "product_type": CHARGEBACK_PRODUCT_TYPE, "product_description": description,
               "supporting_documents": docs}
    guesses = ["CHALLENGE_2_REASON", "CHALLENGE_METHOD", "CHARGEBACK_PRODUCT_TYPE", "CHALLENGE_DOC_TYPE",
               "SHIPPING_COMPANY"] + (["USE_AI_METHOD_WHEN_AVAILABLE"] if method != CHALLENGE_METHOD else [])
    ev_guesses = guesses + ["SEND_EVIDENCE_OBJECT_ON_STANDARD", "CHALLENGE_2_EVIDENCE_FIELD"]
    variants: list[Variant] = []
    if SEND_EVIDENCE_OBJECT_ON_STANDARD or method == "AI_DISPUTE_AUTOMATION":
        variants.append(Variant("evidence-pdf", {**full, "evidence": {CHALLENGE_2_EVIDENCE_FIELD: [fid]}}, ev_guesses))
        if EVIDENCE_OBJECT_JPEG_FALLBACK:  # a NEW JPEG: never the round-1 file
            variants.append(Variant(
                "evidence-jpg",
                lambda: jpeg_evidence_body(ctx, full, "evidence_2_jpg", EVIDENCE_2, CHALLENGE_2_EVIDENCE_FIELD,
                                           avoid | {fid}),
                ev_guesses + ["EVIDENCE_OBJECT_JPEG_FALLBACK"], when=names_file_problem))
    if method != "AI_DISPUTE_AUTOMATION":
        variants.append(Variant("documents-only", full, guesses, when=not_reason_error))
        variants.append(Variant("minimal", minimal, ["CHALLENGE_2_REASON", "CHARGEBACK_PRODUCT_TYPE",
                                                     "CHALLENGE_DOC_TYPE"], when=not_reason_error))
        if CHALLENGE_FALLBACK_REASON != CHALLENGE_2_REASON:
            variants.append(Variant("reason-fallback", {**minimal, "reason": CHALLENGE_FALLBACK_REASON},
                                    ["CHALLENGE_2_REASON", "CHALLENGE_FALLBACK_REASON"], when=names_reason))
    name, body, resp, attempts = try_variants(
        ctx, f"/api/v1/pa/payment_disputes/{did}/challenge", variants, label=f"dispute-challenge2-{case}",
        meta={"kind": "challenge", "case": case, "round": 2, "stage_before": stage_of(before),
              "ai_status": ai.get("status")},
        still_valid=still_open(ctx, case, "CHARGEBACK"))
    after = wait_dispute(ctx, case, "after-challenge-2", lambda d: d.get("status") != "REQUIRES_RESPONSE", tries=3)
    how = applied(resp, after, lambda d: own_detail(d, "challenge_details", "CHARGEBACK") is not None)
    if not how:
        raise StepFail(f"challenge-2 failed: {attempts_summary(attempts)}")
    record_check(ctx, case, "challenge-2", persisted_check(body, after or resp.data, "CHARGEBACK"))
    mark_used(ctx, body, "challenge-2")
    if stage_of(after) == "PRE_ARBITRATION" or status_of(after) == "PENDING_DECISION":
        log(f"  NOTE: after the CHARGEBACK response the dispute is {stage_of(after)}/{status_of(after)}; "
            "Visa fraud cases can go straight to pre-arbitration, which resolve also accepts")
    return f"{how}; variant '{name}' with new file(s) {sorted(collect_file_ids(body))}: {stage_of(after)}/{status_of(after)}"


def step_resolve(ctx: Ctx) -> str:
    case = "headphones"
    skip_if_done(ctx, "resolve")
    did = dispute_id(ctx, case)
    before = get_dispute(ctx, case, "before-resolve")
    expected = RESOLVED_STATUSES.get(RESOLVE_IN_FAVOR_OF, set())
    if before and before.get("status") in expected and not ctx.force:
        return f"reconciled: the dispute is already {before.get('status')}"
    # Resolving before the CHARGEBACK challenge would end the dispute with that round never answered.
    require(ctx, before, {"CHARGEBACK", "PRE_ARBITRATION"}, {"CHALLENGED", "PENDING_DECISION"}, "resolve")
    resp = ctx.api.request("POST", f"/api/v1/simulation/pa/payment_disputes/{did}/resolve",
                           label=f"sim-dispute-resolve-{case}", idempotent=False,
                           json_body={"in_favor_of": RESOLVE_IN_FAVOR_OF, "comment": RESOLVE_COMMENT},
                           meta={"kind": "sim_resolve", "case": case, "stage_before": stage_of(before),
                                 "status_before": status_of(before), "guesses": ["RESOLVE_IN_FAVOR_OF"]})
    if resp.ok and is_dispute(resp.data):
        remember_dispute(ctx, case, resp.data)
    after = wait_dispute(ctx, case, "after-resolve", lambda d: d.get("status") in FINAL_STATUSES, tries=6)
    if resp.ok:
        how = "ok"
    elif not resp.settled and after is not None and after.get("status") in expected:
        how = "reconciled: no clean response, but the dispute is resolved"
    else:
        raise StepFail(f"resolve failed: {resp.summary()}")
    return f"{how}; in_favor_of={RESOLVE_IN_FAVOR_OF}: {stage_of(after)}/{status_of(after)}"


def step_accept_chargeback(ctx: Ctx) -> str:
    case = "earbuds"
    skip_if_done(ctx, "accept-chargeback")
    did = dispute_id(ctx, case)
    before = wait_dispute(ctx, case, "before-accept", actionable_or_final, tries=3)
    if not ctx.force and own_detail(before, "accept_details", "CHARGEBACK"):
        return "reconciled: the dispute already holds our CHARGEBACK accept (a previous response was lost)"
    require(ctx, before, {"CHARGEBACK"}, {"REQUIRES_RESPONSE"}, "accept-chargeback")
    body = {"request_id": "", "reason": ACCEPT_REASON_VALID, "accepted_by": ACTOR,  # no refund object at CHARGEBACK
            "description": "Return delivered and no credit issued; the chargeback already returned the funds."}
    name, _, resp, attempts = try_variants(
        ctx, f"/api/v1/pa/payment_disputes/{did}/accept", [Variant("no-refund", body, ["ACCEPT_REASON_VALID"])],
        label=f"dispute-accept-{case}", meta={"kind": "accept", "case": case, "stage_before": stage_of(before)})
    after = wait_dispute(ctx, case, "after-accept", lambda d: d.get("status") != "REQUIRES_RESPONSE", tries=3)
    refunds = list_refunds(ctx, case, "after-accept-chargeback")
    how = applied(resp, after, lambda d: d.get("status") == "ACCEPTED"
                  or own_detail(d, "accept_details", "CHARGEBACK") is not None)
    if not how:
        raise StepFail(f"accept at CHARGEBACK failed: {attempts_summary(attempts)}")
    return (f"{how}; {stage_of(after)}/{status_of(after)}; "
            f"refunds on the payment: {len(refunds) if refunds is not None else '?'}")


def step_refunds(ctx: Ctx) -> str:
    counts, ok_calls, tried = [], 0, 0
    for case in ctx.cases(tuple(CASES)):
        if (ctx.state.payments.get(case) or {}).get("id"):
            tried += 1
            items = list_refunds(ctx, case, "refunds-step")
            ok_calls += items is not None
            counts.append(f"{case}={len(items) if items is not None else '?'}")
    resp = ctx.api.request("GET", "/api/v1/pa/refunds", params={"page_num": 0, "page_size": 100},
                           label="refunds-all", meta={"kind": "refunds_list", "case": None, "phase": "all"})
    for case in ctx.cases(tuple(CASES)):  # refunds the dispute objects point at
        for rid in (ctx.state.disputes.get(case) or {}).get("refund_ids") or []:
            ctx.api.request("GET", f"/api/v1/pa/refunds/{rid}", label=f"refund-get-{case}",
                            meta={"kind": "refund_get", "case": case})
    if not resp.ok and ok_calls == 0:
        raise StepFail(f"every refunds call failed ({tried} per-payment lists and the account list): {resp.summary()}")
    total = len((resp.data or {}).get("items") or []) if resp.ok and isinstance(resp.data, dict) else "?"
    return f"per payment: {', '.join(counts) or 'no payments'}; account first page: {total}"


def step_probe(ctx: Ctx) -> str:
    """Deliberately invalid calls on two throwaway disputes, to read the validation messages:
    one dispute for accept probes and one for challenge probes, so neither can mask the other."""
    skip_if_done(ctx, "probe")
    ready = []
    for case in PROBE_CASES:
        try:
            ensure_payment(ctx, case)
            if ctx.force or not (ctx.state.disputes.get(case) or {}).get("id"):
                create_dispute(ctx, case)
            ready.append(case)
        except (StepFail, StepSkip) as exc:
            log(f"  {case}: not available ({exc})")
    if not ready:
        raise StepFail("no probe dispute could be set up")
    results = []

    def probe(case: str, name: str, action: str, body: dict) -> None:
        did = dispute_id(ctx, case)
        before = get_dispute(ctx, case, f"before-{name}")
        if status_of(before) != "REQUIRES_RESPONSE":
            results.append(f"{name}: not sent (dispute is {stage_of(before)}/{status_of(before)})")
            return
        resp = ctx.api.request("POST", f"/api/v1/pa/payment_disputes/{did}/{action}", label=f"probe-{name}",
                               json_body=body, idempotent="request_id" in body,
                               meta={"kind": "probe", "case": case, "probe": name, "stage_before": stage_of(before)})
        results.append(f"{name}: {resp.summary()}")

    if "probe_challenge" in ready:
        pfile = ctx.state.files.get("probe_evidence")
        if not pfile:
            try:
                pfile = upload_evidence(ctx, "probe_evidence", EVIDENCE_PROBE)
            except StepFail as exc:
                log(f"  probe file upload failed ({exc}); challenge probes go without a file")
        docs = {"documents": [{"type": CHALLENGE_DOC_TYPE, "file_ids": [pfile["file_id"]] if pfile else [],
                               "description": "probe"}]}
        # Order matters: the invalid-reason probe first, since the no-request_id one may succeed.
        probe("probe_challenge", "challenge-invalid-reason", "challenge",
              {"request_id": new_request_id(), "reason": "NOT_A_REAL_REASON", "challenged_by": ACTOR,
               "supporting_documents": docs})
        probe("probe_challenge", "challenge-without-request-id", "challenge",
              {"reason": "OTHER_REASONS", "challenged_by": ACTOR, "supporting_documents": docs})
    if "probe_accept" in ready:
        probe("probe_accept", "accept-invalid-reason", "accept",
              {"request_id": new_request_id(), "reason": "NOT_A_REAL_REASON", "accepted_by": ACTOR})
        probe("probe_accept", "accept-without-request-id", "accept", {"reason": "OTHERS", "accepted_by": ACTOR})
    # Close whatever is still open, so the throwaway disputes leave the REQUIRES_RESPONSE queue.
    for case in ready:
        d = get_dispute(ctx, case, "before-cleanup")
        if status_of(d) == "REQUIRES_RESPONSE":
            resp = ctx.api.request("POST", f"/api/v1/pa/payment_disputes/{dispute_id(ctx, case)}/accept",
                                   label=f"probe-cleanup-accept-{case}",
                                   json_body={"request_id": new_request_id(), "reason": "OTHERS", "accepted_by": ACTOR,
                                              "description": "Closing a spike probe dispute"},
                                   meta={"kind": "accept", "case": case, "variant": "probe-cleanup",
                                         "stage_before": stage_of(d)})
            results.append(f"cleanup {case}: {resp.summary()}")
            get_dispute(ctx, case, "after-cleanup")
    return "; ".join(results)


STEPS: dict[str, Callable[[Ctx], str | None]] = {
    "login": step_login,
    "payments": step_payments,
    "disputes": step_disputes,
    "read": step_read,
    "accept-small": step_accept_small,
    "upload": step_upload,
    "challenge-1": step_challenge_1,
    "escalate": step_escalate,
    "challenge-2": step_challenge_2,
    "resolve": step_resolve,
    "accept-chargeback": step_accept_chargeback,
    "refunds": step_refunds,
    "probe": step_probe,
}
ALL_ORDER = ["login", "payments", "disputes", "read", "accept-small", "upload", "challenge-1",
             "escalate", "challenge-2", "resolve", "accept-chargeback", "refunds"]  # 'all' appends 'probe'


def safe_mark(ctx: Ctx, name: str, ok: bool, detail: str) -> None:
    try:
        ctx.state.mark(name, ok, detail)
    except OSError as exc:
        log(f"  WARNING: could not save state.json ({exc}); OneDrive or antivirus may be holding it")


def run_step(ctx: Ctx, name: str) -> tuple[str, str]:
    """Run one step; never raises, so 'all' can keep going."""
    ctx.api.step = name
    log("")
    log(f"=== {name} " + "=" * max(4, 70 - len(name)))
    owner = STEP_CASE.get(name)
    if ctx.only_case and ((owner and owner != ctx.only_case) or name == "probe"):
        msg = f"not part of --case {ctx.only_case}"
        log(f"--- {name}: SKIPPED: {msg}")
        return "skipped", msg
    try:
        detail = STEPS[name](ctx) or "ok"
    except StepSkip as exc:
        msg = scrub(str(exc))
        log(f"--- {name}: SKIPPED: {msg}")
        return "skipped", msg
    except StepFail as exc:
        detail = scrub(str(exc))
        safe_mark(ctx, name, False, detail)
        log(f"--- {name}: FAILED: {detail}")
        return "failed", detail
    except Exception as exc:  # unexpected bug or response shape: record it and move on
        detail = scrub(f"{type(exc).__name__}: {exc}")
        safe_mark(ctx, name, False, detail)
        log(f"--- {name}: FAILED (unexpected): {detail}")
        log(traceback.format_exc(limit=-8))  # the innermost frames, where it actually broke
        return "failed", detail
    safe_mark(ctx, name, True, detail)
    log(f"--- {name}: OK: {detail}")
    return "ok", detail


# ----------------------------------------------------------------------------- report
def load_records(folder: Path = FIXTURES) -> list[dict]:
    if not folder.is_dir():
        return []
    records = []
    for path in folder.glob("*.json"):
        if not re.match(r"\d+-", path.name):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict):
            rec["_file"] = path.name
            records.append(rec)
    return sorted(records, key=lambda r: r.get("seq") or 0)


def load_state_quiet() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _meta(r: dict) -> dict:
    return _dict(r.get("meta"))


def _kind(r: dict) -> str:
    return str(_meta(r).get("kind") or "")


def _status(r: dict) -> int | None:
    return _dict(r.get("response")).get("status")


def _ok(r: dict) -> bool:
    s = _status(r)
    return isinstance(s, int) and 200 <= s < 300


def _body(r: dict) -> dict:
    return _dict(_dict(r.get("request")).get("body"))


def _data(r: dict) -> Any:
    return _dict(r.get("response")).get("json")


def _err(r: dict) -> str:
    resp = _dict(r.get("response"))
    if resp.get("transport_error"):
        return str(resp["transport_error"])
    d = resp.get("json")
    if isinstance(d, dict):
        parts = [d.get(k) for k in ("code", "source", "message")]
        return " | ".join(str(p) for p in parts if p) or json.dumps(d)[:300]
    return str(resp.get("text") or "")[:300]


def _full_err(r: dict, limit: int = 2000) -> str:
    """The whole error body (allowed-value lists can be long)."""
    resp = _dict(r.get("response"))
    if resp.get("json") is not None:
        return json.dumps(resp["json"], ensure_ascii=False)[:limit]
    return str(resp.get("text") or resp.get("transport_error") or "")[:limit]


def _err_blob(r: dict) -> str:
    """Everything an error response said, lowercased, for keyword matching."""
    resp = _dict(r.get("response"))
    return f"{json.dumps(resp.get('json'))} {resp.get('text') or ''} {resp.get('transport_error') or ''}".lower()


def _inconclusive(r: dict) -> bool:
    """A failure that says nothing about the payload: no response, 5xx, 429, duplicate_request, wrong state."""
    status = _status(r)
    if status is None or status >= 500 or status == 429:
        return True
    code = str(_dict(_data(r)).get("code") or "")
    return code.startswith("duplicate_request") or code in STATE_ERROR_CODES or bool(STATE_ERROR_RE.search(_err_blob(r)))


def _outcome(r: dict) -> str:
    if not _ok(r):
        return f"HTTP {_status(r)}: {_err(r)}"
    d = _data(r)
    return f"HTTP {_status(r)} -> {d.get('stage')}/{d.get('status')}" if is_dispute(d) else f"HTTP {_status(r)}"


def final_attempts(records: list[dict]) -> list[dict]:
    """Drop attempts that were retried (the next attempt of the same call follows within a few records)."""
    out = []
    for i, r in enumerate(records):
        later = records[i + 1:i + 4]
        if any(n.get("label") == r.get("label") and (n.get("attempt") or 1) > (r.get("attempt") or 1) for n in later):
            continue
        out.append(r)
    return out


def dispute_cases(records: list[dict], state: dict) -> dict[str, str]:
    """dispute id -> scenario case name."""
    mapping = {d["id"]: case for case, d in _dict(state.get("disputes")).items() if _dict(d).get("id")}
    for h in state.get("history") or []:
        if _dict(h).get("kind") == "dispute" and h.get("id"):
            mapping.setdefault(h["id"], str(h.get("case")))
    for r in records:
        d, case = _data(r), _meta(r).get("case")
        if case and is_dispute(d):
            mapping.setdefault(d["id"], case)
    return mapping


def _refund_line(r: dict) -> str:
    items = _dict(_data(r)).get("items") or []
    return ", ".join(f"{i.get('id')} {i.get('amount')} {i.get('status')}" for i in items if isinstance(i, dict)) or "none"


def _challenge_checks(finals: list[dict]) -> dict[int, dict]:
    """seq of each successful challenge -> persisted_check against the last re-read before the next mutation."""
    out: dict[int, dict] = {}
    mutations = {"challenge", "accept", "sim_escalate", "sim_resolve", "probe"}
    for i, r in enumerate(finals):
        if _kind(r) != "challenge" or not _ok(r):
            continue
        case, target = _meta(r).get("case"), _data(r)
        for n in finals[i + 1:]:
            if _meta(n).get("case") != case:
                continue
            if _kind(n) in mutations:
                break
            if _kind(n) == "dispute_get" and _ok(n) and is_dispute(_data(n)):
                target = _data(n)
        out[r.get("seq") or 0] = persisted_check(_body(r), target if isinstance(target, dict) else None,
                                                 _meta(r).get("stage_before"))
    return out


def _reason_table(add: Callable[[str], None], calls: list[dict], documented: tuple, extra: Callable[[dict], str],
                  reason_types: dict[str, str], what: str, checks: dict[int, dict] | None = None) -> None:
    if not calls:
        add("  not run yet")
    for r in calls:
        m, b = _meta(r), _body(r)
        tag = str(m.get("variant") or m.get("probe") or "")
        case = str(m.get("case"))
        add(f"  {r.get('seq', 0):>4} {case:<15} {tag:<28} reason={b.get('reason', '(none)')} {extra(b)}")
        add(f"       dispute reason.type={reason_types.get(case, '?')}  stage before={m.get('stage_before', '?')}"
            f" -> {_outcome(r)}")
        chk = (checks or {}).get(r.get("seq") or 0)
        if chk:  # what challenge_details kept of this (successful) challenge
            add(f"       silent-drop check: {describe_check(chk)}")
            add(f"       evidence sent={chk.get('evidence_sent')} kept={chk.get('evidence_kept')}; "
                f"file ids sent={chk.get('file_ids_sent')} kept={chk.get('file_ids_kept')}")
    took = sorted({str(_body(r).get("reason")) for r in calls if _ok(r) and _body(r).get("reason")})
    refused = sorted({str(_body(r).get("reason")) for r in calls if not _ok(r) and _body(r).get("reason")} - set(took))
    if calls:
        add(f"  reasons the API accepted: {', '.join(took) or 'none yet'}")
        add(f"  reasons only ever rejected: {', '.join(refused) or 'none'} (read the error: it may be the stage, not the enum)")
    add(f"  documented request enum (not all exercised): {', '.join(documented)}")
    invalid = [r for r in calls if str(_meta(r).get("probe") or "").endswith("invalid-reason")]
    if not invalid:
        add(f"  UNANSWERED: the full {what} enum needs the probe step (python scripts/spike.py probe)")
    for r in invalid:
        add(f"  probe with an invalid {what} reason -> HTTP {_status(r)}; the full answer:")
        add(f"    {_full_err(r)}")


def print_report(all_runs: bool = False) -> None:
    """SPIKE REPORT: answers the open API questions from the recorded fixtures."""
    records = load_records()
    state = load_state_quiet()
    run_id = state.get("run_id")
    if run_id and not all_runs:
        records = [r for r in records if r.get("run_id") == run_id]
    finals = final_attempts(records)
    cases = dispute_cases(records, state)
    out: list[str] = []
    add = out.append

    def section(title: str) -> None:
        add("")
        add(title)
        add("-" * len(title))

    add("=" * 78)
    add("SPIKE REPORT")
    scope = "all runs" if all_runs or not run_id else f"run {run_id}"
    add(f"{scope}: {len(records)} recorded HTTP attempts in {FIXTURES.relative_to(ROOT).as_posix()}/")
    add("=" * 78)
    if not records:
        add("No recordings yet. Run: python scripts/spike.py all")
        log("\n".join(out))
        return

    reason_types: dict[str, str] = {}
    for r in finals:
        for d in dispute_objects(_data(r) if _ok(r) else None):
            rtype = _dict(d.get("reason")).get("type")
            if rtype and cases.get(d["id"]):
                reason_types[cases[d["id"]]] = str(rtype)

    section("1. Calls")
    counts = Counter(str(_status(r)) for r in records)
    add("  HTTP status counts: " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
    add(f"  retried attempts: {sum(1 for r in records if (r.get('attempt') or 1) > 1)} "
        "(same body and request_id; calls without a request_id retry only if nothing reached the server)")
    login = next((r for r in reversed(finals) if _kind(r) == "login" and _ok(r)), None)
    if login:
        add(f"  login: HTTP {_status(login)}, expires_at looks like {_dict(_data(login)).get('expires_at')!r}")

    section("2. Payment intents")
    for case in CASES:
        recs = [r for r in finals if _meta(r).get("case") == case and _kind(r).startswith("pi_")]
        if not recs:
            continue
        last = next((r for r in reversed(recs) if _ok(r) and isinstance(_data(r), dict)), None)
        pi = _dict(_data(last)) if last else {}
        attempt = _dict(pi.get("latest_payment_attempt"))
        auth = _dict(attempt.get("authentication_data"))
        add(f"  {case:<15} {pi.get('id', '?')} status={pi.get('status')} attempt={attempt.get('status')} "
            f"cvc={auth.get('cvc_result')} avs={auth.get('avs_result')}")
        for r in recs:
            if not _ok(r):
                add(f"      {r.get('label')}: {_outcome(r)}")

    section("3. Dispute stage/status after each step")
    rows: list[tuple[str, dict[str, str]]] = []  # one row per run of a step, in order
    for r in finals:
        step = str(r.get("step"))
        if not rows or rows[-1][0] != step:
            rows.append((step, {}))
        d = _data(r)
        if _ok(r) and is_dispute(d):
            rows[-1][1][cases.get(d["id"], "?")] = f"{d.get('stage')}/{d.get('status')}"
    for step, seen in rows:
        if seen:
            add(f"  {step:<18} " + "  ".join(f"{c}={v}" for c, v in seen.items()))
    add("  timeline (every dispute object a call returned):")
    for r in finals:
        d = _data(r)
        if _ok(r) and is_dispute(d):
            add(f"    {r.get('seq', 0):>4}  {cases.get(d['id'], '?'):<15} {str(d.get('stage')):<15} "
                f"{str(d.get('status')):<18} {r.get('label')}")

    accepts = [r for r in finals if _kind(r) == "accept" or (_kind(r) == "probe" and "accept" in str(_meta(r).get("probe")))]
    challenges = [r for r in finals
                  if _kind(r) == "challenge" or (_kind(r) == "probe" and "challenge" in str(_meta(r).get("probe")))]
    checks = _challenge_checks(finals)

    section("4. Accept: which reasons the API took")
    _reason_table(add, accepts, DOC_ACCEPT_REASONS, lambda b: f"refund={'yes' if 'refund' in b else 'no'}",
                  reason_types, "accept")

    section("5. Challenge: which reasons the API took, and what it kept")
    _reason_table(add, challenges, DOC_CHALLENGE_REASONS, lambda b: (
        f"method={b.get('challenge_method', '-')} product_type={b.get('product_type', '-')} "
        f"evidence={','.join(sorted(_dict(b.get('evidence')))) or '-'}"), reason_types, "challenge", checks)

    section("6. request_id on accept / challenge")
    add("  docs: request_id is REQUIRED on accept and on challenge "
        "(docs/api/payments/payment_disputes/accept.md, challenge.md)")
    calls = accepts + challenges
    with_id = [r for r in calls if "request_id" in _body(r)]
    without = [r for r in calls if "request_id" not in _body(r)]
    add(f"  sent with a fresh uuid4 request_id: {len(with_id)} call(s), {sum(1 for r in with_id if _ok(r))} succeeded")
    for r in without:
        add(f"  sent WITHOUT request_id [{_meta(r).get('probe')}]: {_outcome(r)}")
    if not without:
        add("  never sent without request_id: UNANSWERED by this run (python scripts/spike.py probe)")
    for r in records:
        if not _ok(r) and ("request_id" in _err(r).lower() or "duplicate_request" in _err(r).lower()):
            add(f"  error mentioning request_id: {r.get('label')}: {_err(r)}")
    echoed = any("request_id" in _dict(_data(r)) for r in with_id if _ok(r))
    add(f"  request_id echoed back in the dispute response: {'yes' if echoed else 'no'}")

    section("7. Is the escalate comment visible on the dispute?")
    escalates = [r for r in finals if _kind(r) == "sim_escalate"]
    if not escalates:
        add("  escalate not run yet")
    for r in escalates:
        add(f"  {r.get('seq', 0):>4} body keys {sorted(_body(r))} from "
            f"{_meta(r).get('stage_before')}/{_meta(r).get('status_before')} -> {_outcome(r)}")
    target = _dict(_dict(state.get("disputes")).get("headphones")).get("id")
    first_escalate = escalates[0] if escalates else None
    if first_escalate:
        marker = str(_meta(first_escalate).get("marker") or ESCALATE_MARKER)
        target = target or _dict(_data(first_escalate)).get("id")
        later = [r for r in finals if (r.get("seq") or 0) >= (first_escalate.get("seq") or 0) and _ok(r)
                 and is_dispute(_data(r)) and _data(r).get("id") == target]
        paths = sorted({p for r in later for p in find_text_paths(_data(r), marker)})
        add(f"  comment text found at: {', '.join(paths) if paths else 'NOT FOUND in any later dispute object'}")
        if later:
            last = _data(later[-1])
            add(f"  latest issuer_comment={last.get('issuer_comment')!r} issuer_documents={last.get('issuer_documents')!r}")

    section("8. ai_dispute_automation and challenge_method")
    seen_ai: dict[str, set[str]] = {}
    absent: dict[str, set[str]] = {}  # stage -> kinds of response that lacked the field
    methods: set[str] = set()
    for r in finals:
        for d in dispute_objects(_data(r) if _ok(r) else None):
            stage = str(d.get("stage"))
            if "ai_dispute_automation" in d:
                seen_ai.setdefault(stage, set()).add(json.dumps(d["ai_dispute_automation"], sort_keys=True)[:200])
            else:
                absent.setdefault(stage, set()).add(_kind(r))
            for c in d.get("challenge_details") or []:
                if isinstance(c, dict):
                    methods.add(f"{c.get('stage')}:{c.get('challenge_method')}")
    for stage, values in seen_ai.items():
        add(f"  ai_dispute_automation present at {stage}: {' | '.join(sorted(values))}")
    for stage, kinds in absent.items():
        add(f"  ai_dispute_automation absent at {stage} in: {', '.join(sorted(kinds))} responses")
    if not seen_ai and not absent:
        add("  no dispute objects recorded yet")
    add(f"  challenge_details[].challenge_method seen (stage:method): {', '.join(sorted(methods)) or 'none'}")

    section("9. Did accept at CHARGEBACK work? (earbuds)")
    chargeback_accepts = [r for r in accepts if _meta(r).get("case") == "earbuds"]
    if not chargeback_accepts:
        add("  accept-chargeback not run yet")
    for r in chargeback_accepts:
        add(f"  stage before={_meta(r).get('stage_before')} reason={_body(r).get('reason')} "
            f"refund object sent={'yes' if 'refund' in _body(r) else 'no'} -> {_outcome(r)}")
    lists = [r for r in finals if _kind(r) == "refunds_list" and _meta(r).get("case") == "earbuds" and _ok(r)]
    if chargeback_accepts and lists:
        add(f"  refunds on the earbuds payment afterwards: {_refund_line(lists[-1])}")

    section("10. Did the RFI accept create a refund? (ear tips)")
    rfi_accepts = [r for r in accepts if _meta(r).get("case") == "ear_tips"]
    if not rfi_accepts:
        add("  accept-small not run yet")
    else:
        ref = next((r for r in rfi_accepts if _ok(r)), rfi_accepts[-1])
        add(f"  accept variant '{_meta(ref).get('variant')}' (refund object sent: "
            f"{'yes' if 'refund' in _body(ref) else 'no'}) -> {_outcome(ref)}")
        if _ok(ref):
            add(f"  refunds[] in the accept response: {_dict(_data(ref)).get('refunds')!r}")
            add(f"  accept_details in the accept response: {json.dumps(_dict(_data(ref)).get('accept_details'))[:600]}")
        gets = [r for r in finals if _kind(r) == "dispute_get" and _meta(r).get("case") == "ear_tips"
                and (r.get("seq") or 0) > (ref.get("seq") or 0) and _ok(r)]
        if gets:
            add(f"  refunds[] on the dispute afterwards: {_dict(_data(gets[-1])).get('refunds')!r}")
        lists = [r for r in finals if _kind(r) == "refunds_list" and _meta(r).get("case") == "ear_tips" and _ok(r)]
        if lists:
            items = [i for i in _dict(_data(lists[-1])).get("items") or [] if isinstance(i, dict)]
            add(f"  GET /pa/refunds for the payment: {len(items)} refund(s)")
            for it in items:
                add(f"    {it.get('id')} amount={it.get('amount')} {it.get('currency')} status={it.get('status')} "
                    f"reason={it.get('reason')!r} request_id={it.get('request_id')!r}")
            add("  -> YES, the payment has a refund after the RFI accept" if items
                else "  -> NO refund found: the merchant may need to refund it (check again before creating one)")

    section("11. Validation and other errors (every failed attempt)")
    failed = [r for r in records if not _ok(r)]
    if not failed:
        add("  none")
    for r in failed:
        add(f"  {r.get('seq', 0):>4} [{r.get('step')}] {r.get('label')} (attempt {r.get('attempt', 1)}): "
            f"HTTP {_status(r)}: {_err(r)}")
        details = _dict(_data(r)).get("details")
        if details:
            add(f"       details: {json.dumps(details)[:2000]}")

    section("12. Other observations")
    for r in finals:
        kind, d = _kind(r), _data(r)
        if kind == "sim_create" and _ok(r) and is_dispute(d):
            reason = _dict(d.get("reason"))
            add(f"  create {cases.get(d['id'], '?')}: sent reason_code={_body(r).get('reason_code')!r} "
                f"stage={_body(r).get('stage')} -> {d.get('stage')}/{d.get('status')}, "
                f"reason.original_code={reason.get('original_code')!r}, reason.type={reason.get('type')}, "
                f"payment_method_type={d.get('payment_method_type')!r}, card_brand={d.get('card_brand')!r}, "
                f"mode={d.get('mode')}, due_at={d.get('due_at')!r}")
        elif kind == "sim_resolve":
            add(f"  resolve in_favor_of={_body(r).get('in_favor_of')} from "
                f"{_meta(r).get('stage_before')}/{_meta(r).get('status_before')} -> {_outcome(r)}")
        elif kind == "upload":
            add(f"  upload {_meta(r).get('file_key')} ({_meta(r).get('format', 'pdf')}): {_outcome(r)}; "
                f"response keys {sorted(_dict(d))}; created={_dict(d).get('created')!r}")
        elif kind == "dispute_list" and _meta(r).get("probe"):
            items = _dict(d).get("items") if _ok(r) else None
            add(f"  list filter probe {_meta(r).get('probe')} {_meta(r).get('params')}: {_outcome(r)}"
                + (f", {len(items)} item(s)" if isinstance(items, list) else ""))
        elif kind == "related":
            items = _dict(d).get("items") if _ok(r) else None
            add(f"  related_payment_intents (headphones): {_outcome(r)}"
                + (f", {len(items)} item(s)" if isinstance(items, list) else ""))
        elif r.get("step") == "challenge-2" and kind == "dispute_get" and _ok(r) and is_dispute(d) and (
                d.get("stage") == "PRE_ARBITRATION" or d.get("status") == "PENDING_DECISION"):
            add(f"  after challenge-2 the dispute went to {d.get('stage')}/{d.get('status')} ({r.get('label')})")
    queue = next((r for r in finals if _kind(r) == "dispute_list" and _ok(r) and not _meta(r).get("probe")
                  and isinstance(_data(r), dict)), None)
    if queue:
        page = _data(queue)
        add(f"  list response keys besides items: {sorted(k for k in page if k != 'items')}; "
            f"page_after={page.get('page_after')!r} page_before={page.get('page_before')!r}")
    for step, info in _dict(state.get("steps")).items():
        if "reconciled" in str(_dict(info).get("detail")):
            add(f"  step {step} was reconciled from the dispute, not from a clean response: {_dict(info).get('detail')}")

    section("13. UNCONFIRMED guesses in this file and what the API said")
    add("  [ok] accepted and kept | [CHECK] some errors name this field | [LIKELY WRONG] no call using it")
    add("  succeeded and an error names it | [UNKNOWN] no call using it succeeded, errors do not name it")
    add("  [ACCEPTED BUT DROPPED] 2xx, but challenge_details lacks what it sent | [untested] not used yet")
    add("  Failures that say nothing about the payload (no response, 5xx, 429, duplicate_request, wrong")
    add("  dispute state) are counted as inconclusive, not as rejections.")
    for name, hints in GUESSES.items():
        tagged = [r for r in finals if name in (_meta(r).get("guesses") or [])]
        value = globals().get(name)
        used = [r for r in tagged if _ok(r) or not _inconclusive(r)]
        if not used:
            add(f"  [{'INCONCLUSIVE' if tagged else 'untested'}] {name} = {value!r}"
                + (f": {len(tagged)} call(s), none with a payload verdict" if tagged else ""))
            continue
        rejected = [r for r in used if not _ok(r)]
        pointed = [r for r in rejected if not hints or any(h in _err_blob(r) for h in hints)]
        succeeded = len(used) - len(rejected)
        drop_hints = DROP_HINTS.get(name, ())
        dropped = [r for r in used if _ok(r) and drop_hints and any(
            x.startswith(drop_hints) for x in checks.get(r.get("seq") or 0, {}).get("dropped", []))]
        if succeeded == 0:
            verdict = "LIKELY WRONG" if pointed else "UNKNOWN"
        elif dropped:
            verdict = "ACCEPTED BUT DROPPED"
        else:
            verdict = "CHECK" if pointed else "ok"
        skipped = len(tagged) - len(used)
        add(f"  [{verdict}] {name} = {value!r}: {succeeded} ok, {len(rejected)} rejected, "
            f"{len(pointed)} of those name {'/'.join(hints) or 'anything'}"
            + (f", {skipped} inconclusive" if skipped else ""))
        for r in (pointed or (rejected if verdict == "UNKNOWN" else []))[:2]:
            add(f"      {r.get('label')}: {_err(r)}")
        for r in dropped[:2]:
            add(f"      {r.get('label')}: {describe_check(checks[r.get('seq') or 0])}")
    add("")
    add("Recorded bodies show the values actually sent; this section shows the constants as they are now.")
    log("\n".join(out))


def safe_report(all_runs: bool = False) -> None:
    try:
        print_report(all_runs)
    except Exception:  # a surprising response shape must not lose the run's summary
        log("report failed:\n" + traceback.format_exc())


# ----------------------------------------------------------------------------- main
def archive_fixtures() -> None:
    """Move earlier recordings, files and state into fixtures/spike/archive-<timestamp>/."""
    if not FIXTURES.is_dir():
        return
    items = [p for p in FIXTURES.iterdir() if not p.name.startswith(("archive-", "."))]
    if not items:
        return
    dest = FIXTURES / f"archive-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    dest.mkdir(parents=True)
    for p in items:
        shutil.move(str(p), str(dest / p.name))
    log(f"archived {len(items)} file(s) to {dest.relative_to(ROOT).as_posix()}/")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # odd characters in API text must not crash a Windows console
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="spike.py", description="Airwallex sandbox spike for Disputable (Kit 4).",
                                     epilog="See the module docstring for the scenario and the files it writes.")
    parser.add_argument("step", choices=[*ALL_ORDER, "probe", "all", "report"], help="step to run")
    parser.add_argument("--force", action="store_true",
                        help="redo steps that state.json marks as done (creates new objects, clears dependent steps)")
    parser.add_argument("--case", choices=MAIN_CASES, help="limit payments, disputes, read, refunds and --force to one case")
    parser.add_argument("--no-probe", action="store_true", help="leave the probe step out of 'all'")
    parser.add_argument("--reset", action="store_true", help="archive fixtures/spike/* first and start a new run")
    parser.add_argument("--all-runs", action="store_true", help="report: include the recordings of earlier runs")
    args = parser.parse_args(argv)
    if args.reset:
        archive_fixtures()
    if args.step == "report":
        safe_report(args.all_runs)
        return 0
    cfg = load_config()
    state = State(STATE_PATH)
    api = Api(cfg, Recorder(FIXTURES), state)
    ctx = Ctx(api=api, state=state, force=args.force, only_case=args.case)
    log(f"Disputable spike | run {state.run_id} | {cfg.base_url} | files {cfg.files_url}"
        + (f" | case {args.case}" if args.case else "") + (" | --force" if args.force else ""))
    if args.step == "all":
        names = ALL_ORDER + ([] if args.no_probe or args.case else ["probe"])
    else:
        names = [args.step]
    results: list[tuple[str, str, str]] = []
    try:
        for i, name in enumerate(names):
            outcome, detail = run_step(ctx, name)
            results.append((name, outcome, detail))
            if name == "login" and outcome == "failed" and len(names) > 1:
                log("\nlogin failed: skipping every other step (they all need a token)")
                results += [(rest, "skipped", "no token: login failed") for rest in names[i + 1:]]
                break
    finally:
        api.close()
        try:
            state.save()
        except OSError as exc:
            log(f"WARNING: could not save state.json ({exc})")
    if args.step == "all":
        log("")
        log("STEP SUMMARY")
        for name, outcome, detail in results:
            log(f"  {name:<18} {outcome.upper():<8} {detail[:110]}")
        log("")
        safe_report()
    return 1 if any(outcome == "failed" for _, outcome, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
