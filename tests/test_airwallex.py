"""The Airwallex client: logins, the throttle and the retries, the answer-to-outcome map, the exact bodies and their
guard, the upload, the ids that go into paths, and the fake sandbox the loop's tests run on. Offline: httpx.MockTransport.
"""

from __future__ import annotations

import ast
import base64
import functools
import hashlib
import importlib.util
import ipaddress
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from disputable import airwallex, lifecycle, privacy
from disputable.airwallex import (DISPUTES, LOGIN, MAX_DETAIL, PAYMENTS, TOKEN_REUSE_SECONDS, UPLOAD, AgentClient,
                                  Answer, BadId, Credentials, Outcome, PostRequest, Sent, Session, accept_body,
                                  body_problem, body_text, challenge_body, json_number, not_before, outcome_of, problem,
                                  refusal, respond_path)
from disputable.config import Settings, SetupError
from disputable.documents import MAX_PDF_BYTES, Document
from disputable.engine import Action, Evidence, Stage, dispute_from_api
from disputable.fees import EvidenceClass, money
from disputable.lifecycle import ACTOR, approve, keep, request_id_for, send_failed, sending
from disputable.seed.fernhill import (ENDING_2, FERNHILL, FIRST_PACKAGE, FIRST_PACKAGE_FILE, MONDAY, SECOND_PACKAGE,
                                      SECOND_PACKAGE_FILE, THREE_WEEKS_LATER)
from disputable.seed.suite import SUITE
from fake_airwallex import (ACCOUNT_ID, API_KEY, CLIENT_ID, NOT_SUPPORTED, PROFILE, SIMULATION, FakeAirwallex,
                            credentials, file_id)
from sandbox import MINUTE, Sandbox, Story

NO_READ = Evidence(EvidenceClass.NONE, conflicts=(
    "Claude couldn't read this case's records (this run doesn't use Claude), so a person reads them before anything is "
    "sent.",))
EAR_TIPS = f"{DISPUTES}/dst_ear_tips"
FIRST_ID, SECOND_ID = file_id("evidence-64631fdcbf12d048.pdf", 1), file_id("evidence-2d8e9d0a0b30c647.pdf", 2)


class Time:
    """A monotonic clock that only sleeping moves, and every sleep it was asked for."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def fake_with(*case_ids, store=FERNHILL):
    fake = FakeAirwallex(Sandbox(store))
    for case_id in case_ids:
        fake.sandbox.open(case_id)
    return fake


def timed(fake, time=None):
    time = time or Time()
    return AgentClient(fake.session(monotonic=time.monotonic, sleep=time.sleep)), time


# ----------------------------------------------------------------------------- the story's plans
@functools.cache
def plans():
    """The plans of the story's rounds, as code made them: name -> lifecycle.Plan."""
    monday = Story()
    monday.poll(MONDAY)
    monday.decide(MONDAY)
    found = {name: monday[name].rounds[-1].plan for name in ("ear_tips", "earbuds", "headphones")}
    for store, name in ((FERNHILL, "warranty"), (ENDING_2, "no_new_record")):
        story = Story(store, cases=("headphones",))
        story.run(MONDAY)
        story.sandbox.escalate("dst_headphones")
        story.poll(THREE_WEEKS_LATER)
        story.decide(THREE_WEEKS_LATER)
        found[name] = story["headphones"].rounds[-1].plan
    return found


def with_terms(plan, **changes):
    """A copy of the plan on other terms, with the request id those terms give."""
    terms = replace(plan.terms, **changes)
    return replace(plan, terms=terms, request_id=request_id_for(terms))


def post_for(plan, files=(), round_number=1):
    body = accept_body(plan) if plan.terms.action is Action.ACCEPT else challenge_body(plan, files)
    assert body_problem(plan, body, files) is None
    return PostRequest(plan.request_id, plan.terms.dispute_id, round_number, respond_path(plan), body_text(body))


# ----------------------------------------------------------------------------- the session
def test_token_reuse_and_relogin():
    fake = fake_with("ear_tips")
    client, time = timed(fake)
    for _ in range(5):
        assert client.dispute("dst_ear_tips").ok
    assert fake.logins == 1 and fake.seen().count(LOGIN) == 1
    time.now += TOKEN_REUSE_SECONDS - 5
    assert client.dispute("dst_ear_tips").ok and fake.logins == 1
    time.now += 10  # 25 minutes after the login: a new one, though the token would last 30
    assert client.dispute("dst_ear_tips").ok and fake.logins == 2
    # A 401 logs in again once and sends the same request once more.
    fake.expire_tokens()
    before = len(fake.calls)
    assert client.dispute("dst_ear_tips").ok and fake.logins == 3
    assert fake.seen()[before:] == [EAR_TIPS, LOGIN, EAR_TIPS]
    # A second 401 after the new login is the answer: no loop.
    fake.fail("GET", EAR_TIPS, status=401, times=2)
    before = len(fake.calls)
    assert client.dispute("dst_ear_tips").status == 401 and fake.seen()[before:] == [EAR_TIPS, LOGIN, EAR_TIPS]
    # A POST too: a 401 means it wasn't processed, so the same bytes go once more and Airwallex applies them once.
    fake.expire_tokens()
    post = post_for(plans()["ear_tips"])
    before = len(fake.calls)
    assert client.respond(post, retry=False).outcome is Outcome.TAKEN
    assert fake.seen()[before:] == [f"{EAR_TIPS}/accept", LOGIN, f"{EAR_TIPS}/accept"]
    assert {request.content for request in fake.calls[before:] if request.url.path.endswith("/accept")} == {
        post.body.encode("ascii")}
    assert len(fake.sandbox.disputes["dst_ear_tips"]["accept_details"]) == 1


def test_only_the_login_carries_the_keys_and_the_token_stays_in_memory():
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    answer = client.dispute("dst_ear_tips")
    login, read = fake.calls
    assert (login.headers["x-client-id"], login.headers["x-api-key"]) == (CLIENT_ID, API_KEY)
    assert "authorization" not in login.headers and login.content == b""
    assert "x-api-key" not in read.headers and "x-client-id" not in read.headers
    token = read.headers["authorization"].removeprefix("Bearer ")
    assert token in fake.tokens and token not in repr(answer) and token not in repr(client.__dict__)
    assert CLIENT_ID not in repr(credentials()) and API_KEY not in repr(credentials())


