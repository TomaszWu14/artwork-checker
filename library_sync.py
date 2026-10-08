"""
DocCompare Library Sync Agent
==============================
Uruchom na komputerze z dostępem do dysku sieciowego Z:.
Synchronizuje metadane + miniatury plików z biblioteki artworków do serwera DocCompare.
Pełne pliki PDF NIE są przesyłane — tylko miniatura (~30 KB) i ścieżka sieciowa.

Użycie:
  python library_sync.py                 # synchronizacja inkrementalna (metadane + miniatury)
  python library_sync.py --full          # prześlij wszystkie metadane od nowa
  python library_sync.py --dry-run       # tylko sprawdź co wymaga sync
  python library_sync.py --search NL753  # wyszukaj plik w zsynchronizowanej bibliotece
  python library_sync.py --upload-masters          # wyślij PEŁNE PDF tylko potwierdzonych masterów
  python library_sync.py --upload-masters --dry-run # pokaż, które mastery zostałyby wysłane

Wymagania: pip install requests pymupdf pillow
"""

import logging
import os
import sys
import hashlib
import argparse
import json
import time
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    # Bez twardego sys.exit() na poziomie modułu — to zabijało interpreter każdemu,
    # kto tylko zaimportował ten plik (np. testy). Brak `requests` blokuje wyłącznie
    # realne wysyłanie; sprawdzamy go w main(), gdzie faktycznie jest potrzebny.
    requests = None

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False
    print("[warn] PyMuPDF nie jest zainstalowany — miniatury PDF wyłączone")
    print("       Zainstaluj: pip install pymupdf")

# ─── KONFIGURACJA ────────────────────────────────────────────────────────────
# Nadpisywalne przez zmienne środowiskowe (bez edycji/commitowania kodu):
#   LIBRARY_SYNC_ROOT   — ścieżka do biblioteki (domyślnie dysk Z:\)
#   LIBRARY_SYNC_URL    — adres serwera (np. http://127.0.0.1:5000 dla lokalnego)
#   LIBRARY_SYNC_TOKEN  — token ze strony /library
# Przykład (PowerShell, sync do lokalnego serwera):
#   $env:LIBRARY_SYNC_URL="http://127.0.0.1:5000"; $env:LIBRARY_SYNC_TOKEN="<token>"; python library_sync.py

LIBRARY_ROOT = os.environ.get("LIBRARY_SYNC_ROOT", r"Z:\ISO\WZORY ETYKIET I INSTRUKCJI")
SERVER_URL   = os.environ.get("LIBRARY_SYNC_URL", "https://compare.example.com")
SYNC_TOKEN   = os.environ.get("LIBRARY_SYNC_TOKEN", "WKLEJ_TOKEN_Z_STRONY_LIBRARY")

# Opcje zaawansowane:
EXTENSIONS   = {".pdf", ".PDF", ".jpg", ".JPG", ".jpeg", ".JPEG", ".png", ".PNG"}
TIMEOUT_S    = 60                   # timeout HTTP (sekundy)
RETRY_COUNT  = 3                    # ile razy ponawiać po błędzie sieci
STATE_FILE   = os.path.join(os.path.dirname(__file__), ".library_sync_state.json")
# ─────────────────────────────────────────────────────────────────────────────


def _md5(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _stat_stamp(st) -> str:
    """Odcisk metadanych pliku (mtime + rozmiar) — tani zamiennik MD5 przy sprawdzaniu
    „czy się zmieniło". Mtime zaokrąglony do sekundy, bo SMB i różne systemy plików
    nie zgadzają się co do rozdzielczości ułamkowej i pełna precyzja dawałaby
    fałszywe „zmienione" przy każdym przebiegu."""
    return f"{int(st.st_mtime)}:{st.st_size}"


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"  [warn] Nie można zapisać stanu: {e}")


