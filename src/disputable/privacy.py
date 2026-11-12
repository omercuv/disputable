"""Privacy: what a recording may keep, and the checks that refuse a file before it goes into the public repo.

The scrubber is scripts/spike.py's, moved here with its secrets and allowed addresses as arguments: redact() and
redact_headers() blank the fields that hold secrets, scrub_strings() and scrub() remove every known secret and every
address that isn't allowed, and hide_account_id() puts a stand-in for the Airwallex account id wherever a file id or the
store URL Airwallex fills in gives it away. tests/test_privacy.py runs both copies on the same inputs.

Readings are checked, not scrubbed: scrubbing would change the seed's documentation IPs inside Claude's quotes, and the
checks would then drop those facts. reading_problems() refuses instead. Every check names places and kinds, never the
values, so its lines can be printed and kept. This module never imports the seed: the stores come in as arguments.

The recorder, the eval and the tests use it like this:
    scrubber = Scrubber([client_id, api_key], allowed.addresses), then scrubber.learn(token) for each login's token
    each exchange's headers go through Scrubber.redact_headers() first, by their names
    after the first upload's answer, before that exchange is written: scrubber.learn(account_id_of(file_id))
    line = scrubber.dumps(exchange)   # the spike's order: redact, scrub_strings, json.dumps, scrub, hide_account_id
    keep() refuses a reading when reading_problems(reader.to_json(reading), Allowed.of(FERNHILL, ENDING_2, SUITE))
    the story run keeps or reuses a reading only when reading_problems(..., exact=True) is [], as check_files() checks it
    before a folder is copied into the repo: check_files(its files, allowed, scrubber.secrets) must be []
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping

if TYPE_CHECKING:
    from disputable.store import Store

# ----------------------------------------------------------------------------- the spike's scrubbing
REDACTED = "<redacted>"
SECRET_HEADERS = frozenset({"authorization", "x-api-key", "x-client-id", "cookie", "set-cookie",
                            "cf-ray"})  # cf-ray ends in the code of the Cloudflare site that answered: a rough location
SECRET_KEYS = frozenset({"token", "access_token", "refresh_token", "client_secret", "api_key",
                         "device_data"})  # device_data holds the paying browser's real IP address
ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)"  # a port or the end of a sentence may follow
                     r"|(?<![\w.])(?:[0-9A-Fa-f]{0,4}:){3,7}[0-9A-Fa-f]{0,4}(?![\w:]|\.\d)")
LOCAL = "127.0.0.1"  # the local checkout page
ACCOUNT_ID_STAND_IN = "00000000-0000-4000-8000-000000000000"
SEPARATOR = ",|,"  # a file id is base64 of '<account id>,|,<region>,|,<file name>'
MIN_SECRET = 6  # a shorter value would blank ordinary words, so the spike skips it too


class Scrubber:
    """The spike's scrubbing for one run: the secrets it knows, and the addresses it lets stand."""

    def __init__(self, secrets: Iterable[str], allowed: frozenset[str]) -> None:
        self.allowed = frozenset(allowed)
        self._secrets: set[str] = set()
        for secret in secrets:
            self.learn(secret)

    @property
    def secrets(self) -> frozenset[str]:
        """Every secret it knows, for check_files(). Never printed."""
        return frozenset(self._secrets)

    def learn(self, secret: str) -> None:
        """Scrub this value from every later text, as the spike's add_secret() does: a login's token, or the account id
        decoded from the first file id. A value that isn't text, or is under 6 characters, is skipped."""
        if isinstance(secret, str) and len(secret) >= MIN_SECRET:
            self._secrets.add(secret)

    def scrub(self, text: str) -> str:
        """Last line of defence: remove every known secret value and any address that isn't allowed. An error page can
        print the caller's IP in a field no rule above knows about."""
        for value in sorted(self._secrets, key=lambda secret: (-len(secret), secret)):
            text = text.replace(value, REDACTED)
        return ADDRESS.sub(lambda found: found.group() if found.group() in self.allowed else REDACTED, text)

    def scrub_strings(self, value: Any) -> Any:
        """Each string before it's encoded: in JSON text a line break reads \\n, and that n hides the address after it."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {key: self.scrub_strings(inner) for key, inner in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_strings(inner) for inner in value]
        return value

    @staticmethod
    def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
        return {k: (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in (headers or {}).items()}

    @staticmethod
    def redact(obj: Any) -> Any:
        """Copy of obj with secret fields redacted and card numbers masked."""
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                lowered = str(key).lower()
                if lowered in SECRET_KEYS and value not in (None, ""):
                    out[key] = REDACTED
                elif lowered == "card" and isinstance(value, dict):
                    out[key] = _mask_card(Scrubber.redact(value))
                else:
                    out[key] = Scrubber.redact(value)
            return out
        if isinstance(obj, list):
            return [Scrubber.redact(v) for v in obj]
        return obj

    @staticmethod
    def hide_account_id(text: str) -> str:
        """For recordings only. A file id is base64 of '<account id>,|,<region>,|,<file name>', and Airwallex also fills
        in a store URL from the id, so both forms get a stand-in. Run state that a resume needs keeps the real ids."""
        found = set(re.findall(r'github\.com/([0-9a-f-]{36})"', text))
        for token in re.findall(r'"([A-Za-z0-9+/]{48,})"', text):
            try:
                decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode()
            except ValueError:  # not base64 or not text
                continue
            if SEPARATOR in decoded:
                found.add(decoded.split(SEPARATOR)[0])
        for account_id in found:
            if len(account_id) == len(ACCOUNT_ID_STAND_IN):  # 36 bytes, so its base64 is a clean prefix of each file id
                text = text.replace(account_id, ACCOUNT_ID_STAND_IN).replace(_base64(account_id),
                                                                             _base64(ACCOUNT_ID_STAND_IN))
        return text

    def dumps(self, value: Any, **options: Any) -> str:
        """value as JSON text, scrubbed in the spike's order: redact(), scrub_strings(), json.dumps(value, **options),
        scrub(), then hide_account_id()."""
        return self.hide_account_id(self.scrub(json.dumps(self.scrub_strings(self.redact(value)), **options)))


def _mask_card(card: dict) -> dict:
    number = card.get("number")
    if isinstance(number, str) and len(number) > 10:
        card["number"] = number[:6] + "*" * (len(number) - 10) + number[-4:]
    if card.get("cvc"):
        card["cvc"] = "***"
    return card


def _base64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ----------------------------------------------------------------------------- base64 and file ids
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{48,}={0,2}")
_JOINS = re.compile(r"[+/_-]")
MIN_TOKEN = 48  # an Airwallex file id is longer: 48 characters of base64 hold the account id alone


def base64_tokens(text: str) -> list[str]:
    """Every base64 run of 48 or more characters in text, for the secrets checks, with the places a token could start
    inside it: each piece between the characters a path or a name joins one with (+ / _ -), so a file id in a URL is
    found too, and each piece without its first one, two or three characters, so one glued to a word is found too.
    When one of these is a character too long to be base64, it comes without its last character as well, so a token
    with a word glued after it is found too."""
    found: dict[str, None] = {}
    for run in _BASE64_RUN.findall(text):
        for piece in (run, *_JOINS.split(run.rstrip("="))):
            for start in range(4):
                token = piece[start:]
                if len(token) >= MIN_TOKEN:
                    found[token] = None
                body = token.rstrip("=")
                if len(body) % 4 == 1 and len(body) - 1 >= MIN_TOKEN:  # base64 is never 1 past a whole group of 4
                    found[body[:-1]] = None
    return list(found)


def decode_base64(token: str) -> str | None:
    """The text a base64 token decodes to, standard or URL-safe, with its padding or without, or None when it isn't
    base64. Bytes that aren't UTF-8, such as those of a cut-off start, read as U+FFFD, so the text after them shows."""
    body = token.rstrip("=")
    urlsafe = "-" in body or "_" in body
    if urlsafe and ("+" in body or "/" in body):
        return None
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_" if urlsafe else None, validate=True)
    except ValueError:  # binascii.Error: not base64, or one character past a whole group; or not ASCII at all
        return None
    return raw.decode("utf-8", "replace")


