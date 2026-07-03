"""Ingestion of real-world fuel-card export shapes.

These cover the formats that silently produced an incomplete return before:

* A "Love's"-style transaction export whose jurisdiction column is just "St"
  and which interleaves taxable diesel with non-taxable DEF/reefer lines.
* Files that yield no usable rows must warn loudly, never be dropped silently.
"""

from __future__ import annotations

from pathlib import Path

from ifta.ingest import _classify_column, ingest_file, ingest_folder

# A minimal Love's-style export: state under "St", a "Truck Stop" merchant
# column that must NOT be mistaken for the truck id, and DEF lines mixed in.
LOVES_CSV = (
    "Orig. Date,Trans. #,Truck #,Trailer #,Driver,Driver ID,City,St,Truck Stop,Product,Qty,Retl. Price\n"
    "2026/06/03,000020,55,55,Dmytro,Y1,El Paso,TX,Loves 214,DEF Diesel Exhaust Fluid,13.63,4.99\n"
    "2026/06/03,000020,55,55,Dmytro,Y1,El Paso,TX,Loves 214,Premium Diesel 2,206.22,5.14\n"
    "2026/06/05,000025,55,55,Dmytro,Y1,Laramie,WY,Loves 723,Premium Diesel 2,230.04,5.24\n"
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- header classification -------------------------------------------------


def test_bare_st_header_is_state():
    assert _classify_column("St") == "state"
    assert _classify_column("st") == "state"


def test_st_is_only_matched_exactly_not_as_substring():
    # These contain the letters "st" but are not a state column.
    for header in ("Cost", "Last Name", "First", "Truck Stop", "Customer"):
        assert _classify_column(header) != "state", header


def test_product_column_is_classified():
    assert _classify_column("Product") == "product"


# --- Love's export end to end ---------------------------------------------


def test_loves_export_extracts_diesel_by_state(tmp_path: Path):
    data = ingest_file(_write(tmp_path, "loves.csv", LOVES_CSV))
    by_state = {f.state: f.gallons for f in data.fuel}
    # DEF (13.63 gal) is dropped; only the two diesel lines remain.
    assert by_state == {"TX": 206.22, "WY": 230.04}
    assert sum(f.gallons for f in data.fuel) == 436.26


def test_def_line_is_not_counted(tmp_path: Path):
    data = ingest_file(_write(tmp_path, "loves.csv", LOVES_CSV))
    # 13.63 gal of DEF must appear nowhere in the extracted fuel.
    assert all(abs(f.gallons - 13.63) > 0.001 for f in data.fuel)


def test_truck_stop_column_does_not_clobber_truck_id(tmp_path: Path):
    data = ingest_file(_write(tmp_path, "loves.csv", LOVES_CSV))
    # Truck id comes from "Truck #" (55), not the "Truck Stop" merchant column.
    assert {f.truck_id for f in data.fuel} == {"55"}


def test_reefer_and_dyed_are_also_excluded(tmp_path: Path):
    csv = (
        "Truck #,St,Product,Qty\n"
        "9,TX,Premium Diesel,100\n"
        "9,TX,Reefer Diesel,40\n"
        "9,OK,Dyed Diesel,25\n"
    )
    data = ingest_file(_write(tmp_path, "f.csv", csv))
    assert {f.state: f.gallons for f in data.fuel} == {"TX": 100.0}


# --- backward compatibility ------------------------------------------------


def test_file_without_product_column_counts_all_fuel(tmp_path: Path):
    csv = "State,Gallons\nTX,100\nOK,50\n"
    data = ingest_file(_write(tmp_path, "simple.csv", csv))
    assert {f.state: f.gallons for f in data.fuel} == {"TX": 100.0, "OK": 50.0}


# --- silent-drop protection ------------------------------------------------


def test_state_less_file_warns_and_contributes_nothing(tmp_path: Path, capsys):
    # A driver-total export with no jurisdiction column — unusable for IFTA.
    _write(tmp_path, "no_state.csv", "Name,Quantity\nDavyd,2356.74\nMark,1846.55\n")
    merged = ingest_folder(tmp_path)
    assert merged.fuel == [] and merged.miles == []
    warning = capsys.readouterr().out
    assert "no_state.csv" in warning
    assert "contributed NOTHING" in warning


def test_usable_file_does_not_warn(tmp_path: Path, capsys):
    _write(tmp_path, "ok.csv", "Truck #,St,Product,Qty\n7,TX,Diesel,120\n")
    ingest_folder(tmp_path)
    assert "contributed NOTHING" not in capsys.readouterr().out
