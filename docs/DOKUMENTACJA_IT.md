# DocCompare v6 — dokumentacja dla Dyrektora IT

> Skrót zarządczo‑techniczny. Stan na 2026‑06‑15. Dotyczy aplikacji DocCompare (ACME), domena produkcyjna **compare.example.com**.

---

## 1. Streszczenie
DocCompare to wewnętrzna aplikacja webowa (Python/Flask) do **inteligentnego porównywania dokumentów handlu zagranicznego** (PO, proformy/faktury, packing listy, deklaracje celne) oraz **artworków/opakowań**, a także do **prowadzenia procesu dostaw przychodzących** (workflow 18 kroków: zamówienie → proforma → artwork → spedycja → odprawa → magazyn → rozliczenie). Wspiera dział zakupów, spedycji, magazynu i rozliczeń. Wykorzystuje AI (Claude) do walidacji i podpowiedzi.

Skala kodu: ~20 tys. linii w `app.py`, **358 endpointów HTTP**, kilkadziesiąt modułów Pythona, baza ~15 tys. indeksów produktowych.

## 2. Użytkownicy i role
Role rosnące: **user < manager < superuser < admin**. Dodatkowo **portale zewnętrzne** (spedytor, agencja celna) z izolowanym dostępem. Od czerwca 2026 wdrożony **podział per dział** — użytkownik/manager widzi tylko dostawy swojego działu; superuser/admin globalnie.

## 3. Stos technologiczny
- **Backend:** Python 3.11, Flask 3, Gunicorn (2 workery × 3 wątki).
- **Baza:** PostgreSQL (produkcja) / SQLite (dev) — wspólna warstwa abstrakcji `db.py` (tłumaczy dialekty, parametryzuje zapytania).
- **Frontend:** Jinja2 + Alpine.js (reaktywność), Tailwind CSS, Chart.js, Dropzone.
- **Przetwarzanie PDF/OCR/CV:** PyMuPDF, pdfplumber, Camelot, Tesseract, DocTR, PyTorch (CPU), scikit‑image (SSIM), pyzbar (kody kreskowe).
- **AI/ML:** Anthropic **Claude API** (walidacja, uczenie profili), rapidfuzz + scikit‑learn (dopasowanie semantyczne REF).
- **Raporty:** ReportLab (PDF), openpyxl (Excel).
- **Monitoring/poczta:** Sentry (błędy), Resend (e‑mail transakcyjny).

## 4. Architektura — główne moduły
- `app.py` — routing i logika tras (rdzeń).
- `table_extractor.py` / `normalizer.py` / `semantic_matcher.py` / `enhanced_comparator.py` — pipeline porównań dokumentów handlowych.
- `artwork_comparator.py` / `artwork_index.py` / `barcode_validator.py` — porównanie artworków, indeks masterów, walidacja EAN/GS1.
- `material_master.py` — master data produktów (REF, opisy, przeliczniki opakowań SZT/OP/OPZ/KAR/PAZ/PPA, wymiary, EAN).
- `ai_validator.py` / `ai_learning.py` — warstwa AI (ocena ryzyka, sugestie mapowań).
- `export_engine.py` — eksport PDF/Excel.
- `db.py` — abstrakcja bazy. `migrate_db.py` — migracje (uruchamiane przy starcie kontenera).

## 5. Hosting i wdrożenie
- **Konteneryzacja:** Docker (`python:3.11-slim`); obraz zawiera Tesseract (wiele języków), Poppler, Ghostscript, PyTorch CPU oraz dwie biblioteki CV instalowane z `git+https` (LightGlue, GroundingDINO).
- **Start kontenera:** `python migrate_db.py && gunicorn app:app --workers 2 --worker-class gthread --threads 3 --timeout 300`. **Migracja DB wykonuje się przy każdym starcie** (idempotentnie).
- **Środowisko produkcyjne:** `compare.example.com` (wdrożenia przez Coolify wg logów). W repo jest też `render.yaml` (Render.com, region Frankfurt/EU, dysk trwały `doccompare-data`) — warto ujednolicić dokumentację z faktycznym hostingiem.
- **CI/CD:** każdy push na gałąź roboczą przechodzi bramkę testów (pytest), a po zielonym wyniku jest auto‑scalany do `main` i deployowany.