def test_a_failed_login_sends_nothing_and_is_retried_by_the_next_pass():
    fake = fake_with("ear_tips")
    wrong = replace(credentials(), api_key="fake-api-key-wrong")
    client = AgentClient(Session(wrong, actor="agent", transport=fake.transport(), sleep=lambda seconds: None))
    answer = client.dispute("dst_ear_tips")
    assert answer.login_failed and answer.status == 401 and not answer.ok
    assert fake.seen() == [LOGIN]  # the read never went out
    assert problem(answer) == ("this app couldn't log in to Airwallex (HTTP 401, credentials_invalid: The client id or "
                               "the API key is wrong)")
    sent = client.respond(post_for(plans()["ear_tips"]), retry=False)
    assert sent.outcome is Outcome.RETRY and sent.detail == problem(answer)
    assert "/accept" not in "".join(fake.seen())


def test_throttle():
    fake = fake_with("ear_tips")
    client, time = timed(fake)
    for _ in range(3):
        client.dispute("dst_ear_tips")
    assert time.slept == [0.25, 0.25, 0.25]  # the login, then each GET 0.25 s after the call before it
    time.now += 0.1
    client.dispute("dst_ear_tips")
    assert time.slept[-1] == pytest.approx(0.15)
    time.now += 5
    client.dispute("dst_ear_tips")
    assert len(time.slept) == 4  # a call long after the last one doesn't wait


@pytest.mark.parametrize("fault, wait", [
    ({"status": 429, "retry_after": 3}, 3.0),
    ({"status": 429, "retry_after": 99}, 30.0),  # capped
    ({"status": 429, "retry_after": "Mon, 02 Nov 2026 17:00:07 GMT", "headers": {"Date": "Mon, 02 Nov 2026 17:00:00 GMT"}},
     7.0),  # an HTTP date, read against the answer's own Date
    ({"status": 429, "retry_after": "soon"}, 2.0),
    ({"status": 429}, 2.0),
    ({"status": 503}, 2.0),
    ({"status": 500}, 2.0),
    ({"status": 408}, 2.0),
    ({"timeout": True}, 2.0),
    ({"connect": True}, 2.0),
])
def test_a_get_is_retried_once(fault, wait):
    fake = fake_with("ear_tips")
    client, time = timed(fake)
    client.dispute("dst_ear_tips")
    fake.fail("GET", EAR_TIPS, **fault)
    time.slept.clear()
    assert client.dispute("dst_ear_tips").ok
    assert fake.seen()[-2:] == [EAR_TIPS, EAR_TIPS] and time.slept[-1] == wait
    fake.fail("GET", EAR_TIPS, times=2, **fault)
    second = client.dispute("dst_ear_tips")  # one retry only: the second failure is the answer
    assert not second.ok and fake.seen()[-2:] == [EAR_TIPS, EAR_TIPS]


@pytest.mark.parametrize("fault", [{"status": 404}, {"status": 400}, {"status": 403}])
def test_a_get_that_was_refused_is_not_retried(fault):
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    fake.fail("GET", EAR_TIPS, **fault)
    assert client.dispute("dst_ear_tips").status == fault["status"] and fake.seen().count(EAR_TIPS) == 1


@pytest.mark.parametrize("fault", [{"status": 503}, {"status": 429, "retry_after": 1}, {"timeout": True},
                                   {"connect": True}, {"status": 408}])
def test_a_post_is_never_retried_inside_the_client(fault):
    fake = fake_with("ear_tips", "headphones")
    client, _ = timed(fake)
    post = post_for(plans()["ear_tips"])
    fake.fail("POST", post.path, **fault)
    sent = client.respond(post, retry=False)
    assert fake.seen("POST").count(post.path) == 1
    assert sent.outcome is (Outcome.FAILED if fault.get("status") == 408 else Outcome.RETRY)
    fake.fail("POST", UPLOAD, status=503)
    assert client.upload(FIRST_PACKAGE).file_id is None and fake.seen("POST").count(UPLOAD) == 1


# ----------------------------------------------------------------------------- answers to outcomes
def answered(status, code="", message="", retry_after=None):
    data = {"code": code, "message": message} if code or message else ({"id": "dst_x"} if status < 300 else None)
    return Answer(status, data, "", "", 5, retry_after)


def no_answer(error):
    return Answer(None, None, "", error, 30_000, None)


@pytest.mark.parametrize("answer, outcome, detail", [
    (answered(200), Outcome.TAKEN, "Airwallex took it"),
    (answered(201), Outcome.TAKEN, "Airwallex took it"),
    (answered(400, "duplicate_request", "The request_id was used."), Outcome.TAKEN, "Airwallex already had this request"),
    (answered(409, "duplicate_request_id"), Outcome.TAKEN, "Airwallex already had this request"),
    (answered(400, "validation_error", NOT_SUPPORTED), Outcome.FAILED,
     "HTTP 400, validation_error: Dispute transition is not supported"),
    (answered(400, "validation_error", "Invalid accept_reason NOT_A_REAL_REASON"), Outcome.FAILED,
     "HTTP 400, validation_error: Invalid accept_reason NOT_A_REAL_REASON"),
    (answered(401, "unauthorized"), Outcome.FAILED, "HTTP 401, unauthorized"),  # after the one new login
    (answered(403, message="Forbidden."), Outcome.FAILED, "HTTP 403: Forbidden"),
    (answered(404, "resource_not_found"), Outcome.FAILED, "HTTP 404, resource_not_found"),
    (answered(408), Outcome.FAILED, "HTTP 408"),  # a 4xx: the state machine's rule (S3)
    (answered(409, "conflict"), Outcome.FAILED, "HTTP 409, conflict"),
    (answered(429, retry_after=5.0), Outcome.RETRY, "Airwallex asked this app to wait"),
    (answered(500), Outcome.RETRY, "HTTP 500"),
    (answered(503), Outcome.RETRY, "HTTP 503"),
    (answered(302), Outcome.RETRY, "HTTP 302"),
    (answered(101), Outcome.RETRY, "HTTP 101"),
    (no_answer("ReadTimeout"), Outcome.RETRY, "no answer in 30 seconds"),
    (no_answer("WriteTimeout"), Outcome.RETRY, "no answer in 30 seconds"),
    (no_answer("PoolTimeout"), Outcome.RETRY, "no answer in 30 seconds"),
    (no_answer("ConnectError"), Outcome.RETRY, "the connection failed"),
    (no_answer("ConnectTimeout"), Outcome.RETRY, "the connection failed"),
    (no_answer("RemoteProtocolError"), Outcome.RETRY, "the connection broke before Airwallex answered"),
    # The login the POST needs failed, so the POST never went out: a 429 there waits as long as on the POST.
    (replace(answered(429, retry_after=5.0), login_failed=True), Outcome.RETRY,
     "this app couldn't log in to Airwallex (Airwallex asked this app to wait)"),
    (replace(answered(401, "credentials_invalid", "The client id or the API key is wrong."), login_failed=True),
     Outcome.RETRY, "this app couldn't log in to Airwallex (HTTP 401, credentials_invalid: The client id or the API key is "
                    "wrong)"),
    (replace(answered(503), login_failed=True), Outcome.RETRY, "this app couldn't log in to Airwallex (HTTP 503)"),
])
def test_outcomes(answer, outcome, detail):
    sent = outcome_of(answer, retry=False)
    assert (sent.outcome, sent.status, sent.detail) == (outcome, answer.status, detail)
    assert sent.retry_after == (5.0 if answer.status == 429 else None)
    assert not sent.detail.endswith(".") and len(sent.detail) <= MAX_DETAIL
    if outcome is not Outcome.FAILED:  # only a refusal reads differently on a retry
        assert outcome_of(answer, retry=True) == sent


