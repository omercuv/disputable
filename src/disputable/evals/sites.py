"""Where the eval reads: the 83 read cases as the app reads them, and the 14 trap controls, in the order a run reads them.

A site is a case of disputable.seed.read_cases.READ with one change: the story's round two, in both endings, is dated from
round one, as lifecycle.as_case() dates it. So its reading key is the one the story's own rounds compute, and a story run
reuses the eval's reading instead of reading live. Re-dated, round two keeps READ's evidence and decision; only the
findings' day counts change. The dispute is always the store's own (Store.dispute()), so no suite case goes through
lifecycle.

A control is a trap's case on a store with each planted line cut from every field of every record, the bank's comment
included. It is used only when code alone concludes and decides there exactly as on the trap's store, so a difference in
a reading can only come from the line. Pure: no reading and no file here.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Mapping, Sequence

from disputable import prompts, rules
from disputable.engine import Dispute, Stage, decide
from disputable.prompts import BANK_COMMENT, StoreProfile
from disputable.reader import ReadRequest, Toolbox, reading_key
from disputable.seed.answers import Answer
from disputable.seed.read_cases import READ
from disputable.store import Case, Store, field_text

STORY = ("earbuds", "headphones", "headphones_round_2", "ending_2")  # the four reads the story makes
ROUND_TWO = ("headphones_round_2", "ending_2")  # dated from round one, as lifecycle.as_case() dates them
FIRST_ROUND = "headphones"  # the story's round one, whose date round two keeps
KINDS = ("customers", "orders", "scans", "emails", "warranty", "cases")  # every kind of record a store holds
CONTROL = "control-"  # a control's label is this and its trap's
CHANGES_CODE = "The control store for {label} changes what code alone decides, so it can't test the planted line."
_TEXTS: dict[int, tuple[Store, tuple[tuple[str, tuple[str, ...]], ...], dict[str, tuple[str, ...]]]] = {}  # by store
_REACH: dict[tuple[int, int], tuple[Store, Case, frozenset[str]]] = {}
_BOXES: dict[int, tuple[Site, Toolbox]] = {}


@dataclass(frozen=True)
class Site:
    label: str  # a READ label, or "control-<label>"
    store_name: str  # "fernhill", "ending_2", "suite", or "control"
    store: Store
    case: Case  # as the app reads it
    dispute: Dispute  # store.dispute(case_id, now)
    now: datetime
    answer: Answer  # a control's has must_ignore=()
    trap: str = ""  # a control: the label of the case it controls


@functools.cache
def sites() -> tuple[Site, ...]:
    """The 83 read cases and the 14 controls, in run order. Made once: every value is frozen."""
    cases = read_sites()
    return ordered((*cases, *controls(cases)))


@functools.cache
def read_sites() -> tuple[Site, ...]:
    """The 83 read cases in READ's order, round two dated from round one."""
    first_day = READ[FIRST_ROUND].case.opened_at
    found = []
    for label, read in READ.items():
        case = replace(read.case, opened_at=first_day) if label in ROUND_TWO else read.case
        found.append(Site(label, read.store_name, read.store, case, read.dispute, read.now, read.answer))
    return tuple(found)


def by_label() -> Mapping[str, Site]:
    return {site.label: site for site in sites()}


def controls(cases: Sequence[Site]) -> tuple[Site, ...]:
    """For each site with planted lines, the same case on a store with every line cut out. ValueError when the cut
    changes what code alone concludes or decides there: then the line isn't only a trap."""
    found = []
    for site in cases:
        if not site.answer.must_ignore:
            continue
        store = site.store
        for line in site.answer.must_ignore:
            store = cut(store, line)
        case = replace(store.case(site.case.id), opened_at=site.case.opened_at)
        dispute = store.dispute(site.case.id, site.now)
        alone, there = rules.evaluate(site.store, site.case, []), rules.evaluate(store, case, [])
        if (alone.evidence, decide(site.dispute, alone.evidence, site.now)) != (
                there.evidence, decide(dispute, there.evidence, site.now)):
            raise ValueError(CHANGES_CODE.format(label=site.label))
        found.append(Site(CONTROL + site.label, "control", store, case, dispute, site.now,
                          replace(site.answer, must_ignore=()), trap=site.label))
    return tuple(found)


