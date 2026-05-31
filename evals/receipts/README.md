# Eval receipt corpus

Put your **curated, stable** set of receipt photos here — this is the regression baseline
for the vision extractor, separate from the live `inbox/fuel-photos/` drop folder.

Aim for ~30–50 that cover the real partitions, messy ones over-represented:

- **quality:** clean, faded thermal, crumpled, glare, blur, torn/partial
- **content:** handwritten unit #, DEF + diesel together, reefer, two pumps, cash vs fleet
- **boundaries:** dated just outside the quarter, rare state, unseen brand, Canadian province

Then:

```bash
ifta receipt-eval label                              # you label them (blind)
ifta receipt-eval run --model claude-sonnet-4-6      # model extracts them
ifta receipt-eval report --model claude-sonnet-4-6   # accuracy + calibration
```

Full method: [`docs/RECEIPT_EVAL_GUIDE.md`](../../docs/RECEIPT_EVAL_GUIDE.md).

These images and the labels/predictions derived from them are **git-ignored** (they contain
customer PII) and never leave the Mac mini.
