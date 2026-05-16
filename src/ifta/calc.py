"""IFTA quarterly calculations.

Formulas (matching the CDTFA online filing — verified against MENSHIKOV LLC
Q4 2025, total tax due $795.16):

  Fleet MPG     = round(total miles ÷ total gallons, 2)
  Taxable Gal   = round(state miles ÷ fleet MPG)      [whole gallons]
  Net Taxable   = Taxable Gal − round(Tax-Paid Gal)   [whole gallons]
  Tax Due       = round_half_up(Net Taxable × Rate, 2)

The two non-obvious points: fleet MPG is rounded to 2 decimals BEFORE
per-state calculation, and dollar amounts use round-half-up (not Python's
banker's rounding).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ifta.models import IFTA_JURISDICTIONS, CleanData
from ifta.rates import RateTable


def _round_half_up(value: float, ndigits: int = 2) -> float:
    q = Decimal(10) ** -ndigits
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _tax(net_gal: float, rate: float) -> float:
    """Compute tax = net_gal * rate using exact decimal arithmetic, then
    round half-up to 2 decimals. Matches CDTFA portal math exactly.

    Float multiplication (39 * 0.2850 == 11.114999...) loses the half-cent;
    converting both operands to Decimal first preserves it.
    """
    result = Decimal(str(int(round(net_gal)))) * Decimal(str(rate))
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class StateLine:
    state: str
    miles: float
    tax_paid_gal: float  # gallons purchased in this state
    taxable_gal: float
    net_taxable_gal: float
    rate: float
    tax_due: float
    is_surcharge: bool = False  # True for the separate KY/VA surcharge lines

    @property
    def is_credit(self) -> bool:
        return self.tax_due < 0

    @property
    def label(self) -> str:
        return f"{self.state} Surcharge" if self.is_surcharge else self.state


@dataclass
class TruckSummary:
    truck_id: str
    miles: float
    gallons: float

    @property
    def mpg(self) -> float:
        return self.miles / self.gallons if self.gallons else 0.0


@dataclass
class IftaReturn:
    quarter: str
    fuel: str
    fleet_miles: float
    fleet_gallons: float
    fleet_mpg: float
    trucks: list[TruckSummary]
    lines: list[StateLine]
    rate_source_quarter: str | None = None
    rate_fallback_used: bool = False
    rate_warning: str | None = None

    @property
    def total_tax_due(self) -> float:
        return sum(line.tax_due for line in self.lines)

    @property
    def total_taxable_gal(self) -> float:
        return sum(line.taxable_gal for line in self.lines)

    @property
    def total_tax_paid_gal(self) -> float:
        return sum(line.tax_paid_gal for line in self.lines)


def compute_per_truck_lines(
    data: CleanData, ret: IftaReturn, rates: RateTable
) -> dict[str, list[StateLine]]:
    """Per-truck Jurisdiction Summary lines, scoped to one truck's contribution.

    Uses the FLEET MPG and fleet rates so the sum of per-truck lines across
    all trucks reconciles back to the fleet's per-state line (modulo
    per-state integer rounding drift). Each truck gets its own surcharge
    line for KY/VA when it has miles in that state.

    Returns: dict mapping truck_id → list[StateLine] (sorted by state).
    """
    fleet_mpg = ret.fleet_mpg

    # Group raw inputs by (truck, state)
    miles_by_tk_st: dict[tuple[str, str], float] = {}
    for r in data.miles:
        miles_by_tk_st[(r.truck_id, r.state)] = (
            miles_by_tk_st.get((r.truck_id, r.state), 0.0) + r.miles
        )
    gallons_by_tk_st: dict[tuple[str, str], float] = {}
    for r in data.fuel:
        gallons_by_tk_st[(r.truck_id, r.state)] = (
            gallons_by_tk_st.get((r.truck_id, r.state), 0.0) + r.gallons
        )

    truck_ids = sorted(
        {t.truck_id for t in ret.trucks},
        key=lambda t: (t == "unknown", t),
    )

    per_truck: dict[str, list[StateLine]] = {}
    for truck_id in truck_ids:
        states = sorted(
            {s for (t, s) in miles_by_tk_st if t == truck_id}
            | {s for (t, s) in gallons_by_tk_st if t == truck_id}
        )
        lines: list[StateLine] = []
        for s in states:
            miles = miles_by_tk_st.get((truck_id, s), 0.0)
            tax_paid_gal = gallons_by_tk_st.get((truck_id, s), 0.0)
            taxable_gal = round(miles / fleet_mpg) if fleet_mpg else 0.0
            net = taxable_gal - round(tax_paid_gal)
            if s not in IFTA_JURISDICTIONS:
                rate = 0.0
                tax_due = 0.0
            else:
                rate = rates.get(s, 0.0)
                tax_due = _tax(net, rate)
            lines.append(
                StateLine(
                    state=s,
                    miles=miles,
                    tax_paid_gal=tax_paid_gal,
                    taxable_gal=float(taxable_gal),
                    net_taxable_gal=float(net),
                    rate=rate,
                    tax_due=tax_due,
                )
            )
            # Surcharge line: only when this truck has miles in that state.
            sur_rate = rates.surcharge(s) if hasattr(rates, "surcharge") else 0.0
            if sur_rate > 0 and miles > 0:
                sur_tax = _tax(float(taxable_gal), sur_rate)
                lines.append(
                    StateLine(
                        state=s,
                        miles=0.0,
                        tax_paid_gal=0.0,
                        taxable_gal=float(taxable_gal),
                        net_taxable_gal=float(taxable_gal),
                        rate=sur_rate,
                        tax_due=sur_tax,
                        is_surcharge=True,
                    )
                )
        per_truck[truck_id] = lines
    return per_truck


def compute_return(data: CleanData, rates: RateTable) -> IftaReturn:
    miles_by_state: dict[str, float] = {}
    miles_by_truck: dict[str, float] = {}
    for mileage_record in data.miles:
        miles_by_state[mileage_record.state] = (
            miles_by_state.get(mileage_record.state, 0.0) + mileage_record.miles
        )
        miles_by_truck[mileage_record.truck_id] = (
            miles_by_truck.get(mileage_record.truck_id, 0.0) + mileage_record.miles
        )

    gallons_by_state: dict[str, float] = {}
    gallons_by_truck: dict[str, float] = {}
    for fuel_record in data.fuel:
        gallons_by_state[fuel_record.state] = (
            gallons_by_state.get(fuel_record.state, 0.0) + fuel_record.gallons
        )
        gallons_by_truck[fuel_record.truck_id] = (
            gallons_by_truck.get(fuel_record.truck_id, 0.0) + fuel_record.gallons
        )

    fleet_miles = sum(miles_by_truck.values())
    fleet_gallons = sum(gallons_by_truck.values())
    # CDTFA filing rounds fleet MPG to 2 decimals before per-state
    # taxable-gallon calculation. Verified against MENSHIKOV LLC Q4 2025.
    fleet_mpg = _round_half_up(fleet_miles / fleet_gallons, 2) if fleet_gallons else 0.0

    trucks = sorted(
        {*miles_by_truck, *gallons_by_truck},
        key=lambda t: (t == "unknown", t),
    )
    truck_summaries = [
        TruckSummary(t, miles_by_truck.get(t, 0.0), gallons_by_truck.get(t, 0.0)) for t in trucks
    ]

    all_states = sorted(set(miles_by_state) | set(gallons_by_state))
    lines: list[StateLine] = []
    for s in all_states:
        miles = miles_by_state.get(s, 0.0)
        tax_paid_gal = gallons_by_state.get(s, 0.0)
        # The IFTA portal rounds taxable gallons to whole gallons (verified
        # against MENSHIKOV LLC Q4 2025 CDTFA filing).
        taxable_gal = round(miles / fleet_mpg) if fleet_mpg else 0.0
        # Tax-paid gallons are also rounded for portal display, but we keep
        # 3-decimal precision internally and round only when reporting.
        net = taxable_gal - round(tax_paid_gal)
        if s not in IFTA_JURISDICTIONS:
            rate = 0.0
            tax_due = 0.0
        else:
            rate = rates.get(s, 0.0)
            tax_due = _tax(net, rate)
        lines.append(
            StateLine(
                state=s,
                miles=miles,
                tax_paid_gal=tax_paid_gal,
                taxable_gal=float(taxable_gal),
                net_taxable_gal=float(net),
                rate=rate,
                tax_due=tax_due,
            )
        )
        # Surcharge line (KY, VA): same taxable gallons, surcharge rate,
        # miles=0, no tax-paid credit (surcharge isn't paid at the pump).
        sur_rate = rates.surcharge(s) if hasattr(rates, "surcharge") else 0.0
        if sur_rate > 0:
            sur_tax = _tax(float(taxable_gal), sur_rate)
            lines.append(
                StateLine(
                    state=s,
                    miles=0.0,
                    tax_paid_gal=0.0,
                    taxable_gal=float(taxable_gal),
                    net_taxable_gal=float(taxable_gal),  # surcharge: net = taxable
                    rate=sur_rate,
                    tax_due=sur_tax,
                    is_surcharge=True,
                )
            )

    return IftaReturn(
        quarter=rates.quarter,
        fuel=rates.fuel,
        fleet_miles=fleet_miles,
        fleet_gallons=fleet_gallons,
        fleet_mpg=fleet_mpg,
        trucks=truck_summaries,
        lines=lines,
        rate_source_quarter=rates.source_quarter,
        rate_fallback_used=rates.fallback_used,
        rate_warning=rates.warning,
    )