def cut(store: Store, line: str) -> Store:
    """The store with a planted line cut from every field of every record that holds it, the bank's comment included."""
    def without(record):
        changes = {item.name: value.replace(line, "") for item in fields(record)
                   if isinstance(value := getattr(record, item.name), str) and line in value}
        return replace(record, **changes) if changes else record
    return replace(store, **{name: tuple(map(without, getattr(store, name))) for name in KINDS})


def ordered(found: Sequence[Site]) -> tuple[Site, ...]:
    """Run order: the story's four first, so its readings exist even if a run stops early; then the other cases, RFI
    and then CHARGEBACK (the tools differ by stage, so each stage's cached prefix is written once), by label; then the
    controls, RFI and then CHARGEBACK, by label."""
    def place(site: Site) -> tuple[int, int, int, str]:
        story = STORY.index(site.label) if site.label in STORY else len(STORY)
        return bool(site.trap), story, site.case.stage is not Stage.RFI, site.label
    return tuple(sorted(found, key=place))


def request_for(site: Site, effort: str = prompts.EFFORT, profile: StoreProfile = prompts.FERNHILL) -> ReadRequest:
    """The read request rounds builds at DECIDE, with the site's case in place of lifecycle.as_case(record)."""
    return ReadRequest(site.store, site.case, site.dispute, reading_key(site.store, site.case, profile=profile,
                                                                        effort=effort))


def holders(store: Store, line: str) -> tuple[str, ...]:
    """The ids of every record with a field that holds the line, in the store's order. Each store's text is read once,
    and each line looked for once: the store is held, so its id can't be reused."""
    known = _TEXTS.get(id(store))
    if known is None or known[0] is not store:
        known = _TEXTS[id(store)] = store, tuple((record.id, tuple(leaf for item in fields(record)
                                                                   for leaf in field_text(record, item.name) or ()))
                                                 for name in KINDS for record in getattr(store, name)), {}
    found = known[2].get(line)
    if found is None:
        found = known[2][line] = tuple(record_id for record_id, leaves in known[1]
                                       if any(line in leaf for leaf in leaves))
    return found


def toolbox(site: Site) -> Toolbox:
    """The site's read tools, made once: the site is held, so its id can't be reused."""
    known = _BOXES.get(id(site))
    if known is None or known[0] is not site:
        known = _BOXES[id(site)] = site, Toolbox.for_case(site.store, site.case)
    return known[1]


def reach(store: Store, case: Case) -> frozenset[str]:
    """The ids of every record the stage's tools can show for the case. Answered once per store and case, both held."""
    known = _REACH.get((id(store), id(case)))
    if known is not None and known[0] is store and known[1] is case:
        return known[2]
    box = Toolbox.for_case(store, case)
    found = {order.id for order in store.get_order_history(case.order_number)}
    for number in box.numbers:
        found |= {record.id for record in (*store.get_carrier_scans(number), *store.search_inbox(number))}
    found |= {entry.id for serial in box.serials.values() for entry in store.get_warranty_log(serial)}
    _REACH[(id(store), id(case))] = store, case, frozenset(found)
    return frozenset(found)


def where(site: Site, line: str) -> str:
    """Where a planted line is for this case: the bank's comment, which the case prompt shows word for word, else the
    first record in the case's reach that holds it, else "" (no tool shows it)."""
    if line in (site.case.issuer_comment or ""):
        return BANK_COMMENT
    reachable = reach(site.store, site.case)
    return next((record_id for record_id in holders(site.store, line) if record_id in reachable), "")
