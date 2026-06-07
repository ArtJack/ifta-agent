# IFTA Agent — Requirements (SDD)

> Spec-driven development artifact. Written to define *what* the system must do and *why*,
> independent of implementation. See [design.md](design.md) for *how*.

## 1. Purpose
Turn a carrier's raw quarterly mileage and fuel-card exports into a **filing-ready IFTA
return**, automatically reviewed by an LLM agent before a human submits it — accurate enough
to put on a government tax form.

## 2. Users
- **Carriers / owner-operators** — upload raw files, receive a filing-ready packet.
- **Operator (admin)** — approves customers, oversees filings.
- **Future:** bookkeepers / permit services filing on behalf of multiple carriers (multi-tenant).

## 3. Functional requirements
- **FR-1 Intake.** Accept raw mileage + fuel files as CSV, Excel, or PDF, per client, per quarter.
- **FR-2 Normalize.** Parse and clean heterogeneous exports into a canonical mileage/fuel model.
- **FR-3 Compute.** Fleet MPG, taxable gallons per jurisdiction, per-jurisdiction tax and
  surcharge (KY/IN/VA), and net tax due/credit — using the correct quarter's rate matrix.
- **FR-4 Validate.** Rule-based preflight that flags missing/inconsistent data before computing.
- **FR-5 Review.** An LLM agent reviews the *computed* return against the regulations KB, the
  live rate matrix, and the client's filing history, and writes a pre-filing review note.
- **FR-6 Output.** Produce the gov-portal upload CSV, a review-ready Excel, per-truck breakdowns,
  and a human-readable review note.
- **FR-7 Multi-tenant isolation.** A client can only ever see/process its own data.
- **FR-8 Operator gate.** No customer file is processed until an admin has approved that customer.
- **FR-9 Delivery.** Return the packet to the customer (web download / Telegram / email).

## 4. Non-functional requirements
- **NFR-1 Correctness.** Match known-correct historical filings **to the penny**; enforced by
  regression tests that fail the build on any drift.
- **NFR-2 Trust boundary.** The LLM never *computes* a number that lands on the form — it only
  reviews deterministic output. Numbers come from Python; judgment comes from the agent.
- **NFR-3 Evaluability.** Agent and extractor quality measured against human-checked gold via a
  layered eval harness; only mechanical layers may block a filing.
- **NFR-4 Cost.** Routine reviews cheap by default (Haiku/Sonnet); escalate to Opus only for
  high-risk filings. Target ≤ ~$0.15 model cost per reviewed filing.
- **NFR-5 Security/PII.** Customer receipts, labels, traces, and Telegram IDs are git-ignored,
  never committed. Magic-link tokens, CAPTCHA, per-IP rate limiting, atomic writes.
- **NFR-6 Cheap to run.** No cloud GPU bill — backend on a Mac mini behind a Cloudflare Tunnel,
  frontend on Vercel.

## 5. Out of scope (current)
- Direct automated submission to each state portal (the carrier files the produced CSV).
- Fuel types beyond diesel.

## 6. Acceptance criteria
- Reproduces real historical filings to the penny (regression-tested). ✓
- 400+ automated tests passing (currently **427**). ✓
- In production for at least one real recurring carrier. ✓ (DM Express)
