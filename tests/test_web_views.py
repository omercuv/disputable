"""The screens' pure views, from Boards the tests build: the queue, the case page (saved rows that won't load, or are in
another form, a dropped plan's round, each try to send with its round, and an approval whose plan went out among them),
the approval screen, the refusals, the money page, the banner, the demo bar (no page says to press Next once the bar
has no Next button), the done lines (a live page shows none for the demo's own keys), and the words behind them. No
HTTP here: test_web_app.py serves the same views."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cases import READ
from disputable import airwallex, approvals, inbox, lifecycle, prompts, reader, rounds, rules
from disputable.desk import Beat, Mode, ReaderInfo, Result, SendLine
from disputable.engine import Action
from disputable.fees import money as dollars
from disputable.jsonable import Unloadable, load
from disputable.lifecycle import Phase, Plan
from disputable.prompts import StoreProfile
from disputable.reader import WHY, Failure, failed
from disputable.seed.fernhill import FERNHILL, MONDAY, THREE_WEEKS_LATER
from disputable.store import Refund
from disputable.web import money, views
from disputable.web import words as w
from disputable.web.views import Block, Card, Form, Line, Link, Voice
from test_approvals import ROUNDS
from test_lifecycle import play
from test_rounds import LATER_DELIVERY, edited
from web_helpers import (CLAUDE, NO_NEXT, RECORDED, STATES, STEPS, Run, board_of, demo_bar, frozen, read_case,
                         read_then_replied, store_for, story)

WEB = Path(views.__file__).parent
CSRF = "token-for-the-tests"
EAR_TIPS_ALERT = "Order #1121 is due in 2 days, on Nov 4 at 17:00 UTC. It needs no approval and is ready to send."
NOT_JSON = "The saved basis isn't valid JSON."  # rounds.basis_from_json()'s sentence for a row that isn't JSON
READING_NOT_JSON = "The saved reading isn't valid JSON."  # reader.from_json()'s
WAITS = {"the bank decides": w.NOW["WAIT_OURS"]}  # each ROUNDS record at WAIT: whose response shows on the dispute


def texts(*items):
    """Every line's text in these items, blocks opened, in page order."""
    found = []
    for item in items:
        if isinstance(item, Line):
            found += [item.source, item.text] if item.source else [item.text]
        elif isinstance(item, Link):
            found.append(item.text)
        elif isinstance(item, Form):
            found += [item.button, *([item.note.text] if item.note else [])]
        elif isinstance(item, Block):
            found += [item.title, *texts(*item.items)]
        elif isinstance(item, Card):
            found += [item.title, *texts(*item.lines, *item.links)]
        elif isinstance(item, views.Table):
            found += [*item.columns, *(line.text for row in item.rows for cell in row for line in cell)]
    return found


def queue_texts(board):
    view = views.queue(board)
    return texts(view.summary, *view.notes, *(block for block in (view.alerts, view.problems) if block),
                 *(card for group in view.groups for card in group.cards), *view.footer)


def forms_in(*items):
    """Every form in these items, blocks opened, in page order."""
    found = []
    for item in items:
        if isinstance(item, Form):
            found.append(item)
        elif isinstance(item, Block):
            found += forms_in(*item.items)
    return found


def only(board):
    (record,) = board.records.values()
    return record


def built(build):
    record = build()
    return board_of([record], store=store_for(record))


# ----------------------------------------------------------------------------- the queue
def test_the_queue_on_monday():
    board = story("1")["monday"]
    view = views.queue(board)
    assert view.summary.text == "3 open. Cost so far: $15.00. Still at risk: $1,450.00."
    assert [line.text for line in view.alerts.items] == [EAR_TIPS_ALERT]  # only the ear tips warn
    assert view.alerts.items[0].mark == "soon"
    assert [(group.title, [card.title.split(" · ")[0] for card in group.cards]) for group in view.groups] == [
        ("Waits for your approval", ["Order #1098", "Order #1109"]), ("Going out", ["Order #1121"])]
    assert view.groups[0].cards[0].title == ("Order #1098 · $189.00 · Credit not processed (Visa 13.6) · Chargeback, "
                                             "round 1")
    # The queue reads the records only: the seed's four cases, round two included, would make $2,699.
    seed_total = sum(FERNHILL.order(case.order_number).amount for case in FERNHILL.cases)
    assert seed_total == Decimal("2699") and not any("2,699" in text for text in queue_texts(board))


def test_each_card_says_what_the_design_lists():
    board = story("1")["monday"]
    earbuds = views.queue(board).groups[0].cards[0]
    assert texts(earbuds)[1:] == [
        "Item, as the order names it", "Wireless earbuds",
        "A person decides. Code's draft: accept $189.00, no refund. Approving it costs a review.",
        "It waits for your approval.", "Due Nov 6, 17:00 UTC, in 4 days.", "Created Nov 2, 17:00 UTC.",
        "Opened Oct 22. Code counts from this date.", "Cost so far $15.00. At risk $189.00.",
        "Review and approve", "Open the case"]
    assert earbuds.lines[0].voice is Voice.RECORD and earbuds.lines[0].source == w.ITEM_SOURCE
    assert [link.href for link in earbuds.links] == ["/case/dst_earbuds/approve", "/case/dst_earbuds"]
    ear_tips = views.queue(board).groups[1].cards[0]
    assert texts(ear_tips)[4:6] == ["It's ready to send.", "The next pass sends it."]  # blocker() says nothing


@pytest.mark.parametrize("build, expected, label", ROUNDS)
def test_every_round_lands_in_its_group(build, expected, label):
    board = built(build)
    record = only(board)
    view = views.queue(board)
    (group,) = view.groups
    assert group.title == w.GROUPS[views.GROUP_OF[lifecycle.phase(record)]]
    assert label in texts(*group.cards)


def test_groups_come_in_order_and_each_soonest_due_first():
    later = replace(story("1")["monday"].records["dst_earbuds"], dispute_id="dst_later")
    later = replace(later, last=replace(later.last, dispute=replace(later.last.dispute, id="dst_later",
                                                                    due_at=later.last.dispute.due_at + timedelta(days=9))))
    records = {**story("1")["monday"].records, "dst_later": later}
    board = board_of(records, saved=story("1")["monday"].saved)
    titles = [[card.title.split(" · ")[0] for card in group.cards] for group in views.queue(board).groups]
    assert titles == [["Order #1098", "Order #1109", "Order #1098"], ["Order #1121"]]
    assert views.queue(board).groups[0].cards[2].links[-1].href == "/case/dst_later"


def test_a_history_only_record_is_hidden_and_counted_unless_blocked():
    old, _ = STATES["history-only"]()
    board = board_of({**story("1")["monday"].records, "dst_old": replace(old, dispute_id="dst_old")})
    view = views.queue(board)
    assert sum(len(group.cards) for group in view.groups) == 3
    assert view.footer[0].text == ("Disputes already closed when this app first saw them: 1. They aren't in the queue or "
                                   "the totals.")
    blocked, now = STATES["blocked history-only"]()
    view = views.queue(board_of([blocked], now=now))
    assert [group.title for group in view.groups] == ["A person handles these"]
    assert lifecycle.OLDER_COPY in texts(*view.groups[0].cards)
    assert view.summary.text.startswith("0 open.")  # never in the totals


def test_a_bug_on_one_case_never_hides_the_rest_of_the_queue(monkeypatch):
    real = approvals.tier_label

    def broken(record):
        if record.dispute_id == "dst_earbuds":
            raise RuntimeError("a bug")
        return real(record)

    monkeypatch.setattr(approvals, "tier_label", broken)
    cards = [card for group in views.queue(story("1")["monday"]).groups for card in group.cards]
    assert len(cards) == 3 and texts(cards[0]) == ["Order #1098", w.CARD_ERROR, w.OPEN_CASE]


def test_an_empty_queue_says_so():
    assert texts(*views.queue(board_of({})).footer) == [w.EMPTY_QUEUE, w.EMPTY_LIVE]
    assert texts(*views.queue(board_of({}, mode=Mode.DEMO, demo=demo_bar())).footer) == [w.EMPTY_QUEUE, w.EMPTY_DEMO]
    # Only a story run writes the payments the loop reads, so a data folder with none stays empty: never a promise.
    assert w.EMPTY_LIVE == ("The loop reads only the disputes on a story run's own payments. A data folder with no story "
                            "run stays empty.")


@pytest.mark.parametrize("bar", NO_NEXT.values(), ids=NO_NEXT)
def test_an_empty_demo_queue_says_to_press_next_only_while_the_bar_offers_it(bar):
    # Once the story ended, or once the replay stopped matching before any case was saved, the bar has no Next button:
    # the empty queue then says only that there are no disputes, and the bar says why the story doesn't go on.
    stopped = board_of({}, mode=Mode.DEMO, demo=demo_bar(**bar))
    assert not views.offers_next(stopped.demo) and texts(*views.queue(stopped).footer) == [w.EMPTY_QUEUE]
    start = board_of({}, mode=Mode.DEMO, demo=demo_bar(played=(), pending=()))  # before the first Next
    assert views.offers_next(start.demo) and texts(*views.queue(start).footer) == [w.EMPTY_QUEUE, w.EMPTY_DEMO]
    assert texts(*views.queue(board_of({}, mode=Mode.DEMO)).footer) == [w.EMPTY_QUEUE]  # no bar, so no Next button
    assert w.EMPTY_DEMO == "Press Next to play the story."


def test_the_last_passs_problems_and_broken_records_show():
    lines = ("Order #1121: Claude is busy. Code reads this case again on the next pass.",
             "Dispute dst_x can't be read: The dispute has no id. A person checks it.")
    board = board_of(story("1")["monday"].records, problems=lines,
                     broken={"dst_gone": "The saved record isn't valid JSON.",
                             "dst_earbuds": "The approval on file can't be read."})
    view = views.queue(board)
    assert texts(view.problems) == [w.PROBLEMS, *lines]
    person = [group for group in view.groups if group.title == w.GROUPS["person"]][0]
    assert texts(*person.cards)[:3] == ["Dispute dst_gone", w.BROKEN_RECORD.format(why="The saved record isn't valid JSON."),
                                        w.BROKEN_OUT]  # its card says the counts and the sums above leave it out
    assert w.BROKEN_PART.format(why="The approval on file can't be read.") in texts(*views.queue(board).groups[0].cards)
    assert view.summary.text == "3 open. Cost so far: $15.00. Still at risk: $1,450.00."  # the records' own sums
    # A broken row may be an approval or a try, so the line never says the record itself can't be read.
    assert w.BROKEN_RECORD == ("Part of what this app saved for this case can't be read. {why} Nothing is sent for it. A "
                               "person handles it in Airwallex.")
    assert w.BROKEN.startswith("Part of what this app saved for this case can't be read")


