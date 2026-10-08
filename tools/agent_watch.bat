@echo off
REM ============================================================================
REM  Auto-dosyłka podglądów artworków z Z:\ do chmury DocCompare (tryb ciągły).
REM
REM  Co robi: co --interval sekund sprawdza, których masterów (potwierdzonych/
REM  grupowych) brakuje w chmurze i AUTOMATYCZNIE wysyła je z Z:\ — bez klikania.
REM  Uruchom na maszynie, ktora widzi Z:\ (najlepiej zawsze wlaczonej / serwerze).
REM
REM  KONFIGURACJA — uzupelnij 3 wartosci ponizej:
REM ============================================================================
set BASE_URL=https://compare.example.com
set DC_USER=ZMIEN_NA_LOGIN_MANAGERA
set ROOT=Z:\ISO\WZORY ETYKIET I INSTRUKCJI

REM Haslo: ustaw zmienna srodowiskowa DOCCOMPARE_PASS (bezpieczniej niz wpisywac tu).
REM   - w Harmonogramie zadan: dodaj zmienna srodowiskowa konta, albo
REM   - odkomentuj ponizsza linie (mniej bezpieczne, haslo jawne w pliku):
REM set DOCCOMPARE_PASS=twoje_haslo

cd /d "%~dp0"
python scan_artwork_index.py --base-url %BASE_URL% --user %DC_USER% --root "%ROOT%" --watch --interval 120

REM Jednorazowa szybka dosylka (bez trybu ciaglego): zamien --watch --interval 120 na --only-upload
pause
