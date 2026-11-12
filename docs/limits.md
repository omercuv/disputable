# Assumptions and known limits

This page lists Disputable's assumptions and known limits. The store, Fernhill Audio, is made up, and so are its people, its
records and the chances in the fee table. Code makes every money decision. Claude only reads records and drafts.

## Money and fees

- Only disputes in USD are priced, since the fee table, fees-v1, has only USD fees. A dispute in another currency goes to a
  person. Its amount in dispute and what is at risk show in its own currency, the queue's and money page's sums leave it out
  and say so, and its booked entries, such as a refund, still show with $.
- The $250 later-stage fee is an assumption. Airwallex's pages conflict on whether Visa charges a pre-arbitration fee.
- Visa's extra fee on fraud chargebacks without 3DS is left out: no amount is published. It would only add to a fight's cost.
- At RFI a win carries no $15 chargeback fee, so the submitted line "With a $15 fee, even a win loses money" isn't accurate
  there. Code accepts the $12 ear tips because even a certain win costs a person's $16 review, more than the refund.
- Ending 1, which the store wins, costs $279.30 ($1,778.30 had it lost). Accepting everything at once: $1,465.60.
- Approving an accept that code made itself books no review. So if a Claude reading raised no point, Ending 2 would cost
  $1,512.30. With no reading, as recorded, round two's accept is a person's call on code's draft and books one $16 review:
  $1,528.30.

## Stages, Visa and CE 3.0

- The story uses RFI (a request for information) because the sandbox's simulator offers it. Real Visa disputes start later.
- The headphones case uses CE 3.0 at RFI. That follows the simulator, not real Visa behaviour: Airwallex's CE 3.0 page limits
  CE 3.0 to the chargeback stage, and Visa's merchant papers describe it as a response at pre-arbitration.
- Visa's papers and Airwallex's pages disagree, or say nothing, on four points: where the 120-to-365-day window is counted
  from, whether an earlier dispute that wasn't for fraud still counts, whether the same two details must match on both
  earlier orders, and an earlier order that was refunded. A case that depends on one of them is unclear, and a person
  decides.
- This app counts its evidence as turned down only when the bank asks again at a later stage.
- After this app's chargeback challenge, PRE_ARBITRATION with the status PENDING_DECISION means the bank is deciding.
- An RFI that expired and then escalated was never seen in the sandbox.
- The sandbox never reports `challenge_method`, so only a made-up case shows the skip of a case AI Dispute Automation fought.
- Order #1038's card is still in use after a May fraud report on that order. The idea's
  [v2](idea/idea-submission-v2-detailed.md) write-up sets both facts.

## The CE 3.0 check's assumptions

- Both ends of the 120-to-365-day window count.
- Two orders on the same card at the same moment count as two.
- An order's time stands in for Visa's processing date, and days are calendar days in UTC.
- Any later round counts Visa's single CE 3.0 attempt as used.
- Claude's tags for orders the bank reports as fraud count only where the bank's own comment names the order. To find order
  numbers, the check skips the numbers in the comment's dates, times and amounts, as follows.
- A bare number, a year too, still names the order with that number. So does the year in date forms the check doesn't read:
  "2026-05", "05-2026", "May 1 to 14, 2026", "2026 May 14", "May 14, 2026-10:42", "14 May 2026-10:42", a range whose second
  date has no year ("1 May 2026-31 May"), and one mixing a month-first and a day-first date ("May 14, 2026-31 May 2026").
- A month and year are a date only with a slash (05/2026), since a hyphen can be part of an order number (SO-05-2026-0042).
- A number reads as a year only from two years before the dispute's UTC year to one after (2024 to 2027 for a 2026 dispute),
  so order 2001 after "May 14" is still named.
- A year followed by a hyphen and digits (2026-0042) isn't a year, unless it follows its month (after one space, ", " or "-")
  and the digits start a whole next date, its own year included, on the same line: "1 May 2026-31 May 2026".
- A year after a day never runs on: "May 14 2026-1 Jun 2026" and "May 14 2026-1 Mary Sample" name order 2026-1.
- Only a row written exactly like a range still hides a short order: a day-first date, the order, then a month and year
  ("14 Dec 2025-31 Jan 2026"), or a month column with one space before the order ("June 2026-12 Jul 2026").
