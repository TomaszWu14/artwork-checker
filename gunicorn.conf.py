import os

# Worker configuration
# CLI flags in render.yaml and Dockerfile have been removed — this file is the
# authoritative gunicorn config.  Override via env vars in Render dashboard or
# .env for local dev.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("THREADS_PER_WORKER", "3"))
worker_class = "gthread"

# Timeouts
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5

# Request limits — restart workers periodically to reclaim memory leaks
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.environ.get("LOG_LEVEL", "warning").lower()

# Performance — use RAM-backed tmpfs for worker heartbeat files
# (avoids disk I/O stalls when /tmp is slow or full). Tylko jeśli /dev/shm
# istnieje — na minimalnych kontenerach bez tmpfs gunicorn inaczej nie wstaje.
import os as _os
# Bandit B108: /dev/shm to zalecany przez gunicorn tmpfs na pliki heartbeat workerów, nie dane aplikacji.
worker_tmp_dir = "/dev/shm" if _os.path.isdir("/dev/shm") else None  # nosec B108
