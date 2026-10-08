"""jobs.py — kolejkowanie ciężkich zadań (Poziom 1: RQ + Redis).

Cel: ciężką pracę (porównania artworków, batche, ekstrakcja PDF) wykonywać w
**osobnym procesie workera** zamiast w wątku procesu web. Web tylko wrzuca
zadanie do kolejki i jest natychmiast wolny.

ZASADA BEZPIECZEŃSTWA — brak twardej zależności od Redis:
- jeśli `REDIS_URL` jest ustawiony i biblioteki `rq`/`redis` są dostępne →
  zadanie trafia do kolejki RQ (czyta je `worker.py` w osobnym kontenerze),
- w przeciwnym razie → zadanie leci w wątku-daemonie (DOTYCHCZASOWE zachowanie).

Dzięki temu dev/SQLite oraz obecna produkcja działają bez zmian, dopóki nie
włączysz Redis. Pełny opis i runbook: docs/KOLEJKOWANIE.md.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

# Nazwa kolejki i limit czasu na pojedyncze zadanie (sekundy) — sterowane env.
QUEUE_NAME = os.environ.get("RQ_QUEUE", "doccompare")
JOB_TIMEOUT = int(os.environ.get("RQ_JOB_TIMEOUT", "1800"))  # 30 min / zadanie


def redis_enabled() -> bool:
    """True, jeśli skonfigurowano Redis (sterownik kolejki)."""
    return bool(os.environ.get("REDIS_URL", "").strip())


def get_queue():
    """Zwraca `rq.Queue` albo None, gdy Redis/rq są niedostępne.

    Celowo importuje redis/rq leniwie — gdy pakiety nie są zainstalowane,
    `enqueue()` po prostu użyje wątku (fallback), bez wysypywania aplikacji.
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        return Queue(QUEUE_NAME, connection=conn, default_timeout=JOB_TIMEOUT)
    except Exception as exc:  # brak pakietów / brak połączenia → fallback
        logger.warning("RQ/Redis niedostępne (%s) — fallback na wątek", exc)
        return None


def enqueue(func, *args, **kwargs) -> str | None:
    """Uruchom ciężkie zadanie `func(*args, **kwargs)`.

    - Tryb kolejki (REDIS_URL + rq): wrzuca do RQ; zwraca id zadania RQ.
      `func` musi być importowalny po ścieżce modułowej (RQ pickluje referencję),
      a `args`/`kwargs` muszą być serializowalne (lekkie — ścieżki/identyfikatory,
      nie wielkie obiekty). Worker liczy zadanie i aktualizuje tabelę postępu.
    - Tryb fallback (brak Redis): odpala `func` w wątku-daemonie (jak dotąd);
      zwraca None.

    Kontrakt postępu (tabela `job_progress`/`artwork_batch_jobs`) NIE zmienia się
    — zmienia się tylko *gdzie* liczy się zadanie.
    """
    queue = get_queue()
    if queue is not None:
        try:
            job = queue.enqueue(func, *args, **kwargs)
            logger.info(
                "Zadanie %s w kolejce RQ jako %s",
                getattr(func, "__name__", func), job.id,
            )
            return job.id
        except Exception as exc:
            # Nie chcemy stracić zadania, jeśli kolejka chwilowo padła.
            logger.warning("enqueue do RQ nie powiódł się (%s) — fallback na wątek", exc)

    t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return None


def run(func, *args, inline_sem=None, result_timeout=None, **kwargs):
    """Policz ciężkie zadanie i ZWRÓĆ jego wynik (kontrakt synchroniczny).

    Dla endpointów, które muszą oddać wynik w tej samej odpowiedzi HTTP
    (np. `/api/compare`, `/api/table_compare`):
    - Tryb kolejki (REDIS_URL): `func` liczy **worker** (web nie zżera CPU/RAM),
      a web czeka na wynik (czekanie to I/O, nie obciążenie). `func` musi być
      funkcją modułową, a argumenty/zwrot — serializowalne.
    - Tryb inline (brak Redis lub błąd kolejki): liczy bieżący proces; jeśli podano
      `inline_sem` (semafor), liczy pod nim — ochrona przed OOM bez kolejki.

    Zachowanie i kształt zwracanego wyniku są identyczne w obu trybach.
    """
    queue = get_queue()
    if queue is not None:
        try:
            import time as _time

            job = queue.enqueue(func, *args, job_timeout=JOB_TIMEOUT, **kwargs)
            deadline = _time.time() + (result_timeout or JOB_TIMEOUT)
            while _time.time() < deadline:
                status = job.get_status(refresh=True)
                if status == "finished":
                    rv = getattr(job, "return_value", None)
                    return rv() if callable(rv) else job.result
                if status in ("failed", "stopped", "canceled"):
                    raise RuntimeError(f"RQ job {job.id} status={status}")
                _time.sleep(0.5)
            raise TimeoutError(f"RQ job {job.id} przekroczył czas oczekiwania na wynik")
        except Exception as exc:
            logger.warning("RQ run nie powiódł się (%s) — liczę inline", exc)

    if inline_sem is not None:
        with inline_sem:
            return func(*args, **kwargs)
    return func(*args, **kwargs)
