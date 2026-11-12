"""What Claude's reads cost, and the guard that stops a run before it spends past its cap.

Prices are dollars per million tokens, from Anthropic's model list and its prompt-caching guide, read on Nov 6, 2026. A
call is priced from its own counts at its own model's rates. When the API lists the attempts inside one request (a model
that declined and the fallback that served), each attempt is priced at its own model's rates: the top-level counts are
only the serving attempt's. A model the table doesn't name is priced at the dearest rate of each column, so a guess is
never low, and unpriced() names it. The reader's cache entries last 5 minutes, so a cache write costs 1.25 x input.

The Meter wraps an SDK-shaped client. Before each request it works out the most the request could cost, and refuses
with BudgetReached when that could take the run past its cap, or one read past its own cap: nothing is sent then. A
request that may fall back can bill two answers (the requested model's, declined partway, and the fallback model's), so
the most counts both. After the request it charges what the API reported. An error response isn't charged: the API
doesn't bill it. But a request the SDK sent again after a timeout or a lost connection, or one cut by Ctrl+C, may be
billed with no answer to charge, so the Console's bill can be higher than the meter (a README limit). BudgetReached is a
plain Exception, not an SDK error and not a ValueError, so it leaves ClaudeReader.read() as it is. BudgetReader turns it
into a failed read (CAPPED): in the app the round goes to a person with code's draft, and its line says the run hit a
spend cap or the number of reads it was given. No clock, no network and no key here: the caller builds the client, and
the user approves the cap first (config.confirm_spend).

The eval, the story run and the loop use it like this:
    meter = Meter(cap)                                  # the cap the user approved, as a Decimal
    reader = BudgetReader(ClaudeReader(meter.wrap(client), profile=..., effort=...), meter)
    reading = reader.read(request)                      # each read under the read cap; BudgetReached becomes CAPPED
    the eval: with meter.read(label): reading = claude.read(request), and BudgetReached ends that read or the run
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping

from disputable import prompts
from disputable.fees import usd
from disputable.reader import Call, Failure, Iteration, Reader, Reading, ReadRequest, failed

MILLION = Decimal(1_000_000)
READ_CAP = Decimal("2.50")  # one read: a cut-off turn sent again still fits, with two answers reserved a request
CHARS_A_TOKEN = 2  # fewer than any text here takes, so the guess at a request's input is never low
PRICES_READ = "Nov 6, 2026"


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # a 5-minute cache entry: 1.25 x input


PRICES: Mapping[str, Price] = MappingProxyType({
    "claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00"), Decimal("0.20"), Decimal("5.00")),
    "claude-opus-5": Price(Decimal("5.00"), Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),  # a fallback
    "claude-opus-4-8": Price(Decimal("5.00"), Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),  # a fallback
})
DEAREST = Price(*(max(getattr(price, name) for price in PRICES.values())
                  for name in ("input", "output", "cache_read", "cache_write")))

RUN_CAP_REACHED = "The next request could take the run past its {cap} cap, so it wasn't sent."
READ_CAP_REACHED = "The next request could take this read past its {cap} cap, so it wasn't sent."


def price(model: str) -> Price:
    """The model's rates, or the dearest of each column for a model the table doesn't name."""
    return PRICES.get(model, DEAREST)


def call_cost(call: Call) -> Decimal:
    """What one request cost: each attempt in call.iterations at its own model's rates, when the API listed them, else
    the top-level counts at call.model's. An attempt that names no model is the requested model's when it is the first
    ("message"), else the model that served the turn."""
    if call.iterations:
        return sum((_cost(price(_model(item, call)), item.input_tokens, item.output_tokens, item.cache_read,
                          item.cache_write) for item in call.iterations), Decimal(0))
    return _cost(price(call.model), call.input_tokens, call.output_tokens, call.cache_read, call.cache_write)


def reading_cost(reading: Reading) -> Decimal:
    """What every request of a read cost, the attempts that didn't propose included."""
    return sum((call_cost(call) for call in reading.calls), Decimal(0))


def unpriced(calls: Iterable[Call]) -> tuple[str, ...]:
    """The models these calls name that the price table doesn't: they were priced at the dearest rates. Sorted."""
    named = set()
    for call in calls:
        named.add(call.model)
        named.update(_model(item, call) for item in call.iterations)
    return tuple(sorted(model for model in named if model not in PRICES))