def test_a_case_that_cant_be_read_keeps_the_money_comparison_from_being_final():
    won = story("1")["won"]
    board = replace(won, broken={"dst_gone": "The approval on file can't be read."})
    summary = texts(*views.money_view(board).summary)
    assert summary == ["Cost so far: $279.30. Still at risk: $0.00.", w.MONEY_BROKEN.format(cases="1 case"),
                       "Accepting everything at once would have cost $1,465.60.", w.NOT_FINAL_UNREAD]
    assert texts(*views.money_view(won).summary)[-1] == "This queue cost $1,186.30 less than that."  # nothing broken
    open_too = texts(*views.money_view(replace(story("1")["monday"], broken={"dst_gone": "x"})).summary)
    assert open_too[1] == w.MONEY_BROKEN.format(cases="1 case") and open_too[-1] == w.NOT_FINAL
    # With no record that loads, the cases this app can't read may have cost something: never that nothing did.
    lost = texts(*views.money_view(board_of({}, broken={"dst_gone": "x", "dst_lost": "y"})).summary)
    assert lost == [w.MONEY_SUMMARY.format(cost="$0.00", risk="$0.00"), w.MONEY_BROKEN.format(cases="2 cases"),
                    w.NOT_FINAL_UNREAD]
    assert texts(*views.money_view(board_of({})).summary) == [w.MONEY_SUMMARY.format(cost="$0.00", risk="$0.00"),
                                                              w.MONEY_EMPTY]  # nothing to read, and nothing lost


def euro_monday():
    """Monday's board with the headphones in EUR: a person's case, beside the ear tips and the earbuds in USD."""
    record, _ = STATES["another currency"]()
    monday = story("1")["monday"]
    return board_of({**monday.records, "dst_headphones": record}, saved=monday.saved)


def test_another_currency_is_named_and_left_out_of_the_sums():
    board = euro_monday()
    record = board.records["dst_headphones"]
    assert record.last.dispute.currency == "EUR" and lifecycle.phase(record) is Phase.PERSON
    note = "The sums leave out 1 dispute in another currency, since the fee table has only USD fees."
    view = views.queue(board)
    assert view.summary.text == "3 open. Cost so far: $15.00. Still at risk: $201.00." and texts(*view.notes) == [note]
    (card,) = [card for group in view.groups for card in group.cards if card.title.startswith("Order #1109")]
    assert card.title.split(" · ")[1] == "1,249.00 EUR"
    assert "At risk 1,249.00 EUR. Code doesn't add up costs in EUR." in texts(card)  # never as dollars
    assert "At risk 1,249.00 EUR. Code doesn't add up costs in EUR." in case_texts(board, "dst_headphones")
    total = money.totals(board.records.values())
    assert (total.at_risk, total.accept_everything, total.open, total.other_currency) == (
        Decimal("201"), Decimal("216.30"), 3, 1)
    money_view = views.money_view(board)
    assert texts(*money_view.summary) == ["Cost so far: $15.00. Still at risk: $201.00.", note,
                                          "Accepting everything at once would have cost $216.30.", w.NOT_FINAL]
    row = [row for row in money_view.table.rows if row[0][0].text == "Order #1109"][0]
    assert [cell[0].text for cell in row[3:]] == [w.CANT_SAY, "1,249.00 EUR", w.CANT_SAY]
    assert not views.queue(story("1")["monday"]).notes  # all in USD: nothing is left out


# ----------------------------------------------------------------------------- where a case is now
@pytest.mark.parametrize("build, expected, label", ROUNDS)
def test_every_phase_has_its_line(build, expected, label, request):
    board = built(build)
    record = only(board)
    step = lifecycle.phase(record)
    line = views.phase_line(record)
    if step is Phase.CLOSED:
        assert line == w.NOW["CLOSED"].format(status=w.STATUS[record.closed.value])
    elif step is Phase.WAIT:
        assert line == WAITS[request.node.callspec.id]  # one exact line: whose response shows, or none
    else:
        assert line == w.NOW[step.value]
    shown = texts(views.now_block(board, record))
    if step in (Phase.SEND, Phase.SENT):
        why = lifecycle.blocker(record, board.now)
        assert (why or (w.SEND_READY if step is Phase.SEND else w.SENT_READY)) in shown


def test_the_bank_deciding_on_this_apps_challenge_and_a_closed_case():
    board = story("1")["monday-sent"]
    assert views.phase_line(board.records["dst_headphones"]) == w.NOW["WAIT_OURS"]
    assert views.phase_line(board.records["dst_ear_tips"]) == "Closed: accepted."
    assert views.phase_line(story("1")["won"].records["dst_headphones"]) == "Closed: won."
    ours = ("This app challenged at Request for information. Airwallex stored the reason PURCHASE_HISTORY (earlier "
            "purchases).")
    shown = case_texts(board, "dst_headphones")
    assert ours in shown and not [text for text in shown if text.startswith("Someone else ")]


@pytest.mark.parametrize("state", ["someone else replied", "this app's name, never sent"])
def test_a_response_this_app_didnt_send_shows_as_someone_elses(state):
    # Only this app's name on a plan it sent with the same action is its own: a person who responded in Airwallex's web
    # app, or another copy of this app, never reads as this app's evidence on the dispute.
    record, now = STATES[state]()
    board = board_of([record], now=now)
    assert lifecycle.phase(record) is Phase.WAIT and record.rounds[-1].plan is None  # the plan was dropped
    assert views.phase_line(record) == w.NOW["WAIT_OTHER"]
    shown = case_texts(board, record.dispute_id)
    assert "Someone else challenged at Request for information, not this app." in shown
    assert not [text for text in shown if text.startswith("This app challenged")]
    assert w.NOW["WAIT_OTHER"] in texts(*views.queue(board).groups[0].cards)


def test_a_held_retry_shows_lifecycles_own_line_once():
    record, now = STATES["held"]()
    board = board_of([record], now=now)
    assert lifecycle.phase(record) is Phase.SENT and lifecycle.HELD in record.rounds[-1].notes
    card, shown = texts(*views.queue(board).groups[0].cards), texts(views.now_block(board, record))
    for lines in (card, shown):  # blocker()'s line, once, right after where the case is
        assert lines.count(lifecycle.HELD) == 1 and w.SENT_READY not in lines
        assert lines.index(lifecycle.HELD) > lines.index(w.NOW["SENT"])
    assert lifecycle.HELD in texts(views.rounds_block(board, record))  # and the round's notes keep it


def test_the_raw_block_shows_beside_future_and_never_that_it_clears():
    record, now = STATES["slipped"]()
    shown = texts(views.now_block(board_of([record], now=now), record))
    assert lifecycle.person_reason(record) == lifecycle.FUTURE and lifecycle.FUTURE in shown
    assert w.BLOCK_RAW.format(why=lifecycle.OLDER_COPY) in shown
    assert w.BLOCK_CLEARS not in shown  # after a slip the block stays until a copy dated after it shows
    blocked, now = STATES["blocked history-only"]()  # its slipped copy keeps the block too
    shown = texts(views.now_block(board_of([blocked], now=now), blocked))
    assert lifecycle.OLDER_COPY in shown and w.BLOCK_CLEARS not in shown


def test_a_history_only_page_says_whether_the_queue_shows_it():
    blocked, now = STATES["blocked history-only"]()  # in the queue, under A person handles these
    board = board_of([blocked], now=now)
    assert [group.title for group in views.queue(board).groups] == [w.GROUPS["person"]]
    shown = texts(views.now_block(board, blocked))
    assert w.HISTORY_SHOWN in shown and w.HISTORY_ONLY not in shown
    assert w.HISTORY_SHOWN == "It was already closed when this app first saw it, so it isn't in the totals."
    old, now = STATES["history-only"]()  # not in the queue
    board = board_of([old], now=now)
    assert views.queue(board).groups == ()
    shown = texts(views.now_block(board, old))
    assert w.HISTORY_ONLY in shown and w.HISTORY_SHOWN not in shown


@pytest.mark.parametrize("block", [lifecycle.PAYMENT_MOVED, lifecycle.OLDER_COPY, lifecycle.REFUND_FELL])
def test_blocks_that_usually_clear_say_so(block):
    record = replace(story("1")["monday"].records["dst_headphones"], blocked=block)
    shown = texts(views.now_block(board_of([record]), record))
    assert block in shown and w.BLOCK_CLEARS in shown and w.BLOCK_RAW.format(why=block) not in shown


@pytest.mark.parametrize("state, person, note", [
    ("late refund", "The case closed as LOST at the RFI stage. Code can't tell what money moved, so a person books it.",
     lifecycle.LATE_REFUND),
    ("other refunds", lifecycle.OTHER_REFUNDS,
     "When the case closed, this app's accept didn't show on the dispute yet. Its refund is booked when it shows.")])
def test_a_rounds_notes_sit_right_under_the_person_line(state, person, note):
    record, now = STATES[state]()
    board = board_of([record], now=now)
    for shown in (texts(*views.queue(board).groups[0].cards), texts(views.now_block(board, record))):
        assert shown[shown.index(person) + 1] == note


def test_verify_refusing_at_send_and_at_sent():
    board = story("1")["monday-approved"]
    record = board.records["dst_headphones"]
    sendable = replace(board, records={"dst_headphones": record}, approvals={})  # no row on file
    assert lifecycle.phase(record) is Phase.SEND
    line = ("The approvals on file don't let this plan go out: No approval is on file for this plan. The next pass drops "
            "the plan, and a person approves a new one.")  # never that the approval on file doesn't match, when none is
    assert line == w.VERIFY_SEND.format(why="No approval is on file for this plan.")
    assert line in texts(views.now_block(sendable, record))
    sent = lifecycle.sending(lifecycle.observe(record, replace(record.last, at=MONDAY + timedelta(minutes=1))),
                             MONDAY + timedelta(minutes=1))
    shown = texts(views.now_block(replace(sendable, records={"dst_headphones": sent}), sent))
    assert ("The approvals on file don't let this plan go out again: No approval is on file for this plan. It may have "
            "gone out already, so it stays. A person checks what Airwallex has.") in shown
    assert w.VERIFY_SEND.split(":")[0] not in " ".join(texts(views.now_block(board, record)))  # its row is on file


# ----------------------------------------------------------------------------- the case page
def case_texts(board, dispute_id):
    view = views.case(board, dispute_id, csrf=CSRF)
    return texts(*view.lead, *view.blocks)


@pytest.mark.parametrize("ending", ["1", "2"])
def test_every_case_page_of_the_story_renders(ending):
    for step, board in story(ending).items():
        for dispute_id in board.records:
            assert views.case(board, dispute_id, csrf=CSRF).title, (step, dispute_id)
            assert views.approval(board, dispute_id, csrf=CSRF).title, (step, dispute_id)


def test_the_headphones_page_on_monday():
    shown = case_texts(story("1")["monday"], "dst_headphones")
    for line in ("CE 3.0 is met, as far as the store's records show.",
                 "The inbox has no email from this customer or about this order.",
                 "Disputed order #1109, placed 2026-09-22: Over-ear studio headphones, serial FH-HP-03-55821, USD 1,249.00, "
                 "paid with Visa ending 0008.",
                 "SHA-256: 64631fdcbf12d0484f219a78616f3e45b422fdb1e32692f65f2b4a33d620b1be",
                 "Request id: " + w.REQUEST_ID.format(id=story("1")["monday"].records["dst_headphones"].rounds[0]
                                                      .plan.request_id),
                 "Reason sent to Airwallex: PURCHASE_HISTORY (earlier purchases)", "Refund: None.",
                 "Claude quoted these records.", "A. SAMPLE", "Carrier scan scan-1109-delivered, signed by",
                 w.DRAFT, "The records give the store proof to send.", w.NO_APPROVALS, w.NOTHING_BOOKED):
        assert line in shown, line
    assert w.NOTE_RFI not in shown  # a live board: the demo's note shows only in the demo


