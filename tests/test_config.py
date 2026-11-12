"""config.py, and the rest of the foundation: settings from the .env file only, the sandbox guard, the data folder, the
spend question; the Desk's types (desk.py); and the foundation's imports, which never reach the seed.

Every .env here is a temp file with made-up values: the project's own .env is never read.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from disputable import config, desk
from disputable.config import (IN_PROJECT, NO_AIRWALLEX_KEYS, NOT_MEASURED, NOT_SANDBOX, QUESTION, STOPPED, SYNCED,
                               SetupError, Settings, check_data_dir, confirm_spend, default_data_dir, is_sandbox_url,
                               load_settings, measured_prices)

ROOT = Path(__file__).resolve().parent.parent
SHELL_KEY = "sk-" + "ant-from-the-shell-not-the-users"  # made up, and built so no scanner mistakes it for a key
FILE_KEYS = {"AWX_CLIENT_ID": "cid_made_up_4242", "AWX_API_KEY": "key_made_up_4242",
             "ANTHROPIC_API_KEY": "sk-" + "ant-made-up-file-key"}


def env(tmp_path, *lines, name=".env", encoding="utf-8"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding=encoding)
    return path


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def shell(monkeypatch, tmp_path):
    """A developer's shell that holds keys and settings of its own: none of them may reach the settings."""
    for name, value in {"ANTHROPIC_API_KEY": SHELL_KEY, "ANTHROPIC_BASE_URL": "https://claude.example.com",
                        "AWX_CLIENT_ID": "cid_from_the_shell", "AWX_API_KEY": "key_from_the_shell",
                        "AWX_BASE_URL": "https://api.airwallex.com", "AWX_FILES_URL": "https://files.airwallex.com",
                        "DISPUTABLE_DATA": str(tmp_path / "from-the-shell")}.items():
        monkeypatch.setenv(name, value)


# ----------------------------------------------------------------------------- keys from the .env file only
def test_keys_come_from_the_env_file_only(tmp_path, shell, data):
    path = env(tmp_path, *(f"{name}={value}" for name, value in FILE_KEYS.items()))
    settings = load_settings(path, need_airwallex=True, data=data)
    assert (settings.airwallex_client_id, settings.airwallex_api_key, settings.anthropic_api_key) == tuple(
        FILE_KEYS.values())
    assert (settings.base_url, settings.files_url) == (config.DEFAULT_BASE_URL, config.DEFAULT_FILES_URL)
    assert settings.data_dir == data.resolve()


def test_a_key_the_shell_holds_is_never_used(tmp_path, shell, data):
    only_airwallex = env(tmp_path, "AWX_CLIENT_ID=cid_made_up_4242", "AWX_API_KEY=key_made_up_4242")
    assert load_settings(only_airwallex, need_airwallex=True, data=data).anthropic_api_key is None
    blank = env(tmp_path, "ANTHROPIC_API_KEY=", "AWX_CLIENT_ID=  ", name="blank.env")
    settings = load_settings(blank, need_airwallex=False, data=data)
    assert (settings.anthropic_api_key, settings.airwallex_client_id, settings.airwallex_api_key) == (None, None, None)
    # interpolation is off: a ${NAME} stays as written and never reaches into the shell
    pointing = env(tmp_path, "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}", "AWX_BASE_URL=${AWX_BASE_URL}",
                   name="pointing.env")
    with pytest.raises(SetupError) as refused:
        load_settings(pointing, need_airwallex=False, data=data)
    assert str(refused.value) == NOT_SANDBOX.format(name="AWX_BASE_URL")
    settings = load_settings(env(tmp_path, "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}", name="key.env"),
                             need_airwallex=False, data=data)
    assert settings.anthropic_api_key == "${ANTHROPIC_API_KEY}" and SHELL_KEY not in repr(vars(settings))


def test_a_missing_env_file_holds_no_settings(tmp_path, shell, data):
    settings = load_settings(tmp_path / "missing.env", need_airwallex=False, data=data)
    assert (settings.airwallex_client_id, settings.anthropic_api_key) == (None, None)
    with pytest.raises(SetupError) as refused:
        load_settings(tmp_path / "missing.env", need_airwallex=True, data=data)
    assert str(refused.value) == NO_AIRWALLEX_KEYS
    with pytest.raises(SetupError):
        load_settings(env(tmp_path, "AWX_CLIENT_ID=cid_made_up_4242"), need_airwallex=True, data=data)


