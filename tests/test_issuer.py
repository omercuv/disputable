"""The bank: the story's simulated issuer on the fake sandbox's simulator routes, its guards, the customer's checkout
page, and the bank preflight. Offline: the fake sandbox's pay() stands in for the browser checkout, which no test runs.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from disputable import airwallex, issuer as bank
from disputable.airwallex import DISPUTES, PAYMENTS, UPLOAD, json_number
from disputable.config import Settings, SetupError
from disputable.db import Database
from disputable.engine import Action, Stage
from disputable.issuer import PREFLIGHT_QUESTION, SIMULATION, Issuer, IssuerError, preflight
from disputable.lifecycle import ACTOR
from disputable.privacy import Allowed, check_files
from disputable.seed.fernhill import FERNHILL, MONDAY, REJECTION_COMMENT
from fake_airwallex import ACCOUNT_ID, API_KEY, CLIENT_ID, FakeAirwallex
from recordings import Ticking
from sandbox import Sandbox

RUN = "a1b2c3d4"
DAY = timedelta(days=1)
CORE = ("config", "privacy", "readings_file", "desk", "db", "clock", "airwallex", "loop", "service", "spend",
        "recording", "demo")


class Bank:
    """The bank on the fake sandbox: its own session, the run's database, and a clock that moves as it is read."""

    def __init__(self, folder: Path, *, pays: bool = True) -> None:
        self.fake = FakeAirwallex(Sandbox(FERNHILL))
        self.wall = Ticking()
        self.db = Database(Path(folder) / "live.sqlite3", stamp=self.wall)
        self.paid: list[tuple[str, str]] = []

        def pay(payment_id: str, secret: str) -> None:  # the customer, in the checkout page
            self.paid.append((payment_id, secret))
            if pays:
                self.fake.pay(payment_id)

        self.issuer = Issuer(self.fake.session("bank", sleep=self.wall.sleep), FERNHILL, self.db, run=RUN, pay=pay,
                             clock=self.wall, sleep=self.wall.sleep)


def created(fake: FakeAirwallex, path: str) -> list[dict]:
    return [json.loads(request.content) for request in fake.calls if request.method == "POST" and request.url.path == path]


# ----------------------------------------------------------------------------- the story's steps
def test_monday_opens_each_case_on_its_own_paid_payment_with_the_storys_deadline(tmp_path):
    run = Bank(tmp_path)
    opened = run.issuer.monday()
    fake = run.fake
    assert [case.case_id for case in opened] == ["ear_tips", "earbuds", "headphones"]
    for case in opened:
        seed = FERNHILL.case(case.case_id)
        order = FERNHILL.order(seed.order_number)
        payment = fake.sandbox.payments[case.payment.payment_id]
        assert payment["status"] == "SUCCEEDED" and payment["amount"] == json_number(order.amount)
        assert payment["metadata"] == {"order_no": order.number, "run": RUN}  # the case's order, or a person's case
        assert re.fullmatch(rf"FH-{order.number}-{RUN}-[0-9a-f]{{4}}", payment["merchant_order_id"])
        assert case.payment.merchant_order_id == payment["merchant_order_id"] and case.payment.order_number == order.number
        dispute = fake.sandbox.disputes[case.dispute_id]
        assert (dispute["stage"], dispute["status"], dispute["reason"]["original_code"], dispute["issuer_comment"]) == (
            seed.stage.value, "REQUIRES_RESPONSE", seed.reason_code, seed.issuer_comment)
        assert dispute["payment_intent_id"] == case.payment.payment_id
        assert dispute["amount"] == json_number(order.amount if seed.amount is None else seed.amount)
        due = datetime.fromisoformat(dispute["due_at"])  # from the bank's clock, now, and 15 minutes past the days
        assert bank.DUE_GRACE == timedelta(minutes=15)
        assert bank.DUE_GRACE <= due - (MONDAY + seed.due_in) < bank.DUE_GRACE + timedelta(minutes=1)
        assert case.date.opened_at == seed.opened_at  # the story's day, which the sandbox's created_at can't say
    assert [round((datetime.fromisoformat(fake.sandbox.disputes[case.dispute_id]["due_at"]) - MONDAY) / DAY)
            for case in opened] == [2, 4, 6]
    assert run.db.store_payments() == sorted((case.payment for case in opened), key=lambda payment: payment.payment_id)
    assert run.db.opened() == {case.dispute_id: case.date.opened_at for case in opened}
    assert [payment_id for payment_id, _ in run.paid] == [case.payment.payment_id for case in opened]
    assert all(secret.startswith("fake-client-secret-") for _, secret in run.paid)  # the create's own, in memory
    requests = [body["request_id"] for body in created(fake, f"{PAYMENTS}/create")]
    assert len(set(requests)) == 3
    assert not [path for path in fake.seen("POST") if path.endswith(("/accept", "/challenge")) or path == UPLOAD]


