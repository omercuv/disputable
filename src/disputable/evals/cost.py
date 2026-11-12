"""The offline guess at what live reads cost. No client, no key and no call: prices, the meter and the cost of a reading
are disputable.spend's.

Sizes are measured from this repo: the system prompt and the stage's tools as the API gets them, each case prompt, and
the results of every read the stage's tools reach (the reads the scripted reader makes). Claude's thinking and answers are
the unknown part, about 80% of the cost, so three scenarios guess them. Token guesses are often off by 3 to 10 times, so
nothing quotes this as a price: config.confirm_spend() quotes only what the eval measured, and the live command prints
this as "Offline guess, not measured".

How a read is costed, request by request: request 1 sends the cached prefix (tools and system: a cache read once the
stage's prefix is warm) and writes the case prompt to the cache; each later request reads everything before it from the
cache and writes what the last turn added (Claude's output, the tool calls and their results). Each stage's first read
writes its prefix once.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from disputable import prompts, spend
from disputable.engine import Stage
from disputable.evals.sites import STORY, Site
from disputable.fees import usd
from disputable.reader import Toolbox

CENT = Decimal("0.01")
OFFLINE = ("Offline estimate. No API call. Sizes come from this repo's prompts, tools and records. Claude's thinking is "
           "the unknown part, so these are three guesses. The pilot measures the real price.")
_SIZES: dict[int, tuple[Site, "Size"]] = {}


@dataclass(frozen=True)
class Scenario:
    """A guess at what can't be measured offline."""
    prose: float  # characters a token, for the prompts' text
    json: float  # characters a token, for JSON
    overhead: int  # tokens the API adds for tool use
    extra: float  # results beyond what the scripted reads show, as a share of them
    output: Mapping[str, tuple[int, ...]]  # output tokens of each request of a read, by effort


SCENARIOS: Mapping[str, Scenario] = {
    "low": Scenario(4.0, 3.2, 300, 0.0, {"medium": (250, 400, 900), "high": (400, 700, 1600)}),
    "central": Scenario(3.5, 2.7, 400, 0.0, {"medium": (350, 600, 500, 1600), "high": (500, 900, 800, 2600)}),
    "high": Scenario(3.0, 2.2, 600, 0.3, {"medium": (700, 1200, 1200, 1000, 3500),
                                          "high": (1200, 2200, 2200, 1800, 6500)}),
}


@dataclass(frozen=True)
class Size:
    """What one site sends, in characters."""
    prompt: int  # the case prompt
    history: int  # the order history's result
    others: int  # the results of every other read the stage's tools reach
    calls: int  # those reads


@dataclass(frozen=True)
class Guess:
    scenario: str
    effort: str
    reads: int
    total: Decimal  # every read, and each stage's first prefix write
    story: Decimal  # the story's reads among them
    median: Decimal  # one read
    most: Decimal


@dataclass(frozen=True)
class Estimate:
    system: int  # characters of the system prompt
    tools: Mapping[str, int]  # characters of each stage's tools, as JSON
    prompts: tuple[int, ...]  # each site's case prompt, sorted
    results: tuple[int, ...]  # each site's tool results, sorted
    guesses: tuple[Guess, ...]

    def guess(self, scenario: str, effort: str) -> Guess:
        return next(guess for guess in self.guesses if (guess.scenario, guess.effort) == (scenario, effort))

    def total(self, scenario: str) -> Decimal:
        return sum((guess.total for guess in self.guesses if guess.scenario == scenario), Decimal(0))


def estimate(sites: Sequence[Site], efforts: Sequence[str]) -> Estimate:
    """The guess for reading these sites at these efforts: offline, from the sizes this repo gives today."""
    for effort in efforts:
        if any(effort not in scenario.output for scenario in SCENARIOS.values()):
            raise ValueError(f"There's no guess for effort {effort}. The eval reads at medium or high.")
    sizes = [(site, size_of(site)) for site in sites]
    stages = sorted({site.case.stage for site in sites}, key=lambda stage: stage.value)
    guesses = []
    for name, scenario in SCENARIOS.items():
        for effort in efforts:
            each = [(site, _read_cost(size, site.case.stage, effort, scenario)) for site, size in sizes]
            costs = sorted(cost for _, cost in each)
            premium = float(_rates().cache_write - _rates().cache_read)  # a cold prefix is written, not read
            cold = sum(_prefix(stage, scenario) * premium for stage in stages) / 1e6
            guesses.append(Guess(name, effort, len(each), _money(sum(costs) + cold),
                                 _money(sum(cost for site, cost in each if site.label in STORY)),
                                 _money(statistics.median(costs)) if costs else Decimal(0),
                                 _money(costs[-1]) if costs else Decimal(0)))
    tools = {stage.value: len(json.dumps(prompts.tools_for(stage), sort_keys=True)) for stage in (Stage.RFI,
                                                                                                    Stage.CHARGEBACK)}
    return Estimate(len(prompts.system_prompt()), tools, tuple(sorted(size.prompt for _, size in sizes)),
                    tuple(sorted(size.history + size.others for _, size in sizes)), tuple(guesses))


