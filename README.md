> **Projekt portfolio.** Nazwy firm są zamienione na fikcyjne, a dane demo i testowe są syntetyczne.
>
> Kod udostępniony do wglądu (portfolio), wszelkie prawa zastrzeżone — patrz [`LICENSE`](LICENSE).

# Artwork — porównywanie projektów opakowań

[![ci](https://github.com/TomaszWu14/artwork-checker/actions/workflows/ci.yml/badge.svg)](https://github.com/TomaszWu14/artwork-checker/actions/workflows/ci.yml)

Aplikacja Flask dla działu zakupów/jakości firmy dystrybucyjnej: porównuje
artworki opakowań (PDF) z masterami, waliduje kody kreskowe (EAN/GS1), prowadzi
indeks masterów z wersjonowaniem i renderuje opakowania w 3D. Wydzielona
z większego systemu porównywania dokumentów (PO/PI/CI).

## W skrócie

| | |
|---|---|
| **Problem** | Ręczne porównywanie projektów opakowań od dostawców z wzorcami — błędy w kodach kreskowych, datach, nazwach i wersjach wychodziły dopiero na towarze. |
| **Rozwiązanie** | Porównanie strefowe PDF (tekst + obraz), walidacja GS1/EAN, indeks masterów z historią wersji, raport różnic (PDF/Excel), podgląd 3D opakowania. |
| **Stack** | Python, Flask, pdfplumber + PyMuPDF + camelot (PDF), RapidOCR/Tesseract (OCR), PostgreSQL/SQLite, gunicorn, Docker; frontend: szablony Jinja + JS (pdf.js, three.js). |
| **Jakość** | ~450 testów (`pytest tests/`), CI w GitHub Actions, skany bezpieczeństwa. |

## Uruchomienie

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python migrate_db.py            # baza SQLite w instance/
python app.py                   # http://localhost:5000
```

Konta demo tworzone przy migracji: `admin` / `admin123`, `superuser` / `super123`,
`jan.kowalski` / `haslo123`. Zmień je przed wystawieniem aplikacji poza localhost.

Opcjonalnie: Tesseract (OCR) i Ghostscript poprawiają jakość parsowania PDF.

## Struktura

| Plik / katalog | Rola |
|---|---|
| `app.py` | aplikacja Flask (trasy, widoki) |
| `artwork_comparator.py`, `artwork_zone_comparator.py` | porównanie artwork ↔ master |
| `barcode_validator.py` | walidacja EAN/GS1 i dat |
| `artwork_index.py`, `artwork_naming.py` | indeks masterów, nazewnictwo wersji |
| `artwork_3d.py`, `dieline_templates.py` | wykrojniki i podgląd 3D |
| `export_engine.py`, `artwork_report_engine.py` | raporty PDF/Excel |
| `templates/`, `static/` | interfejs |
| `tests/` | testy pytest |

## Testy

```bash
pip install pytest
pytest tests/ -q
```
