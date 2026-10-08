# Proces Zakupy → Transport → Odprawa → Rozliczenie — specyfikacja w aplikacji

> Dokument roboczy. Powstaje krok po kroku na podstawie `Proces_zakupyflow2.xlsx`.
> Spisuje, **co aplikacja robi na każdym kroku** procesu (27 kroków, 4 obszary).

## Założenia fundamentalne

- **Wspólny klucz** łączący Zakupy i Transport: **Numer zamówienia (PO) + numer kontenera**.
  PO tworzą Zakupy (krok 1), Transport dokleja później kontener, dostawę 18\*\*\*\*, FO, ETD i status.
  Jeden rekord żyje przez cały proces.
- **Miejsce w aplikacji:** nowy moduł **„Kolejka / Tablica zleceń"** — wspólna tablica widoczna dla
  obu działów, zastępuje dotychczasowy excel na Sharepoint (kroki 10, 16, 17, 18).

## Integracje / zależności do zaprojektowania osobno

- ⚠️ **SAP BW (kostka OLAP)** — źródło „prawdy" do porównania w kroku 4. Apka pobiera dane
  zamówienia bezpośrednio z połączonej kostki SAP BW (nie z PDF, nie z ręcznego excela).
- **Biblioteka wzorców artworku** — zatwierdzone wzorce per produkt/REF do porównania w kroku 8.
- **Powiązanie Artwork ↔ Proforma** — LOT i daty drukowane na opakowaniu (artwork) weryfikowane
  względem danych z proformy (krok 3), bo to tam te wartości występują.

---

## BLOK A — ZAKUPY (kroki 1–8)

### Krok 1 — Stworzenie zamówienia
- Rekord PO w module „Kolejka" tworzony **ręcznie lub przez upload z excela**.
- Aplikacja przechowuje **tylko link do folderu na dysku Z** (pliki zostają na Z).
- Pola startowe: `nr_zamowienia` (klucz), `data_utworzenia`, `status = "Utworzone"`, link do folderu Z.

