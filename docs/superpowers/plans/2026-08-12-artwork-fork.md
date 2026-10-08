# Artwork Fork — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wydzielić nowe repo `artwork` (fork z historią DocCompare v6), odchudzone do 100% pracy artworkowej; stare repo archiwizować dopiero gdy Artwork stabilne.

**Architecture:** Faza 0 tworzy nowe repo z pełną historią git i rebrandem, boot+testy zielone. Fazy 1–2 to mechaniczne pętle usuwania balastu (transport/spedycja/agencja/porównywarka dokumentów), każde usunięcie weryfikowane przez `test_app_boots` + `pytest tests/`. Faza 3 archiwizuje stare repo.

**Tech Stack:** Python 3, Flask (monolit `app.py`), pytest, git, GitHub CLI (`gh`), Windows/PowerShell + Bash.

## Global Constraints

- Nazwa projektu: **Artwork**; repo GitHub: `artwork` (NIE „AWS" — kolizja z Amazon Web Services).
- Fork z **pełną historią git** (nie fresh init).
- Po KAŻDYM usunięciu w Fazach 1–2: `python -m pytest tests/ -q` musi być zielone (w tym `tests/test_app_boots.py`) oraz `python -m compileall .` czyste. Czerwone = cofnij ostatni krok.
- Zostają: artwork core (10 modułów) + infra + 4 szare (material_master+uom, supplier_master+profiles, ai_validator+ai_learning, api_usage_tracker).
- Wychodzą: transport/kolejka, spedycja (forwarder), agencja celna (customs_agent), porównywarka dokumentów handlowych.
- Nowa lokalizacja repo lokalnie: `C:\Users\tomas\PycharmProjects\artwork`.
- Źródło forka: gałąź `main` repo `compare`.
- Stare repo `compare` NIE archiwizowane do Fazy 3.
- Deploy do Coolify (stary UUID `<UUID-aplikacji>`) MUSI być wyłączony w nowym repo w Fazie 0.

---

## FAZA 0 — Fork + rebrand (repo boot-uje, testy zielone)

### Task 0.1: Utworzenie lokalnego klonu z historią i nowego repo GitHub

**Files:**
- Create: katalog `C:\Users\tomas\PycharmProjects\artwork` (klon z historią)

**Interfaces:**
- Produces: nowe repo repo docelowe na GitHubie z pełną historią gałęzi `main`; lokalny katalog `artwork` z `origin` wskazującym nowe repo.

- [ ] **Step 1: Sklonuj `compare` (gałąź main) do nowego katalogu z historią**

```bash
cd "C:\Users\tomas\PycharmProjects"
git clone --branch main --single-branch "C:\Users\tomas\PycharmProjects\compare" artwork
```

- [ ] **Step 2: Odłącz stary origin**

```bash
cd "C:\Users\tomas\PycharmProjects\artwork"
git remote remove origin
```

- [ ] **Step 3: Utwórz repo na GitHubie i wypchnij (prywatne, z historią)**

```bash
cd "C:\Users\tomas\PycharmProjects\artwork"
gh repo create artwork --private --source=. --remote=origin --push
```

Expected: `gh` tworzy repo docelowe, pushuje `main`, ustawia `origin`.

- [ ] **Step 4: Weryfikacja**

```bash
cd "C:\Users\tomas\PycharmProjects\artwork"
git remote -v
git log --oneline -3
```

Expected: `origin` = repo docelowe; historia commitów obecna (ten sam top-commit co `main` w compare).

> UWAGA: cała reszta planu (Task 0.2+ i Fazy 1–3) wykonywana jest w katalogu
> `C:\Users\tomas\PycharmProjects\artwork`, NIE w `compare`.

### Task 0.2: Rebrand nazwy i wersji

**Files:**
- Modify: `constants.py:91`
- Modify: `templates/base.html:7,11,97,325`
- Modify: `README.md` (nagłówek/tytuł), `CLAUDE.md` (nagłówek Project Overview)

**Interfaces:**
- Consumes: nic (pierwsza edycja w nowym repo).
- Produces: aplikacja prezentuje nazwę „Artwork"; `constants.APP_VERSION` bumped.

- [ ] **Step 1: Zbumpuj wersję i nazwę w `constants.py`**

Zmień linię 91:

```python
APP_VERSION = "1.0.0-artwork"
```

- [ ] **Step 2: Podmień brand w `templates/base.html`**

Linia 7:
```html
<meta name="description" content="Artwork — porównywanie i wizualizacja artworków opakowań ACME">
```
Linia 11:
```html
<title>{% block title %}Artwork{% endblock %} — ACME</title>
```
Linia 97:
```html
        <div class="text-[11px] font-medium" style="color:var(--nav-muted);">Artwork&nbsp;v1</div>
```
Linia 325:
```html
        <span class="text-[15px] font-semibold text-slate-900">{% block page_title %}Artwork{% endblock %}</span>
```

- [ ] **Step 3: Zaktualizuj nagłówek `README.md` i `CLAUDE.md`**

W obu plikach zamień nagłówkowy opis produktu na jedno zdanie:
> **Artwork** — aplikacja Flask do porównywania artworków/opakowań, walidacji kodów kreskowych, indeksu masterów i wizualizacji 3D opakowań (ACME). Wydzielona z DocCompare v6.

(Zostaw resztę CLAUDE.md — architektura infra/artwork nadal aktualna; sekcje transportu poprawimy w Fazie 2.)

- [ ] **Step 4: Boot + testy**

```bash
cd "C:\Users\tomas\PycharmProjects\artwork"
python -m compileall constants.py
python -m pytest tests/ -q
```

Expected: compileall bez błędów; testy zielone (bez zmian ilości — to tylko rebrand).

- [ ] **Step 5: Commit**

```bash
git add constants.py templates/base.html README.md CLAUDE.md
git commit -m "Rebrand DocCompare -> Artwork (nazwa, wersja, opis)"
```

### Task 0.3: Wyłączenie starego deployu Coolify w CI

**Files:**
- Modify: `.github/workflows/auto-merge-claude.yml` (usuń krok „Deploy na Coolify", linie ~72–77)
- Modify: `.github/workflows/ml-base-image.yml` — sprawdź czy pushuje do GHCR starego repo; jeśli tak, wyłącz trigger (patrz Step 2)

**Interfaces:**
- Consumes: nic.
- Produces: CI nowego repo uruchamia testy i auto-merge `claude/**`→`main`, ale NIE deployuje na stary Coolify UUID.

- [ ] **Step 1: Usuń krok deployu z `auto-merge-claude.yml`**

Usuń cały krok:
```yaml
      - name: Deploy na Coolify
        run: |
          curl -s -o /dev/null -w "%{http_code}" \
            ...
            "http://203.0.113.10:8000/api/v1/deploy?uuid=<UUID-aplikacji>&force=false"
```
Zamień na komentarz-placeholder:
```yaml
      # Deploy wyłączony do czasu ustawienia targetu deployu dla repo `artwork`.
      # TODO(faza-3+): dodać nowy Coolify/Render target.
```

- [ ] **Step 2: Sprawdź `ml-base-image.yml`**

```bash
cd "C:\Users\tomas\PycharmProjects\artwork"
grep -n "ghcr.io\|on:\|push:\|schedule:" .github/workflows/ml-base-image.yml
```

Jeśli obraz jest tagowany nazwą `compare`/starego repo LUB pushowany na push — zmień `on:` na `workflow_dispatch:` (ręczne), by nie budować obrazu ML w nowym repo automatycznie:

```yaml
on:
  workflow_dispatch:
```

- [ ] **Step 3: Commit + push (uruchom CI nowego repo)**

```bash
git add .github/workflows/
git commit -m "CI: wyłącz deploy Coolify + auto build ML w repo artwork"
git push origin main
```

- [ ] **Step 4: Weryfikacja CI**

```bash
gh run list --limit 3
```

Expected: run `ci.yml` na `main` przechodzi (compileall + pytest zielone). Brak akcji deployu.

**BRAMKA FAZY 0:** nowe repo istnieje z historią, boot-uje, `pytest tests/` zielone, CI zielone, deploy wyłączony. Stare `compare` nietknięte (backup).

---

## FAZA 1 — Playbook usuwania balastu (pętla mechaniczna)

> To NIE jest praca TDD (brak nowego kodu). To powtarzalna pętla usuwania.
> Każda grupa = osobny commit. Kolejność: od najbardziej samodzielnych do
> najbardziej wplecionych (porównywarka dokumentów na końcu).

**Pętla dla każdej grupy G (jeden commit):**

- [ ] **1. Usuń moduły grupy G** (pliki `.py` z listy poniżej).
- [ ] **2. Znajdź i usuń ich trasy w `app.py`:** `grep -n "def <nazwa>\|url_for('<endpoint>'\|@app.route" ` — usuń bloki `@app.route(...) def ...:` należące do G oraz nieużywane importy modułów G na górze `app.py`.
- [ ] **3. Usuń szablony G** z `templates/` (te renderowane tylko przez usunięte trasy).
- [ ] **4. Usuń testy G** z `tests/`.
- [ ] **5. Weryfikuj:** `python -m compileall app.py` → `python -m pytest tests/ -q`.
  - Zielone → commit: `git commit -am "Usuń <grupa>: moduł+trasy+szablony+testy"`.
  - Czerwone (najczęściej `test_app_boots` — brakujący helper/import) → `git checkout -- app.py` dla ostatniego bloku, zidentyfikuj wspólny helper. Jeśli helper używany też przez artwork → NIE kasuj, przenieś do `core/` i zaktualizuj import. Powtórz.

**Grupy (kolejność):**

1. **Demurrage/detention:** `demurrage.py` + trasy `/demurrage*`.
2. **Alerty:** `alert_engine.py` + konfiguracja alertów.
3. **Audyt CN:** `cn_audit.py` + trasy audytu celnego.
4. **Magazyn:** `warehouse_stock.py`, `intake_queue` + trasy.
5. **Kolejka/transport:** `kolejka_*`, `zlecenia_*`, `delivery_workflow.py` + trasy `/kolejka`, `/transport`, `/zlecenia`, `/delivery`, `/intake`.
6. **Portal spedycji:** trasy `/spedycja`, `/forwarder`, szablony forwarder.
7. **Portal agencji celnej:** trasy `/agencja`, `/customs`, szablony customs.
8. **Porównywarka dokumentów (ostatnia — najwięcej wspólnych helperów):**
   `enhanced_comparator.py`, `comparator.py`, `semantic_matcher.py`,
   `typo_detector.py`, `po_parsing.py`, `sap_extract.py`, `scan_extractor.py`,
   `batch_processor.py` + trasy porównania PO/PI/CI/PL/SAD + ich szablony.
   > UWAGA: `normalizer.py`, `uom.py`, `table_extractor.py`, `pdf_extractor.py`
   > ZOSTAJĄ (używa ich artwork). Kasuj tylko moduły z listy.

---

## FAZA 2 — Odchudzenie app.py + nawigacja

- [ ] **Task 2.1:** Wyczyść `templates/base.html` z linków nawigacyjnych do usuniętych obszarów (transport, spedycja, agencja, porównywarka dokumentów). Zostaw: artwork, biblioteka masterów, 3D, master data, auth. Weryfikacja: `python -m pytest tests/ -q` + ręczny boot `python app.py` (strona ładuje się bez martwych `url_for`).
- [ ] **Task 2.2:** Usuń role zewnętrzne z kodu: `FORWARDER_ALLOWED_PREFIXES`, `CUSTOMS_AGENT_ALLOWED_PREFIXES`, `EXTERNAL_ROLE_HOME` i logikę przekierowań ról `forwarder`/`customs_agent` (grep tych stałych w `app.py`/`core/security.py`). Weryfikacja: `pytest tests/ -q` zielone.
- [ ] **Task 2.3:** Usuń nieużywane importy na górze `app.py` (pozostałości po usuniętych modułach): `python -m pyflakes app.py` lub `ruff check app.py --select F401`. Usuń zgłoszone F401 należące do usuniętych modułów. Weryfikacja: `python -m compileall .` + `pytest tests/ -q`.
- [ ] **Task 2.4:** Zaktualizuj `CLAUDE.md` — usuń sekcje opisujące transport/spedycję/agencję/porównywarkę dokumentów (Architecture → Transport/Logistics Domain, portale). Zostaw artwork + infra. Commit.

**BRAMKA FAZY 2:** apka wyłącznie artworkowa, `compileall` czyste, `pytest tests/` zielone, `base.html` bez martwych linków.

---

## FAZA 3 — Archiwizacja starego repo (dopiero gdy Artwork stabilne)

- [ ] **Task 3.1:** Gdy Artwork realnie używany (boot OK, testy zielone, praca na nim trwa) — zarchiwizuj stare repo:

```bash
gh repo archive <repo-źródłowe> --yes
```

Expected: `compare` ustawione jako read-only. (Do tego momentu `compare` = aktywny backup.)

---

## Self-Review

- **Spec coverage:** Faza 0 (fork+rebrand+CI) → Task 0.1–0.3. Faza 1 (kasowanie) → playbook grup 1–8. Faza 2 (odchudzenie app.py/nav) → Task 2.1–2.4. Faza 3 (archiwizacja) → Task 3.1. Wszystkie sekcje spec pokryte.
- **Placeholder scan:** brak „TBD"/„implement later" w krokach kodu; jedyne `TODO` to świadomy komentarz o przyszłym targecie deployu (Faza 3+), zgodny ze spec.
- **Type consistency:** brak nowych typów/sygnatur (praca to usuwanie + rebrand stringów); nazwy stałych (`FORWARDER_ALLOWED_PREFIXES`, `EXTERNAL_ROLE_HOME`, `APP_VERSION`) zgodne ze źródłem.
