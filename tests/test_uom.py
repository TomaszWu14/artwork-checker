"""tests/test_uom.py — jednostki miary i auto-przeliczanie ilości."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import uom


def test_canonical_unit_aliases_and_plural():
    assert uom.canonical_unit("szt.") == "PCS"
    assert uom.canonical_unit("PCS") == "PCS"
    assert uom.canonical_unit("Karton") == "CTN"
    assert uom.canonical_unit("Kartony") == "CTN"     # liczba mnoga
    assert uom.canonical_unit("PIECES") == "PCS"
    assert uom.canonical_unit("") == ""


def test_canonical_unit_polish_plurals():
    # Liczba mnoga PL (-I/-A/-Y) — generyczny strip ich nie zdejmuje, więc
    # muszą być rozpoznane jako jawne aliasy (inaczej konwersja milcząco nie działa).
    assert uom.canonical_unit("OPAKOWANIA") == "OP"
    assert uom.canonical_unit("Opakowania") == "OP"
    assert uom.canonical_unit("SZTUKI") == "PCS"
    assert uom.canonical_unit("sztuk") == "PCS"
    assert uom.canonical_unit("PALETY") == "PAL"
    assert uom.canonical_unit("ZESTAWY") == "SET"
    assert uom.canonical_unit("BOXES") == "CTN"
    assert uom.canonical_unit("UNITS") == "PCS"


def test_get_factor_global_and_inverse():
    conv = [{"ref_norm": "*", "unit_from": "CTN", "unit_to": "PCS", "factor": 1000.0}]
    assert uom.get_factor("karton", "szt", "ANY", conv) == 1000.0
    assert uom.get_factor("szt", "karton", "ANY", conv) == 0.001   # odwrotny kierunek
    assert uom.get_factor("CTN", "PAL", "ANY", conv) is None
    assert uom.get_factor("PCS", "PCS", "ANY", conv) == 1.0          # ta sama jednostka


def test_get_factor_per_ref_overrides_global():
    conv = [
        {"ref_norm": "*",    "unit_from": "CTN", "unit_to": "PCS", "factor": 1000.0},
        {"ref_norm": "AB12", "unit_from": "CTN", "unit_to": "PCS", "factor": 500.0},
    ]
    assert uom.get_factor("CTN", "PCS", "AB-12", conv) == 500.0
    assert uom.get_factor("CTN", "PCS", "OTHER", conv) == 1000.0


def test_convert_qty():
    conv = [{"ref_norm": "*", "unit_from": "CTN", "unit_to": "PCS", "factor": 1000.0}]
    val, f = uom.convert_qty(2, "karton", "szt", "X", conv)
    assert val == 2000.0 and f == 1000.0
    assert uom.convert_qty(2, "CTN", "PAL", "X", conv) == (None, None)


def test_each_canonicalizes_to_pcs():
    # BUGFIX: 'EACH' (częsta jednostka EN) gubił się — 'EA' dawało PCS, 'EACH' nie,
    # więc dokument z 'EACH' nie zgadzał się z 'EA'/'PCS' w porównaniu.
    assert uom.canonical_unit("EACH") == "PCS"
    assert uom.canonical_unit("each") == "PCS"
    assert uom.canonical_unit("EACHES") == "PCS"
    # ten sam kanon → przelicznik 1:1 bez tabeli
    assert uom.get_factor("EACH", "EA", "X", []) == 1.0
    assert uom.get_factor("EACH", "PCS", "X", []) == 1.0
