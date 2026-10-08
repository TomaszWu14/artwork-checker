# Spec: Wydzielenie projektu **Artwork** z DocCompare v6

**Data:** 2026-08-12
**Autor:** Tomasz (+ Claude)
**Status:** zatwierdzony do implementacji

## Cel

Stworzyć nowe, odchudzone repo **`artwork`** skupione w 100% na pracy z artworkami:
porównywanie artworków, walidacja kodów kreskowych, indeks/biblioteka masterów,
wizualizacja 3D opakowań (artwork_3d / PalViz), zarządzanie danymi opakowań.
Obecne repo `compare` (DocCompare v6) zostaje **zarchiwizowane** na GitHubie
(read-only) pod dotychczasową nazwą. Nowe repo powstaje jako **fork z pełną
historią git** (nie fresh start — zachowujemy historię plików artworkowych).

## Kontekst / stan wyjściowy

- `app.py` ma **~367 tras**, z czego **tylko ~22 są artworkowe**. Reszta (~345) to
  Zakupy / transport / spedycja / agencja celna.
- Wniosek: chirurgiczne wycięcie 345 tras w jednym commicie jest zbyt ryzykowne
  (wspólne helpery, sesje, dekoratory). Idziemy fazami z zielonym boot+testami.
- Strażnik regresji: `tests/test_app_boots.py` + pełny `pytest tests/`.

## Zakres — CO ZOSTAJE

**Artwork core (10 modułów):**
`artwork_comparator.py`, `artwork_3d.py`, `artwork_index.py`, `artwork_naming.py`,
`artwork_report_engine.py`, `artwork_batch_processor.py`, `artwork_zone_comparator.py`,
`barcode_validator.py`, `library_sync.py`, `yolo_detector.py`.

**Wspólna infra:**
`db.py`, `core/` (security, audit), `normalizer.py`, `uom.py`, `constants.py`,
`migrate_db.py`, `pdf_extractor.py`, `table_extractor.py` + silniki OCR,
`translations*.py`, `blueprints/` (dict/incoterms wg potrzeb).

**Szare (potwierdzone jako KEEP — używane też przez artwork):**
`material_master.py` + `uom_conversion`, `supplier_master.py` + `supplier_profiles.py`,
`ai_validator.py` + `ai_learning.py`, `api_usage_tracker.py`.

**Trasy:** auth + ~22 trasy artwork/library/master/3d. Szablony: `base.html` +
`artwork_*.html` + auth.

## Zakres — CO WYCHODZI (balast)

- **Transport/kolejka:** `delivery_workflow.py`, `demurrage.py`, `alert_engine.py`,
  `warehouse_stock.py`, `cn_audit.py`, `kolejka_*`, `zlecenia_*`, `intake_queue`.
- **Portale:** spedycja (`forwarder`), agencja celna (`customs_agent`) — role
  zewnętrzne, prefixy, `EXTERNAL_ROLE_HOME`.
- **Porównywarka dokumentów handlowych:** `enhanced_comparator.py`, `comparator.py`,
  `semantic_matcher.py`, `typo_detector.py`, `po_parsing.py`, `sap_extract.py`,
  `scan_extractor.py`, `batch_processor.py`.
- Odpowiadające `templates/*.html` i testy z `tests/`.

## Plan implementacji — 3 fazy

### Faza 0 — Fork + archiwizacja (1 commit)
1. Utworzyć **nowe repo `artwork`** na GitHubie (`gh repo create`).
2. Lokalnie: skopiować obecne repo z historią (`git clone` / nowy remote), NIE fresh.
3. Rebrand: `README`, `CLAUDE.md`, `constants.APP_VERSION`, nazwa w `base.html`.
4. **Bramka:** apka boot-uje i `pytest tests/` zielone (jak dziś).

> Archiwizacja starego repo `compare` NIE tutaj — dopiero po Fazie 2, gdy Artwork
> stoi na nogach (patrz niżej). Do tego czasu `compare` zostaje aktywne jako backup.

### Faza 1 — Kasowanie modułów + szablonów + tras (bounded commity)
- Dla każdej grupy balastu: usuń moduł(y) → usuń ich bloki tras z `app.py` →
  usuń ich `templates/*.html` → usuń ich testy → `pytest tests/` + boot.
- Czerwone testy = cofnij ostatni blok, znajdź wspólny helper, popraw punktowo.
- Kolejność: najpierw najbardziej samodzielne (demurrage, alert_engine, cn_audit),
  potem portale, na końcu porównywarka dokumentów (najwięcej wspólnych helperów).

### Faza 2 — Odchudzenie `app.py` + nawigacja
- Wyczyść `base.html` z linków do usuniętych obszarów.
- Usuń role zewnętrzne (`FORWARDER_*`, `CUSTOMS_AGENT_*`, `EXTERNAL_ROLE_HOME`).
- Usuń nieużywane importy; `python -m compileall` + `pytest tests/` zielone.
- **Bramka końcowa:** apka wyłącznie artworkowa, boot OK, testy zielone.

### Faza 3 — Archiwizacja starego repo (dopiero gdy Artwork stabilne)
- Gdy Artwork boot-uje, testy zielone i realnie używany → stare repo `compare`
  na GitHubie ustawić na **Archive** (read-only). Wcześniej `compare` = backup.

## Ryzyka

- Faza 1 = gruby diff. Mitygacja: małe commity, boot-test po każdym.
- Wspólny helper transportowy używany przez artwork → nie kasować, przenieść do
  `core/` jeśli trzeba.
- CI (`auto-merge-claude.yml`) w nowym repo wskazuje inny remote/deploy — do
  poprawienia w Fazie 0 (albo wyłączyć auto-deploy do czasu stabilizacji).

## Definition of Done

- Nowe repo `artwork` na GitHubie, pełna historia; stare `compare` zarchiwizowane
  DOPIERO gdy Artwork stabilne (Faza 3).
- `pytest tests/` zielone, `compileall` czyste, apka boot-uje tylko z artworkiem.
- `base.html` bez martwych linków; brak importów wywalonych modułów.
