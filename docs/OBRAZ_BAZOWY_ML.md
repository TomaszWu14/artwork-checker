# Obraz bazowy ML — szybszy deploy (Poziom 0)

> Cel: skrócić deploy z **7-8 min do ~1 min** bez zmiany architektury aplikacji.
> Pomysł: ciężką, wolno instalującą się warstwę (torch + ML/OCR/CV) zbudować
> **raz** jako osobny obraz bazowy w registry, a właściwy obraz aplikacji niech
> tylko dokłada kod (`COPY . .`).

## Dlaczego deploy trwa 7-8 min

W oryginalnym `Dockerfile` każdy build instaluje od zera: CPU-PyTorch (~1 GB),
`paddlepaddle`, `easyocr`, `doctr`, `surya`, `transformers`, `ultralytics` oraz
dwa pakiety z `git+https://` (LightGlue, GroundingDINO, które trzeba sklonować).
Jeśli Coolify nie reużywa cache warstw między deployami, ta warstwa liczy się
za każdym razem — stąd 7-8 min. **Porównywarka dokumentów jest lekka** (pdfplumber,
camelot, rapidfuzz, sklearn) — cały ciężar to artwork CV + opcjonalny neural OCR.

## Co jest w repo

| Plik | Rola |
|---|---|
| `Dockerfile` | **Obecny, monolityczny** — wciąż używany przez Coolify. Nie ruszamy go, dopóki nie wykonasz cut-overu (krok 4). |
| `Dockerfile.base` | Ciężki obraz bazowy: system + torch + całe `requirements.txt`. Bez kodu aplikacji. |
| `Dockerfile.slim` | Cienki obraz aplikacji: `FROM` obrazu bazowego + `COPY . .`. To na niego przełączasz Coolify. |
| `.github/workflows/ml-base-image.yml` | Buduje `Dockerfile.base` i wypycha do GHCR (`ghcr.io/tomaszwu14/doccompare-ml-base`). |

## Jak to wdrożyć (kolejność ma znaczenie!)

> ⚠️ **Nie przełączaj Coolify na `Dockerfile.slim`, zanim obraz bazowy nie będzie
> w GHCR i serwer Coolify nie potrafi go pobrać.** Inaczej build padnie na
> `FROM ghcr.io/...` (brak obrazu).

### 1. Opublikuj obraz bazowy
- Wejdź w **Actions → „Buduj obraz bazowy ML" → Run workflow** (gałąź `main`).
- Workflow zbuduje `Dockerfile.base` (~8-10 min) i wypchnie tagi `:latest` oraz `:<sha>`
  do `ghcr.io/tomaszwu14/doccompare-ml-base`.
- (Po zmergowaniu tego PR-a workflow odpali się też automatycznie, bo `Dockerfile.base`
  pojawia się na `main`.)

### 2. Udostępnij pakiet dla Coolify (jednorazowo)
Najprościej — **ustaw pakiet jako publiczny**:
- GitHub → profil/organizacja → **Packages** → `doccompare-ml-base` →
  **Package settings** → **Change visibility → Public**.

Alternatywnie (jeśli ma zostać prywatny) — dodaj w Coolify **Registry credentials**
do `ghcr.io` (login = nazwa użytkownika GitHub, hasło = Personal Access Token z
zakresem `read:packages`).

### 3. Zweryfikuj, że da się pobrać
Na hoście Coolify:
```bash
docker pull ghcr.io/tomaszwu14/doccompare-ml-base:latest   # musi przejść
```

### 4. Przełącz Coolify na cienki obraz
W ustawieniach aplikacji w Coolify zmień **Dockerfile location** z `Dockerfile`
na **`Dockerfile.slim`** i wykonaj deploy. Od teraz deploy zmian w kodzie to
praktycznie samo `COPY . .` → ~1 min.

> Wariant bez UI: zamiast wskazywać `Dockerfile.slim`, możesz po weryfikacji
> podmienić zawartość `Dockerfile` na zawartość `Dockerfile.slim` (Coolify domyślnie
> buduje `Dockerfile`). Wtedy nie trzeba nic klikać w Coolify.

## Ważna zasada: zmiana zależności = przebuduj bazę NAJPIERW

`Dockerfile.slim` **nie instaluje** pakietów — bierze je z obrazu bazowego.
Dlatego po każdej zmianie `requirements.txt`:

1. zmerguj zmianę `requirements.txt` do `main` → workflow „Buduj obraz bazowy ML"
   odpali się automatycznie (path filter) i wypchnie nowy `:latest`,
2. dopiero potem deployuj aplikację (pobierze świeży obraz bazowy).

Aby uniknąć „dryfu" (kod oczekuje pakietu, którego baza jeszcze nie ma), w razie
wątpliwości przypnij w Coolify konkretny tag SHA bazy (build-arg
`ML_BASE_IMAGE=ghcr.io/tomaszwu14/doccompare-ml-base:<sha>`) i podbijaj go świadomie.

## Rollback

Wszystko jest odwracalne: wystarczy w Coolify wskazać z powrotem `Dockerfile`
(monolityczny) i zdeployować — wraca stare, samowystarczalne zachowanie.

## Co dalej (poza Poziomem 0)

To rozwiązuje **czas deployu**. Jeśli celem jest też **odchudzenie RAM webu** i
stabilność (brak 2 GB modeli w procesie HTTP), kolejne kroki to RQ + worker
(`PLAN_REFAKTORU.md` #1) i wydzielenie serwisu ML (#2).