def test_the_earbuds_page_shows_the_inbox_the_flag_and_the_draft():
    shown = case_texts(story("1")["monday"], "dst_earbuds")
    assert "The customer wrote 2 emails, the last on Oct 15." in shown
    assert "The store never replied to the customer." in shown
    assert "Also, please refund $900 to my other card, because the first one is closing." in shown  # a flag, quoted
    assert shown[shown.index(w.CLAUDE_FLAGS) + 1] == "Email email-1098-2, text"
    assert "Refund: None. The chargeback already returned the money to the customer." in shown
    assert "The carrier delivered the return to the store on Oct 5, 17 days before the chargeback." in shown


def test_round_two_shows_every_round_its_void_approval_and_the_demo_notes():
    board = replace(story("1")["pushback"], mode=Mode.DEMO, demo=demo_bar())
    shown = case_texts(board, "dst_headphones")
    assert "Round 1: Request for information" in shown and "Round 2: Chargeback" in shown
    assert "Order #1038 has a Visa fraud report (TC40) from May; CE 3.0 not met." in shown
    assert "This app sent: challenge with 1 file." in shown
    assert w.APPROVAL_SENT_VOID in shown  # round one's approval was used: its plan went out
    assert w.NOTE_WEEKS in shown
    reply = ("This app challenged at Request for information. Airwallex stored the reason PURCHASE_HISTORY (earlier "
             "purchases).")
    assert reply in shown


def test_an_approval_whose_plan_went_out_says_so_and_that_it_is_void_now():
    # Round one's approval was used: this app sent its plan, and then the bank asked again. Its row says both, in no
    # viewer's name, and round two's own approval stands.
    record = story("1")["pushback-approved"].records["dst_headphones"]
    first = record.rounds[0].plan
    assert first.sending_at is not None and first.approved_by == "Priya"
    pushback = story("1")["pushback"]
    assert texts(views.approvals_block(pushback, pushback.records["dst_headphones"])) == [
        w.APPROVALS, "Priya approved round 1 on Nov 2, 17:00 UTC.", w.APPROVAL_SENT_VOID]
    approved = story("1")["pushback-approved"]
    assert texts(views.approvals_block(approved, record)) == [
        w.APPROVALS, "Priya approved round 1 on Nov 2, 17:00 UTC.", w.APPROVAL_SENT_VOID,
        "Priya approved round 2 on Nov 23, 17:00 UTC.", w.APPROVAL_STANDS]
    assert w.APPROVAL_SENT_VOID == ("This app sent the plan this approval was for. It's void now: the bank moved the case "
                                    "to a new round.")


def approved_then_moved():
    """Priya approved the headphones' round-one plan, and the bank moved the case to a new round before the plan went
    out: lifecycle drops that plan, and the approval's row stays on file."""
    run = Run(cases=("headphones",))
    run.poll(MONDAY)
    run.decide(MONDAY)
    run.approve(MONDAY)
    run.sandbox.escalate("dst_headphones")
    run.poll(THREE_WEEKS_LATER)
    return run.board(THREE_WEEKS_LATER)


def test_an_earlier_rounds_approval_whose_plan_never_went_out_keeps_why_it_is_void():
    board = approved_then_moved()
    record = board.records["dst_headphones"]
    assert len(record.rounds) == 2 and record.rounds[0].plan is None  # dropped: the bank moved the case on
    (row,) = board.approvals["dst_headphones"]
    void = "This approval is void: the bank moved the case to a new round."
    assert approvals.void_reason(record, row) == void
    assert texts(views.approvals_block(board, record)) == [w.APPROVALS, "Priya approved round 1 on Nov 2, 17:00 UTC.",
                                                           void]
    # A row whose round's plan went out, but by someone else's approval, for another request or for a plan code made
    # again before the send (the same terms and request id, a new digest), isn't that plan's own; and a plan with the
    # row's approver and request id that never went out wasn't sent.
    sent = story("1")["pushback"]
    sent_record = sent.records["dst_headphones"]
    (used,) = sent.approvals["dst_headphones"]
    assert approvals.plan_sha256(sent_record.rounds[0].plan) == used.plan_sha256  # the sent plan's own row matches
    for other in (replace(used, by="Sam Example"), replace(used, request_id="another-request"),
                  replace(used, plan_sha256="0" * 64)):
        lines = texts(views.approvals_block(replace(sent, approvals={"dst_headphones": (other,)}), sent_record))
        assert lines[2] == approvals.void_reason(sent_record, other) != w.APPROVAL_SENT_VOID
    first, *later = sent_record.rounds
    unsent = replace(sent_record, rounds=(replace(first, plan=replace(first.plan, sending_at=None)), *later))
    assert texts(views.approvals_block(sent, unsent))[2] == approvals.void_reason(unsent, used) == void


def test_each_try_to_send_names_its_round_and_a_taken_try_is_one_line():
    # A taken try's detail says what its outcome says, so it isn't said again on a line of its own. A refused or
    # retried try keeps Airwallex's own phrase under it, and so does duplicate_request. A try whose request id is no
    # round's plan's names no round.
    board = story("1")["won"]
    first, later = MONDAY + timedelta(minutes=1), THREE_WEEKS_LATER + timedelta(minutes=1)
    refused = "HTTP 400, validation_error: Dispute transition is not supported"
    tries = (SendLine(first, "taken", 200, airwallex.TOOK, 1), SendLine(later, "retry", 503, "HTTP 503", 2),
             SendLine(later, "failed", 400, refused, 2), SendLine(later, "taken", 400, airwallex.HAD_IT, 2),
             SendLine(later, "taken", 201, "airwallex took it.", None),
             SendLine(later, "retry", None, airwallex.NO_ANSWER, None), SendLine(later, "taken", 200, "", 2))
    shown = texts(views.sends_block(replace(board, sends={"dst_headphones": tries}), board.records["dst_headphones"]))
    assert shown == [
        w.SENDS_TITLE, "Round 1, Nov 2, 17:01 UTC: Airwallex took it (HTTP 200).",
        "Round 2, Nov 23, 17:01 UTC: no clear answer, so the same request goes again (HTTP 503).", "HTTP 503",
        "Round 2, Nov 23, 17:01 UTC: Airwallex refused it (HTTP 400).", refused,
        "Round 2, Nov 23, 17:01 UTC: Airwallex took it (HTTP 400).", "Airwallex already had this request",
        "Nov 23, 17:01 UTC: Airwallex took it (HTTP 201).",
        "Nov 23, 17:01 UTC: no clear answer, so the same request goes again.", "no answer in 30 seconds",
        "Round 2, Nov 23, 17:01 UTC: Airwallex took it (HTTP 200)."]
    assert w.OUTCOMES["taken"] == airwallex.TOOK  # the words a taken try's detail repeats


SANDBOX_NOTE = ("Demo note: the sandbox can't wait three weeks, so the simulated bank pushed back right after Monday's "
                "responses.")
STANDIN_NOTE = "Demo note: the stand-in can't wait three weeks, so its simulated bank pushed back seconds later."


@pytest.mark.parametrize("airwallex, note", [("sandbox", SANDBOX_NOTE), ("stand-in", STANDIN_NOTE),
                                             ("something else", STANDIN_NOTE)], ids=["sandbox", "stand-in", "unknown"])
def test_the_round_two_note_names_the_sandbox_only_on_a_recording_made_there(airwallex, note):
    # As the banner's rule goes: only a recording made on the sandbox gets the sandbox's words, and a kind this code
    # doesn't know never passes for one. The stand-in's bank pushed back about 14 seconds after Monday.
    board = replace(story("1")["pushback"], mode=Mode.DEMO, demo=demo_bar(airwallex=airwallex))
    shown = case_texts(board, "dst_headphones")
    assert shown.count(note) == 1  # round two's only
    assert [text for text in shown if "sandbox" in text] == ([note] if airwallex == "sandbox" else [])
    assert (w.NOTE_WEEKS, w.NOTE_WEEKS_STANDIN) == (SANDBOX_NOTE, STANDIN_NOTE)


def test_the_rfi_note_shows_on_a_fraud_case_at_rfi_in_the_demo():
    board = replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar())
    shown = case_texts(board, "dst_headphones")
    assert shown[shown.index("CE 3.0 is met, as far as the store's records show.") + 4] == w.NOTE_RFI
    assert w.NOTE_RFI not in case_texts(board, "dst_earbuds")


def test_current_gets_the_boards_profile_and_effort(monkeypatch):
    seen = []
    real = rounds.current

    def spy(*args, **options):
        seen.append((options["profile"], options["effort"]))
        return real(*args, **options)

    monkeypatch.setattr(rounds, "current", spy)
    profile = StoreProfile("Another Store", "Sam", "help@another-store.example")
    board = replace(story("1")["monday"], reader=ReaderInfo(profile, "medium", "claude", prompts.MODEL, None, None))
    views.case(board, "dst_headphones", csrf=CSRF)
    views.approval(board, "dst_headphones", csrf=CSRF)
    assert seen == [(profile, "medium"), (profile, "medium")]
    # The inbox lines count replies from the profile's support address too.
    assert "The store never replied to the customer." in case_texts(board, "dst_earbuds")


def every_sent_record():
    """Records whose round plan's first POST try is saved: sent, waiting, a person after a send, and closed."""
    found = []
    for ending in ("1", "2"):
        for step, board in story(ending).items():
            found += [(board, record) for record in board.records.values()
                      if record.rounds[-1].plan is not None and record.rounds[-1].plan.sending_at is not None]
    for state in ("held", "late refund", "other refunds"):
        record, now = STATES[state]()
        found.append((board_of([record], now=now), record))
    return found


def test_files_for_is_never_called_once_the_plan_went_out(monkeypatch):
    def refuse(*args, **options):
        raise AssertionError("files_for() after the first POST try")

    monkeypatch.setattr(rounds, "files_for", refuse)
    phases, shown_files = set(), 0
    for board, record in every_sent_record():
        phases.add(lifecycle.phase(record))
        files = views.case_files(board, record)
        views.case(board, record.dispute_id, csrf=CSRF)
        views.approval(board, record.dispute_id, csrf=CSRF)
        shown_files += len(files.documents)
    assert {Phase.SENT, Phase.WAIT, Phase.PERSON, Phase.CLOSED} <= phases
    assert shown_files >= 4  # the basis's files: round one's, then round two's warranty file


def test_files_for_builds_the_files_of_an_unsent_plan(monkeypatch):
    calls = []
    real = rounds.files_for
    monkeypatch.setattr(rounds, "files_for", lambda *args: calls.append(args) or real(*args))
    board = story("1")["monday"]
    files = views.case_files(board, board.records["dst_headphones"])
    assert len(calls) == 1 and [document.sha256[:8] for document in files.documents] == ["64631fdc"]
    assert views.case_files(board, board.records["dst_earbuds"]).sends is False  # an accept sends no file


def test_a_file_that_moved_offers_the_drop_in_a_live_run_only():
    board = story("1")["monday"]
    record = board.records["dst_headphones"]
    moved = replace(board, saved=rounds.MemorySaved())  # no basis on file: NO_BASIS
    shown = texts(views.files_block(moved, record, views.case_files(moved, record), csrf=CSRF))
    assert w.FILE_MOVED.format(why=rounds.NO_BASIS) in shown and w.DROP_BUTTON in shown
    drop = [item for item in views.files_block(moved, record, views.case_files(moved, record), csrf=CSRF).items
            if isinstance(item, Form)]
    assert drop[0].action == "/case/dst_headphones/drop" and drop[0].fields == (("csrf", CSRF), ("version", "1"))
    demo = replace(moved, mode=Mode.DEMO, demo=demo_bar())
    assert w.DROP_BUTTON not in texts(views.files_block(demo, record, views.case_files(demo, record), csrf=CSRF))
    assert views.approval(moved, "dst_headphones", csrf=CSRF).form is None  # nothing to approve that can't be built