- A date never spans a line break, a tab, or a spaced hyphen or slash: "May 14 - 2026" is a date, then order 2026. Runs of
  spaces can still join its parts. No-break spaces read as spaces, never as nothing.
- An amount is skipped only with $, €, £ or USD in front: "1249 USD" names order 1249, so "Order 1038 USD 39.00" keeps 1038.
- A USD after a number on the same line belongs to that number, whatever the gap, so in "X USD Y" Y is named even when it is
  an amount. Only USD does this: "1001 39,00 € 1002" still hides 1002.
- A currency at the end of a line never reaches the next line, and a USD that starts a line leads the number after it. So
  rows like "1001 600.00 USD" then "1002 600.00 USD", and space-aligned tables, name every order.
- A range such as 1001-1003 names only its two ends.

## Evidence files

- Code writes every file from the store's records, and a person approves every file.
- A file shows names, addresses and signers as written, after their label. In a record's words that it quotes (a carrier's
  note, a dispute field it can't read), every quotation mark becomes a single one, and a run of them becomes one.
- A character the file's font can't show (outside Windows-1252) in a name, address, signer or carrier note makes the CE 3.0
  file a person's job, with no draft, even when a tag would leave that record out of the file.
- A file may be at most 2 MiB (Visa's exact limit wasn't checked). The PDF has no /ID in its trailer.
- "Nobody signed" is a list of the usual carrier words, read as words. A role such as DOCK counts as a signer.
- A file says "the billing address" only when the scan's place is the customer's billing address, the cardholder's name is
  the customer's, and no order on the card names someone else. The store keeps no billing address for a card. For this claim
  a name is blank only when the whole name is one placeholder, so "Na Na" on another order on the card stops it.
- The check reads a placeholder in a prior order's dispute field as an unreadable dispute, which can leave its result unclear
  for a person to decide. The file reads it as no dispute, but "unknown", "undefined", and a blank with "?" or an unsure part
  ("Unknown / N/A") read "The store's records don't show whether it had a dispute." It quotes other unreadable values
  ("FRAUD?" too) and calls none a dispute.
- The warranty file dates every order it names, fraud marks included. It doesn't mark a fraud report the bank made (a TC40),
  or a dispute the check can't read, on an order it names.
- The warranty file says none of the customer's orders "came from" the sign-up's device or IP only of the orders that show
  that detail, and names those that show none: the sign-up can't be shown independent of a device not on record.
- Warranty phones are compared digit for digit, so the same number with and without its country code doesn't match: the line
  says so, and the case goes to a person. A number whose last seven digits are zeros, in any script, is no number.