def test_no_push_back_before_this_apps_challenge_shows_and_no_ruling_before_its_chargeback_one(tmp_path):
    run = Bank(tmp_path)
    headphones = {case.case_id: case.dispute_id for case in run.issuer.monday()}["headphones"]
    sandbox = run.fake.sandbox
    with pytest.raises(IssuerError) as early:
        run.issuer.push_back(headphones)
    assert str(early.value) == ("Order #1109 can't be pushed back yet: the dispute is RFI/REQUIRES_RESPONSE, and this "
                                "app's challenge doesn't show.")
    sandbox.reply(headphones, Action.CHALLENGE, "someone-else", "PURCHASE_HISTORY")  # not this app's
    sandbox.change(headphones, status="CHALLENGED")
    with pytest.raises(IssuerError, match="RFI/CHALLENGED, and this app's challenge doesn't show"):
        run.issuer.push_back(headphones)
    with pytest.raises(IssuerError, match="can't be ruled on yet: it is RFI/CHALLENGED"):
        run.issuer.rule(headphones, "MERCHANT")
    assert not [path for path in run.fake.seen("POST") if path.endswith(("/escalate", "/resolve"))]
    sandbox.reply(headphones, Action.CHALLENGE, ACTOR, "PURCHASE_HISTORY")
    pushed = run.issuer.push_back(headphones)
    assert (pushed["stage"], pushed["status"], pushed["issuer_comment"]) == ("CHARGEBACK", "REQUIRES_RESPONSE",
                                                                            REJECTION_COMMENT)
    left = datetime.fromisoformat(pushed["due_at"]) - run.wall.at  # 7 days and the grace, so a pass reads 7 days
    assert timedelta(days=7) < left <= timedelta(days=7) + bank.DUE_GRACE
    with pytest.raises(IssuerError, match="CHARGEBACK/REQUIRES_RESPONSE, and this app's chargeback challenge"):
        run.issuer.rule(headphones, "MERCHANT")
    sandbox.change(headphones, status="CHALLENGED")  # the bank deciding, with only this app's RFI reply on it
    with pytest.raises(IssuerError, match="CHARGEBACK/CHALLENGED, and this app's chargeback challenge doesn't show"):
        run.issuer.rule(headphones, "MERCHANT")
    assert not [path for path in run.fake.seen("POST") if path.endswith("/resolve")]  # its RFI reply doesn't count
    sandbox.change(headphones, status="REQUIRES_RESPONSE")
    sandbox.reply(headphones, Action.CHALLENGE, ACTOR, "PRODUCT_RECEIVED")  # at CHARGEBACK
    sandbox.change(headphones, stage="PRE_ARBITRATION", status="PENDING_DECISION")
    assert run.issuer.rule(headphones, "MERCHANT")["status"] == "WON"
    assert created(run.fake, f"{SIMULATION}/{headphones}/resolve") == [
        {"comment": "Issuer accepted the signed delivery and warranty registration; ruled for the merchant.",
         "in_favor_of": "MERCHANT"}]
    with pytest.raises(ValueError):
        run.issuer.rule(headphones, "NOBODY")


