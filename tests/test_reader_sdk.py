"""The reader on the real anthropic client, offline: an httpx2.MockTransport answers every request, so the SDK itself
builds the body and headers, parses each reply and maps each error status to its class. No network and no key."""

from __future__ import annotations

import json
import os

import anthropic
import httpx2
import pytest

from disputable import prompts
from disputable.engine import Stage
from disputable.prompts import MODEL, case_prompt, system_prompt, tools_for
from disputable.reader import (READS, ClaudeReader, Failure, ReadRequest, ReaderBusy, claude_client, from_json,
                               live_reader, reading_key, to_json)
from disputable.seed.fernhill import FERNHILL as STORY, MONDAY
from disputable.seed.scripted import error_body, fallback, proposal, text, thinking, tool_use

CASE = STORY.case("headphones")


class Server:
    """The Messages API as a script: every request is kept, and each is answered with the next (status, JSON body), or
    by raising the next exception, as a dropped connection does."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx2.Response(status, json=body, headers={"request-id": f"req_{len(self.requests):03d}"})

    def body(self, number: int) -> dict:
        return json.loads(self.requests[number].content)


def read(*answers):
    server = Server(*answers)
    client = anthropic.Anthropic(api_key="test", base_url="https://api.anthropic.com", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(server)))
    request = ReadRequest(STORY, CASE, STORY.dispute(CASE.id, MONDAY), reading_key(STORY, CASE))
    return ClaudeReader(client).read(request), server


def reply(*content, stop_reason=None, model=MODEL, usage=None, stop_details=None):
    """A 200 from the API, as JSON. With no usage given, the cache counts are absent, as they can be."""
    if stop_reason is None:
        stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in content) else "end_turn"
    return 200, {"id": "msg_01", "type": "message", "role": "assistant", "model": model, "content": list(content),
                 "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
                 "usage": usage or {"input_tokens": 2400, "output_tokens": 310}}


def reads():
    return [tool_use(name, {"order_number": "1109"}) for name in READS]


def propose(**parts):
    return tool_use("propose_decision", proposal(**parts))


def test_the_sdk_sends_the_designed_request():
    fact = {"record_id": "scan-1109-delivered", "field": "signed_by", "quote": "A. SAMPLE"}
    reading, server = read(reply(*reads()), reply(propose(facts=[fact])))
    assert reading.failure is None and reading.complete and reading.rows[0].kept
    first = server.requests[0]
    assert (first.method, first.url.host, first.url.path) == ("POST", "api.anthropic.com", "/v1/messages")
    assert first.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert first.headers["authorization"] == "Bearer test" and first.headers["anthropic-version"]  # SDK 2.5.0's way
    body = server.body(0)
    assert set(body) == {"model", "max_tokens", "system", "tools", "tool_choice", "messages", "output_config",
                         "cache_control", "fallbacks"}  # no thinking, no sampling and no betas in the body
    assert (body["model"], body["max_tokens"], body["fallbacks"]) == (MODEL, 16_000, "default")
    assert body["tool_choice"] == {"type": "auto"} and body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["system"] == [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}]
    assert body["tools"] == tools_for(Stage.RFI) and all(tool["strict"] for tool in body["tools"])
    assert body["messages"] == [{"role": "user", "content": case_prompt(STORY, CASE)}]
    later = server.body(1)
    assert later["tools"] == body["tools"] and later["system"] == body["system"]  # the cached prefix never moves
    assert later["messages"][0] == body["messages"][0] and [turn["role"] for turn in later["messages"]] == [
        "user", "assistant", "user"]
    assert [(call.model, call.cache_read, call.cache_write) for call in reading.calls] == [(MODEL, 0, 0)] * 2
    assert from_json(to_json(reading)) == reading


def test_a_fallback_turn_goes_back_from_its_seam():
    seam, calls = fallback(category="general_harms"), reads()
    turn = [thinking("declined", "sig-a"), {**text("Reading."), "citations": None},
            tool_use("get_order_history", {"order_number": "1109"}, id="toolu_cut"), seam,
            thinking("", "sig-b"), text("Reading the records."), *calls]
    each = {"input_tokens": 1200, "output_tokens": 150, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    served = {"input_tokens": 1200, "output_tokens": 150, "iterations": [
        {"type": "message", **each, "model": MODEL}, {"type": "fallback_message", **each, "model": "claude-opus-5"}]}
    reading, server = read(reply(*turn, model="claude-opus-5", usage=served), reply(propose()))
    assert reading.failure is None
    sent = server.body(1)["messages"][1]["content"]
    assert sent == [{"type": "text", "text": "Reading."}, seam, {"type": "thinking", "thinking": "", "signature": "sig-b"},
                    {"type": "text", "text": "Reading the records."}, *calls]
    assert [block["tool_use_id"] for block in server.body(1)["messages"][2]["content"][:-1]] == [
        block["id"] for block in calls]
    assert "toolu_cut" not in server.requests[1].content.decode()
    assert [(call.model, call.fell_back) for call in reading.calls] == [("claude-opus-5", True), (MODEL, False)]


ERRORS = [  # (label, status, error_code, what the read comes to)
    ("429", 429, None, ReaderBusy),
    ("429 spend limit", 429, "enforced_spend_limit_reached", Failure.ACCOUNT),
    ("402 billing", 402, None, Failure.ACCOUNT),
    ("400 on hold", 400, "organization_on_hold", Failure.ACCOUNT),
    ("401", 401, None, Failure.NOT_ALLOWED),
    ("403", 403, None, Failure.NOT_ALLOWED),
    ("400", 400, None, Failure.BAD_REQUEST),
    ("404", 404, None, Failure.BAD_REQUEST),
    ("413", 413, None, Failure.BAD_REQUEST),
    ("422", 422, None, Failure.BAD_REQUEST),
    ("500", 500, None, ReaderBusy),
    ("503", 503, None, ReaderBusy),
    ("504", 504, None, ReaderBusy),
    ("529", 529, None, ReaderBusy),
    ("408", 408, None, ReaderBusy),
    ("409", 409, None, ReaderBusy),
]


@pytest.mark.parametrize("status, code, outcome", [error[1:] for error in ERRORS], ids=[error[0] for error in ERRORS])
def test_errors_map_from_real_status_codes_and_bodies(status, code, outcome):
    answers = (reply(*reads()), (status, error_body(status, code=code)))
    if outcome is ReaderBusy:
        with pytest.raises(ReaderBusy) as busy:
            read(*answers)
        assert [call.name for call in busy.value.log] == list(READS) and len(busy.value.calls) == 1
        assert str(status) in busy.value.why
        return
    reading, server = read(*answers)
    assert reading.failure is outcome and len(server.requests) == 2  # max_retries=0: the SDK tried once
    assert [call.name for call in reading.tool_log] == list(READS) and from_json(to_json(reading)) == reading


def test_a_400_for_an_account_with_no_credit_is_the_accounts_failure():
    """The API answers an account with no credit with a 400, not a 402 (seen live on Nov 11, 2026). The read is then an
    account failure, so an eval run stops at once on it, never with "a bug in this app"."""
    message = ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or "
               "purchase credits.")
    reading, server = read((400, error_body(400, message=message, error_type="invalid_request_error")))
    assert reading.failure is Failure.ACCOUNT and len(server.requests) == 1 and reading.tool_log == ()
    assert from_json(to_json(reading)) == reading
    other, _ = read((400, error_body(400, message="max_tokens: must be a positive number")))
    assert other.failure is Failure.BAD_REQUEST  # any other 400 is still this app's own request


@pytest.mark.parametrize("error", [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")],
                         ids=["connection", "timeout"])
def test_a_connection_that_fails_is_busy(error):
    with pytest.raises(ReaderBusy) as busy:
        read(error)
    assert busy.value.log == () and busy.value.calls == ()


def test_the_sdks_guard_against_a_long_call_is_a_failed_read_not_a_crash(monkeypatch):
    monkeypatch.setattr(prompts, "RETRY_MAX_TOKENS", 21_334)  # one token past what the SDK sends without streaming
    reading, server = read(reply(text("..."), stop_reason="max_tokens"))
    assert reading.failure is Failure.ERROR and len(server.requests) == 1  # the resend never left this machine


def test_a_refusal_whose_fallback_could_not_run_is_busy_and_a_plain_one_is_final():
    could_not_run = {"type": "refusal", "category": "cyber", "explanation": None, "recommended_model": "claude-opus-5"}
    with pytest.raises(ReaderBusy):
        read(reply(stop_reason="refusal", stop_details=could_not_run))
    final = {**could_not_run, "recommended_model": None}
    reading, _ = read(reply(stop_reason="refusal", stop_details=final))
    assert reading.failure is Failure.REFUSED


def test_live_reader_names_the_anthropic_api_whatever_the_shell_says(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    reader = live_reader("sk-ant-test")
    assert str(reader.client.base_url).rstrip("/") == "https://api.anthropic.com"
    assert reader.client.api_key == "sk-ant-test" and reader.client.max_retries == 2


def test_the_claude_client_takes_nothing_from_the_shell(monkeypatch, tmp_path):
    """The address, the headers, the proxy and the certificates are this app's own, whatever the shell sets (cli-1,
    cli-5). A certificate file that holds no certificate would make a client that trusts the shell fail to build."""
    no_certificate = tmp_path / "none.pem"
    no_certificate.write_text("not a certificate\n", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(no_certificate))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example.com")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer from-the-shell\nX-From-The-Shell: yes")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:9")
    client = claude_client(" sk-ant-test ")
    try:
        assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
        assert client.api_key == "sk-ant-test" and client.max_retries == 2
        headers = {name.lower(): value for name, value in client.default_headers.items()}
        assert "x-from-the-shell" not in headers and "from-the-shell" not in str(headers)
        assert client._client.trust_env is False  # no certificate or .netrc setting from the shell
        assert not client._client._mounts  # and no proxy: the SDK mounts the shell's only when it has no transport
        assert type(client._client._transport._pool).__name__ == "ConnectionPool"  # not an HTTPProxy pool
        assert client._client._transport._pool._socket_options  # the SDK's TCP keepalive, kept for long reads
        assert os.environ["ANTHROPIC_CUSTOM_HEADERS"].startswith("Authorization: Bearer from-the-shell")  # put back
    finally:
        client.close()
    reader = live_reader("sk-ant-test")
    assert str(reader.client.base_url).rstrip("/") == "https://api.anthropic.com"
    assert "x-from-the-shell" not in {name.lower() for name in reader.client.default_headers}
    reader.client.close()
