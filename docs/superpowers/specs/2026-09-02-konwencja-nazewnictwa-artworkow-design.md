# Konwencja nazewnictwa plików artworków — dokument docelowy

**Data:** 2026-09-02
**Cel:** jednolita, docelowa konwencja nazw plików masterów artworków ACME —
podstawa do (1) uporządkowania bazy (~31k plików) przez zespół oraz (2) wymogu
wysyłanego dostawcom.
**Status:** ustalenie (dokument tekstowy). Nie zmienia kodu — opisuje reguły,
których od teraz trzymamy się przy nazywaniu i przyjmowaniu plików.

---

## 1. Wzorzec nazwy

```
REF_poziom_EAN_Rev.NN_DD-MM-RRRR[_NazwaHandlowa].pdf
```

Pole `NazwaHandlowa` (§6a) jest **opcjonalne** — dokładamy je tylko, gdy trzeba
rozróżnić pliki, które inaczej miałyby identyczną nazwę (rebrand bez podbicia
rewizji). Standardowo pliku nie ma.

Przykłady:

```
805016_pouch_5900010800810_Rev.02_14-05-2026.pdf                 ← standard
805016_pouch_5900010800810_Rev.02_14-05-2026_AlphaTex.pdf        ← rebrand (patrz §6a)
```

- Stała kolejność pól.
- `_` (podkreślnik) jest **jedynym** separatorem między polami.
- Rozszerzenie zawsze małymi literami: `.pdf`.

## 2. Pola