def test_a_refused_response_keeps_its_file_for_a_person():
    board = story("1")["monday-approved"]
    record = board.records["dst_headphones"]
    record = lifecycle.observe(record, replace(record.last, at=MONDAY + timedelta(minutes=1)))
    record = lifecycle.sending(record, MONDAY + timedelta(minutes=1))
    record = lifecycle.send_failed(record, record.rounds[-1].plan.request_id, "validation_error: no.", MONDAY)
    board = replace(board, records={"dst_headphones": record})
    files = views.case_files(board, record)
    assert files.kept and [document.sha256[:8] for document in files.documents] == ["64631fdc"]
    shown = texts(views.files_block(board, record, files, csrf=CSRF))
    assert shown[1] == w.FILE_KEPT
    unread = replace(board, saved=frozen(board.saved, {("dst_headphones", 1): "{"}))  # its saved copy can't be read
    files = views.case_files(unread, record)
    assert (files.documents, files.kept, files.unreadable) == ((), False, NOT_JSON)  # no file to keep, and it says why
    assert texts(views.files_block(unread, record, files, csrf=CSRF))[1:] == [w.FILE_UNREADABLE.format(why=NOT_JSON)]


# ----------------------------------------------------------------------------- a saved row that won't load
def unreadable(step, dispute_id="dst_headphones", text="{"):
    """The story's board at a step, its saved rows as a page's snapshot reads them, with the case's newest basis row
    replaced by text that won't load: a hand edit, say, or a rounds.FORMAT change over an old database."""
    board = story("1")[step]
    record = board.records[dispute_id]
    return replace(board, saved=frozen(board.saved, {(dispute_id, len(record.rounds)): text}))


def test_the_frozen_rows_read_as_the_story_saved_them():
    board = story("1")["monday"]
    record = board.records["dst_headphones"]
    assert frozen(board.saved).basis("dst_headphones", 1) == board.saved.basis("dst_headphones", 1)
    assert views.case_files(replace(board, saved=frozen(board.saved)), record) == views.case_files(board, record)


@pytest.mark.parametrize("step", ["monday", "pushback"])
@pytest.mark.parametrize("text", ["{", json.dumps({"format": rounds.FORMAT + 1, "basis": {}})], ids=["not JSON", "a format"])
def test_a_plan_whose_basis_cant_be_read_offers_the_drop_and_no_approval(step, text):
    board = unreadable(step, text=text)
    record = board.records["dst_headphones"]
    why = NOT_JSON if text == "{" else f"This isn't a format {rounds.FORMAT} basis."
    assert lifecycle.phase(record) is Phase.APPROVE and views.case_files(board, record).moved == why
    for view in (views.case(board, "dst_headphones", csrf=CSRF), views.approval(board, "dst_headphones", csrf=CSRF)):
        shown = texts(*view.blocks)
        assert w.FILE_MOVED.format(why=why) in shown and w.ROUND_UNREADABLE.format(why=why) in shown
        assert w.EVIDENCE_BUG not in shown  # an unreadable row isn't a bug in code
        assert [(form.action, form.fields[1]) for form in forms_in(*view.blocks)] == [
            ("/case/dst_headphones/drop", ("version", "1"))]  # one way out: the drop, and no approval
    assert views.approval(board, "dst_headphones", csrf=CSRF).form is None
    demo = replace(board, mode=Mode.DEMO, demo=demo_bar())
    assert forms_in(*views.case(demo, "dst_headphones", csrf=CSRF).blocks) == []  # the demo has no drop


def test_an_accept_whose_basis_cant_be_read_offers_the_drop_too():
    board = unreadable("monday", "dst_earbuds")
    view = views.case(board, "dst_earbuds", csrf=CSRF)
    shown = texts(*view.blocks)
    assert w.ROUND_UNREADABLE.format(why=NOT_JSON) in shown and w.FILES_NONE in shown
    assert w.FILE_MOVED.format(why=NOT_JSON) not in shown  # an accept sends no file
    assert shown[shown.index(w.ROUND_UNREADABLE.format(why=NOT_JSON)) + 1] == w.DROP_OFFER
    assert [form.action for form in forms_in(*view.blocks)] == ["/case/dst_earbuds/drop"]
    assert views.approval(board, "dst_earbuds", csrf=CSRF).form is None
    status, refusal = views.refused(board, Result(False, "file-moved", NOT_JSON), "dst_earbuds", csrf=CSRF)
    assert status == 409 and texts(*refusal.lines)[:3] == [w.ROUND_UNREADABLE.format(why=NOT_JSON), w.DROP_OFFER,
                                                           w.DROP_BUTTON]  # never that an accept's file moved


@pytest.mark.parametrize("step", ["monday-sent", "won"])
def test_a_sent_plans_basis_that_cant_be_read_shows_no_file_and_says_why(step):
    board = unreadable(step)
    record = board.records["dst_headphones"]
    files = views.case_files(board, record)
    assert (files.documents, files.moved, files.unreadable) == ((), None, NOT_JSON)
    shown = texts(*views.case(board, "dst_headphones", csrf=CSRF).blocks)
    assert w.FILE_UNREADABLE.format(why=NOT_JSON) in shown and w.ROUND_UNREADABLE.format(why=NOT_JSON) in shown
    assert w.DROP_BUTTON not in shown and views.approval(board, "dst_headphones", csrf=CSRF).form is None


def unreadable_reading(step="monday", dispute_id="dst_headphones", text="{"):
    """The story's board at a step, its saved rows as a page's snapshot reads them, with the newest row of the reading
    the round's plan was made on replaced by text that won't load. Its basis loads, so the plan's file still builds."""
    board = story("1")[step]
    number = len(board.records[dispute_id].rounds)
    key = board.saved.basis(dispute_id, number).reading_key
    return replace(board, saved=frozen(board.saved, reading_rows={(dispute_id, number, key): text}))


def test_a_reading_that_cant_be_read_is_refused_as_a_saved_row_never_as_a_moved_file():
    board = unreadable_reading()
    record = board.records["dst_headphones"]
    files = views.case_files(board, record)
    assert files.moved is None and [document.sha256[:8] for document in files.documents] == ["64631fdc"]  # it builds
    status, refusal = views.refused(board, Result(False, "file-moved", READING_NOT_JSON), "dst_headphones", csrf=CSRF)
    assert status == 409 and texts(*refusal.lines)[:3] == [w.ROUND_UNREADABLE.format(why=READING_NOT_JSON), w.DROP_OFFER,
                                                           w.DROP_BUTTON]  # as the case page says it, never FILE_MOVED
    shown = texts(*views.case(board, "dst_headphones", csrf=CSRF).blocks)
    assert w.ROUND_UNREADABLE.format(why=READING_NOT_JSON) in shown
    assert w.FILE_HASH.format(sha256=files.documents[0].sha256) in shown  # the file as planned
    assert not [text for text in shown if text.startswith(w.FILE_MOVED.split("{")[0])]
    assert [form.action for form in forms_in(*views.case(board, "dst_headphones", csrf=CSRF).blocks)] == [
        "/case/dst_headphones/drop"]


def test_a_refusal_names_the_file_only_while_the_file_cant_be_built():
    board = story("1")["monday"]
    for why in (rounds.MOVED, NOT_JSON, READING_NOT_JSON):  # the Service's sentence, on a board whose file builds
        lines = texts(*views.refused(board, Result(False, "file-moved", why), "dst_headphones", csrf=CSRF)[1].lines)
        assert not [text for text in lines if text.startswith(w.FILE_MOVED.split("{")[0])], why
    moved = replace(board, store=edited(FERNHILL, LATER_DELIVERY))  # the delivery scan moved: the file can't be built
    lines = texts(*views.refused(moved, Result(False, "file-moved", rounds.MOVED), "dst_headphones", csrf=CSRF)[1].lines)
    assert lines[:3] == [w.FILE_MOVED.format(why=rounds.MOVED), w.DROP_OFFER, w.DROP_BUTTON]


def test_a_reading_that_cant_be_read_and_a_file_that_moved_are_refused_with_the_files_own_reason():
    # Both faults at once. The Service's sentence is the reading's, but the refusal names the file with its own reason,
    # the pair the case page shows: the reading's sentence after FILE_MOVED would read as why the file moved.
    board = replace(unreadable_reading(), store=edited(FERNHILL, LATER_DELIVERY))  # and the delivery scan moved
    assert views.case_files(board, board.records["dst_headphones"]).moved == rounds.MOVED
    shown = case_texts(board, "dst_headphones")
    assert w.ROUND_UNREADABLE.format(why=READING_NOT_JSON) in shown and w.FILE_MOVED.format(why=rounds.MOVED) in shown
    status, refusal = views.refused(board, Result(False, "file-moved", READING_NOT_JSON), "dst_headphones", csrf=CSRF)
    lines = texts(*refusal.lines)
    assert status == 409 and lines[:3] == [w.FILE_MOVED.format(why=rounds.MOVED), w.DROP_OFFER, w.DROP_BUTTON]
    assert not [text for text in lines if READING_NOT_JSON in text]


@pytest.mark.parametrize("build, dispute_id", [
    (lambda: unreadable("monday"), "dst_headphones"), (lambda: unreadable("monday", "dst_earbuds"), "dst_earbuds"),
    (lambda: unreadable("monday-sent"), "dst_headphones"), (lambda: unreadable("pushback"), "dst_headphones"),
    (unreadable_reading, "dst_headphones")],
    ids=["a challenge's basis", "an accept's basis", "a sent plan's basis", "round two's basis", "a reading"])
def test_a_round_whose_saved_row_cant_be_read_never_says_claude_didnt_read_it(build, dispute_id):
    board = build()
    record = board.records[dispute_id]
    number = len(record.rounds)
    assert [reading for where, at, reading in story("1")["won"].saved.readings if (where, at) == (dispute_id, number)]
    (block,) = [block for block in views.case(board, dispute_id, csrf=CSRF).blocks if block.title == w.CLAUDE]
    assert texts(block) == [w.CLAUDE, w.CLAUDE_CANT_SHOW]  # a reading is on file: the page can't show it
    beside = [block for block in views.approval(board, dispute_id, csrf=CSRF).blocks if block.title == w.READING_BESIDE]
    assert [texts(block) for block in beside] == (
        [[w.READING_BESIDE, w.CLAUDE_CANT_SHOW]] if lifecycle.phase(record) is Phase.APPROVE else [])
    assert w.CLAUDE_CANT_SHOW == "This page can't show what Claude read for this round."


@pytest.mark.parametrize("error, line", [(rules.NoRules("No rule set covers this claim."), w.EVIDENCE_ERROR.format(
    why="No rule set covers this claim.")), (KeyError("a bug"), w.EVIDENCE_BUG)], ids=["no rules", "a bug"])
def test_a_round_code_cant_work_out_never_says_claude_didnt_read_it(monkeypatch, error, line):
    def fails(*args, **options):
        raise error

    monkeypatch.setattr(rounds, "current", fails)  # the earbuds' round was read: its reading is on file
    shown = case_texts(story("1")["monday"], "dst_earbuds")
    assert line in shown and shown[shown.index(w.CLAUDE) + 1] == w.CLAUDE_CANT_SHOW and w.CLAUDE_NOT_READ not in shown


