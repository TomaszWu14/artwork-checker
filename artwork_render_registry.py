"""artwork_render_registry.py — rejestr renderów 3D (Faza 1: „co mamy").

Jeden wspólny helper zapisu (register_render) wołany z obu choke-pointów
(_render_library_file_to_cache — biblioteka; /api/artwork/3d/accept — manual/
photo/MARM, Plan 02) — REG-04. Dedup po source_hash / dopisuje historię
(append-only, D-02), audytuje każdy zapis (REG-05, core.audit).

Moduł jest „czysty" (kształt jak material_uom.py): funkcje db-first, żadnej
kontroli przepływu opartej o Flask poza audytem — core.audit.log_audit jest
już bezpieczny do wołania z daemon-threadu bez request contextu.
"""
from __future__ import annotations

import hashlib

from constants import RenderVariant
from core.audit import log_audit as _log_audit
from db import escape_like
from uom import normalize_ref

_VALID_VARIANTS = {v.value for v in RenderVariant}


def marm_source_hash(ref, unit, dims_mm) -> str:
    """source_hash dla wariantu 'marm' (D-01): REF znormalizowany + jednostka +
    W×H×D w mm, zaokrąglone. `dims_mm` MUSI już być po konwersji jednostek
    (np. przez material_uom._dims_mm) — ta funkcja tylko hashuje, nie konwertuje,
    żeby CM i MM tej samej fizycznej geometrii dały ten sam hash u wołającego,
    który je najpierw znormalizował. Deterministyczna, stabilna między wywołaniami."""
    w, h, d = (round(float(v), 2) for v in dims_mm)
    payload = f"{normalize_ref(ref)}|{(unit or '').upper()}|{w}|{h}|{d}"
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


def photo_source_hash(image_bytes_list) -> str:
    """source_hash dla wariantu 'photo' (D-01): md5 konkatenacji surowych bajtów
    wejściowych zdjęć (wzór library_sync._md5). Brak zdjęć → '' — pusty hash
    nigdy nie dedupuje (patrz register_render), więc plain-photo bez zdjęć
    zawsze dopisuje nowy wiersz historii, tak jak dla dieline/marm bez danych."""
    if not image_bytes_list:
        return ""
    h = hashlib.md5(usedforsecurity=False)
    for b in image_bytes_list:
        h.update(b)
    return h.hexdigest()


def register_render(db, *, fid=None, ref_norm="", variant, source_hash="",
                     glb_path, created_by=None) -> dict:
    """Zapisuje lub potwierdza render w rejestrze.

    Klucz tożsamości: `fid` gdy podany, inaczej `ref_norm` (D-04). Ten sam
    `source_hash` (niepusty) dla (klucz, variant) bumpuje `last_confirmed_at`
    najświeższego wiersza — bez nowego wiersza. Inny/brak dopasowania lub
    pusty `source_hash` dopisuje nowy wiersz (append-only, D-02): pusty hash
    nie dowodzi „bez zmian", więc nigdy nie dedupuje.

    Rzuca ValueError gdy `variant` nie należy do RenderVariant (V5 — walidacja
    przed INSERT; wołający w warstwie HTTP przekłada to na 400).

    Zwraca {'action': 'bumped'|'inserted', 'id': <id wiersza>}.
    """
    if variant not in _VALID_VARIANTS:
        raise ValueError(f"Nieznany wariant renderu: {variant!r}")

    latest = db.execute(
        "SELECT id, source_hash FROM artwork_render_registry "
        "WHERE variant=? AND ((? IS NOT NULL AND fid=?) OR (? IS NULL AND ref_norm=?)) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (variant, fid, fid, fid, ref_norm),
    ).fetchone()

    # ponytail: append-only toleruje wyścig (dwie zakładki / daemon-thread vs
    # manual accept dają co najwyżej redundantny wiersz historii — nie korupcja);
    # UNIQUE(klucz,variant,source_hash) blokowałoby legit re-render z
    # source_hash='' — dodać locking/UNIQUE tylko gdy realnie zaboli.
    if latest and source_hash and latest["source_hash"] == source_hash:
        db.execute(
            "UPDATE artwork_render_registry SET last_confirmed_at=datetime('now') WHERE id=?",
            (latest["id"],),
        )
        db.commit()
        action, row_id = "bumped", latest["id"]
    else:
        cur = db.execute(
            "INSERT INTO artwork_render_registry "
            "(fid, ref_norm, variant, source_hash, glb_path, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (fid, ref_norm, variant, source_hash, glb_path, created_by),
        )
        db.commit()
        action, row_id = "inserted", cur.lastrowid

    _log_audit(
        "render_registered", None,
        f"variant={variant} fid={fid} ref_norm={ref_norm!r} glb_path={glb_path}",
        extra={"variant": variant, "source_hash": source_hash, "glb_path": glb_path,
               "fid": fid, "ref_norm": ref_norm},
    )
    return {"action": action, "id": row_id}