def test_the_retry_wording():
    first = outcome_of(answered(400, "validation_error", NOT_SUPPORTED), retry=False)
    again = outcome_of(answered(400, "validation_error", NOT_SUPPORTED), retry=True)
    assert again.outcome is Outcome.FAILED
    assert again.detail == ("Airwallex refused the retry: HTTP 400, validation_error: Dispute transition is not "
                            "supported. The first try may have landed")
    assert refusal(first.detail) == "HTTP 400, validation_error: Dispute transition is not supported."
    assert refusal(again.detail) == ("HTTP 400, validation_error: Dispute transition is not supported. That was a retry, "
                                     "and the first try may have landed. A person checks what Airwallex has.")
    # What Priya reads once send_failed() takes it: "refused" once, and Airwallex's words as they came.
    record = being_sent()
    plan = record.rounds[-1].plan
    person = send_failed(record, plan.request_id, refusal(again.detail), MONDAY + 2 * MINUTE).rounds[-1].person
    assert person == ("Airwallex refused the response: HTTP 400, validation_error: Dispute transition is not supported. "
                      "That was a retry, and the first try may have landed. A person checks what Airwallex has. A person "
                      "handles this round.")
    assert person.count("refused") == 1
    long = outcome_of(answered(400, "validation_error", "word " * 100), retry=True)
    assert len(long.detail) <= MAX_DETAIL and long.detail.endswith(". The first try may have landed")
    assert refusal(long.detail).endswith(". That was a retry, and the first try may have landed. A person checks what "
                                         "Airwallex has.") and "refused" not in refusal(long.detail)


def being_sent():
    story = Story(cases=("ear_tips",))
    story.poll(MONDAY)
    story.decide(MONDAY)
    story.poll(MONDAY + MINUTE)
    return sending(story["ear_tips"], MONDAY + MINUTE)


def test_airwallexs_words_become_one_plain_line_with_no_file_id():
    hidden = FIRST_ID
    message = f"Bad\nrequest:\tfile {hidden} isn't\u0000 here \ud800 and {'x' * 300}."
    sent = outcome_of(answered(400, "validation_error", message), retry=False)
    assert "\n" not in sent.detail and "\t" not in sent.detail and "\u0000" not in sent.detail
    assert hidden not in sent.detail and "(a file id)" in sent.detail and "\ufffd" in sent.detail
    assert sent.detail.startswith("HTTP 400, validation_error: Bad request: file (a file id) isn't here")
    assert len(sent.detail) <= MAX_DETAIL and not sent.detail.endswith(".")
    assert not [token for token in privacy.base64_tokens(sent.detail)
                if privacy.SEPARATOR in (privacy.decode_base64(token) or "")]
    plain = "QUJD" * 20  # a long base64 run that isn't a file id stays as it is
    assert plain in outcome_of(answered(400, "x", plain), retry=False).detail
    # An account id written plainly, or any other UUID, never reaches a try, a person line or a page.
    named = f"Account {ACCOUNT_ID} can't respond to dispute dst_x (request {ACCOUNT_ID.upper()})."
    for said in (outcome_of(answered(400, "forbidden", named), retry=False).detail,
                 outcome_of(answered(400, "forbidden", named), retry=True).detail, problem(answered(404, "x", named))):
        assert ACCOUNT_ID not in said.lower() and not privacy.UUID.search(said)
        assert "Account (an id) can't respond to dispute dst_x (request (an id))" in said


def test_only_a_file_id_or_a_uuid_in_airwallexs_words_is_hidden():
    """problem()'s rule: a file id or a UUID shows as "(a file id)" or "(an id)"; other ids, such as dispute and payment
    ids, stay as they came."""
    message = (f"Dispute dst_hkdmr7ftsg8a0b1c2d3 on payment int_hkdmr7ftsg9z8y7x6w5 can't move. Request {ACCOUNT_ID} was "
               f"refused.")
    words = ("HTTP 400, validation_error: Dispute dst_hkdmr7ftsg8a0b1c2d3 on payment int_hkdmr7ftsg9z8y7x6w5 can't move. "
             "Request (an id) was refused")
    assert problem(answered(400, "validation_error", message)) == words
    assert outcome_of(answered(400, "validation_error", message), retry=True).detail == (
        f"Airwallex refused the retry: {words}. The first try may have landed")


def test_not_before_follows_retry_after_only():
    at = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
    assert not_before(at, Sent(Outcome.RETRY, 429, "Airwallex asked this app to wait", 12.5)) == at + timedelta(
        seconds=12.5)
    assert not_before(at, Sent(Outcome.RETRY, 503, "HTTP 503", None)) is None
    assert not_before(at, Sent(Outcome.TAKEN, 200, "Airwallex took it", None)) is None
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    post = post_for(plans()["ear_tips"])
    date = "Mon, 02 Nov 2026 17:00:00 GMT"  # the answer's own Date: an HTTP date is read against it
    for header, answered_on, seconds in (  # Retry-After as Airwallex might send it. A 429 always sets a wait.
            (7, None, 7.0), (0.5, None, 0.5), (99, None, 30.0), (0, None, 0.0),
            ("Mon, 02 Nov 2026 17:00:12 GMT", date, 12.0), ("Mon, 02 Nov 2026 17:05:00 GMT", date, 30.0),
            ("Mon, 02 Nov 2026 16:59:00 GMT", date, 0.0),  # already past: it may go now
            ("Mon, 02 Nov 2026 17:00:12 GMT", None, 30.0),  # no Date to read it against
            ("-1", None, 30.0), ("nan", None, 30.0), ("soon", None, 30.0),  # none this app can read
            (None, None, 30.0)):  # none at all: the longest wait this app keeps
        fake.fail("POST", post.path, status=429, retry_after=header, headers={"Date": answered_on} if answered_on else None)
        sent = client.respond(post, retry=False)
        assert (sent.outcome, sent.detail, sent.retry_after) == (Outcome.RETRY, "Airwallex asked this app to wait", seconds)
        assert not_before(at, sent) == at + timedelta(seconds=seconds)
    assert fake.sandbox.disputes["dst_ear_tips"]["accept_details"] == []


