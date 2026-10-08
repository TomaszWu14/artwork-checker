"""
English translation layer for DocCompare v6.
Applied as an after_request HTML post-processor when session lang == 'en'.

Approach:
  - Split rendered HTML on <script>/<style> blocks (never touch those).
  - In HTML-only parts, replace text nodes (between > and <) and
    visible attributes (placeholder, aria-label, title).
  - Phrases sorted longest-first to prevent partial-match clobbering.
"""

import re

# ---------------------------------------------------------------------------
# PHRASE MAP  Polish → English
# Each value replaces its key EXACTLY (case-sensitive).
# Sorted longest-first at module load so longer phrases match before
# their sub-phrases (e.g. "Błąd serwera" before "Błąd").
# ---------------------------------------------------------------------------
_RAW = {
    # ── Navigation / sidebar ────────────────────────────────────────────────
    "Przejdź do treści": "Skip to content",
    "Porównanie dokumentów": "Document comparison",
    "Agent proforma": "Proforma agent",
    "Odprawa celna / SAD": "Customs clearance / SAD",
    "Historia porównań": "Comparison history",
    "Dostawy / Śledzenie": "Deliveries / Tracking",
    "Checklisty dokumentów": "Document checklists",
    "Kalendarz ETD/ETA": "ETD/ETA Calendar",
    "Awizacja / Dispatch": "Pre-advice / Dispatch",
    "Kolejka transportowa": "Transport queue",
    "Centrum danych": "Data centre",
    "Słownik tłumaczeń": "Translation dictionary",
    "Logi aktywności": "Activity logs",
    "Biblioteka plików": "File library",
    "Panel admina": "Admin panel",
    "Zgłoszenia rozbieżności": "Discrepancy reports",
    "Zakupy": "Purchasing",
    "Transport": "Transport",
    "Dostawy": "Deliveries",
    "Wspólne": "Common",
    "Magazyn": "Warehouse",
    "Produkty": "Products",
    "Dostawcy": "Suppliers",
    "Pomoc": "Help",
    "Wyloguj": "Log out",
    "Artwork": "Artwork",
    "KPI": "KPI",
    "Koszty API": "API costs",

    # ── Topbar / shell ──────────────────────────────────────────────────────
    "Rozwiń sidebar": "Expand sidebar",
    "Zwiń sidebar": "Collapse sidebar",
    "Powiadomienia": "Notifications",
    "🔔 Powiadomienia": "🔔 Notifications",
    "Oznacz jako przeczytane": "Mark as read",
    "Oznacz wszystkie jako przeczytane": "Mark all as read",
    "Brak nowych powiadomień 🎉": "No new notifications 🎉",
    "Brak powiadomień.": "No notifications.",
    "Brak powiadomień": "No notifications",
    "Ładowanie…": "Loading…",
    "Ładowanie...": "Loading...",

    # ── Maintenance overlay ─────────────────────────────────────────────────
    "Aktualizacja w toku…": "Update in progress…",
    "Aktualizacja w toku&hellip;": "Update in progress&hellip;",
    "Aplikacja zostanie automatycznie wznowiona": "The app will automatically resume",
    "po zakończeniu wdrożenia.": "after the deployment completes.",

    # ── Confirm modal ────────────────────────────────────────────────────────
    "Potwierdź": "Confirm",
    "Anuluj": "Cancel",

    # ── Document comparison page (analyze.html) ──────────────────────────
    "Wgraj dwa pliki PDF, wybierz typ dokumentu i uruchom analizę różnic.":
        "Upload two PDF files, select the document type and run the comparison.",
    "Nowe porównanie": "New comparison",
    "Dokument A": "Document A",
    "— źródłowy (np. PO)": "— source (e.g. PO)",
    "Wgraj Dokument A — kliknij lub przeciągnij plik PDF":
        "Upload Document A — click or drag a PDF file",
    "Przeciągnij plik lub kliknij": "Drag a file or click",
    "PDF do 25 MB": "PDF up to 25 MB",
    "Usuń plik A": "Remove file A",
    "Usuń plik B": "Remove file B",
    "Dokument B": "Document B",
    "— porównywany (np. PI/CI)": "— compared (e.g. PI/CI)",
    "Wgraj Dokument B — kliknij lub przeciągnij plik PDF":
        "Upload Document B — click or drag a PDF file",
    "Typ dok. A": "Doc type A",
    "Typ dok. B": "Doc type B",
    "Typ dokumentu A": "Document type A",
    "Typ dokumentu B": "Document type B",
    "Walidacja AI": "AI Validation",
    "Porównaj dokumenty": "Compare documents",
    "Wgraj oba dokumenty, aby uruchomić porównanie.":
        "Upload both documents to run the comparison.",

    # ── Document type options ───────────────────────────────────────────────
    "PO — Zamówienie": "PO — Purchase Order",
    "PI — Proforma": "PI — Proforma Invoice",
    "CI — Faktura handlowa": "CI — Commercial Invoice",
    "PL — Packing list": "PL — Packing List",
    "SAD — Dok. celny": "SAD — Customs document",
    "BL — Konosament": "BL — Bill of Lading",
    "WZ — Wydanie zewn.": "WZ — Goods Issue",
    "FV — Faktura": "FV — Invoice",
    "CMR — List przewozowy": "CMR — Consignment Note",

    # ── Loading / progress ──────────────────────────────────────────────────
    "Analiza dokumentów…": "Analysing documents…",
    "Analizuję dokumenty…": "Analysing documents…",
    "Ekstrakcja pól z PDF": "Extracting fields from PDF",
    "Dopasowanie pozycji i kwot": "Matching items and amounts",
    "Ocena ryzyka": "Risk assessment",

    # ── Risk levels ──────────────────────────────────────────────────────────
    "Wysokie ryzyko": "High risk",
    "Średnie ryzyko": "Medium risk",
    "Niskie ryzyko": "Low risk",
    "AI Risk Score": "AI Risk Score",

    # ── Counters ─────────────────────────────────────────────────────────────
    "Błędy krytyczne": "Critical errors",
    "Błędów krytycznych": "Critical errors",
    "Bez błędów": "No errors",
    "Ostrzeżenia": "Warnings",
    "Zgodne": "Matching",
    "Błędy": "Errors",
    "Błąd": "Error",

    # ── Result metadata ──────────────────────────────────────────────────────
    "Dostawca": "Supplier",
    "Pól w tabeli": "Fields in table",
    "Eksport PDF": "Export PDF",
    "Eksport Excel": "Export Excel",
    "Historia": "History",
    "Ocena AI": "AI Assessment",

    # ── Differences table ────────────────────────────────────────────────────
    "Tabela różnic": "Differences table",
    "Brak rozbieżności": "No discrepancies",
    "Dokumenty są zgodne we wszystkich sprawdzonych polach.":
        "Documents match in all checked fields.",
    "Pole": "Field",
    "Wartość A": "Value A",
    "Wartość B": "Value B",
    "Status": "Status",
    "Komentarz": "Comment",
    "Zamknij szczegóły pola": "Close field details",
    "Zamknij": "Close",

    # ── Chips / status badges ────────────────────────────────────────────────
    "Ostrzeżenie": "Warning",
    "Brak A": "Missing A",
    "Brak B": "Missing B",
    "Tylko A": "Only A",
    "Tylko B": "Only B",
    "Różnica": "Difference",

    # ── History page (history.html) ──────────────────────────────────────
    "Wszystkie porównania — wszyscy użytkownicy": "All comparisons — all users",
    "Twoje porównania": "Your comparisons",
    "Szukaj w historii porównań": "Search comparison history",
    "Szukaj pliku, PO, dostawcy, usera…": "Search file, PO, supplier, user…",
    "Wszystkie statusy": "All statuses",
    "Szukaj": "Search",
    "Wyczyść": "Clear",
    "Usuń zaznaczone": "Delete selected",
    "Usunąć zaznaczone rekordy?": "Delete selected records?",
    "Tak, usuń": "Yes, delete",
    "Data i czas": "Date & time",
    "Użytkownik": "User",
    "Dok. A": "Doc A",
    "Dok. B": "Doc B",
    "Różnic": "Diffs",
    "Raport": "Report",
    "Otwórz": "Open",
    "Brak historii porównań artworków": "No artwork comparison history",
    "Brak historii porównań": "No comparison history",
    "Spróbuj zmienić frazy wyszukiwania lub wyczyść filtry.":
        "Try changing the search terms or clear the filters.",
    "Wykonaj pierwsze porównanie dokumentów, aby zobaczyć historię.":
        "Run your first document comparison to see history here.",
    "Poprzednia strona": "Previous page",
    "Następna strona": "Next page",
    "← Poprzednia": "← Previous",
    "Następna →": "Next →",
    "Strona": "Page",
    "Raport porównania": "Comparison report",
    "Ładowanie raportu…": "Loading report…",
    "Łącznie:": "Total:",
    "rekordów": "records",
    "z": "of",

    # ── Report modal / overlay (history.html) ────────────────────────────
    "Przegląd": "Overview",
    "Pola nagłówkowe": "Header fields",
    "Pozycje towarowe": "Line items",
    "Pola tekst.": "Text fields",
    "Literówki": "Typos",
    "PROBLEMY KRYTYCZNE": "CRITICAL ISSUES",
    "Brak krytycznych problemów": "No critical issues",
    "Brak rozbieżności w tym module": "No discrepancies in this module",
    "Brak rozbieżności w polach tekstowych": "No discrepancies in text fields",
    "Brak literówek ani błędów I↔1": "No typos or I↔1 errors",
    "Ważność": "Severity",
    "Kategoria": "Category",
    "Opis": "Description",
    "Dok. A": "Doc A",
    "Dok. B": "Doc B",
    "Opis A": "Desc A",
    "Opis B": "Desc B",
    "Ilość A": "Qty A",
    "Ilość B": "Qty B",
    "Cena A": "Price A",
    "Cena B": "Price B",
    "Net A": "Net A",
    "Net B": "Net B",
    "Komentarz": "Comment",
    "Dokumenty zgodne": "Documents match",
    "Błędy krytyczne": "Critical errors",
    "rozbieżności": "discrepancies",
    "ostrzeżenia": "warnings",
    "błędów": "errors",

    # ── Artwork comparison ────────────────────────────────────────────────
    "Porównanie artworku": "Artwork comparison",
    "Porównaj artwork": "Compare artwork",
    "Historia artwork": "Artwork history",
    "Porównanie grafiki": "Graphics comparison",
    "Wgraj pliki master (A) — kliknij lub przeciągnij": "Upload master files (A) — click or drag",
    "Wgraj PDF dostawcy (B) — kliknij lub przeciągnij": "Upload supplier PDF (B) — click or drag",
    "Wgraj pliki PDF — kliknij lub przeciągnij": "Upload PDF files — click or drag",
    "Brak historii porównań artworków": "No artwork comparison history",
    "Weryfikacja kodów kreskowych": "Barcode verification",
    "Postęp porównania artworków": "Artwork comparison progress",
    "Postęp ekstrakcji": "Extraction progress",
    "Postęp weryfikacji pól": "Field verification progress",
    "Pobierz pełny raport HTML ze zdjęciami": "Download full HTML report with images",
    "Usuń raport": "Delete report",
    "Otwórz porównanie A↔B": "Open comparison A↔B",
    "Otwórz w nowej karcie": "Open in new tab",
    "Obróć w lewo": "Rotate left",
    "Obróć w prawo": "Rotate right",
    "Obróć wycinek o 90°": "Rotate crop 90°",
    "Powiększ oba (scroll też działa)": "Zoom both (scroll also works)",
    "Powiększ (+)": "Zoom in (+)",
    "Powiększ": "Zoom",
    "Podgląd": "Preview",
    "Zatwierdź wszystkie pola z automatycznie wykrytymi pozycjami i uruchom porównanie":
        "Approve all auto-detected fields and run comparison",
    "Edytuj nazwę strefy": "Edit zone name",
    "Nowa nazwa strefy": "New zone name",
    "Usuń strefę": "Delete zone",
    "Brak regionów — narysuj prostokąty": "No regions — draw rectangles",
    "scroll = zoom · drag = pan · klik tło lub Esc = zamknij":
        "scroll = zoom · drag = pan · click background or Esc = close",

    # ── Suppliers page ────────────────────────────────────────────────────
    "Baza dostawców": "Supplier database",
    "Brak danych dostawców": "No supplier data",
    "Nowy dostawca": "New supplier",
    "Profil dostawcy": "Supplier profile",
    "Kod dostawcy": "Supplier code",
    "Kraj": "Country",
    "Waluta": "Currency",
    "Tolerancja ceny": "Price tolerance",
    "Tolerancja ilości": "Qty tolerance",
    "Warunki płatności": "Payment terms",
    "Synonimy produktów": "Product synonyms",
    "Mapowanie kolumn": "Column mapping",
    "Szukaj dostawcy, kodu, kraju…": "Search supplier, code, country…",

    # ── Products / database ───────────────────────────────────────────────
    "Baza produktów": "Product database",
    "Baza REF — nazwy produktów": "REF database — product names",
    "Centralna baza kodów REF, EAN i taryf CN dla produktów ACME":
        "Central REF, EAN and CN tariff code database for ACME products",
    "Dodaj REF ręcznie lub zaimportuj z pliku CSV.":
        "Add REF manually or import from a CSV file.",
    "Szukaj nazwy / EAN / REF…": "Search name / EAN / REF…",
    "Szukaj nazwy, EAN, REF…": "Search name, EAN, REF…",
    "Szukaj REF lub nazwy…": "Search REF or name…",
    "Baza spedytorów, kierowców i historia powiadomień SMS":
        "Forwarder, driver database and SMS notification history",

    # ── Deliveries / shipments ────────────────────────────────────────────
    "Śledzenie kontenerów": "Container tracking",
    "Śledzenie": "Tracking",
    "Brak śledzonych kontenerów": "No tracked containers",
    "Brak kontenerów w kolejce": "No containers in queue",
    "Kolejka kontenerów": "Container queue",
    "Raport kontenerowy": "Container report",
    "Numer kontenera": "Container number",
    "Numer BL": "BL number",
    "Port załadunku": "Loading port",
    "Port rozładunku": "Discharge port",
    "Data załadunku": "Loading date",
    "Data przybicia": "Arrival date",
    "Data ETA": "ETA date",
    "Data ETD": "ETD date",
    "Armator / przewoźnik": "Carrier / shipping line",
    "Agent celny (uzupełnia spedytor)": "Customs agent (completed by forwarder)",
    "Bez zgody na wypłynięcie": "No sailing approval",
    "Cofnij zgodę": "Revoke approval",
    "Dodaj kierowcę": "Add driver",
    "Dodaj kierowcę klikając przycisk powyżej.": "Add a driver using the button above.",
    "Brak kierowców": "No drivers",
    "Brak spedytorów": "No forwarders",
    "Powiadom kierowcę": "Notify driver",
    "Awizacja kierowców · Magazyn": "Driver pre-advice · Warehouse",

    # ── Warehouse / transport ─────────────────────────────────────────────
    "Rejestr przyjęć": "Receipt register",
    "Data przyjęcia": "Receipt date",
    "Edytuj przyjęcie": "Edit receipt",
    "Usuń przyjęcie": "Delete receipt",
    "Stan kontenera, uszkodzenia, odchylenia...":
        "Container condition, damage, deviations...",

    # ── Calendar ─────────────────────────────────────────────────────────
    "Kalendarz ETD/ETA": "ETD/ETA Calendar",
    "Nawigacja kalendarza": "Calendar navigation",
    "Poprzedni miesiąc": "Previous month",
    "Następny miesiąc": "Next month",
    "Przejdź do dzisiaj": "Go to today",
    "Bieżący miesiąc": "Current month",

    # ── Checklists ────────────────────────────────────────────────────────
    "Checklisty dokumentów": "Document checklists",
    "Brak zdarzeń.": "No events.",
    "Brak zdarzeń": "No events",

    # ── SAD approvals ─────────────────────────────────────────────────────
    "Zatwierdź SAD": "Approve SAD",
    "Brak zgłoszeń SAD. Porównania z typem dokumentu SAD pojawią się tutaj automatycznie.":
        "No SAD submissions. Comparisons with document type SAD will appear here automatically.",

    # ── Data hub ──────────────────────────────────────────────────────────
    "Hub danych": "Data hub",
    "Centrum danych": "Data centre",
    "Centralne miejsce danych referencyjnych — produkty, dostawcy, artworki, śledzenie i słowniki.  🔁 Zakupy + Transport + Artworki":
        "Central reference data — products, suppliers, artwork, tracking and dictionaries.  🔁 Purchasing + Transport + Artwork",

    # ── Admin / users ─────────────────────────────────────────────────────
    "Panel administratora": "Administration panel",
    "Aktywność użytkowników": "User activity",
    "Aktywnych użytkowników (7 dni)": "Active users (7 days)",
    "Aktywność": "Activity",
    "Brak aktywnych użytkowników": "No active users",
    "Brak zarejestrowanej aktywności": "No recorded activity",
    "Brak użytkowników": "No users",
    "Konto użytkownika": "User account",
    "Dane konta, ustawienia i aktywność": "Account details, settings and activity",

    # ── Profile ───────────────────────────────────────────────────────────
    "Pokaż/ukryj hasło": "Show/hide password",
    "Pokaż/ukryj nowe hasło": "Show/hide new password",
    "Pokaż/ukryj obecne hasło": "Show/hide current password",
    "Pokaż/ukryj potwierdzenie hasła": "Show/hide password confirmation",
    "Pokaż/ukryj potwierdzenie": "Show/hide confirmation",

    # ── Tickets ──────────────────────────────────────────────────────────
    "Zgłoszenia": "Tickets",
    "Brak zgłoszeń": "No tickets",
    "Krótki opis problemu lub sugestii": "Short description of the issue or suggestion",
    "Krótki opis": "Short description",
    "Opisz dokładnie: co się stało, w którym miejscu aplikacji, jakie pliki były użyte, jaki jest oczekiwany wynik...":
        "Describe in detail: what happened, where in the app, what files were used, what was the expected result...",

    # ── Library ──────────────────────────────────────────────────────────
    "Biblioteka PO": "PO Library",
    "Biblioteka plików": "File library",
    "Biblioteka Masterów": "Master Library",

    # ── AI agent / proforma ───────────────────────────────────────────────
    "Agent pro-formy": "Proforma agent",
    "Agent analizuje pro-formę…Ekstrakcja numeru PO → wyszukanie w bibliotece → porównanie → email":
        "Agent is analysing the proforma… Extracting PO number → searching library → comparing → email",
    "Agent wykrył numer zamówienia:": "Agent detected order number:",
    "Analiza AI wymaga aktywnego klucza Anthropic API. Jeśli klucz nie jest skonfigurowany, sekcja AI nie pojawi się.":
        "AI analysis requires an active Anthropic API key. If the key is not configured, the AI section will not appear.",

    # ── Scan / PDF→Excel ─────────────────────────────────────────────────
    "Automatyczne wyciąganie danych z WZ, CMR, PZ do Excela/SAP":
        "Automatic data extraction from delivery notes, CMR, GR to Excel/SAP",
    "Dla dokumentów wielostronicowych PDF każda strona jest przetwarzana osobno i trafia jako osobne wiersze. Skany z odręcznymi dopiskami (LOT, ilości odebrane) są obsługiwane przez AI.":
        "For multi-page PDFs each page is processed separately and added as separate rows. Scans with handwritten notes (LOT, received quantities) are handled by AI.",

    # ── Help page headings ────────────────────────────────────────────────
    "Logowanie do systemu": "System login",
    "Nawigacja po aplikacji": "App navigation",
    "Zarządzaj typami poziomów opakowań": "Manage packaging level types",
    "Analiza kosztów i wywołań AI": "AI call and cost analysis",
    "Analiza literówek i rozbieżności": "Typo and discrepancy analysis",
    "Analizuj dokumenty →": "Analyse documents →",
    "Analizuj literówki →": "Analyse typos →",
    "Automatyczne tłumaczenia terminów handlowych. Zarządzanie słownikiem terminologii.":
        "Automatic translation of trade terms. Terminology dictionary management.",

    # ── Common actions / buttons ─────────────────────────────────────────
    "Zapisz": "Save",
    "Usuń": "Delete",
    "Dodaj": "Add",
    "Edytuj": "Edit",
    "Odśwież": "Refresh",
    "Kliknij aby odświeżyć": "Click to refresh",
    "Kliknij aby edytować": "Click to edit",
    "Kliknij aby edytować emoji": "Click to edit emoji",
    "Kliknij aby edytować opis": "Click to edit description",
    "Kliknij aby usunąć": "Click to delete",
    "Kliknij: szczegóły, status, zgoda, oś czasu": "Click: details, status, approval, timeline",
    "Dodaj pełny pasek poziomy": "Add full horizontal bar",
    "Przeciągnij aby zmienić kolejność": "Drag to reorder",
    "Przełącz widoczność": "Toggle visibility",
    "Kopiuj link potwierdzenia": "Copy confirmation link",
    "Powiązane porównanie": "Related comparison",
    "Zobacz w historii porównań": "View in comparison history",

    # ── Error / status states ─────────────────────────────────────────────
    "Błąd serwera": "Server error",
    "Błąd ładowania danych — odśwież stronę.": "Error loading data — refresh the page.",
    "Błąd ładowania danych. Spróbuj ponownie.": "Error loading data. Please try again.",
    "Błąd eksportu": "Export error",
    "Błąd połączenia — sprawdź sieć i spróbuj ponownie.":
        "Connection error — check your network and try again.",
    "Błąd analizy dokumentów — spróbuj ponownie.":
        "Document analysis error — please try again.",
    "Brak dokumentów dla tego PO.": "No documents for this PO.",

    # ── Table / results general ─────────────────────────────────────────
    "Brak wierszy pasujących do filtru.": "No rows matching the filter.",
    "Brak pól dla wybranego filtra.": "No fields for the selected filter.",
    "Brak materiałów": "No materials",
    "Brak szablonów": "No templates",
    'Brak typów dokumentów. Kliknij „+ Dodaj typ” aby dodać pierwszy.':
        "No document types. Click '+ Add type' to add the first one.",
    "Brak raportów strukturalnych": "No structural reports",
    "Brak zapisanych szablonów.": "No saved templates.",
    "Brak wpisów": "No entries",
    "Brak porównań": "No comparisons",
    "Brak kursów walut": "No exchange rates",
    "Brak wyników": "No results",
    "Brakujące": "Missing",
    "Częściowe": "Partial",

    # ── Pagination ────────────────────────────────────────────────────────
    "&#8592; Powrót": "&#8592; Back",
    "Paginacja": "Pagination",

    # ── Column / role helpers (supplier wizard) ──────────────────────────
    "Mapowanie kolumn": "Column mapping",
    "Podgląd tabeli": "Table preview",
    "Brak zaznaczenia = dostęp do wszystkich modułów.":
        "No selection = access to all modules.",
    "Co możesz robić w zależności od swojej roli": "What you can do depending on your role",
    "Co możesz zrobić?": "What can you do?",
    "(zgodę nadaje rola: specjalista)": "(approval granted by role: specialist)",

    # ── Currencies ────────────────────────────────────────────────────────
    "Kursy walut": "Exchange rates",
    "Brak kursów walut": "No exchange rates",
    "Data ważności": "Expiry date",
    "Data zamówienia": "Order date",

    # ── Artwork admin ─────────────────────────────────────────────────────
    "Zarządzanie artworkami": "Artwork management",
    "Brakujące pola": "Missing fields",
    "Brak materiałów": "No materials",
    "Dodaj materiał": "Add material",
    "Dalej — Mapowanie PI →": "Next — PI mapping →",
    "Dalej — Mapowanie PO →": "Next — PO mapping →",
    "Dalej — Mapowanie SAD →": "Next — SAD mapping →",
    "Dalej — Tolerancje →": "Next — Tolerances →",
    "Dalej →": "Next →",

    # ── Zone/region compare ───────────────────────────────────────────────
    "Wykryte pary — sprawdź i uruchom": "Detected pairs — review and run",
    "2. Wykryte pary — sprawdź i uruchom": "2. Detected pairs — review and run",
    "Kliknij Dodaj strefę → powtórz dla kolejnych stref →":
        "Click Add zone → repeat for subsequent zones →",
    "Kliknij Porównaj zaznaczone strefy.": "Click Compare selected zones.",
    "Brak zaznaczonych stref": "No zones selected",

    # ── Help / docs numbers ───────────────────────────────────────────────
    "3 Porównanie dokumentów handlowych (PO / PI / CI / PL)":
        "3 Trade document comparison (PO / PI / CI / PL)",
    "3. Porównanie dokumentów handlowych":
        "3. Trade document comparison",
    "4 Odczytywanie wyników analizy": "4 Reading analysis results",
    "4. Odczytywanie wyników analizy": "4. Reading analysis results",
    "5 Porównanie artworków i opakowań": "5 Artwork and packaging comparison",
    "5 Tolerancje i reguły porównania": "5 Tolerances and comparison rules",
    "5. Porównanie artworków i opakowań": "5. Artwork and packaging comparison",
    "6 Ekstrakcja skanów dostaw (nowe)": "6 Delivery scan extraction (new)",
    "6. Ekstrakcja skanów dostaw": "6. Delivery scan extraction",
    "8 Zgłaszanie problemów i sugestii": "8 Reporting issues and suggestions",
    "8. Zgłaszanie problemów i sugestii": "8. Reporting issues and suggestions",
    "9 Role użytkowników i uprawnienia": "9 User roles and permissions",
    "9. Role użytkowników i uprawnienia": "9. User roles and permissions",

    # ── Misc / inline JS strings visible in UI ────────────────────────────
    "DO NATYCHMIASTOWEGO DZIAŁANIA:": "IMMEDIATE ACTION REQUIRED:",
    "Do natychmiastowego działania:": "Immediate action required:",
    "Analiza artworku wielostronicowego (np. 8 stron) może trwać 2–3 minuty. Baner ostrzeżenia pojawi się automatycznie — nie zamykaj karty. Próba zamknięcia okna wyświetli potwierdzenie.":
        "Multi-page artwork analysis (e.g. 8 pages) may take 2–3 minutes. A warning banner will appear automatically — do not close the tab. Attempting to close the window will show a confirmation.",
    "DocCompare — wybierz moduł": "DocCompare — select module",
    "Spedycja": "Freight",
    "A → Z (pole)": "A → Z (field)",
    "A–Z nazwa": "A–Z name",
    "DE → PL": "DE → PL",
    "0% = zero tolerancji na różnice ilości": "0% = zero tolerance for quantity differences",
    "300 znaków pozostało": "300 characters remaining",
    "180° — obrót o 180°": "180° — rotate 180°",
    "270° — obrót w lewo": "270° — rotate left",
    "90° — obrót w prawo": "90° — rotate right",

    # ── Placeholders ─────────────────────────────────────────────────────
    "13-cyfrowy kod EAN": "13-digit EAN code",
    "Dodaj REF (Enter)": "Add REF (Enter)",
    "Dodaj komentarz do decyzji…": "Add a comment to the decision…",
    "Dodatkowe informacje...": "Additional information...",
    "Dodatkowe informacje…": "Additional information…",
    "Dodatkowe wymagania, terminy, uwagi...": "Additional requirements, deadlines, notes...",
    "E-mail agenta": "Agent e-mail",
    "Etykieta (np. Display Box)": "Label (e.g. Display Box)",
    "Kod (np. display)": "Code (e.g. display)",
    "Napisz uwagi, np. możliwe opóźnienie, pytania...":
        "Write notes, e.g. possible delay, questions...",
    "Nazwa agenta *": "Agent name *",
    "Nazwa dostawcy (opcjonalnie)": "Supplier name (optional)",
    "Nazwa nagłówka kolumny…": "Column header name…",
    "Nazwa szablonu…": "Template name…",
    "Notatki, numery LOT, specjalne instrukcje...":
        "Notes, LOT numbers, special instructions...",
    "Numery PO": "PO numbers",
    "Opcjonalna odpowiedź dla użytkownika...": "Optional reply to user...",
    "Opcjonalne uwagi…": "Optional notes…",
    "Opcjonalny opis dla operatora…": "Optional description for operator…",
    "Opcjonalny opis produktu": "Optional product description",
    "Opcjonalny opis szablonu…": "Optional template description…",
    "Szukaj PO, dostawcy, referencji…": "Search PO, supplier, reference…",
    "Szukaj pliku lub użytkownika...": "Search file or user...",
    "Szukaj pliku, PO, dostawcy, usera…": "Search file, PO, supplier, user…",
    "Szukaj pola SAD, nazwy, opisu…": "Search SAD field, name, description…",
    "Jan Kowalski": "John Smith",
    "Marek Nowak": "Mark Brown",
    "Spedycja Kowalski Sp. z o.o.": "Freight Kowalski Ltd.",

    # ── Misc short labels ─────────────────────────────────────────────────
    "Typ": "Type",
    "Opis": "Description",
    "Nazwa": "Name",
    "Data": "Date",
    "Ilość": "Qty",
    "Cena": "Price",
    "Wartość": "Value",
    "Waluta": "Currency",
    "REF": "REF",
    "Kraj": "Country",
    "Uwagi": "Notes",
    "Akcje": "Actions",
    "Szczegóły": "Details",
    "Wyniki": "Results",
    "Więcej": "More",
    "Mniej": "Less",
    "Podsumowanie": "Summary",
    "Zestawienie": "Overview",
    "Statystyki": "Statistics",
    "Konfiguracja": "Configuration",
    "Ustawienia": "Settings",
    "Profil": "Profile",
    "Rola": "Role",
    "Aktywny": "Active",
    "Nieaktywny": "Inactive",
    "Tak": "Yes",
    "Nie": "No",
    "Otwórz →": "Open →",
    "&#8592; Powrót": "&#8592; Back",
}

