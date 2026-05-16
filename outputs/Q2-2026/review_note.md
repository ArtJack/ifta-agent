# IFTA Review Note

## Summary
TEST LOGISTICS LLC (KY base, 5 trucks) Q2-2026 nets $267.04 tax due on 120,466 fleet miles and 17,648.98 gallons (fleet MPG 6.83). Raw inputs parse cleanly with matching truck IDs in both miles and fuel files. The biggest blocker is a RATE_FALLBACK: Q2-2026 rates were unpublished, so the engine used Q1-2026 rates. KY and VA surcharge lines are present and Oregon is correctly $0 (WMT).

## Issues
- [blocker] RATE_FALLBACK: Q2-2026 IFTA rates not published; calculations used 1Q2026 rates. Per-state tax_due values (incl. $267.04 total) will change if any state revises rates. Do not submit until Q2-2026 rate matrix is confirmed.
- [info] SURCHARGE_VERIFY: KY surcharge $73.08 (696 gal × 0.105) and VA surcharge $113.54 (794 gal × 0.143) are included. Confirm portal renders both as separate lines.
- [info] OREGON_WMT: OR 2,290 miles report at $0 IFTA rate (weight-mile tax filed separately with ODOT). Ensure ODOT WMT return is filed.
- [info] MPG_SANITY: Fleet MPG 6.83 is within normal Class-8 diesel range (5.5–8.0). Per-truck MPGs 6.43–7.11 are tight and consistent — no anomaly. No historical baseline exists for this client (profile=none), so no trend check possible.
- [info] NO_CLIENT_HISTORY: client_id=test_logistics has no prior filings to compare against; this is the first quarter on record.

## Filing reminders
- Deadline: Q2-2026 IFTA return + payment due July 31, 2026. Late = $50 or 10% of tax due (greater) + 0.4167%/mo interest per IFTA Articles.
- Base state: Kentucky DOR — file via KY OneStop / IFTA portal. KY-specific upload format (not CDTFA CSV).
- KY surcharge: KY base 696 gal × $0.105 = $73.08. Must appear as a separate surcharge line on the KY portal.
- VA surcharge: VA 794 gal × $0.143 = $113.54. Separate surcharge line — easy to miss.
- Oregon WMT: OR IFTA tax = $0 by design. File the Oregon Weight-Mile Tax return separately with ODOT for the 2,290 OR miles.
- NY HUT: 1,461 NY miles — verify NY Highway Use Tax return is filed separately (HUT-100 series).
- NM WDT: 2,194 NM miles — verify New Mexico Weight Distance Tax return is filed separately if vehicles >26,000 lbs GVW.
- KYU: Base state KY: if any of T1–T5 has declared gross weight > 59,999 lbs, file the Kentucky Weight Distance Tax (KYU) separately.

## Next steps
- [ ] Confirm Q2-2026 IFTA rate matrix is published; re-run pipeline and re-verify total before submitting.: blocker
- [ ] Cross-check the 40 jurisdiction lines + 2 surcharge lines against the KY portal entry screen; confirm KY and VA surcharges post as separate rows.
- [ ] File Oregon WMT with ODOT (2,290 mi).
- [ ] Confirm/file NY HUT (1,461 mi) and NM WDT (2,194 mi) if vehicle weights trigger.
- [ ] Confirm KYU obligation for any truck > 59,999 lbs GVW; file separately if applicable.
- [ ] Spot-check a handful of large credit lines (NC -$210.33, MN -$93.56, UT -$91.72) — retain fuel receipts in case KY audits the credits.
- [ ] Save the per-truck workbook for T1–T5 to client records before clicking Submit.

## Agent run details

- **Model:** `claude-opus-4-7`
- **Wall time:** 30.2s
- **Model calls:** 2
- **Input tokens:** 19,229 (uncached 7,273 · cached-read 10,830 · cache-write 1,126)
- **Output tokens:** 1,887
- **Estimated cost:** $0.10 USD
