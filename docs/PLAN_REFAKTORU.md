# DocCompare — plan refaktoru (bez zmiany frameworka)

> Cel: zwiększyć stabilność, wydajność i utrzymywalność **bez** przepisywania na FastAPI/Django.
> Każdy krok jest mały, odwracalny i można go wdrażać niezależnie. Stan na 2026‑06‑16.

## Punkt wyjścia (fakty z kodu)
- `app.py` ≈ **20 000 linii**, **~358 endpointów**; `artwork_comparator.py` ≈ **7 500 linii** (PyTorch/CV).
- **Blueprinty już wystartowały** (`incoterms`, `translation_dict`, `transit`) — wzorzec sprawdzony.
- **Zadania w tle już istnieją**: tabela `async_jobs` + progres (`_set_progress`, polling), oparte na **wątkach** w procesie web (gunicorn gthread, 2× workery × 3 wątki) + semafor `_HEAVY_SEM` na ciężkie operacje, timeout 300 s.
- Integracja **Claude API** rozproszona po wielu modułach (`ai_validator`, `ai_learning`, `enhanced_comparator`, `artwork_comparator`…).
- Wąskie gardła: **CPU/ML/RAM** (PyTorch ~2 GB/worker) i **latencja Claude API** — *nie* I/O, więc async frameworka by nie pomógł.

---

## 1. Zadania w tle: z wątków na dedykowanego workera (RQ + Redis)
**Problem (stan obecny):** ciężkie operacje (PDF/OCR/porównania artworków) liczą się **w procesie web** na wątkach. To: (a) zjada RAM/CPU workerów obsługujących HTTP, (b) grozi timeoutami (300 s), (c) przy restarcie/deployu zadanie ginie. Semafor `_HEAVY_SEM` to obejście, nie rozwiązanie.

**Cel:** web tylko przyjmuje żądanie, zwraca `job_id` i jest natychmiast wolny; ciężką pracę robi **osobny proces worker**.

**Rozwiązanie:** wprowadzić **RQ (Redis Queue)** — lekkie, proste, idealne do CPU‑bound (Celery to overkill).
- Worker jako **osobny kontener/proces** (ten sam obraz, inny `command`: `rq worker`).
- **Zachować istniejący kontrakt API** `async_jobs`/progres (front już odpytuje) — zmienia się tylko „silnik" wykonania: zamiast `threading.Thread` → `queue.enqueue(...)`, a worker aktualizuje `async_jobs`.

**Kroki:**
1. Dodać Redis (w Coolify jako zasób) + `rq` do `requirements.txt`.
2. `jobs.py`: definicje funkcji‑zadań (porównanie dokumentów, porównanie artworku, ekstrakcja PDF) wołające istniejącą logikę.
3. Endpointy ciężkie: zamiast liczyć inline → `enqueue` + zwróć `job_id` (jak już robi `async_jobs`).
4. Worker w osobnym procesie aktualizuje `async_jobs` (status/pct/result).
5. Usunąć `_HEAVY_SEM` (kolejka sama ogranicza współbieżność: liczbą workerów).

**Koszt/ryzyko:** średni; ryzyko niskie (kontrakt frontu bez zmian, można migrować endpoint po endpoincie). **Priorytet: WYSOKI.**
**Efekt mierzalny:** brak timeoutów na web, stabilne RAM workerów web, równoległość sterowana liczbą workerów.

---

## 2. Wydzielenie ciężkiego ML/CV do osobnego serwisu
**Problem:** `artwork_comparator.py` (7,5 tys. linii) ładuje modele PyTorch (LightGlue, GroundingDINO, DocTR, Table Transformer) — **~2 GB RAM na workera**. Trzyma to cały obraz „ciężkim" i wymusza duże maszyny dla całej apki.

**Cel:** web + logika biznesowa lekkie; modele żyją w jednym, skalowanym niezależnie miejscu.

**Rozwiązanie:** mikroserwis **`artwork-engine`** (osobny kontener) z jednym endpointem `POST /compare` (wzorzec + fabryczny → wynik). Komunikacja HTTP wewnętrzna (albo przez tę samą kolejkę RQ).
- Modele ładowane **raz** w serwisie ML (nie w każdym workerze web).
- Web/worker wołają serwis ML zamiast importować PyTorch.

**Kroki:**
1. Owinąć `artwork_comparator.compare_artworks` w cienki serwis (Flask/uvicorn — tu akurat FastAPI **ma sens dla jednego serwisu**, ale to opcjonalne).
2. Wyciąć ciężkie zależności (`torch`, `timm`, DocTR, GroundingDINO) z obrazu głównego → tylko serwis ML je ma.
3. W głównej apce: klient HTTP do serwisu ML.

