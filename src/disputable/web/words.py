"""Every sentence the screens show that another module doesn't write. Short and plain: Priya reads them, and so do the
judges. Templates hold no words of their own: they show these, and the lines other modules wrote (lifecycle's,
approvals', rounds', rules', ce3's and the runtime's), as written.

No sentence here holds a quotation mark, so none can present a paraphrase as a quote. This module imports nothing, so
the CLI can print STARTING before the slow imports.
"""

from __future__ import annotations

APP = "Disputable"
NAV = {"queue": "Disputes", "money": "Money"}
UNCLEAR = "unclear"

# ----------------------------------------------------------------------------- the banner
DEMO = "Demo"
DEMO_REPLAYS = ("This replays a run recorded on the Airwallex sandbox on {day}. Nothing goes to Airwallex or to Claude, "
                "and no keys are needed.")
DEMO_STANDIN = ("This replays a run on a stand-in for the Airwallex sandbox, built into this app's tests. It isn't a "
                "recording of Airwallex. Nothing goes to Airwallex or to Claude, and no keys are needed.")
MADE_UP = "The store, its customers and its orders are made up."
READ_RECORDED = "Claude's readings were recorded from {model} on {day}."
READ_RECORDED_SPAN = "Claude's readings were recorded from {model} between {first} and {last}."
READ_RECORDED_UNDATED = "Claude's readings were recorded from {model}."
READ_SCRIPTED = "Claude's readings here are scripted stand-ins, not Claude's own."
READ_NONE = "No Claude reading is recorded, so every case Claude would read goes to a person."
CLOCK_REPLAY = "Clock: {when}, the recording's time. Story time: {story}."
CLOCK_REPLAY_START = "Clock: {when}, the recording's time."  # the bar says the story hasn't started yet
LIVE = "Live"
LIVE_READS = "This app reads the Airwallex sandbox and sends its responses there."
LIVE_LOOP = "The loop does the reading and the sending. These pages show what it saved and take approvals."
LIVE_CLAUDE = "Claude ({model}, effort {effort}) reads the records when a fight could pay."
LIVE_NO_KEY = "No Claude API key is set, so every case Claude would read goes to a person."
CLOCK_LIVE = "Clock: {when}."
LAST_PASS = "Last pass: {when}."
NO_PASS = "No pass yet."

# ----------------------------------------------------------------------------- the queue
QUEUE_TITLE = "Disputes"
QUEUE_SUMMARY = "{open} open. Cost so far: {cost}. Still at risk: {risk}."
DEADLINES = "Deadlines"
PROBLEMS = "Problems from the last pass"
GROUPS = {"approve": "Waits for your approval", "person": "A person handles these", "out": "Going out",
          "bank": "The bank decides", "closed": "Closed"}
EMPTY_QUEUE = "No disputes yet."
EMPTY_LIVE = "The loop reads only the disputes on a story run's own payments. A data folder with no story run stays empty."
EMPTY_DEMO = "Press Next to play the story."
CURRENCY_LEFT_OUT = "The sums leave out {disputes} in another currency, since the fee table has only {fees} fees."
HIDDEN = "Disputes already closed when this app first saw them: {count}. They aren't in the queue or the totals."
ORDER = "Order #{number}"
NO_ORDER_NUMBER = "Dispute {id}"
ITEM_SOURCE = "Item, as the order names it"
CLAIM = "{name} (Visa {code})"
CLAIMS = {"10.4": "Card-absent fraud", "13.1": "Not received", "13.6": "Credit not processed"}
CLAIM_OTHER = "Claim {code}"
STAGE_ROUND = "{stage}, round {number}"
STAGES = {"RFI": "Request for information", "PRE_CHARGEBACK": "Pre-chargeback", "CHARGEBACK": "Chargeback",
          "PRE_ARBITRATION": "Pre-arbitration", "ARBITRATION": "Arbitration"}
