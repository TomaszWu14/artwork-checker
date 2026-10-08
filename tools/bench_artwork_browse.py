#!/usr/bin/env python3
"""
tools/bench_artwork_browse.py — SRCH-03 standalone latency benchmark for
artwork_index.browse(), OUTSIDE the pytest gate (CLAUDE.md: CI "runs in
seconds" — this is a manual/backstop measurement, not an automated perf gate).

Seeds a THROWAWAY SQLite database (temp dir, never the dev/prod instance),
bulk-inserts --rows synthetic artwork_index rows via executemany (a third of
them also get an artwork_render_registry row, so the SRCH-02 has_3d badge
join runs under load), then times browse() with time.perf_counter() for:
  - a baseline empty query (q="")
  - an exact-REF query (the pin/SRCH-01 path)
  - a high-cardinality substring query ("karton" — every 4th row's packaging
    type, RESEARCH assumption A1)

Not a `tests/test_*.py` file — python -m pytest tests/ -q does not collect it.

Usage:
  python tools/bench_artwork_browse.py             # smoke run, 1000 rows
  python tools/bench_artwork_browse.py --rows 31000 # SRCH-03 real measurement
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

# Running as `python tools/bench_artwork_browse.py` only puts tools/ on
# sys.path, not the repo root where migrate_db.py/db.py/artwork_index.py live.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PACKAGING_TYPES = ["karton", "op", "opz", "sztuka"]
_VARIANTS = ["dieline", "marm", "photo"]


def _open_isolated_db(sqlite_path: str):
    """SQLITE_PATH is read at db.py import time — must be set BEFORE the
    first `import db` (directly or via migrate_db/artwork_index) anywhere in
    this process. Never points at the dev/prod instance DB (T-02-05)."""
    os.environ["SQLITE_PATH"] = sqlite_path
    os.environ.pop("DATABASE_URL", None)
    import migrate_db
    migrate_db.run()  # only place artwork_render_registry's table/index live (#1)
    from db import get_db
    return get_db()


def seed(db, n: int) -> str:
    import artwork_index as ai

    ai.ensure_table(db)
    db.execute("DELETE FROM artwork_index")
    db.execute("DELETE FROM artwork_render_registry")

    index_rows, registry_rows = [], []
    for i in range(n):
        ref_code = f"REF{i:06d}"
        ref_norm = ai.normalize_ref(ref_code)
        pt = _PACKAGING_TYPES[i % len(_PACKAGING_TYPES)]
        filename = f"{ref_code.lower()}_{pt}_rev_a.pdf"
        index_rows.append((ref_norm, ref_code, filename, f"z/{i}/{filename}", pt, "A", 1, ""))
        if i % 3 == 0:
            registry_rows.append((ref_norm, _VARIANTS[i % len(_VARIANTS)], "bench", f"/bench/{i}.glb"))
    db.executemany(
        "INSERT INTO artwork_index(ref_norm,ref_code,filename,rel_path,packaging_type,"
        "revision,revision_rank,ean) VALUES(?,?,?,?,?,?,?,?)", index_rows)
    db.executemany(
        "INSERT INTO artwork_render_registry(ref_norm,variant,source_hash,glb_path) "
        "VALUES(?,?,?,?)", registry_rows)
    db.commit()
    return index_rows[0][1]  # first ref_code, for the exact-match query


def _timed_browse(db, ai, **kwargs):
    t0 = time.perf_counter()
    res = ai.browse(db, **kwargs)
    return (time.perf_counter() - t0) * 1000.0, res


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=1000,
                         help="synthetic artwork_index row count (default: 1000 smoke; SRCH-03 real run: 31000)")
    args = parser.parse_args()

    tmp_dir = tempfile.mkdtemp(prefix="artwork_bench_")
    tmp_db = os.path.join(tmp_dir, "bench.db")
    try:
        db = _open_isolated_db(tmp_db)
        try:
            import artwork_index as ai
            first_ref = seed(db, args.rows)
            print(f"Seeded {args.rows} artwork_index rows (+ ~{args.rows // 3} registry rows) at {tmp_db}")

            ms_empty, _ = _timed_browse(db, ai, q="", per_page=50)
            print(f"  baseline (q=''):              {ms_empty:9.2f} ms")

            ms_exact, res_exact = _timed_browse(db, ai, q=first_ref, per_page=50)
            print(f"  exact-REF pin ({first_ref!r}):    {ms_exact:9.2f} ms  (total={res_exact['total']})")

            ms_hc, res_hc = _timed_browse(db, ai, q="karton", per_page=50)
            print(f"  high-cardinality ('karton'):  {ms_hc:9.2f} ms  (total={res_hc['total']})")
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
