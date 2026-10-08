#!/usr/bin/env python3
"""
tools/scan_artwork_index.py — agent skanujący indeks masterów artworków (#5).

Uruchamiasz LOKALNIE w sieci ACME (maszyna widzi Z:\\). Skrypt chodzi po
folderze, zbiera metadane plików (nazwa, ścieżka względna, czas modyfikacji) i
wysyła PACZKAMI do chmury (POST /api/artwork/index/sync). PLIKI ZOSTAJĄ NA Z:\\ —
wysyłamy wyłącznie metadane (bez treści), więc dane wrażliwe nie opuszczają dysku.

Cykliczne uruchamianie (np. Harmonogram zadań Windows co godzinę) utrzymuje indeks
aktualny — nowe rewizje pojawią się automatycznie.

Po skanie agent DOSYŁA też bajty masterów potrzebnych do podglądu (potwierdzone/
grupowe), czytając pliki po znanej ścieżce — dzięki temu „podgląd wkrótce…" znika
automatycznie, bez ręcznego wgrywania.

Wymaga:  pip install requests

Przykład (najpierw test):
  python scan_artwork_index.py --base-url https://compare.example.com \\
      --user manager_login --root "Z:\\ISO\\WZORY ETYKIET I INSTRUKCJI" \\
      --batch 500 --limit 1000 --dry-run
  # potem bez --dry-run, na końcu bez --limit (pełny skan)

Szybka, częsta dosyłka SAMYCH podglądów (bez kosztownego skanu — do uruchamiania
np. co 10 min w Harmonogramie zadań Windows):
  python scan_artwork_index.py --base-url https://compare.example.com \\
      --user manager_login --root "Z:\\ISO\\WZORY ETYKIET I INSTRUKCJI" --only-upload

Tryb CIĄGŁY (agent „pilnuje" sam — uruchom raz na maszynie zawsze online w sieci
wewnętrznej; dosyła nowe podglądy automatycznie, bez ręcznego ładowania):
  python scan_artwork_index.py --base-url https://compare.example.com \\
      --user manager_login --root "Z:\\ISO\\WZORY ETYKIET I INSTRUKCJI" \\
      --watch --interval 120
"""
import argparse
import getpass
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Brak biblioteki 'requests' — zainstaluj:  pip install requests")

_CSRF_META = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_CSRF_FORM = re.compile(r'name="_csrf_token"\s+value="([^"]+)"')
_EXTS = {".pdf"}


def _login(session, base_url, user, password):
    r = session.get(base_url + "/login", timeout=30)
    m = _CSRF_META.search(r.text) or _CSRF_FORM.search(r.text)
    token = m.group(1) if m else ""
    r = session.post(base_url + "/login",
                     data={"identifier": user, "password": password, "_csrf_token": token},
                     headers={"X-CSRF-Token": token}, timeout=30, allow_redirects=True)
    if "/login" in r.url:
        sys.exit("Logowanie nieudane — sprawdź login/hasło.")
    # token do kolejnych POST-ów
    r2 = session.get(base_url + "/dostawy", timeout=30)
    m2 = _CSRF_META.search(r2.text) or _CSRF_FORM.search(r2.text)
    return m2.group(1) if m2 else token


def _iter_entries(root):
    root = os.path.abspath(root)
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in _EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(os.path.getmtime(full)))
            except OSError:
                mtime = ""
            rel = os.path.relpath(full, root).replace("\\", "/")
            yield {"filename": fn, "rel_path": rel, "source_mtime": mtime}