# Build sorted list (longest key first to avoid substring clobbering)
_SORTED_PHRASES = sorted(_RAW.items(), key=lambda kv: len(kv[0]), reverse=True)

# Attribute names whose values should be translated
_TRANSLATABLE_ATTRS = ["placeholder", "aria-label", "title"]

# Regex to identify <script...>...</script> and <style...>...</style> blocks
_BLOCK_RE = re.compile(
    r'(<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>)',
    re.IGNORECASE,
)

# Regex to find text nodes between HTML tags
_TEXT_NODE_RE = re.compile(r'(>)([^<]+)(<)')

# Regex to find translatable attribute values
_ATTR_RE = re.compile(
    r'((?:' + '|'.join(_TRANSLATABLE_ATTRS) + r')=["\'])([^"\']+)(["\'])',
    re.IGNORECASE,
)


def _apply_phrases(text: str) -> str:
    """Apply all Polish→English phrase substitutions to a plain text string."""
    for pl, en in _SORTED_PHRASES:
        if pl in text:
            text = text.replace(pl, en)
    return text


def _translate_text_nodes(html_chunk: str) -> str:
    """Replace Polish text in text nodes and translatable attributes of an HTML chunk."""

    def _node_sub(m):
        return m.group(1) + _apply_phrases(m.group(2)) + m.group(3)

    def _attr_sub(m):
        return m.group(1) + _apply_phrases(m.group(2)) + m.group(3)

    html_chunk = _TEXT_NODE_RE.sub(_node_sub, html_chunk)
    html_chunk = _ATTR_RE.sub(_attr_sub, html_chunk)
    return html_chunk


def translate_html(html: str) -> str:
    """
    Translate a full rendered HTML page from Polish to English.
    Skips <script> and <style> blocks entirely.
    """
    parts = _BLOCK_RE.split(html)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Odd = script/style block — do NOT touch
            out.append(part)
        else:
            out.append(_translate_text_nodes(part))
    return "".join(out)
