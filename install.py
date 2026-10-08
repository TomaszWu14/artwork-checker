"""
install.py — automatyczny instalator aktualizacji DocCompare ACME.

Co robi:
  1. Tworzy backup app.py → app.py.bak
  2. Wstrzykuje nowe endpointy na koniec app.py (przed if __name__)
  3. Patchuje api_analyze() o auto-learning hook
  4. Tworzy folder templates/ jeśli nie istnieje
  5. Weryfikuje że wszystkie wymagane pliki są na miejscu
  6. Uruchamia migrate_db.py

Uruchomienie:
    python install.py

Aby cofnąć:
    copy app.py.bak app.py  (Windows)
    cp app.py.bak app.py    (Linux/Mac)
"""

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Pliki które muszą być w katalogu projektu ────────────────────────────────
REQUIRED_FILES = [
    "enhanced_comparator.py",
    "ai_validator.py",
    "ai_learning.py",
    "artwork_comparator.py",
    "migrate_db.py",
    "app.py",
]

# Plik z endpointami do wklejenia
ENDPOINTS_FILE = "WSZYSTKIE_ENDPOINTY_do_app.py"

# Template do skopiowania
ARTWORK_TEMPLATE_SRC = "artwork.html"
ARTWORK_TEMPLATE_DST = os.path.join("templates", "artwork.html")


def check() -> bool:
    """Sprawdź czy wszystkie wymagane pliki są na miejscu."""
    print("\n── Weryfikacja plików ──")
    ok = True
    for f in REQUIRED_FILES + [ENDPOINTS_FILE, ARTWORK_TEMPLATE_SRC]:
        path = os.path.join(HERE, f)
        exists = os.path.exists(path)
        size = os.path.getsize(path) // 1024 if exists else 0
        status = f"✅  {f} ({size}KB)" if exists else f"❌  BRAK: {f}"
        print(f"  {status}")
        if not exists:
            ok = False
    return ok


def backup_app():
    """Tworzy backup app.py."""
    src = os.path.join(HERE, "app.py")
    dst = os.path.join(HERE, "app.py.bak")
    shutil.copy2(src, dst)
    print(f"  ✅  Backup: app.py → app.py.bak")


def inject_endpoints():
    """Wstrzykuje nowe endpointy na koniec app.py."""
    app_path      = os.path.join(HERE, "app.py")
    endpoints_path = os.path.join(HERE, ENDPOINTS_FILE)

    with open(app_path, encoding="utf-8") as _f:
        app_src = _f.read()
    with open(endpoints_path, encoding="utf-8") as _f:
        endpoints_src = _f.read()

    # Sprawdź czy już wstrzyknięte
    if "api_artwork_compare" in app_src:
        print("  ⏭   Endpointy już wstrzyknięte — pomijam")
        return False

    # Znajdź if __name__ == "__main__":
    main_idx = app_src.rfind('\nif __name__')
    if main_idx == -1:
        # Wklej na koniec
        new_src = app_src + "\n\n" + endpoints_src
    else:
        new_src = app_src[:main_idx] + "\n\n" + endpoints_src + "\n" + app_src[main_idx:]

    with open(app_path, "w", encoding="utf-8") as f:
        f.write(new_src)

    print("  ✅  Endpointy wstrzyknięte do app.py")
    return True