def _make_thumb(abs_path: str) -> bytes | None:
    """Generate ~30KB JPEG thumbnail. Returns None if not possible."""
    ext = Path(abs_path).suffix.lower()
    try:
        if ext == '.pdf':
            if not HAS_FITZ:
                return None
            doc = fitz.open(abs_path)
            try:
                if not doc.page_count:
                    return None
                page = doc[0]
                mat = fitz.Matrix(0.3, 0.3)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                return pix.tobytes("jpeg", jpg_quality=70)
            finally:
                doc.close()
        elif ext in {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp'}:
            from PIL import Image
            import io
            with Image.open(abs_path) as _raw:   # BUGFIX: zamknij uchwyt pliku
                img = _raw.convert("RGB")
            img.thumbnail((200, 300))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            return buf.getvalue()
    except Exception:
        return None
    return None


def _sync_file(rel_path: str, abs_path: str, checksum: str, dry_run: bool,
               send_file: bool = False) -> str:
    """
    Wysyła miniaturę i metadane do serwera. Zwraca 'created' | 'updated' | 'skipped' | 'error'.
    Gdy send_file=True — przesyła też PEŁNY plik PDF (tryb --upload-masters); w zwykłej
    synchronizacji pełny plik NIE jest przesyłany.
    """
    if dry_run:
        return "would_sync"

    filename = os.path.basename(abs_path)
    modified = datetime.fromtimestamp(os.path.getmtime(abs_path)).isoformat()
    size_bytes = os.path.getsize(abs_path)

    meta = {
        "rel_path":    rel_path.replace("\\", "/"),
        "filename":    filename,
        "checksum":    checksum,
        "modified_at": modified,
        "size_bytes":  size_bytes,
        "z_path":      abs_path,
    }
    if send_file:
        # Znacznik dla serwera: to potwierdzony master → zarejestruj jako profil
        # mapowania (pojawi się w zakładce „Mapowanie" jako do zmapowania).
        meta["is_master"] = True

    # Generate thumbnail
    thumb_bytes = _make_thumb(abs_path)

    files_payload = {}
    if thumb_bytes:
        files_payload["thumb"] = ("thumb.jpg", thumb_bytes, "image/jpeg")
    if send_file:
        # Pełny plik PDF — serwer zapisze go i ustawi file_path (auto-podpinanie).
        try:
            with open(abs_path, "rb") as _pdf:
                files_payload["file"] = (filename, _pdf.read(), "application/octet-stream")
        except Exception as e:
            print(f"  [warn] nie można wczytać pliku do wysłania: {e}")
            return "error"

    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.post(
                f"{SERVER_URL}/api/library/sync",
                headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
                data={"meta": json.dumps(meta)},
                files=files_payload if files_payload else None,
                timeout=TIMEOUT_S,
            )
            if resp.status_code == 401:
                print("  [BŁĄD] Nieprawidłowy token synchronizacji!")
                sys.exit(1)
            if resp.ok:
                return resp.json().get("status", "ok")
            print(f"  [warn] HTTP {resp.status_code}: {resp.text[:100]}")
        except requests.exceptions.ConnectionError as e:
            logger.warning("Library sync connection error (attempt %d/%d): %s",
                           attempt + 1, RETRY_COUNT, e)
            if attempt < RETRY_COUNT - 1:
                time.sleep(2 ** attempt)
                continue
            return "error"
        except Exception as e:
            logger.warning("Library sync unexpected error: %s", e)
            print(f"  [warn] {e}")
            return "error"
    return "error"


def run_sync(full: bool = False, dry_run: bool = False):
    root = Path(LIBRARY_ROOT)
    if not root.exists():
        print(f"[BŁĄD] Ścieżka nie istnieje: {LIBRARY_ROOT}")
        print("       Sprawdź czy dysk Z: jest zamontowany.")
        sys.exit(1)

    print(f"DocCompare Library Sync (tryb: miniatury + metadane, bez przesyłania PDF)")
    print(f"Root  : {LIBRARY_ROOT}")
    print(f"Server: {SERVER_URL}")
    print(f"Tryb  : {'PEŁNA' if full else 'inkrementalna'}{' (dry-run)' if dry_run else ''}")
    print("-" * 60)

    state = {} if full else _load_state()
    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
    # Zacznij od poprzedniego stanu (incremental): katalogi pominięte przez
    # PermissionError nie zostaną przeskanowane, więc bez tego ich wpisy zniknęłyby
    # i następny przebieg wgrałby wszystko pod nimi od nowa.
    new_state = dict(state)

    all_files = []
    dirs_to_scan = [root]
    while dirs_to_scan:
        current = dirs_to_scan.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError) as e:
            print(f"  [POMIŃ] {current.name}: {e}")
            continue
        for p in entries:
            try:
                if p.is_dir():
                    dirs_to_scan.append(p)
                elif p.is_file() and p.suffix.lower() in EXTENSIONS:
                    all_files.append(p)
            except (PermissionError, OSError) as e:
                print(f"  [POMIŃ] {p.name}: {e}")
                continue
    total = len(all_files)
    print(f"Znaleziono {total} plików\n")

    for idx, abs_path in enumerate(sorted(all_files), 1):
        rel_path = str(abs_path.relative_to(root)).replace("\\", "/")
        prev = state.get(rel_path, {})
        try:
            st = abs_path.stat()
            stamp = _stat_stamp(st)
        except (OSError, FileNotFoundError) as e:
            print(f"  [POMIŃ] {abs_path.name}: {e}")
            stats["skipped"] += 1
            continue

        # Szybkie pominięcie po metadanych: mtime+rozmiar zgadzają się ze stanem
        # z poprzedniego przebiegu → plik na pewno nietknięty, MD5 pomijamy.
        # Wcześniej liczyliśmy MD5 ZAWSZE, czyli każdy przebieg czytał całą
        # zawartość 31k PDF-ów przez dysk sieciowy tylko po to, żeby stwierdzić
        # „bez zmian". _md5 zostaje jedynym źródłem prawdy przy realnym sync.
        if not full and prev.get("stamp") == stamp and prev.get("checksum"):
            new_state[rel_path] = dict(prev)
            stats["skipped"] += 1
            continue

        try:
            checksum = _md5(str(abs_path))
        except (OSError, FileNotFoundError) as e:
            print(f"  [POMIŃ] {abs_path.name}: {e}")
            stats["skipped"] += 1
            continue

        if not full and prev.get("checksum") == checksum:
            # Metadane się ruszyły (kopia, dotknięcie pliku), ale treść ta sama.
            new_state[rel_path] = {"checksum": checksum, "stamp": stamp}
            stats["skipped"] += 1
            continue

        bar = f"[{idx:4d}/{total}]"
        print(f"{bar} {rel_path[:70]}", end="", flush=True)

        status = _sync_file(rel_path, str(abs_path), checksum, dry_run)
        stats[status] = stats.get(status, 0) + 1
        print(f"  → {status}")
        # Zapisuj checksum TYLKO po udanym sync. Wcześniej zapisywano go przed
        # próbą — błąd sieci ("error") zostawał z nowym checksumem i przy kolejnym
        # przebiegu plik był pomijany (nigdy nie ponawiany). Zachowaj stary wpis.
        # Ten sam powód dotyczy 'stamp' — zapisany po błędzie wyciszyłby plik
        # jeszcze skuteczniej, bo pomijanie po metadanych jest przed MD5.
        if status in ("created", "updated", "ok"):
            new_state[rel_path] = {"checksum": checksum, "stamp": stamp}
        elif rel_path in state:
            new_state[rel_path] = state[rel_path]

    if not dry_run:
        _save_state(new_state)

    print("\n" + "=" * 60)
    if dry_run:
        would = stats.get('would_sync', 0)
        print(f"Wyniki (dry-run): do_wysłania={would}  "
              f"pominięte={stats.get('skipped',0)}")
        print("(dry-run — żadne pliki nie zostały przesłane)")
    else:
        print(f"Wyniki: nowe={stats.get('created',0)}  "
              f"zaktualizowane={stats.get('updated',0)}  "
              f"pominięte={stats.get('skipped',0)}  "
              f"błędy={stats.get('error',0)}")


