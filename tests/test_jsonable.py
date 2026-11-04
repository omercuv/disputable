from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from disputable.engine import Dispute, Stage, Status
from disputable.jsonable import Unloadable, canonical, dump, load
from disputable.ledger import Entry

STORE_TIME = timezone(timedelta(hours=-8))


@dataclass(frozen=True)
class Sample:
    name: str
    count: int
    flag: bool
    amount: Decimal
    at: datetime
    stage: Stage
    tags: frozenset[str] = frozenset()
    entries: tuple[Entry, ...] = ()
    maybe: Decimal | None = None


SAMPLE = Sample("x", 2, True, Decimal("12.30"), datetime(2026, 11, 2, 9, 0, tzinfo=STORE_TIME), Stage.CHARGEBACK,
                frozenset({"b", "a"}), (Entry("Refund", Decimal("12")),), None)


def test_a_dataclass_comes_back_equal():
    assert load(Sample, dump(SAMPLE)) == SAMPLE


def test_the_saved_text_is_canonical():
    text = canonical(SAMPLE)
    assert text == canonical(load(Sample, json.loads(text)))
    assert '"tags":["a","b"]' in text
    assert '"amount":"12.30"' in text
    assert '"at":"2026-11-02T09:00:00-08:00"' in text


def test_nan_survives_a_round_trip():
    dispute = Dispute("d", Stage.RFI, Status.REQUIRES_RESPONSE, Decimal("12"), "USD", "13.1",
                      datetime(2026, 11, 4, tzinfo=timezone.utc), Decimal("12"), refunded_amount=Decimal("NaN"))
    back = load(Dispute, json.loads(canonical(dispute)))
    assert back.refunded_amount.is_nan()
    assert canonical(back) == canonical(dispute)


def test_an_offset_is_kept():
    back = load(Sample, dump(SAMPLE))
    assert back.at.utcoffset() == timedelta(hours=-8)


@pytest.mark.parametrize("field, value", [
    ("stage", "SOMETHING_NEW"),  # Stage itself would read this as UNKNOWN
    ("stage", "rfi"),
    ("amount", 12.3),
    ("amount", "twelve"),
    ("amount", "sNaN"),  # it would raise on the first sum or comparison
    ("count", True),
    ("count", "2"),
    ("flag", 1),
    ("name", None),
    ("at", "2026-11-02T09:00:00"),
    ("at", "Monday"),
    ("tags", ["a", "a"]),
    ("tags", "ab"),
    ("entries", [{"label": "Refund"}]),
])
def test_a_value_that_doesnt_fit_is_refused(field, value):
    with pytest.raises(Unloadable):
        load(Sample, {**dump(SAMPLE), field: value})


def test_a_missing_or_extra_key_is_refused():
    data = dump(SAMPLE)
    with pytest.raises(Unloadable, match="missing"):
        load(Sample, {key: item for key, item in data.items() if key != "count"})
    with pytest.raises(Unloadable, match="not expected"):
        load(Sample, {**data, "extra": 1})


def test_a_naive_time_or_a_float_is_never_saved():
    with pytest.raises(ValueError):
        dump(datetime(2026, 11, 2, 9, 0))
    with pytest.raises(TypeError):
        dump(1.5)


def test_the_place_of_a_bad_value_is_named():
    with pytest.raises(Unloadable, match=r"the record\.entries\[0\]\.amount"):
        load(Sample, {**dump(SAMPLE), "entries": [{"label": "Refund", "amount": 12}]})
