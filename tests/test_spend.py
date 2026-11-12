"""spend.py: prices, the cost of a call and of a read, the meter's two caps, and BudgetReader.

Offline: the scripted FakeClient answers with real SDK usage objects, so the meter charges what the API would report.
Nothing here reads live or holds a key.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import cases
from disputable import prompts
from disputable.lifecycle import Phase, as_case, phase
from disputable.reader import WHY, Call, ClaudeReader, Failure, Iteration, NoReader, ReaderBusy, failed, reading_key
from disputable.rounds import E1, MemorySaved, decide_round
from disputable.seed import scripted
from disputable.seed.fernhill import FERNHILL, MONDAY
from disputable.spend import (DEAREST, PRICES, READ_CAP, BudgetReached, BudgetReader, Meter, Price, call_cost,
                              message_cost, price, reading_cost, unpriced)
from test_rounds import at_decide

MILLION = 1_000_000
OPUS_5_5, OPUS_5 = "claude-opus-5-5", "claude-opus-5"


def call(model=OPUS_5_5, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, iterations=()):
    return Call(model, bool(iterations), "end_turn", 16_000, input_tokens, output_tokens, cache_read, cache_write,
                tuple(iterations))


def script(label="headphones"):
    """The perfect reader's replies for a read case, and the request they answer."""
    read = cases.READ[label]
    return list(scripted.perfect(read.store, read.case, read.answer)), cases.request(label)