def run_upload_masters(dry_run: bool = False):
    """Wysyła PEŁNE pliki PDF tylko dla POTWIERDZONYCH masterów, których serwer
    jeszcze nie ma. Serwer zwraca listę potrzebnych (potwierdzone + aktualne, bez
    pliku na dysku), agent dosyła tylko je — bez przesyłania całej biblioteki."""
    root = Path(LIBRARY_ROOT)
    print("DocCompare — wysyłka POTWIERDZONYCH masterów (pełne pliki PDF)")
    print(f"Root  : {LIBRARY_ROOT}")
    print(f"Server: {SERVER_URL}")
    print(f"Tryb  : {'dry-run' if dry_run else 'wysyłka'}")
    print("-" * 60)

    try:
        resp = requests.get(
            f"{SERVER_URL}/api/library/needed-masters",
            headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code == 401:
            print("[BŁĄD] Nieprawidłowy token synchronizacji!")
            sys.exit(1)
        data = resp.json()
    except Exception as e:
        print(f"[BŁĄD] Nie udało się pobrać listy potrzebnych masterów: {e}")
        return

    items = data.get("files", [])
    total = len(items)
    print(f"Potwierdzonych masterów bez pliku na serwerze: {total}\n")
    if not total:
        print("Nic do wysłania — wszystkie potwierdzone mastery są już na serwerze.")
        return

    stats = {"sent": 0, "missing": 0, "error": 0, "would": 0}
    for idx, it in enumerate(items, 1):
        rel = (it.get("rel_path") or "").strip()
        zpath = (it.get("z_path") or "").strip()
        # Źródło: preferuj ścieżkę sieciową z serwera; inaczej root/rel_path.
        abs_path = zpath if (zpath and os.path.exists(zpath)) else str(root / rel)
        bar = f"[{idx:4d}/{total}]"
        print(f"{bar} {rel[:70]}", end="", flush=True)
        if not os.path.exists(abs_path):
            print("  → BRAK PLIKU na dysku (sprawdź Z:)")
            stats["missing"] += 1
            continue
        if dry_run:
            print("  → do_wysłania")
            stats["would"] += 1
            continue
        try:
            checksum = _md5(abs_path)
        except Exception as e:
            print(f"  → błąd odczytu: {e}")
            stats["error"] += 1
            continue
        status = _sync_file(rel, abs_path, checksum, dry_run=False, send_file=True)
        if status in ("created", "updated", "ok"):
            print(f"  → wysłany ({status})")
            stats["sent"] += 1
        else:
            print(f"  → {status}")
            stats["error"] += 1

    print("\n" + "=" * 60)
    if dry_run:
        print(f"Wyniki (dry-run): do_wysłania={stats['would']}  brak_pliku={stats['missing']}")
    else:
        print(f"Wyniki: wysłane={stats['sent']}  brak_pliku={stats['missing']}  błędy={stats['error']}")
        print("Po wysyłce mastery podepną się do dostaw automatycznie.")


def run_search(query: str):
    """Wyszukuje pliki w zsynchronizowanej bibliotece."""
    try:
        resp = requests.get(
            f"{SERVER_URL}/api/library/search",
            params={"q": query, "limit": 20},
            headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
            timeout=TIMEOUT_S,
        )
        data = resp.json()
    except Exception as e:
        print(f"Błąd połączenia: {e}")
        return

    results = data.get("results", [])
    if not results:
        print(f'Brak wyników dla "{query}"')
        return

    print(f'Wyniki dla "{query}" ({len(results)}):')
    print("-" * 70)
    for r in results:
        print(f"  ID {r['id']:5d}  {r['filename']}")
        print(f"         {r['breadcrumb']}")
        if r.get("modified"):
            print(f"         Zmieniony: {r['modified'][:16]}  "
                  f"Rozmiar: {r.get('size',0)//1024} KB")
        if r.get("z_path"):
            print(f"         Ścieżka: {r['z_path']}")
        print()


if __name__ == "__main__":
    if requests is None:
        print("Zainstaluj requests: pip install requests")
        sys.exit(1)
    parser = argparse.ArgumentParser(description="DocCompare Library Sync")
    parser.add_argument("--full",    action="store_true", help="Prześlij wszystkie pliki od nowa")
    parser.add_argument("--dry-run", action="store_true", help="Tylko sprawdź co wymaga sync")
    parser.add_argument("--search",  metavar="QUERY",     help="Wyszukaj plik w bibliotece")
    parser.add_argument("--upload-masters", action="store_true",
                        help="Wyślij PEŁNE pliki PDF tylko dla potwierdzonych masterów "
                             "(potrzebnych przez dostawy) — bez całej biblioteki")
    args = parser.parse_args()

    if args.search:
        run_search(args.search)
    elif args.upload_masters:
        run_upload_masters(dry_run=args.dry_run)
    else:
        run_sync(full=args.full, dry_run=args.dry_run)
