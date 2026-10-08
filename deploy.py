"""
deploy.py — wdraża wszystkie pliki z aktualizacji DocCompare.

Uruchom w katalogu projektu (tam gdzie jest app.py):
    python deploy.py

Co robi:
  1. Kopiuje nowe pliki Python (comparator, AI, artwork)
  2. Tworzy templates/ i kopiuje HTML
  3. Migruje bazę danych
  4. Weryfikuje składnię
"""
import os, sys, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

FILES_TO_COPY = {
    # źródło (obok deploy.py) → cel (w projekcie)
    "enhanced_comparator.py":        "enhanced_comparator.py",
    "ai_validator.py":               "ai_validator.py",
    "ai_learning.py":                "ai_learning.py",
    "artwork_comparator.py":         "artwork_comparator.py",
    "migrate_db.py":                 "migrate_db.py",
    "app.py":                        "app.py",
}
TEMPLATES = {
    "artwork.html":  os.path.join("templates", "artwork.html"),
    "kpi.html":      os.path.join("templates", "kpi.html"),
}

def run():
    print("=" * 56)
    print("  DocCompare ACME — Deploy")
    print("=" * 56)

    # Backup app.py
    if os.path.exists("app.py") and not os.path.exists("app.py.bak"):
        shutil.copy2("app.py", "app.py.bak")
        print("  ✅  Backup: app.py → app.py.bak")

    # Kopiuj pliki Python
    print("\n── Pliki Python ──")
    for src_name, dst_name in FILES_TO_COPY.items():
        src = os.path.join(HERE, src_name)
        if not os.path.exists(src):
            print(f"  ⚠   BRAK: {src_name} (pomiń)")
            continue
        shutil.copy2(src, dst_name)
        print(f"  ✅  {dst_name}")

    # Kopiuj templates
    print("\n── Templates HTML ──")
    os.makedirs("templates", exist_ok=True)
    for src_name, dst_path in TEMPLATES.items():
        src = os.path.join(HERE, src_name)
        if not os.path.exists(src):
            print(f"  ⚠   BRAK: {src_name} (pomiń)")
            continue
        if os.path.exists(dst_path):
            shutil.copy2(dst_path, dst_path + ".bak")
        shutil.copy2(src, dst_path)
        print(f"  ✅  {dst_path}")

    # Patch templates (nawigacja)
    patch_script = os.path.join(HERE, "patch_templates.py")
    if os.path.exists(patch_script):
        print("\n── Patch nawigacji templates ──")
        result = subprocess.run([sys.executable, patch_script],
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.strip():
                print(f"  {line.strip()}")

    # Składnia app.py
    print("\n── Weryfikacja ──")
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("  ✅  app.py — składnia OK")
    else:
        print(f"  ❌  Błąd składni: {r.stderr[:300]}")
        return

    # Migracja bazy
    if os.path.exists("migrate_db.py"):
        print("\n── Migracja bazy ──")
        r2 = subprocess.run([sys.executable, "migrate_db.py"],
                            capture_output=True, text=True)
        for line in r2.stdout.splitlines():
            if "✅" in line or "❌" in line or "Migracja" in line:
                print(f"  {line.strip()}")
        # Nie ukrywaj nieudanej migracji — pokaż stderr i kod wyjścia.
        if r2.returncode != 0:
            print(f"  ⚠ Migracja zakończona kodem {r2.returncode}")
            if r2.stderr:
                print("  " + r2.stderr.strip()[:500])

    print("\n" + "=" * 56)
    print("  ✅  Gotowe! Uruchom: python app.py")
    print("=" * 56)
    print()
    print("  Nowe funkcje:")
    print("  → http://192.0.2.196:5000/artwork    🎨 Artwork")
    print("  → http://192.0.2.196:5000/kpi        📊 KPI")

if __name__ == "__main__":
    run()
