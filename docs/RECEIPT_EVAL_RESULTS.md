# Receipt Extraction — Eval Results

A worked, end-to-end evaluation of a vision extractor on a high-stakes (tax-filing)
task: build a gold set, measure, adjudicate, encode domain expertise, and let the
harness **gate** the change. Harness + method: [`RECEIPT_EVAL_GUIDE.md`](RECEIPT_EVAL_GUIDE.md).

## Setup
- **Task:** extract structured fields (date, state, gallons, amount, vendor, card,
  payment method, …) from photos of trucking fuel receipts.
- **Dataset:** 47 real receipts, hand-labeled **blind** by a domain expert, tagged by
  difficulty and annotated with reading notes. *(Receipts and labels are customer PII —
  git-ignored, never committed.)*
- **Model:** Claude Sonnet (vision), driven by `ifta receipt-eval run`.
- **Scoring:** five outcomes per field — CORRECT / WRONG / MISSING / HALLUCINATION /
  CORRECT_NULL. **Tax-critical** fields = date, state, gallons (a wrong one can mis-file
  a return).

## What the harness found

**1. The extractor is already excellent where it matters.** Against a corrected gold,
date/state/gallons scored **100%**, with **zero** dangerous (wrong/hallucinated)
tax-critical errors across 47 receipts.

**2. The first two "errors" were the *oracle's*, not the model's.** The raw run read as
96% tax-safe with 2 dangerous receipts — but adjudicating against the photos showed the
model was right both times: one was a human year typo (labeled `2025`, the receipt reads
`2023`); the other was a *two-receipts-in-one-photo* image the labeler left blank. Fixing
the gold → 100%. The eval's first job was QA on the gold itself.

**3. A real edge case surfaced:** multiple receipts in one photo. The model silently
picked one *at high confidence* — a data-intake risk worth guarding.

## Turning domain notes into a change — and gating it

The labeler annotated 36 receipts with reading heuristics (fleet-card codes TCH/WEX/PFJ;
loyalty- vs payment-card; state-from-address; invoice/transaction/receipt naming; date
formats; a multi-receipt rule). These were distilled into
[`RECEIPT_HEURISTICS.md`](RECEIPT_HEURISTICS.md) and folded into the extraction prompt —
then measured:

| field | original | v1 (all rules) | v2 (refined) |
|---|--:|--:|--:|
| tax-safe (date+state+gallons) | 100% | 98% | **100%** |
| gallons | 100% | 98% | **100%** |
| amount | 98% | 96% | 98% |
| card_last4 | 91% | **62%** | **94%** |
| vendor | 79% | 83% | 83% |
| payment_method | 41% | 48% | 41% |
| **multi-receipt photo flagged for review?** | no (conf 0.97) | **yes (0.40)** | **yes (0.40)** |

**v1 (every rule) regressed `card_last4` 91% → 62%** and nudged a tax-critical field — a
well-meaning change making the high-stakes path *worse*. **v2** kept the one proven win
(the **multi-receipt guard**: the model now drops confidence 0.97 → 0.40 on a two-receipt
photo so a human reviews it) and reverted the aggressive `card_last4` / recompute rules.
Result: **tax-critical holds at 100%, `card_last4` recovers, no regressions.** Only the
change that measurably helped with no downside shipped.

## Takeaways
- The model was trustworthy on tax-critical fields all along; the eval's value was
  **proving it**, catching gold errors, and surfacing an edge case.
- A "prompt improvement" built from real domain expertise still **regressed a field** —
  and the gate stopped it from shipping to a tax path. Decisions came from data, not vibes.
- `payment_method`'s ceiling here is **label ambiguity** (the labeler's conservative
  "unknown" vs the model's defensible "fleet_card"), not the model — a note for the next
  labeling pass.

*Raw labels, predictions, and receipt images are customer PII and are git-ignored; this
write-up is the methodology and aggregate numbers only.*
