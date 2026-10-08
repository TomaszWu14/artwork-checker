#!/bin/bash
# Run the app in staging mode (separate DB, port 5001)
# .env.staging is gitignored (may hold real secrets) — seed it from the tracked
# template on first run so a fresh clone works out of the box.
[ -f .env.staging ] || cp .env.staging.example .env.staging
export $(cat .env.staging | xargs)
python app.py