def test_the_env_file_may_start_with_a_byte_order_mark_and_quote_its_values(tmp_path, data):
    path = env(tmp_path, '\ufeffAWX_CLIENT_ID="cid made up"', "AWX_API_KEY='key_made_up_4242'  ", "# a comment")
    settings = load_settings(path, need_airwallex=True, data=data)
    assert (settings.airwallex_client_id, settings.airwallex_api_key) == ("cid made up", "key_made_up_4242")


def test_an_env_file_that_isnt_utf8_is_refused_plainly(tmp_path, data):
    path = tmp_path / ".env"
    path.write_bytes(b"AWX_CLIENT_ID=caf\xe9\n")
    with pytest.raises(SetupError) as refused:
        load_settings(path, need_airwallex=False, data=data)
    assert str(refused.value) == config.NOT_UTF8


def test_the_settings_repr_holds_no_key(tmp_path, data):
    settings = load_settings(env(tmp_path, *(f"{name}={value}" for name, value in FILE_KEYS.items())),
                             need_airwallex=True, data=data)
    for shown in (repr(settings), str(settings), f"{settings}"):
        for value in FILE_KEYS.values():
            assert value not in shown
    assert "base_url='https://api.sandbox.airwallex.com'" in repr(settings)
    assert dataclasses.is_dataclass(settings) and Settings.__dataclass_params__.frozen


def test_a_setup_error_is_its_message():
    error = SetupError(NO_AIRWALLEX_KEYS)
    assert str(error) == NO_AIRWALLEX_KEYS and isinstance(error, Exception)


# ----------------------------------------------------------------------------- the sandbox only
@pytest.mark.parametrize("url", ["https://api.sandbox.airwallex.com", "https://files.sandbox.airwallex.com",
                                 "https://API.Sandbox.Airwallex.com/api/v1"])
def test_sandbox_urls_pass(url):
    assert is_sandbox_url(url)


@pytest.mark.parametrize("url", ["https://api.airwallex.com", "http://api.sandbox.airwallex.com",
                                 "https://api.sandbox.airwallex.com.evil.example", "https://sandbox.airwallex.com",
                                 "https://evil.example/.sandbox.airwallex.com",
                                 "https://evil.example#.sandbox.airwallex.com", "ftp://api.sandbox.airwallex.com",
                                 "api.sandbox.airwallex.com", "", "https://[::1", None])
def test_other_urls_are_refused(url):
    assert not is_sandbox_url(url)


def test_the_sandbox_rule_is_the_spikes():
    spec = importlib.util.spec_from_file_location("spike_for_config", ROOT / "scripts" / "spike.py")
    spike = importlib.util.module_from_spec(spec)
    sys.modules["spike_for_config"] = spike  # dataclasses look the module up while it loads
    spec.loader.exec_module(spike)
    for url in ("https://api.sandbox.airwallex.com", "https://files.sandbox.airwallex.com", "https://api.airwallex.com",
                "http://api.sandbox.airwallex.com", "https://api.sandbox.airwallex.com.evil.io",
                "https://sandbox.airwallex.com"):
        assert is_sandbox_url(url) == spike.is_sandbox_url(url), url


@pytest.mark.parametrize("name", ["AWX_BASE_URL", "AWX_FILES_URL"])
def test_an_env_file_that_names_another_host_is_refused(tmp_path, data, name):
    with pytest.raises(SetupError) as refused:
        load_settings(env(tmp_path, f"{name}=https://api.airwallex.com"), need_airwallex=False, data=data)
    assert str(refused.value) == NOT_SANDBOX.format(name=name)


def test_a_sandbox_url_is_trimmed_as_the_spike_trims_it(tmp_path, data):
    path = env(tmp_path, "AWX_BASE_URL= https://api.sandbox.airwallex.com/api/v1/ ",
               "AWX_FILES_URL=https://files.sandbox.airwallex.com/")
    settings = load_settings(path, need_airwallex=False, data=data)
    assert (settings.base_url, settings.files_url) == ("https://api.sandbox.airwallex.com",
                                                       "https://files.sandbox.airwallex.com")
    with pytest.raises(SetupError):
        Settings(None, None, None, "https://api.airwallex.com", config.DEFAULT_FILES_URL, data)


