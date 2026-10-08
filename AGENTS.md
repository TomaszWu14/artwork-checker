# AGENTS.md

See `CLAUDE.md` for full project overview, architecture, conventions, and common commands.

## Cursor Cloud specific instructions

### Service overview

DocCompare v6 is a single monolithic Flask app (`app.py`). There is no separate frontend build step, no Docker, and no external services required for local dev. SQLite is the default dev database (auto-created at `instance/doccompare.db`).

### Running the dev server

```bash
python3 app.py
```

The server starts on `http://localhost:5000` with debug mode enabled. The login form field for username is `identifier` (accepts username or email).

### Default account

The app is single-user by design. First startup seeds exactly **one** account when no `admin`
user exists:

| Username | Password | Role |
|---|---|---|
| admin | random, generated at first run | admin |

The generated password is printed to stdout and saved to `INITIAL_ADMIN_PASSWORD.txt` in the
project root (gitignored). No `superuser` / `manager` / `user` accounts are created — those roles
exist in the hierarchy but hold no account until one is added deliberately. See `CLAUDE.md` for
how to reset a forgotten password.

### Database migrations

Run `python3 migrate_db.py` after code changes that alter the schema. This is idempotent and safe to re-run.

### System dependencies

The VM update script installs Python packages only. These system packages must be pre-installed (they are baked into the VM snapshot):

- `tesseract-ocr` + `tesseract-ocr-pol` (OCR fallback for scanned PDFs)
- `poppler-utils` (required by `pdf2image` for PDF-to-image conversion)
- `ghostscript` (improves Camelot PDF table parsing quality)

### Gotchas

- There is **no test suite or linter** configured in this repo. Testing is done manually via the web UI.
- The `python` command is not available; always use `python3`.
- The `.env` file must exist with at least `SECRET_KEY` and `SQLITE_PATH=instance/doccompare.db` set. Copy from `.env.example` if missing.
- `ANTHROPIC_API_KEY` is required for AI validation features but the app starts fine without it; AI-dependent routes will error at runtime.
- `RESEND_API_KEY` is optional (password-reset emails). `SENTRY_DSN` is optional (error tracking).
- Uploaded PDFs go to `uploads/` (auto-created, gitignored).
