"""The cases whose records get read, shared by the rules, scripted and walk tests: each one's store, clock and answer
key (disputable.seed.read_cases, which the eval reads too), code alone on it, and one scripted reading of it per reader.
Every value here is made once in a run and then shared, so the files that walk all 83 cases don't make the same reading
twice."""

from __future__ import annotations

import functools

from disputable.engine import decide
from disputable.rules import evaluate
from disputable.seed.read_cases import READ, STORES, Read, read_cases  # re-exported: the tests import them here


@functools.cache
def code_alone(label: str):
    """evaluate() with nothing reported, and decide() on its evidence. Cached: tests that patch a module call evaluate()."""
    read = READ[label]
    conclusion = evaluate(read.store, read.case, [])
    return conclusion, decide(read.dispute, conclusion.evidence, read.now)


# The readers need the SDK, which takes seconds to import here. The rules tests never read, so they never load it.
@functools.cache
def request(label: str):
    """The read request for a case, keyed as the default reader keys it."""
    from disputable.reader import ReadRequest, reading_key
    read = READ[label]
    return ReadRequest(read.store, read.case, read.dispute, reading_key(read.store, read.case))


def read_with(label: str, reader: str = "perfect"):
    """One reading of a case and the client it ran on: by the perfect reader, the empty one, or an attack of
    seed.scripted.ATTACKS. The real ClaudeReader reads it, on the scripted FakeClient. Made once a run, however it's
    asked for: read_with(label) and read_with(label, "perfect") are the same reading."""
    return _read_with(label, reader)


@functools.cache
def _read_with(label: str, reader: str):
    from disputable.reader import ClaudeReader
    from disputable.seed.scripted import FakeClient, empty, fooled, perfect
    read = READ[label]
    if reader == "perfect":
        script = perfect(read.store, read.case, read.answer)
    elif reader == "empty":
        script = empty(read.store, read.case)
    else:
        script = fooled(read.store, read.case, read.answer, reader)
    client = FakeClient(script)
    return ClaudeReader(client).read(request(label)), client
