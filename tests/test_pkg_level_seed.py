"""Seed jednostek opakowaniowych flow (SZT<JU<OP<OPZ<KAR) — idempotentny upsert + migracja
starego seeda (JED→SZT, dodanie JU) bez klobrowania edycji admina."""
import app as _app_mod


def _old_seed(db):
    db.execute("DELETE FROM artwork_pkg_level_type")
    for code, label, so, color in [
        ("sztuka", "Sztuka (JED)", 1, "#10b981"),
        ("op",     "Opakowanie (OP)", 2, "#3b82f6"),
        ("opz",    "OPZ",         3, "#8b5cf6"),
        ("karton", "Karton (KAR)", 4, "#f59e0b"),
    ]:
        db.execute("INSERT INTO artwork_pkg_level_type (code,label,sort_order,color) "
                   "VALUES (?,?,?,?)", (code, label, so, color))
    db.commit()


def _rows(db):
    return {r["code"]: dict(r) for r in db.execute(
        "SELECT code,label,sort_order FROM artwork_pkg_level_type").fetchall()}


def _reseed(db):
    # Seed/migracja odpala się raz na proces (guard _artwork_tables_initialized) —
    # w teście resetujemy guard, by wymusić ponowny przebieg.
    _app_mod._artwork_tables_initialized = False
    _app_mod._ensure_artwork_profile_tables(db)


def test_migrates_old_seed_to_flow_units():
    db = _app_mod.get_db()
    try:
        _reseed(db)                                   # tabela istnieje
        _old_seed(db)
        _reseed(db)                                   # migracja
        r = _rows(db)
        assert set(r) == {"sztuka", "ju", "op", "opz", "karton"}
        assert r["sztuka"]["label"] == "Sztuka (SZT)"
        assert r["opz"]["label"] == "Opakowanie zbiorcze (OPZ)"
        # hierarchia SZT<JU<OP<OPZ<KAR
        order = [c for c, _ in sorted(r.items(), key=lambda kv: kv[1]["sort_order"])]
        assert order == ["sztuka", "ju", "op", "opz", "karton"]
    finally:
        db.close()


def test_admin_deletion_is_durable():
    """Usunięty przez admina poziom NIE wraca przy kolejnym boot (re-seed tylko
    dla pustej tabeli lub starego seeda z 'Sztuka (JED)')."""
    db = _app_mod.get_db()
    try:
        _reseed(db)
        db.execute("DELETE FROM artwork_pkg_level_type WHERE code='ju'")
        db.commit()
        _reseed(db)
        assert "ju" not in _rows(db)
    finally:
        db.close()


def test_preserves_admin_edits():
    """Etykieta zmieniona przez admina nie jest nadpisywana przy kolejnym boot."""
    db = _app_mod.get_db()
    try:
        _reseed(db)
        db.execute("UPDATE artwork_pkg_level_type SET label=? WHERE code=?",
                   ("Moja etykieta", "op"))
        db.commit()
        _reseed(db)
        r = _rows(db)
        assert r["op"]["label"] == "Moja etykieta"
    finally:
        db.close()
