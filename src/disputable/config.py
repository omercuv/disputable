"""Settings from the .env file only, the sandbox guard, the data folder and its guards, and the one spend question.

Keys come from the .env file a command runs with, never from the process environment: a developer's shell may hold an
ANTHROPIC_API_KEY or ANTHROPIC_BASE_URL that isn't the user's to spend. The file is read with interpolation off, so a
${NAME} in it stays as written and never reaches into the shell. Airwallex URLs must be the sandbox's. The database
lives outside any folder a sync client watches (OneDrive can hold a file while it syncs it) and outside the project (it
holds real ids). Before any live Claude read, confirm_spend() asks once, and the only price it quotes is one the eval
measured. No network and no clock here.

The CLI, the story run and the eval use it like this:
    settings = load_settings(need_airwallex=True, data=args.data, allow_synced=args.allow_synced)
    reader = reader.live_reader(settings.anthropic_api_key)    # NoReader when no key is set: print NO_CLAUDE_KEY
    if not confirm_spend(plan, reads=n, cap=args.cap, yes=args.yes): return 1
    a SetupError's message is the whole text: print it, with no traceback
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from dotenv import dotenv_values

from disputable.fees import usd

DEFAULT_BASE_URL = "https://api.sandbox.airwallex.com"
DEFAULT_FILES_URL = "https://files.sandbox.airwallex.com"
SANDBOX_HOSTS = ".sandbox.airwallex.com"
PROJECT = Path(__file__).resolve().parents[2]  # the repo root: this file is <root>/src/disputable/config.py
RESULTS = PROJECT / "docs" / "eval" / "results.json"  # the eval's results: "measured" holds the price of a read
SYNC_FOLDERS = frozenset({"onedrive", "dropbox", "icloud drive", "google drive", "com~apple~clouddocs", "cloudstorage"})
SYNC_STARTS = ("onedrive -", "onedrive-", "dropbox (", "dropbox-", "googledrive-")  # OneDrive - Contoso, Dropbox (Team)
_EFFORT = re.compile(r"[a-z]{1,20}")

NO_AIRWALLEX_KEYS = ("Set AWX_CLIENT_ID and AWX_API_KEY in the .env file in this folder. docs/getting-api-keys.md shows "
                     "how.")
NOT_SANDBOX = ("{name} must be an https://*.sandbox.airwallex.com address. This app only talks to the Airwallex "
               "sandbox.")
NO_CLAUDE_KEY = ("No Claude API key is set, so every case that needs its records read goes to a person, with code's "
                 "draft. Add ANTHROPIC_API_KEY to .env to let Claude read them.")
NOT_UTF8 = "The .env file isn't UTF-8 text. Save it as UTF-8 and try again."
SYNCED = ("The database can't live in {path}. A sync client watches that folder and may lock the file. Choose another "
          "folder with DISPUTABLE_DATA in .env or --data.")
IN_PROJECT = ("The database can't live in {path}, inside the project. Choose another folder with DISPUTABLE_DATA in .env "
              "or --data.")
IN_REPO = ("The database can't live in {path}. {repo} is a git repository, so the database could be committed there. "
           "Choose another folder with DISPUTABLE_DATA in .env or --data.")
READS = "This run makes at most {count}."
READS_CAPPED = "This run makes at most {count}, and stops before it spends more than {cap}."
MEASURED = "The eval measured {price} a read at effort {effort}."
NOT_MEASURED = "The price of a read isn't measured yet."
QUESTION = "Type yes to go on: "
STOPPED = "Stopped. Nothing was read."


class SetupError(Exception):
    """A setup problem the user fixes. The message is the whole text: the CLI prints it, with no traceback."""


@dataclass(frozen=True)
class Settings:
    """What a command runs with. The keys never show in its repr, so a traceback or a log line can't print them."""
    airwallex_client_id: str | None = field(repr=False)  # None: not set, and the command doesn't need Airwallex
    airwallex_api_key: str | None = field(repr=False)
    anthropic_api_key: str | None = field(repr=False)  # None: no key, so every case that needs a read goes to a person
    base_url: str  # the sandbox's API: https://api.sandbox.airwallex.com unless .env names another sandbox host
    files_url: str  # the sandbox's file service
    data_dir: Path  # where the database lives: absolute, never synced, never in the project

    def __post_init__(self) -> None:
        for name, url in (("AWX_BASE_URL", self.base_url), ("AWX_FILES_URL", self.files_url)):
            if not is_sandbox_url(url):
                raise SetupError(NOT_SANDBOX.format(name=name))


def load_settings(env_file: Path = Path(".env"), *, need_airwallex: bool, data: Path | None = None,
                  allow_synced: bool = False) -> Settings:
    """The settings in env_file, and nothing from the process environment. A missing file holds no settings.

    need_airwallex: both Airwallex keys must be set. The data folder is data (the CLI's --data), else DISPUTABLE_DATA in
    the file, else the per-user default, and check_data_dir() must pass it. Raises SetupError with a plain message."""
    values = _read_env(env_file)
    base_url = _sandbox_url(values, "AWX_BASE_URL", DEFAULT_BASE_URL)
    files_url = _sandbox_url(values, "AWX_FILES_URL", DEFAULT_FILES_URL)
    client_id, api_key = values.get("AWX_CLIENT_ID"), values.get("AWX_API_KEY")
    if need_airwallex and not (client_id and api_key):
        raise SetupError(NO_AIRWALLEX_KEYS)
    if data is None:
        data = Path(values["DISPUTABLE_DATA"]) if "DISPUTABLE_DATA" in values else default_data_dir()
    return Settings(client_id, api_key, values.get("ANTHROPIC_API_KEY"), base_url, files_url,
                    check_data_dir(data, allow_synced=allow_synced))