def test_a_429_on_the_login_a_post_needs_sets_the_same_wait():
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    post = post_for(plans()["ear_tips"])
    at = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
    for header, seconds in ((7, 7.0), (99, 30.0), ("soon", 30.0), (None, 30.0)):  # as on the POST's own 429
        fake.fail("POST", LOGIN, status=429, retry_after=header, times=2)  # the login, and its one retry
        sent = client.respond(post, retry=False)
        assert (sent.outcome, sent.status, sent.retry_after) == (Outcome.RETRY, 429, seconds)
        assert sent.detail == "this app couldn't log in to Airwallex (Airwallex asked this app to wait)"
        assert not_before(at, sent) == at + timedelta(seconds=seconds)
    assert "/accept" not in "".join(fake.seen()) and fake.sandbox.disputes["dst_ear_tips"]["accept_details"] == []
    fake.fail("POST", LOGIN, status=503, times=2)  # any other failed login: no clear answer, and no wait
    assert not_before(at, client.respond(post, retry=False)) is None


# ----------------------------------------------------------------------------- the bodies, exactly as the plan says
def test_accept_bodies():
    ear_tips, earbuds, ending_2 = plans()["ear_tips"], plans()["earbuds"], plans()["no_new_record"]
    assert body_text(accept_body(ear_tips)) == (
        '{"accepted_by":"disputable","reason":"LOW_VALUE_TRANSACTION","refund":{"amount":12,"reason":'
        f'"REQUESTED_BY_CUSTOMER"}},"request_id":"{ear_tips.request_id}"}}')
    assert body_text(accept_body(earbuds)) == (
        f'{{"accepted_by":"disputable","reason":"VALID_CUSTOMER_DISPUTE","request_id":"{earbuds.request_id}"}}')
    assert body_text(accept_body(ending_2)) == (
        f'{{"accepted_by":"disputable","reason":"OTHERS","request_id":"{ending_2.request_id}"}}')
    cents = with_terms(ear_tips, amount=Decimal("15.71"), refund_amount=Decimal("15.71"))
    text = body_text(accept_body(cents))
    assert '"refund":{"amount":15.71,"reason":"REQUESTED_BY_CUSTOMER"}' in text
    assert money(json.loads(text)["refund"]["amount"]) == Decimal("15.71")
    assert body_problem(cents, accept_body(cents), ()) is None
    for plan in (ear_tips, earbuds, ending_2):
        assert body_problem(plan, accept_body(plan), ()) is None
    with pytest.raises(ValueError, match="builds an accept"):
        accept_body(plans()["headphones"])


def test_challenge_bodies():
    rfi, chargeback = plans()["headphones"], plans()["warranty"]
    assert (rfi.terms.stage, rfi.terms.file_hashes) == (Stage.RFI, frozenset({FIRST_PACKAGE_FILE}))
    assert body_text(challenge_body(rfi, [(FIRST_PACKAGE, FIRST_ID)])) == (
        '{"challenge_method":"STANDARD","challenged_by":"disputable","reason":"PURCHASE_HISTORY",'
        f'"request_id":"{rfi.request_id}","supporting_documents":{{"documents":[{{"description":"Earlier purchases on '
        f'the same card as order #1109","file_ids":["{FIRST_ID}"],"type":"PRIMARY"}}]}}}}')
    assert (chargeback.terms.stage, chargeback.terms.file_hashes) == (Stage.CHARGEBACK, frozenset({SECOND_PACKAGE_FILE}))
    assert body_text(challenge_body(chargeback, [(SECOND_PACKAGE, SECOND_ID)])) == (
        '{"challenge_method":"STANDARD","challenged_by":"disputable","product_type":"PHYSICAL_GOODS",'
        f'"reason":"PRODUCT_RECEIVED","request_id":"{chargeback.request_id}","supporting_documents":{{"documents":['
        '{"description":"Warranty registration of serial FH-HP-03-55821 from order #1109",'
        f'"file_ids":["{SECOND_ID}"],"type":"PRIMARY"}}]}}}}')
    for plan, files in ((rfi, [(FIRST_PACKAGE, FIRST_ID)]), (chargeback, [(SECOND_PACKAGE, SECOND_ID)])):
        assert not set(challenge_body(plan, files)) & {"evidence", "customer_info", "delivery_info", "order_info",
                                                       "seller_info", "product_description"}
    # Two files go in the order of their text hashes, whatever order they come in.
    both = with_terms(chargeback, file_hashes=frozenset({FIRST_PACKAGE_FILE, SECOND_PACKAGE_FILE}))
    files = [(SECOND_PACKAGE, SECOND_ID), (FIRST_PACKAGE, FIRST_ID)]
    assert challenge_body(both, files) == challenge_body(both, files[::-1])
    entries = challenge_body(both, files)["supporting_documents"]["documents"]
    assert SECOND_PACKAGE_FILE < FIRST_PACKAGE_FILE
    assert [entry["file_ids"] for entry in entries] == [[SECOND_ID], [FIRST_ID]]
    assert [entry["description"] for entry in entries] == [SECOND_PACKAGE.description, FIRST_PACKAGE.description]
    with pytest.raises(ValueError, match="builds a challenge"):
        challenge_body(plans()["ear_tips"], [])


def test_the_actor_is_disputable():
    for name, plan in plans().items():
        files = {"headphones": [(FIRST_PACKAGE, FIRST_ID)], "warranty": [(SECOND_PACKAGE, SECOND_ID)]}.get(name, [])
        body = accept_body(plan) if plan.terms.action is Action.ACCEPT else challenge_body(plan, files)
        assert body.get("accepted_by", body.get("challenged_by")) == ACTOR == "disputable", name
        assert body_problem(plan, body, files) is None, name
        for who in ("accepted_by", "challenged_by"):
            if who in body:
                assert body_problem(plan, {**body, who: "disputable-spike"}, files) == airwallex.NOT_THIS_APP


