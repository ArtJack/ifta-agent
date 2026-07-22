"""Canonical data structures used across the pipeline."""

from collections.abc import Iterable
from dataclasses import dataclass, field

# IFTA member jurisdictions (US states + Canadian provinces).
US_STATES: frozenset[str] = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ]
)
CA_PROVINCES: frozenset[str] = frozenset(
    ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
)
JURISDICTIONS: frozenset[str] = US_STATES | CA_PROVINCES

# Jurisdictions that do NOT participate in IFTA fuel-tax filing, so they carry no
# tax rate and are excluded from the taxable lines. AK, HI, DC plus the three
# Canadian territories — Yukon (YT), Northwest Territories (NT), Nunavut (NU) —
# are not IFTA members. Mirrors data/regulations.json
# special_states.non_ifta_jurisdictions; keep the two in sync.
NON_IFTA: frozenset[str] = frozenset({"AK", "HI", "DC", "YT", "NT", "NU"})
IFTA_JURISDICTIONS: frozenset[str] = JURISDICTIONS - NON_IFTA


@dataclass
class MileageRecord:
    truck_id: str
    state: str  # 2-letter
    miles: float


@dataclass
class FuelRecord:
    truck_id: str
    state: str
    gallons: float
    tax_paid: float = 0.0  # state-fuel-tax already paid at the pump


@dataclass
class CleanData:
    """Normalized result of ingest stage.

    `truck_drivers` and `truck_cards` are truck-level lookups (not per-record)
    since they're properties of the truck/driver pairing, not of each trip.
    """

    miles: list[MileageRecord] = field(default_factory=list)
    fuel: list[FuelRecord] = field(default_factory=list)
    truck_drivers: dict[str, str] = field(default_factory=dict)
    truck_cards: dict[str, str] = field(default_factory=dict)

    @property
    def trucks(self) -> list[str]:
        seen: dict[str, None] = {}
        for mileage_record in self.miles:
            seen.setdefault(mileage_record.truck_id, None)
        for fuel_record in self.fuel:
            seen.setdefault(fuel_record.truck_id, None)
        return list(seen)

    @property
    def states(self) -> list[str]:
        seen: dict[str, None] = {}
        for mileage_record in self.miles:
            seen.setdefault(mileage_record.state, None)
        for fuel_record in self.fuel:
            seen.setdefault(fuel_record.state, None)
        return sorted(seen)

    def driver(self, truck_id: str) -> str | None:
        return self.truck_drivers.get(truck_id)

    def card(self, truck_id: str) -> str | None:
        return self.truck_cards.get(truck_id)


def is_jurisdiction(code: str) -> bool:
    return isinstance(code, str) and code.strip().upper() in JURISDICTIONS


def normalize_state(value: object) -> str | None:
    if value is None:
        return None
    code = str(value).strip().upper()
    if code in JURISDICTIONS:
        return code
    return None


def coalesce_records(records: Iterable[MileageRecord]) -> list[MileageRecord]:
    """Sum duplicate (truck, state) rows."""
    acc: dict[tuple[str, str], float] = {}
    for r in records:
        key = (r.truck_id, r.state)
        acc[key] = acc.get(key, 0.0) + (r.miles or 0.0)
    return [MileageRecord(t, s, m) for (t, s), m in acc.items()]


def coalesce_fuel(records: Iterable[FuelRecord]) -> list[FuelRecord]:
    acc: dict[tuple[str, str], list[float]] = {}
    for r in records:
        key = (r.truck_id, r.state)
        if key not in acc:
            acc[key] = [0.0, 0.0]
        acc[key][0] += r.gallons or 0.0
        acc[key][1] += r.tax_paid or 0.0
    return [FuelRecord(t, s, g, tp) for (t, s), (g, tp) in acc.items()]