STAGE_OTHER = "Stage {stage}"
HEAD = "{order} · {amount} · {claim} · {stage}"
DUE = "Due {when}, {left}."
LEFT_IN = "in {span}"
LEFT_AGO = "{span} ago"
UNDER_AN_HOUR = "under an hour"
CREATED = "Created {when}."
CREATED_UNKNOWN = "Airwallex sent no creation date."
OPENED = "Opened {day}. Code counts from this date."
COST_RISK = "Cost so far {cost}. At risk {risk}."
COST_OTHER = "At risk {risk}. Code doesn't add up costs in {currency}."
OPEN_CASE = "Open the case"
REVIEW_APPROVE = "Review and approve"
BROKEN_RECORD = ("Part of what this app saved for this case can't be read. {why} Nothing is sent for it. A person handles "
                 "it in Airwallex.")
BROKEN_OUT = "It isn't in the counts or the totals."
BROKEN_PART = "Part of what this app saved for this case can't be read. {why}"
CARD_ERROR = "This app hit an error while it showed this case. The terminal shows the details."

# ----------------------------------------------------------------------------- where a case is now
NOW = {
    "DECIDE": "Code decides this round on the next pass.",
    "APPROVE": "It waits for your approval.",
    "SEND": "It's ready to send.",
    "SENT": "It's being sent.",
    "WAIT_OURS": "This app's response shows on the dispute. The bank decides.",
    "WAIT_OTHER": "A response from someone else shows on the dispute. The bank decides.",
    "WAIT": "Airwallex doesn't ask for a response now.",
    "PERSON": "A person handles this round. Nothing goes out from here.",
    "CLOSED": "Closed: {status}.",
}
STATUS = {"ACCEPTED": "accepted", "WON": "won", "LOST": "lost", "EXPIRED": "expired", "REVERSED": "reversed",
          "CHALLENGED": "challenged", "PENDING_DECISION": "the bank is deciding", "PENDING_CLOSURE": "closing",
          "REQUIRES_RESPONSE": "asks for a response"}
SEND_READY = "The next pass sends it."
SENT_READY = "The next pass tries again with the same request id."
HISTORY_ONLY = "It was already closed when this app first saw it. It isn't in the queue or the totals."
HISTORY_SHOWN = "It was already closed when this app first saw it, so it isn't in the totals."  # a blocked one shows
BLOCK_RAW = "The latest reading wasn't applied: {why}"
BLOCK_CLEARS = "This usually clears on the next reading."
VERIFY_SEND = ("The approvals on file don't let this plan go out: {why} The next pass drops the plan, and a person "
               "approves a new one.")
VERIFY_SENT = ("The approvals on file don't let this plan go out again: {why} It may have gone out already, so it stays. "
               "A person checks what Airwallex has.")

# ----------------------------------------------------------------------------- the case page
NOW_TITLE = "Where it is now"
PLAN = "What code plans"
NO_PLAN_DECIDE = "Code hasn't made a plan for this round yet."
NO_PLAN_PERSON = "Code drafted nothing for this round."
WHY = "Why code chose this"
TERMS = "The exact terms"
TERM_NAMES = {"dispute": "Dispute", "stage": "Stage", "action": "Action", "reason": "Reason sent to Airwallex",
              "amount": "Amount in dispute", "refund": "Refund", "refunded_before": "Refunded before this plan",
              "customer": "Customer", "facts": "Facts it cites", "files": "Files it sends", "due": "Due",
              "fee_table": "Fee table", "request_id": "Request id"}
TERM = "{name}: {value}"
CUSTOMER_NAME = "The customer's name on file"
NO_CUSTOMER = "None on file."
NONE_LISTED = "None."
ACTIONS = {"ACCEPT": "Accept", "CHALLENGE": "Challenge"}
REASONS = {"LOW_VALUE_TRANSACTION": "low value", "VALID_CUSTOMER_DISPUTE": "the customer's claim is valid",
           "OTHERS": "other", "AGREEMENT_REACHED_WITH_CUSTOMER": "agreed with the customer",
           "CUSTOMER_RELATIONSHIP_MAINTENANCE": "to keep the customer", "COLLABORATION_ACCEPTED_MANUAL": "accepted by hand",
           "PURCHASE_HISTORY": "earlier purchases", "PRODUCT_RECEIVED": "the customer received the product",
           "CUSTOMER_WITHDRAWN": "the customer withdrew", "CUSTOMER_REFUNDED": "the customer was refunded",
           "NOT_ENTITLED": "the customer isn't entitled", "SEPARATE_PRODUCT": "a separate product",
           "AUTHENTIC_PRODUCT": "an authentic product", "OTHER_REASONS": "other reasons"}
