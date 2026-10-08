"""artwork_marm_link.py — trwałe powiązanie artwork (REF) → wiersz MARM (ref, unit).

MARM-01/02 (Faza 3). Gdy user generuje model 3D z MARM dla danego artworku,
zapamiętujemy jego wybór (REF materiału + jednostka). Przy ponownym otwarciu
generatora dla tego artworku podpowiadamy zapamiętane REF+jednostkę — bez
ręcznego wybierania od nowa.

Model zaufania propose/correct/remember (ten sam co uczenie orientacji 3D,
tabela artwork_3d_orientation): ustawiane ręcznie, zawsze nadpisywalne, upsert
przez DELETE+INSERT (portable SQLite/PG przez db.py — bez dialektowego ON CONFLICT).

Moduł „czysty": operuje na połączeniu DB przekazanym z app.py.
"""
from __future__ import annotations

from artwork_index import normalize_ref


def get_link(db, art_ref) -> dict | None:
    """Zapamiętane powiązanie dla artworku (po znormalizowanym REF) albo None."""
    rn = normalize_ref(art_ref)
    if not rn:
        return None
    row = db.execute(
        "SELECT art_ref_norm, marm_ref, marm_unit, updated_by, updated_at "
        "FROM artwork_marm_link WHERE art_ref_norm = ?", (rn,)
    ).fetchone()
    return dict(row) if row else None


def set_link(db, art_ref, marm_ref, marm_unit, user) -> bool:
    """Ustawia/nadpisuje powiązanie. Zwraca False (nic nie zapisano), gdy REF pusty.
    Upsert: DELETE istniejący + INSERT — jeden wiersz per artwork, korekta nadpisuje."""
    rn = normalize_ref(art_ref)
    if not rn:
        return False
    db.execute("DELETE FROM artwork_marm_link WHERE art_ref_norm = ?", (rn,))
    db.execute(
        "INSERT INTO artwork_marm_link (art_ref_norm, marm_ref, marm_unit, updated_by) "
        "VALUES (?, ?, ?, ?)",
        (rn, (marm_ref or "").strip(), (marm_unit or "").strip(), user)
    )
    db.commit()
    return True
