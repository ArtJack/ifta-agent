# Receipt-Reading Heuristics

How a human expert reads messy trucking fuel receipts — captured from operator
annotations on 47 real receipts during the extraction eval. These rules drive the
vision `EXTRACTION_PROMPT` (`src/ifta/intake/extract.py`) and inform the review agent.

## Payment method — fleet vs personal vs unknown
- **Fleet card** if you see a fleet processor code — `TCH`, `WEX`, `Comdata`, `EFS`, or
  combined forms like `TCHPFJ` / `TCHPFL` (TCH = fleet processor, PFJ = Pilot/Flying J).
  A **truck #** and/or **driver ID** on the receipt also signals a fleet card — stations
  require them for fleet fuel.
- **Personal card** if `Company Name: PERSONAL`, `VISA CREDIT`/`DEBIT` is printed, or
  there is **no truck #/driver ID** (those aren't required for personal purchases).
- Otherwise **unknown** — don't guess.

## Card number — payment card, not loyalty card
- `card_last4` is the **payment** card (e.g. `VISA ****8920`).
- A number printed next to **points/rewards** is a **loyalty card** — never use it.
- Fully masked (`****`) → null.

## Invoice number — vendors name it differently
- The reference prints as **Invoice #**, **Receipt #**, or **Transaction #**.
- Priority: **Invoice # → Receipt # → Transaction #**.

## State — infer from the address when not printed
- If the 2-letter code isn't legible, **look up the city/address** to find the state
  (e.g. a city verified to be in `MT`). Only leave null if location is unreadable too.

## Date — formats and a sanity window
- Accept `MM/DD/YYYY`, `04 2026`, short `'26` (= 2026), `Apr 2026`.
- Current period is **mid-2026**: a far-future (2028) or very old year is suspicious —
  keep the most plausible reading at lower confidence, don't silently mark "wrong receipt."
- `DSL` = Diesel.

## Gallons / amount — cross-check with math
- If a digit is unclear, **calculate**: `gallons ≈ amount ÷ price-per-gallon`
  (e.g. `$227.48 ÷ $3.939 = 57.75`), or the inverse for a missing amount.
- A trailing `G` means gallons (don't misread `178.557G`).
- Only **diesel** counts toward gallons — not DEF/reefer.

## Sanity bounds — flag, don't silently accept
- A diesel fill is ~**30–300 gal** (tank max ~250–300); a single fill far above is a misread.
- Price/gal sits ~**$2–$8** (mid-2026); $10+ is suspect.
- Two fuels totaling **>400 gal** for one truck, or implausible spacing (a truck refuels
  ~every 2 days / ~750 mi), is suspicious — surface it in the review summary.
- **VOID / returned** lines (`240.53 VOID → Converted 233.56`): use the amount actually
  charged, never the void.

## Multi-receipt photos
- Drivers sometimes shoot **two receipts in one frame**. The goal is to parse each; for
  now, extract the most complete one and **flag the image for human review** (low
  confidence) — never silently pick one and proceed.

---

### Status
Encoded in `EXTRACTION_PROMPT` (where vision-appropriate): fleet/loyalty/invoice rules,
state-from-address, date formats + year sanity, the gallons math cross-check, the sanity
window, VOID handling, and the multi-receipt low-confidence flag. The full "parse *both*
receipts" behavior and a structured `anomalies` field for the review summary are noted
next steps.

_Source: operator annotations captured in `evals/receipt_labels.json` (`_notes`)._