REASON = "{value} ({name})"
REASON_UNKNOWN = "a reason this app doesn't know"
REFUND_TO_CARD = "{amount}, to the card that paid."
REFUND_NONE = "None."
REFUND_NONE_CHARGEBACK = "None. The chargeback already returned the money to the customer."
REQUEST_ID = "{id}. Every try sends this same id."
DUE_TERM = "{when}, {left}"
FOUND = "What code found"
NOT_DECIDED = "Code hasn't decided this round yet. This is what the records show now."
CE3 = "The CE 3.0 check"
INBOX = "The inbox"
OTHER_FINDINGS = "Other findings"
FINDINGS_NONE = "Code found nothing more to note."
FOUND_NONE = "Code's rules didn't read the store's records for this round."  # the round has no saved basis at all
FOUND_DROPPED = "Code's findings for this round were for a plan that was dropped, so they aren't shown."
EVIDENCE_ERROR = "Code can't work out this round's evidence: {why}"
EVIDENCE_BUG = "Code hit an error while it read this case's records. The terminal shows the details."
ROUND_UNREADABLE = "This app can't read what it saved for this round. {why}"
ROW_FORM = "A saved row of this round isn't in the form this app reads."  # when the row's own text is in code's terms
FILES = "The files"
FILES_NONE = "This plan sends no file."
FILE_INTRO = "Code wrote this file from the store's records. The bank reads it word for word."
FILE_HASH = "SHA-256: {sha256}"
FILE_DOWNLOAD = "Download the PDF"
FILE_KEPT = "Airwallex refused the response. The file is still here for a person."
FILE_MOVED = "This plan's file can't be built again exactly as planned. {why}"
FILE_UNREADABLE = "The saved copy of this plan's file can't be read, so this page can't show it. {why}"
DROP_OFFER = "You can drop the plan. Code then decides the round again on the next pass."
DROP_BUTTON = "Drop this plan"
SENDS_TITLE = "Tries to send"
SEND_TRY = "{when}: {outcome}."
SEND_TRY_STATUS = "{when}: {outcome} (HTTP {status})."
SEND_ROUND = "Round {number}, {when}"  # a try's time, after the round whose plan it sent
OUTCOMES = {"taken": "Airwallex took it", "failed": "Airwallex refused it",
            "retry": "no clear answer, so the same request goes again"}
OUTCOME_OTHER = "an answer this app doesn't know"

# ----------------------------------------------------------------------------- what Claude read
CLAUDE = "What Claude read"
CLAUDE_NOT_ASKED = "Code didn't ask Claude. The numbers settled this round without the records."
CLAUDE_NOT_YET = "Claude hasn't read this round yet."
CLAUDE_NOT_READ = "Claude didn't read this round."
CLAUDE_MISSING = "Claude's reading of this round isn't on file."
CLAUDE_CANT_SHOW = "This page can't show what Claude read for this round."  # What code found says why
CLAUDE_DROPPED_PLAN = "The reading on file for this round was for a plan that was dropped, so it isn't shown."
CLAUDE_FAILED = "Claude couldn't read this case's records: {why}."
CLAUDE_PARTIAL = "Claude didn't read all of this case's records."
CLAUDE_INTRO = "Code checked each quote against the record it names."
CLAUDE_FACTS = "Claude quoted these records."
CLAUDE_POINTS = "Claude raised these points. Each one sends the round to a person."
COUNTED = {"K1": "Code counted it as: the customer says an earlier order was reported as fraud.",
           "K2": "Code counted it as: the customer takes the dispute back.",
           "K5": "Code counted it as: a record a person should read."}
CLAUDE_TAGS = "Claude says the bank's comment reports {orders}. Code uses an order only when the comment names it."
CLAUDE_FLAGS = "Claude flagged these lines. They changed nothing."
CLAUDE_NOTHING = "Claude sent nothing that code kept."
CLAUDE_DROPPED = "Code dropped {items} that Claude sent."
CLAUDE_DROPPED_WHY = "Why code dropped them"
DRAFT = "Claude's draft. Code didn't use it."
DRAFT_ACTION = "Claude suggested: {action}, with this reason:"
DRAFT_ACTIONS = {"ACCEPT": "accept", "CHALLENGE": "challenge", "HAND_OFF": "hand it to a person"}
DRAFT_SOURCE = "Claude wrote"
DRAFT_REFUND = "The refund Claude wrote:"
DRAFT_CARD_YES = "Claude said the refund goes to the card that paid."
DRAFT_CARD_NO = "Claude said the refund goes somewhere else."
DRAFT_NOTE = "Claude's note:"
DRAFT_CHECKS = "Code's checks on the draft:"
DRAFT_FINE = "Code's checks found no problem with it."

