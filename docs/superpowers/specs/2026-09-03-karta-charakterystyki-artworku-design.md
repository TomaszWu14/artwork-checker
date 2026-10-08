# Karta charakterystyki artworku — dokument docelowy

**Data:** 2026-09-03
**Cel:** jeden autorytatywny rekord na **REF** — komplet danych, których grafik
potrzebuje do analizy/tworzenia artworku, a system do **masowej kontroli jakości**
(zestawianie przychodzącego pliku z wartościami oczekiwanymi).
**Status:** ustalenie (dokument tekstowy). Definiuje kontrakt pól i źródła danych.
Nie zmienia jeszcze kodu — decyzja „widok read-only czy tabela" zależy od tego dokumentu.
**Powiązanie:** rozszerza [`2026-09-02-konwencja-nazewnictwa-artworkow-design.md`](2026-09-02-konwencja-nazewnictwa-artworkow-design.md).
Nazwa pliku to **wskaźnik** do tego rekordu (pole #1 nazwy `REF` = klucz karty).

---

## 0. Zasada nadrzędna: nazwa ≠ karta

Nazwa pliku jest **identyfikatorem** (rozpoznanie i sortowanie). Karta jest
**rekordem prawdy** (pełne dane). Nie konkurują — karta zdejmuje z nazwy presję
niesienia wszystkiego (np. kompletu EAN-ów per poziom, którego nazwa unieść nie może,
por. §6 konwencji nazewnictwa).

Wzorzec z REACH bierzemy jako **koncept, nie formę**: jeden arkusz na wyrób,
wersjonowany, o **zamkniętym zestawie pól**. Nie kopiujemy 16 sekcji SDS.

## 1. Granularność

**Jedna karta = jeden REF.** W środku:
- dane wspólne wyrobu (raz),
- sekcja **per poziom opakowania** (słownik §3 konwencji),
- **historia rewizji** (wszystkie Rev.NN),
- **kontrakt wartości oczekiwanych** do QA.

Klucz karty = `REF`. Rewizja obowiązująca jest polem karty (nie osobnym rekordem);
poprzednie rewizje żyją w sekcji historii.

### 1a. Rekord per REF, ale ARKUSZ per poziom (prezentacja i OCR)

Rekord danych jest jeden na REF (§1). **Prezentacja i wydruk** jest jednak
rozbita — **każdy poziom opakowania to osobny arkusz** z jednoznacznym nagłówkiem:

```
[ REF 805016 · elastoDERM F-IV ]  ·  POZIOM: box  ·  EAN 5900010800827  ·  Rev.02
```

Powód: **OCR ma wtedy łatwą, jednoznaczną robotę** — jeden arkusz = jeden poziom =
jeden komplet danych do odczytu, bez mieszania kodów/wymiarów różnych poziomów na
jednej stronie. Nagłówek (artykuł + poziom) jest stały i na górze, więc odczyt
maszynowy wie od razu, czego dotyczą dane poniżej. To reguła prezentacji, nie zmiana
modelu — dane nadal agregują się pod jednym REF (§2).

## 2. Karta to AGREGAT, nie nowy magazyn

Każde pole ma **źródło prawdy**. Karta w pierwszym kroku jest **widokiem/agregatem**
nad istniejącymi tabelami i nad samym artworkiem — nie drugą kopią danych do ręcznego
utrzymywania. Ręcznie zatwierdzamy tylko to, czego w źródłach nie ma (§9).

## 3. Pola karty — kontrakt i źródła

### 3.1 Dane wspólne wyrobu (raz na REF)

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| REF (klucz) | kod REF | `material_master` |
| Rodzaj wyrobu | tekst | `material_master.family` |
| Rodzina / marka | tekst | `material_master` |
| Marka handlowa | tekst (opcj.) | `material_master` / pole §6a nazwy |
| Klasa wyrobu med. | enum + `sterylny?` | `material_master` (nowe pole) — patrz §9 |
| Wariant / rozmiar | tekst | `material_master` |
| Dostawca (fabryka) | nazwa | `supplier_master` |
| Kod producenta | kod | `supplier_master` / `material_master.producer_codes` |

### 3.2 Ikonografia obowiązkowa

Zamknięta lista symboli **ISO 15223-1** wymaganych dla wyrobu. Przypięta
**per poziom opakowania** (pouch ≠ box mogą mieć różne zestawy — patrz §5),
nie globalnie do REF.

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| Wymagane symbole (per poziom) | lista kodów ISO (np. `5.1.1`, `5.1.5`, `5.1.4`, `5.4.2`, `5.2.3`, `5.1.6`, `5.4.3`) | **ręczne zatwierdzenie** (§9) — słownik symboli jak §3 konwencji |
| Znak CE + numer jedn. notyf. | `CE ####` lub „brak" | `material_master` (klasa) + ręczne |

> Symbole to obraz — karta trzyma **kody** (co ma być), nie same grafiki.
> Renderowanie ikon jest po stronie widoku (SVG), tak jak w makiecie.

### 3.3 Poziomy opakowania i wymiary (sekcja per poziom)

Dla **każdego** poziomu z konwencji (`pouch`, `box`, `carton`, `carton_sticker`…):

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| Poziom | słowo ze słownika §3 konwencji | — |
| Rola poziomu | np. „jednostka handlowa", „zbiorcze ×N" | `material_master` (per-level) |
| EAN poziomu | 13 cyfr / `NO-EAN` | **tabelka wewnątrz artworka** (§6 konwencji), lustrzane w `material_master` |
| Krotność | liczba (ile niższych sztuk) | `material_master` / `uom_conversion` |
| Format netto (S × W) | mm | `material_master` (per-level dims) |
| Głębokość (G/D) | mm | `material_master` |
| Spad (bleed) | mm/bok | ręczne (spec druku) |
| Bezpieczny margines | mm | ręczne |
| Min. wysokość fontu regulacyjnego | mm / pt | ręczne (wymóg prawny) |
| Typ kodu (jeśli nie EAN) | np. `GS1-128 (01)(10)(17)` | `barcode_validator` / `barcode_report` |

> **Uwaga na wiele EAN-ów na poziomie** (sztuka vs multipak): karta trzyma komplet
> per poziom — czego nazwa pliku nieść nie musi.

### 3.4 Kolor / materiał / parametry druku (raz na REF, ew. per poziom)

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| Paleta (zamknięta) | lista Pantone/CMYK | ręczne (spec druku) |
| Profil ICC | np. `ISO Coated v2` | ręczne |
| Podłoże (per poziom) | materiał + gramatura/µm | ręczne / `supplier_master` |
| Wykończenie | mat/UV/… | ręczne |
| Format pliku | np. `PDF/X-4` | ręczne (stała firmowa) |
| Min. rozdzielczość | dpi | ręczne (stała firmowa) |

### 3.5 Nazwy i tłumaczenia

Jedno miejsce prawdy dla treści tekstowej — grafik **wkleja stąd, nie tłumaczy sam**.

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| Nazwa PL | tekst | `material_master` |
| Nazwa EN | tekst | `material_master` |
| Nazwa DE / FR / … | tekst per język rynkowy | `blueprints/translation_dict` |
| Nazwa u wytwórcy (fabryka) | tekst | `supplier_master` |
| Synonimy historyczne | lista (np. nazwa przed rebrandem) | `supplier_profiles` (product synonyms) |

### 3.6 Historia rewizji

| Pole | Format | Źródło prawdy |
|------|--------|---------------|
| Rev.NN | dwie cyfry | nazwa pliku / `artwork_index` |
| Data zatwierdzenia | `DD-MM-RRRR` | nazwa pliku (§8 konwencji) / ręczne |
| Opis zmiany | tekst | ręczne (przy zatwierdzeniu rewizji) |
| Rewizja obowiązująca | wskaźnik na jedną Rev.NN | `artwork_naming` (ranking rewizji) |

## 4. Kontrakt wartości oczekiwanych (QA)

To „prawa strona" porównania. Przychodzący artwork (od dostawcy albo nowa rewizja)
jest zestawiany **z kartą**, nie z pamięcią człowieka. Pola oznaczone jako
*oczekiwane* wchodzą do automatycznego porównania:

| Sprawdzenie | Oczekiwane z | Wynik |
|-------------|--------------|-------|
| EAN każdego poziomu | §3.3 | suma kontrolna EAN-13 + zgodność z kartą (`barcode_validator`) |
| Rewizja w nazwie = obowiązująca | §3.6 | zgodne / nieaktualne |
| Wymiary netto per poziom | §3.3 | w tolerancji ± (do ustalenia) |
| Komplet piktogramów per poziom | §3.2 | N/N obecnych |
| Znak CE + jednostka | §3.2 | obecny na wymaganych poziomach |
| **Tekst jako grafika** (§4a) | warstwa tekstowa PDF | napisy widoczne, których NIE ma w warstwie tekstu → błąd do poprawy |

## 4a. Wykrywanie „tekst jako grafika" (błąd krytyczny)

Napis wklejony na artwork **jako grafika** (zrasteryzowany / zamieniony na krzywe)
zamiast żywego tekstu to realny błąd: nie da się go poprawić, przetłumaczyć ani
sprawdzić literówek, a OCR/QA łatwo go przeoczy. Karta wyłapuje to automatycznie.

**Zaimplementowane:** `artwork_text_layer.py` — zestawia boxy OCR (co widać) z boxami
warstwy tekstowej PDF (co jest tekstem). Napis widoczny bez odpowiednika w warstwie
tekstu → zgłoszony jako kandydat na „tekst-grafikę" (sortowane wg pokrycia; `cover≈0`
= najpewniejsze). Rdzeń jest czysty i przetestowany (`tests/test_text_layer.py`);
ekstrakcja z PDF lazy-importuje PyMuPDF (reuse `page.get_text` — CI bez ML zostaje szybkie).