def fell_back(context=4_600, declined=15_900, served=16_000):
    """Usage of a turn the requested model declined partway, after `declined` output tokens, and a fallback model then
    answered whole: the API bills both attempts, each at its own model's rates, and the fallback's cache is cold."""
    return {"input_tokens": 300, "output_tokens": served, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": context - 300,
            "iterations": [
                {"type": "message", "model": OPUS_5_5, "input_tokens": 300, "output_tokens": declined,
                 "cache_read_input_tokens": context - 300, "cache_creation_input_tokens": 0},
                {"type": "fallback_message", "model": OPUS_5, "input_tokens": 300, "output_tokens": served,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": context - 300}]}


def falling_back(replies, grows=0):
    """The replies with every turn declined partway and answered by the fallback model. grows: tokens the context
    gains each turn, as a read's does."""
    return [reply.model_copy(update={"model": OPUS_5, "usage": scripted.message(
        usage=fell_back(4_600 + grows * number)).usage}) for number, reply in enumerate(replies)]


# ----------------------------------------------------------------------------- prices
def test_the_prices_are_the_model_lists_with_five_minute_cache_writes():
    assert PRICES == {
        OPUS_5_5: Price(Decimal("4.00"), Decimal("20.00"), Decimal("0.20"), Decimal("5.00")),
        OPUS_5: Price(Decimal("5.00"), Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),
        "claude-opus-4-8": Price(Decimal("5.00"), Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),
    }
    assert all(rates.cache_write == rates.input * Decimal("1.25") for rates in PRICES.values())
    assert DEAREST == Price(Decimal("5.00"), Decimal("25.00"), Decimal("0.50"), Decimal("6.25"))
    assert prompts.MODEL in PRICES and scripted.FALLBACK_MODEL in PRICES
    assert price("claude-made-up-9") == DEAREST and price(OPUS_5_5) == PRICES[OPUS_5_5]


def test_a_call_is_priced_from_its_counts_at_its_models_rates():
    assert call_cost(call(input_tokens=MILLION)) == Decimal("4")
    assert call_cost(call(output_tokens=MILLION)) == Decimal("20")
    assert call_cost(call(cache_read=MILLION)) == Decimal("0.2")
    assert call_cost(call(cache_write=MILLION)) == Decimal("5")
    assert call_cost(call(input_tokens=1_200, output_tokens=300, cache_read=4_000, cache_write=800)) == Decimal("0.0156")
    assert call_cost(call(OPUS_5, input_tokens=1_200, output_tokens=300)) == Decimal("0.0135")
    assert call_cost(call()) == 0 and type(call_cost(call())) is Decimal


def test_a_fallback_call_is_priced_by_each_iteration_at_its_own_model():
    # The model that declined bills at its own rates, the fallback at its own: the top-level counts are only the
    # serving attempt's, so pricing them alone would leave the first attempt out.
    reply = scripted.message(scripted.text("done"), usage=scripted.usage(1_000, 200, served_by=OPUS_5), model=OPUS_5)
    expected = Decimal(1_000 * 4 + 200 * 20 + 1_000 * 5 + 200 * 25) / MILLION
    assert message_cost(reply, 16_000) == expected == Decimal("0.018")
    # An attempt that names no model is the requested model's when it comes first, else the one that served.
    unnamed = (Iteration("message", "", 1_000, 0, 0, 0), Iteration("fallback_message", "", 1_000, 0, 0, 0))
    assert call_cost(call(OPUS_5, iterations=unnamed)) == Decimal("0.009")
    # Through the reader: the Call it records is priced the same.
    replies, request = script()
    replies[-1] = replies[-1].model_copy(update={"usage": reply.usage, "model": OPUS_5})
    reading = ClaudeReader(scripted.FakeClient(replies)).read(request)
    assert reading.calls[-1].fell_back and call_cost(reading.calls[-1]) == expected


def test_an_unknown_model_is_priced_at_the_dearest_rates_and_named():
    unknown = call("claude-made-up-9", input_tokens=MILLION, output_tokens=MILLION, cache_read=MILLION,
                   cache_write=MILLION)
    assert call_cost(unknown) == Decimal("36.75")  # 5.00 + 25.00 + 0.50 + 6.25
    assert unpriced([unknown, call()]) == ("claude-made-up-9",)
    assert unpriced([call(OPUS_5, iterations=(Iteration("message", "claude-other-1", 1, 1, 0, 0),))]) == (
        "claude-other-1",)
    assert unpriced([call(), call(OPUS_5)]) == ()


def test_a_readings_cost_is_every_request_it_made():
    reading = cases.read_with("headphones")[0]
    assert len(reading.calls) == 3 and reading_cost(reading) == sum(map(call_cost, reading.calls))
    assert reading_cost(reading) == 3 * Decimal(1_200 * 4 + 300 * 20) / MILLION  # the scripted usage, three times


# ----------------------------------------------------------------------------- the meter
def test_the_meter_refuses_a_request_that_could_pass_the_cap():
    replies, request = script()
    client = scripted.FakeClient(replies)
    meter = Meter(Decimal("0.30"))  # less than all of max_tokens at the dearest output rate: $0.40
    with pytest.raises(BudgetReached) as reached:
        ClaudeReader(meter.wrap(client)).read(request)
    assert reached.value.scope == "run" and client.requests == [] and meter.spent == 0 and meter.requests == 0
    assert str(reached.value) == "The next request could take the run past its $0.30 cap, so it wasn't sent."
    # A cap that lets one request through (it reserves about $0.79: two answers, as the turn may fall back): the
    # second could pass it, so it never reaches the client.
    client = scripted.FakeClient(replies)
    meter = Meter(Decimal("0.80"))
    with pytest.raises(BudgetReached):
        ClaudeReader(meter.wrap(client)).read(request)
    assert len(client.requests) == 1 and meter.requests == 1 and meter.spent == Decimal("0.0108")


def test_budget_reached_leaves_claude_reader_read_as_it_is():
    # Not an SDK error and not a ValueError: the reader doesn't turn it into ERROR, and nothing is caught on the way.
    assert not issubclass(BudgetReached, (ValueError, ReaderBusy)) and issubclass(BudgetReached, Exception)
    replies, request = script()
    with pytest.raises(BudgetReached):
        ClaudeReader(Meter(Decimal("0.01")).wrap(scripted.FakeClient(replies))).read(request)
    with pytest.raises(ValueError):
        BudgetReached("month", Decimal(0), Decimal(1))


def test_the_read_cap_ends_one_read_and_the_run_goes_on():
    meter = Meter(Decimal("10"), read_cap=Decimal("0.80"))
    replies, request = script()
    with meter.read("headphones"):
        with pytest.raises(BudgetReached) as reached:
            ClaudeReader(meter.wrap(scripted.FakeClient(replies))).read(request)
    assert reached.value.scope == "read" and meter.read_spent == Decimal("0.0108")
    assert str(reached.value) == "The next request could take this read past its $0.80 cap, so it wasn't sent."
    # The next read starts at 0 again, and with a read cap that holds it, it reads to the end.
    meter.read_cap = READ_CAP
    with meter.read("headphones again"):
        reading = ClaudeReader(meter.wrap(scripted.FakeClient(replies))).read(request)
    assert reading.failure is None and meter.read_spent == reading_cost(reading)
    assert meter.spent == Decimal("0.0108") + reading_cost(reading) and meter.requests == 4


def test_outside_a_read_only_the_run_cap_holds_and_reads_dont_nest():
    meter = Meter(Decimal("10"), read_cap=Decimal("0.01"))
    replies, request = script()
    reading = ClaudeReader(meter.wrap(scripted.FakeClient(replies))).read(request)
    assert reading.failure is None and meter.spent == reading_cost(reading) and meter.read_spent == 0
    with meter.read("one"):
        with pytest.raises(RuntimeError):
            with meter.read("two"):
                pass
    for bad in (Decimal(0), Decimal(-1), Decimal("NaN"), 3, 0.5):
        with pytest.raises(ValueError):
            Meter(bad)


def test_the_meter_charges_a_read_that_fails_validation():
    read = cases.READ["headphones"]
    replies = [*scripted.empty(read.store, read.case)[:-1],
               scripted.message(scripted.thinking(), scripted.tool_use("propose_decision", {"facts": "all of them"}))]
    meter = Meter(Decimal("10"))
    with meter.read("headphones"):
        reading = ClaudeReader(meter.wrap(scripted.FakeClient(replies))).read(cases.request("headphones"))
    assert reading.failure is Failure.MALFORMED and reading.proposal
    assert meter.spent == reading_cost(reading) == 3 * Decimal("0.0108")


def test_the_meters_spend_for_a_read_equals_its_readings_cost():
    # A cut-off turn is sent again with more room, and a declined turn bills both attempts: the meter charges each
    # response as the reader records it.
    replies, request = script("headphones_round_2")
    cut = scripted.message(scripted.thinking(), stop_reason="max_tokens", usage=scripted.usage(5_000, 16_000))
    fell = replies[-1].model_copy(update={"usage": scripted.message(
        usage=scripted.usage(2_000, 900, cache_read=3_000, cache_write=500, served_by=OPUS_5)).usage})
    meter = Meter(Decimal("10"))  # the default read cap leaves room for the resend: $0.34 spent, $1.19 reserved
    with meter.read("round two"):
        reading = ClaudeReader(meter.wrap(scripted.FakeClient([cut, *replies[:-1], fell]))).read(request)
    assert reading.failure is None and len(reading.calls) == 4 and reading.calls[0].stop_reason == "max_tokens"
    assert meter.spent == meter.read_spent == reading_cost(reading) and meter.requests == 4
    # Every scripted read case, perfect and empty, charged one at a time.
    for label in ("earbuds", "headphones", "ending_2", "trick_system_note"):
        for make in (scripted.perfect_for, scripted.empty_for):
            meter = Meter(Decimal("10"))
            request = cases.request(label)
            with meter.read(label):
                reading = ClaudeReader(meter.wrap(scripted.FakeClient(make(request)))).read(request)
            assert meter.spent == reading_cost(reading) > 0, label


def test_a_turn_that_falls_back_bills_two_answers_and_stays_inside_both_caps():
    # Every request asks for a fallback model. A turn the requested model declines partway bills that attempt as well
    # as the fallback's answer, so the meter reserves both, and neither cap is ever passed. Spend only grows, so a
    # check at the end holds for every request.
    replies, request = script()
    each = message_cost(scripted.message(usage=fell_back()), 16_000)
    assert each == Decimal("0.748435")  # $0.32 for the answer that was declined, $0.43 for the fallback's
    answered = {}
    for cap in (Decimal("0.50"), Decimal("0.80"), Decimal("1.60"), Decimal("3")):
        meter = Meter(cap)
        with contextlib.suppress(BudgetReached):
            ClaudeReader(meter.wrap(scripted.FakeClient(falling_back(replies)))).read(request)
        assert meter.spent <= cap and meter.spent == meter.requests * each, cap
        answered[cap] = meter.requests
    # $0.50 can't hold one such turn; $0.80 holds the first (it reserves $0.79), $1.60 too; $3 holds the whole read.
    assert answered == {Decimal("0.50"): 0, Decimal("0.80"): 1, Decimal("1.60"): 1, Decimal("3"): 3}
    # The cap for one read holds the same way, at the cap the eval used before and at today's.
    for read_cap in (Decimal("1.50"), READ_CAP):
        meter = Meter(Decimal("100"), read_cap=read_cap)
        with meter.read("headphones"), contextlib.suppress(BudgetReached):
            ClaudeReader(meter.wrap(scripted.FakeClient(falling_back(replies, grows=4_400)))).read(request)
        assert 0 < meter.read_spent <= read_cap, read_cap


def test_an_error_is_not_charged():
    _, request = script()
    meter = Meter(Decimal("10"))
    with pytest.raises(ReaderBusy):
        ClaudeReader(meter.wrap(scripted.FakeClient([scripted.sdk_error(529)]))).read(request)
    assert meter.spent == 0 and meter.requests == 0
    reading = ClaudeReader(meter.wrap(scripted.FakeClient([scripted.sdk_error(401)]))).read(request)
    assert reading.failure is Failure.NOT_ALLOWED and meter.spent == 0


def test_the_worst_a_request_can_cost():
    meter = Meter(Decimal("10"))
    first = {"system": [{"type": "text", "text": "x" * 4_001}], "tools": [{"name": "a_tool"}],
             "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16_000}
    chars = sum(len(json.dumps(first[name], ensure_ascii=False)) for name in ("system", "tools", "messages"))
    assert meter.worst(first) == (Decimal(-(-chars // 2)) * Decimal("6.25") + 16_000 * Decimal(25)) / MILLION
    # After a response in this read: at least its whole input and its output, and the newest turn. Claude's thinking
    # stays in the context but is never sent back as text, so the characters alone would guess too low.
    replies, request = script()
    small = {"system": "s", "tools": [], "messages": [{"role": "user", "content": "y" * 10}], "max_tokens": 16_000}
    newest = -(-len(json.dumps(small["messages"][-1])) // 2)
    with meter.read("headphones"):
        ClaudeReader(meter.wrap(scripted.FakeClient(replies[:1] + [scripted.sdk_error(400)]))).read(request)
        assert meter.worst(small) == (Decimal(1_200 + 300 + newest) * Decimal("6.25") + 16_000 * 25) / MILLION
    assert meter.worst(small) < (Decimal(1_200) * Decimal("6.25") + 16_000 * 25) / MILLION  # a new read starts afresh
    # With no fallback, the biggest request reserves one answer, about $0.75: 21,000 output tokens, a 30,000-token
    # context.
    big = {**first, "system": [{"type": "text", "text": "z" * 60_000}], "max_tokens": prompts.RETRY_MAX_TOKENS}
    assert Decimal("0.70") < meter.worst(big) < Decimal("0.75")
    # A request that may fall back, as each of this app's does, reserves two answers: the requested model's at its own
    # rates (it can decline partway, and that is billed) and the fallback model's at the dearest rates.
    tokens = Decimal(-(-chars // 2))
    falls = {**first, "model": OPUS_5_5, "betas": list(prompts.BETAS), "fallbacks": prompts.FALLBACKS}
    assert meter.worst(falls) == meter.worst(first) + (tokens * Decimal("5.00") + 16_000 * Decimal(20)) / MILLION
    assert Decimal("1.25") < meter.worst({**big, "model": OPUS_5_5, "fallbacks": prompts.FALLBACKS}) < Decimal("1.30")
    # Named fallbacks: one more answer each, at its own model's rates (the dearest for one the table doesn't name) and
    # within its own max_tokens when it gives one.
    named = {**falls, "fallbacks": [{"model": "claude-opus-4-8", "max_tokens": 1_000}, {"model": "claude-made-up-9"}]}
    assert meter.worst(named) == (tokens * Decimal("5.00") + 16_000 * Decimal(20) + tokens * Decimal("6.25")
                                  + 1_000 * Decimal(25) + tokens * Decimal("6.25") + 16_000 * Decimal(25)) / MILLION
    assert READ_CAP == Decimal("2.50")  # room for a cut-off turn sent again: $0.34 spent, then about $1.19 reserved


# ----------------------------------------------------------------------------- BudgetReader
def test_budget_reader_sends_the_round_to_a_person_with_codes_draft():
    record = at_decide("headphones")
    replies, _ = script()
    meter = Meter(Decimal("0.30"))
    saved = MemorySaved()
    step = decide_round(record, FERNHILL, BudgetReader(ClaudeReader(meter.wrap(scripted.FakeClient(replies))), meter),
                        saved, MONDAY)
    assert step.reading == failed(reading_key(FERNHILL, as_case(record)), Failure.CAPPED)
    plan = step.record.rounds[-1].plan
    assert phase(step.record) is Phase.APPROVE and plan.hand_off and plan.needs_approval
    # The line names the run's own cap, never the Claude account: the account was fine.
    assert ("Claude couldn't read this case's records (this run hit a spend cap or the number of reads it was given), so "
            "a person reads them before anything is sent.") == E1.format(why=WHY[Failure.CAPPED])
    assert E1.format(why=WHY[Failure.CAPPED]) in plan.reasons
    assert not [reason for reason in plan.reasons if "account" in reason.casefold()]
    # The draft is code alone's: what the same round gives with no key at all.
    alone = decide_round(record, FERNHILL, NoReader(), MemorySaved(), MONDAY).record.rounds[-1].plan
    assert plan.terms == alone.terms
    assert saved.readings[-1][2] == step.reading and meter.spent == 0


def test_budget_reader_keys_as_the_reader_it_wraps():
    profile = prompts.StoreProfile("Another Store", "Sam", "help@another.example")
    replies, request = script()
    wrapped = BudgetReader(ClaudeReader(scripted.FakeClient(replies), profile=profile, effort="medium"))
    assert (wrapped.profile, wrapped.effort) == (profile, "medium")
    assert (BudgetReader(NoReader()).profile, BudgetReader(NoReader()).effort) == (prompts.FERNHILL, prompts.EFFORT)
    record = at_decide("headphones")
    step = decide_round(record, FERNHILL, wrapped, MemorySaved(), MONDAY)
    assert step.reading.key == reading_key(FERNHILL, as_case(record), profile=profile, effort="medium")
    assert step.reading.failure is None
    # Without a meter the reader's own answer comes through, and a cap anywhere becomes CAPPED.
    assert BudgetReader(ClaudeReader(scripted.FakeClient(replies))).read(request) == cases.read_with("headphones")[0]
    capped = BudgetReader(ClaudeReader(Meter(Decimal("0.01")).wrap(scripted.FakeClient(replies))))
    assert capped.read(request) == failed(request.key, Failure.CAPPED)


# ----------------------------------------------------------------------------- what spend imports
NEVER = ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web")
FRESH = """
import importlib, importlib.abc, json, sys, types

class Never(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == never or name.startswith(never + ".") for never in %r):
            raise ImportError(name + " is blocked: spend must not need it")
        return None

sys.meta_path.insert(0, Never())
sdk = types.ModuleType("anthropic")  # a stand-in for the SDK, which takes seconds to import here
sdk.__getattr__ = lambda name: type(name, (Exception,), {})
sys.modules["anthropic"] = sdk
importlib.import_module("disputable.spend")
print(json.dumps(sorted(name for name in sys.modules if name.startswith("disputable"))))
""" % (NEVER,)


def test_spend_never_imports_the_seed_or_the_outer_parts():
    tree = ast.parse(Path(importlib.util.find_spec("disputable.spend").origin).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update({node.module or "", *(f"{node.module}.{alias.name}" for alias in node.names)})
    assert "disputable.reader" in names
    assert not [name for name in names if any(name == never or name.startswith(never + ".") for never in NEVER)]
    fresh = subprocess.run([sys.executable, "-B", "-c", FRESH], capture_output=True, text=True, timeout=120)
    assert fresh.returncode == 0, fresh.stderr
    assert "disputable.spend" in json.loads(fresh.stdout)
