"""tests/test_artwork_index.py — indeks masterów artworków (sync, najnowsza rewizja)."""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import artwork_index as ai


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def _entries():
    return [
        {"filename": "RNBM10001_pouch Rev.01_5900010800810.pdf",
         "rel_path": "Z/etyk/RNBM10001_pouch Rev.01.pdf", "source_mtime": "2026-01-10"},
        {"filename": "RNBM10001_pouch Rev.03_5900010800810.pdf",
         "rel_path": "Z/etyk/RNBM10001_pouch Rev.03.pdf", "source_mtime": "2026-05-10"},
        {"filename": "RNBM10001_pouch Rev.02_5900010800810.pdf",
         "rel_path": "Z/etyk/RNBM10001_pouch Rev.02.pdf", "source_mtime": "2026-03-10"},
    ]


def test_sync_and_lookup_newest_revision():
    db = _db()
    res = ai.sync_entries(db, _entries())
    assert res["indexed"] == 3
    best = ai.lookup_best(db, "RNBM 10001")     # dopasowanie po znormalizowanym REF
    assert best is not None
    assert best["revision_rank"] == 3
    assert "Rev.03" in best["revision"]


def test_all_revisions_sorted():
    db = _db()
    ai.sync_entries(db, _entries())
    revs = [r["revision"] for r in ai.all_revisions(db, "RNBM10001")]
    assert revs[0].endswith("03") and revs[-1].endswith("01")


def test_resync_updates_not_duplicates():
    db = _db()
    ai.sync_entries(db, _entries())
    ai.sync_entries(db, _entries()[:1])
    assert db.execute("SELECT COUNT(*) FROM artwork_index").fetchone()[0] == 3


def test_suggest_for_refs():
    db = _db()
    ai.sync_entries(db, _entries())
    s = ai.suggest_for_refs(db, ["RNBM10001", "NOPE"])
    assert s["RNBM10001"]["found"] is True
    assert s["NOPE"]["found"] is False


def test_suggest_by_text_matches_separator_filename_without_ref():
    # Regresja: nazwa pliku bez REF, z separatorami (easycare_nitrile_carton_S_a100)
    # ma być proponowana po OPISIE materiału. Prefiltr LIKE musi używać realnych
    # słów (easycare/nitrile), nie sztucznych bigramów/kodów.
    db = _db()
    ai.ensure_table(db)
    db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,revision,revision_rank) "
               "VALUES('','','easycare_nitrile_carton_S_a100.pdf','Z/easycare_nitrile_carton_S_a100.pdf','',0)")
    db.commit()
    opis = "RNBS10001 easyCARE nitr. gloves, PF, n/s, S_A100 easyCARE nitrile niebies S A100"
    sug = ai.suggest_by_text(db, opis, limit=1)
    assert sug, "powinno zaproponować plik pasujący do opisu"
    assert sug[0]["filename"] == "easycare_nitrile_carton_S_a100.pdf"
    # nie proponuj dla kompletnie innego materiału
    assert ai.suggest_by_text(db, "surgical face mask blue large box", limit=1) == []


def test_pick_then_confirm_then_revision_drift():
    # Pełny łańcuch: wybór mastera (alias+confirm) → brak dryfu; nowa rewizja → dryf.
    db = _db()
    ai.ensure_table(db)
    db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,revision,revision_rank) "
               "VALUES('','','easycare_carton_a100_rev01.pdf','Z/v1.pdf','01',1)")
    db.commit()
    R = "RNBS10001"
    ai.add_alias(db, R, "easycare_carton_a100", replace=True)
    ai.confirm_artwork(db, R, "Z/v1.pdf", user="tester")
    st = ai.lookup_for_comparison(db, R)
    assert st["newest"] and st["confirmed"] and st["revision_changed"] is False
    # nowsza rewizja przychodzi ze skanu (nazwa pasuje do aliasu → sync ustawia ref_norm)
    ai.sync_entries(db, [{"filename": "easycare_carton_a100_rev02.pdf",
                          "rel_path": "Z/v2.pdf", "source_mtime": "2026-06-09"}])
    db.execute("UPDATE artwork_index SET revision_rank=2, revision='02' WHERE rel_path='Z/v2.pdf'")
    db.commit()
    st = ai.lookup_for_comparison(db, R)
    assert st["newest"]["rel_path"] == "Z/v2.pdf"
    assert st["confirmed"]["rel_path"] == "Z/v1.pdf"
    assert st["revision_changed"] is True


def test_import_paths_and_stats():
    db = _db()
    ai.ensure_table(db)
    paths = [
        r'Z:\ISO\WZORY\RNBM10001_pouch Rev.01_5900010800810.pdf',
        'Z:/ISO/WZORY/easycare_nitrile_carton_S_a100.pdf',
        '  "Z:\\ISO\\notatka.txt"  ',   # nie-PDF → pominięte
        '',                              # puste → pominięte
    ]
    res = ai.import_paths(db, paths)
    assert res["indexed"] == 2 and res["total_lines"] == 4
    st = ai.index_stats(db)
    assert st["total"] == 2
    assert st["with_ref"] >= 1            # RNBM10001 ma REF w nazwie
    assert st["last_scan"]                # ustawiony scanned_at
    # idempotentny re-import (po rel_path) — bez duplikatów
    ai.import_paths(db, paths)
    assert ai.index_stats(db)["total"] == 2


def test_import_listing_columnar_csv_with_header():
    # Format jak eksport Windows: FullName;SizeBytes;IsFolder;LastWriteTime
    db = _db(); ai.ensure_table(db)
    rows = [
        ["FullName", "SizeBytes", "IsFolder", "LastWriteTime"],
        ["Z:\\ISO\\WZORY\\ABSOFORM", "0", "True", "26.02.2026 14:40"],            # folder → pomiń
        ["Z:\\ISO\\WZORY\\RNBM10001_pouch Rev.01.pdf", "12345", "False", "18.05.2026 07:58"],
        ["Z:\\ISO\\easycare_nitrile_carton_S_a100.pdf", "999", "False", "01.03.2026 09:00"],
    ]
    res = ai.import_listing(db, rows)
    assert res["candidates"] == 2 and res["indexed"] == 2
    # data modyfikacji znormalizowana do ISO (sortowalna)
    r = db.execute("SELECT source_mtime FROM artwork_index WHERE filename LIKE 'RNBM%'").fetchone()
    assert r["source_mtime"] == "2026-05-18 07:58:00"