def test_body_problem():
    rfi, ear_tips, earbuds = plans()["headphones"], plans()["ear_tips"], plans()["earbuds"]
    files = [(FIRST_PACKAGE, FIRST_ID)]
    good = challenge_body(rfi, files)

    def challenge(**changes):
        return body_problem(rfi, {**good, **changes}, files)

    assert body_problem(rfi, good, files) is None
    assert challenge(reason="NOT_A_REAL_REASON") == airwallex.REASON
    unknown = with_terms(rfi, reason="NOT_A_REAL_REASON")  # the API would store it as OTHER_REASONS with a 200
    assert body_problem(unknown, challenge_body(unknown, files), files) == airwallex.REASON
    assert challenge(request_id=earbuds.request_id) == airwallex.REQUEST_ID
    assert challenge(evidence={"previous_purchase_similar_product_file_ids": [FIRST_ID]}) == airwallex.EXTRA_FIELD
    assert challenge(customer_info={}) == airwallex.EXTRA_FIELD
    assert challenge(challenged_by="someone") == airwallex.NOT_THIS_APP
    assert challenge(challenge_method="AI_DISPUTE_AUTOMATION") == airwallex.METHOD
    assert challenge(product_type="PHYSICAL_GOODS") == airwallex.PRODUCT  # only at CHARGEBACK
    chargeback = plans()["warranty"]
    second = [(SECOND_PACKAGE, SECOND_ID)]
    without = {key: value for key, value in challenge_body(chargeback, second).items() if key != "product_type"}
    assert body_problem(chargeback, without, second) == airwallex.PRODUCT
    # The files: none missing, none extra, none in the plan's place, one PRIMARY entry and one file id each.
    assert body_problem(rfi, challenge_body(rfi, []), []) == airwallex.FILES
    extra = [*files, (SECOND_PACKAGE, SECOND_ID)]
    assert body_problem(rfi, challenge_body(rfi, extra), extra) == airwallex.FILES
    assert body_problem(rfi, challenge_body(rfi, second), second) == airwallex.FILES
    entry = good["supporting_documents"]["documents"][0]
    for broken in ({**entry, "type": "OTHER"}, {**entry, "file_ids": [FIRST_ID, SECOND_ID]}, {**entry, "file_ids": []},
                   {**entry, "description": "Something else"}):
        assert challenge(supporting_documents={"documents": [broken]}) == airwallex.DOCUMENTS
    quoted = [(FIRST_PACKAGE, 'id"with a quote')]
    assert body_problem(rfi, challenge_body(rfi, quoted), quoted) == airwallex.DOCUMENTS
    long_notes = replace(FIRST_PACKAGE)
    object.__setattr__(long_notes, "notes", "n" * 51)  # Document itself refuses such notes; the guard checks again
    assert body_problem(rfi, challenge_body(rfi, [(long_notes, FIRST_ID)]), [(long_notes, FIRST_ID)]) == airwallex.NOTES
    # Two files in another order than their text hashes.
    both = with_terms(rfi, file_hashes=frozenset({FIRST_PACKAGE_FILE, SECOND_PACKAGE_FILE}))
    two = [(FIRST_PACKAGE, FIRST_ID), (SECOND_PACKAGE, SECOND_ID)]
    body = challenge_body(both, two)
    assert body_problem(both, body, two) is None
    flipped = {**body, "supporting_documents": {"documents": body["supporting_documents"]["documents"][::-1]}}
    assert body_problem(both, flipped, two) == airwallex.DOCUMENTS
    same_id = [(FIRST_PACKAGE, FIRST_ID), (SECOND_PACKAGE, FIRST_ID)]
    assert body_problem(both, challenge_body(both, same_id), same_id) == airwallex.DOCUMENTS
    # The refund: exactly when the plan refunds, only at RFI, with its exact amount.
    accept = accept_body(ear_tips)
    for refund in ({"amount": 11, "reason": "REQUESTED_BY_CUSTOMER"}, {"amount": "12", "reason": "REQUESTED_BY_CUSTOMER"},
                   {"amount": True, "reason": "REQUESTED_BY_CUSTOMER"}, {"amount": 12}, {"amount": 12, "reason": "X"},
                   {"amount": 12, "reason": "REQUESTED_BY_CUSTOMER", "description": "x"}, None):
        assert body_problem(ear_tips, {**accept, "refund": refund}, ()) == airwallex.REFUND
    assert body_problem(ear_tips, {key: value for key, value in accept.items() if key != "refund"}, ()) == airwallex.REFUND
    assert body_problem(earbuds, {**accept_body(earbuds), "refund": accept["refund"]}, ()) == airwallex.REFUND
    late = with_terms(earbuds, refund_amount=Decimal("189"))  # a refund at CHARGEBACK would pay the customer twice
    assert body_problem(late, accept_body(late), ()) == airwallex.REFUND_STAGE
    assert body_problem(ear_tips, {**accept, "description": "Low value."}, ()) == airwallex.EXTRA_FIELD
    assert body_problem(ear_tips, accept, files) == airwallex.ACCEPT_FILES
    assert body_problem(ear_tips, [accept], ()) == airwallex.NOT_EXACT
    assert body_problem(with_terms(ear_tips, dispute_id="dst/../x"), accept, ()) == airwallex.DISPUTE_ID
    assert body_problem(with_terms(ear_tips, action=Action.HAND_OFF), accept, ()) == airwallex.NOT_A_RESPONSE


@pytest.mark.parametrize("amount, number", [
    ("12", 12), ("12.00", 12), ("1249", 1249), ("1E+2", 100), ("0", 0), ("15.71", 15.71), ("0.1", 0.1), ("-3.5", -3.5),
    ("1778.30", 1778.3),
])
def test_json_numbers(amount, number):
    value = json_number(Decimal(amount))
    assert value == number and type(value) is type(number)
    assert money(json.loads(json.dumps(value))) == Decimal(amount)  # back exactly, as engine.money() reads it


def test_json_numbers_refuse_what_isnt_exact():
    for amount in ("NaN", "Infinity", "0.12345678901234567890", "12345678901234567.5"):
        with pytest.raises(ValueError):
            json_number(Decimal(amount))
    for amount in (12.5, 12, "12"):
        with pytest.raises(TypeError):
            json_number(amount)
    assert json.dumps({"amount": json_number(Decimal("15.71"))}) == '{"amount": 15.71}'