def account_id_of(file_id: str) -> str | None:
    """The account id an Airwallex file id carries: its base64 decoded, up to the first ,|,. None when it isn't a file
    id. The recorder learns it as a secret, and only the tests compare it: it is never printed."""
    text = decode_base64(file_id.strip()) if isinstance(file_id, str) else None
    if text is None or "\ufffd" in text or SEPARATOR not in text:
        return None
    return text.split(SEPARATOR)[0] or None


# ----------------------------------------------------------------------------- what the seed holds
ANTHROPIC_KEY = re.compile(r"(?i)sk-ant-")
KEY_HEADER = re.compile(r"(?i)x-api-key")
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}")
API_ID = re.compile(r"(?<![A-Za-z0-9])(?:req|msg|toolu)_[A-Za-z0-9]{16,}")
EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+(?:[ ().-]{0,2}\d){10,15}(?![ ().-]{0,2}\d)"  # + and 10 to 15 digits
                   r"|(?<!\d)\(\d{3}\) ?\d{3}-\d{4}(?!\d)"  # (xxx) xxx-xxxx
                   r"|(?<![\d-])\d{3}-\d{3}-\d{4}(?![\d-])")  # xxx-xxx-xxxx
UUID = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
                  r"(?![0-9A-Fa-f])")
LOCAL_PATH = re.compile(r"(?i:\b[a-z]:[\\/]+users[\\/])|/home/|/Users/")
FICTIONAL_PHONE = re.compile(r"\d*55501\d\d")  # 555-0100 to 555-0199: the numbers kept for fiction
DOCUMENTATION = tuple(ipaddress.ip_network(network) for network in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
RESERVED_ENDINGS = (".example", ".test", ".invalid")


@dataclass(frozen=True)
class Allowed:
    """The made-up values the seed's records hold, so a check can tell them from anything else."""
    addresses: frozenset[str]  # IPs as the records write them, and 127.0.0.1: the scrubber's allow-list
    emails: frozenset[str]  # in lower case
    phones: frozenset[str]  # the last 10 digits of each number
    uuids: frozenset[str]  # in lower case: the device ids

    @classmethod
    def of(cls, *stores: Store) -> Allowed:
        """Every address, email, phone and UUID in any field of any record of these stores."""
        texts = [text for store in stores for text in _texts(store)]
        return cls(frozenset({LOCAL, *(found for text in texts for found in ADDRESS.findall(text))}),
                   frozenset(found.lower() for text in texts for found in EMAIL.findall(text)),
                   frozenset(_digits(found)[-10:] for text in texts for found in PHONE.findall(text)),
                   frozenset(found.lower() for text in texts for found in UUID.findall(text)))


# What a check found, in plain words. A line names its place and one of these: never the value.
FOUND_API_KEY = "an Anthropic API key"
FOUND_KEY_HEADER = "an API key header"
FOUND_BEARER = "a bearer token"
FOUND_API_ID = "a request, message or tool-use id"
FOUND_IP = "an IP address the seed doesn't hold"
FOUND_EMAIL = "an email address the seed doesn't hold"
FOUND_PHONE = "a phone number the seed doesn't hold"
FOUND_UUID = "a UUID the seed doesn't hold"
FOUND_FILE_ID = "an Airwallex file id"
FOUND_LOCAL_PATH = "a local path"
FOUND_SECRET = "a secret this run learned"
FOUND_SECRET_ENCODED = "a secret this run learned, in base64"
FOUND_OPEN_FILE_ID = "an Airwallex file id whose account id isn't hidden"
FOUND_STORE_URL = "an account id in a store URL"
FOUND_UNREDACTED = "a field a recording keeps only redacted"
MAX_PLACES = 5  # places named for one kind in one file; the rest are counted
KEPT_REDACTED = SECRET_HEADERS | SECRET_KEYS | {"ip_address"}  # in an exchange: <redacted>, or empty, and nothing else
EMPTY = (REDACTED, None, "")


def reading_problems(text: str, allowed: Allowed, *, exact: bool = False) -> list[str]:
    """What in a reading's text, or in a whole readings file, isn't the seed's own made-up data: one line per place and
    kind, such as "reading 5e8143e1..., rows[3].note: an email address the seed doesn't hold." Every string in the JSON
    is checked as it reads once decoded, so an escaped line break can't hide what follows it. [] when nothing is found.

    Never allowed: an Anthropic key, an x-api-key header, a bearer token, a request, message or tool-use id, an
    Airwallex file id or a local path. An IP passes when the seed holds it or it is in a documentation range or
    127.0.0.1; an email when the seed holds it or its domain is reserved for examples; a phone number when the seed
    holds its digits or it is a 555-01xx number; a UUID when the seed holds it, so an account id never passes.
    exact: an IP passes only when the seed holds it or it is 127.0.0.1, as scrubbing with the seed's allow-list leaves it
    and as check_files() checks a readings file. The story run checks a reading so before it keeps or reuses one."""
    found: dict[str, None] = {}
    for prefix, place, name, value, _ in _documents(text):
        for kind in _kinds(value, name, allowed, exchange=False, exact=exact, secrets=frozenset()):
            found[f"{_place(prefix, place)}: {kind}."] = None
    return list(found)


def check_files(paths: Iterable[Path], allowed: Allowed, secrets: Iterable[str]) -> list[str]:
    """Every check on each file, before a folder is copied into the repo: [] when all pass, else lines naming places and
    counts, never values. On every string: reading_problems()'s, with an IP passing only when the seed holds it or it
    is 127.0.0.1, as scrubbing with the seed's allow-list leaves it; no learned secret, as itself or in base64; and the
    account-id check. Inside an exchange (an object with a request and a response) the run's request ids are UUIDs and
    its file ids carry the stand-in, so there a UUID passes outside a store URL, and a file id passes when its account id
    is the stand-in. An exchange keeps the fields the scrubber redacts (a cf-ray, a cookie, a token, the paying browser's
    device data) and any ip_address only as <redacted> or empty, as the spike's own tests check them by name: a value
    that slipped past the scrub there may hold nothing else this check could find. Last, scrubbing the file's text must
    leave it as it is."""
    scrubber = Scrubber(secrets, allowed.addresses)
    lines: list[str] = []
    for path in paths:
        lines += _check_file(Path(path), allowed, scrubber)
    return lines


def _check_file(path: Path, allowed: Allowed, scrubber: Scrubber) -> list[str]:
    label = f"{path.parent.name}/{path.name}" if path.parent.name else path.name
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError:
        return [f"{label}: it can't be read."]
    except UnicodeDecodeError:
        return [f"{label}: it isn't UTF-8 text."]
    places: dict[str, dict[str, None]] = {}
    secrets = scrubber.secrets
    for prefix, document in _parsed(text, by_line=path.suffix.lower() == ".jsonl"):
        for place, name, value, exchange in _walked(document):
            for kind in _kinds(value, name, allowed, exchange=exchange, exact=True, secrets=secrets):
                places.setdefault(kind, {})[_place(prefix, place)] = None
        if _is_exchange(document):
            for place in _unredacted(document):
                places.setdefault(FOUND_UNREDACTED, {})[_place(prefix, place)] = None
    lines = []
    for kind, where in places.items():
        named = list(where)
        lines += [f"{label}, {place}: {kind}." for place in named[:MAX_PLACES]]
        if len(named) > MAX_PLACES:
            lines.append(f"{label}: {kind} in {len(named) - MAX_PLACES} more places.")
    if not places and scrubber.scrub(text) != text:  # something outside every string, such as a number
        lines.append(f"{label}: scrubbing changes its text.")
    return lines


def _kinds(text: str, name: str | None, allowed: Allowed, *, exchange: bool, exact: bool,
           secrets: frozenset[str]) -> list[str]:
    """What one string holds that it shouldn't. exact: an IP passes only as the scrubber's allow-list lets it."""
    kinds = []
    if ANTHROPIC_KEY.search(text):
        kinds.append(FOUND_API_KEY)
    if not exchange and KEY_HEADER.search(text):  # an exchange names the header it redacted
        kinds.append(FOUND_KEY_HEADER)
    if BEARER.search(text):
        kinds.append(FOUND_BEARER)
    if API_ID.search(text):
        kinds.append(FOUND_API_ID)
    if any(not _address_passes(found, allowed, exact) for found in ADDRESS.findall(text)):
        kinds.append(FOUND_IP)
    if any(not _email_passes(found, allowed) for found in EMAIL.findall(text)):
        kinds.append(FOUND_EMAIL)
    if any(not _phone_passes(_digits(found), allowed) for found in PHONE.findall(text)):
        kinds.append(FOUND_PHONE)
    uuids = {found.lower() for found in UUID.findall(text)}
    if not exchange and uuids - allowed.uuids:
        kinds.append(FOUND_UUID)
    if exchange and (name == "store_url" or "github.com/" in text) and uuids - {ACCOUNT_ID_STAND_IN}:
        kinds.append(FOUND_STORE_URL)
    for token in base64_tokens(text):
        decoded = decode_base64(token)
        if decoded is None:
            continue
        if SEPARATOR in decoded and not exchange:
            kinds.append(FOUND_FILE_ID)
        elif SEPARATOR in decoded and not decoded.split(SEPARATOR)[0].endswith(ACCOUNT_ID_STAND_IN):
            kinds.append(FOUND_OPEN_FILE_ID)
        if any(secret in decoded for secret in secrets):
            kinds.append(FOUND_SECRET_ENCODED)
    if LOCAL_PATH.search(text):
        kinds.append(FOUND_LOCAL_PATH)
    if any(secret in text for secret in secrets):
        kinds.append(FOUND_SECRET)
    return list(dict.fromkeys(kinds))


def _address_passes(found: str, allowed: Allowed, exact: bool) -> bool:
    if found in allowed.addresses:
        return True
    if exact:
        return False
    try:
        address = ipaddress.ip_address(found)
    except ValueError:  # it only looks like an address, so no range holds it
        return False
    return address == ipaddress.ip_address(LOCAL) or any(
        address.version == network.version and address in network for network in DOCUMENTATION)


def _email_passes(found: str, allowed: Allowed) -> bool:
    address = found.lower()
    domain = address.rsplit("@", 1)[1]
    return address in allowed.emails or domain in RESERVED_DOMAINS or domain.endswith(RESERVED_ENDINGS)


def _phone_passes(digits: str, allowed: Allowed) -> bool:
    return digits[-10:] in allowed.phones or FICTIONAL_PHONE.fullmatch(digits) is not None


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


# ----------------------------------------------------------------------------- walking a text
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _documents(text: str, *, by_line: bool = False) -> Iterator[tuple[str, str, str | None, str, bool]]:
    """(prefix, place, name, string, in an exchange) for every string of a text: one JSON document, else line by line
    (always, for an exchanges.jsonl), each line read as JSON when it is, else as itself."""
    for prefix, document in _parsed(text, by_line=by_line):
        for place, name, value, exchange in _walked(document):
            yield prefix, place, name, value, exchange


def _parsed(text: str, *, by_line: bool = False) -> Iterator[tuple[str, Any]]:
    """(prefix, document) for a text: its one JSON document, else each line that isn't blank, parsed when it is JSON
    and else as its own text, with the prefix "line 3"."""
    whole = None if by_line else _json(text)
    if whole is not None:
        yield "", whole
        return
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            document = _json(line)
            yield f"line {number}", line if document is None else document


def _walked(document: Any) -> Iterator[tuple[str, str | None, str, bool]]:
    """(place, name, string, in an exchange) for every string of a parsed document, or for a line that isn't JSON."""
    if isinstance(document, str):
        yield "", None, document, False
        return
    exchange = _is_exchange(document)
    for place, name, value in _strings(document):
        yield place, name, value, exchange


def _unredacted(value: Any, place: str = "") -> Iterator[str]:
    """The place of each field an exchange keeps only redacted whose value is anything but <redacted> or empty, at any
    depth: "response.headers.cf-ray". A field found isn't looked into."""
    if isinstance(value, dict):
        for name, inner in value.items():
            here = _join(place, str(name))
            if str(name).lower() in KEPT_REDACTED and inner not in EMPTY:
                yield here
            else:
                yield from _unredacted(inner, here)
    elif isinstance(value, list):
        for number, inner in enumerate(value):
            yield from _unredacted(inner, f"{place}[{number}]")


def _strings(value: Any, place: str = "", name: str | None = None) -> Iterator[tuple[str, str | None, str]]:
    """Every string in parsed JSON, keys too, with its place: "rows[3].note". An object with a SHA-256 key, a reading,
    is named by the key's first 8 characters, so a line points at the reading without quoting it. A string that holds
    JSON, such as a reading's proposal, is walked again as JSON."""
    if isinstance(value, str):
        yield place, name, value
        inner = _json(value)
        if inner is not None:
            yield from _strings(inner, place, name)
    elif isinstance(value, dict):
        key = value.get("key")
        if isinstance(key, str) and _SHA256.fullmatch(key):
            place = f"reading {key[:8]}..."
        for inner_name, inner in value.items():
            here = _join(place, str(inner_name))
            yield here, None, str(inner_name)
            yield from _strings(inner, here, str(inner_name))
    elif isinstance(value, list):
        for number, inner in enumerate(value):
            yield from _strings(inner, f"{place}[{number}]", name)


def _json(text: str) -> Any:
    """text as a JSON object or list, or None."""
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] not in "{[":
        return None
    try:
        found = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, (dict, list)) else None


def _is_exchange(document: Any) -> bool:
    return isinstance(document, dict) and "request" in document and "response" in document


def _join(place: str, name: str) -> str:
    if not place:
        return name
    return f"{place}, {name}" if place.endswith("...") else f"{place}.{name}"


def _place(prefix: str, place: str) -> str:
    return ", ".join(part for part in (prefix, place) if part) or "the text"


def _texts(value: Any) -> Iterator[str]:
    """Every value of a record, at any depth, as text."""
    if isinstance(value, str):
        yield value
    elif is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            yield from _texts(getattr(value, item.name))
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield from _texts(key)
            yield from _texts(inner)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for inner in value:
            yield from _texts(inner)
    elif isinstance(value, (datetime, date)):
        yield value.isoformat()
    elif isinstance(value, Enum):
        yield from _texts(value.value)
    elif value is not None:
        yield str(value)