Wynik ląduje w sekcji zgłoszeń poziomu (`artwork_card_issue`, §8a.3) jako pozycja
do poprawy, z zaznaczoną strefą na dielinie.

Status z ostatniej kontroli: `zgodne` / `do weryfikacji` / `niezgodność krytyczna`.

## 5. Zasady twarde

- **Ikonografia per poziom, nie per REF.** pouch (jednostka handlowa) i box
  (zbiorcze) mają różne wymogi symboli — trzymamy je w sekcji poziomu.
- **EAN zawsze z tabelki artworka**, lustrzany w `material_master` — nigdy z pamięci
  (to samo źródło co §6 konwencji nazewnictwa).
- **Karta trzyma KODY symboli, nie grafiki** — obrazy renderuje widok.
- **Brak EAN → `NO-EAN`**, nigdy ciąg zer (§4 konwencji).
- **Jeden REF = jedna karta.** Nowy rozmiar nie tworzy nowej karty, jeśli dzieli REF;
  jeśli ma własny REF → własna karta (spójne z regułą rewizji §7 konwencji).

## 6. Przykład

Wizualna makieta (poglądowa, dane wymyślone): REF 805016 „elastoDERM F-IV" —
patrz artefakt HTML z 2026-09-03 (7 sekcji: identyfikacja, ikonografia, poziomy+wymiary,
kolor/druk, tłumaczenia, kontrakt QA, historia rewizji).