def doesnt_fit(step, dispute_id):
    """The round's basis as a row of this format whose fields don't fit: a Basis field added in code with no FORMAT
    bump, read against an old database. jsonable's own sentence for it names the fields in code's terms."""
    board = story("1")[step]
    data = json.loads(rounds.basis_to_json(board.saved.basis(dispute_id, len(board.records[dispute_id].rounds))))
    del data["basis"]["ce3_reasons"]
    return unreadable(step, dispute_id, json.dumps(data))


@pytest.mark.parametrize("build, dispute_id, files", [
    (lambda: doesnt_fit("monday", "dst_headphones"), "dst_headphones", "FILE_MOVED"),
    (lambda: doesnt_fit("monday", "dst_earbuds"), "dst_earbuds", None),  # an accept sends no file
    (lambda: doesnt_fit("monday-sent", "dst_headphones"), "dst_headphones", "FILE_UNREADABLE"),
    (lambda: unreadable_reading(text=json.dumps({"format": reader.FORMAT, "reading": {}})), "dst_headphones", None)],
    ids=["a challenge's basis", "an accept's basis", "a sent plan's basis", "a reading"])
def test_a_saved_row_in_another_form_shows_a_plain_sentence_and_logs_the_detail(build, dispute_id, files, caplog):
    board = build()
    with caplog.at_level("WARNING", logger="disputable.web"):
        shown = case_texts(board, dispute_id) + approve_texts(board, dispute_id)
    for line in (w.ROUND_UNREADABLE, *([getattr(w, files)] if files else [])):  # and the files block's line
        assert line.format(why=w.ROW_FORM) in shown, line
    assert not [text for text in shown if "doesn't fit" in text or "ce3_reasons" in text]  # jsonable's words: the log's
    logged = [record.getMessage() for record in caplog.records if record.name == "disputable.web"]
    assert logged and all(dispute_id in message and "doesn't fit" in message for message in logged)
    assert w.ROW_FORM == "A saved row of this round isn't in the form this app reads."


def test_a_refusal_never_shows_a_saved_rows_sentence_in_codes_terms(caplog):
    board = unreadable("monday", "dst_earbuds")
    raw = "the basis doesn't fit Basis: missing ['ce3_reasons'], not expected []."
    with caplog.at_level("WARNING", logger="disputable.web"):
        lines = texts(*views.refused(board, Result(False, "file-moved", raw), "dst_earbuds", csrf=CSRF)[1].lines)
    assert lines[0] == w.ROUND_UNREADABLE.format(why=w.ROW_FORM) and raw not in lines
    assert [record for record in caplog.records if record.name == "disputable.web" and raw in record.getMessage()]


def test_rounds_and_readers_own_sentences_show_as_written():
    # Every sentence rounds.basis_from_json() and reader.from_json() write for a row that won't load is plain, so the
    # pages show it as written. Each is made here by the loaders themselves, so a sentence they change fails this test.
    board = story("1")["monday"]
    basis = json.loads(rounds.basis_to_json(board.saved.basis("dst_headphones", 1)))
    nameless, failing = json.loads(json.dumps(basis)), json.loads(json.dumps(basis))
    nameless["basis"]["files"][0]["title"] = ""  # a file its own checks refuse
    failing["basis"]["failure"] = "SOMETHING_NEW"
    rows = [(rounds.basis_from_json, text) for text in (
        "{", json.dumps({"format": rounds.FORMAT + 1, "basis": {}}), json.dumps(nameless), json.dumps(failing))]
    rows += [(reader.from_json, text) for text in ("{", json.dumps({"format": reader.FORMAT + 1, "reading": {}}))]
    found = set()
    for loader, text in rows:
        with pytest.raises(Unloadable) as raised:
            loader(text)
        assert views.unreadable_why(raised.value, "dst_headphones") == str(raised.value)
        found.add(str(raised.value))
    assert found == views.OWN_UNLOADABLE


def test_claude_view_of_a_failed_reading_and_one_with_nothing_kept():
    board = story("1")["monday"]
    assert texts(*views.claude_view(failed("key", Failure.NO_KEY), board.store)) == [
        w.CLAUDE_FAILED.format(why=WHY[Failure.NO_KEY])]
    reading = board.saved.reading("dst_earbuds", 1, rounds.current(board.records["dst_earbuds"], board.store,
                                                                   board.saved, board.now).reading.key)
    empty = replace(reading, rows=(), reported=(), draft=None, complete=False)
    assert texts(*views.claude_view(empty, board.store)) == [w.CLAUDE_PARTIAL, w.CLAUDE_INTRO, w.CLAUDE_NOTHING]


def test_claude_view_counts_what_code_dropped_and_never_shows_those_quotes():
    reading = read_case("trick_system_note", "obey").saved.readings[-1][2]
    store = read_case("trick_system_note", "obey").store
    shown = texts(*views.claude_view(reading, store))
    dropped = [row for row in reading.rows if not row.kept]
    assert dropped and w.CLAUDE_DROPPED.format(items=w.count(len(dropped), "item")) in shown
    for row in dropped:
        assert row.why in shown
        assert row.quote not in shown or any(row.quote == kept.quote for kept in reading.rows if kept.kept)
    assert not any(row.note and row.note in shown for row in reading.rows)  # Claude's row notes never show


def test_ear_tips_were_decided_without_claude():
    shown = case_texts(story("1")["monday"], "dst_ear_tips")
    assert w.CLAUDE_NOT_ASKED in shown and w.FOUND_NONE in shown
    # No rule read the records, but code still counted the inbox: so that line comes first, and it says only that.
    assert shown[shown.index(w.FOUND) + 1:shown.index(w.FOUND) + 4] == [w.FOUND_NONE, w.INBOX, inbox.M0]
    assert w.FOUND_NONE == "Code's rules didn't read the store's records for this round."


def test_a_round_whose_plan_was_dropped_says_what_code_and_claude_read_was_for_that_plan():
    # Code read the round, kept a plan and saved its basis and Claude's reading; then someone else responded first and
    # the plan was dropped. Those rows stay on file, so the page never says the rules or Claude didn't read the round.
    board = read_then_replied()
    record = board.records["dst_headphones"]
    assert lifecycle.phase(record) is Phase.WAIT and record.rounds[-1].plan is None
    assert board.saved.basis("dst_headphones", 1).plan_sha256 and [
        reading for where, number, reading in board.saved.readings if (where, number) == ("dst_headphones", 1)]
    shown = case_texts(board, "dst_headphones")
    found = shown.index(w.FOUND)
    assert shown[found + 1:found + 4] == [w.FOUND_DROPPED, w.INBOX, inbox.M0]  # nothing more: the findings were its
    assert shown[shown.index(w.CLAUDE) + 1] == w.CLAUDE_DROPPED_PLAN
    assert w.FOUND_NONE not in shown and w.CLAUDE_NOT_READ not in shown
    assert w.FOUND_DROPPED == "Code's findings for this round were for a plan that was dropped, so they aren't shown."
    assert w.CLAUDE_DROPPED_PLAN == ("The reading on file for this round was for a plan that was dropped, so it isn't "
                                     "shown.")
    # With no saved rows, as lifecycle's own states carry none, the round was never read here: the lines say so.
    alone, now = STATES["someone else replied"]()
    shown = case_texts(board_of([alone], now=now), "dst_headphones")
    assert w.FOUND_NONE in shown and w.CLAUDE_NOT_READ in shown and w.FOUND_DROPPED not in shown


def test_a_round_at_decide_with_no_reading_yet():
    record = read_case("trick_system_note", None).records["dst_trick_system_note"]
    shown = case_texts(read_case("trick_system_note", None), "dst_trick_system_note")
    assert lifecycle.phase(record) is Phase.DECIDE
    assert w.NOT_DECIDED in shown and w.CLAUDE_NOT_YET in shown and w.NO_PLAN_DECIDE in shown


def test_a_broken_record_has_a_page_that_says_so():
    board = board_of({}, broken={"dst_gone": "The saved record isn't valid JSON."})
    view = views.case(board, "dst_gone", csrf=CSRF)
    assert texts(*view.blocks) == [w.NOW_TITLE, w.BROKEN_RECORD.format(why="The saved record isn't valid JSON."),
                                   w.BROKEN_OUT]
    assert views.case(board, "dst_nobody", csrf=CSRF) is None and views.approval(board, "dst_nobody", csrf=CSRF) is None


def test_the_store_records_are_cut_at_20000_characters():
    order = FERNHILL.order("1121")
    long = replace(order, shipping_address="x" * 20_005)
    view = views.record_view(long, FERNHILL)
    cut = [line for line in view.items if isinstance(line, Line) and line.text.startswith("x")]
    assert len(cut[0].text) == 20_000
    assert view.items[view.items.index(cut[0]) + 1].text == "[cut: 5 more characters]"


def test_an_orders_items_show_each_part_under_its_own_name():
    view = views.record_view(FERNHILL.order("1109"), FERNHILL)
    lines = [item for item in view.items if isinstance(item, Line)]
    assert "items" not in [line.source for line in lines]  # never four values under one name
    (item,) = [block for block in view.items if isinstance(block, Block)]
    assert item.title == "Item 1" and [(line.source, line.text, line.voice) for line in item.items] == [
        ("SKU", "FH-HP-03", Voice.RECORD), ("description", "Over-ear studio headphones", Voice.RECORD),
        ("unit price", "1249", Voice.RECORD), ("quantity", "1", Voice.RECORD),
        ("serial number", "FH-HP-03-55821", Voice.RECORD)]
    assert ("3DS", "False") in [(line.source, line.text) for line in lines]  # as the store holds it, the record's words
    refunded = replace(FERNHILL.order("1121"), refunds=(Refund(Decimal("12"), MONDAY),))
    (refund,) = [block for block in views.record_view(refunded, FERNHILL).items
                 if isinstance(block, Block) and block.title.startswith("Refund")]
    assert refund.title == "Refund 1" and [line.source for line in refund.items] == ["amount", "time"]


def test_an_empty_value_shows_no_record_line():
    for scan in FERNHILL.get_carrier_scans("1121"):
        view = views.record_view(scan, FERNHILL)
        assert all(line.text.strip() for line in view.items if isinstance(line, Line) and line.voice is Voice.RECORD)
        assert ("note" in [line.source for line in view.items]) is bool(scan.note)  # "Left at front door" only


# ----------------------------------------------------------------------------- the approval screen
def approve_texts(board, dispute_id):
    view = views.approval(board, dispute_id, csrf=CSRF)
    return texts(*view.lead, *view.blocks, *view.closing, *([view.form] if view.form else []))


def test_the_headphones_approval_screen():
    board = story("1")["monday"]
    view = views.approval(board, "dst_headphones", csrf=CSRF)
    shown = approve_texts(board, "dst_headphones")
    assert view.title == "Approve order #1109"
    assert shown[:2] == ["One approval sends exactly this: challenge with 1 file. Any change voids it.",
                         "Approving it books a review: $16.00 of your time."]
    assert w.SENDS in shown and w.FRESH in shown and w.AIRWALLEX_ADDS in shown and w.SENDS_ONLY in shown
    # 8 quotes from 5 records: each quote is a line, and several can come from one record.
    assert "Claude quoted 8 lines from 5 records, raised 0 points and flagged 0 lines. Code dropped 0 items." in shown
    assert view.form.note.text == "You approve as Priya." and view.form.button == w.APPROVE_BUTTON
    fields = dict(view.form.fields)
    assert set(fields) == {"csrf", "plan", "reading"} and fields["plan"].isascii()
    step = rounds.current(board.records["dst_headphones"], board.store, board.saved, board.now)
    assert fields["reading"] == step.reading.key


