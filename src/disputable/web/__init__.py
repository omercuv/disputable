"""The screens: a local web app over a Desk, on 127.0.0.1 only.

    app = create_app(desk, port=8765)                     # for tests and for serve()
    serve(desk, port=8765, open_browser=True)             # uvicorn until Ctrl+C: 0, or 1 when the port is in use

The web layer is given a Desk and nothing else from the runtime: no key, no client, no database and no clock. Who
approves is board.approver, fixed when the desk was made: no form ever names a person. The FastAPI import waits until
one of these is called, so words.py can be read at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from disputable.desk import Desk


def create_app(desk: Desk, *, port: int) -> FastAPI:
    from disputable.web.app import create_app as build
    return build(desk, port=port)


def serve(desk: Desk, *, port: int = 8765, open_browser: bool = True) -> int:
    from disputable.web.app import serve as run
    return run(desk, port=port, open_browser=open_browser)