# ----------------------------------------------------------------------------- records, rounds, approvals, money
RECORDS = "The store's records"
RECORDS_INTRO = "As the store holds them. Nothing here is code's or Claude's."
RECORDS_NONE = "This payment isn't tied to an order in the store's records."
RECORD_KINDS = {"orders": "Orders by this customer or on this card", "scans": "Scans of order #{number}",
                "emails": "Emails in the inbox for order #{number}",
                "warranty": "Warranty sign-ups for the serials on order #{number}"}
RECORD_LABELS = {"order": "Order #{number}", "carrier": "Carrier scan {id}", "warehouse": "Warehouse scan {id}",
                 "scan": "Scan {id}", "email": "Email {id}", "warranty": "Warranty sign-up {id}",
                 "record": "Record {id}", "missing": "A record the store doesn't have: {id}"}
RECORD_CUT = "[cut: {count} more characters]"
RECORDS_EMPTY = "None."
RECORD_PARTS = {"items": "Item {n}", "refunds": "Refund {n}"}  # a field that holds several parts: each one on its own
RECORD_PART = "{name} {n}"
PART_NAMES = {"sku": "SKU", "description": "description", "unit_price": "unit price", "quantity": "quantity",
              "amount": "amount"}  # the parts' own fields, where the reader's field names have none
ROUNDS = "Rounds"
ROUND = "Round {number}: {stage}"
ROUND_SEEN = "This app first saw the bank ask on {when}."
BANK_WROTE = "The bank wrote:"
BANK_SILENT = "The bank wrote no comment."
REPLY_OURS = "This app {verb} at {stage}. Airwallex stored the reason {reason}."
REPLY_OTHER = "Someone else {verb} at {stage}, not this app."
VERBS = {"ACCEPT": "accepted", "CHALLENGE": "challenged"}
SENT_PLAN = "This app sent: {summary}."
REFUSED_PLAN = "This app sent: {summary}. Airwallex refused it."
UNSENT_PLAN = "Code's plan: {summary}."
NOTES = "Notes"
NO_NOTES = "No notes."
PERSON_LINE = "Why a person handles it: {why}"
APPROVALS = "Approvals"
NO_APPROVALS = "Nobody has approved anything on this dispute."
APPROVAL = "{by} approved round {number} on {when}."
APPROVAL_STANDS = "It stands."
APPROVAL_SENT_VOID = "This app sent the plan this approval was for. It's void now: the bank moved the case to a new round."
APPROVAL_UNCLEAR = "This app can't tell whether this approval stands."
CASE_MONEY = "Money for this case"
NOTHING_BOOKED = "Nothing is booked yet."
ENTRY = "{label}: {amount}"

# ----------------------------------------------------------------------------- the approval screen
APPROVE_TITLE = "Approve order #{number}"
APPROVE_TITLE_NO_ORDER = "Approve dispute {id}"
SENDS = "What it sends"
REVIEW_BOOKS = "Approving it books a review: {cost} of your time."
REVIEW_DONE = "This round's review is already booked, so approving adds nothing."
REVIEW_NONE = "Approving it books no review: code made this accept itself."
READING_BESIDE = "Claude's reading beside this plan"
READING_SUMMARY = "Claude quoted {quotes} from {records}, raised {points} and flagged {flags}. Code dropped {dropped}."
READING_QUOTES = "See Claude's quotes on the case page"
FRESH = "Code reads the dispute again before it sends. If anything changed, the plan is dropped and you approve a new one."
AIRWALLEX_ADDS = ("Airwallex adds a summary file of its own to every challenge. It may also fill in the store's profile "
                  "from the store's Airwallex account, and the order's number, currency and date from the payment.")