def test_the_review_line_for_an_accept_code_made_itself():
    board = story("2")["pushback"]
    record = board.records["dst_headphones"]
    assert lifecycle.phase(record) is Phase.APPROVE and record.rounds[-1].plan.terms.action is Action.ACCEPT
    assert views.review_line(record) == w.REVIEW_NONE
    assert w.AIRWALLEX_ADDS not in approve_texts(board, "dst_headphones")
    assert w.SENDS_ONLY not in approve_texts(board, "dst_headphones")


@pytest.mark.parametrize("step", ["monday", "pushback"])
def test_a_challenges_screen_says_what_airwallex_adds_and_what_else_this_app_sends(step):
    closing = texts(*views.approval(story("1")[step], "dst_headphones", csrf=CSRF).closing)
    assert closing[-2:] == [w.AIRWALLEX_ADDS, w.SENDS_ONLY]
    assert w.AIRWALLEX_ADDS == ("Airwallex adds a summary file of its own to every challenge. It may also fill in the "
                                "store's profile and the order's number, currency and date. At a chargeback it may add an "
                                "IP address and a device id that this app never sends.")  # fixtures/spike/066, 073; Ending 1


def test_the_fields_the_screen_names_are_the_ones_a_challenge_sends():
    # Round two is at the chargeback, so the product type goes too. Beyond the terms and the files the page shows, the
    # body may hold only what the sentence names: a new field in it needs the sentence changed too.
    board = story("1")["pushback"]
    record = board.records["dst_headphones"]
    files = rounds.files_for(record, board.store, board.saved)
    body = airwallex.challenge_body(record.rounds[-1].plan, [(document, "a-made-up-file-id") for document in files])
    shown = {"request_id", "reason", "supporting_documents"}  # the request id, the reason and the files are the terms'
    named = {"challenged_by": "this app's name", "challenge_method": "the standard challenge method",
             "product_type": "the product type"}
    assert set(body) == airwallex.CHALLENGE_KEYS == shown | set(named)
    assert (body["challenged_by"], body["challenge_method"], body["product_type"]) == (
        lifecycle.ACTOR, "STANDARD", "PHYSICAL_GOODS")
    (entry,) = body["supporting_documents"]["documents"]
    assert entry == {"type": "PRIMARY", "file_ids": ["a-made-up-file-id"], "description": files[0].description}
    # The upload sends each file's name and its notes: with the description, the file's name, type and descriptions.
    assert files[0].filename and files[0].notes and files[0].description
    for words in (*named.values(), "each file's name, type and short descriptions", "at a chargeback"):
        assert words in w.SENDS_ONLY, words
    assert w.SENDS_ONLY.startswith("Besides the terms and files shown here, this app sends only fixed fields: ")


def test_the_review_line_is_what_approve_books_on_every_plan_of_the_walk():
    waited = 0
    for label in READ:
        board = read_case(label, "perfect")
        record = only(board)
        if lifecycle.phase(record) is not Phase.APPROVE:
            continue
        waited += 1
        after = lifecycle.approve(record, record.rounds[-1].plan, "Priya", board.now)
        booked = len(after.bookings) > len(record.bookings)
        assert (views.review_line(record) == w.REVIEW_BOOKS.format(cost="$16.00")) is booked, label
        assert (views.review_line(record) == w.REVIEW_NONE) is not booked, label
    assert waited == 74


def test_a_review_already_booked_this_round():
    board = story("1")["monday"]
    record = board.records["dst_headphones"]
    approved = lifecycle.approve(record, record.rounds[-1].plan, "Priya", board.now)
    dropped = lifecycle.drop(approved, "its file couldn't be built again exactly as planned")
    again = lifecycle.keep(dropped, evidence_of(board, record), board.now, round_number=1)
    assert lifecycle.phase(again) is Phase.APPROVE and views.review_line(again) == w.REVIEW_DONE


def evidence_of(board, record):
    """The round's evidence, merged from its saved reading, as decide_round() merges it."""
    step = rounds.current(record, board.store, board.saved, board.now)
    return rounds.evidence_for(board.store, lifecycle.as_case(record), lifecycle.as_dispute(record), step.reading,
                               board.now)[0]


def test_nothing_to_approve_and_a_person_responds():
    board = story("1")["monday-sent"]
    view = views.approval(board, "dst_headphones", csrf=CSRF)
    assert view.form is None and texts(*view.blocks)[1:3] == [w.NOTHING_TO_APPROVE, w.NOW["WAIT_OURS"]]
    record, now = STATES["late refund"]()
    person = replace(record, rounds=(replace(record.rounds[0], plan=None),))
    shown = approve_texts(board_of([person], now=now), record.dispute_id)
    assert w.PERSON_RESPONDS in shown


def test_approvals_are_off_without_an_approver():
    board = replace(story("1")["monday"], approver=None)
    view = views.approval(board, "dst_headphones", csrf=CSRF)
    assert view.form is None and w.APPROVALS_OFF in texts(*view.closing)


# ----------------------------------------------------------------------------- money
@pytest.mark.parametrize("ending, step, cost, risk", [
    ("1", "monday", "15.00", "1450.00"), ("1", "monday-approved", "47.00", "1450.00"),
    ("1", "monday-sent", "248.30", "1249.00"), ("1", "pushback", "263.30", "1249.00"),
    ("1", "pushback-approved", "279.30", "1249.00"), ("1", "deciding", "279.30", "1249.00"),
    ("1", "won", "279.30", "0"), ("2", "pushback-approved", "263.30", "1249.00"), ("2", "accepted", "1512.30", "0"),
])
def test_the_money_at_each_step_of_the_story(ending, step, cost, risk):
    total = money.totals(story(ending)[step].records.values())
    assert (total.cost, total.at_risk, total.accept_everything) == (Decimal(cost), Decimal(risk), Decimal("1465.60"))


def test_accepting_everything_matches_the_story_that_accepts_everything():
    everything = play(accept_everything=True)
    records = story("1")["monday"].records
    for dispute_id, record in records.items():
        assert money.accept_everything(record) == lifecycle.cost(everything.records[dispute_id]), dispute_id
    assert [money.accept_everything(records[f"dst_{case}"]) for case in ("ear_tips", "earbuds", "headphones")] == [
        Decimal("12.30"), Decimal("204.00"), Decimal("1249.30")]
    assert money.totals(records.values()).accept_everything == Decimal("1465.60")
    assert sum(lifecycle.cost(record) for record in everything.records.values()) == Decimal("1465.60")


def test_the_money_page_at_the_end_of_ending_one():
    view = views.money_view(story("1")["won"])
    assert texts(*view.summary) == ["Cost so far: $279.30. Still at risk: $0.00.",
                                    "Accepting everything at once would have cost $1,465.60.",
                                    "This queue cost $1,186.30 less than that."]
    assert view.table.columns == w.COLUMNS
    assert [row[0][0].text for row in view.table.rows] == ["Order #1121", "Order #1098", "Order #1109"]
    headphones = view.table.rows[2]
    assert [line.text for line in headphones[2]] == ["A person's review: $16.00", "Chargeback fee: $15.00",
                                                     "A person's review: $16.00"]
    assert (headphones[3][0].text, headphones[4][0].text, headphones[5][0].text) == ("$47.00", "$0.00", "$1,249.30")
    notes = texts(view.notes)
    assert notes[1] == ("Fees from fees-v1: chargeback fee $15.00, refund fee $0.30, a person's review $16.00, later-stage "
                        "fees $250.00 (an assumption).")
    assert w.AT_RISK_NOTE in notes and w.ACCEPT_NOTE in notes and w.NOTE_FEE.format(later="$250.00") not in notes


def test_the_money_page_while_cases_are_open_and_in_the_demo():
    board = replace(story("2")["monday"], mode=Mode.DEMO, demo=demo_bar())
    view = views.money_view(board)
    assert texts(*view.summary)[-1] == w.NOT_FINAL
    notes = texts(view.notes)
    assert w.NOTE_FEE.format(later="$250.00") in notes
    assert w.NOTE_CHANCES.format(chance="45%", version="fees-v1") in notes
    ended = replace(story("2")["accepted"], mode=Mode.DEMO, demo=demo_bar(finished=True, coming=None,
                                                                          total=Decimal("1512.30")))
    summary = texts(*views.money_view(ended).summary)
    assert summary[-1] == "The recording's own total for this ending: $1,512.30."
    assert summary[2] == "This queue cost $46.70 more than that."
    assert not any("1,778" in text or "279.30" in text for text in summary + texts(views.money_view(ended).notes))


def test_a_case_first_seen_past_the_chargeback_cant_be_priced():
    record = story("1")["monday"].records["dst_earbuds"]
    later = replace(record, rounds=(replace(record.rounds[0], stage=lifecycle.Stage.PRE_ARBITRATION),))
    assert money.accept_everything(later) is None
    total = money.totals([later])
    assert total.unpriced == (lifecycle.Stage.PRE_ARBITRATION,) and total.accept_everything == 0
    view = views.money_view(board_of([later]))
    assert view.table.rows[0][5][0].text == w.CANT_SAY
    assert w.CANT_SAY_NOTE.format(stage="Pre-arbitration") in texts(view.notes)


def test_history_only_records_are_never_in_the_totals():
    old, _ = STATES["history-only"]()
    assert money.handled([old]) == [] and money.totals([old]).cost == 0
    assert views.money_view(board_of([old])).table is None


# ----------------------------------------------------------------------------- times and amounts
def test_times_round_down():
    now = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
    assert views.left(now + timedelta(days=2, hours=23, minutes=59), now) == "in 2 days"
    assert views.left(now + timedelta(hours=23, minutes=59), now) == "in 23 hours"
    assert views.left(now + timedelta(minutes=59), now) == "in under an hour"
    assert views.left(now - timedelta(days=1, hours=1), now) == "1 day ago"
    assert views.when(datetime(2026, 11, 9, 14, 59, 59, tzinfo=timezone.utc), now) == "Nov 9, 14:59 UTC"
    assert views.when(datetime(2025, 11, 24, 10, 30, tzinfo=timezone.utc), now) == "Nov 24, 2025, 10:30 UTC"
    assert views.when(MONDAY, now) == "Nov 2, 17:00 UTC"  # shown in UTC, whatever zone it came in
    assert views.day(datetime(2026, 10, 5, 23, 30, tzinfo=timezone(timedelta(hours=-8))), now) == "Oct 6"


def test_spans_count_as_approvals_counts():
    for minutes in (0, 1, 59, 60, 61, 119, 120, 23 * 60 + 59, 24 * 60, 24 * 60 + 1, 47 * 60 + 59, 48 * 60, 49 * 60 + 1,
                    10 * 24 * 60 + 5):
        length = timedelta(minutes=minutes, seconds=30)
        assert views.span(length) == approvals._span(length), minutes