**Koszt/ryzyko:** duży (rozdział zależności, infrastruktura 2 kontenerów). **Priorytet: ŚREDNI** (robić po #1).
**Efekt:** obraz web lżejszy i szybszy w buildzie (mniej `pip`/`git+https`), tańsze skalowanie, mniejsza powierzchnia awarii buildów.

---

## 3. Trwały storage + backup (domknięcie tematu)
**Problem:** `UPLOAD_FOLDER` bywa efemeryczny; jedyne źródło prawdy danych to baza. (Mastery artworków już przeniesione do PostgreSQL.)

**Cel:** żaden plik ani dane nie giną przy redeployu; jest sprawdzony backup.

**Rozwiązanie/Kroki:**
1. Trwały wolumen `/data` w Coolify **albo** dalsze trzymanie binariów w PG (zrobione dla masterów).
2. **Backup PostgreSQL** w Coolify (harmonogram) + **test odtworzenia** (raz, udokumentowany).
3. Wskaźnik wolnego miejsca w `/admin` (już dodany) — pilnować budżetów.

**Koszt/ryzyko:** mały; głównie infrastruktura. **Priorytet: WYSOKI** (ochrona danych).

---

## 4. Rozbicie `app.py` na Blueprinty (kontynuacja)
**Problem:** monolit 20 tys. linii — trudny w nawigacji, ryzyko kolizji zmian, wolniejsze code‑review.

**Cel:** moduły domenowe ~500–1500 linii każdy; trasy zgrupowane tematycznie.

**Rozwiązanie:** kontynuować istniejący wzorzec blueprintów. Wydzielać **po jednej domenie naraz** (czysto mechaniczne przeniesienie tras, bez zmiany logiki):
- `auth_bp` (login/logout/reset hasła), `compare_bp` (porównania dokumentów), `artwork_bp` (artwork/index/profiles/master‑files), `dostawy_bp` (dostawy + analityka), `warehouse_bp`, `transport_bp`, `suppliers_bp`, `admin_bp`, `api_bp` (eksport/itp.).

**Kroki (na każdą domenę):**
1. Nowy plik `blueprints/<domena>.py`, `bp = Blueprint(...)`.
2. Przenieść trasy (wytnij/wklej), podmienić `@app.route` → `@bp.route`.
3. `app.register_blueprint(bp)`; uruchomić testy (bramka CI) + ręczny smoke.

**Koszt/ryzyko:** średni rozłożony; ryzyko niskie (mechaniczne, weryfikowalne). **Priorytet: ŚREDNI**, ale **wysoka stopa zwrotu** w utrzymaniu. Robić przyrostowo.

---

## 5. Walidacja wejścia (Pydantic/marshmallow — wewnątrz Flaska)
**Problem:** żądania API parsowane ręcznie (`request.get_json()`, `data.get(...)`), walidacja rozproszona/niespójna.

**Cel:** spójna walidacja i czytelne błędy 400; mniej „cichych" błędów typu.

**Rozwiązanie:** **Pydantic v2** do modeli wejścia w endpointach API (bez zmiany frameworka). Dekorator `@validate_body(Model)` parsujący i zwracający 400 z opisem.

**Kroki:** zacząć od najbardziej „wejściowych" tras (import danych, tworzenie dostaw, mapowania), potem reszta. Modele obok blueprintów.

**Koszt/ryzyko:** mały‑średni, przyrostowy. **Priorytet: ŚREDNI.**
**Efekt:** mniej błędów 500 na złych danych, samodokumentujące się API.

---

## 6. Równoległe wywołania Claude API (gdzie niezależne)
**Problem:** część scenariuszy woła Claude **sekwencyjnie** (np. ocena wielu pozycji/par), a to czysta latencja sieci.

**Cel:** skrócić czas operacji wielokrotnych wywołań.

**Rozwiązanie:** `concurrent.futures.ThreadPoolExecutor` (klient Anthropic jest I/O‑bound → wątki działają) dla niezależnych wywołań; limit współbieżności + istniejący rate‑limiter (`api_rate_events`).

**Kroki:** zidentyfikować pętle wołające Claude po kolei (`ai_validator`, `ai_learning`) → zrównoleglić z pulą 3–5; uważać na limity API i koszt (licznik `api_usage` już jest).

**Koszt/ryzyko:** mały. **Priorytet: NISKI‑ŚREDNI** (szybki efekt tam, gdzie są pętle).

---

## Czego ŚWIADOMIE nie robimy
- **Przepisania na FastAPI/Django** — wąskie gardła (CPU/ML/RAM, koszt API) **pozostałyby te same**, a koszt i ryzyko regresji ogromne. Async nie przyspiesza zadań CPU‑bound. (Wyjątek: *nowy* serwis ML — patrz #2 — może użyć FastAPI lokalnie, bez ruszania reszty.)
- **Wprowadzania ORM** — `db.py` (surowy SQL, dual SQLite/PG) działa; ORM = duży przepis bez korzyści.

---

## Sugerowana kolejność (roadmap)
| Faza | Co | Priorytet | Dlaczego teraz |
|---|---|---|---|
| **0** | Backup PG + trwały storage (#3) | WYSOKI | Ochrona danych — fundament. |
| **1** | Kolejka RQ + worker (#1) | WYSOKI | Usuwa timeouty i obciążenie web; bazuje na istniejącym `async_jobs`. |
| **2** | Blueprinty — 2–3 domeny (#4) | ŚREDNI | Utrzymywalność; niskie ryzyko, kontynuacja wzorca. |
| **3** | Zrównoleglenie Claude (#6) | ŚREDNI | Szybki zysk na latencji. |
| **4** | Walidacja Pydantic na kluczowych trasach (#5) | ŚREDNI | Mniej błędów na wejściu. |
| **5** | Wydzielenie serwisu ML (#2) | ŚREDNI | Lekki web, tańsze skalowanie — największa zmiana, na końcu. |

**Zasada:** każdy krok osobno, za bramką testów (CI już jest), z możliwością cofnięcia. Żaden nie wymaga „wielkiego przepisania".