SENDS_ONLY = ("Besides the terms and files shown here, this app sends only fixed fields: this app's name, the standard "
              "challenge method, each file's name, type and short descriptions and, at a chargeback, the product type.")
APPROVE_AS = "You approve as {name}."
APPROVE_BUTTON = "Approve this exact plan"
APPROVALS_OFF = "Approvals are off. Start the app with --approver and your name."
NOTHING_TO_APPROVE = "Nothing waits for an approval on this case."
PERSON_RESPONDS = "Respond in Airwallex's web app. This app sends nothing for this round."

# ----------------------------------------------------------------------------- refusals and results
NOT_DONE = "Not done"
NOT_APPROVED = "Not approved."
NOT_DROPPED = "Not dropped."
NOW_LINE = "Now: {now}"
FORM_FOREIGN = ("This app refused the form: it didn't come from this app's own page. Open the page again and try once "
                "more.")
FORM_OLD = "This page is from before the app started again, so nothing was done. Open the page again."
FORM_BAD = "The form didn't carry what this app needs. Open the page again."
FORM_TOO_BIG = "The form is too large."
FORM_TYPE = "This app takes only its own forms."
PLAN_UNREADABLE = "The form didn't carry a plan this app can read. Open the case again."
NO_CASE = "This app has no dispute with that id."
NO_FILE = "This case has no file with that hash."
NO_PAGE = "This app has no page here."
NO_METHOD = "This page doesn't take that kind of request."
READING_CHANGED = "What the screen showed beside the plan changed. Open the case again."
STALE = "The case changed while you were on the page, so nothing was saved. Open it again."
ALREADY = "You already approved this plan."
NOT_RECORDED = "This app couldn't record the approval, so nothing was saved."
DROP_NOT_NEEDED = "This app found no reason to drop this plan, so it stays."  # a file that builds, no file, or a bug
BROKEN = ("Part of what this app saved for this case can't be read, so nothing can be done here. A person handles it in "
          "Airwallex.")
ONLY_LIVE = "This works only in a live run."
ONLY_DEMO = "This works only in the demo."
DEMO_MOVED = "The demo moved on since the page was shown. Here it is now."
DEMO_FINISHED = "The story has ended. Start it again to replay it."
DEMO_MISMATCH = "The replay no longer matches the recording, so the demo can't go on."
NOT_IN_RECORDING = ("In the demo, only the approvals {approver} made in the recording can be made, when the story reaches "
                    "them. Press Next.")
NOT_IN_RECORDING_NO_NEXT = ("In the demo, only the approvals {approver} made in the recording can be made, when the "
                            "story reaches them.")  # the bar has no Next button
NO_ENDING = "That ending isn't recorded."
UNKNOWN_RESULT = "This app couldn't do that, and it can't say why."
ERROR = "This app hit an error on this page. The terminal shows the details."
BACK_CASE = "Back to the case"
BACK_APPROVAL = "Open the approval page again"
BACK_QUEUE = "Back to the disputes"
DONE = {"approved": "You approved it. Code reads the dispute again, then sends it.",
        "already": ALREADY,
        "dropped": "The plan was dropped. Code decides the round again on the next pass.",
        "next-played": "The demo played the next step.",
        "restarted": "The story starts again. Press Next to play it."}
DONE_DEMO = {"approved": "You approved it as {approver} did in the recording. Press Next to go on.",
             "already": "{approver}'s approval of this plan is already on file. Press Next to go on.",  # Next's, or yours
             "restarted": DONE["restarted"]}
# Once the bar has no Next: the story ended, or stopped, since the page was shown. A restart's page loaded again then
# shows no line, since the bar says why the story doesn't go on.
DONE_DEMO_NO_NEXT = {"approved": "You approved it as {approver} did in the recording.",
                     "already": "{approver}'s approval of this plan is already on file.",
                     "restarted": ""}

# ----------------------------------------------------------------------------- the demo
ENDINGS = {"1": "Ending 1: the warranty record", "2": "Ending 2: no new record"}
ENDING_OTHER = "Ending {ending}"
STEP = "Step {n} of {count}: {story}."
STEP_SO_FAR = "Step {n}: {story}."
NOT_STARTED = "The story hasn't started yet."
PENDING_ONE = ("{approver} approved {order} in the recording. Approve it on its page, or press Next and the demo approves "
               "it as {approver}.")