def message_cost(message: Any, max_tokens: int) -> Decimal:
    """What the API reported one response cost, read as the reader records it in a Call."""
    return call_cost(_as_call(message, max_tokens))


class BudgetReached(Exception):
    """The next request could take the run, or one read, past its cap, so it wasn't sent. A plain Exception, so it leaves
    ClaudeReader.read() as it is. scope: "run" or "read"."""

    def __init__(self, scope: str, spent: Decimal, cap: Decimal) -> None:
        if scope not in ("run", "read"):
            raise ValueError('scope is "run" or "read".')
        line = RUN_CAP_REACHED if scope == "run" else READ_CAP_REACHED
        super().__init__(line.format(cap=usd(cap)))
        self.scope, self.spent, self.cap = scope, spent, cap


class Meter:
    """The run's spend, charged from what the API reports, and the two caps it holds before each request."""

    def __init__(self, cap: Decimal, read_cap: Decimal = READ_CAP) -> None:
        for name, value in (("cap", cap), ("read_cap", read_cap)):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} is a dollar amount above 0, as a Decimal.")
        self.cap, self.read_cap = cap, read_cap
        self.spent = Decimal(0)  # the whole run
        self.read_spent = Decimal(0)  # the read in progress, or the last one
        self.requests = 0  # requests sent and answered
        self._reading = False  # inside read(): read_cap holds too
        self._last: tuple[int, int] | None = None  # the last response: (its whole input, its output)

    def wrap(self, client: Any) -> Any:
        """The client with each beta.messages.create(**params) checked against both caps, then charged."""
        return _Metered(self, client)

    @contextmanager
    def read(self, label: str = "") -> Iterator[None]:
        """One read: its spend starts at 0, and its requests are held to read_cap as well as to the run's cap. Outside a
        read only the run's cap holds. label names the read for the caller; reads don't nest."""
        if self._reading:
            raise RuntimeError("A read is already open on this meter.")
        self._reading, self.read_spent, self._last = True, Decimal(0), None
        try:
            yield
        finally:
            self._reading, self._last = False, None

    def worst(self, params: Mapping[str, Any]) -> Decimal:
        """The most the next request could cost: each answer it may bill (_answers), with all of its max_tokens as output
        and the request's input at the cache-write rate. The input is the characters sent over 2 and, after a response
        in this read, at least that response's whole input and output (thinking stays in the context, unseen) and the
        newest turn's characters over 2. Every request of this app may fall back, so each reserves two answers: about
        $1.30 at most, and a run stops up to that much below its cap."""
        messages = params.get("messages") or []
        tokens = _tokens(params.get("system"), params.get("tools"), messages)
        if self._last is not None:
            before, said = self._last
            tokens = max(tokens, before + said + _tokens(messages[-1] if messages else None))
        output = max(int(params.get("max_tokens") or 0), 0)
        return sum((tokens * rates.cache_write + most * rates.output for rates, most in _answers(params, output)),
                   Decimal(0)) / MILLION

    def check(self, params: Mapping[str, Any]) -> None:
        """Raises BudgetReached, run first, when the next request could pass a cap."""
        worst = self.worst(params)
        if self.spent + worst > self.cap:
            raise BudgetReached("run", self.spent, self.cap)
        if self._reading and self.read_spent + worst > self.read_cap:
            raise BudgetReached("read", self.read_spent, self.read_cap)

    def charge(self, message: Any, max_tokens: int) -> Decimal:
        """Adds what the API reported for a response, and keeps its sizes for the next request's check."""
        cost = message_cost(message, max_tokens)
        self.spent += cost
        if self._reading:
            self.read_spent += cost
        self.requests += 1
        usage = message.usage
        whole = int(usage.input_tokens or 0) + int(usage.cache_read_input_tokens or 0) + int(
            usage.cache_creation_input_tokens or 0)
        self._last = (whole, int(usage.output_tokens or 0))
        return cost