def _upload_wanted(session, base_url, token, root):
    """Dosyła bajty masterów, których chmura potrzebuje do podglądu (potwierdzone/
    grupowe). Pliki zostają na Z:\\, ale potwierdzone kopiujemy do chmury na żądanie."""
    try:
        r = session.get(base_url + "/api/artwork/master-files/wanted", timeout=60)
        if r.status_code != 200:
            print(f"  podgląd: HTTP {r.status_code}"); return
        wanted = r.json().get("wanted", [])
    except requests.RequestException as e:
        print(f"  podgląd: błąd sieci: {e}"); return
    if not wanted:
        print("  podgląd: brak nowych plików do dosłania."); return
    print(f"  podgląd: dosyłam {len(wanted)} plik(ów) master…")
    up, miss = 0, 0
    root_abs = os.path.abspath(root)
    for rel in wanted:
        full = os.path.normpath(os.path.join(root, rel.replace("/", os.sep)))
        # Ochrona przed path-traversal: nie wychodź poza katalog root (rel z serwera).
        if os.path.commonpath([os.path.abspath(full), root_abs]) != root_abs:
            print(f"    {rel}: poza katalogiem root — pomijam"); miss += 1; continue
        if not os.path.isfile(full):
            miss += 1; continue
        try:
            with open(full, "rb") as fh:
                resp = session.post(base_url + "/api/artwork/master-files/upload",
                                    data={"rel_path": rel},
                                    files={"file": (os.path.basename(rel), fh,
                                                    "application/pdf")},
                                    headers={"X-CSRF-Token": token}, timeout=180)
            if resp.status_code == 200:
                up += 1
            else:
                print(f"    {rel}: HTTP {resp.status_code} {resp.text[:120]}")
        except (requests.RequestException, OSError) as e:
            print(f"    {rel}: {e}")
    print(f"  podgląd: wgrano {up}, brak na dysku {miss}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="np. https://compare.example.com")
    ap.add_argument("--user", required=True)
    ap.add_argument("--root", required=True, help="folder z masterami (Z:\\...)")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--limit", type=int, default=0, help="0 = bez limitu")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-upload", action="store_true",
                    help="nie dosyłaj plików master do podglądu (tylko metadane)")
    ap.add_argument("--only-upload", action="store_true",
                    help="POMIŃ skanowanie indeksu — dosyłaj WYŁĄCZNIE brakujące "
                         "pliki master do podglądu (szybkie; do częstego uruchamiania, "
                         "np. co 10 min w Harmonogramie zadań Windows)")
    ap.add_argument("--watch", action="store_true",
                    help="TRYB CIĄGŁY: agent działa bez końca i co --interval sekund "
                         "sam dosyła brakujące podglądy (pobiera pliki z Z:\\ gdy tylko "
                         "pojawi się nowy master). Uruchom na maszynie zawsze online "
                         "w sieci wewnętrznej. Ctrl+C kończy.")
    ap.add_argument("--interval", type=int, default=120,
                    help="odstęp w sekundach dla --watch (domyślnie 120, min 30)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if not os.path.isdir(args.root):
        sys.exit(f"Nie znaleziono folderu: {args.root}")

    pwd = os.environ.get("DOCCOMPARE_PASS") or getpass.getpass("Hasło: ")
    session = requests.Session()
    token = "" if args.dry_run else _login(session, base, args.user, pwd)

    # Tryb ciągły: agent „pilnuje" sam i dosyła nowe podglądy bez ręcznej akcji.
    # Świeże logowanie na każdy cykl — sesja w chmurze może wygasnąć przy długim
    # działaniu; pojedynczy błąd cyklu nie zabija pętli.
    if args.watch:
        if args.dry_run:
            sys.exit("--watch nie współpracuje z --dry-run.")
        interval = max(30, args.interval)
        print(f"Tryb ciągły: dosyłka podglądów co {interval}s (root: {args.root}). "
              f"Ctrl+C aby zakończyć.")
        try:
            while True:
                try:
                    token = _login(session, base, args.user, pwd)
                    _upload_wanted(session, base, token, args.root)
                except (requests.RequestException, SystemExit) as e:
                    print(f"  cykl pominięty (błąd sieci/logowania): {e}")
                except Exception as e:                       # nie zabijaj pętli na innym błędzie
                    print(f"  cykl pominięty (błąd: {type(e).__name__}): {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nZatrzymano.")
        return

    # Szybki tryb: tylko dosyłka podglądów (bez kosztownego skanu całego Z:\).
    if args.only_upload:
        if args.dry_run:
            print("--only-upload + --dry-run: nic nie wysyłam (tryb testowy).")
            return
        _upload_wanted(session, base, token, args.root)
        return

    batch, sent, indexed, skipped = [], 0, 0, 0

    def flush():
        nonlocal batch, indexed, skipped
        if not batch:
            return
        if args.dry_run:
            indexed += len(batch); batch = []; return
        for attempt in range(4):
            try:
                resp = session.post(base + "/api/artwork/index/sync",
                                    json={"entries": batch},
                                    headers={"X-CSRF-Token": token}, timeout=60)
                if resp.status_code == 200:
                    d = resp.json()
                    indexed += d.get("indexed", 0); skipped += d.get("skipped", 0)
                    batch = []
                    return
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as e:
                print(f"  błąd sieci: {e}")
            time.sleep(2 ** attempt)
        sys.exit("Zbyt wiele błędów wysyłki — przerwano.")

    for e in _iter_entries(args.root):
        batch.append(e); sent += 1
        if len(batch) >= args.batch:
            flush()
            print(f"  …wysłano {sent} (zindeksowane {indexed}, pominięte {skipped})")
        if args.limit and sent >= args.limit:
            break
    flush()
    print(f"\nGotowe. Plików: {sent} | zindeksowane: {indexed} | pominięte: {skipped}"
          + (" [DRY-RUN]" if args.dry_run else ""))
    # Dosyłanie bajtów masterów potrzebnych do podglądu (potwierdzone/grupowe).
    if not args.dry_run and not args.no_upload:
        _upload_wanted(session, base, token, args.root)


if __name__ == "__main__":
    main()
