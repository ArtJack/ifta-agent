"""Portal worksheet CSV regression guards."""

from __future__ import annotations

import csv

from ifta.calc import IftaReturn, StateLine, TruckSummary
from ifta.report import write_portal_csv


def _sample_return(*, fallback: bool = False) -> IftaReturn:
    return IftaReturn(
        quarter="2Q2026",
        fuel="diesel",
        fleet_miles=120_466,
        fleet_gallons=17_649,
        fleet_mpg=6.83,
        trucks=[TruckSummary("T1", 120_466, 17_649)],
        lines=[
            StateLine("CA", 1000, 200, 146, -54, 0.79, -42.66),
            StateLine("KY", 500, 0, 73, 73, 0.287, 20.95),
            StateLine("KY", 0, 0, 73, 73, 0.105, 7.67, is_surcharge=True),
        ],
        rate_source_quarter="1Q2026" if fallback else "2Q2026",
        rate_fallback_used=fallback,
        rate_warning=(
            "2Q2026 IFTA rates were not published, so calculations used 1Q2026 rates."
            if fallback
            else None
        ),
    )


def _rows(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_generic_portal_csv_uses_plain_fuel_label_and_blanks_total_gallon_drift(tmp_path) -> None:
    path = write_portal_csv(_sample_return(), tmp_path / "ifta_portal.csv")
    rows = _rows(path)

    assert rows[0][0] == "Jurisdiction"
    assert rows[1][2] == "Diesel"
    assert rows[1][2] != "2. Diesel"

    total_row = rows[-1]
    assert total_row[0] == "TOTAL"
    assert total_row[6] == ""
    assert total_row[7] == ""
    assert total_row[8] == ""


def test_cdtfa_portal_csv_keeps_cdtfa_fuel_code(tmp_path) -> None:
    path = write_portal_csv(_sample_return(), tmp_path / "ifta_portal.csv", portal="cdtfa")
    rows = _rows(path)

    assert rows[1][2] == "2. Diesel"


def test_rate_fallback_writes_do_not_file_banner(tmp_path) -> None:
    path = write_portal_csv(_sample_return(fallback=True), tmp_path / "ifta_portal.csv")
    rows = _rows(path)

    assert rows[0][0] == "WARNING"
    assert "1Q2026" in rows[0][1]
    assert rows[1][0] == "DO_NOT_FILE"
    assert rows[3][0] == "Jurisdiction"