# ----------------------------------------------------------------------------- the data folder
@pytest.mark.parametrize("synced", ["OneDrive", "onedrive", "OneDrive - Contoso", "OneDrive-Personal", "Dropbox",
                                    "Dropbox (Team)", "iCloud Drive", "Google Drive", "CloudStorage"])
def test_the_database_never_lives_in_a_synced_folder(tmp_path, synced):
    folder = tmp_path / synced / "Disputable"
    with pytest.raises(SetupError) as refused:
        check_data_dir(folder)
    assert str(refused.value) == SYNCED.format(path=folder.resolve())
    assert check_data_dir(folder, allow_synced=True) == folder.resolve()


def test_the_database_never_lives_in_the_project_or_a_repo(tmp_path):
    for folder in (ROOT, ROOT / "data", ROOT / "fixtures" / "story" / "db"):
        with pytest.raises(SetupError) as refused:
            check_data_dir(folder, allow_synced=True)  # the flag lets a synced folder pass, never the project
        assert str(refused.value) == IN_PROJECT.format(path=folder.resolve())
    (tmp_path / "other-repo" / ".git").mkdir(parents=True)
    with pytest.raises(SetupError) as other:
        check_data_dir(tmp_path / "other-repo" / "data")
    assert str(other.value) == (f"The database can't live in {(tmp_path / 'other-repo' / 'data').resolve()}. "
                                f"{(tmp_path / 'other-repo').resolve()} is a git repository, so the database could be "
                                "committed there. Choose another folder with DISPUTABLE_DATA in .env or --data.")
    plain = tmp_path / "plain" / "Disputable"
    assert check_data_dir(plain) == plain.resolve() and not plain.exists()  # checked, not made


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_a_git_repository_above_the_default_folder_is_named(tmp_path, platform):
    """Someone once ran git init in their home folder: the line names that repository, not the project."""
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    folder = default_data_dir(platform=platform, environ={}, home=home)
    with pytest.raises(SetupError) as refused:
        check_data_dir(folder)
    assert str(refused.value) == config.IN_REPO.format(path=folder.resolve(), repo=home.resolve())
    assert "inside the project" not in str(refused.value)


def test_the_data_folder_is_the_flag_then_the_env_file_then_the_default(tmp_path, shell, monkeypatch):
    chosen, in_file = tmp_path / "chosen", tmp_path / "in-file"
    path = env(tmp_path, f"DISPUTABLE_DATA={in_file}")
    assert load_settings(path, need_airwallex=False, data=chosen).data_dir == chosen.resolve()
    assert load_settings(path, need_airwallex=False).data_dir == in_file.resolve()
    monkeypatch.setattr(config, "default_data_dir", lambda: tmp_path / "default")
    settings = load_settings(env(tmp_path, "AWX_CLIENT_ID=x", name="no-data.env"), need_airwallex=False)
    assert settings.data_dir == (tmp_path / "default").resolve()  # the shell's DISPUTABLE_DATA is never used
    with pytest.raises(SetupError, match="A sync client watches that folder"):
        load_settings(env(tmp_path, f"DISPUTABLE_DATA={tmp_path / 'Dropbox'}", name="synced.env"), need_airwallex=False)
    assert load_settings(env(tmp_path, f"DISPUTABLE_DATA={tmp_path / 'Dropbox'}", name="synced.env"),
                         need_airwallex=False, allow_synced=True).data_dir == (tmp_path / "Dropbox").resolve()


def test_the_default_folder_is_the_platforms_own():
    home = Path("/home/someone")
    assert default_data_dir(platform="win32", environ={"LOCALAPPDATA": r"C:\Users\someone\AppData\Local"},
                            home=home) == Path(r"C:\Users\someone\AppData\Local") / "Disputable"
    assert default_data_dir(platform="win32", environ={}, home=home) == home / "AppData" / "Local" / "Disputable"
    assert default_data_dir(platform="darwin", environ={}, home=home) == home / "Library" / "Application Support" / \
        "Disputable"
    assert default_data_dir(platform="linux", environ={}, home=home) == home / ".local" / "share" / "disputable"
    assert default_data_dir(platform="linux", environ={"XDG_DATA_HOME": "/srv/data"}, home=home) == Path(
        "/srv/data") / "disputable"
    assert default_data_dir(platform="linux", environ={"XDG_DATA_HOME": "relative"}, home=home) == home / ".local" / \
        "share" / "disputable"