def test_a_lost_simulator_answer_is_found_on_the_payment_never_opened_twice(tmp_path):
    run = Bank(tmp_path)
    run.fake.fail("POST", f"{SIMULATION}/create", lost=True)  # applied, then the answer never came
    (case,) = run.issuer.monday(("ear_tips",))
    assert list(run.fake.sandbox.disputes) == [case.dispute_id]
    assert run.fake.seen("POST").count(f"{SIMULATION}/create") == 1


def test_a_payment_create_is_tried_again_with_its_own_request_id(tmp_path):
    run = Bank(tmp_path)
    run.fake.fail("POST", f"{PAYMENTS}/create", status=503)
    run.issuer.monday(("ear_tips",))
    sent = created(run.fake, f"{PAYMENTS}/create")
    assert len(sent) == 2 and sent[0] == sent[1] and len(run.fake.sandbox.payments) == 1
    again = Bank(tmp_path / "lost")
    again.fake.fail("POST", f"{PAYMENTS}/create", lost=True)  # it landed, so the retry is a duplicate
    with pytest.raises(IssuerError) as lost:
        again.issuer.monday(("ear_tips",))
    assert str(lost.value) == ("Airwallex already had order #1121's payment request, but its first answer was lost, so "
                               "the bank stops. Run the story again.")
    assert not again.fake.sandbox.disputes and not again.db.store_payments()


def test_a_payment_that_never_succeeds_stops_the_bank(tmp_path):
    run = Bank(tmp_path, pays=False)
    with pytest.raises(IssuerError) as stopped:
        run.issuer.monday(("ear_tips",))
    assert str(stopped.value) == ("Order #1121's payment ended at REQUIRES_PAYMENT_METHOD, not SUCCEEDED, so the bank "
                                  "stops.")
    assert len(run.paid) == 1 and not run.fake.sandbox.disputes and not run.db.store_payments()


def test_the_bank_runs_on_its_own_session_only(tmp_path):
    run = Bank(tmp_path)
    with pytest.raises(ValueError, match="its own session"):
        Issuer(run.fake.session("agent"), FERNHILL, run.db, run=RUN, pay=lambda payment_id, secret: None)
    with pytest.raises(IssuerError, match="never at PRE_CHARGEBACK"):
        Issuer(run.fake.session("bank"), _with_stage(FERNHILL, "ear_tips", "PRE_CHARGEBACK"), run.db, run=RUN,
               pay=lambda payment_id, secret: None).monday(("ear_tips",))


def _with_stage(store, case_id, stage):
    from dataclasses import replace

    from disputable.engine import Stage

    return replace(store, cases=tuple(replace(case, stage=Stage(stage)) if case.id == case_id else case
                                      for case in store.cases))


# ----------------------------------------------------------------------------- the customer's checkout
def test_the_test_card_goes_only_into_airwallexs_own_iframe_on_a_local_page():
    page = bank.CHECKOUT_HTML
    assert re.findall(r'<script src="([^"]+)"', page) == [bank.AIRWALLEX_JS_URL]
    assert bank.AIRWALLEX_JS_URL.startswith("https://static.airwallex.com/")
    assert bank.TEST_CARD not in page and bank.TEST_CVC not in page
    source = inspect.getsource(bank.pay_in_browser)
    assert source.count("TEST_CARD") == 1 and 'frame_locator("#card iframe")' in source  # the card: into the iframe
    assert 'page.goto(f"http://{LOCAL}:{server.server_address[1]}/")' in source  # no intent or secret in the URL
    assert "client_secret" in source.split("page.evaluate(", 1)[1]  # handed to the page's script instead
    server = bank.checkout_server()
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


# ----------------------------------------------------------------------------- bank preflight
def _settings(data: Path, *, keys: bool = True) -> Settings:
    return Settings(CLIENT_ID if keys else None, API_KEY if keys else None, None, "https://api.sandbox.airwallex.com",
                    "https://files.sandbox.airwallex.com", data)


