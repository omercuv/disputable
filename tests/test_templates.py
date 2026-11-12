"""The template lint: the templates hold no words of their own, nothing that turns escaping off, no markup that can run
code or load anything, and no HTML comment. Every sentence a page shows comes from words.py or another module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from disputable.web import views

WEB = Path(views.__file__).parent
TEMPLATES = WEB / "templates"
NAMES = ("_voice.html", "approve.html", "base.html", "case.html", "money.html", "queue.html", "refused.html")
JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
TAG = re.compile(r"<[^<>]*>")


def source(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_the_templates_are_exactly_these():
    assert tuple(sorted(path.name for path in TEMPLATES.iterdir())) == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_no_letters_outside_tags_and_expressions(name):
    left = TAG.sub("", JINJA.sub("", source(name)))
    assert not re.search(r"[^\W\d_]", left), (name, re.findall(r"\S+", left)[:5])


@pytest.mark.parametrize("name", NAMES)
def test_nothing_turns_escaping_off(name):
    text = source(name)
    for bad in ("safe", "Markup", "autoescape", "raw", "|e", "| e", "escape", "mark_safe"):
        assert bad not in " ".join(JINJA.findall(text)), (name, bad)


@pytest.mark.parametrize("name", NAMES)
def test_no_markup_that_runs_code_or_loads_anything(name):
    markup = JINJA.sub("", source(name)).lower()
    assert "<!--" not in markup  # templates comment with {# #}: an HTML comment could hide what follows it
    assert "<script" not in markup and "javascript:" not in markup and "<iframe" not in markup
    assert "style=" not in markup and "<style" not in markup
    assert not re.search(r"\son[a-z]+\s*=", markup)  # no onclick= and the like
    assert "http://" not in markup and "https://" not in markup and "//" not in markup.replace("://", "")
    for found in re.findall(r"\b(?:href|src|action)=\"([^\"{]+)\"", markup):
        assert found.startswith("/"), (name, found)  # only this app's own paths


@pytest.mark.parametrize("name", NAMES)
def test_every_quoted_word_sits_in_its_own_paragraph(name):
    # The said part of a container holds the text and nothing else, so a planted line can't sit beside code's words.
    for found in re.findall(r'<p class="said">(.*?)</p>', source(name)):
        assert found == "{{ line.text }}", (name, found)


def test_only_the_voice_macro_writes_a_lines_text():
    for name in NAMES:
        text = source(name)
        if name != "_voice.html":
            assert ".text" not in text.replace("link.text", "").replace("x.text", ""), name


def test_the_stylesheet_loads_nothing():
    css = (WEB / "static" / "app.css").read_text(encoding="utf-8").lower()
    assert "url(" not in css and "@import" not in css and "expression(" not in css and "http" not in css


@pytest.mark.parametrize("path", [*(TEMPLATES / name for name in NAMES), WEB / "static" / "app.css"],
                         ids=[*NAMES, "app.css"])
def test_files_are_lf_utf8_and_lines_are_short(path):
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    for number, line in enumerate(raw.decode("utf-8").split("\n"), 1):
        assert len(line) <= 125 and line == line.rstrip(), (path.name, number)


def test_the_lint_itself_finds_what_it_looks_for():
    assert re.search(r"[^\W\d_]", TAG.sub("", JINJA.sub("", "<p>{{ x }} Approve</p>")))
    assert not re.search(r"[^\W\d_]", TAG.sub("", JINJA.sub("", '<p class="a">{{ x }}</p>{% if y %}{# note #}{% endif %}')))
    assert TAG.sub("", JINJA.sub("", "<h{{ level }}>{{ title }}</h{{ level }}>")) == ""
