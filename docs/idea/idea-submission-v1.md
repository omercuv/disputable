# Title

ChargeWise: an agent that decides which chargebacks are worth fighting

# Description

**Track:** Starter Kit 04 – Dispute Response Agent (Revenue Protection)

**The problem**
Online merchants get a steady queue of chargebacks, each with a deadline, an amount at risk and scattered evidence (order history, delivery scans, support emails). The merchant pays a dispute fee even when they win, so fighting a small dispute can cost more than accepting it. Small finance teams usually either accept everything (and lose revenue) or fight everything (and lose time and fees). Missing a deadline means losing automatically.

**What ChargeWise does**
ChargeWise works through the dispute queue and makes one decision per case: **accept and refund, challenge with evidence, or escalate to a person**. It decides on economics and evidence strength, not gut feel:

- **Deterministic code** holds the money logic: amount at risk, configurable dispute fee, deadline, an expected-value score for challenging vs. accepting, and approval thresholds.
- **Claude** handles the unstructured work: reading order records, delivery notes and customer emails, matching them to what each Visa reason code requires, scoring evidence strength, and drafting the rebuttal.
- **The agent re-decides when new information arrives.** If the issuing bank rejects our evidence, ChargeWise re-evaluates the case with the new state instead of repeating the first decision.

**Demo (three cases plus a twist)**
1. A large fraud claim (Visa 10.4): the device fingerprint and IP match three earlier undisputed orders and delivery was signed for. Decision: **challenge**, with an evidence packet uploaded as PDF.
2. A small "not received" claim (13.1) with no delivery signature, where the fee is larger than the amount: **accept and refund** automatically.
3. A "credit not processed" claim (13.6) where the customer emailed support twice with no reply: **escalate** to a human with a summary and recommendation.
4. Twist: the issuer rejects the evidence on case 1 and the dispute moves to the chargeback stage. The agent never resends the same file. It looks for new evidence, re-scores the case, and either challenges again with that new evidence (after a person approves) or accepts. It shows the before/after reasoning and the final status.

**Airwallex APIs used**
Payment intents (create and confirm with the test card), dispute simulation, `GET /pa/payment_disputes`, evidence upload to the files host, `accept` and `challenge` endpoints, escalate/resolve simulations, and `GET /pa/refunds`. Docs MCP and Developer MCP are used during development, and REST is used for accept/challenge.

**Safety and controls**
- The model gets typed tools only, never raw API credentials.
- Any challenge or refund above a threshold needs a person's approval, and that approval is tied to the specific case, amount, currency and evidence shown.
- Every create call uses a unique `request_id` with safe retries, so no duplicate actions are taken.
- Deadlines are enforced in code, with a full audit log of every decision and the reason for it.
- Sandbox-only simulation calls sit behind one interface so they can be swapped for live webhooks later.

**Stack**
Python backend (FastAPI), Claude for reasoning and tool use, SQLite/Postgres for case state and the audit log, and a lightweight web dashboard showing the dispute queue, each decision and its reasoning.

**Team**
Solo full-stack developer (back-end, front-end, databases).