def _preflight(tmp_path, fake, **options):
    wall, said = Ticking(), []
    code = preflight(_settings(tmp_path), say=said.append, transport=fake.transport(),
                     pay=lambda payment_id, secret: fake.pay(payment_id), clock=wall, sleep=wall.sleep, run="b1c2d3e4",
                     **options)
    return code, said


def added_by_airwallex(fake: FakeAirwallex, dispute_id: str) -> str:
    """The preflight's line on what Airwallex added to this app's challenge, from what the sandbox holds."""
    entry = next(entry for entry in fake.sandbox.disputes[dispute_id]["challenge_details"]
                 if entry["challenged_by"] == ACTOR)
    yes = {True: "yes", False: "no"}
    return (f"  Airwallex filled in a seller profile: {yes[bool(entry.get('seller_info'))]}. It filled in the order's "
            f"details: {yes[bool(entry.get('order_info'))]}. It added a file of its own: "
            f"{yes[bool(entry['supporting_documents'].get('generated_files'))]}.")


def test_preflight_sends_the_apps_exact_bodies_twice_and_says_what_came_back(tmp_path):
    fake = FakeAirwallex(Sandbox(FERNHILL))
    code, said = _preflight(tmp_path, fake, yes=True)
    taken = "  The first POST: HTTP 200. The app reads this as taken."
    again = "  The same request id again: HTTP 400, duplicate_request. The app reads this as taken."
    shows = "  The dispute shows this app's response: yes."
    folder = tmp_path / "preflight-b1c2d3e4"
    assert said == [
        "Step 1: a challenge at RFI.", taken, again, shows, added_by_airwallex(fake, "dst_fake0002"),
        "  The dispute stays open in the sandbox: the simulator rules on chargebacks only.",
        "Step 2: a challenge at CHARGEBACK.", taken, again, shows, added_by_airwallex(fake, "dst_fake0004"),
        "  The bank closed the dispute: WON.",
        "Step 3: an accept at RFI with a refund.", taken, again, shows,
        f"The scrubbed calls are in {folder}.", "Both challenge shapes were taken."]
    assert said[4].startswith("  Airwallex filled in a seller profile: yes.") and said[4].endswith("file of its own: yes.")
    assert code == 0
    assert not [line for line in said if re.search(r"(dst|int)_\w|https?://|\$|\b25\b|9101", line)]  # no id or amount
    bodies = {request.url.path.rsplit("/", 1)[-1]: json.loads(request.content) for request in fake.calls
              if request.url.path.endswith(("/accept", "/challenge"))}
    assert set(bodies["challenge"]) == {"request_id", "reason", "challenged_by", "challenge_method", "product_type",
                                        "supporting_documents"}  # the last challenge: at CHARGEBACK
    assert bodies["accept"]["refund"] == {"amount": 25, "reason": "REQUESTED_BY_CUSTOMER"}
    assert {body["challenged_by" if "challenged_by" in body else "accepted_by"] for body in bodies.values()} == {ACTOR}
    assert not fake.sandbox.opened  # three throwaway disputes, in no run's scope
    log = (folder / "exchanges.jsonl").read_text(encoding="ascii")
    for secret in (CLIENT_ID, API_KEY, ACCOUNT_ID, "fake-token-", "fake-client-secret-"):
        assert secret not in log, secret
    secrets = {CLIENT_ID, API_KEY, ACCOUNT_ID, "fake-token-0001-", "fake-token-0002-", "fake-client-secret-"}
    assert check_files([folder / "exchanges.jsonl"], Allowed.of(), secrets) == []