def lines(guess: Estimate, efforts: Sequence[str]) -> list[str]:
    """The estimate as the estimate command prints it."""
    said = [OFFLINE,
            f"The system prompt is {guess.system:,} characters. The tools are {guess.tools['RFI']:,} characters of JSON "
            f"at RFI and {guess.tools['CHARGEBACK']:,} at CHARGEBACK."]
    if guess.prompts:
        said.append(f"A case prompt is {_span(guess.prompts)} characters. One read's tool results are "
                    f"{_span(guess.results)} characters.")
    for effort in efforts:
        low, central, high = (guess.guess(name, effort) for name in SCENARIOS)
        said.append(f"Effort {effort}, {central.reads} reads: low {usd(low.total)}, central {usd(central.total)}, high "
                    f"{usd(high.total)}. One read, central: median {usd(central.median)}, most {usd(central.most)}.")
        if central.story:
            said.append(f"The story's reads among them at effort {effort}: low {usd(low.story)}, central "
                        f"{usd(central.story)}, high {usd(high.story)}.")
    return said


def size_of(site: Site) -> Size:
    """What one site sends. Measured once per site: the site is held, so its id can't be reused."""
    known = _SIZES.get(id(site))
    if known is not None and known[0] is site:
        return known[1]
    box = Toolbox.for_case(site.store, site.case)
    history = len(box.run("get_order_history", {"order_number": site.case.order_number})[0])
    reads = [(name, {"order_number": number}) for number in box.numbers for name in ("get_carrier_scans", "search_inbox")]
    reads += [("get_warranty_log", {"serial": serial}) for serial in box.serials.values()]
    others = sum(len(box.run(name, given)[0]) for name, given in reads)
    size = Size(len(prompts.case_prompt(site.store, site.case)), history, others, 1 + len(reads))
    _SIZES[id(site)] = site, size
    return size


def _rates() -> spend.Price:
    return spend.price(prompts.MODEL)


def _prefix(stage: Stage, scenario: Scenario) -> float:
    """Tokens of the prefix every request sends: the system prompt, the stage's tools and the API's tool-use overhead."""
    system = len(prompts.system_prompt()) / scenario.prose
    tools = len(json.dumps(prompts.tools_for(stage), sort_keys=True)) / scenario.json
    return system + tools + scenario.overhead


def _read_cost(size: Size, stage: Stage, effort: str, scenario: Scenario) -> float:
    """One read's dollars, with the stage's prefix warm."""
    rates = _rates()
    read_price, write_price, out_price = float(rates.cache_read), float(rates.cache_write), float(rates.output)
    prefix, outs = _prefix(stage, scenario), scenario.output[effort]
    requests = len(outs)
    case = size.prompt / scenario.prose * 1.1  # a little JSON and numbers in the prompt
    middle = size.others * (1 + scenario.extra) / scenario.json / max(1, requests - 2)
    results = [size.history / scenario.json, *[middle] * (requests - 2), 0.0]
    reminder = len(prompts.REMINDER) / scenario.prose + 10 * size.calls / max(1, requests - 1)
    calls = 25 * size.calls / max(1, requests - 1)  # the tool calls Claude writes, as they go back
    cost, history = 0.0, case
    for index in range(requests):
        if index == 0:
            read_tokens, write_tokens = prefix, case
        else:
            added = outs[index - 1] + calls + results[index - 1] + reminder
            read_tokens, write_tokens = prefix + history, added
            history += added
        cost += (read_tokens * read_price + write_tokens * write_price + outs[index] * out_price) / 1e6
    return cost


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)


def _span(values: tuple[int, ...]) -> str:
    return f"{values[0]:,} to {values[-1]:,} (median {statistics.median(values):,.0f})"