| # | Pole | Format | Uwagi |
|---|------|--------|-------|
| 1 | **REF** | numer/kod REF produktu, np. `805016`, `GS1710123-SS` | Klucz łączący z `material_master`. Zawsze na początku → sortowanie po nazwie = sortowanie po produkcie. Rękawice **też mają REF** (koniec dawnego wyjątku „rodzaj + rozmiar bez REF"). |
| 2 | **poziom** | pełne słowo z listy (§3) | Rozdziela pouch/box/carton tego samego REF-u. **Bez skrótów** (`CS`, `crt`) — patrz §5. |
| 3 | **EAN** | 13 cyfr, albo `NO-EAN` | Kopiowany **z tabelki wewnątrz artworka**, nigdy przepisywany z pamięci (§6). Gdy poziom nie ma EAN → `NO-EAN`. |
| 4 | **Rewizja** | `Rev.NN` (dwie cyfry) | np. `Rev.00`, `Rev.02`. Musi być widoczna w nazwie — bez niej nie wiadomo, który plik jest aktualny (§7). |
| 5 | **Data zatwierdzenia rewizji** | `DD-MM-RRRR` | Data zatwierdzenia bieżącej rewizji, np. `14-05-2026`. Myślniki, nie kropki (§8). |
| 6 | **Nazwa handlowa** *(opcjonalne)* | `CamelCase`, bez spacji i znaków | Tylko gdy odróżnia pliki inaczej identyczne (rebrand bez zmiany rewizji) — §6a. |

## 3. Lista dozwolonych poziomów opakowania (słownik zamknięty)

Pełne słowa, małymi literami, wielosłowowe łączone `_`:

```
pouch, box, carton, carton_sticker, carton_print, box_print,
sticker, print, label, etykieta, tyvek, blister, insert, ulotka, instrukcja, ifu
```

Nowego poziomu nie wymyślamy w locie — dopisujemy najpierw tutaj, potem używamy.

## 4. Placeholder braku EAN

Gdy dany poziom **nie ma** kodu EAN, w polu #3 wpisujemy dokładnie:

```
NO-EAN
```

**Nigdy** ciągu zer (`00000000000` / `0000000000000`) — trzynaście zer ma
poprawną sumę kontrolną EAN-13 i zostałoby odczytane jako prawdziwy kod
(cichy fałsz). Placeholder musi być tekstem, którego nie da się pomylić z kodem.

## 5. Zasady twarde (co „haczy" i jest zakazane)

- **Bez skrótów poziomu.** `carton_sticker`, nie `CS`. Skrót = poziom
  nierozpoznany przez system. Dłuższa forma jest bezpieczniejsza niż krótsza.
- **Bez znaków niewidzialnych i specjalnych.** Zwłaszcza „miękki myślnik"
  (soft hyphen, U+00AD) wklejany czasem zamiast zwykłego `-` — wygląda jak
  myślnik, ale cicho psuje odczyt REF. Dozwolone znaki: litery A–Z/a–z, cyfry,
  `_`, `-`, `.`. Nic poza tym.
- **`_` jest bezpieczny** jako separator — nie „haczy". Zakaz dotyczy znaków
  spoza dozwolonego zbioru, nie podkreślnika.
- **Bez licznika kopii** typu `(2)` w nazwie docelowej.

## 6. Skąd bierzemy EAN

Kod EAN w nazwie **kopiujemy z tabelki wewnątrz artworka** (te kody są
prawidłowe), nigdy nie przepisujemy z pamięci. Powód: system przyjmuje EAN
tylko z poprawną sumą kontrolną — literówka nie daje błędu, tylko **po cichu
znika** z odczytu. Kopiowanie 1:1 z artworka eliminuje ten problem.

> Uwaga na poziomy z **więcej niż jednym EAN** (np. sztuka vs multipak):
> w nazwie zapisujemy EAN właściwy dla danego poziomu opakowania. Pełny komplet
> kodów pozostaje w tabelce artworka / indeksie — nazwa nie musi ich unieść
> wszystkich.

## 6a. Zmiana nazwy handlowej (rebrand) i pole opcjonalne

Produkt bywa przemianowany (np. `ALPHATEX` → nowa marka). Zmiana nazwy handlowej
to zmiana artworku — **normalnie podbija rewizję** i wtedy stary/nowy plik różnią
się `Rev.NN` + datą; pole nazwy **nie jest potrzebne**:

```
805016_pouch_5900010800810_Rev.01_02-01-2025.pdf   ← stara nazwa
805016_pouch_5900010800810_Rev.02_14-05-2026.pdf   ← nowa nazwa (rewizja podbita)
```

**Ale rewizja czasem się NIE zmienia przy zmianie nazwy.** Wtedy REF/poziom/EAN/
Rev.NN są identyczne i bez dodatkowego pola dwa pliki miałyby **tę samą nazwę
= kolizja**. W takim i **tylko takim** przypadku dokładamy `NazwaHandlowa` jako
ostatnie pole, żeby je rozróżnić i żeby od razu było widać, co się zmieniło:

```
805016_pouch_5900010800810_Rev.02_14-05-2026_BetaSafe.pdf
805016_pouch_5900010800810_Rev.02_14-05-2026_AlphaTex.pdf
```

Reguły pola: `CamelCase`, bez spacji i znaków specjalnych (marki mają spacje i
znaki — `elastoDERM F-IV` — których w nazwach plików unikamy; zapisujemy je
zbite, np. `ElastoDermFIV`). Pole jest opcjonalne — dodajemy je wyłącznie do
rozróżnienia, nie do każdego pliku.

## 7. Rewizje (reguła biznesowa)

- **Zmiana artworku = globalnie.** Przy zmianie danego rodzaju wyrobu
  podnosimy rewizję dla **wszystkich** istniejących REF tego rodzaju.
- **Nowy rozmiar = bieżąca rewizja.** Wprowadzenie nowego rozmiaru produktu
  robimy w aktualnie obowiązującej rewizji (nie podbijamy numeru).

Rewizja w nazwie (`Rev.NN`) jest tym, co pozwala wybrać aktualny plik bez
otwierania — dlatego jest obowiązkowa.

## 8. Format daty

`DD-MM-RRRR` (dzień-miesiąc-rok, myślniki). Kropek nie używamy w dacie, bo
kropka w nazwie pliku koliduje z rozszerzeniem `.pdf` oraz z `Rev.02`, a
myślnik nie. Data odzwierciedla **zatwierdzenie bieżącej rewizji** — jest
odporna na kopiowanie pliku (daty systemowe na dysku sieciowym zmieniają się
przy każdym kopiowaniu i nie są wiarygodne).

## 9. Wymóg dla dostawców

Dostawcy przysyłają artworki do sprawdzenia nazwane **dokładnie tak, jak pliki,
które od nas dostają**. Mogą dopisać własny sufiks **na końcu** nazwy, po
myślniku, dla swoich potrzeb — nie ruszając pól 1–5:

```
805016_pouch_5900010800810_Rev.02_14-05-2026-ACME123.pdf
                                              ^^^^^^^^ sufiks dostawcy (dozwolony)
```

### Gotowy zapis do wysłania dostawcy (EN)

> Please name every artwork file **exactly** as the file we send you, using the
> pattern `REF_level_EAN_Rev.NN_DD-MM-RRRR.pdf`
> (e.g. `805016_pouch_5900010800810_Rev.02_14-05-2026.pdf`). Some files carry an
> extra commercial-name field at the end (e.g. `…14-05-2026_AlphaTex.pdf`) —
> keep it exactly as sent.
> Use full packaging-level words (`pouch`, `box`, `carton`, `carton_sticker`…),
> not abbreviations. If a level has no EAN, write `NO-EAN`. Do not use spaces or
> special characters — only letters, digits, `_`, `-`, `.`. You may append your
> own suffix at the **end** after a hyphen (e.g. `…14-05-2026-yourref`), but do
> not change the five fields.

## 10. Świadome uproszczenia / poza zakresem

- Ten dokument **nie** zmienia parsera ani nie dokłada walidatora — to
  ustalenie tekstowe (droga A). Jeśli później zechcemy egzekwować konwencję
  automatycznie przy imporcie (wykrywać soft hyphen, nieznany poziom, brak REF)
  — to osobny, wdrożeniowy krok, do zaplanowania oddzielnie.
- Parser dziś **nie odczytuje pola daty** z nazwy — data służy ludziom i
  odporności na kopiowanie. Gdyby data miała być czytana maszynowo, parser
  trzeba będzie douczyć (osobne zadanie).