## 7. Świadome uproszczenia / poza zakresem

- **Start jako widok read-only.** Zanim powstanie tabela `artwork_card`, karta może
  być składana w locie z istniejących źródeł (§2). Osobną tabelę dokładamy tylko dla
  pól bez źródła (§9) i dla cache'u kontraktu QA — jako oddzielny, wdrożeniowy krok.
- **Dielines/wykrojniki** to załącznik, nie pole karty (istnieje już `artwork_3d`
  czytający panele z crease-lines — karta może tylko linkować).
- **Renderowanie ikon ISO** (SVG) jest po stronie widoku; karta = kody.
- **Egzekwowanie QA przy imporcie** (auto-porównanie plik ↔ karta) to osobne zadanie —
  ten dokument tylko definiuje, które pola są „oczekiwane".

## 8a. Moduł „Karta produktu artworkowego" (widok 3D + dieline + zgłoszenia)

Rozszerzenie karty (§0–§7) do pełnego **modułu w aplikacji**: karta produktu z
podglądem 3D per poziom, płaskim dielinem z możliwością zgłaszania błędów, oraz
eksportem PDF ze skosem. Zasada nadrzędna bez zmian: **maksymalny reuse istniejącego
pipeline'u 3D**, nie budowa od zera.

### 8a.1 Co już istnieje (reużywamy, nie piszemy)