# ----------------------------------------------------------------------------- the spend question
class Console:
    """What confirm_spend() says, and the answers it gets."""

    def __init__(self, *answers):
        self.answers, self.said, self.asked = list(answers), [], []

    def ask(self, question):
        self.asked.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def say(self, line):
        self.said.append(line)


def results(tmp_path, data):
    path = tmp_path / "results.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def spend(console, path, *, reads=4, cap=Decimal("3"), yes=False, what="Live run: the story's 4 reads at high."):
    return confirm_spend(what, reads=reads, cap=cap, yes=yes, ask=console.ask, say=console.say, results=path)


@pytest.mark.parametrize("answer", ["yes", "Yes", " YES \n"])
def test_spend_goes_on_only_with_the_word_yes(tmp_path, answer):
    console = Console(answer)
    assert spend(console, tmp_path / "missing.json") is True
    assert console.asked == [QUESTION] and STOPPED not in console.said


@pytest.mark.parametrize("answer", ["", "y", "no", "yes please", "ok", EOFError(), KeyboardInterrupt()])
def test_anything_but_yes_stops_and_nothing_is_read(tmp_path, answer):
    console = Console(answer)
    assert spend(console, tmp_path / "missing.json") is False
    assert console.said[-1] == STOPPED == "Stopped. Nothing was read."


def test_yes_given_up_front_skips_the_question(tmp_path):
    console = Console()
    assert spend(console, tmp_path / "missing.json", yes=True) is True
    assert console.asked == []


def test_spend_says_what_the_reads_and_the_cap_and_that_no_price_is_measured(tmp_path):
    console = Console("yes")
    spend(console, tmp_path / "missing.json")
    assert console.said == ["Live run: the story's 4 reads at high.",
                            "This run makes at most 4 live Claude reads, and stops before it spends more than $3.00.",
                            NOT_MEASURED]
    console = Console("yes")
    spend(console, tmp_path / "missing.json", reads=1, cap=None, what="")
    assert console.said == ["This run makes at most 1 live Claude read.", "The price of a read isn't measured yet."]


def test_spend_quotes_the_price_the_eval_measured(tmp_path):
    console = Console("yes")
    spend(console, results(tmp_path, {"format": 1, "measured": {"high": "0.1234"}}))
    assert console.said[2:] == ["The eval measured $0.12 a read at effort high."]
    console = Console("yes")
    spend(console, results(tmp_path, {"measured": {"medium": "0.081", "high": "0.125"}}))
    assert console.said[2:] == ["The eval measured $0.08 a read at effort medium.",
                                "The eval measured $0.13 a read at effort high."]


@pytest.mark.parametrize("data", [
    {"format": 1, "prices": {"claude-opus-5-5": {"input": "4.00", "output": "20.00"}}, "estimate": {"central": "0.12"}},
    {"measured": {}}, {"measured": {"high": 0.12}}, {"measured": {"high": "about a dollar"}}, {"measured": {"high": "0"}},
    {"measured": {"high": "-0.10"}}, {"measured": {"high": "NaN"}}, {"measured": {"high": "Infinity"}},
    {"measured": {"high": "0.12", "Effort 2!": "0.10"}}, {"measured": ["high", "0.12"]}, ["measured"],
    "not json {", "",
])
def test_spend_never_quotes_a_guessed_price(tmp_path, data):
    console = Console("yes")
    spend(console, results(tmp_path, data))
    assert console.said[2:] == [NOT_MEASURED]
    assert not [line for line in console.said[2:] if "$" in line]


def test_spend_refuses_a_plan_it_cant_state(tmp_path):
    for reads, cap in ((-1, None), (2.5, None), (True, None), (3, Decimal("0")), (3, Decimal("NaN")), (3, 3)):
        with pytest.raises(ValueError):
            spend(Console("yes"), tmp_path / "missing.json", reads=reads, cap=cap)


def test_the_committed_results_quote_a_price_only_once_the_eval_measured_one():
    prices = measured_prices()
    assert all(price > 0 for _, price in prices)
    if not config.RESULTS.exists():
        assert prices == ()


