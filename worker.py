"""worker.py — proces workera RQ (Poziom 1 kolejkowania).

Pobiera ciężkie zadania z kolejki Redis i je wykonuje (porównania artworków,
batche itd.), aktualizując tabelę postępu w PostgreSQL — dokładnie tę samą,
którą odpytuje front. Web pozostaje lekki i wolny.

Uruchomienie (osobny kontener/serwis, TEN SAM obraz co web):
    python worker.py
albo standardowym CLI RQ:
    rq worker -u "$REDIS_URL" doccompare

Wymaga env `REDIS_URL` (np. redis://redis:6379/0) oraz tych samych zmiennych co
web (DATABASE_URL/SQLITE_PATH, ANTHROPIC_API_KEY, …). Jeśli web i worker są w
osobnych kontenerach, muszą współdzielić katalog uploadów (wspólny wolumen) —
patrz docs/KOLEJKOWANIE.md.
"""

import os
import sys


def main() -> int:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        print("REDIS_URL nie ustawiony — worker RQ nie ma z czego czytać.", file=sys.stderr)
        return 1

    try:
        from redis import Redis
        from rq import Queue, Worker
    except ImportError:
        print("Brak pakietów 'rq'/'redis' — dodaj je do requirements i przebuduj obraz.",
              file=sys.stderr)
        return 1

    # Import aplikacji ładuje logikę biznesową i modele (OCR/CV) w TYM procesie,
    # dzięki czemu zadania mogą wołać funkcje z app.py po referencji.
    import app  # noqa: F401

    queue_name = os.environ.get("RQ_QUEUE", "doccompare")
    conn = Redis.from_url(url)
    worker = Worker([Queue(queue_name, connection=conn)], connection=conn)
    worker.work()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
