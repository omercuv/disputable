"""The web app over a Desk: the routes, the form pipeline, the error pages, the security headers and the Host allow-list.

Every page is one Board, read once per request, and a pure view of it. Every write is one Desk call: approve(),
drop_moved(), demo_next() or demo_restart(). The desk runs every check and saves; this layer only reads forms, maps the
Result's key to its words and never echoes what was posted. A POST that worked answers 303 with ?done=<key>, and one that
didn't answers the refusal page with the module's own sentence. The app binds 127.0.0.1 only (there is no host option),
refuses any other Host, takes forms only from its own pages with its per-process token, sets no cookie, loads no script
and reads no clock: board.now is the only time it shows. FastAPI's telemetry is off, so nothing leaves the computer.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from disputable.desk import Board, Desk, Result
from disputable.web import forms, views
from disputable.web import words as w

LOG = logging.getLogger("disputable.web")
HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
HOSTS = ["127.0.0.1", "localhost"]
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; "
       "base-uri 'none'")
HEADERS = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
           "Cache-Control": "no-store", "Cross-Origin-Opener-Policy": "same-origin",
           "Cross-Origin-Resource-Policy": "same-origin"}
TELEMETRY_OFF = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}
SHA256 = re.compile(r"[0-9a-f]{64}")
NUMBER = re.compile(r"[0-9]{1,9}")
ENDING = re.compile(r"[0-9A-Za-z_-]{1,16}")
APPROVE_FIELDS, DROP_FIELDS = ("csrf", "plan", "reading"), ("csrf", "version")
NEXT_FIELDS, RESTART_FIELDS = ("csrf", "played"), ("csrf", "ending")


class SecurityHeaders:
    """Sets the security headers on every answer, the Host refusal included. A PDF gets no CSP, which could stop the
    browser's own PDF viewer; it is sent as an attachment."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                secure(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, sending)


def secure(headers: MutableHeaders) -> None:
    for name, value in HEADERS.items():
        headers[name] = value
    if headers.get("content-type", "").startswith("application/pdf"):
        if "content-security-policy" in headers:
            del headers["content-security-policy"]
    else:
        headers["Content-Security-Policy"] = CSP


@functools.cache
def templates() -> Environment:
    """The templates, compiled once: autoescape on, and a missing name is an error, never an empty string."""
    return Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True, undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True, auto_reload=False)