def test_an_amount_that_isnt_a_number_is_unclear():
    assert views.amount(Decimal("NaN")) == views.amount(Decimal("Infinity")) == views.amount(Decimal("sNaN")) == w.UNCLEAR
    assert views.amount(dollars("1249")) == "$1,249.00" and views.amount(dollars("1249"), "EUR") == "1,249.00 EUR"
    assert views.amount(dollars("-12.3")) == "-$12.30"
    record = story("1")["monday"].records["dst_ear_tips"]
    plan = record.rounds[0].plan
    odd = replace(plan, terms=replace(plan.terms, refund_amount=Decimal("NaN")))
    assert "Refund: unclear" in texts(views.terms_block(story("1")["monday"], odd, open=True))


def test_an_unknown_claim_and_stage_are_named_by_code():
    assert views.claim("12.6") == "Claim 12.6" and views.claim("") == "Claim unclear"
    assert views.reason_text("OTHER_REASONS") == "OTHER_REASONS (other reasons)"
    assert views.reason_text("SOMETHING_NEW") == "SOMETHING_NEW" and views.reason_text("<b>") == w.UNCLEAR


# ----------------------------------------------------------------------------- the banner and the demo bar
@pytest.mark.parametrize("reader, line", [
    (RECORDED, "Claude's readings were recorded from claude-opus-5-5 on Nov 2, 2026."),
    (replace(RECORDED, last_read=MONDAY.date() + timedelta(days=2)),
     "Claude's readings were recorded from claude-opus-5-5 between Nov 2, 2026 and Nov 4, 2026."),
    (CLAUDE, "Claude's readings were recorded from claude-opus-5-5."),
    (replace(CLAUDE, source="none", model=""), w.READ_NONE),
    (replace(CLAUDE, source="scripted"), w.READ_SCRIPTED),
    (replace(CLAUDE, source="something else"), w.READ_SCRIPTED),
], ids=["recorded", "over days", "undated", "none", "scripted", "unknown"])
def test_the_demo_banner_says_whose_readings_these_are(reader, line):
    board = replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar(), reader=reader)
    banner = views.banner(board)
    assert banner.title == w.DEMO and [item.text for item in banner.lines] == [
        "This replays a run recorded on the Airwallex sandbox on Nov 2, 2026. Nothing goes to Airwallex or to Claude, "
        "and no keys are needed.", w.MADE_UP, line,
        "Clock: Nov 2, 17:00 UTC, the recording's time. Story time: Monday 9:00."]


@pytest.mark.parametrize("airwallex", ["stand-in", "something else"])
def test_the_demo_banner_of_a_stand_in_says_it_isnt_a_recording_of_airwallex(airwallex):
    """A run on the tests' stand-in, or one of a kind this code doesn't know, never passes for the sandbox's. The
    readings line is as for the sandbox's."""
    for reader, line in ((RECORDED, "Claude's readings were recorded from claude-opus-5-5 on Nov 2, 2026."),
                         (replace(CLAUDE, source="none", model=""), w.READ_NONE)):
        board = replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar(airwallex=airwallex), reader=reader)
        banner = views.banner(board)
        assert banner.title == w.DEMO and [item.text for item in banner.lines] == [
            "This replays a run on a stand-in for the Airwallex sandbox, built into this app's tests. It isn't a "
            "recording of Airwallex. Nothing goes to Airwallex or to Claude, and no keys are needed.", w.MADE_UP, line,
            "Clock: Nov 2, 17:00 UTC, the recording's time. Story time: Monday 9:00."]
        assert not [item for item in banner.lines if "recorded on the Airwallex sandbox" in item.text]
    assert w.DEMO_STANDIN == views.replays_line(demo_bar(airwallex="stand-in"))


def test_the_banner_names_the_story_time_as_a_label_and_the_start_once():
    played = (*demo_bar().played, Beat("Three weeks later", "The bank turns down the evidence.", "bank"))
    board = replace(story("1")["pushback"], mode=Mode.DEMO, demo=demo_bar(played=played, pending=()), reader=RECORDED)
    assert views.banner(board).lines[-1].text == (f"Clock: {views.when(board.now, board.now)}, the recording's time. "
                                                  "Story time: Three weeks later.")  # never a capital mid-sentence
    start = board_of({}, mode=Mode.DEMO, demo=demo_bar(played=(), pending=()), reader=RECORDED)
    page = views.page(start, w.QUEUE_TITLE, csrf=CSRF)
    assert page.banner.lines[-1].text == "Clock: Nov 2, 17:00 UTC, the recording's time."
    assert [line.text for line in (*page.banner.lines, *page.bar.lines)].count(w.NOT_STARTED) == 1  # the bar says it


@pytest.mark.parametrize("reader, line", [
    (CLAUDE, "Claude (claude-opus-5-5, effort high) reads the records when a fight could pay."),
    (replace(CLAUDE, source="none", model=""), w.LIVE_NO_KEY), (replace(CLAUDE, source="scripted"), w.READ_SCRIPTED)])
def test_the_live_banner(reader, line):
    board = replace(story("1")["monday"], reader=reader, last_pass=MONDAY - timedelta(minutes=20))
    assert [item.text for item in views.banner(board).lines] == [
        w.LIVE_READS, w.LIVE_LOOP, line, "Clock: Nov 2, 17:00 UTC.", "Last pass: Nov 2, 16:40 UTC."]
    assert views.banner(replace(board, last_pass=None)).lines[-1].text == w.NO_PASS


def test_the_clock_is_the_boards_never_the_wall_clock():
    later = THREE_WEEKS_LATER + timedelta(days=400)
    board = replace(story("1")["monday"], now=later, mode=Mode.DEMO, demo=demo_bar())
    assert views.banner(board).lines[-1].text.startswith(f"Clock: {views.when(later, later)}, the recording's time.")


def bar_texts(bar):
    return [line.text for line in bar.lines]


def test_the_demo_bar_with_approvals_pending():
    bar = views.demo_bar(demo_bar(), CSRF)
    assert bar.title == "Ending 1: the warranty record"
    assert bar_texts(bar) == ["Step 2: Monday 9:00.", "The agent reads the queue.",
                              "Priya approved order #1098 and order #1109 in the recording. Approve them on their pages, "
                              "or press Next. Each press approves one of them as Priya."]  # Next plays one beat a press
    assert [(link.text, link.href) for link in bar.links] == [("Order #1098", "/case/dst_earbuds/approve"),
                                                              ("Order #1109", "/case/dst_headphones/approve")]
    nxt, again, other = bar.buttons
    assert nxt.button == "Next: Monday 9:05: Priya approves code's draft on order #1098."
    assert (nxt.action, nxt.fields) == ("/demo/next", (("csrf", CSRF), ("played", "2")))
    assert (again.button, again.fields) == ("Start Ending 1 again", (("csrf", CSRF), ("ending", "1")))
    assert (other.button, other.fields) == ("Start Ending 2", (("csrf", CSRF), ("ending", "2")))


def test_the_demo_bar_with_one_approval_pending():
    bar = views.demo_bar(demo_bar(pending=(("dst_headphones", "Order #1109"),)), CSRF)
    assert bar_texts(bar)[2] == ("Priya approved order #1109 in the recording. Approve it on its page, or press Next and "
                                 "the demo approves it as Priya.")
    assert [(link.text, link.href) for link in bar.links] == [("Order #1109", "/case/dst_headphones/approve")]


def test_the_demo_bar_counts_the_steps_once_the_desk_names_them():
    class Counted:  # a DemoBar that names its beat count, as the spec's bar line wants
        def __init__(self, bar):
            self.__dict__.update(vars(bar), count=13)

    bar = views.demo_bar(Counted(demo_bar(pending=())), CSRF)
    assert bar_texts(bar)[0] == "Step 2 of 13: Monday 9:00."


def test_the_demo_bar_before_the_story_and_with_one_ending():
    bar = views.demo_bar(demo_bar(played=(), pending=(), endings=("1",),
                                  coming=Beat("Monday 9:00", "The bank opens three disputes.", "bank")), CSRF)
    assert bar_texts(bar) == [w.NOT_STARTED]
    assert [button.button for button in bar.buttons] == ["Next: Monday 9:00: The bank opens three disputes.",
                                                         "Start Ending 1 again"]
    assert bar.buttons[0].fields[1] == ("played", "0")


def test_the_demo_bar_at_the_end():
    bar = views.demo_bar(demo_bar(pending=(), coming=None, finished=True, total=Decimal("279.30"), left_unused=2), CSRF)
    assert bar_texts(bar)[2:] == ["The story ends. This ending cost $279.30.",
                                  "The recording holds 2 calls this replay never made."]
    assert [button.button for button in bar.buttons] == ["Start Ending 1 again", "Start Ending 2"]
    one = views.demo_bar(demo_bar(pending=(), coming=None, finished=True, total=None, left_unused=1), CSRF)
    assert bar_texts(one)[2:] == [w.END_PLAIN, "The recording holds 1 call this replay never made."]


AGAIN_STANDIN = ("Make the stand-in again: uv run python scripts/record_standin.py (add --no-claude if it says the Claude "
                 "readings aren't there).")  # as recording.AGAIN_STANDIN says it


@pytest.mark.parametrize("airwallex, then", [
    ("sandbox", "Record the story again."), ("stand-in", AGAIN_STANDIN), ("something else", AGAIN_STANDIN)],
    ids=["sandbox", "stand-in", "unknown"])
def test_the_demo_bar_after_a_mismatch_has_no_next(airwallex, then):
    # A recording made on the stand-in is made again by its maker: the sandbox may not be able to record the story.
    why = "Order #1109: the recording holds no reading for this case as the code reads it now."
    bar = views.demo_bar(demo_bar(mismatch=why, airwallex=airwallex), CSRF)
    assert bar_texts(bar)[2] == (f"The replay no longer matches the recording: {why} The code changed after the recording "
                                 f"was made. {then}")
    assert bar.links == () and [button.action for button in bar.buttons] == ["/demo/restart", "/demo/restart"]
    assert w.MISMATCH_STANDIN.endswith(AGAIN_STANDIN)


def test_a_done_key_shows_its_sentence_and_an_unknown_one_nothing():
    live, demo = story("1")["monday"], replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar())
    assert views.done_line(live, "approved") == w.DONE["approved"]
    assert views.done_line(demo, "approved") == "You approved it as Priya did in the recording. Press Next to go on."
    assert views.done_line(live, "<script>") == views.done_line(live, "booked") == ""
    # In the demo the approval may be Next's, made as Priya while the judge's tab was open: never that the judge made it.
    assert views.done_line(demo, "already") == "Priya's approval of this plan is already on file. Press Next to go on."
    assert views.done_line(live, "already") == w.ALREADY == "You already approved this plan."


def test_a_live_page_shows_nothing_for_the_demos_own_done_keys():
    # Only the demo plays a step or starts the story again. A tab left on the demo's restart landing, loaded again once
    # serve runs on the same port, never says the demo played a step, or to press a Next button a live page hasn't.
    live = story("1")["monday"]
    for done in ("next-played", "restarted"):
        assert views.done_line(live, done) == "" and views.page(live, w.QUEUE_TITLE, csrf=CSRF, done=done).done == ""
        assert views.done_line(replace(live, demo=demo_bar()), done) == ""  # a live board's mode decides, never a bar
    assert [views.done_line(live, done) for done in ("approved", "already", "dropped")] == [
        w.DONE["approved"], w.DONE["already"], w.DONE["dropped"]]  # a live run's own keys still show
    demo = replace(live, mode=Mode.DEMO, demo=demo_bar())
    assert (views.done_line(demo, "next-played"), views.done_line(demo, "restarted")) == (
        w.DONE["next-played"], w.DONE["restarted"])


