# Receipt Extraction — QA & Eval Guide

How to measure whether the receipt-vision extractor is good enough to trust, and how
**you** act as the human oracle. Written for someone who knows testing: think of this as
black-box testing of an extraction function whose oracle is *you*, with a confusion matrix
per field.

---

## 1. Why this exists

The extractor reads a fuel-receipt photo into structured fields. Those fields feed a
**government tax filing**. We can't ship "it usually works." We need numbers:

- How often is `gallons` / `state` / `date` exactly right?
- When the model is *confident*, is it actually right? (So we know what to auto-accept.)
- Where does it fail — faded receipts? handwriting? specific states?

The payoff: today every receipt needs human review. With eval numbers, you can **safely
auto-accept the easy, high-confidence ones** and spend your review time only where it
matters. You earn automation with evidence, not vibes.

---

## 2. The mental model

**You are the oracle.** For each receipt you write down the *true* values — the ground
truth. The model then makes its guess. We compare. Three rules make this trustworthy:

1. **Label blind.** Never look at the model's answer before you label. If you see its
   guess first, you'll unconsciously agree with it (anchoring bias) and the eval becomes
   worthless. The `label` command never shows you predictions.
2. **Score per field, not per receipt.** A receipt that's 90% right can still have a wrong
   `gallons` that mis-files the return. We grade each field.
3. **Distinguish five outcomes** — this is the heart of it:

| Outcome | Gold (you) | Model | Meaning | How bad? |
|---|---|---|---|---|
| **CORRECT** | value | same value | nailed it | ✅ |
| **CORRECT_NULL** | blank | blank | correctly abstained | ✅ |
| **MISSING** | value | blank | model gave up | 🟡 safe — *you* catch a blank |
| **WRONG** | value | different value | confidently incorrect | 🔴 dangerous |
| **HALLUCINATION** | blank | a value | invented data | 🔴 dangerous |

The job of the eval is to push **WRONG + HALLUCINATION on `date`, `state`, `gallons`**
toward **zero**. A MISSING is fine — the existing review net holds blanks for you. A WRONG
gallons is the thing that can silently corrupt a filing.

---

## 3. Build a representative set (equivalence partitioning)