- In the check and the files, "N/A.", "(none)", "--", "0" (a guest's account), "localhost" and its Linux hosts-file names,
  and joined ones like "None, None, None" say nothing. A stray ".", "!" or "?" after an IP or device id is ignored.
- Still read as values: "nan" (Nan is a name), "00", "-1", "anonymous", "Not set, not set", an IP with a port, broadcast and
  multicast IPs, zero-padded IPs like "127.000.000.001" (compared as text), and IPv6 transition forms (6to4, NAT64).
- Unspecified, loopback and link-local IPs say nothing, and so do the private, shared and unique-local ones a proxy or a load
  balancer has. The check names such an IP, or an all-zeros device id, on the disputed order as what it is.
- A public proxy's or CDN's IP on every order still matches. The next guard would look for one IP on many customers' orders.
- Only the files, not the check, compare device ids that aren't UUIDs without letter case.

## The case state machine

- Code never lets a refund it saw go down. On an open case, a payment reading that shows less refunded counts as an old copy,
  one that doesn't say keeps the amount seen, and one that shows more is kept, even beside an odd or older copy of the
  dispute. The plan is checked against the amount kept too, so an unclear reading never hides a refund from it.
- A refund that this app's accept shows counts before the payment shows it, so a later round never pays twice.
- A second dispute on a payment sends every open case on it to a person, so the customer is never paid twice. So does a
  response that may land at the next stage, until it shows.

### Known limits

- A case this app closed that later changes stays blocked with a person. It shows $0 at risk, and a person's booking doesn't
  clear the block. A lagging read that repeats the last applied copy clears it for one read.
- A far-future `updated_at` (a slip in Airwallex's date) sends a case this app handles, even a closed one, to a person from
  that reading on. It stays there until Airwallex shows a copy dated after the slipped one, even once nothing else changes,
  and a booking doesn't clear it. A slip of under two days isn't caught.
- A dispute first seen closed shows only an older-copy block after a slip. It stays until a copy dated after the slipped one,
  even if the copy that set it was only a lagging one.
- While a slip holds, no reading is applied: a stage that opens and closes meanwhile is never booked, and a deadline can
  pass. The case shows the slip all along, so a person books what moved.
- A dispute read with no `updated_at` can be placed only by this app's clock: outside a slip, a replica's older copy read
  after a newer one counts as newer.
- A refund that can't be this app's, shown while the bank decides on this app's response, reaches a person only when the
  dispute asks again or closes. If that RFI closes ACCEPTED with no entry and no more refunded than this app's accept, nobody
  is told.
- After a challenge Airwallex never answered and that never showed, a loss books later-stage fees and asks a person to check.
- A refund reversal keeps an open case blocked with a person. A closed case never sees it: the record keeps the most it saw.
- A read that swaps one conflict for a different one makes a new plan without the first.
- An unclear refund reading while this app's accept is being sent holds every retry, so a request that never landed waits for
  a person at the deadline.
- A dispute read code can't use (an unknown status, say) builds no reading. A back-office refund shown only on that pass is
  missed, and one lagging payment read after it could let an approved accept out.
- A refund that no read by this app ever shows can't be seen.

## Claude's reading and its limits

- Code decides. A reading adds only facts, which the pages show; fixed lines, each of which sends the round to a person; tags
  for earlier orders the bank reports as fraud, which can only take orders away from the CE 3.0 check; and flags. Its draft
  is shown, and code never makes the plan from it.
- What goes out with no person is what code alone would send. Every fight is one that code alone drafts, and every refund is
  code's. The eval checks these three rules as I1, I2 and I3.
- A tag never lets code build a file code alone can't: code alone's conclusion then stands, and a person writes the file.
- When the tags change code's evidence and the round would end in an accept, a person checks the bank's comment first.
- A correct tag can clear the CE 3.0 check's own mismatch line. Then the case is a plain challenge, which needs one approval.
- A failed read counts for nothing but its line that a person reads the records, not even its tags.
- A tool result holds at most 4,000 characters a field, 25 records and 40,000 characters. Past a cap it shows the disputed
  order first in its history, then the newest records that fit, and skips a record too big for the room left. A read that saw
  only part of a result is incomplete, and a person reads the records.
- An attempt makes at most 40 tool calls, reads at most 300,000 characters of results and takes at most 10 turns. A proposal
  on the last turn, or once that budget is used up, is kept. A second, fresh attempt runs only when the first gave no answer.
- A read starts no new request after 10 minutes, but doesn't stop then: a server's retry-after can hold one request longer.
- A busy read starts again from the first turn on the next pass. The third busy pass in a round, or a pass 72 hours or less
  before the deadline, hands the round to a person.
- The inbox shows mail from the customer's address as the sender check reads it or in full lower case, so it shows at least
  every email the sender check could call the customer's.
- Sender checks forgive only the case of ASCII letters. Any other difference (ß for ss, a fullwidth letter, another case of a
  non-ASCII letter) makes another address. A customer's own non-ASCII address still matches itself.
- A blank or placeholder card fingerprint ties no orders into a case's history.
- In the v2 write-up, "no warehouse scan" on the earbuds means no scan of the return. Every order has a warehouse PACKED scan
  going out, so a missing scan of the return means something.

## The loop, payments and sending

- The agent's scope is the run's own payments. A new dispute that names a payment outside them never becomes a case, and that
  payment isn't read. Live mode reads only a story run's payments (`--data "<data>/story-<run>"`); on a data folder with no
  story run, a pass reads nothing and says so.
- A new dispute counts on the payment its own read names, even when Airwallex lists it under another of the run's payments.
  When its read fails before it names a payment, or names one outside the run, it counts on the first of the run's payments
  whose list showed it. When a list and a read name two of the run's payments, it counts on both.
- A case with a saved row this app can't read stays a person's case, with nothing sent for it. It counts on each of the run's
  payments whose list shows it. The counts and sums leave it out, and the money page says its numbers aren't final.
- When a payment's list of disputes doesn't answer, nothing is sent for that payment's cases on that pass.
- Known limit: if Airwallex's lists stopped filtering by payment and also stopped naming each dispute's payment, a new
  dispute whose read fails before it names a payment would count on the first of the run's payments. So the case on its real
  payment could still go out on that pass.
- An upload whose answer is lost or unreadable is tried again on the next pass, and each try may leave a file with Airwallex.
- If a crash or Ctrl+C loses a response's answer, it goes again as a retry, whose refusal says the first try may have landed.
- When Airwallex asks this app to wait (HTTP 429), on a response or on the login it needs, the next try waits as long as
  Airwallex says: at most 30 seconds, or 30 seconds when it says nothing this app can read.
- Airwallex's own words show as one line of at most 200 characters. A file id or a UUID in them, an account id among them,
  shows as "(a file id)" or "(an id)". Dispute and payment ids show as they came.
- Airwallex adds a summary file of its own to every challenge. It may also fill in the store's profile from the store's
  Airwallex account, and the order's number, currency and date from the payment (spike fixtures 066 and 073 show both).
  At a chargeback it may also add an IP address and a device id: Ending 1's recording shows both.
- Besides the terms and files a person approves, a challenge sends only fixed fields: this app's name, the standard challenge
  method, each file's name, type and short descriptions, and, at a chargeback, the product type. An accept that refunds also
  sends a fixed refund reason, REQUESTED_BY_CUSTOMER.
- The approval screen and the upload both build the plan's file again. If it doesn't come out exactly as planned, the screen
  refuses the approval and the upload drops the plan, so code decides the round again for a fresh approval.

## Spend and the eval

- Claude hasn't read the eval's cases yet, since the Claude account had no API credit. The report holds only scripted
  readers, and no price of a read is measured. Medium effort isn't measured either.
- A live eval run stops when 5 of its first 10 reads end in the model's own failure or a read-cap stop, or when the read
  cap stops 3 reads in a row. A read that cap stops isn't kept.
- After a change to the reading checks, `disputable eval refresh` checks every kept reading again for free.
- `disputable eval` exits 0 when done, 1 for an error its message names, 2 when a run stopped early, and 3 when a reading
  broke I1, I2 or I3.
- Before it sends a Claude request, the spend guard reserves the most it could cost. A request may fall back to another model
  when Claude declines partway, and the API then bills both answers, so the guard reserves two: about $0.80 for a read's
  first request, up to about $1.30 for the largest. So a run's spend can end up to that much below its `--cap`.
- One read may spend at most $2.50: fixed for pass, loop and story, and set by `disputable eval live --read-cap` in the eval.
- When a spend cap stops a read in a pass or loop run, that case goes to a person with code's draft, and a later case that
  still fits is read as usual. Once the run made the number of reads you said yes to, each later case goes to a person.
- An account with no credit gets HTTP 400 "Your credit balance is too low...". In a pass each case then goes to a person with
  code's draft, and an eval run stops at once and says to check the Console's billing page.
- The spend count uses what each answered request reports. A request the SDK sent again after a timeout, or one cut by
  Ctrl+C, may be billed but not counted, so the Console's bill is the final word.
- The eval's measured price of a read is what its recorded runs spent, divided by every read that cost money, kept or not.
  The eval dates kept readings and runs by the UTC day.

## The demo and its recordings

- Both endings were recorded live on the Airwallex sandbox on Nov 12, 2026 (UTC), with `--no-claude`, since the Claude
  account had no API credit then. Their manifests say "airwallex": "sandbox". They hold no Claude reading: every case Claude
  would read goes to a person with code's draft, and the demo says so.
- A story run with Claude stops when a Claude read fails, since the recording would show code's hand-off as Claude's
  reading. --lenient records the failed read, except one a spend cap stopped. A recording whose manifest says Claude read
  holds no failed read, and test_story_recordings checks it.
- A reading that holds what the public repo can't, such as an IP address the seed doesn't hold, is never kept or reused.
- Run the demo from a clone, after `uv sync`, which installs the project editable. The recordings live in fixtures/story, and
  an installed copy of the package doesn't hold them. The project pins Python 3.12, and only 3.12.10 has run the tests.
- When a replay stops matching its recording (the code changed after it was made), the bar and `demo --no-serve` say so, say
  to record the story again. `demo --no-serve` then exits 1.
- The stand-in (`scripts/record_standin.py`) replays a run on the tests' fake sandbox, tests/fake_airwallex.py. It isn't a
  recording of Airwallex, the demo says so, and it never replaces a sandbox recording.