PENDING = ("{approver} approved {orders} in the recording. Approve them on their pages, or press Next. Each press "
           "approves one of them as {approver}.")  # Next plays one beat a press
NEXT = "Next: {story}: {text}"
END = "The story ends. This ending cost {total}."
END_PLAIN = "The story ends."
LEFT_UNUSED = "The recording holds {calls} this replay never made."
MISMATCH = ("The replay no longer matches the recording: {mismatch} The code changed after the recording was made. "
            "Record the story again.")
MISMATCH_STANDIN = ("The replay no longer matches the recording: {mismatch} The code changed after the recording was "
                    "made. Make the stand-in again: uv run python scripts/record_standin.py (add --no-claude if it says "
                    "the Claude readings aren't there).")
RESTART_SAME = "Start Ending {ending} again"
RESTART_OTHER = "Start Ending {ending}"
NOTE_RFI = "Demo note: the simulated bank asks at RFI. Real Visa disputes don't use CE 3.0 at RFI."
NOTE_FEE = ("Demo note: the {later} later-stage fee is an assumption. Airwallex's pages disagree on a Visa "
            "pre-arbitration fee.")
NOTE_WEEKS = ("Demo note: the sandbox can't wait three weeks, so the simulated bank pushed back right after Monday's "
              "responses.")
NOTE_WEEKS_STANDIN = "Demo note: the stand-in can't wait three weeks, so its simulated bank pushed back seconds later."
NOTE_CHANCES = "Demo note: the chances, such as {chance} with a warranty record, are assumptions in the fee table {version}."

# ----------------------------------------------------------------------------- money
MONEY_TITLE = "Money"
MONEY_SUMMARY = "Cost so far: {cost}. Still at risk: {risk}."
ACCEPT_ALL = "Accepting everything at once would have cost {cost}."
CHEAPER = "This queue cost {difference} less than that."
DEARER = "This queue cost {difference} more than that."
SAME = "This queue cost the same."
NOT_FINAL = "Some cases are still open, so this isn't final."
NOT_FINAL_UNREAD = "This app can't read every case, so this isn't final."
MONEY_BROKEN = "The sums leave out {cases} whose saved data this app can't read."
MONEY_EMPTY = "No dispute has cost anything yet."
COLUMNS = ("Dispute", "Now", "What it cost", "Cost", "At risk", "Accepting at once")
CANT_SAY = "can't say"
CANT_SAY_NOTE = "Code can't price accepting at once for a case it first saw at {stage}, so the sum leaves it out."
FEES = ("Fees from {version}: chargeback fee {chargeback}, refund fee {refund}, a person's review {review}, later-stage "
        "fees {later} (an assumption).")
AT_RISK_NOTE = ("At risk is the amount in dispute on each open case. Once this app's RFI refund shows, the refund counts "
                "as a cost instead, until the bank asks again.")
ACCEPT_NOTE = ("Accepting at once means a refund at RFI, plus the refund fee, or at a chargeback the amount the bank took "
               "back, plus the chargeback fee. It costs no review.")
RECORDED_TOTAL = "The recording's own total for this ending: {total}."
NOTES_TITLE = "Notes"

# ----------------------------------------------------------------------------- the terminal
STARTING = "Starting Disputable. The first start takes about 15 seconds."
RUNNING = "Disputable is running at {url}. Press Ctrl+C to stop."
PORT_BUSY = "Port {port} is in use. Start again with --port and another number, such as {other}."
BAD_APPROVER = ("The approver must be a person's name of 1 to 60 characters, with no tabs, line breaks or special spaces, "
                "and not the agent's.")


def count(number: int, unit: str) -> str:
    """1 item, 2 items."""
    return f"{number} {unit}{'' if number == 1 else 's'}"


def listed(names: list[str] | tuple[str, ...]) -> str:
    """a, b and c."""
    names = list(names)
    if not names:
        return ""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def orders(numbers: list[str] | tuple[str, ...]) -> str:
    """order #1038; orders #1012 and #1038."""
    numbers = list(numbers)
    return f"order{'' if len(numbers) == 1 else 's'} {listed([f'#{number}' for number in numbers])}"
