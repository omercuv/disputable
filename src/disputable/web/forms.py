"""The checks every POST passes before anything else runs, in this order: the form came from this app's own page
(Origin, or Referer when there's no Origin, with the port), it is a small urlencoded form in plain ASCII, it parses
strictly, it carries this process's token, and it has exactly the fields its route takes. No python-multipart: the
standard library parses the body. Another site can post a form to 127.0.0.1, but it can't read a page, so it can't
learn the token; there are no cookies, so nothing rides along. No field ever names who approves.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Iterable
from urllib.parse import parse_qsl

from starlette.requests import Request

from disputable.web import words as w

MAX_BODY = 64 * 1024  # the largest plan in the read cases is about 1,100 characters of JSON
MAX_FIELDS = 8
FORM_TYPE = "application/x-www-form-urlencoded"


class FormRefused(Exception):
    """The form can't be taken. status is the HTTP status, and the message is a fixed sentence from words.py."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def new_token() -> str:
    """A secret for this process's forms. A restart makes a new one, so an old page's form is refused."""
    return secrets.token_urlsafe(32)


def origins(port: int) -> tuple[str, ...]:
    """This app's own origins at the port. A browser leaves http's default port out of an origin, so on port 80 its
    pages send http://127.0.0.1 and http://localhost: those are this app's too then, and only then."""
    named = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    return (*named, "http://127.0.0.1", "http://localhost") if port == 80 else named


def same_origin(origin: list[str], referer: list[str], port: int) -> bool:
    """Whether the form came from this app's own page: its Origin, or with no Origin its Referer, is this app at this
    port. Another local server on another port is refused too. No header, or Origin null, is refused."""
    allowed = origins(port)
    if origin:
        return len(origin) == 1 and origin[0] in allowed
    return len(referer) == 1 and any(referer[0].startswith(f"{base}/") for base in allowed)


def check_type(content_type: str | None) -> None:
    if (content_type or "").split(";")[0].strip().lower() != FORM_TYPE:
        raise FormRefused(415, w.FORM_TYPE)


def parse(body: bytes, fields: Iterable[str], token: str) -> dict[str, str]:
    """The fields of a form body: ASCII, strictly parsed, the token checked, then exactly the route's fields, each once.
    The form's Origin or Referer is already this app's, so one token that isn't this process's is a page this app showed
    before it started again; no token, or two, is no page of this app's."""
    try:
        text = body.decode("ascii")
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_FIELDS,
                          encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise FormRefused(400, w.FORM_BAD) from None
    given = [value for name, value in pairs if name == "csrf"]
    if len(given) != 1:
        raise FormRefused(403, w.FORM_FOREIGN)
    if not hmac.compare_digest(given[0].encode("utf-8"), token.encode("utf-8")):
        raise FormRefused(403, w.FORM_OLD)
    names = [name for name, _ in pairs]
    if sorted(names) != sorted(set(fields)) or len(names) != len(set(names)):
        raise FormRefused(400, w.FORM_BAD)
    return dict(pairs)


async def read(request: Request, fields: Iterable[str], *, port: int, token: str) -> dict[str, str]:
    """The route's fields, or FormRefused. The body is read from the stream, at most MAX_BODY bytes of it."""
    headers = request.headers
    if not same_origin(headers.getlist("origin"), headers.getlist("referer"), port):
        raise FormRefused(403, w.FORM_FOREIGN)
    check_type(headers.get("content-type"))
    length = headers.get("content-length")
    if length is not None and not (length.isascii() and length.isdigit()):
        raise FormRefused(400, w.FORM_BAD)
    if length is not None and int(length) > MAX_BODY:
        raise FormRefused(413, w.FORM_TOO_BIG)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise FormRefused(413, w.FORM_TOO_BIG)
    return parse(bytes(body), fields, token)