# ----------------------------------------------------------------------------- the saved request and its POST
def test_respond_sends_the_saved_bytes_and_the_fake_dedups_them():
    fake = fake_with("ear_tips", "earbuds")
    client, _ = timed(fake)
    post = post_for(plans()["ear_tips"])
    sent = client.respond(post, retry=False)
    assert sent == Sent(Outcome.TAKEN, 200, "Airwallex took it", None)
    request = fake.calls[-1]
    assert request.content == post.body.encode("ascii") and request.headers["content-type"] == "application/json"
    dispute = fake.sandbox.disputes["dst_ear_tips"]
    assert dispute["status"] == "ACCEPTED" and dispute["accept_details"][-1]["accepted_by"] == "disputable"
    assert fake.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 12
    # The same request id again: duplicate_request, which the map takes as landed.
    assert client.respond(post, retry=True) == Sent(Outcome.TAKEN, 400, "Airwallex already had this request", None)
    assert len(dispute["accept_details"]) == 1
    # Another request on a dispute that no longer asks: refused.
    other = replace(post, request_id="other-request", body=post.body.replace(post.request_id, "other-request"))
    refused = client.respond(other, retry=False)
    assert refused.outcome is Outcome.FAILED and refused.detail == f"HTTP 400, validation_error: {NOT_SUPPORTED}"
    # A lost answer: the accept was applied, the answer never came, and the retry gets duplicate_request.
    earbuds = post_for(plans()["earbuds"])
    fake.fail("POST", earbuds.path, lost=True)
    assert client.respond(earbuds, retry=False).detail == "no answer in 30 seconds"
    assert fake.sandbox.disputes["dst_earbuds"]["status"] == "ACCEPTED"
    assert client.respond(earbuds, retry=True).outcome is Outcome.TAKEN


def test_a_saved_request_is_checked_when_it_is_built():
    post = post_for(plans()["ear_tips"])
    for changes, why in (({"path": f"{DISPUTES}/dst_ear_tips/refund"}, "its path"),
                         ({"path": f"{DISPUTES}/dst_earbuds/accept"}, "its path"),
                         ({"dispute_id": "dst/x"}, "dispute id"), ({"round_number": 0}, "round"),
                         ({"request_id": ""}, "request id"), ({"body": "not json"}, "isn't JSON"),
                         ({"body": json.dumps(json.loads(post.body))}, "saved as this app writes"),
                         ({"request_id": "another"}, "saved as this app writes")):
        with pytest.raises(ValueError, match=why):
            replace(post, **changes)
    with pytest.raises(TypeError):
        AgentClient(fake_with().session()).respond(post.body, retry=False)


# ----------------------------------------------------------------------------- the upload
def test_upload():
    fake = fake_with("headphones")
    client, _ = timed(fake)
    uploaded = client.upload(FIRST_PACKAGE)
    request = fake.calls[-1]
    assert request.url.host == "files.sandbox.airwallex.com" and request.url.path == UPLOAD
    assert request.headers["content-type"] == f"multipart/form-data; boundary=disputable-{FIRST_PACKAGE.sha256[:32]}"
    assert f'filename="{FIRST_PACKAGE.filename}"'.encode() in request.content
    assert b'name="notes"\r\n\r\nEarlier purchases, order 1109\r\n' in request.content
    assert FIRST_PACKAGE.pdf() in request.content and b"request_id" not in request.content
    pdf = FIRST_PACKAGE.pdf()
    assert (uploaded.pdf_sha256, uploaded.size) == (hashlib.sha256(pdf).hexdigest(), len(pdf))
    assert uploaded.file_id == next(iter(fake.files))
    assert privacy.account_id_of(uploaded.file_id) == ACCOUNT_ID
    assert fake.files[uploaded.file_id]["filename"] == FIRST_PACKAGE.filename
    # The same document gives the same bytes, so a replay matches the upload by them.
    client.upload(FIRST_PACKAGE)
    assert fake.calls[-1].content == request.content
    # The file id comes from "file_id", else "id"; no id, a refusal or no answer is no upload.
    for body, expected in (({"id": FIRST_ID}, FIRST_ID), ({"file_id": "bad id with spaces"}, None), ({}, None)):
        fake.fail("POST", UPLOAD, status=201, body=body)
        assert client.upload(FIRST_PACKAGE).file_id == expected
    fake.fail("POST", UPLOAD, timeout=True)
    assert client.upload(FIRST_PACKAGE).file_id is None


def test_an_upload_too_big_or_holding_its_boundary_never_goes(monkeypatch):
    fake = fake_with()
    client, _ = timed(fake)
    monkeypatch.setattr(Document, "pdf", lambda self: b"%PDF-1.4 " + b"x" * MAX_PDF_BYTES)
    with pytest.raises(ValueError, match="over the 2,097,152 bytes"):
        client.upload(FIRST_PACKAGE)
    monkeypatch.setattr(Document, "pdf", lambda self: b"%PDF-1.4 disputable-" + self.sha256[:32].encode())
    with pytest.raises(ValueError, match="boundary"):
        client.upload(FIRST_PACKAGE)
    assert fake.calls == []


# ----------------------------------------------------------------------------- ids, paths and who may call what
@pytest.mark.parametrize("bad", ["", "../x", "dst/x", "dst x", "dst?x=1", "dst#x", "x" * 65, "dst%2Fx", "d\u00e9", None, 5])
def test_ids_in_paths(bad):
    fake = fake_with()
    client, _ = timed(fake)
    for call in (client.dispute, client.payment, client.disputes_on):
        with pytest.raises(BadId, match="won't put in a web address"):
            call(bad)
    assert fake.calls == []
    if isinstance(bad, str):
        with pytest.raises(BadId):
            respond_path(with_terms(plans()["ear_tips"], dispute_id=bad))
    assert airwallex.safe_id("dst_sgpvfc6s8hnpdljxs0s") and airwallex.safe_id("x" * 64)