def test_preflight_asks_first_and_says_when_a_challenge_shape_is_refused(tmp_path):
    fake = FakeAirwallex(Sandbox(FERNHILL))
    asked = []
    code, said = _preflight(tmp_path, fake, ask=lambda question: asked.append(question) or "no")
    assert (code, asked, said) == (1, [PREFLIGHT_QUESTION], ["Stopped. Nothing was paid."]) and not fake.calls
    assert PREFLIGHT_QUESTION == "This makes 3 test payments in the sandbox with Airwallex's test card. Type yes to go on: "
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=400)  # the first dispute: step 1's
    code, said = _preflight(tmp_path, fake, ask=lambda question: "yes")
    assert code == 1 and said[1] == "  The first POST: HTTP 400, validation_error. The app reads this as refused."
    assert said[-1] == "A challenge shape was refused. Fix the request bodies before the story is recorded."
    with pytest.raises(SetupError, match="AWX_CLIENT_ID"):
        preflight(_settings(tmp_path, keys=False), yes=True)


def test_preflight_says_when_airwallex_gave_no_clear_answer_and_blames_no_body(tmp_path):
    """As on Nov 7, when the sandbox answered every challenge with HTTP 500: nothing refused the shapes."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    for dispute_id in ("dst_fake0002", "dst_fake0004"):  # steps 1 and 2: the first POST and the repeat
        fake.fail("POST", f"{DISPUTES}/{dispute_id}/challenge", status=500, times=2)
    code, said = _preflight(tmp_path, fake, yes=True)
    assert code == 1 and said[1:4] == [
        "  The first POST: HTTP 500, internal_error. The app would read the dispute again and send the same request.",
        "  The same request id again: HTTP 500, internal_error. The app would read the dispute again and send the same "
        "request.", "  The dispute shows this app's response: no."]
    assert said[-2:] == [
        "Step 1 couldn't check the challenge shape: Airwallex answered HTTP 500 each time. Run the preflight again later.",
        "Step 2 couldn't check the challenge shape: Airwallex answered HTTP 500 each time. Run the preflight again later."]
    assert not [line for line in said if "refused" in line or "Fix the request bodies" in line]


def test_preflight_says_a_shape_it_couldnt_send_wasnt_checked(tmp_path):
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", UPLOAD, status=503)  # step 1's file
    code, said = _preflight(tmp_path, fake, yes=True)
    assert code == 1 and said[1] == "  The file didn't upload (HTTP 503, internal_error), so nothing was sent."
    assert said[-1] == "Step 1 couldn't check the challenge shape: the file didn't upload. Run the preflight again later."
    assert said[2] == "Step 2: a challenge at CHARGEBACK."  # the next step still runs


def test_preflight_reads_a_first_post_whose_answer_was_lost_from_the_dispute(tmp_path):
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", lost=True)  # it landed; its answer never came
    code, said = _preflight(tmp_path, fake, yes=True)
    assert said[1:4] == ["  The first POST: no answer (ReadTimeout). The app would read the dispute again and send the "
                         "same request.",
                         "  The same request id again: HTTP 400, duplicate_request. The app reads this as taken.",
                         "  The dispute shows this app's response: yes."]
    assert (code, said[-1]) == (0, "Both challenge shapes were taken.")


UNCLEAR_FIRST = ("  The first POST: HTTP 500, internal_error. The app would read the dispute again and send the same "
                 "request.")
NOT_SHOWN = "  The dispute shows this app's response: no."
RFI_OPEN = "  The dispute stays open in the sandbox: the simulator rules on chargebacks only."


def test_preflight_says_a_shape_was_refused_when_the_repeat_of_an_unclear_post_is_refused(tmp_path):
    """The first POST got no clear answer, then Airwallex refused the same request while nothing shows: the body is
    what to fix, so the line never sends the user back to run the preflight again."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=500)  # step 1's first POST
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=400)  # and the repeat: refused
    code, said = _preflight(tmp_path, fake, yes=True)
    assert said[:6] == ["Step 1: a challenge at RFI.", UNCLEAR_FIRST,
                        "  The same request id again: HTTP 400, validation_error. The app reads this as refused.",
                        NOT_SHOWN, RFI_OPEN, "Step 2: a challenge at CHARGEBACK."]
    assert code == 1 and said[-2:] == [
        f"The scrubbed calls are in {tmp_path / 'preflight-b1c2d3e4'}.",
        "A challenge shape was refused. Fix the request bodies before the story is recorded."]