## 6. Baza danych i przechowywanie plików
- **Dane:** dostawy (`kolejka_zlecenia`), pozycje PO, kontenery, dokumenty, porównania, master data produktów (~15 tys.), stany magazynowe (import), indeks artworków, użytkownicy, audyt.
- **⚠️ Pliki uploadów są EFEMERYCZNE:** aplikacja loguje ostrzeżenie `UPLOAD_FOLDER='uploads' may be ephemeral`. Wgrane pliki (np. bajty masterów artworków do podglądu, tymczasowe PDF‑y, raporty) **mogą zniknąć po restarcie/redeployu kontenera**, jeśli katalog nie jest na trwałym wolumenie. **Rekomendacja:** zamontować trwały wolumen i ustawić `UPLOAD_FOLDER=/data/uploads`.
- **Master data** wgrywana importem XLSX/CSV; struktura kolumn odwzorowuje plik migracyjny.

## 7. Integracje zewnętrzne
- **Anthropic Claude API** — koszt zmienny zależny od liczby porównań/walidacji (klucz `ANTHROPIC_API_KEY`). W aplikacji jest licznik użycia API (`api_usage`) i rate‑limiting.
- **Resend** — e‑maile (reset hasła, powiadomienia spedycyjne).
- **Sentry** — śledzenie błędów (opcjonalne).
- **Agent skanujący dysk Z:\\** (`tools/scan_artwork_index.py`, uruchamiany on‑premise) — cyklicznie indeksuje pliki artworków i wysyła **metadane** do chmury; pliki PDF zostają na Z:\\. Od czerwca 2026 agent dosyła też **bajty potwierdzonych masterów** (do podglądu i porównania) przez uwierzytelnione API.

## 8. Bezpieczeństwo i kontrola dostępu
- **Uwierzytelnianie:** sesje Flask (klucz `SECRET_KEY`), logowanie hasłem; reset hasła e‑mailem z tokenem.
- **Autoryzacja:** kontrola ról w trasach; **izolacja portali zewnętrznych** (spedytor/agent celny nie widzą wnętrza systemu); **podział per dział** dla dostaw (centralna bramka `before_request` — anty‑IDOR + filtr na listach).
- **CSRF:** ochrona `@csrf_protect` na akcjach modyfikujących; agent i UI przekazują token w nagłówku `X-CSRF-Token`.
- **SQL:** wyłącznie zapytania parametryzowane przez `db.py` (ochrona przed SQL‑injection).
- **Sekrety:** w zmiennych środowiskowych (`.env` / panel hostingu): `SECRET_KEY`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `SENTRY_DSN`, `APP_BASE_URL`. **Nie trzymać w repo.**
- **Audyt:** logi działań (`audit_log`), historia zmian statusów dostaw.

## 9. Kopie zapasowe i ciągłość
- **DB:** to jedyne źródło prawdy dla danych operacyjnych — wymaga regularnych, testowanych backupów (PITR po stronie dostawcy PostgreSQL: Supabase/Neon lub własny). **Do potwierdzenia z hostingiem.**
- **Pliki:** patrz pkt 6 — bez trwałego wolumenu uploady nie są w backupie.
- **Sekrety:** przechowywać w menedżerze sekretów / bezpiecznym vaulcie; mieć procedurę rotacji.

## 10. Wydajność i skalowanie
- Obraz jest „ciężki" (PyTorch + modele CV + Tesseract), build długi i wrażliwy na sieć przy `pip`/`git+https` (sporadyczne błędy builda = przejściowe, nie kod).
- RAM: 2 workery × ~2 GB modeli; rekomendowane ≥8 GB. Plan „starter" w `render.yaml` to minimum — pod realne obciążenie PDF/CV warto „standard".
- Długie operacje (PDF/AI) mają timeout 300 s; ciężkie zadania objęte semaforem współbieżności.

## 11. Najważniejsze ryzyka i rekomendacje (priorytetowo)
1. **Trwałość uploadów** — zamontować wolumen `/data/uploads` (inaczej utrata wgranych plików po redeployu). *Wysokie.*
2. **Backup DB** — potwierdzić i przetestować odtwarzanie. *Wysokie.*
3. **Ujednolicić dokumentację hostingu** (Render vs Coolify) i dostęp awaryjny. *Średnie.*
4. **Monitoring kosztów Claude API** + limity. *Średnie.*
5. **Rotacja sekretów** i zasada najmniejszych uprawnień dla konta agenta Z:\\. *Średnie.*
6. **Poufność handlowa/RODO** — dane cenowe dostawców i kontrahentów; podział per dział wdrożony, warto przypisać działy do wszystkich kont/dostaw (dostawy bez działu są widoczne szerzej). *Średnie.*

## 12. Kontakt / utrzymanie
- Repozytorium: repo źródłowe (GitHub).
- Brak osobnego zestawu testów E2E — testy jednostkowe (pytest) jako bramka CI; weryfikacja funkcjonalna ręcznie przez UI.
