# UX_AUDIT.md — Audyt UX aplikacji Artwork wg 10 heurystyk Nielsena

**Aplikacja:** Artwork (fork DocCompare v6) — suita GROOVE / ACME.
**Stack:** Flask + Jinja2 + Alpine.js + Tailwind, `app.py` (~12,7k linii) + `blueprints/` + `core/`.
**Użytkownik / urządzenie:** biurowy, **desktop, Chrome**, praca z dokumentami/PDF i AI, plus zarządzanie biblioteką masterów (31k plików z `Z:\`) i generowanie modeli 3D. **Brak skanera Zebra / pracy na hali** — oceniano przez pryzmat scenariusza desktopowego (DOCCMP/ZARIA). Heurystyki „skanerowe" (autofocus skanu, HID, dźwięk/wibracja, cele 44px, tryb offline) w większości N/A.
**Metoda:** 6 równoległych audytów read-only (5 klastrów widoków + skan backendu), 56 szablonów, trasy i gating zweryfikowane w `app.py`/`blueprints/`. Znaleziska cytują realne `plik:linia`. Kodu nie zmieniano.
**Data:** 2026-09-04. Bazuje na gałęzi `main`.

> **Legenda ocen:** ✅ OK · ⚠️ do poprawy · ⛔ brak (funkcja nieobsłużona/relikt).
> `[Hn]` = numer heurystyki Nielsena (1–10).

---

## 1. Tabela zbiorcza

Ze względu na skalę (56 widoków) tabela grupuje **wzorce przekrojowe** (jeden problem × wiele ekranów — szczegóły w §2) oraz **dominujące znalezisko per widok**. Pełne listy w sekcjach per widok (§3).

### 1a. Wzorce przekrojowe (napraw raz — zyskaj wszędzie)

| Zakres | Heur. | Ocena | Opis (1 zdanie) |
|---|---|---|---|
| **~20 szablonów** | H2/H4 | ⚠️ | Relikt marki „DocCompare" w `<title>`/brandzie/pomocy mimo że produkt to „Artwork v1". |
| **~15 szablonów** | H2 | ⚠️ | Daty renderowane jako surowy ISO (`[:16]`/`[:10]`) zamiast suitowego `dd.mm.rrrr`. |
| **~10 szablonów** | H2/H6 | ⚠️ | Surowe kody enum w UI: statusy `active/ignored`, `pending/sent/failed`, warianty `dieline/marm/photo`, `status\|upper` → CRITICAL/ERROR. |
| **wszystkie z zapisem** | H1/H5 | ⚠️ | Brak twardej blokady podwójnego submitu / spinnera na akcjach async (zapisy, runy AI, importy). |
| **~15 miejsc (JS)** | H9 | ⚠️ | Puste `catch(e){}` maskujące błąd sieci — pusta tabela udaje „brak danych". |
| **cała apka poza kilkoma** | H4 | ⚠️ | Rozłam design systemu: tokeny `var(--…)` vs hardkodowane hexy/fiolet `#7c3aed` bez dark-mode. |
| **`palviz_viewer`, `palviz.py`** | H9 | ⛔ | Surowy traceback Pythona zwracany do UI (wyciek + nieczytelny dla użytkownika). |
| **`templates.html`, `checklists.html`, `suppliers*`, `doc_types`, `duplicates`, `search`, `versions`** | H8/H2 | ⛔ | Całe martwe ekrany/sekcje po usuniętych modułach (Zakupy/dokumenty handlowe PO/PI/CI/SAD). |
| **`artwork.html`** | H8/H9 | ⛔ | ~700 linii martwego kodu starego komparatora + pękające handlery (TypeError na starcie, zepsuta akcja „użyj jako master"). |
| **`report`, `scorecard`, `api_costs`** | H1/H9 | ⚠️ | Zależności z CDN (React/Babel/Chart.js) — pod CSP/offline główna treść cicho znika. |

### 1b. Dominujące znalezisko per widok

| Widok | Heur. | Ocena | Opis (1 zdanie) |
|---|---|---|---|
| login / forgot / reset | H1/H5 | ⚠️ | Submit bez blokady/spinnera; poza tym solidne (PL, a11y, walidacja siły hasła). |
| base.html (powłoka) | H1/H9 | ✅/⚠️ | Bardzo mocny fundament (apiFetch, confirmModal+Esc, overlay deployu), ale placeholder wyszukiwarki i kilka cichych `catch`. |
| profile.html | H9/H4 | ⚠️ | `fetch` bez try/catch (nieużyty apiFetch), lokalne komponenty zamiast systemu, lista działów z reliktami. |
| history.html | H4/H9 | ✅/⚠️ | Wzorcowe (spill enum→PL, eksport z spinnerem), ale dwa różne wzorce potwierdzenia usunięcia na 1 ekranie. |
| activity.html | H2 | ⚠️ | Nazwy zdarzeń i daty surowo z DB (kody EN/ISO) w chipach i tabeli. |
| help.html | H10 | ⛔ | Instrukcja odsyła do nieistniejącego modułu zgłoszeń (`/tickets` → 404) i opisuje usunięte funkcje. |
| versions.html | H4 | ⛔ | Widok o dokumentach handlowych (PO/PI→CI/kwota) — moduł usunięty, prawdopodobnie martwy. |
| 404 / 500 | H9 | ✅ | Wzorowe strony błędu bez tracebacku; drobna niespójność palety 500 vs 404. |
| artwork.html | H8/H9 | ⛔ | Martwy kod komparatora + pękające handlery (patrz §1a). |
| artwork_batch.html | H1/H5 | ⚠️ | „Uruchom batch" bez blokady → dubluje płatny run AI; brak „Przerwij". |
| artwork_report.html | H5/H1 | ⚠️ | Płatny re-run AI jednym klikiem bez potwierdzenia; React z CDN. |
| artwork_zone_select | H9 | ⚠️ | `deleteTemplate` nie sprawdza `res.ok` → fałszywy „usunięto". |
| artwork_card.html | H2 | ✅/⚠️ | Read-only, używa tokenów (wzór), ale data ISO i liczby do lewej. |
| artwork_templates.html | H1/H9 | ⚠️ | Zapis bez blokady; surowy obiekt błędu w komunikacie; nazwy ścian EN. |
| templates.html | H2/H8 | ⛔ | Relikt „szablony porównań dokumentów" (PO/PI/CI) — usunięty moduł. |
| checklists.html | H2/H9 | ⛔ | Relikt (PO/PI/CI/SAD/BL/CMR); dropdown dostawców z cicho połkniętym błędem. |
| scorecard.html | H6/H8 | ⚠️ | Liczby do lewej, brak legend ocen A–F, Chart.js z CDN. |
| artwork_index.html | H7/H1 | ⚠️ | 6 filtrów bez „Wyczyść"/deep-linku; brak spinnera przy 31k plików. |
| library.html | — | ✅ | **Wzorzec do naśladowania** (baner nieaktualnych danych, confirmModal danger). |
| artwork_manage.html | — | ✅ | Solidny hub (liczby `pl-PL`, „ostatni skan"), do ochrony. |
| artwork_manage_unbound | H5 | ⚠️ | `bind()` bez blokady → podwójny submit; poza tym dobry (spinnery, chipy). |
| artwork_manage_gaps | H9/H2 | ⚠️ | 3 puste `catch`, surowe statusy `active/ignored`, brak eksportu. |
| artwork_mapping_groups | H4/H5 | ⚠️ | Natywny `confirm` zamiast confirmModal; brak Enter=zapis; brak blokady submitu. |
| artwork_aliases | H4/H9 | ⚠️ | Natywny `confirm`; `load()` łyka błąd → pusta lista udaje „brak aliasów". |
| artwork_revisions | H9 | ⚠️ | Błąd `load()` bez komunikatu → „wszystko aktualne ✓" może kłamać. |
| duplicates.html | H8/H2 | ⛔ | Relikt „duplikaty faktur" (Total A/B, PO) — usunięty moduł Zakupy. |
| data_hub.html | H8 | ⚠️ | Plakietki reklamują usunięte moduły (Zakupy+Transport). |
| search.html | H8/H2 | ⛔ | Sekcja „Porównania dokumentów" → `/history` z usuniętego modułu. |
| materials.html | H1/H2 | ⚠️ | Brak spinnera przy 31k, data ISO, liczby do lewej; `lv-paz` OK. |
| artwork_materials.html | H5/H1 | ⚠️ | `saveAllLevels()` N×PUT bez `r.ok`/transakcji → częściowy zapis udaje sukces. |
| suppliers.html | H8/H9 | ⛔ | Relikt (mapowanie PI/PO/SAD); import bez raportu odrzuconych. |
| suppliers_master.html | — | ✅ | **Wzorzec** (tokeny, dark-mode, inline-edit z disabled+toast). |
| supplier_wizard.html | H1/H8 | ⛔ | Relikt (PI/PO/SAD); walidacja tylko kroku 1. |
| artwork_profiles.html | H9/H4 | ⚠️ | Płatna AI-detekcja bez potwierdzenia kosztu; największy rozjazd stylów (fiolet). |
| admin.html | H8/H2 | ⚠️ | Martwy JS (loadRoleModules ReferenceError, null-DOM), koszty w USD vs PLN w /api-costs. |
| artwork_admin.html | H10 | ⚠️ | Odsyła do dodawania userów w `/admin`, którego tam nie ma. |
| api_costs.html | H9/H4 | ⚠️ | Chart.js z CDN; biały motyw niespójny z admin.html. |
| email_settings.html | — | ✅/⚠️ | **Wzorzec** (hasło nie wraca, disabled+busy), drobne: port `type=text`. |
| doc_types.html | H8 | ⛔ | Built-iny PO/PI/CI/PL/SAD — relikt; poza tym confirmModal danger OK. |
| incoterms_dictionary | H1/H9 | ⚠️ | `icAdd/icSave` bez `.catch` ani stanu ładowania. |
| transit_countries | H9 | ✅/⚠️ | **Wzorzec** (confirm+toast z nazwą), ale `load()` łyka błąd. |
| translation_dictionary | H9/H5 | ⚠️ | Import CSV bez raportu odrzuconych; inline-edit SAD ginie przy zmianie taba. |
| artwork_kpi.html | H4/H2 | ⚠️ | Cały plik hardkodowane kolory; `status\|upper`, data ISO, liczby do lewej. |
| artwork_3d.html | H1/H3 | ⚠️ | Render ~5 min bez paska/procentu i bez „Anuluj". |
| artwork_3d_marm.html | H5 | ⚠️ | Brak ostrzeżenia o wymiarze odstającym (MARM 0=placeholder → box 0×0×0). |
| artwork_3d_photo.html | H1/H2 | ⚠️ | Render synchroniczny bez postępu; `panels_found` surowe kody ścian. |
| artwork_3d_batch.html | H1 | ✅/⚠️ | **Wzorzec paska postępu**, ale po zamknięciu karty `jid` przepada. |
| artwork_palviz_batch | H9/H2 | ⚠️ | `innerHTML` bez escapowania błędów; kody poziomów EN/PL zmieszane. |
| artwork_palviz_viewer | H9 | ⛔ | Surowy traceback z serwera pokazany w UI. |
| artwork_render_coverage | H2/H6 | ⚠️ | Kolumna „Push" pokazuje surowy `dieline`, obok kolumna „3D" tłumaczy na „Wykrojnik". |
| admin_palviz_push | H2/H1 | ✅/⚠️ | **Wzorzec baneru „push wyłączony"**, ale surowe kody wariant/status + data ISO. |

---

## 2. Wzorce przekrojowe (szczegóły z licznikami)

**W2.1 — Marka „DocCompare" [H2/H4] — ~20 wystąpień.**
`<title>`/brand/pomoc mimo „Artwork v1" (base.html:97). M.in.: login/forgot(:41)/reset(:8,48), profile:2, versions:2, activity:2, history:2, help(:2,179,226,289,884 — „DocCompare v6"), artwork:2, artwork_batch:2, artwork_report, artwork_card:2, templates:2, checklists:2, scorecard:2, artwork_index:2, library(:133,163 nazwa zadania „DocCompare Library Sync"), duplicates/mapping_groups/aliases/revisions/data_hub:2, materials/suppliers/…:2, artwork_render_coverage:2. **Fix:** jeden `{{ app_name }}` w `base.html` + sweep.

**W2.2 — Daty ISO zamiast `dd.mm.rrrr` [H2] — ~15 wystąpień.**
profile(:137,141,250,262), activity:220, history (tooltip), card:73, history:188, materials:181,228, suppliers:437, admin:145,147, artwork_admin:79,98,125, translation_dictionary:312, artwork_kpi:100, scorecard:141, checklists:204, artwork:1838, admin_palviz_push:64, artwork_index:34, library:55,196. **Fix:** wspólny filtr Jinja/JS `dataPL()` (wzorzec: `timeAgo()` w base.html:426, `spill()` w history).

**W2.3 — Surowe kody enum w UI [H2/H6] — ~10 miejsc.**
`status|upper` → CRITICAL/ERROR (artwork_kpi:117, artwork_admin:131), `f.status` active/ignored (manage_gaps:81,111), warianty `dieline/marm/photo` (coverage:69, admin_palviz_push:57), push `pending/sent/failed` (admin_palviz_push:61,21-23), `panels_found` front/back (3d_photo:305), `direct/inherit` (artwork_materials:466), zdarzenia `login/compare/upload_pdf` (activity:162-236), poziomy `box/carton/karton` (palviz_viewer:45, palviz_batch:30). **Fix:** wspólna mapa kod→PL.

**W2.4 — Brak blokady podwójnego submitu [H1/H5] — praktycznie każdy ekran z zapisem.**
Najkosztowniejsze (płatny AI/duplikat danych): artwork_batch:121,217 (run/parowanie), artwork_report:792 (re-analyze AI), artwork.html spRunBtn (:526), artwork_profiles runAiDetect (:940), checklists submit ×3 (:490,595,633), artwork_materials saveAll (:487), templates save (:204), supplier_wizard save (:529), unbound bind (:82), mapping_groups/aliases add, doc_types saveOrder, incoterms/transit add, palviz_viewer gen (:87), 3d_marm/photo/palviz gen. **Wzorzec poprawny do powielenia:** `history.exportRep` (:731 disabled+spinner), `email_settings` (:disabled=busy), `coverage busy[id]`.

**W2.5 — Puste `catch(e){}` maskujące błąd [H9] — ~15 w JS + ~200 `except: pass` w backendzie.**
JS krytyczne (ukrywają błąd akcji, nie tylko fluff): materials:232, suppliers_master:139,143, transit_countries:61, manage_gaps:99,104,111, artwork_profiles:529,540, aliases:86, mapping_groups:110, revisions:66, artwork_report:2581 (bcCopy), artwork.html:2389 (thumbnail wisi „Wczytuję…"). Backend: patrz §4.

**W2.6 — Rozłam design systemu [H4] — większość ekranów.**
Tokeny `var(--…)` (wzory: artwork_card, suppliers_master, admin, doc_types, email_settings, manage_gaps, coverage) vs hardkodowane hexy/fiolet `#7c3aed`/`#1a1a2e` bez dark-mode (najgorsze: artwork_kpi cały plik, artwork_profiles, artwork_materials, suppliers, api_costs, artwork_admin, artwork_batch ~15×, artwork_history, artwork_templates). artwork_materials:20-22 sam w komentarzu przyznaje niespójność.

**W2.7 — `res.json()` przed sprawdzeniem `res.ok`/status [H9] — ≥4 w klastrze 3D + inne.**
3d single:186, 3d batch:96, photo:294, palviz_viewer:100, history:285. Przy 404/500-HTML daje kryptyczne „Unexpected token <" zamiast „Zadanie wygasło". **Fix:** `if(!res.ok) throw` przed parsowaniem.

**W2.8 — Zależności CDN cicho psujące treść [H1/H9].**
artwork_report:527-529 (React+ReactDOM+Babel unpkg — cała sekcja pól = pustka offline), scorecard:154 (Chart.js jsdelivr), api_costs:148 (Chart.js jsdelivr). **Fix:** zvendorować lokalnie jak `model-viewer`.

**W2.9 — Natywne `alert()`/`confirm()`/`prompt()` zamiast komponentów [H4].**
`prompt()`/`alert()` na wymiary/REF (artwork:1872,2500), `confirm()` (mapping_groups:136, aliases:108, materials:258), `alert()` retry (coverage:123,129, admin_palviz_push:102,109), `alert()` błędy admin (:257). Łamie design system i jest nietłumaczalne/nieostylowane.

---

## 3. Znaleziska per widok

> Poniżej tylko znaleziska **nieujęte w §2** lub wymagające lokalnego kontekstu. Powtórki (marka, daty, submit, catch, style) są policzone w §2 i nie dublowane tutaj.

### Klaster: Autoryzacja + powłoka + błędy + pomoc

**login.html**
- [H2/H4] :76 — podtytuł „Platforma weryfikacji **dokumentów** i artworków" — „dokumenty" to usunięty moduł → zawęzić do artworków/opakowań.
- [H9] :101 — po błędzie logowania `identifier` nie jest re-wypełniany z `request.form` → dodać `value` + wskazać czego dotyczy błąd.
- ✅ Pełny PL, `aria-label` na toggle hasła, `autocomplete`, `noindex`.

**profile.html**
- [H9] :317-322,343-348 — `saveProfile()`/`changePassword()` `fetch` bez try/catch mimo gotowego `apiFetch` w base.html → błąd sieci = cichy rejection. **Przepiąć na `window.apiFetch`.**
- [H4] :5-95 — lokalne `.card`/`.btn-save`/`.form-group` zamiast `panel`/`btn btn-primary`.
- [H2/H4] :187 — lista działów `['Zakupy','Logistyka',…]` z reliktami usuniętych modułów.
- do sprawdzenia ręcznie: istnienie klas `alert-ok`/`alert-err` w app.css (:305).

**base.html**
- [H2] :288 — placeholder wyszukiwarki „Szukaj: PO, kontener, dostawca, porównanie…" — pojęcia z usuniętych modułów → REF/rewizja/master.
- [H9] :471,483,493 — ciche `catch` w powiadomieniach; min. toast na `markAllRead`.
- [H1] — brak ostrzeżenia o wygasającej sesji; POST po wygaśnięciu → redirect na /login z utratą danych (do sprawdzenia ręcznie).
- [H4] :360-414 — dwa wzorce potwierdzeń (globalny `confirmModal` vs inline confirm-bary w podstronach) → ujednolicić.
- ✅ **Fundament do ochrony:** `apiFetch` (:50), toast z escapem (:381), confirmModal+Esc+klik-tła (:400-414), overlay deployu z pollingiem /health (:515), flash→toast (:326), skip-link, `aria-current`.

**history.html**
- [H4] :220-234 vs base — bulk delete `#confirmBar` vs `deleteOne` `confirmModal` — dwa wzorce na jednym ekranie.
- [H9] :409-421 — `confirmDelete`/`deleteOne` `fetch` bez try/catch → błąd sieci = rekord zostaje, brak toastu.
- [H9] :435 — „Błąd ładowania raportu #id" bez przyczyny/„co dalej".
- ✅ **Wzorzec:** `spill()`/`sevpill()` enum→PL (:749), `esc()` w renderze, Esc+klik-tła (:449,452), eksport z disabled+spinner (:731), filtry w URL.

**activity.html**
- [H2] :162-236 — nazwy zdarzeń surowo z DB (kody EN) w chipach/filtrze/tabeli → mapować jak `spill()`.
- [H1] :138 — „Eksport JSONL" → `/api/admin/activity-log?limit=10000` bez `download`/rozmiaru (do sprawdzenia: pobiera czy renderuje).
- ✅ Filtry z licznikiem + „Załaduj więcej", dark-mode chipów, `sr-only`.

**help.html** ⛔
- [H1/H10] :780-838 — sekcja „8. Zgłaszanie problemów" opisuje moduł ticketów `/tickets`, który **jest 404** (footer:885 sam to przyznaje) → usunąć/przepisać.
- [H10] :861-872 — tabela ról wymienia usunięte funkcje (Porównywanie dokumentów, Ekstrakcja skanów, Profile dostawców).
- [H10] :886,878,257 — „skontaktuj się z administratorem" bez podania kontaktu.
- [H1] :884 — „DocCompare v6" vs realne „Artwork v1".

**versions.html** ⛔ — cały widok o dokumentach handlowych (PO/PI→CI/kwota, `/api/export/pdf`) — moduł usunięty → potwierdzić czy trasa ma sens (do sprawdzenia ręcznie).

**500.html**
- [H4] :6 — nagłówek `text-red-600` (Tailwind) vs 404 `var(--accent)` → token.
- [H10] :9 — „skontaktuj się z administratorem" bez kanału kontaktu.
- ✅ Bez tracebacku, PL, „spróbuj ponownie".

### Klaster: Porównywarka artworków / raporty

**artwork.html** ⛔ (najwyższy priorytet)
- [H8] :786-1465 — ~700 linii martwej warstwy starego komparatora (`runCompare`, `renderVerdict/KPI/Fields`, `loadPageImages`) celuje w nieistniejące DOM-idy → usunąć.
- [H9] :1401-1411,1464 — `loadHist()` woła się na starcie, `#histBox` nie istnieje → niewyłapany TypeError przy każdym wejściu.
- [H9] :2145-2156 — „📌 Użyj jako wzorzec (A)" wstrzykuje do nieistniejącego `#fileA` → **akcja z serwera zepsuta**.
- [H3] :3325-3328 — `pvClose()` kasuje `_pvOverrides` (ręczna praca operatora) bez potwierdzenia.
- [H3] Esc niespójny na 5 modalach (spCropModal, pvOverlay, fqPanel, libModal, masterLibModal).
- [H8] :749-752 — dwa konkurujące CTA obok siebie.
- ✅ **Chronić:** uczciwy pasek `/compare-progress` (:3790), recovery zachowuje pliki (:3817), kolejka pól z AbortController+timeout wymusza błędne pola jako krytyczne (chroni przed cichym APPROVED), a11y (role=dialog/progressbar), `esc()`/`_safeHex()`.

**artwork_batch.html**
- [H3] :128-135 — batch (minuty) bez „Przerwij".
- [H9] :301,309 — `catch{}` łyka błędne linie NDJSON → para znika bez śladu → licznik błędów.
- [H9] :288-320 — zerwanie streamu renderowane jako komplet → porównać `batchResults.length` do `detectedPairs.length`.
- do sprawdzenia ręcznie: czy `toast()` jest zdefiniowany w tym pliku (jeśli nie — błędy ciche).
- ✅ Dropzone a11y, pasek z ETA+elapsed, empty-state.

**artwork_report.html**
- [H5] :792 — „Przeanalizuj ponownie" = **płatny re-run AI jednym klikiem bez potwierdzenia** → confirm z nazwą modelu + lock.
- [H9] :527-529 — React/Babel z unpkg → offline pustka bez komunikatu.
- [H6] :1659-1668 — kolumny „Carton sticker"/„Pouch label" po EN i hardkodowane niezależnie od pary.
- [H1] :806-812 — przełącznik „Poziom raportu" filtruje `.comp-row` nieobecne w widoku React (prawdopodobnie no-op — do sprawdzenia).
- ✅ Stan wygasłej sesji (:815), lightbox, lupa z sumą kontrolną EAN-13, dopasowanie EAN A↔B ✓/✗.

**artwork_zone_select.html**
- [H9] :388-393 — `deleteTemplate` nie sprawdza `res.ok` → **fałszywy „usunięto"** przy błędzie.
- [H9] :431-432 — klucze `STATUS_CSS` bez diakrytyków → „Różnica…" spada do szarego „unknown".
- [H6] :369 — surowe `both/text/graphic` zamiast PL.

**artwork_card.html** — [H8] :18-20 liczby/EAN do lewej → do prawej. ✅ używa tokenów (wzór). do sprawdzenia: `/artwork/card/NONEXISTENT` i etykieta PAZ (:47).

**artwork_templates.html** — [H9] :83,128 `.catch(e=>…="Błąd: "+e)` surowy obiekt; [H5] :125 `name||"szablon"` cichy fallback; [H6] nazwy ścian EN.

**templates.html** ⛔ — relikt „szablony porównań dokumentów" (PO/PI/CI, :264); [H9] :250-262 import bez raportu odrzuconych.

**checklists.html** ⛔ — relikt (PO/PI/CI/SAD/BL/CMR); [H9] :302-311 + app.py:3308 dropdown dostawców z cicho połkniętym `except` na serwerze → pusta lista bez komunikatu; [H5] :301-381 dublujące „Dostawca" (select) + „Nazwa własna" (input) bez reguły pierwszeństwa.

**scorecard.html** — [H8] :121,133 liczby do lewej; [H6] :39-42 oceny A–F bez legendy; [H1] :154 Chart.js CDN; [H3] :58 brak filtrów przy 100+ dostawcach.

### Klaster: Indeks / biblioteka / zarządzanie

**artwork_index.html** — [H7] :46-70 6 filtrów bez „Wyczyść filtry"/deep-linku (URLSearchParams tylko do fetch); [H1] :171 brak spinnera przy 31k; [H5] :140 upload bez walidacji rozmiaru i bez blokady kliknięcia. ✅ obsługa `browseErr` z retry (chroni przed „pusty indeks").

**library.html** ✅ **wzorzec** — baner „statystyki nieaktualne" z powodem (:39), toast tylko przy przejściu w błąd (:201), confirmModal danger na reset tokena (:177). [H8] :59 kafel „–" do pierwszego odświeżenia → „…".

**artwork_manage_unbound.html** — [H5] :82,119-126 `bind()` bez blokady → podwójny submit; do sprawdzenia: czy `/api/artwork/manage/bind` odrzuca nieistniejący REF. ✅ spinnery, chipy źródeł.

**artwork_manage_gaps.html** — [H7] :52 brak eksportu listy „produkty bez artworku"; [H2] :81 surowe `active/ignored`; [H5] :82 zmiana statusu (ukrywa plik) bez confirm/undo. ✅ rozróżnia `loadedG/D` od pustki (wzór).

**artwork_mapping_groups.html** — [H4] :136 natywny `confirm`; [H6] :42 brak Enter=zapis; [H5] :44 brak blokady submitu. ✅ ostrzeżenie „⚠️ pliku brak w indeksie".

**artwork_aliases.html** — [H4] :108 natywny `confirm`; [H9] :86 `load()` łyka błąd. ✅ kandydaci z score, komunikat przy braku kandydatów.

**artwork_revisions.html** — [H9] :66-67 błąd `load()` bez komunikatu → „wszystko aktualne ✓" może kłamać.

**duplicates.html** ⛔ — relikt „duplikaty faktur" (Total A/B, PO, :46-114); [H6] :99 statusy mieszają języki.

**data_hub.html** — [H8] :74 plakietka „🔁 Zakupy + Transport + Artworki" (usunięte moduły); :98 kafel „Profile dostawców" opisuje mapowanie PO/PI. ✅ opis materiałów używa **PAZ** poprawnie (:84).

**search.html** ⛔ — [H8] :57-70 sekcja „Porównania dokumentów" → `/history` z usuniętego modułu; [H6] brak autocomplete/historii na ekranie lookup.

### Klaster: Dane wzorcowe + admin + słowniki + KPI

**materials.html** — [H1] :232 brak spinnera przy `limit=0` (31k); [H5] :73-74 „Usuń starsze niż" (masowa destrukcyjna) w toolbarze obok importu; [H8] :104 `:key="m.ref_code"` zakłada unikalność REF. ✅ `lv-paz`, `localeCompare('pl',{numeric:true})`.

**artwork_materials.html** ⚠️ (ryzyko danych) — [H5] :487-503 `saveAllLevels()` N×PUT bez `r.ok` ani transakcji → **częściowy zapis udaje sukces**; [H8] :329 checkbox „zaznacz wszystkie" bez akcji zbiorczej (martwa kolumna); [H10] :217 hint CSV z nazwami kolumn DB, bez pliku wzorcowego.

**suppliers.html** ⛔ — relikt (PI/PO/SAD, :99,171); [H9] :460-468 import pętlą POST bez transakcji, raport tylko zbiorczy; [H8] :195 `dc.CI.get()` bez `.get('CI',{})` → ryzyko 500.

**suppliers_master.html** ✅ **wzorzec** (tokeny, dark-mode, inline-edit disabled+toast). [H9] :139-145 puste `catch` w load/open; [H5] :98 `type=email` bez walidacji formatu.

**supplier_wizard.html** ⛔ relikt — [H1] :177-266 walidacja tylko kroku 1 → zapis z niekompletnym mapowaniem. ✅ bogate hinty domenowe (SAD ZC415).

**artwork_profiles.html** — [H9] :940-957 `runAiDetect()` (płatne, Claude) bez potwierdzenia kosztu/budżetu + surowy `e.message`; [H4] największy rozjazd stylów (fiolet `#7c3aed`, zakładka „Mastery" też fioletowa — semantyka AI rozmyta). ✅ fallback podglądu canvas→thumb→błąd po PL.

**admin.html** — [H9] :390 `loadRoleModules()` wołane w init, niezdefiniowane → **ReferenceError przy każdym wejściu** (do sprawdzenia); [H8] :290-329 martwy JS na null-DOM (RefDb); :267 `ALL_MODULES` = usunięte moduły; [H1] route podaje `users`, ale tabeli userów brak w szablonie; [H2/H4] :88-151 koszty w **USD `$`.kropka**, a `/api-costs` w **PLN `zł`,przecinek**; [H4] :257 `alert()` zamiast `toast()`; [H5] :200,226 POST bez nagłówka CSRF (do sprawdzenia).

**artwork_admin.html** — [H10] :87-89 odsyła do dodawania userów w `/admin` (którego tam nie ma).

**api_costs.html** — [H9] :148 Chart.js CDN; [H4] biały motyw vs admin.html. ✅ koszty w PLN z jawnym kursem (właściwy kierunek — rozjazd z admin).

**email_settings.html** ✅ **wzorzec** — [H5] :33 „Port" `type=text` → `type=number`/`inputmode` + zakres 1-65535; [H1] :135 „Wyślij testowy" używa zapisanej konfiguracji przy niezapisanej edycji (dirty) → zablokować. ✅ hasło nie wraca, disabled+busy, presety M365/Google.

**doc_types.html** ⛔ relikt (PO/PI/CI/PL/SAD) — [H9] :351-365 `saveOrder()` zawsze toast „✓" bez `r.ok`; :261-277 inline `saveField` przy błędzie zostawia zmienioną wartość (rozjazd UI↔DB); [H2] :237 sort bez `'pl'`. ✅ confirmModal danger, built-iny bez „Usuń".

**incoterms_dictionary.html** — [H1/H9] :75-101 `icAdd/icSave` bez `.catch` ani stanu ładowania; [H5] :33 auto-uppercase tylko CSS (wysyłka surowa); [H10] :42 brak rozwinięcia kodów (FOB/CIF).

**transit_countries.html** ✅ **wzorzec** (confirm+toast z nazwą, `:disabled=!trim`). [H9] :60-61 `load()` `catch{}` → „Brak krajów" myli błąd z pustką.

**translation_dictionary.html** — [H9/H5] :388-423 import CSV bez raportu odrzuconych; [H5] :497-517 inline-edit SAD `_sadDirty` ginie przy zmianie taba; [H2] :251 `LANGS` po EN; [H8] :135 filtry stylizowane jako `.btn`. ✅ parser CSV RFC-4180, confirmModal danger.

**artwork_kpi.html** — [H1] :50 brak „stan na" (data przeliczenia); [H2] :65 `strftime("%B %Y")` → EN miesiąc zależnie od locale (do sprawdzenia); [H8] :99 liczby do lewej; [H4] cały plik hardkodowane kolory + lokalny reset `*{}` nadpisujący base. ✅ etykieta dnia `%d.%m` (poprawny PL).

### Klaster: 3D + PalViz + pokrycie

**artwork_3d.html** — [H1] :180-190 render ~5 min bez procentu/paska (gdy backend nie zwraca `step_label`); [H3] :163-234 brak „Anuluj". ✅ blokada pobrania do akceptacji egzekwowana serwerowo, `showMsg` textContent (bez XSS), orientacja jako propozycja z resetem akceptacji.

**artwork_3d_marm.html** — [H5] :266-274 **brak ostrzeżenia o wymiarze odstającym** (MARM 0=placeholder → box 0×0×0) — route dieline waliduje 5–2000 mm, tu nie; [H1] :432-461 generacja synchroniczna bez licznika; [H9] :218 błąd jednostek bez treści/retry. ✅ prefill+sugestia jako propozycja „Użyj".

**artwork_3d_photo.html** — [H2] :305 `panels_found` surowe kody ścian (front/back) mimo że UI zna `PANELS` PL; [H5] :219 przycisk aktywny bez kompletu wymiarów → błąd dopiero po kliknięciu. ✅ instrukcja kadrowania PL.

**artwork_3d_batch.html** ✅ **wzorzec paska postępu** (`done/total`+`step_label`, :97) — powielić w single. [H3] :90-107 po zamknięciu karty `jid` przepada mimo obietnicy „możesz wrócić" (:26); [H1] :86 download bez kroku akceptacji (niespójność zasady vs single — świadome?).

**artwork_palviz_batch.html** — [H9] :83-84 `it.error`/warning przez `innerHTML` **bez escapowania** (XSS/łamanie layoutu); [H5] :69 wiersze bez pliku/SKU cicho pomijane bez raportu; [H2] brak `page_title`.

**artwork_palviz_viewer.html** ⛔ — [H9] :115 + palviz.py:124-126 endpoint zwraca `{"error": str(e), "detail": traceback}` a UI pokazuje `res.error` wprost → **surowy wyjątek Pythona do biurowego użytkownika**; [H5] :87 „Generuj" nie blokowany → podwójny render; [H9] :110 zakłada `manifest.faces` bez guardu. ✅ `needs_manual_dims` z jasnym komunikatem, scoping plików serwerowy.

**artwork_render_coverage.html** — [H2/H6] :69 kolumna „Push" pokazuje surowy `dieline` uppercase, obok kolumna „3D" tłumaczy na „Wykrojnik" → **niespójność etykiet tej samej encji w jednym wierszu**; [H1] :127 retry sukces bez potwierdzenia (tylko `load()`); [H9] :129 `alert()`; [H2] :60 „nieaktualny" na `badge-err` (czerwony=błąd) → rozważyć żółty (ostrzeżenie). ✅ pełna obsługa stanów pusty/loading/błąd z retry, retry gated podwójnie (UI + `require_role`), `busy[id]` blokuje double-submit.

**admin_palviz_push.html** — [H2/H6] :57,61 kolumny „Wariant"/„Status" surowe `dieline/pending/sent/failed` (coverage tłumaczy na PL — niespójność); [H2] :64 `push_at` surowo (ISO); [H1] :9 brak agregatu (ile pending/sent/failed) mimo że to panel monitoringu. ✅ **baner „push wyłączony — brak PALVIZ_PUSH_URL" z instrukcją** (wzór widoczności stanu), podwójne gating, `busy[id]`, kolumna „Błąd" z `:title`.

---

## 4. Ryzyka danych (pkt 5 i 9 — mogą wprowadzić błędne dane lub obejść walidację)

> To **nie kosmetyka** — te znaleziska pozwalają zapisać niespójny/błędny stan lub ukrywają awarię.

**RD-1 [WYSOKA] Wyciek tracebacku do UI.** `blueprints/palviz.py:126,268` — `jsonify({"error": str(e), "detail": traceback.format_exc()[-500:]}), 500` renderowany wprost w `artwork_palviz_viewer.html:115`. Wyciek wewnętrznych ścieżek + nieczytelne dla użytkownika. → log serwerowy + generyczny PL komunikat.

**RD-2 [WYSOKA] Ciche błędy warstwy DB.** `db.py:375,384` — `except Exception: pass` na poziomie połączenia/puli → awaria DB może być niewidoczna. `migrate_db.py:139` — nieudany zapis wersji migracji cicho ignorowany → stan migracji może kłamać. (Część z ~200 `except: pass` w repo — rozkład: app.py ~70, artwork_comparator ~40, artwork_index 9.) → `logger.exception` przynajmniej na ścieżkach DB/migracji/walidacji.

**RD-3 [ŚREDNIA] Częściowy zapis bez transakcji udaje sukces.** `artwork_materials.html:487-503` (`saveAllLevels` N×PUT bez `r.ok`), `suppliers.html:460` i `translation_dictionary.html:388` (import pętlą POST bez transakcji i bez raportu odrzuconych) → część rekordów zapisana, UI mówi „OK". → sprawdzać `r.ok` per krok + raport odrzuconych; rozważyć endpoint batch transakcyjny. **Wzorzec poprawny:** import CSV materiałów `app.py:12082-12086` (per-wiersz rollback + `errors[]`).

**RD-4 [ŚREDNIA] Fałszywy sukces akcji destrukcyjnej.** `artwork_zone_select.html:388` (delete bez `res.ok`), `doc_types.html:351` (saveOrder zawsze „✓"), `manage_gaps.html:99` (setStatus cichy) → wiersz znika/„zapisano" mimo błędu serwera. → gate na `res.ok`, zostaw stan przy błędzie.

**RD-5 [ŚREDNIA] Wymiar odstający akceptowany.** `artwork_3d_marm.html:266-274` — MARM zwraca 0/placeholder (znany „0=placeholder"), UI wypełnia `0×0×0` i pozwala generować (route dieline waliduje 5–2000 mm, ten flow nie). → walidacja zakresu przed generacją.

**RD-6 [ŚREDNIA] Płatna operacja AI bez potwierdzenia i bez blokady.** `artwork_report.html:792` (re-run AI), `artwork_profiles.html:940` (AI-detekcja), `artwork_batch.html:121` (run batch) — jeden/podwójny klik odpala koszt Claude. → confirm z modelem/kosztem + disabled synchroniczny. (Budżet śledzi `api_usage_tracker` — pokazać go w potwierdzeniu.)

**RD-7 [ŚREDNIA] Upload bez walidacji pliku.** `blueprints/palviz.py:100-102` — `file.save` bez sprawdzenia MIME/rozszerzenia przed `build_bundle` (tylko `secure_filename` na nazwie). Klienckie sprawdzenia rozszerzenia w artwork.html/batch nie chronią serwera.

**RD-8 [NISKA, single-user] Brak obsługi konfliktu edycji.** Update materiału/dostawcy/profilu bez `updated_at`-guard → last-write-wins. Aplikacja jest single-user by design, więc mało realne, ale przy dodaniu 2. konta staje się aktywne.

**RD-9 [do sprawdzenia ręcznie] Gating importu MARM tylko w UI.** `artwork_3d_marm.html:35` ukrywa „Import MARM" dla ról < manager, ale endpoint `/api/materials/marm-import` nie pojawił się z `@require_role` w przeglądzie. **Scenariusz:** zaloguj jako `user`, POST na `/api/materials/marm-import` w DevTools → czy 403? (Uwaga: aplikacja jest single-user, więc realne ryzyko niskie, ale kontrakt powinien być serwerowy.)

**Brak surowego `str(e)` w UI — ~30 miejsc** zwraca `str(e)[:200]` do odpowiedzi (najgroźniejsze bez limitu: `app.py:5247,4684,5618,9532`, `palviz.py:207`). Treść wyjątku w UI, choć przycięta. → mapować na komunikat domenowy, `str(e)` tylko do logu.

---

## 5. Niespójności z suitą GROOVE

| Konwencja suity | Stan w Artwork | Dowód |
|---|---|---|
| jednostka palety = **PAZ**, nigdy PAL | **ROZŁAM:** silnik UOM używa `"PAL"` jako kanonicznej (utrwalone testem), ale ekrany UI używają `PAZ` poprawnie | `uom.py:30-31`, `tests/test_uom.py:25` (PAL) vs `materials.html` `lv-paz`, `data_hub.html:84` (PAZ) → **decyzja właściciela: ujednolicić na PAZ** (zmiana dotknie test) |
| daty **dd.mm.rrrr** | wszędzie surowy ISO (`[:16]`/`[:10]`) | ~15 wystąpień (§2.2) |
| przecinek dziesiętny | liczby renderowane surowo (kropka / bez formatu) | materials:172, artwork_kpi:99, koszty admin.html (`$`.kropka) |
| interfejs po polsku, zero angielskich stringów | kody enum EN w UI (`CRITICAL`/`active`/`dieline`/`pending`), „Batch", „Scorecard", statusy `OK/CRITICAL/ERROR`, `LANGS` EN | §2.3; artwork_batch, scorecard, history:192, translation_dictionary:251 |
| nazwy statusów/ról spójne między aplikacjami | statusy pliku raz `active/ignored` (gaps), raz PL, warianty raz `dieline` raz „Wykrojnik" w sąsiednich kolumnach | coverage:69 vs :59; manage_gaps:81 |
| jeden design system, bez lokalnych wariantów | dwa obozy: tokeny `var(--…)` vs hardkodowane hexy/fiolet bez dark-mode; lokalne `.card`/`.btn-save` (profile), lokalne komponenty (versions, artwork_kpi) | §2.6 |
| Enter = zapis, Esc = wyjście, jednakowo | Esc niespójny (część modali zamyka, część nie: artwork ×5, templates, checklists); Enter=zapis brak w formularzach CRUD (mapping_groups:42, aliases:35) | §2 W2, artwork.html, templates.html |
| brak reliktów po forku | całe ekrany/sekcje o usuniętych modułach (Zakupy, dokumenty PO/PI/CI/SAD, moduł ticketów, transport) | templates, checklists, suppliers*, doc_types, duplicates, search, versions, help §8, base:288, data_hub, admin `ALL_MODULES` |
| marka spójna | „DocCompare"/„DocCompare v6" vs „Artwork v1" | ~20 wystąpień (§2.1) |
| waluta spójna | koszty API raz USD (admin.html), raz PLN (api_costs) | admin.html:88 vs api_costs |

---

## 6. Priorytety (max 10, wg wpływu/kosztu)

| # | Poprawka | Warstwa | Rozmiar | Uzasadnienie |
|---|---|---|---|---|
| 1 | Usunąć martwy kod + pękające handlery w `artwork.html` (TypeError na starcie, zepsuta „użyj jako master") | JS | **M** | Główny ekran; realnie zepsuta akcja + błąd w konsoli przy każdym wejściu. |
| 2 | Globalny sweep marki „DocCompare"→„Artwork" (`{{ app_name }}` w base) | szablon | **S** | ~20 ekranów, jeden fix, wysoka widoczność, buduje zaufanie. |
| 3 | Potwierdzenie + blokada na płatnych operacjach AI (report re-run, profiles AI-detect, batch) | JS + szablon | **S** | Ryzyko danych/kosztu (RD-6); jeden klik = wydatek Claude. |
| 4 | Naprawić wyciek tracebacku (`palviz.py:126,268`) + ~30 `str(e)` → komunikat PL, `str(e)` do logu | backend | **S/M** | RD-1 (wysoka): wyciek + nieczytelność. |
| 5 | Wspólny helper `dataPL()` + mapa enum→PL, rozlać na daty/statusy/warianty | szablon + JS | **M** | ~15 dat + ~10 enumów; naprawia H2/H6 hurtem; wzorzec już jest (`spill`, `timeAgo`). |
| 6 | Blokada podwójnego submitu (disabled+spinner) na zapisach/importach/runach | JS | **M** | Rozproszone, ale wzorzec gotowy (`history.exportRep`); RD-3/RD-6. |
| 7 | Ciche `catch{}` → widoczny błąd (rozróżnić „błąd API" od „brak danych"), min. na load()/delete/setStatus | JS + backend | **M** | ~15 JS + `db.py`/checklists suppliers `except`; RD-4, użytkownik ufa pustej tabeli. |
| 8 | Decyzja właściciela + usunięcie reliktów forka (templates, checklists, suppliers*, doc_types, duplicates, search-section, versions, help §8, `ALL_MODULES`) | szablon + backend | **M/L** | H8/H10; martwe ekrany mylą i zwiększają powierzchnię utrzymania. |
| 9 | Walidacja wymiaru odstającego w `3d_marm` + escaping `innerHTML` w `palviz_batch` | JS | **S** | RD-5 (box 0×0×0) + RD (XSS/layout). |
| 10 | Zvendorować CDN (React/Babel w report, Chart.js w scorecard/api_costs) jak `model-viewer` | szablon | **S/M** | H1/H9; offline/CSP cicho gubi główną treść. |

---

## 7. Co jest zrobione dobrze (chronić przy refaktorze)

- **Fundament powłoki (`base.html`):** `window.apiFetch` z jednolitym surfacingiem błędów (:50), toast z escapem HTML (:381), `confirmModal` z Esc + klik-tła + wariant danger (:400), flash→toast (:326), overlay „Aktualizacja w toku" z pollingiem `/health` i auto-reloadem (:515), skip-link, `aria-current`. **To kanoniczne wzorce — reszta apki powinna do nich dążyć.**
- **`history.html`:** mapowanie enum→PL (`spill()`/`sevpill()`), `esc()` w renderze, eksport z disabled+spinner (:731 — wzorzec double-submit), filtry w URL, pełna a11y.
- **`library.html`:** baner „dane nieaktualne" z powodem, toast tylko przy przejściu w błąd, confirmModal danger na reset tokena — **wzorzec widoczności statusu**.
- **`suppliers_master.html` / `email_settings.html`:** konsekwentne tokeny + dark-mode, inline-edit z disabled+busy+toast, hasło nigdy nie wraca z serwera — **wzorzec formularza**.
- **`artwork_3d_batch.html`:** pasek postępu z `done/total`+`step_label` — najlepszy feedback async w klastrze 3D, do powielenia w renderach single.
- **`admin_palviz_push.html` / `artwork_render_coverage.html`:** baner „push wyłączony" z instrukcją, retry gated podwójnie (UI + `require_role`), `busy[id]` blokuje double-submit, pełna obsługa stanów pusty/loading/błąd z retry.
- **Bezpieczeństwo:** scoping plików po serwerze (`palviz._serve` sprawdza `uid_`/`can_see_all`), auto-escaping Jinja + `esc()`/`_safeHex()` w JS wstawiającym HTML, import CSV materiałów z per-wierszowym rollback + raportem odrzuconych (`app.py:12082`), `normalize_ref()` przed porównaniem REF.
- **Strony błędu 404/500:** bez tracebacku, po polsku, z akcją wyjścia.
- **Model „propozycja, nie wyrok"** w 3D (orientacja/prefill MARM jako sugestia z akcją „Użyj", reset akceptacji po ręcznej zmianie) — zgodny z założeniem uczenia.

---

## 8. Do sprawdzenia ręcznie (nieoceniane statycznie)

1. **Gating importu MARM serwerowo** — jako `user` POST `/api/materials/marm-import` → oczekiwane 403 (RD-9).
2. **CSRF na POST admina** — `admin.html:200,226` zapis klucza → Network 400 jeśli route ma `@csrf_protect`?
3. **`admin.html:390` `loadRoleModules`** — ReferenceError w konsoli przy wejściu na /admin?
4. **`artwork_kpi` miesiąc** — `strftime("%B %Y")` daje „September" czy „wrzesień" (locale)?
5. **Wygaśnięcie sesji** — otwarty formularz profilu → wygasić → Zapisz → utrata danych?
6. **`/versions` i `/duplicates`** — czy mają jeszcze dane w forku, czy zawsze pusty stan?
7. **Batch 3D** — zamknij kartę, wróć na `/artwork/3d/batch` → czy odzyskasz `job_id`? (obietnica „możesz wrócić").
8. **Spójność badge wariantu 3D** — ten sam REF w wyszukiwarce vs Pokrycie 3D vs panel push → identyczna PL etykieta?
9. **Klasy `alert-ok`/`alert-err`** w `app.css` (profile.showMsg) — istnieją?
10. **Eksport JSONL** (activity:138) — pobiera plik czy renderuje w karcie?

---

*Raport wygenerowany automatycznie (6 równoległych audytów read-only). Nie zmieniono żadnego kodu. Oczekuję decyzji, które pozycje wdrożyć.*