# ----------------------------------------------------------------------------- the Desk's types (desk.py)
DESK_TYPES = (desk.ReaderInfo, desk.SendLine, desk.Beat, desk.DemoBar, desk.Board, desk.Result)
SPEC_KEYS = {"approved", "already", "approvals-off", "no-case", "broken", "plan-unreadable", "refused", "reading-changed",
             "file-moved", "not-recorded", "stale", "not-in-recording", "dropped", "drop-not-needed", "demo-only",
             "live-only", "demo-moved", "demo-mismatch", "demo-finished", "no-ending", "next-played", "restarted"}


def test_the_desk_types_are_frozen():
    for kind in DESK_TYPES:
        assert dataclasses.is_dataclass(kind) and kind.__dataclass_params__.frozen, kind.__name__
    line = desk.SendLine(datetime(2026, 11, 10, 9, 14, tzinfo=timezone.utc), "taken", 201, "Airwallex took it.")
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.detail = "changed"
    info = desk.ReaderInfo(config_profile(), "high", "claude", "claude-opus-5-5", date(2026, 11, 8), date(2026, 11, 8))
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.source = "scripted"


def config_profile():
    from disputable.prompts import FERNHILL
    return FERNHILL


def test_the_desks_fields_are_the_specs():
    names = {kind.__name__: [item.name for item in dataclasses.fields(kind)] for kind in DESK_TYPES}
    assert names == {
        "ReaderInfo": ["profile", "effort", "source", "model", "first_read", "last_read"],
        "SendLine": ["at", "outcome", "status", "detail", "round_number"],  # round_number: the round whose plan it sent
        "Beat": ["story", "text", "kind"],
        "DemoBar": ["ending", "endings", "recorded_on", "played", "coming", "pending", "approver", "mismatch",
                    "finished", "count", "airwallex", "total", "left_unused"],
        "Board": ["mode", "now", "store", "records", "versions", "broken", "approvals", "saved", "sends", "problems",
                  "last_pass", "reader", "approver", "demo"],
        "Result": ["ok", "key", "message"],
    }
    assert [mode.value for mode in desk.Mode] == ["LIVE", "DEMO"]


def test_result_keys_hold_every_key_the_spec_names():
    assert desk.RESULT_KEYS == SPEC_KEYS
    assert desk.Result(True, "approved") == desk.Result(True, "approved", "")
    for key in SPEC_KEYS:
        desk.Result(False, key, "a sentence as written")
    with pytest.raises(ValueError):
        desk.Result(False, "aproved")


def test_the_desk_protocol_is_the_specs():
    calls = {name: list(inspect.signature(getattr(desk.Desk, name)).parameters)
             for name in ("board", "approve", "drop_moved", "demo_next", "demo_restart")}
    assert calls == {"board": ["self"], "approve": ["self", "dispute_id", "plan_text", "reading_key"],
                     "drop_moved": ["self", "dispute_id", "version"], "demo_next": ["self", "played"],
                     "demo_restart": ["self", "ending"]}

    class OnlyBoards:
        def board(self):
            return None

    class Everything(OnlyBoards):
        def approve(self, dispute_id, plan_text, reading_key):
            return desk.Result(False, "approvals-off")

        def drop_moved(self, dispute_id, version):
            return desk.Result(False, "live-only")

        def demo_next(self, played):
            return desk.Result(False, "demo-only")

        def demo_restart(self, ending):
            return desk.Result(False, "demo-only")

    assert isinstance(Everything(), desk.Desk) and not isinstance(OnlyBoards(), desk.Desk)


# ----------------------------------------------------------------------------- the foundation's imports
FOUNDATION = ("config", "privacy", "readings_file", "desk")
NEVER = ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web")
FRESH = """
import importlib, importlib.abc, json, sys, types

class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == never or name.startswith(never + ".") for never in %r):
            raise ImportError(name + " is blocked: the foundation must not need it")
        return None

sys.meta_path.insert(0, Blocked())
# A stand-in for the SDK, which takes seconds to import here and can't reach the seed: any name it's asked for is a class.
sdk = types.ModuleType("anthropic")
sdk.__getattr__ = lambda name: type(name, (Exception,), {})
sys.modules["anthropic"] = sdk
for name in %r:
    importlib.import_module("disputable." + name)
print(json.dumps(sorted(name for name in sys.modules if name.startswith("disputable"))))
""" % (NEVER, FOUNDATION)