def coverage(db, ref="", lvl1="", page=1, per_page=50) -> dict:
    """Widok „co mamy" (COV-01..03): jeden wiersz = jeden plik `library_files`
    (D-03), LEFT JOIN rejestru po `fid` LUB `ref_norm` (D-04) — union obu, nie
    priorytet wykluczający, bo accept-path (Plan 02) i library-path mogą oba
    wskazywać ten sam artwork. Filtruje po REF/nazwie pliku i `lvl1`, paginuje
    server-side (backstop DoS na 31k+ wierszy — T-01-05).

    Batched: dokładnie JEDEN SELECT do artwork_render_registry dla całej
    strony (IN po fids/ref_norms), nigdy per-wiersz (Pitfall 5 / T-01-05).

    Zwraca {'items':[{fid,ref,filename,lvl1,has_dieline,has_marm,has_photo}],
    'total','page','per_page','total_pages'}.
    """
    ref = (ref or "").strip()
    lvl1 = (lvl1 or "").strip()
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(int(per_page or 50), 50))
    except (TypeError, ValueError):
        per_page = 50

    where, params = [], []
    if ref:
        esc = escape_like(ref)
        where.append("(ref LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\')")
        params += [f"%{esc}%", f"%{esc}%"]
    if lvl1:
        where.append("lvl1=?")
        params.append(lvl1)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        f"SELECT COUNT(*) FROM library_files{where_sql}", params).fetchone()[0]  # nosec B608

    offset = (page - 1) * per_page
    rows = [dict(r) for r in db.execute(
        # Bandit B608: SQL złożony wyłącznie ze stałych fragmentów w kodzie; dane użytkownika idą jako parametry ?.
        f"SELECT id, ref, filename, lvl1, checksum FROM library_files{where_sql} "  # nosec B608
        "ORDER BY filename LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()]

    fids = [r["id"] for r in rows]
    refnorms = sorted({normalize_ref(r["ref"]) for r in rows if r.get("ref")})

    # Per (klucz, variant) trzymamy NAJNOWSZY wiersz: variant → {id, push_status,
    # created_at} — „aktualny" = MAX(created_at) (append-only, brak is_current).
    # Push (Faza 4) dotyczy najświeższego renderu danego wariantu.
    by_fid: dict = {}
    by_refnorm: dict = {}

    def _keep_latest(bucket, key, rr):
        vslot = bucket.setdefault(key, {})
        prev = vslot.get(rr["variant"])
        if prev is None or (rr["created_at"] or "") >= (prev["created_at"] or ""):
            vslot[rr["variant"]] = {"id": rr["id"], "created_at": rr["created_at"],
                                    "push_status": (rr["push_status"] or "pending"),
                                    "source_hash": (rr["source_hash"] or "")}

    if fids or refnorms:
        clauses, reg_params = [], []
        if fids:
            clauses.append(f"fid IN ({','.join('?' * len(fids))})")
            reg_params += fids
        if refnorms:
            clauses.append(f"ref_norm IN ({','.join('?' * len(refnorms))})")
            reg_params += refnorms
        reg_rows = db.execute(
            # Bandit B608: interpolowane są tylko placeholdery ? (liczba = długość listy); wartości jako parametry.
            "SELECT id, fid, ref_norm, variant, push_status, source_hash, created_at "  # nosec B608
            "FROM artwork_render_registry "
            f"WHERE {' OR '.join(clauses)}", reg_params).fetchall()
        for rr in reg_rows:
            if rr["fid"] is not None:
                _keep_latest(by_fid, rr["fid"], rr)
            if rr["ref_norm"]:
                _keep_latest(by_refnorm, rr["ref_norm"], rr)

    # ponytail: jeden wiersz = jeden library_files.id, bez kolapsowania rewizji
    # (pick_current_revisions działa tylko na już-pobranej liście Pythona, brak
    # SQL-kolapsu na 31k) — dołożyć w Fazie 2 jeśli lista duplikatów przeszkadza.
    items = []
    for r in rows:
        slot_fid = by_fid.get(r["id"], {})
        slot_ref = by_refnorm.get(normalize_ref(r.get("ref") or ""), {})
        # union po wariantach; przy kolizji wariantu bierz nowszy (fid vs ref_norm)
        merged: dict = {}
        for slot in (slot_fid, slot_ref):
            for v, info in slot.items():
                prev = merged.get(v)
                if prev is None or (info["created_at"] or "") >= (prev["created_at"] or ""):
                    merged[v] = info
        # PUSH-02: status pusha najświeższego renderu per wariant (+ id do retry)
        push = {v: {"registry_id": info["id"], "status": info["push_status"]}
                for v, info in merged.items()}
        # STALE-01: render dieline jest nieaktualny, gdy zapisany source_hash (md5
        # PDF przy renderze) ≠ bieżący library_files.checksum (md5 pliku po sync).
        # Oba już w pamięci — zero I/O. Tylko dieline (fid-owy, source_hash=checksum);
        # marm/photo (accept-path po ref_norm) nie mają tu taniego bieżącego źródła.
        dieline = slot_fid.get(RenderVariant.DIELINE.value)
        cur_checksum = (r.get("checksum") or "")
        dieline_stale = bool(
            dieline and dieline["source_hash"] and cur_checksum
            and dieline["source_hash"] != cur_checksum)
        items.append({
            "fid": r["id"],
            "ref": r.get("ref") or "",
            "filename": r["filename"],
            "lvl1": r.get("lvl1") or "",
            "has_dieline": RenderVariant.DIELINE.value in merged,
            "has_marm": RenderVariant.MARM.value in merged,
            "has_photo": RenderVariant.PHOTO.value in merged,
            "push": push,
            "dieline_stale": dieline_stale,
        })

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {"items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": total_pages}
