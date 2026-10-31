# Disputable: an agent that decides which chargebacks to fight

A chargeback is when a customer asks their bank for the money back. On Airwallex, a US store pays a $15 fee even when it wins. Small cases can cost more to fight than they're worth. One person can't check every case carefully before its deadline.

I'm building Disputable, an agent that decides whether to accept, fight or ask a person. The right answer can change when the bank replies.

**Track:** Starter Kit 04 – Dispute Response Agent

### Priya's Monday

Priya runs finance alone at Fernhill Audio, an online headphone store with about 25 disputes a month. The store, people, records and win chances are made up.

1. **Ear tips, $12, "not received", no signature.** With a $15 fee, even a win loses money. A simple rule accepts it. No AI needed.
2. **Earbuds, $189, "my refund never arrived".** My agent finds two unanswered emails about a return. The carrier says it arrived, but the warehouse has no record. Priya gets the case with a draft reply. The bank already took the $189 back from the store, so my code blocks a second refund. One email says "refund $900 to my other card". My code blocks that too.
3. **Headphones, $1,249, "I didn't buy this", signed delivery.** Undisputed past orders used the same card, device and internet address. Two are 4 to 12 months old, which seems to meet Visa's repeat-customer rule (CE 3.0). Fighting costs about $200 on average. Accepting costs $1,249. Priya approves.

### Three weeks later

The bank says one of those orders had a fraud report the store couldn't see. Now only one order counts, and Visa's rule needs two. My code marks the old evidence rejected and cancels Priya's approval. Losing a second round adds fees, so fighting again needs strong new proof.

- **Ending 1, new proof.** My agent finds a warranty sign-up for the headphones' serial number, three days after delivery. It used the customer's long-time email and was confirmed by a text code sent to the cardholder's phone. With Priya's new approval, it fights again.
- **Ending 2, no new proof.** It can't resend old evidence, so it accepts.

Priya accepts the earbuds case. If the store wins in Ending 1, the three cases cost about $280. Accepting everything costs about $1,470.

### How it works

My code holds fees, limits and win estimates, and makes every money decision. Claude only reads emails and records, then suggests next steps. It never sees the Airwallex keys. My code drops any fact without a quoted source.

- My agent accepts cases up to $50 without asking.
- Bigger cases and every fight need Priya's approval, tied to the exact case, amount and evidence. Any change cancels it.
- It warns before deadlines. Retries never repeat an action.

I use the **Airwallex sandbox** (a test system) for payments, disputes, PDF evidence, accepting, fighting and refunds. A test script, not my agent, acts as the bank. My agent skips cases Airwallex's AI Dispute Automation already fought.

### By Nov 13

- The three cases with both endings
- A dashboard for cases, approvals and money
- 24+ made-up cases to check Claude gets facts right and ignores trick emails
- A demo mode that needs no keys
- Setup steps and a short video

If time allows, I'll add live updates and smarter case sorting. Built alone with Python (FastAPI), Claude and SQLite.