def imported(module):
    """Every module an import statement in the module's source names, at any depth."""
    tree = ast.parse(Path(importlib.util.find_spec(f"disputable.{module}").origin).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(part for part in ("disputable" if node.level else "", node.module or "") if part)
            names.update({base, *(f"{base}.{alias.name}" for alias in node.names)})
    return names


def test_the_foundation_never_imports_the_seed_or_the_apps_outer_parts():
    for module in FOUNDATION:
        names = imported(module)
        assert names, module
        assert not [name for name in names if any(name == never or name.startswith(never + ".") for never in NEVER)], \
            module
    fresh = subprocess.run([sys.executable, "-B", "-c", FRESH], capture_output=True, text=True, timeout=120)
    assert fresh.returncode == 0, fresh.stderr
    loaded = set(json.loads(fresh.stdout))
    assert {f"disputable.{module}" for module in FOUNDATION} <= loaded
    assert not [name for name in loaded if any(name.startswith(never) for never in NEVER)]


TESTS_CODE = frozenset({path.stem for path in (ROOT / "tests").glob("*.py")} | {"tests", "conftest"})
BY_NAME = frozenset({"import_module", "__import__"})  # importlib's and the builtin's: a module named at run time
BY_PATH = frozenset({"spec_from_file_location", "run_path", "run_module"})  # a file or module loaded where it lies


def imports_of_the_tests_code(source: str) -> list[tuple[int, str]]:
    """(line, name) of each import in source that may reach the tests' code: an import or a from-import of a tests
    module at any depth, so a lazy one inside a function too; import_module() or __import__() with a literal that names
    one, or with a name made at run time; and every call of spec_from_file_location, run_path or run_module, whatever
    file or module it names. Other loads by path, such as a SourceFileLoader or exec() of a file's text, aren't flagged.
    In line order."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else []  # a relative import stays in the package
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in BY_NAME | BY_PATH:
            called = getattr(node.func, "attr", getattr(node.func, "id", ""))
            first = node.args[0] if node.args else None
            literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            names = [f"{called}()"] if called in BY_PATH or literal is None else [literal]
        else:
            continue
        found += [(node.lineno, name) for name in names if name.endswith("()") or name.split(".")[0] in TESTS_CODE]
    return sorted(found)


def test_no_module_of_the_package_imports_the_tests_code():
    """pytest puts tests/ on sys.path, so a package module that imported the tests' fake would pass every test and fail
    only for a user. scripts/record_standin.py may import it; no module under src/disputable may, lazily or by name."""
    assert {"fake_airwallex", "sandbox", "recordings", "cases", "web_helpers", "test_config"} <= TESTS_CODE
    planted = "\n".join([
        "import fake_airwallex", "from recordings import Ticking", "import tests.sandbox as bank", "def later(name):",
        "    import web_helpers", "    from cases import CASES", "    importlib.import_module('test_rounds')",
        "    __import__('sandbox')", "    importlib.import_module(name)",
        "    importlib.util.spec_from_file_location('x', 'tests/fake_airwallex.py')",
        "    from disputable import story", "    from . import recording", "    importlib.import_module('disputable.cli')"])
    assert imports_of_the_tests_code(planted) == [  # so the scan sees what it guards
        (1, "fake_airwallex"), (2, "recordings"), (3, "tests.sandbox"), (5, "web_helpers"), (6, "cases"),
        (7, "test_rounds"), (8, "sandbox"), (9, "import_module()"), (10, "spec_from_file_location()")]
    modules = sorted((ROOT / "src" / "disputable").rglob("*.py"))
    assert len(modules) > 40 and {"story.py", "cli.py", "recording.py", "issuer.py"} <= {path.name for path in modules}
    found = {path.relative_to(ROOT).as_posix(): imports_of_the_tests_code(path.read_text(encoding="utf-8"))
             for path in modules}
    assert {name: lines for name, lines in found.items() if lines} == {}


def test_python_dash_m_disputable_needs_the_cli_only_when_it_runs():
    tree = ast.parse((ROOT / "src" / "disputable" / "__main__.py").read_text(encoding="utf-8"))
    top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(node.module == "__future__" for node in top if isinstance(node, ast.ImportFrom)) and not [
        node for node in top if isinstance(node, ast.Import)]
    module = importlib.import_module("disputable.__main__")
    assert inspect.signature(module.run).return_annotation in ("int", int)