def test_preflight_says_a_chargeback_whose_response_doesnt_show_stays_open_and_adds_nothing(tmp_path):
    """Step 2's challenge answered HTTP 500 twice: nothing shows, so there is nothing Airwallex added to tell, and the
    bank, which rules only on a response that shows, leaves the chargeback open."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{DISPUTES}/dst_fake0004/challenge", status=500, times=2)  # step 2's first POST and the repeat
    code, said = _preflight(tmp_path, fake, yes=True)
    step_two = said.index("Step 2: a challenge at CHARGEBACK.")
    assert said[step_two:step_two + 6] == [
        "Step 2: a challenge at CHARGEBACK.", UNCLEAR_FIRST,
        "  The same request id again: HTTP 500, internal_error. The app would read the dispute again and send the same "
        "request.", NOT_SHOWN, "  The dispute stays open in the sandbox: the bank rules only on a response that shows.",
        "Step 3: an accept at RFI with a refund."]
    assert said[4] == added_by_airwallex(fake, "dst_fake0002")  # step 1's response shows, so its line is there
    assert [line for line in said if line.startswith("  Airwallex filled in")] == [said[4]]
    assert code == 1 and said[-1] == ("Step 2 couldn't check the challenge shape: Airwallex answered HTTP 500 each time. "
                                      "Run the preflight again later.")


def test_preflight_counts_a_repeat_airwallex_took_as_taken_though_nothing_shows(tmp_path):
    """No clear first answer, then a 2xx for the same request id: Airwallex took the shape, even before it shows."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=500)
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=200, body={})  # taken, but the dispute doesn't show it
    code, said = _preflight(tmp_path, fake, yes=True)
    assert said[:6] == ["Step 1: a challenge at RFI.", UNCLEAR_FIRST,
                        "  The same request id again: HTTP 200. The app reads this as taken.", NOT_SHOWN, RFI_OPEN,
                        "Step 2: a challenge at CHARGEBACK."]
    assert (code, said[-1]) == (0, "Both challenge shapes were taken.")
    assert not [line for line in said if "couldn't check" in line or "refused" in line]


def test_preflight_counts_a_refused_repeat_as_taken_when_the_response_shows(tmp_path):
    """The first POST landed, but its answer was lost. Airwallex refused the repeat with the same request id, as the
    sandbox did on Nov 7 (HTTP 400, invalid_status_for_operation). The dispute shows this app's response, so Airwallex
    took the shape: no request body is blamed."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", lost=True)  # step 1's first POST: it landed, no answer came
    fake.fail("POST", f"{DISPUTES}/dst_fake0002/challenge", status=400,
              body={"code": "invalid_status_for_operation", "message": "A made-up refusal"})  # and the repeat
    code, said = _preflight(tmp_path, fake, yes=True)
    assert said[:7] == ["Step 1: a challenge at RFI.",
                        "  The first POST: no answer (ReadTimeout). The app would read the dispute again and send the "
                        "same request.",
                        "  The same request id again: HTTP 400, invalid_status_for_operation. The app reads this as "
                        "refused.", "  The dispute shows this app's response: yes.",
                        added_by_airwallex(fake, "dst_fake0002"), RFI_OPEN, "Step 2: a challenge at CHARGEBACK."]
    assert said[4] == ("  Airwallex filled in a seller profile: yes. It filled in the order's details: yes. It added a "
                       "file of its own: yes.")
    assert (code, said[-1]) == (0, "Both challenge shapes were taken.")
    assert not [line for line in said if "couldn't check" in line or "Fix the request bodies" in line]


@pytest.mark.parametrize("how", ["a body problem", "a builder that raises"])
def test_preflight_says_when_the_app_couldnt_build_a_challenge_body(tmp_path, monkeypatch, how):
    """Nothing was sent for that step, and the verdict blames the app's own body, not Airwallex."""
    real_problem, real_body = airwallex.body_problem, airwallex.challenge_body

    def at_rfi(plan) -> bool:
        return (plan.terms.action, plan.terms.stage) == (Action.CHALLENGE, Stage.RFI)

    def problem(plan, body, files):
        return "a made-up problem" if at_rfi(plan) else real_problem(plan, body, files)

    def body(plan, files):
        if at_rfi(plan):
            raise ValueError("a made-up problem")
        return real_body(plan, files)

    monkeypatch.setattr(airwallex, *(("body_problem", problem) if how == "a body problem" else ("challenge_body", body)))
    fake = FakeAirwallex(Sandbox(FERNHILL))
    code, said = _preflight(tmp_path, fake, yes=True)
    assert said[:3] == ["Step 1: a challenge at RFI.",
                        "  The app couldn't build the request (a made-up problem), so nothing was sent.",
                        "Step 2: a challenge at CHARGEBACK."]
    assert code == 1 and said[-2:] == [
        f"The scrubbed calls are in {tmp_path / 'preflight-b1c2d3e4'}.",
        "The app couldn't build a challenge body. Fix the request bodies before the story is recorded."]
    assert f"{DISPUTES}/dst_fake0002/challenge" not in fake.seen("POST")  # step 1 sent nothing
    assert f"{DISPUTES}/dst_fake0004/challenge" in fake.seen("POST")  # step 2 still ran


