"""Testy abstrakcji kolejki (jobs.py) — sprawdzają fallback na wątek bez Redis.

Lekkie, bez zależności od redis/rq — pokrywają ścieżkę domyślną (brak REDIS_URL),
która jest dotychczasowym zachowaniem aplikacji.
"""

import os
import threading

import jobs


def test_redis_disabled_by_default(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert jobs.redis_enabled() is False
    assert jobs.get_queue() is None


def test_redis_enabled_flag(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert jobs.redis_enabled() is True
    # Pusty/whitespace traktujemy jak brak.
    monkeypatch.setenv("REDIS_URL", "   ")
    assert jobs.redis_enabled() is False


def test_enqueue_runs_in_thread_when_no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    done = threading.Event()
    captured = {}

    def work(a, b, label=None):
        captured["sum"] = a + b
        captured["label"] = label
        done.set()

    job_id = jobs.enqueue(work, 2, 3, label="x")

    # Fallback wątkowy nie zwraca id zadania RQ.
    assert job_id is None
    assert done.wait(timeout=5), "zadanie fallback nie wykonało się w wątku"
    assert captured["sum"] == 5
    assert captured["label"] == "x"


def test_enqueue_falls_back_when_queue_unavailable(monkeypatch):
    # REDIS_URL ustawiony, ale get_queue zwraca None (np. brak pakietów) →
    # nadal musi zadziałać fallback wątkowy, nie wyjątek.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(jobs, "get_queue", lambda: None)
    done = threading.Event()

    job_id = jobs.enqueue(lambda: done.set())
    assert job_id is None
    assert done.wait(timeout=5)


def test_run_returns_result_inline_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    out = jobs.run(lambda a, b: {"sum": a + b}, 4, 5)
    assert out == {"sum": 9}


def test_run_uses_inline_semaphore(monkeypatch):
    import threading
    monkeypatch.delenv("REDIS_URL", raising=False)
    sem = threading.Semaphore(1)
    # Semafor musi być wolny przed i po (run zwalnia go po wykonaniu).
    assert sem.acquire(blocking=False)
    sem.release()
    result = jobs.run(lambda: sem._value, inline_sem=sem)
    # Wewnątrz sekcji semafor był zajęty (value 0).
    assert result == 0
    # Po zakończeniu semafor znów wolny.
    assert sem.acquire(blocking=False)
    sem.release()
