"""Load-test the review agent at fleet scale.

Generates a synthetic N-truck fleet (random miles + fuel across ~40 states),
runs the full deterministic pipeline + AI pre-filing review at production
settings, and reports wall time / output tokens / cost / findings.

The point it demonstrates: review time is bounded by the number of *findings*
(anomalies), not the number of trucks — the agent reviews the aggregate return
plus exception trucks, and the per-state return caps at ~48 jurisdictions
regardless of fleet size. A 100-truck fleet reviews in roughly the same time as
a 5-truck one.

    python scripts/load_test_fleet.py            # 100 trucks
    python scripts/load_test_fleet.py --trucks 250

No real customer data is touched — the fleet is synthetic (seeded for
reproducibility).
"""

from __future__ import annotations

import argparse
import csv
import random
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

STATES = [
    "AL", "AR", "AZ", "CA", "CO", "CT", "FL", "GA", "IA", "ID", "IL", "IN", "KS",
    "KY", "LA", "MD", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NM", "NV",
    "NY", "OH", "OK", "OR", "PA", "SC", "SD", "TN", "TX", "UT", "VA", "WA", "WI",
    "WV", "WY",
]


def generate_fleet(inbox: Path, n_trucks: int, seed: int = 42) -> tuple[int, int]:
    rng = random.Random(seed)
    inbox.mkdir(parents=True, exist_ok=True)
    miles_rows, fuel_rows = [], []
    for t in range(1, n_trucks + 1):
        tid = f"T{t:03d}"
        states = rng.sample(STATES, rng.randint(8, 14))
        for s in states:
            miles_rows.append((tid, s, rng.randint(50, 4000)))
        for s in rng.sample(states, rng.randint(2, 5)):
            fuel_rows.append((tid, s, rng.randint(80, 600)))
    with open(inbox / "synth_miles.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["truck", "state", "miles"])
        w.writerows(miles_rows)
    with open(inbox / "synth_fuel.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["truck", "state", "gallons"])
        w.writerows(fuel_rows)
    return len(miles_rows), len(fuel_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trucks", type=int, default=100)
    ap.add_argument("--quarter", default="Q1-2026")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workdir", default="/tmp/ifta_load_test")
    args = ap.parse_args()

    work = Path(args.workdir)
    inbox, out = work / "inbox", work / "out"
    out.mkdir(parents=True, exist_ok=True)
    n_mi, n_fu = generate_fleet(inbox, args.trucks)
    print(f"generated {args.trucks} trucks: {n_mi} mile-rows, {n_fu} fuel-rows", flush=True)

    from ifta.agent import review

    t0 = time.monotonic()
    try:
        note, metrics = review(
            args.quarter,
            inbox_dir=inbox,
            output_dir=out,
            client_name=f"Synthetic {args.trucks}-Truck Fleet",
            base_state="CA",
            model=args.model,
            effort=args.effort,
        )
        wall = time.monotonic() - t0
        n = len(note.issues) + len(note.filing_reminders) + len(note.next_steps)
        print(f"\n{args.trucks}-TRUCK REVIEW ({args.model}/{args.effort}):")
        print(f"  wall:     {wall:.1f}s ({wall / 60:.1f} min)")
        print(f"  output:   {metrics.output_tokens:,} tokens")
        print(f"  cost:     ${metrics.estimated_cost_usd:.4f}")
        print(f"  findings: {n}  status={note.filing_status}")
    except Exception as e:
        wall = time.monotonic() - t0
        print(f"\n{args.trucks}-TRUCK REVIEW FAILED after {wall:.1f}s: {type(e).__name__}: {e}")
        print("(If this is a JSON-truncation error, the fleet's output exceeded the review "
              "token budget — raise DEFAULT_MAX_TOKENS['review'] or cap findings to top-N.)")


if __name__ == "__main__":
    main()
