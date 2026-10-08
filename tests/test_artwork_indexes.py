"""#5 Sprint 1: indeksy DB pod wyszukiwanie artworków.

Sprawdza, że po utworzeniu tabel istnieją indeksy na kolumnach używanych do
wyszukiwania (ean/ref_code/profile_id/filename) i że planer zapytań realnie
z nich korzysta dla równości/JOIN (LIKE '%..%' celowo NIE korzysta — to zadanie
FTS w #7).
"""
import migrate_db as _migrate_db
import app as _app_mod
import artwork_index as _ai

# artwork_render_registry only lives in migrate_db.py (not app.py's own bootstrap) —
# run once so test_badge_join_refnorm_uses_index has a table to EXPLAIN against.
# Mirrors tests/test_coverage_view.py / tests/test_render_registry.py.
_migrate_db.run()


def _index_names(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r["name"] if not isinstance(r, tuple) else r[0] for r in rows}


def test_expected_indexes_exist():
    db = _app_mod.get_db()
    try:
        _ai.ensure_table(db)
        _app_mod._ensure_artwork_profile_tables(db)
        idx = _index_names(db)
    finally:
        db.close()
    for name in (
        "idx_artwork_index_filename",
        "idx_artwork_profiles_ean",
        "idx_artwork_profiles_ref",
        "idx_artwork_profile_fields_profile",
    ):
        assert name in idx, f"brak indeksu {name} (mam: {sorted(idx)})"


def test_ean_lookup_uses_index():
    db = _app_mod.get_db()
    try:
        _app_mod._ensure_artwork_profile_tables(db)
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM artwork_profiles WHERE ean=?", ("123",)
        ).fetchall()
    finally:
        db.close()
    text = " ".join(str(tuple(r)) for r in plan).upper()
    assert "IDX_ARTWORK_PROFILES_EAN" in text or "USING INDEX" in text, \
        f"zapytanie po EAN nie użyło indeksu: {text}"


def test_badge_join_refnorm_uses_index():
    """SRCH-03/D-04: 02-02 Task 2 — badge join's `ref_norm IN (...)` lookup on
    artwork_render_registry (artwork_index.py:_attach_has_3d) must be index-backed,
    not a full table scan. Phase 1's idx_arr_refnorm_variant(ref_norm, variant)
    (migrate_db.py:442) has ref_norm as the LEADING column, so its leftmost prefix
    should already cover this exact query — verify before adding a new index
    (RESEARCH Open Question 1 / Pitfall 3, ponytail: don't add a redundant index)."""
    db = _app_mod.get_db()
    try:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT ref_norm, variant FROM artwork_render_registry "
            "WHERE ref_norm IN (?)", ("BT100",)
        ).fetchall()
    finally:
        db.close()
    text = " ".join(str(tuple(r)) for r in plan).upper()
    assert "IDX_ARR_REFNORM_VARIANT" in text or "USING INDEX" in text or "USING COVERING INDEX" in text, \
        f"badge join ref_norm IN (...) nie użył indeksu: {text}"
