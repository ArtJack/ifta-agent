# `data/` — Knowledge base + cached reference data

| File | Purpose | Updated by |
|---|---|---|
| `regulations.json` | Authoritative IFTA rules — deadlines, surcharge logic, special states, audit rules, base-state filing notes (Kentucky DOR + CDTFA California). The agent grounds every regulatory answer in this. | Manually, when rules change |
| `rates/<NQ20YY>.csv` | Cached IFTA tax-rate matrix from iftach.org. One file per quarter (e.g. `1Q2026.csv`). | Auto-fetched by `ifta.rates.fetch_rates()`. Pass `--refresh-rates` to re-fetch. |
| `david_history.json` | DM EXPRESS INC's parsed quarterly data (Q1 2025 – Q1 2026) from David's `IFTA 2025 ACTIVED.xlsx`. | `scripts/extract_david.py` |
| `david_profile.json` | DM EXPRESS INC operating profile derived from history (fleet evolution, fuel vendors, routes, anomaly thresholds). Base state = Kentucky. | `scripts/extract_david.py` |
| `my_truck_history.json` | 16 quarters of MENSHIKOV LLC's filed CDTFA returns (Q4 2021 – Q4 2025), parsed from PDFs in `../MyTruck/`. **Retired LLC — reference data only.** | `scripts/extract_my_truck.py` |
| `my_truck_profile.json` | MENSHIKOV LLC operating profile. Reference only. | `scripts/extract_my_truck.py` |

To regenerate the parsed files after adding new historical PDFs/xlsx:

```bash
.venv/bin/python scripts/extract_my_truck.py
.venv/bin/python scripts/extract_david.py
```