def create_app(desk: Desk, *, port: int) -> FastAPI:
    """The app over this desk. port is the one the browser uses: a form's Origin must name it."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The port is a whole number from 1 to 65535.")
    token = forms.new_token()
    env = templates()
    css = (HERE / "static" / "app.css").read_bytes()
    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None, telemetry=TELEMETRY_OFF)

    def render(template: str, status: int, page: views.Page, view: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(template).render(page=page, view=view), status_code=status)

    def bare(status: int, message: str) -> HTMLResponse:
        """A refusal that never asks the desk anything: the form checks and the error pages."""
        return render("refused.html", status, views.page(None, w.NOT_DONE, csrf=token), views.plain_refusal(message))

    def refusal(board: Board, result: Result, dispute_id: str | None, *, drop: bool = False) -> HTMLResponse:
        status, view = views.refused(board, result, dispute_id, csrf=token, drop=drop)
        return render("refused.html", status, views.page(board, w.NOT_DONE, csrf=token), view)

    def missing(board: Board, message: str) -> HTMLResponse:
        return render("refused.html", 404, views.page(board, w.NOT_DONE, csrf=token), views.plain_refusal(message))

    def done(request: Request) -> str:
        return request.query_params.get("done", "")

    @app.get("/")
    def queue(request: Request) -> Response:
        board = desk.board()
        page = views.page(board, w.QUEUE_TITLE, csrf=token, done=done(request))
        return render("queue.html", 200, page, views.queue(board))

    @app.get("/case/{dispute_id}")
    def case(request: Request, dispute_id: str) -> Response:
        board = desk.board()
        view = views.case(board, dispute_id, csrf=token)
        if view is None:
            return missing(board, w.NO_CASE)
        return render("case.html", 200, views.page(board, view.title, csrf=token, done=done(request)), view)

    @app.get("/case/{dispute_id}/approve")
    def approve_page(dispute_id: str) -> Response:
        board = desk.board()
        view = views.approval(board, dispute_id, csrf=token)
        if view is None:
            return missing(board, w.NO_CASE)
        return render("approve.html", 200, views.page(board, view.title, csrf=token), view)

    @app.get("/case/{dispute_id}/file/{sha256}.pdf")
    def evidence(dispute_id: str, sha256: str) -> Response:
        board = desk.board()
        record = board.records.get(dispute_id)
        if record is None:
            return missing(board, w.NO_CASE)
        found = [document for document in views.case_files(board, record).documents
                 if SHA256.fullmatch(sha256) and document.sha256 == sha256]
        if not found:
            return missing(board, w.NO_FILE)
        return Response(found[0].pdf(), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{found[0].filename}"'})

    @app.get("/money")
    def money(request: Request) -> Response:
        board = desk.board()
        page = views.page(board, w.MONEY_TITLE, csrf=token, done=done(request))
        return render("money.html", 200, page, views.money_view(board))

    @app.get("/app.css")
    def stylesheet() -> Response:
        return Response(css, media_type="text/css; charset=utf-8")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    async def posted(request: Request, fields: tuple[str, ...], call: Callable[[dict[str, str]], Result | None],
                     dispute_id: str | None, back: Callable[[Result], str], *, drop: bool = False) -> Response:
        """One write: the form checks first, then the desk, then a redirect or the refusal page. drop: the drop form's,
        whose refusal page has the drop's own words."""
        try:
            given = await forms.read(request, fields, port=port, token=token)
        except forms.FormRefused as refused:
            return bare(refused.status, refused.message)
        result = await run_in_threadpool(call, given)
        if result is None:
            return bare(400, w.FORM_BAD)
        if views.succeeded(result):
            return RedirectResponse(back(result), status_code=303)
        board = await run_in_threadpool(desk.board)
        return refusal(board, result, dispute_id, drop=drop)

    def to_case(dispute_id: str) -> Callable[[Result], str]:
        return lambda result: views.case_href(dispute_id) + (f"?done={result.key}" if result.key in w.DONE else "")

    def to_queue(result: Result) -> str:
        return "/" + (f"?done={result.key}" if result.key in w.DONE else "")

    @app.post("/case/{dispute_id}/approve")
    async def approve(request: Request, dispute_id: str) -> Response:
        def call(given: dict[str, str]) -> Result:
            return desk.approve(dispute_id, given["plan"], given["reading"])  # the desk runs every check, then saves
        return await posted(request, APPROVE_FIELDS, call, dispute_id, to_case(dispute_id))

    @app.post("/case/{dispute_id}/drop")
    async def drop(request: Request, dispute_id: str) -> Response:
        def call(given: dict[str, str]) -> Result | None:
            version = given["version"]
            return desk.drop_moved(dispute_id, int(version)) if NUMBER.fullmatch(version) else None
        return await posted(request, DROP_FIELDS, call, dispute_id, to_case(dispute_id), drop=True)

    @app.post("/demo/next")
    async def demo_next(request: Request) -> Response:
        def call(given: dict[str, str]) -> Result | None:
            played = given["played"]
            return desk.demo_next(int(played)) if NUMBER.fullmatch(played) else None
        return await posted(request, NEXT_FIELDS, call, None, to_queue)

    @app.post("/demo/restart")
    async def demo_restart(request: Request) -> Response:
        def call(given: dict[str, str]) -> Result | None:
            ending = given["ending"]
            return desk.demo_restart(ending) if ENDING.fullmatch(ending) else None
        return await posted(request, RESTART_FIELDS, call, None, to_queue)

    async def http_error(request: Request, error: Exception) -> Response:
        status = getattr(error, "status_code", 500)
        message = {404: w.NO_PAGE, 405: w.NO_METHOD}.get(status, w.ERROR)
        response = bare(status, message)
        for name, value in (getattr(error, "headers", None) or {}).items():
            response.headers[name] = value
        return response

    async def unreadable(request: Request, error: Exception) -> Response:
        return bare(400, w.FORM_BAD)

    def failed(request: Request, error: Exception) -> Response:
        LOG.error("This app hit an error while it answered a request.", exc_info=error)
        response = bare(500, w.ERROR)
        secure(response.headers)  # the server's error handler sends this past the middleware
        return response

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, unreadable)
    app.add_exception_handler(Exception, failed)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=HOSTS)  # a rebound name, say evil.example, gets 400
    app.add_middleware(SecurityHeaders)  # added last, so it wraps the Host check too
    return app


def port_free(port: int) -> bool:
    """Whether this app can listen on 127.0.0.1 at the port, tried the way the server binds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":  # as the server binds there; on Windows the option would let two servers share the port
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def serve(desk: Desk, *, port: int = 8765, open_browser: bool = True) -> int:
    """Run the app on 127.0.0.1 only, until Ctrl+C: 0. A port in use prints PORT_BUSY: 1. There is no host option."""
    import uvicorn  # only serving needs the server

    app = create_app(desk, port=port)
    if not port_free(port):
        print(w.PORT_BUSY.format(port=port, other=port + 1 if port < 65535 else port - 1), flush=True)
        return 1
    url = f"http://{HOST}:{port}"
    print(w.RUNNING.format(url=url), flush=True)
    if open_browser:
        opener = threading.Timer(1.0, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0
