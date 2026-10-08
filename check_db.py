"""
check_db.py — sprawdza połączenie z bazą danych i wypisuje listę tabel.

Użycie:
    python check_db.py
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

url = os.environ.get("DATABASE_URL", "")
sqlite_path = os.environ.get("SQLITE_PATH", "instance/doccompare.db")

if url:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    print(f"DATABASE_URL: {url[:60]}...")
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
            tables = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()   # zamknij połączenie nawet gdy zapytanie rzuci
        print(f"PostgreSQL OK — {len(tables)} tabel:")
        for t in tables:
            print(f"  {t}")
    except Exception as e:
        print(f"PostgreSQL błąd: {e}")
else:
    print(f"SQLite: {sqlite_path}")
    try:
        import sqlite3
        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        conn.close()
        print(f"SQLite OK — {len(tables)} tabel:")
        for t in tables:
            print(f"  {t}")
    except Exception as e:
        print(f"SQLite błąd: {e}")