class BudgetReader:
    """A reader whose BudgetReached becomes a failed read (CAPPED), so the round goes to a person with code's draft, and
    its line names the run's own cap, never the Claude account. With a meter, each read is held to the meter's read
    cap. It keys as the reader it wraps: rounds reads its profile and effort."""

    def __init__(self, reader: Reader, meter: Meter | None = None) -> None:
        self.reader, self.meter = reader, meter

    @property
    def profile(self) -> prompts.StoreProfile:
        return getattr(self.reader, "profile", prompts.FERNHILL)

    @property
    def effort(self) -> str:
        return getattr(self.reader, "effort", prompts.EFFORT)

    def read(self, request: ReadRequest) -> Reading:
        try:
            if self.meter is None:
                return self.reader.read(request)
            with self.meter.read(request.key[:8]):
                return self.reader.read(request)
        except BudgetReached:
            return failed(request.key, Failure.CAPPED)


# ----------------------------------------------------------------------------- inside
class _Metered:
    def __init__(self, meter: Meter, client: Any) -> None:
        self.beta = _Beta(meter, client)


class _Beta:
    def __init__(self, meter: Meter, client: Any) -> None:
        self.messages = _Messages(meter, client)


class _Messages:
    def __init__(self, meter: Meter, client: Any) -> None:
        self._meter, self._client = meter, client

    def create(self, **params: Any) -> Any:
        self._meter.check(params)
        message = self._client.beta.messages.create(**params)  # an error isn't charged: there's no answer to charge
        self._meter.charge(message, int(params.get("max_tokens") or 0))
        return message


def _answers(params: Mapping[str, Any], output: int) -> list[tuple[Price, int]]:
    """The answers one request may bill, as (rates, most output tokens). With no fallback: one, at the dearest rates.
    With fallbacks, the requested model's first, at its own rates: it may decline partway, and an answer declined
    partway is billed. Then one for each fallback model that may run, each within its own max_tokens: the dearest rates
    for the model the server picks ("default"), or each named model's rates."""
    fallbacks = params.get("fallbacks")
    if not fallbacks:
        return [(DEAREST, output)]
    answers = [(price(str(params.get("model") or "")), output)]
    for hop in fallbacks if isinstance(fallbacks, (list, tuple)) else [{}]:
        named = hop.get("model") if isinstance(hop, Mapping) else None
        most = hop.get("max_tokens") if isinstance(hop, Mapping) else None
        answers.append((price(named) if isinstance(named, str) and named else DEAREST,
                        output if most is None else max(int(most), 0)))
    return answers


def _cost(rates: Price, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> Decimal:
    return (input_tokens * rates.input + output_tokens * rates.output + cache_read * rates.cache_read
            + cache_write * rates.cache_write) / MILLION


def _model(item: Iteration, call: Call) -> str:
    if item.model:
        return item.model
    return prompts.MODEL if item.type == "message" else call.model


def _as_call(message: Any, max_tokens: int) -> Call:
    """A response as reader.ClaudeReader records it: optional SDK numbers as 0, every attempt in iterations."""
    usage = message.usage
    iterations = getattr(usage, "iterations", None) or ()
    fell_back = any(getattr(item, "type", None) == "fallback_message" for item in iterations) or any(
        block.type == "fallback" for block in message.content or ())
    return Call(str(message.model or ""), fell_back, str(message.stop_reason or ""), max_tokens,
                int(usage.input_tokens or 0), int(usage.output_tokens or 0), int(usage.cache_read_input_tokens or 0),
                int(usage.cache_creation_input_tokens or 0), tuple(_iteration(item) for item in iterations))


def _iteration(item: Any) -> Iteration:
    def count(name: str) -> int:
        return int(getattr(item, name, 0) or 0)
    return Iteration(str(getattr(item, "type", "") or ""), str(getattr(item, "model", "") or ""), count("input_tokens"),
                     count("output_tokens"), count("cache_read_input_tokens"), count("cache_creation_input_tokens"))


def _tokens(*parts: Any) -> int:
    """The tokens these parts could take at most: their characters over 2, rounded up."""
    chars = sum(len(part) if isinstance(part, str) else len(json.dumps(part, ensure_ascii=False, default=str))
                for part in parts if part is not None)
    return math.ceil(chars / CHARS_A_TOKEN)