def test_preflight_never_prints_airwallexs_words_from_a_refused_bank_step(tmp_path):
    """Airwallex's message repeats what it was sent (fixtures/spike/069), so a preflight line gives only the status and
    the error code. Every value here is made up."""
    fake = FakeAirwallex(Sandbox(FERNHILL))
    fake.fail("POST", f"{SIMULATION}/create", status=400, body={
        "code": "validation_error",
        "message": "amount 25 is more than payment int_fake0001 can dispute at https://x.example"})
    wall, said = Ticking(), []
    with pytest.raises(IssuerError) as stopped:
        preflight(_settings(tmp_path), yes=True, say=said.append, transport=fake.transport(),
                  pay=lambda payment_id, secret: fake.pay(payment_id), clock=wall, sleep=wall.sleep, run="b1c2d3e4")
    assert str(stopped.value) == "The bank couldn't open order #9101's dispute (HTTP 400, validation_error)."
    for line in [*said, str(stopped.value)]:
        assert not re.search(r"(dst|int)_\w|https?://|\$|\b25\b", line), line
    story_bank = Bank(tmp_path / "story")  # the story run's bank keeps Airwallex's words for the one running it
    story_bank.fake.fail("POST", f"{SIMULATION}/create", status=400, body={"code": "validation_error",
                                                                          "message": "A made-up refusal"})
    with pytest.raises(IssuerError, match=re.escape("(HTTP 400, validation_error: A made-up refusal)")):
        story_bank.issuer.monday(("ear_tips",))


# ----------------------------------------------------------------------------- the agent and the bank apart
def _imports(module: str) -> set[str]:
    tree = ast.parse(Path(importlib.util.find_spec(module).origin).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return names | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


@pytest.mark.parametrize("module", CORE)
def test_the_agent_never_imports_the_bank(module):
    if importlib.util.find_spec(f"disputable.{module}") is None:
        pytest.skip(f"disputable.{module} isn't built yet.")
    names = _imports(f"disputable.{module}")
    for banned in ("disputable.issuer", "disputable.story", "disputable.seed"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], (module, banned)


def test_the_bank_takes_the_store_as_an_argument():
    names = _imports("disputable.issuer")
    assert "disputable.airwallex" in names  # the scan sees the imports
    for banned in ("disputable.seed", "disputable.story", "disputable.evals", "disputable.web", "disputable.cli"):
        assert not [name for name in names if name == banned or name.startswith(banned + ".")], banned
