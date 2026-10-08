"""Portal zarządzania artworkami — KPI, pliki bez REF + podpowiedź, luki, duplikaty, status."""
import app as _app_mod
import artwork_index as _ai
import material_master as _mm
import artwork_manage as _am


def _seed(db):
    _ai.ensure_table(db)
    _mm.ensure_table(db)
    _am.ensure_status_table(db)
    for t in ("artwork_index", "artwork_alias", "material_master", "artwork_file_status"):
        try:
            # Bandit B608: nazwa tabeli ze stałej krotki w teście, bez danych z zewnątrz.
            db.execute(f"DELETE FROM {t}")  # nosec B608
        except Exception:
            pass
    idx = [
        # ref_norm, ref_code, filename, rel_path, packaging, revision, rank, ean
        ("BT100", "BT-100", "bt100_carton_rev_a.pdf", "z/bt100_a.pdf", "karton", "A", 1, "5900000000017"),
        ("BT100", "BT-100", "bt100_carton_rev_b.pdf", "z/bt100_b.pdf", "karton", "B", 2, "5900000000017"),
        # dwa pliki tej samej grupy (REF+opak+rank) → duplikat
        ("XY777", "XY-777", "xy777_single_v1.pdf", "z/xy777_1.pdf", "sztuka", "", 0, ""),
        ("XY777", "XY-777", "xy777_single_v1_copy.pdf", "z/xy777_2.pdf", "sztuka", "", 0, ""),
        # plik bez REF, z EAN i opisowa nazwa
        ("", "", "opatrunek jalowy sterylny 10x10 5901234123457.pdf", "z/unb1.pdf", "", "", 0, "5901234123457"),
    ]
    for r in idx:
        db.execute("INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,"
                   "packaging_type,revision,revision_rank,ean) VALUES(?,?,?,?,?,?,?,?)", r)
    mats = [
        # ref_code, ref_norm, opis_pl, ean, rodzina, active
        ("BT-100", "BT100", "Bandaż elastyczny", "5900000000017", "Opatrunki", 1),
        ("EAN-P", "EANP", "Produkt po EAN", "5901234123457", "Testy", 1),
        ("OPT-1", "OPT1", "Opatrunek jałowy sterylny 10x10", "", "Opatrunki", 1),
        ("GAP-1", "GAP1", "Produkt bez artworku", "", "Luka", 1),
        ("INA-1", "INA1", "Nieaktywny produkt", "", "Luka", 0),
    ]
    for m in mats:
        db.execute("INSERT INTO material_master(ref_code,ref_norm,opis_pl,ean,rodzina,active) "
                   "VALUES(?,?,?,?,?,?)", m)
    db.commit()


def test_compute_kpis():
    db = _app_mod.get_db()
    try:
        _seed(db)
        k = _am.compute_kpis(db)
        assert k["total"] == 5
        assert k["with_ref"] == 4
        assert k["unbound"] == 1
        assert k["distinct_refs"] == 2          # BT100, XY777
        assert k["obsolete"] == 1               # bt100 rev A przykryty przez B
        assert k["duplicates"] == 1             # grupa XY777/sztuka/rank0
        # EAN-P, GAP-1, OPT-1 nie mają powiązanego pliku (plik bez REF ich nie pokrywa);
        # BT-100 pokryty, INA-1 nieaktywny → 3 luki.
        assert k["gaps"] == 3
        assert k["ignored"] == 0
    finally:
        db.close()


def test_list_unbound():
    db = _app_mod.get_db()
    try:
        _seed(db)
        res = _am.list_unbound(db)
        assert res["total"] == 1
        assert res["rows"][0]["rel_path"] == "z/unb1.pdf"
        assert _am.list_unbound(db, q="opatrunek")["total"] == 1
        assert _am.list_unbound(db, q="brakslowa")["total"] == 0
    finally:
        db.close()


def test_suggest_ref_ean_hit_ranks_first():
    db = _app_mod.get_db()
    try:
        _seed(db)
        sugg = _am.suggest_ref(db, "z/unb1.pdf")
        assert sugg, "spodziewano się propozycji"
        assert sugg[0]["source"] == "ean"
        assert sugg[0]["ref_code"] == "EAN-P"
        assert sugg[0]["score"] == 1.0
    finally:
        db.close()


def test_suggest_ref_text_match():
    db = _app_mod.get_db()
    try:
        _seed(db)
        # usuń EAN pliku, by wymusić dopasowanie po tekście nazwy → opis produktu
        db.execute("UPDATE artwork_index SET ean='' WHERE rel_path='z/unb1.pdf'")
        db.commit()
        sugg = _am.suggest_ref(db, "z/unb1.pdf")
        refs = [s["ref_code"] for s in sugg]
        assert "OPT-1" in refs                  # opis "Opatrunek jałowy sterylny 10x10"
        assert all(s["source"] == "text" for s in sugg)
    finally:
        db.close()


def test_bind_ref_creates_alias_and_binds():
    db = _app_mod.get_db()
    try:
        _seed(db)
        res = _am.bind_ref(db, "z/unb1.pdf", "EAN-P", user="tester")
        assert res.get("ok") and res.get("matched") >= 1
        # plik nie jest już „bez REF"
        assert _am.list_unbound(db)["total"] == 0
        row = db.execute("SELECT ref_norm FROM artwork_index WHERE rel_path='z/unb1.pdf'").fetchone()
        assert row["ref_norm"] == "EANP"
    finally:
        db.close()


def test_find_gaps_excludes_inactive_and_covered():
    db = _app_mod.get_db()
    try:
        _seed(db)
        gaps = _am.find_gaps(db)
        refs = [g["ref_code"] for g in gaps]
        # BT-100 pokryty, INA-1 nieaktywny; reszta bez powiązanego pliku (sort po ref_code)
        assert refs == ["EAN-P", "GAP-1", "OPT-1"]
        assert _am.find_gaps(db, q="GAP")[0]["ref_code"] == "GAP-1"
        assert _am.find_gaps(db, q="nieistnieje") == []
    finally:
        db.close()


def test_find_duplicates_groups_files():
    db = _app_mod.get_db()
    try:
        _seed(db)
        dups = _am.find_duplicates(db)
        assert len(dups) == 1
        grp = dups[0]
        assert grp["ref_code"] == "XY-777"
        assert grp["count"] == 2
        assert {f["rel_path"] for f in grp["files"]} == {"z/xy777_1.pdf", "z/xy777_2.pdf"}
        assert all(f["status"] == "active" for f in grp["files"])
    finally:
        db.close()


def test_set_file_status_persists_and_counts():
    db = _app_mod.get_db()
    try:
        _seed(db)
        assert "error" in _am.set_file_status(db, "z/xy777_1.pdf", "bogus")
        res = _am.set_file_status(db, "z/xy777_1.pdf", "ignored", user="t")
        assert res["ok"] and res["status"] == "ignored"
        assert _am.count_ignored(db) == 1
        # widoczne w grupie duplikatów
        grp = _am.find_duplicates(db)[0]
        statuses = {f["rel_path"]: f["status"] for f in grp["files"]}
        assert statuses["z/xy777_1.pdf"] == "ignored"
        # zmiana z powrotem na active (upsert)
        _am.set_file_status(db, "z/xy777_1.pdf", "active")
        assert _am.count_ignored(db) == 0
    finally:
        db.close()