def is_sandbox_url(url: str) -> bool:
    """The spike's rule: https, and a host ending in .sandbox.airwallex.com."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlparse(url)
    except ValueError:  # such as a bracket that never closes
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower().endswith(SANDBOX_HOSTS)


def default_data_dir(*, platform: str = sys.platform, environ: Mapping[str, str] = os.environ,
                     home: Path | None = None) -> Path:
    """The per-user folder no sync client watches: %LOCALAPPDATA%\\Disputable on Windows, ~/Library/Application
    Support/Disputable on macOS, and ${XDG_DATA_HOME:-~/.local/share}/disputable elsewhere. Only these folder names
    come from the environment, never a setting."""
    home = Path.home() if home is None else home
    if platform == "win32":
        local = environ.get("LOCALAPPDATA", "")
        return (Path(local) if local else home / "AppData" / "Local") / "Disputable"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Disputable"
    xdg = environ.get("XDG_DATA_HOME", "")
    return (Path(xdg) if xdg.startswith("/") else home / ".local" / "share") / "disputable"  # XDG: absolute or unused


def check_data_dir(path: Path, *, allow_synced: bool = False) -> Path:
    """The data folder, made absolute. SetupError when it is in the project or any git repo, where a database could be
    committed, or, unless allow_synced, in a folder a sync client watches, which may lock the file mid-write."""
    folder = Path(path).expanduser().resolve()
    repo = _repo_of(folder)
    if repo == PROJECT:
        raise SetupError(IN_PROJECT.format(path=folder))
    if repo is not None:
        raise SetupError(IN_REPO.format(path=folder, repo=repo))
    if not allow_synced and any(_synced(part) for part in folder.parts):
        raise SetupError(SYNCED.format(path=folder))
    return folder


def confirm_spend(what: str, *, reads: int, cap: Decimal | None, yes: bool, ask: Callable[[str], str] = input,
                  say: Callable[[str], object] = print, results: Path = RESULTS) -> bool:
    """Asks once before a run that may read live with the user's Claude key. It says what, the most reads, the cap
    when there is one, and the price of a read only when the eval measured it (results.json), never a guess. Only the
    word yes goes on; yes=True (--yes) skips the question. Anything else says "Stopped. Nothing was read." and is False."""
    if type(reads) is not int or reads < 0:
        raise ValueError("reads is the most live reads the run may make: a whole number, 0 or more.")
    if cap is not None and (not isinstance(cap, Decimal) or not cap.is_finite() or cap <= 0):
        raise ValueError("cap is a dollar amount above 0, as a Decimal, or None.")
    if what:
        say(what)
    count = f"{reads} live Claude read{'' if reads == 1 else 's'}"
    say(READS.format(count=count) if cap is None else READS_CAPPED.format(count=count, cap=usd(cap)))
    prices = measured_prices(results)
    for effort, price in prices:
        say(MEASURED.format(price=usd(price), effort=effort))
    if not prices:
        say(NOT_MEASURED)
    if yes:
        return True
    try:
        answer = ask(QUESTION)
    except (EOFError, KeyboardInterrupt):  # no input to read, or Ctrl+C at the question: both mean no
        answer = ""
    if isinstance(answer, str) and answer.strip().casefold() == "yes":
        return True
    say(STOPPED)
    return False


def measured_prices(results: Path = RESULTS) -> tuple[tuple[str, Decimal], ...]:
    """The price of one read the eval measured, by effort, from the "measured" object of results.json: {"high":
    "0.1234"}, dollars a read as text. () when the file, the object or any value is missing or unreadable: a price is
    quoted only as the eval measured it."""
    try:
        data = json.loads(Path(results).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return ()
    found = data.get("measured") if isinstance(data, dict) else None
    if not isinstance(found, dict) or not found:
        return ()
    prices = []
    for effort, price in found.items():
        if not (isinstance(effort, str) and _EFFORT.fullmatch(effort) and isinstance(price, str)):
            return ()
        try:
            value = Decimal(price)
        except InvalidOperation:
            return ()
        if not value.is_finite() or value <= 0:
            return ()
        prices.append((effort, value))
    return tuple(prices)


# ----------------------------------------------------------------------------- inside
def _read_env(env_file: Path) -> dict[str, str]:
    """The file's settings, each stripped, without the blank ones. utf-8-sig: Notepad may add a byte-order mark."""
    try:
        raw = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    except UnicodeDecodeError:
        raise SetupError(NOT_UTF8) from None
    return {name: value.strip() for name, value in raw.items() if value is not None and value.strip()}


def _sandbox_url(values: Mapping[str, str], name: str, default: str) -> str:
    url = values.get(name, default).strip().rstrip("/").removesuffix("/api/v1")
    if not is_sandbox_url(url):
        raise SetupError(NOT_SANDBOX.format(name=name))
    return url


def _repo_of(folder: Path) -> Path | None:
    """The nearest folder at or above folder that is the project or holds a .git, or None: where a database could be
    committed."""
    for place in (folder, *folder.parents):
        if place == PROJECT:
            return PROJECT
        try:
            if (place / ".git").exists():
                return place
        except OSError:  # a folder this user can't look into holds no repo of theirs
            continue
    return None


def _synced(part: str) -> bool:
    name = part.casefold().strip()
    return name in SYNC_FOLDERS or name.startswith(SYNC_STARTS)
