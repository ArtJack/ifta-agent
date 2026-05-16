# `scripts/` — One-off data extractors

These run **outside** the pipeline. They parse historical PDFs/spreadsheets
into the structured JSON files in `data/` so the agent can read them via
its tools.

| Script | Reads | Writes |
|---|---|---|
| `extract_my_truck.py` | `../MyTruck/*.pdf` (CDTFA filing PDFs) | `data/my_truck_history.json`, `data/my_truck_profile.json` |
| `extract_david.py` | `../David/IFTA 2025 ACTIVED.xlsx` | `data/david_history.json`, `data/david_profile.json` |

Run from the project root:

```bash
.venv/bin/python scripts/extract_my_truck.py
.venv/bin/python scripts/extract_david.py
```