def test_the_demo_says_press_next_only_while_the_bar_offers_next():
    # Once the story ended, or once the replay stopped matching, the bar has no Next button: an old tab's approval, or
    # a reload, never asks the judge to press it. The restart's page loaded again then says nothing: the bar says why
    # the story doesn't go on, so no line beside it says the story started again.
    won = story("1")["won"]
    ended = demo_bar(pending=(), coming=None, finished=True, total=Decimal("279.30"))
    stopped = demo_bar(mismatch="Order #1109: the recording holds no reading for this case as the code reads it now.")
    for bar in (ended, stopped):
        board = replace(won, mode=Mode.DEMO, demo=bar)
        assert "/demo/next" not in [button.action for button in views.demo_bar(bar, CSRF).buttons]
        assert views.done_line(board, "already") == "Priya's approval of this plan is already on file."
        assert views.done_line(board, "approved") == "You approved it as Priya did in the recording."
        assert views.done_line(board, "restarted") == ""  # the restart's page, loaded again
    going = replace(won, mode=Mode.DEMO, demo=demo_bar())
    assert views.done_line(going, "already") == w.DONE_DEMO["already"].format(approver="Priya")  # Next is there
    assert views.done_line(going, "restarted") == w.DONE["restarted"] == "The story starts again. Press Next to play it."
    assert w.DONE_DEMO_NO_NEXT == {"approved": "You approved it as {approver} did in the recording.",
                                   "already": "{approver}'s approval of this plan is already on file.",
                                   "restarted": ""}


def test_an_approval_the_recording_didnt_make_says_press_next_only_while_the_bar_offers_it():
    # The refusal keeps its first sentence, why nothing was approved, and drops Press Next when the bar has no Next.
    lead = "In the demo, only the approvals Priya made in the recording can be made, when the story reaches them."
    for bar in NO_NEXT.values():
        board = replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar(**bar))
        lines = texts(*views.refused(board, Result(False, "not-in-recording"), "dst_earbuds", csrf=CSRF)[1].lines)
        assert lines == [lead, w.NOW_LINE.format(now=w.NOW["APPROVE"])]
    going = replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar())
    lines = texts(*views.refused(going, Result(False, "not-in-recording"), "dst_earbuds", csrf=CSRF)[1].lines)
    assert lines == [f"{lead} Press Next.", w.NOW_LINE.format(now=w.NOW["APPROVE"])]


def demo_lines(board):
    """Every line the demo's pages can show on this board: the frame with each done key, the queue, the money page,
    each case page and approval screen, and every refusal, of the approval form and of the drop form."""
    found = []
    for done in dict.fromkeys((*w.DONE, *w.DONE_DEMO)):
        frame = views.page(board, w.QUEUE_TITLE, csrf=CSRF, done=done)
        found += [frame.done, *texts(*frame.banner.lines, *frame.bar.lines, *frame.bar.links, *frame.bar.buttons)]
    found += queue_texts(board) + texts(*views.money_view(board).summary)
    for dispute_id in board.records:
        found += case_texts(board, dispute_id) + approve_texts(board, dispute_id)
    dispute_id = next(iter(board.records), None)
    for key in views.RESULTS:
        for drop in (False, True):
            found += texts(*views.refused(board, Result(False, key), dispute_id, csrf=CSRF, drop=drop)[1].lines)
    return found


def demo_boards(**bar):
    """An empty demo board and Monday's, both with this bar."""
    return (board_of({}, mode=Mode.DEMO, demo=demo_bar(**bar)),
            replace(story("1")["monday"], mode=Mode.DEMO, demo=demo_bar(**bar)))


@pytest.mark.parametrize("bar", NO_NEXT.values(), ids=NO_NEXT)
def test_no_page_says_to_press_next_while_the_bar_offers_none(bar):
    # A tab left open, or a page loaded again, once the story ended or stopped: no line of any page says to press
    # Next, since the bar has no Next button.
    for board in demo_boards(**bar):
        assert not views.offers_next(board.demo)
        assert [line for line in demo_lines(board) if "ress Next" in line] == []
    going = [line for board in demo_boards() for line in demo_lines(board)]  # so the check means something
    assert {w.EMPTY_DEMO, w.DONE["restarted"], w.DONE_DEMO["approved"].format(approver="Priya"),
            w.NOT_IN_RECORDING.format(approver="Priya")} <= set(going)


# ----------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("key, status, words", [
    (key, status, words) for key, (status, words) in views.RESULTS.items() if key != "file-moved"])
def test_every_refusal_has_its_status_and_words(key, status, words):
    board = story("1")["monday"]
    got, view = views.refused(board, Result(False, key, "A module's own sentence."), "dst_headphones", csrf=CSRF)
    shown = texts(*view.lines)
    lead = w.NOT_IN_RECORDING_NO_NEXT.format(approver="Priya") if key == "not-in-recording" else words  # no Next here
    assert got == status and shown[:2] == [lead, "A module's own sentence."]
    assert shown[2] == w.NOW_LINE.format(now=w.NOW["APPROVE"])
    assert view.links[-1] == Link(w.BACK_QUEUE, "/")


def test_a_drop_that_isnt_needed_says_so_for_a_plan_with_a_file_and_one_without():
    # The Service answers drop-not-needed for a plan whose file still builds, for an accept that sends no file, and
    # when the rules can't run or the other checks hit a bug: the sentence is true of each, so it names no file.
    board = story("1")["monday"]
    for dispute_id in ("dst_headphones", "dst_earbuds"):  # a challenge with its file, and an accept with none
        status, view = views.refused(board, Result(False, "drop-not-needed"), dispute_id, csrf=CSRF, drop=True)
        assert (status, texts(*view.lines)) == (409, ["This app found no reason to drop this plan, so it stays.",
                                                      w.NOW_LINE.format(now=w.NOW["APPROVE"])])
    assert "file" not in w.DROP_NOT_NEEDED


def test_a_refused_approval_shows_lifecycles_sentence_and_the_way_back():
    board = story("1")["monday"]
    message = "This approval is void: the stage changed from RFI to CHARGEBACK."
    status, view = views.refused(board, Result(False, "refused", message), "dst_headphones", csrf=CSRF)
    assert status == 409 and texts(*view.lines) == [w.NOT_APPROVED, message, "Now: It waits for your approval."]
    assert [link.href for link in view.links] == ["/case/dst_headphones/approve", "/case/dst_headphones", "/"]


def test_a_file_that_moved_is_refused_with_the_drop_offer_in_a_live_run():
    board = replace(story("1")["monday"], store=edited(FERNHILL, LATER_DELIVERY))  # the delivery scan moved since
    status, view = views.refused(board, Result(False, "file-moved", rounds.MOVED), "dst_headphones", csrf=CSRF)
    assert status == 409 and texts(*view.lines)[:3] == [w.FILE_MOVED.format(why=rounds.MOVED), w.DROP_OFFER,
                                                        w.DROP_BUTTON]
    demo = replace(board, mode=Mode.DEMO, demo=demo_bar())
    assert w.DROP_BUTTON not in texts(*views.refused(demo, Result(False, "file-moved", rounds.MOVED), "dst_headphones",
                                                     csrf=CSRF)[1].lines)


def test_an_unknown_refusal_and_a_success_key():
    board = story("1")["monday"]
    status, view = views.refused(board, Result(False, "approved"), None, csrf=CSRF)
    assert status == 409 and texts(*view.lines) == [w.UNKNOWN_RESULT]
    assert views.succeeded(Result(True, "approved")) and views.succeeded(Result(False, "already"))
    assert not views.succeeded(Result(False, "approved")) and not views.succeeded(Result(True, "stale"))


# ----------------------------------------------------------------------------- the words and the module
def constants():
    for name, value in vars(w).items():
        if name.isupper():
            yield name, value


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from strings(item)


def test_each_of_the_words_is_named_once():
    # A second assignment would quietly replace the first sentence wherever the screens show it.
    tree = ast.parse((WEB / "words.py").read_text(encoding="utf-8"))
    names = [target.id for node in tree.body if isinstance(node, ast.Assign) for target in node.targets
             if isinstance(target, ast.Name)]
    assert names and [name for name in dict.fromkeys(names) if names.count(name) > 1] == []


def test_the_words_hold_no_quotation_marks():
    for name, value in constants():
        for text in strings(value):
            assert not set(text) & {'"', "“", "”", "«", "»"}, name


def test_the_words_are_short_plain_sentences():
    for name, value in constants():
        for text in strings(value):
            assert text == text.strip() and "  " not in text, name
            assert len(text) <= 260, name


def test_the_cut_words_are_gone():
    cut = ("BOOK", "BOOK_INTRO", "BOOK_BUTTON", "ABOUT", "DEMO_HOW", "READ_NOW", "PASSING", "WAIT_PAST_DUE", "REUSE",
           "CLAUDE_CALLS", "CLAUDE_REQUESTS", "EARLIER_FILE", "SWITCH", "JUMPED", "MOMENTS", "TRACKS", "STORY",
           "STORY_ENDINGS")
    assert not [name for name in cut if hasattr(w, name)]
    assert w.REQUEST_ID == "{id}. Every try sends this same id."
    assert w.NOTE_RFI == "Demo note: the simulated bank asks at RFI. Real Visa disputes don't use CE 3.0 at RFI."
    assert w.SENDS_ONLY == ("Besides the terms and files shown here, this app sends only fixed fields: this app's name, "
                            "the standard challenge method, each file's name, type and short descriptions and, at a "
                            "chargeback, the product type.")


def test_the_approver_line_names_every_rule_the_name_must_meet():
    # The CLI refuses a blank name and any tab, line break or special space, as the Service does, not only a long one.
    assert w.BAD_APPROVER == ("The approver must be a person's name of 1 to 60 characters, with no tabs, line breaks or "
                              "special spaces, and not the agent's.")


def test_no_clock_is_read_in_the_web_package():
    # board.now is the only time a page shows: no module here calls a clock, or imports one.
    clock = re.compile(r"\.(?:now|utcnow|today|monotonic|perf_counter|time_ns|time)\(")
    for path in (*WEB.glob("*.py"), WEB.parent / "inbox.py"):
        source = path.read_text(encoding="utf-8")
        assert not clock.search(source), path.name
        tree = ast.parse(source)
        modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        modules |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not modules & {"time", "datetime.datetime"}, path.name
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert not calls & {"now", "utcnow", "today", "monotonic", "perf_counter", "time_ns"}, path.name


def test_lines_are_checked_when_made():
    with pytest.raises(ValueError):
        Line("A. SAMPLE", Voice.RECORD)  # a quote needs code's label
    with pytest.raises(ValueError):
        Line("text", mark="blink")
    assert Line("x", Voice.BANK, "The bank wrote:").css == "record bank"
    assert Line("x", mark="warn").css == "code warn"


def test_links_quote_the_dispute_id():
    assert views.case_href("dst a/b?c", "approve") == "/case/dst%20a%2Fb%3Fc/approve"


def test_the_plan_text_round_trips_and_is_ascii():
    for step in STEPS["1"]:
        for record in story("1")[step].records.values():
            plan = record.rounds[-1].plan
            if plan is not None:
                text = views.plan_text(plan)
                assert text.isascii() and load(Plan, json.loads(text)) == plan