Don't grab 5 clean receipts and call it tested. Curate ~**30–50** that cover the real
partitions, with the *messy* ones over-represented (that's where failures live):

- **Quality:** clean, faded thermal, crumpled, glare/flash, motion-blur, torn/partial.
- **Content:** handwritten unit numbers, multiple line items, **DEF + diesel on one
  receipt**, reefer fuel, two pumps summed, cash vs fleet card.
- **Boundaries:** a receipt dated **just outside** the quarter, a state you rarely buy in,
  a brand the model hasn't seen, a foreign (Canadian) province.

Put them in `evals/receipts/`. Keep this set **stable** — it's your regression baseline, not
the live `inbox/fuel-photos/` drop folder that gets cleared each quarter. Tag each with a
difficulty when you label, so the report can show "faded receipts: 60% tax-safe."

---

## 4. The workflow (your standing loop)

```bash
# 1. LABEL — blind. Each photo opens; you type the true values. Resumable.
ifta receipt-eval label

# 2. RUN — the model extracts every labeled receipt (one vision call each; costs $).
ifta receipt-eval run --model claude-sonnet-4-6

# 3. REPORT — accuracy, confidence calibration, error list.
ifta receipt-eval report --model claude-sonnet-4-6
```

Labels live in `evals/receipt_labels.json`, predictions are cached per model in
`evals/predictions/`, so `report` is free to re-run.

---

## 5. The labeling rubric (the most important part)

**Your labels are the oracle. An inconsistent or guessed label poisons every number.**
The single golden rule:

> **If you cannot clearly read it, leave it BLANK.** A blank gold means "the model should
> abstain here too." Never type a value you're guessing — you'd punish the model for being
> honestly uncertain and reward it for hallucinating.

Per-field rules — apply them the *same way every time*:

| Field | Rule |
|---|---|
| **date** | The **purchase** date, ISO `YYYY-MM-DD`. If only `MM/DD` is legible and the year is obvious from the quarter, use it. Truly unreadable → blank. |
| **state** | Where the **station physically is** (from the printed address) — 2-letter code. **Not** the chain's HQ. If only the city is legible and you're sure of the state, fill it; otherwise blank. |
| **gallons** | The **diesel** quantity from the pump line, to the decimals printed. **Not** DEF, not reefer (unless reefer *is* the fuel), not a dollar amount. Two diesel pumps on one receipt → sum them and note it. |
| **amount** | The **fuel** dollar amount (prefer the fuel subtotal). If only a grand total incl. snacks is visible, use it but tag difficulty `partial`. |
| **vendor** | Brand/truck stop ("Pilot", "Love's", "TA"). Scored leniently. |
| **truck_id** | Unit number **only if written on the receipt** (often handwritten). |
| **card_last4** | Last 4 digits of the card. |
| **payment_method** | `fleet_card` / `personal_card` / `cash` / `unknown`. |

When you and the model disagree later, you'll sometimes find **your label was wrong** — that's
normal and good (see §7).

---

## 6. Reading the report & setting thresholds

The report gives you four things:

**Per-field outcomes.** Look at the ⚠️ tax-critical rows first. You want `wrong` and `halluc`
at or near **0**. A high `missing` is acceptable (humans catch blanks). Example read:
*"gallons: 47 correct, 0 wrong, 3 missing → never wrong, just shy on 3 hard photos. Trust it."*

**Tax-safe rate.** % of receipts where date **and** state **and** gallons are all correct.
This is your headline "could I have auto-filed this receipt?" number.

**Confidence calibration** — this is how you set the auto-accept threshold:

| model confidence | graded fields | accuracy |
|---|--:|--:|
| 0.90-1.00 | 80 | 99% |
| 0.80-0.90 | 22 | 86% |
| 0.50-0.80 | 14 | 64% |

Read it as: *"above 0.90, the model is 99% right → auto-accept those. Between 0.80–0.90 it's
86% → still review. Below 0.80 → always review."* Pick the bucket that clears your bar (for a
tax filing, demand ≥99% on gallons/state) and that becomes the line between auto-accept and
human review.

**Error list.** Every receipt with a dangerous tax-critical error, for adjudication.

---

## 7. Adjudication — the oracle can be wrong too

For each disagreement, decide **who is actually right** by looking at the photo again:

- **Model wrong, you right** → a real model error. If a pattern emerges (always misreads
  glare on Pilot receipts), that's a prompt-improvement signal.
- **Model right, you wrong** → fix your gold label. This is normal; it *improves* the set.
- **Genuinely ambiguous** (the photo truly doesn't say) → the gold should be **blank**, and
  the model should ideally be MISSING, not guessing.

Re-run `report` after fixing labels — the numbers only mean something against a clean oracle.

---

## 8. Regression discipline

- **Freeze the set.** Once labeled, treat `evals/receipts/` + `receipt_labels.json` as a
  baseline. Re-run after **any** change to the extraction prompt or model and compare —
  did accuracy go up or down? This is exactly your `evals/cases/` agent suite, but for
  extraction.
- **Don't tune and report on the same data.** If you tweak the prompt until it aces the set,
  you've overfit. As the set grows, hold out a slice you *don't* tune on and report final
  numbers there.
- **Cadence:** run before any commit that touches `src/ifta/intake/extract.py` (the prompt)
  or when you change the model. It's cheap insurance against a silent extraction regression
  reaching a real filing.

---

## 9. Data handling

Real receipts contain PII (addresses, card last-4, driver names). `evals/receipts/`,
`receipt_labels.json`, and `evals/predictions/` are **git-ignored** — they stay on the Mac
mini and never get committed. If you ever want a shareable regression set, anonymize first.