def patch_api_analyze():
    """Dodaje _trigger_background_learning do api_analyze()."""
    app_path = os.path.join(HERE, "app.py")
    src      = open(app_path, encoding="utf-8").read()

    # Sprawdź czy już spatchowane
    if "_trigger_background_learning" in src:
        print("  ⏭   api_analyze patch już zastosowany — pomijam")
        return

    # Znajdź ostatni return jsonify(report) w api_analyze
    # Szukaj wzorca: return jsonify(report) poprzedzonego przez "report["
    pattern = r'(    report\["comparison_id"\][^\n]+\n\n    # ── Powiadomienie[^)]+\n\n    # ── Dołącz[^)]+\n[^\n]+\n\n)(    return jsonify\(report\))'

    replacement = r'\1    _trigger_background_learning(report)\n\2'
    new_src, count = re.subn(pattern, replacement, src, count=1, flags=re.DOTALL)

    if count == 0:
        # Prostsze podejście - znajdź "return jsonify(report)" wewnątrz api_analyze
        # i poprzedź je wywołaniem
        idx = src.find('def api_analyze():')
        if idx == -1:
            print("  ⚠   Nie znaleziono api_analyze() — pomiń patch")
            return

        # Znajdź koniec funkcji (następną definicję na tym samym poziomie wcięcia)
        end_idx = src.find('\n@app.route', idx + 100)
        if end_idx == -1:
            end_idx = len(src)

        func_body = src[idx:end_idx]
        last_return = func_body.rfind('    return jsonify(report)')
        if last_return == -1:
            print("  ⚠   Nie znaleziono return jsonify(report) — pomiń patch")
            return

        abs_pos = idx + last_return
        insert = "    _trigger_background_learning(report)\n"
        new_src = src[:abs_pos] + insert + src[abs_pos:]
        count = 1

    if count > 0:
        with open(app_path, "w", encoding="utf-8") as f:
            f.write(new_src)
        print("  ✅  api_analyze() spatchowany o auto-learning")
    else:
        print("  ⚠   Nie udało się spatchować api_analyze() — zrób ręcznie")


def create_templates():
    """Tworzy folder templates/ i kopiuje artwork.html."""
    tmpl_dir = os.path.join(HERE, "templates")
    os.makedirs(tmpl_dir, exist_ok=True)
    print(f"  ✅  Folder templates/ gotowy")

    src = os.path.join(HERE, ARTWORK_TEMPLATE_SRC)
    dst = os.path.join(HERE, ARTWORK_TEMPLATE_DST)

    if os.path.exists(dst):
        print(f"  ⏭   templates/artwork.html już istnieje — pomijam")
    else:
        shutil.copy2(src, dst)
        print(f"  ✅  templates/artwork.html skopiowany")


def run_migrate():
    """Uruchamia migrate_db.py."""
    result = subprocess.run(
        [sys.executable, os.path.join(HERE, "migrate_db.py")],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        # Wydrukuj tylko kluczowe linie
        for line in result.stdout.split("\n"):
            if "✅" in line or "❌" in line or "Migracja" in line:
                print(f"  {line.strip()}")
    else:
        print("  ❌  Błąd migrate_db.py:")
        print(result.stderr[:500])


def verify_syntax():
    """Sprawdź składnię app.py po patchach."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.join(HERE, "app.py")],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✅  app.py — składnia Python OK")
    else:
        print("  ❌  BŁĄD SKŁADNI app.py:")
        print(result.stderr[:500])
        print("\n  Przywróć backup: copy app.py.bak app.py")
        return False
    return True


def main():
    print("=" * 60)
    print("  DocCompare ACME — Instalator aktualizacji")
    print("=" * 60)

    # 1. Sprawdź pliki
    if not check():
        print("\n❌ Brakuje wymaganych plików. Umieść wszystkie pliki")
        print("   w tym samym katalogu co app.py i uruchom ponownie.")
        sys.exit(1)

    print("\n── Backup ──")
    backup_app()

    print("\n── Endpointy Flask ──")
    inject_endpoints()

    print("\n── Patch api_analyze ──")
    patch_api_analyze()

    print("\n── Templates ──")
    create_templates()

    print("\n── Weryfikacja składni ──")
    ok = verify_syntax()
    if not ok:
        sys.exit(1)

    print("\n── Migracja bazy danych ──")
    run_migrate()

    print("\n" + "=" * 60)
    print("  ✅  Instalacja zakończona pomyślnie!")
    print("=" * 60)
    print()
    print("  Następny krok — uruchom aplikację:")
    print()
    print("  Windows PowerShell:")
    print('    $env:ANTHROPIC_API_KEY = "sk-ant-..."')
    print("    python app.py")
    print()
    print("  Nowe funkcje:")
    print("    http://localhost:5000/artwork        🎨 Artwork Comparator")
    print("    http://localhost:5000/api/ai/status  🤖 Status AI")
    print()


if __name__ == "__main__":
    main()
