#!/usr/bin/env python3
"""
tools/upload_masters.py — lokalny uploader masterów artworków ACME.

Uruchamiasz to LOKALNIE, w sieci ACME (maszyna widzi dysk Z:\\). Skrypt chodzi
po folderze, wgrywa PDF-y PACZKAMI do działającej instancji DocCompare
(POST /api/artwork/masters/upload), zachowując strukturę folderów i oryginalną
ścieżkę (source_path). Serwer sam wyciąga REF + typ opakowania + rewizję z nazwy.

Cechy:
  • wznawialny — pomija pliki z manifestu (uploaded_masters.json),
  • paczki grupowane po folderze (czysty folder_path),
  • retry z backoff, automatyczne ponowne logowanie po wygaśnięciu CSRF,
  • --dry-run i --limit do bezpiecznego testu na małej próbce.

Wymaga:  pip install requests

Przykład (najpierw test na 40 plikach):
  python upload_masters.py --base-url https://twoja-instancja \
      --user manager_login --root "Z:\\ISO\\WZORY ETYKIET I INSTRUKCJI" \
      --batch 20 --limit 40 --dry-run
  # potem bez --dry-run, a na końcu bez --limit
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
from collections import defaultdict

try:
    import requests
except ImportError:
    sys.exit("Brak biblioteki 'requests' — zainstaluj:  pip install requests")

_CSRF_META = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_CSRF_FORM = re.compile(r'name="_csrf_token"\s+value="([^"]+)"')


def login(session, base, user, pw):
    """Loguje się i zwraca świeży token CSRF z uwierzytelnionej strony."""
    r = session.get(base + "/login", timeout=30)
    r.raise_for_status()
    m = _CSRF_FORM.search(r.text)
    token = m.group(1) if m else ""
    r = session.post(base + "/login",
                     data={"username": user, "password": pw, "_csrf_token": token},
                     timeout=30, allow_redirects=True)
    if r.status_code >= 400 or r.url.rstrip("/").endswith("/login"):
        sys.exit("Logowanie nieudane — sprawdź login/hasło i rolę (wymagany manager).")
    r = session.get(base + "/artwork/profiles", timeout=30)
    m = _CSRF_META.search(r.text)
    return m.group(1) if m else token


def iter_pdfs(root):
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith(".pdf"):
                yield os.path.join(dirpath, n)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    ap = argparse.ArgumentParser(description="Lokalny uploader masterów artworków")
    ap.add_argument("--base-url", required=True, help="np. https://twoja-instancja")
    ap.add_argument("--user", required=True, help="login managera w DocCompare")
    ap.add_argument("--password", default=None, help="hasło (lub env DC_PASSWORD / pytanie)")
    ap.add_argument("--root", required=True, help='korzeń, np. "Z:\\ISO\\WZORY ETYKIET I INSTRUKCJI"')
    ap.add_argument("--batch", type=int, default=20, help="plików na jedno żądanie (domyślnie 20)")
    ap.add_argument("--manifest", default="uploaded_masters.json", help="plik postępu (wznawianie)")
    ap.add_argument("--limit", type=int, default=0, help="0 = bez limitu; >0 = tylko N plików (test)")
    ap.add_argument("--dry-run", action="store_true", help="tylko wypisz, nic nie wysyłaj")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    root_abs = os.path.abspath(args.root)
    if not os.path.isdir(root_abs):
        sys.exit(f"Nie znaleziono folderu: {root_abs}")

    done = set()
    if os.path.exists(args.manifest):
        try:
            done = set(json.load(open(args.manifest, encoding="utf-8")))
        except Exception:
            done = set()
    print(f"Manifest: już wgrane {len(done)} plików")

    all_files = [p for p in iter_pdfs(root_abs) if os.path.abspath(p) not in done]
    if args.limit:
        all_files = all_files[:args.limit]
    print(f"Do wgrania: {len(all_files)} PDF (z {root_abs})")
    if not all_files:
        return

    if args.dry_run:
        print("\n--dry-run — pierwsze 20 plików, które poszłyby:")
        for p in all_files[:20]:
            print("   ", p)
        return

    pw = args.password or os.environ.get("DC_PASSWORD") or getpass.getpass("Hasło: ")
    session = requests.Session()
    token = login(session, base, args.user, pw)
    print("Zalogowano.\n")

    # Grupuj po folderze nadrzędnym → spójny folder_path na paczkę
    by_dir = defaultdict(list)
    for p in all_files:
        by_dir[os.path.dirname(p)].append(p)

    ok_total = err_total = 0
    processed = 0

    for dpath, files in by_dir.items():
        rel = os.path.relpath(dpath, root_abs).replace("\\", "/")
        rel = "" if rel == "." else rel
        for batch in chunked(files, args.batch):
            handles = [("files[]", (os.path.basename(p), open(p, "rb"), "application/pdf"))
                       for p in batch]
            data = [("source_paths[]", p) for p in batch] + [("folder_path", rel)]
            sent = False
            for attempt in range(4):
                try:
                    r = session.post(base + "/api/artwork/masters/upload",
                                     files=handles, data=data,
                                     headers={"X-CSRF-Token": token}, timeout=900)
                    if r.status_code == 403:          # token wygasł → przeloguj
                        token = login(session, base, args.user, pw)
                        continue
                    r.raise_for_status()
                    res = r.json()
                    ok_total += res.get("success", 0)
                    err_total += res.get("total", 0) - res.get("success", 0)
                    for p in batch:
                        done.add(os.path.abspath(p))
                    sent = True
                    break
                except Exception as e:
                    wait = 2 ** attempt
                    print(f"   błąd paczki ({str(e)[:120]}); ponawiam za {wait}s")
                    time.sleep(wait)
            if not sent:
                err_total += len(batch)
            for _name, fh in handles:
                try:
                    fh[1].close()
                except Exception:
                    pass
            json.dump(sorted(done), open(args.manifest, "w", encoding="utf-8"))
            processed += len(batch)
            print(f"  [{rel or '/'}] postęp {processed}/{len(all_files)}  ok={ok_total} err={err_total}")

    print(f"\nGOTOWE. Wgrane OK={ok_total}, błędy={err_total}. Manifest: {args.manifest}")


if __name__ == "__main__":
    main()