def test_the_agent_never_calls_the_simulator():
    source = Path(airwallex.__file__).read_text(encoding="utf-8")
    assert "simulation" not in source.lower()
    fake = fake_with("ear_tips")
    agent = fake.session("agent")
    for call in (lambda: agent.post_json(f"{SIMULATION}/create", "{}", label="x"),
                 lambda: agent.post_json(f"{PAYMENTS}/create", "{}", label="x"),
                 lambda: agent.post_json(f"{SIMULATION}/dst_ear_tips/escalate", "{}", label="x"),
                 lambda: agent.get(f"{DISPUTES}/dst_ear_tips/related_payment_intents", label="x"),
                 lambda: agent.post_json(f"{DISPUTES}/dst_ear_tips/accept?x=1", "{}", label="x")):
        with pytest.raises(ValueError):
            call()
    assert fake.calls == []
    with pytest.raises(ValueError, match="agent's own session"):
        AgentClient(fake.session("bank"))
    with pytest.raises(ValueError, match="agent's or the bank's"):
        fake.session("issuer")
    # The bank's own session plays the issuer.
    bank = fake.session("bank")
    created = bank.post_json(f"{PAYMENTS}/create", json.dumps({"request_id": "fake-request-1", "amount": 25,
                                                                "currency": "USD", "metadata": {"order_no": "9101"}}),
                             label="payment-create")
    assert created.status == 201 and created.data["status"] == "REQUIRES_PAYMENT_METHOD"


def test_every_request_says_whose_call_it_is_only_to_the_transport():
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    client.dispute("dst_ear_tips")
    client.disputes_on("int_ear_tips")
    login, read, listed = fake.calls
    assert login.extensions["disputable"] == {"actor": "agent", "label": "login"}
    assert read.extensions["disputable"] == {"actor": "agent", "label": "dispute-dst_ear_tips"}
    assert listed.extensions["disputable"] == {"actor": "agent", "label": "list-int_ear_tips"}
    assert str(listed.url) == f"https://api.sandbox.airwallex.com{DISPUTES}?payment_intent_id=int_ear_tips&size=100"
    assert set(read.headers) == {"host", "accept", "accept-encoding", "connection", "user-agent", "authorization"}
    assert not [value for request in fake.calls for value in request.headers.values() if value in ("agent", "bank")]


def test_only_the_sandbox_and_real_keys():
    for wrong in ({"base_url": "https://api.airwallex.com"}, {"files_url": "http://files.sandbox.airwallex.com"},
                  {"base_url": "https://api.sandbox.airwallex.com@evil.example"}):
        with pytest.raises(SetupError, match="sandbox"):
            Session(replace(credentials(), **wrong), actor="agent")
    for empty in ({"client_id": ""}, {"api_key": ""}):
        with pytest.raises(SetupError, match="AWX_CLIENT_ID"):
            Session(replace(credentials(), **empty), actor="agent")
    settings = Settings(CLIENT_ID, API_KEY, None, "https://api.sandbox.airwallex.com",
                        "https://files.sandbox.airwallex.com", Path("unused"))
    assert airwallex.credentials(settings) == credentials()
    assert CLIENT_ID not in repr(airwallex.credentials(settings)) and API_KEY not in repr(credentials())
    with pytest.raises(SetupError, match="AWX_CLIENT_ID"):
        airwallex.credentials(replace(settings, airwallex_api_key=None))


def test_a_body_that_isnt_json_is_kept_as_text():
    fake = fake_with("ear_tips")
    client, _ = timed(fake)
    fake.fail("GET", EAR_TIPS, status=502, body="<html>Bad gateway</html>", times=2)
    answer = client.dispute("dst_ear_tips")
    assert (answer.status, answer.data, answer.text, answer.code) == (502, None, "<html>Bad gateway</html>", "")
    assert problem(answer) == "HTTP 502"


# ----------------------------------------------------------------------------- the fake sandbox and its amounts
def test_the_sandbox_sends_exact_amounts():
    sandbox = Sandbox(SUITE)
    sandbox.open("just_past_the_certain_loss")
    sandbox.open("part_of_the_order_disputed")
    (cents, payment), (part, whole) = sandbox.reads()
    assert cents["amount"] == 15.71 and type(cents["amount"]) is float and json.dumps(cents["amount"]) == "15.71"
    assert dispute_from_api(cents, payment).amount == Decimal("15.71")
    assert payment["amount"] == payment["captured_amount"] == payment["latest_payment_attempt"]["captured_amount"] == 15.71
    assert (part["amount"], whole["amount"], whole["captured_amount"]) == (100, 600, 600)  # the case's part of its order
    story = Story()
    assert [(d["amount"], type(d["amount"])) for d, _ in story.sandbox.reads()] == [(12, int), (189, int), (1249, int)]


def test_the_sandbox_refunds_exact_amounts():
    story = Story(SUITE, cases=("just_past_the_certain_loss",))
    story.poll(MONDAY)
    record = keep(story["just_past_the_certain_loss"], NO_READ, MONDAY, round_number=1)
    assert record.rounds[-1].plan.terms.refund_amount == Decimal("15.71")
    story.records["dst_just_past_the_certain_loss"] = approve(record, record.rounds[-1].plan, "Priya", MONDAY)
    story.poll(MONDAY + MINUTE)
    story.sandbox.post(sending(story["just_past_the_certain_loss"], MONDAY + MINUTE))
    refunded = story.sandbox.payments["int_just_past_the_certain_loss"]["latest_payment_attempt"]["refunded_amount"]
    assert refunded == 15.71 and money(refunded) == Decimal("15.71")
    ear_tips = Story(cases=("ear_tips",))
    ear_tips.run(MONDAY)
    assert ear_tips.sandbox.payments["int_ear_tips"]["latest_payment_attempt"]["refunded_amount"] == 12
    assert lifecycle.cost(ear_tips["ear_tips"]) == Decimal("12.30")