| Potrzeba | Istniejące w kodzie |
|----------|---------------------|
| GLB z dielineu (panele z crease-lines, tekstury 6 ścian) | `artwork_3d.py`: `extract_dieline_info` → `locate_panels` → `render_panels` → `build_glb` |
| Statyczny podgląd PNG modelu | `_artwork_3d_worker` (produkuje GLB **i** PNG) |
| Podgląd 3D w przeglądarce | `<model-viewer>` (zvendorowany, `static/vendor/model-viewer/`) |
| Nauka + akceptacja orientacji | `/api/artwork/3d/accept`, `_a3d_learned`, `_a3d_accepted` |
| Box 3D z wymiarów bez dielineu | `/artwork/3d/marm` (z MARM / `material_uom_dims`) |
| Masowe generowanie z biblioteki | `/api/artwork/3d/batch/from-library` |
| Strefy zaznaczane przez użytkownika | `artwork_zone_comparator.py` (baza pod zgłoszenia błędów) |

### 8a.2 Widok 3D per poziom — reguła „gdzie się da"

Każdy poziom opakowania (§3.3) dostaje **jeden z trzech** rodzajów podglądu,
degradacja w dół:

1. **GLB (pełne 3D)** — poziom z **dielinem** (`box`, `carton`, `pouch` z wykrojnikiem).
   Generowany przez `build_glb` przy tworzeniu artworku wykrojnikowego — „każdy od
   razu to widzi". To ścieżka domyślna dla opakowań kartonowych.
2. **Box z wymiarów** — poziom bez dielineu, ale ze znanymi W×H×D (§3.3) →
   `/artwork/3d/marm`-owy prostopadłościan (bez tekstur paneli lub z płaskim frontem).
3. **Zdjęcie/grafika poglądowa** — poziom, którego nie da się wyrenderować
   (płaska rękawica, sztuka bez bryły). Pole obrazka wgrywane ręcznie lub linkowane;
   **jawnie oznaczone jako poglądowe**, nie mylić z artworkiem.

> Karta trzyma dla każdego poziomu: `preview_kind` ∈ {`glb`,`box`,`image`} + wskaźnik
> do artefaktu (ścieżka GLB / dims / plik obrazka). Renderowanie po stronie widoku.

### 8a.3 Dieline (płasko do wykrojnika) + zgłaszanie błędów

Obok 3D — **płaski widok dielineu** (to, co realnie idzie na wykrojnik). Na nim:

- **dostawca** może zgłosić błąd (zaznaczyć strefę + komentarz),
- **my** możemy nanieść poprawkę naszego artworku (ta sama mechanika stref).

Reuse: mechanika zaznaczania stref z `artwork_zone_comparator.py`. Nowy, mały rekord:

| Tabela `artwork_card_issue` (nowa) | Pole |
|---|---|
| REF + poziom | do czego się odnosi |
| strefa | bbox na dielinie (jak zone comparator) |
| autor + rola | dostawca (external) / my (internal) — gate przez `core.security` |
| treść | opis błędu / propozycja poprawki |
| status | `zgłoszone` → `w poprawie` → `zamknięte` |
| rewizja | której Rev.NN dotyczy |

> Dostawcy mają role **external** (`forwarder`/`customs_agent` level 0) — jeśli
> zgłaszanie ma być przez portal dostawcy, trzeba świadomie zdecydować o dostępie
> (osobny prefix/portal), a nie otwierać wnętrza aplikacji. **Do potwierdzenia.**

### 8a.4 Eksport PDF ze skosem (podgląd izometryczny)

Podgląd 3D w aplikacji jest interaktywny (`<model-viewer>`), ale karta musi się też
**wydrukować**. Do PDF idzie **statyczny render izometryczny (skos)** każdego poziomu:

- **Lazy path:** ustawić kamerę izometryczną i wyrenderować GLB do PNG tym samym
  kodem, który już robi PNG w `_artwork_3d_worker` (trimesh scene → `save_image`
  z kątem ~30°/45°), zamiast dokładać nowy renderer.
- PNG-i skosów + dane karty składane do PDF istniejącym mechanizmem eksportu
  (fonty DejaVu/Liberation dla ą/ę/ó — jak reszta eksportów, patrz Konwencje).
- Fallback dla poziomów `image`: wstawiamy grafikę poglądową (bez skosu).

### 8a.5 Moduł „kreator karty" w aplikacji

Ekran do **tworzenia/edycji** karty produktu artworkowego:

- wczytuje agregat (§2–§3) dla danego REF, pokazuje sekcje jak w makiecie,
- pozwala uzupełnić pola bez źródła (§8) i zatwierdzić rewizję,
- przy artworku wykrojnikowym **odpala generowanie GLB od razu** (batch/accept),
- podgląd: przełącznik 3D ↔ dieline per poziom,
- akcje: „Zgłoś błąd" (dieline), „Eksport PDF (skos)”, „Zatwierdź kartę”.

Ekran dokładamy wg konwencji routingu artworku (grupa `artwork` w `app.py`),
gate rolą przez `core.security`, audyt przez `core.audit.log_audit`.

### 8a.6 Rollout (mały zakres — nie jeden wielki PR)

1. **Karta read-only** — agregat §2–§3 jako widok produktu per REF (bez 3D jeszcze).
2. **3D per poziom** — wpięcie istniejącego GLB/box/image + przełącznik podglądu.
3. **Dieline + zgłoszenia** — płaski widok + `artwork_card_issue` (najpierw internal).
4. **Eksport PDF (skos)** — render izometryczny + złożenie PDF.
5. **Kreator/edycja + pola §8** — zatwierdzanie karty i rewizji.
6. **Portal dostawcy** (opcjonalnie, po decyzji §8a.3) — zgłaszanie z zewnątrz.

Każdy krok = osobny branch/PR z testem (parser pól, generacja GLB smoke, walidacja
`artwork_card_issue`). 3D/render trzymamy lazy-import (CI bez ciężkich ML).

## 8b. Krok 1 rolloutu — plan implementacji (karta read-only)

Pierwszy realny PR. Zakres: **karta read-only per REF z arkuszami per poziom**,
złożona z istniejących źródeł. Bez 3D, bez zgłoszeń, bez PDF (to kroki 2–4).
**Bez nowej tabeli i bez nowej zależności** — karta jest agregatem (§2).

### Nowy moduł: `artwork_card.py` (czysta agregacja, bez Flaska)