### Krok 2 — Wysyłka zamówienia do dostawcy
- Apka **rejestruje wysyłkę** (przycisk „Wysłane do dostawcy" + data). Mail wysyłany ręcznie z Outlooka.
- Adres dostawcy poza apką. Status → „Wysłane do dostawcy".

### Krok 3 — Proforma od dostawcy
- **Upload pliku proformy** do rekordu PO (apka musi mieć plik do porównania w kroku 4).
- **Jedna proforma na PO** — najnowszy plik nadpisuje poprzedni (bez historii wersji).
- Status → „Proforma otrzymana".

### Krok 4 — Porównanie proformy z SAP ⭐ (rdzeń DocCompare)
- Źródło prawdy: **kostka SAP BW (live)** — NIE PDF zamówienia (PO mógł się zmienić w ME22N).
- Kontrole automatyczne (wszystkie):
  - Zgodność **LOT**
  - **Data produkcji = data wysyłki**
  - **Ważność ≥ 5 lat** (alarm gdy krótsza)
  - **REF / ilość / cena / wartość netto** (standardowe porównanie pozycji)
- Wynik napędza bramkę jakości w kroku 6.

### Krok 5 — Transit time → ME22N
- Apka **liczy datę dostawy automatycznie**: `data_dostawy = ETD + transit_time`. Podpowiedź do ME22N.
- Transit time z **konfigurowalnej tabeli per dostawca/port**.

### Krok 6 — Odesłanie potwierdzenia do dostawcy
- Wraca **podpisana proforma**. Apka rejestruje odesłanie (status → „Potwierdzone do dostawcy").
- **Bramka jakości:** apka **blokuje** status „Potwierdzone" przy błędach krytycznych z kroku 4
  (niezgodny LOT, ważność < 5 lat, różnice cen).

### Krok 7 + 8 — Artworki 🟢 (moduł Artwork)
- (7) Dostawca przysyła wydrukowane artworki do potwierdzenia → upload przy rekordzie PO.
- (8) Weryfikacja: porównanie z **zatwierdzonym wzorcem w bibliotece** + kontrole:
  - Kody kreskowe **EAN-13 / GS1** (suma kontrolna, GTIN/LOT/EXP)
  - **LOT / daty na opakowaniu** — porównane **z danymi z proformy** (krok 3)
  - **Zgodność grafiki ze wzorcem** (pixel/OCR diff)
  - **REF / rewizja / tłumaczenia** (20–30 języków na opakowaniu medycznym)
- Status → „Artwork zatwierdzony" / „Artwork — uwagi".

---

## BLOK B — TRANSPORT (kroki 9–19)

### Krok 9 — Lista ETD od Marci (Oddział Chiny) vs SAP
- **Marcia wpisuje ETD wprost do Kolejki** (Oddział Chiny = użytkownik apki). Excel znika.
- Apka porównuje ETD z kostką SAP BW → **alert + lista braków** ETD do uzupełnienia.

### Krok 10 — Tygodniowa lista do specjalistów ⭐ (serce wspólnego pola)
- Wspólne pola w Kolejce wypełniane przez specjalistów:
  - **Rodzaj transportu** (40HQ / 20DV / LCL) — lista wyboru
  - **Zgoda na wypłynięcie** — flaga + kto/kiedy
  - **Planowane ETD**
  - **Uwagi / priorytet**
- **Zgoda na wypłynięcie** zatwierdzana tylko przez rolę **„specjalista"** (zapis kto+kiedy).
- Łańcuch maili (Marcia→Asystenci→Specjalista→Transport) zastąpiony wspólnym widokiem.

### Krok 11 — Wysyłka zleceń do spedycji
- Apka **generuje zlecenie spedycyjne** z zatwierdzonych PO (tylko ze zgodą na wypłynięcie).
- **Słownik spedytorów/przewoźników** + przypisanie do PO (sealine pod tracking w kroku 12).

### Krok 12 — Raport ETD: terminowo / opóźnienie 🟢 (tracking SafeCube)
- **Hybryda**: auto-tracking kontenera (Sinay Container Tracking API v2) + ręczna korekta gdy brak danych.
- **Auto-liczenie opóźnienia** = realny ETD − planowany ETD; **próg alertu** (np. >3 dni).
- Osobne pole na **potwierdzenie agenta**.

### Krok 13 — Oczekiwanie na dokumenty wysyłkowe
- Upload (packing list, proforma finalna, faktura za próbki) → **apka rozpoznaje typ**.
- Porównanie **packing list vs proforma vs SAP** (ilości, pozycje, wagi) przed odprawą.

### Krok 14 — Folder kontenera + dokumenty do odprawy ⭐ (wchodzi KONTENER)
- Apka tworzy **rekord kontenera**; przypina do niego PO i dokumenty odprawowe
  (faktura, tłumaczenie, BOL, certyfikaty — bez faktury transportowej).
- Relacja **jeden kontener = jedno lub wiele PO** (1:N).
- Tu domyka się wspólny klucz **PO + kontener**.

### Krok 15 — Utworzenie dostawy 18\*\*\*\* w SAP
- Numer dostawy **18\*\*\*\* wpisywany ręcznie** przez asystenta na rekord PO/kontenera.
- **Pełna historia zmian dat dostawy** (kto, kiedy, poprzednia → nowa, który krok) — audyt.

### Krok 16 — Przypisanie 18\*\*\*\* ↔ kontener
- **Sharepoint całkowicie zastąpiony Kolejką** — przypisanie natywne w apce.
- **Asystenci edytują, Transport podgląd** (read-only).

### Krok 17 — Transport zakłada FO (zlecenie frachtowe)
- Transport **wpisuje FO + status w Kolejce** (Sharepoint znika).
- **Predefiniowana lista statusów FO** (założone → w realizacji → rozliczone).

### Krok 18 — Aktualizacja dat w harmonogramie (Kolejka)
- **Apka = główny harmonogram** dla całego Acme; daty auto z trackingu.
- SAP aktualizowany **ręcznie** (bez integracji zapisu do SAP).
- Widok: **tabela sortowana/filtrowana po dacie**.

### Krok 19 — Wysłanie dokumentów do odprawy celnej
- **Checklista kompletności** (faktura + tłumaczenie + BOL + certyfikaty + faktura transportowa)
  → blokada wysyłki przy brakach, potem wysyłka/eksport paczki.
- **Tłumaczenia: AI (Claude API) + słownik terminów celnych** (HS codes, nazwy medyczne) dla spójności.

## BLOK C — ODPRAWA CELNA (kroki 20–21)

### Krok 20 — Draft SADu od agencji celnej
- Apka **porównuje draft SAD z fakturą / proformą / SAP** (wartości, ilości, kody towarowe, kraj pochodzenia).
- **Rejestruje decyzję** (potwierdzony / błędy) + listę rozbieżności; mail do agencji ręczny.

### Krok 21 — PZC + faktury celne → księgowość
- Upload PZC + faktura za odprawę + należności celno-podatkowe do kontenera.
- **Oznaczenie „do księgowości"** (mail ręczny). **Bez wyciągania kwot** cła/VAT (tylko dokumenty).

## BLOK D — ROZLICZENIE (kroki 22–27)

### Krok 22 — Potwierdzenie dostawy przez magazyn
- **Magazyn potwierdza wprost w apce** (magazyn = użytkownik) → status „Dostarczone".
- **Ręczna zgoda na rozliczenie** (świadome odhaczenie przed księgowaniem).

### Krok 23 — Rozliczenie faktury (FIORI + MIR7)
- Ręcznie wpisywany **numer MIR7 + status** (rozliczane / rozliczone).
- 🔑 **Kluczowe pole: numer rozliczenia frachtu** — apka trzyma go razem z MIR7 (wracają w krokach 24–25).

### Krok 24 — Faktura z numerami rozliczenia na dysk Z
- Apka **automatycznie nadrukowuje** numer rozliczenia frachtu + MIR7 na PDF faktury (koniec ręcznego wklejania).
- Finalna faktura: **w apce + link do Z**.

### Krok 25 — Potwierdzenie rozliczenia transportu
- **Powiadomienie w apce** do Zakupów („Transport rozliczony") zamiast wiadomości na Teams.
- 🔔 Wprowadza **centrum powiadomień w apce** dla całego flow (statusy, alerty, zgody, rozliczenia).

### Krok 26 — Złączenie dokumentów w jeden zestaw
- Apka **automatycznie składa komplet**: faktura zakupowa + transportowa + BOL + SAD.
- Zestaw trafia **do folderu** (komplet w jednym miejscu, spójnie z folderem kontenera).

### Krok 27 — Rozliczenie MIR7: porównanie wartości (domknięcie pętli)
- Porównanie wartości **dzieje się już wcześniej** (krok 4, 13, 20) — krok 27 **korzysta z wcześniejszych wyników**, nie powtarza analizy.
- **Bramka**: rozbieżność powyżej **tolerancji cenowej %** (z profilu dostawcy) **blokuje rozliczenie**.

---

## Funkcje przekrojowe (cross-cutting)

### Wspólny rekord i klucz
- Jeden rekord PO żyje od kroku 1 do 27. Klucz **PO + kontener** (kontener: rekord-rodzic, 1:N do PO).
- Statusy (lifecycle): Utworzone → Wysłane do dostawcy → Proforma otrzymana → Zweryfikowane →
  Potwierdzone → Artwork zatwierdzony → ETD ustalone → Zatwierdzone do wypłynięcia →
  W spedycji → Wypłynęło → Dokumenty kompletne → W odprawie → Odprawione → Dostarczone →
  Rozliczane → Rozliczone.

### Role / użytkownicy (rozszerzenie obecnych user < manager < superuser < admin)
- **Asystenci Zakupów** — tworzą PO, wgrywają dokumenty, przypisują 18\*\*\*\*↔kontener.
- **Specjalista** — jedyny może zatwierdzić „zgodę na wypłynięcie" (krok 10).
- **Transport** — spedycja, FO, tracking, harmonogram, rozliczenie frachtu.
- **Oddział Chiny (Marcia)** — wpisuje ETD do Kolejki (krok 9).
- **Magazyn** — potwierdza dostawę w apce (krok 22).
- **Księgowość** — odbiera zestawy dokumentów, rozliczenia.

### Bramki jakości (gates)
- Krok 6 — blokada potwierdzenia przy błędach krytycznych porównania (krok 4).
- Krok 19 — blokada wysyłki do agencji przy niekompletnym komplecie dokumentów.
- Krok 27 — blokada rozliczenia przy rozbieżności wartości > tolerancja.

### Integracje
- **SAP BW (odczyt, kostka OLAP)** — porównania (krok 4), opcjonalnie status dostawy (krok 22).
  Apka **nie zapisuje** do SAP (harmonogram po stronie apki, krok 18).
- **Tracking kontenera** — Sinay Container Tracking API v2 (krok 12), hybryda auto + ręczna korekta.
- **Claude API** — tłumaczenia dokumentów celnych + słownik terminów (krok 19).
- **Resend** — opcjonalnie maile (większość komunikacji rejestrowana, wysyłka ręczna).

### Centrum powiadomień
- Jeden system powiadomień w apce: zmiany statusów, alerty opóźnień (krok 12), zgody (krok 10),
  rozliczenia (krok 25). Zastępuje rozproszoną komunikację mail/Teams w kluczowych punktach.

### Automatyzacje dokumentów
- Auto-rozpoznanie typu dokumentu (krok 13).
- Auto-nadruk numerów rozliczenia na PDF (krok 24).
- Auto-złożenie kompletu dokumentów do folderu (krok 26).
- Generowanie zlecenia spedycyjnego (krok 11).

---

## Implementacja — Etap 1: model danych + szkielet tablicy

> Decyzja: nowy moduł od zera (świadomie obok istniejących `shipments`/`transport_queue`,
> które można później połączyć). Nowa rola **specjalista** w hierarchii.

### Rola
- `constants.py`: dodano `UserRole.SPECJALISTA` na poziomie 2 (między user=1 a manager=3).
  Hierarchia: user(1) < **specjalista(2)** < manager(3) < superuser(4) < admin(5).

### Tabele (migrate_db.py + init_db w app.py)
- **`kolejka_kontenery`** — kontener (transport): numer, typ (40HQ/20DV/LCL), forwarder, sealine,
  porty, etd_plan/etd_real/eta, opóźnienie, potwierdzenie agenta, FO + status,
  **rozliczenie_frachtu**, status odprawy, link do folderu Z.
- **`kolejka_zlecenia`** — zlecenie/PO (klucz `nr_zamowienia`, FK `kontener_id` → 1:N):
  status (16-etapowy lifecycle), link Z, dane proformy (LOT + 3 daty → do artworku),
  `comparison_id`, transit_time, planowane_etd, data_dostawy, rodzaj_transportu,
  zgoda_wyplyniecie (+by/at), artwork_status, dostawa_18, mir7_number, zgoda_rozliczenie,
  priorytet, uwagi.
- **`kolejka_status_log`** — audyt zmian (status, daty) z numerem kroku procesu.
- **`kolejka_powiadomienia`** — centrum powiadomień (adresowanie po user_id lub roli).

### Lifecycle statusów (16)
utworzone → wyslane_do_dostawcy → proforma_otrzymana → zweryfikowane → potwierdzone →
artwork_zatwierdzony → etd_ustalone → zatwierdzone_do_wyplyniecia → w_spedycji → wyplynelo →
dokumenty_kompletne → w_odprawie → odprawione → dostarczone → rozliczane → rozliczone.

### UI
- Route **`/kolejka`** (`kolejka_page`) + szablon `templates/kolejka.html`:
  nagłówek, statystyki (zleceń / bez zgody / bez kontenera / rozliczone),
  filtry (szukaj + status + priorytet, Alpine.js), tabela z joinem kontenera, pusty stan.
- Nawigacja: pozycja **„🗂️ Kolejka zleceń"** w sekcji „🔁 Wspólne".

## Implementacja — Etap 2: tworzenie zleceń (PO)
- `POST /api/kolejka/zlecenia` — tworzenie ręczne (krok 1), obsługa duplikatu (409) i braku numeru (400).
- `POST /api/kolejka/zlecenia/import` — masowy import z Excela, elastyczne mapowanie nagłówków
  (PO/Dostawca/ETD/Priorytet/Uwagi), commit per wiersz, raport created/skipped.
- `DELETE /api/kolejka/zlecenia/<id>` — usuwanie (manager+).
- UI: przycisk „Nowe zlecenie" (modal), „Import z Excela", kolumna usuwania.

## Implementacja — Etap 3: upload proformy + porównanie (kroki 3-4)
- `POST /api/kolejka/zlecenia/<id>/proforma` — upload proformy (przechowywana na stałe),
  status → `proforma_otrzymana`.
- `POST /api/kolejka/zlecenia/<id>/compare` — porównanie proforma↔referencja przez istniejący
  silnik `compare_documents`; wynik do `comparisons`, podpięty (`comparison_id`),
  status → `zweryfikowane`. Bramka: wymaga proformy. ⚠️ Referencja: docelowo SAP BW (w toku),
  na razie wgrywana jako drugi plik.
- UI: kolumna „Proforma / Wynik" — upload, akcja porównania, badge ryzyka + link do `/history`.

## Implementacja — Etap 4: akcje statusu + oś czasu (lifecycle)
- `POST /api/kolejka/zlecenia/<id>/status` — zmiana statusu z walidacją + audyt (stara→nowa),
  mapowanie status→numer kroku.
- `GET /api/kolejka/zlecenia/<id>/historia` — oś czasu zmian z autorami.
- UI: status klikalny → modal „Szczegóły zlecenia" z selektorem statusu i wizualną osią czasu.

## Implementacja — Etap 5: zgoda na wypłynięcie (krok 10)
- `GET /api/kolejka/zlecenia/<id>` — pełny rekord do panelu szczegółów.
- `POST .../rodzaj-transportu` — 40HQ/20DV/LCL z walidacją.
- `POST .../zgoda` — DWIE bramki: **rola** (`require_role("specjalista")`) +
  **jakość** (blokada gdy porównanie = error/critical). → status `zatwierdzone_do_wyplyniecia`.

## Implementacja — Etap 6: kontener 1:N + FO/rozliczenie frachtu (kroki 14/17/23)
- `GET /api/kolejka/kontenery` — lista z liczbą zleceń.
- `POST .../<id>/kontener` — przypięcie (auto-tworzenie kontenera), 1:N.
- `PATCH /api/kolejka/kontenery/<id>` — FO, status FO, rozliczenie frachtu, sealine, daty, odprawa.

## Implementacja — Etap 7: tracking + opóźnienie (krok 12)
- `POST /api/kolejka/kontenery/<id>/track` — auto-tracking (Sinay v2), realny ETD/ETA.
- `_kolejka_recompute_delay` — opóźnienie = etd_real − etd_plan, alert > 3 dni.
- Hybryda: ręczna korekta ETD też przelicza opóźnienie; potwierdzenie agenta.

## Implementacja — Etap 8: centrum powiadomień (krok 25)
- `_kolejka_notify` — powiadomienia (user/rola/broadcast).
- Generowane przy: porównaniu error/critical, zgodzie, alercie opóźnienia, rozliczeniu.
- `GET /api/kolejka/powiadomienia` + oznaczanie odczytu; dzwonek 🔔 z licznikiem w UI.

### Pozostałe (większe, osobne)
- **Integracja SAP BW** (odczyt) jako źródło referencji do porównań (krok 4) — wymaga połączenia.
- Generowanie zlecenia spedycyjnego (krok 11), checklista odprawy (krok 19),
  auto-nadruk numerów na PDF (krok 24), złożenie kompletu (krok 26) — automatyzacje dokumentów.