def test_a_challenge_on_the_fake_gets_what_airwallex_adds():
    fake = fake_with("headphones")
    client, _ = timed(fake)
    uploaded = client.upload(FIRST_PACKAGE)
    plan = plans()["headphones"]
    sent = client.respond(post_for(plan, [(FIRST_PACKAGE, uploaded.file_id)]), retry=False)
    assert sent.outcome is Outcome.TAKEN
    answer = fake.calls[-1]
    assert json.loads(answer.content)["supporting_documents"]["documents"][0]["file_ids"] == [uploaded.file_id]
    dispute = fake.sandbox.disputes["dst_headphones"]
    entry = dispute["challenge_details"][-1]
    assert (entry["challenged_by"], entry["reason"], dispute["status"]) == ("disputable", "PURCHASE_HISTORY", "CHALLENGED")
    assert entry["seller_info"] == PROFILE and ACCOUNT_ID in entry["seller_info"]["store_url"]
    generated = entry["supporting_documents"]["generated_files"]
    assert len(generated) == 1 and privacy.account_id_of(generated[0]) == ACCOUNT_ID
    assert entry["supporting_documents"]["documents"][-1] == {"type": "OTHER", "file_ids": [uploaded.file_id]}
    # The order's number, currency and date from the payment, as the sandbox filled them (fixtures/spike/066, 073).
    payment = fake.sandbox.payments["int_headphones"]
    assert entry["order_info"] == {"id": "FH-1109-fake", "total_currency": "USD", "created_at": payment["created_at"]}
    assert ACCOUNT_ID != privacy.ACCOUNT_ID_STAND_IN  # so a recording can be seen to hide it
    decoded = base64.b64decode(uploaded.file_id + "=" * (-len(uploaded.file_id) % 4)).decode()
    assert decoded.startswith(f"{ACCOUNT_ID},|,sg,|,{FIRST_PACKAGE.filename}_")
    # A payment the bank made carries its own merchant order id, and that is the number Airwallex shows.
    bank = fake.session("bank")
    made = bank.post_json(f"{PAYMENTS}/create", json.dumps({"request_id": "fake-pay-2", "amount": 25, "currency": "USD",
                                                            "merchant_order_id": "FH-9101-fake-0001",
                                                            "metadata": {"order_no": "9101"}}), label="payment-create")
    assert fake._order_info(fake.sandbox.payments[made.data["id"]])["id"] == "FH-9101-fake-0001"


def test_the_fake_sends_what_a_recording_must_hide():
    """The files host's cookie and Cloudflare ray on an upload's answer, and the paying browser's device data on a
    payment, as the sandbox sends them (fixtures/spike/031, 039, 064 and 003, 005, 007). All made up here, so the
    recorder's redaction is tested against them."""
    fake = fake_with("headphones")
    answers = []

    def watch(request):
        answer = fake.handle(request)
        answers.append((request.url.path, answer))
        return answer

    client = AgentClient(Session(credentials(), actor="agent", transport=httpx.MockTransport(watch),
                                 sleep=lambda seconds: None))
    assert client.upload(FIRST_PACKAGE).file_id is not None and client.payment("int_headphones").ok
    (upload,) = [answer for path, answer in answers if path == UPLOAD]
    assert upload.headers["cf-ray"].endswith("-XYZ") and upload.headers["set-cookie"].startswith("__cf_bm=fake-")
    (paid,) = [answer for path, answer in answers if path == f"{PAYMENTS}/int_headphones"]
    device = paid.json()["latest_payment_attempt"]["device_data"]
    assert ipaddress.ip_address(device["ip_address"]) in ipaddress.ip_network("203.0.113.0/24")  # made up: TEST-NET-3
    assert device["device_id"] and "device_data" not in json.dumps(fake.sandbox.payments)  # only the answer carries it
    assert "cf-ray" not in paid.headers and "set-cookie" not in paid.headers  # as the API host's answers in the spike


def test_the_fake_stores_an_unknown_challenge_reason_and_refuses_an_unknown_accept_reason():
    fake = fake_with("ear_tips", "headphones")
    agent = fake.session("agent")
    accept = json.dumps({"request_id": "fake-1", "reason": "NOT_A_REAL_REASON", "accepted_by": "disputable"})
    refused = agent.post_json(f"{EAR_TIPS}/accept", accept, label="accept-dst_ear_tips")
    assert (refused.status, refused.code, refused.message) == (400, "validation_error",
                                                               "Invalid accept_reason NOT_A_REAL_REASON")
    challenge = json.dumps({"request_id": "fake-2", "reason": "NOT_A_REAL_REASON", "challenged_by": "disputable"})
    taken = agent.post_json(f"{DISPUTES}/dst_headphones/challenge", challenge, label="challenge-dst_headphones")
    assert taken.status == 200 and taken.data["challenge_details"][-1]["reason"] == "OTHER_REASONS"
    unknown = json.dumps({"request_id": "fake-3", "reason": "OTHERS", "accepted_by": "x"})
    assert agent.post_json(f"{DISPUTES}/dst_nope/accept", unknown, label="accept-dst_nope").status == 404


def test_the_fake_plays_the_issuer_for_the_bank():
    fake = fake_with()
    bank = fake.session("bank")
    created = bank.post_json(f"{PAYMENTS}/create", json.dumps({"request_id": "fake-pay-1", "amount": 25,
                                                                "currency": "USD", "metadata": {"order_no": "9101"}}),
                             label="payment-create")
    payment_id = created.data["id"]
    fake.pay(payment_id)
    assert fake.sandbox.payments[payment_id]["status"] == "SUCCEEDED"
    opened = bank.post_json(f"{SIMULATION}/create", json.dumps({
        "payment_intent_id": payment_id, "reason_code": "13.1", "stage": "RFI", "due_at": "2026-11-04T17:00:00+0000",
        "amount": 25, "comment": "A check of the request shape."}), label="dispute-create")
    dispute_id = opened.data["id"]
    assert (opened.status, opened.data["status"], opened.data["due_at"]) == (201, "REQUIRES_RESPONSE",
                                                                            "2026-11-04T17:00:00+00:00")
    escalate = json.dumps({"due_at": "2026-11-11T17:00:00+0000", "comment": "Turned down."})
    assert bank.post_json(f"{SIMULATION}/{dispute_id}/escalate", escalate, label="escalate").status == 400  # not yet
    fake.sandbox.change(dispute_id, status="CHALLENGED")
    escalated = bank.post_json(f"{SIMULATION}/{dispute_id}/escalate", escalate, label="escalate")
    assert (escalated.data["stage"], escalated.data["status"]) == ("CHARGEBACK", "REQUIRES_RESPONSE")
    fake.sandbox.change(dispute_id, stage="PRE_ARBITRATION", status="PENDING_DECISION")
    resolved = bank.post_json(f"{SIMULATION}/{dispute_id}/resolve", json.dumps({"in_favor_of": "MERCHANT"}),
                              label="resolve")
    assert resolved.data["status"] == "WON"
    assert AgentClient(fake.session()).disputes_on(payment_id).data["items"][0]["id"] == dispute_id


# ----------------------------------------------------------------------------- the module and the seed
def test_the_client_never_imports_the_seed_or_the_bank():
    tree = ast.parse(Path(importlib.util.find_spec("disputable.airwallex").origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "disputable.lifecycle" in names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.issuer", "disputable.story", "disputable.evals", "disputable.web"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], banned