```python
def build_card(db, ref) -> dict | None:
    """Złóż kartę produktu dla REF z istniejących źródeł. None gdy REF nieznany."""
    # common:    material_master.get_material(db, ref)         → nazwa/rodzina/dostawca/rozmiar
    # levels:    _levels_for(db, ref)                          → lista arkuszy per poziom
    # revisions: artwork_index.all_revisions(db, ref)          → historia + rewizja obow.
    # zwraca {"ref","common","levels":[...],"revisions":[...],"current_rev"}

def _levels_for(db, ref) -> list:
    """Scal poziomy z material_master (per-level dims/EAN) z plikami z artwork_index.
    Dla każdego poziomu: {level, role, ean|'NO-EAN', dims, file (lookup_best), rev}."""
```

Reguła §1a (arkusz per poziom) realizuje się tu: `levels` to lista, każdy element
niesie własny komplet (nagłówek „REF · poziom · EAN · Rev"). Moduł jest testowalny
na tymczasowym SQLite — bez ML, bez sieci.

### Route w `app.py` (grupa artwork, obok `/artwork/revisions` ~L11277)

```python
@app.route("/artwork/card/<ref>")
@login_required
def page_artwork_card(ref):
    import artwork_card
    db = get_db()
    try:
        card = artwork_card.build_card(db, artwork_card.normalize_ref(ref))
    finally:
        db.close()
    if not card:
        abort(404)
    return render_template("artwork_card.html", card=card,
                           username=session.get("username"), role=session.get("role"))

@app.route("/api/artwork/card/<ref>")   # ten sam agregat jako JSON — pod testy i przyszły JS
@login_required
def api_artwork_card(ref): ...
```

`normalize_ref` reuse z `artwork_index`/`material_master` (już istnieje w obu).

### Szablon `templates/artwork_card.html`

- `extends "base.html"`, Polish-first, komponenty/tokeny GROOVE (`page-header`,
  `panel`, `card`, `badge`) — jak reszta ekranów artworku.
- Układ wg makiety: nagłówek REF + dane wspólne → **sekcja per poziom jako osobny
  arkusz** z nagłówkiem `REF · poziom · EAN · Rev` (pod OCR) → historia rewizji.
- Read-only. Miejsca na 3D/dieline/zgłoszenia/skos zostawiamy jako puste sloty
  z komentarzem „krok 2–4" (bez logiki).

### Wejście (nawigacja)

Link „Karta" w wierszu listy `/artwork/materials` i/lub `/artwork/index` →
`/artwork/card/<ref>`. Minimalna zmiana w istniejącym szablonie listy.

### Test `tests/test_artwork_card.py`

- Seed tymczasowego SQLite: 1 materiał (`material_master.upsert_materials`) + kilka
  wpisów indeksu (`artwork_index.sync_entries`) dla 2 poziomów i 2 rewizji.
- Asercje: `build_card` zwraca poprawne `common`, liczbę `levels`, `NO-EAN` dla
  poziomu bez kodu, `current_rev` = najwyższa rewizja, `revisions` posortowane.
- Czysta logika — bez ciężkich importów (spójne z resztą `tests/`).

### Definition of Done kroku 1

- `python -m compileall` czysty, `pytest tests/` zielony (nowy test + reszta).
- Brak nowej tabeli, brak nowej zależności, ciężkie importy nie dochodzą.
- Karta otwiera się dla realnego REF i pokazuje arkusze per poziom.

## 8. Pola bez źródła — do dołożenia (jawnie ręczne)

Te pola nie mają dziś źródła prawdy w kodzie i wymagają zatwierdzenia człowieka
(lub nowej kolumny) — nie udawajmy, że wynikają z istniejących danych:

- klasa wyrobu medycznego + `sterylny?` (kandydat: nowa kolumna w `material_master`),
- wymagane symbole ISO 15223 per poziom + numer jednostki notyfikowanej CE,
- spec druku: paleta Pantone, profil ICC, spad, margines, min. font, wykończenie,
- opis zmiany rewizji.
