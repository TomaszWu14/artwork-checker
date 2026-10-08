#!/usr/bin/env python3
"""
Generuje DocCompare v6 – Edytowalna Mapa Procesów w Excelu
Wynik: doccompare_process_map.xlsx
"""

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ─── Paleta kolorów ───────────────────────────────────────────────────────────
C_NAVY   = "1F3864"
C_BLUE   = "2E75B6"
C_LBLUE  = "D6E4F0"
C_WHITE  = "FFFFFF"
C_YELLOW = "FFF2CC"
C_GREEN  = "E2EFDA"
C_RED    = "FCE4D6"
C_GREY   = "F2F2F2"
C_TEAL   = "DAEEF3"

def _fill(hex_colour):
    return PatternFill("solid", fgColor=hex_colour)

def _font(bold=False, colour="000000", sz=10, italic=False):
    return Font(bold=bold, color=colour, size=sz, italic=italic)

def _border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def _align(wrap=True, h="left", v="top"):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _hdr(ws, row, col, text, bg=C_NAVY, fg=C_WHITE, sz=10, bold=True):
    c = ws.cell(row=row, column=col, value=text)
    c.font = _font(bold=bold, colour=fg, sz=sz)
    c.fill = _fill(bg)
    c.alignment = _align(h="center", v="center", wrap=True)
    c.border = _border()
    return c

def _cell(ws, row, col, text, bg=C_WHITE, bold=False, wrap=True, align="left"):
    c = ws.cell(row=row, column=col, value=text)
    c.font = _font(bold=bold)
    c.fill = _fill(bg)
    c.alignment = _align(wrap=wrap, h=align, v="top")
    c.border = _border()
    return c

# ─── Dane tras (140 tras, pogrupowane według kategorii) ───────────────────────
# Format: (kategoria, metoda, url, funkcja, auth, csrf, opis, moduły)
ROUTES = [
    # Auth
    ("Autoryzacja", "GET",  "/login",                      "login_page",             "Nie",     "Nie",  "Formularz logowania", "db"),
    ("Autoryzacja", "POST", "/login",                       "do_login",               "Nie",     "Tak",  "Weryfikuje dane logowania, ustawia sesję", "db"),
    ("Autoryzacja", "GET",  "/logout",                      "logout",                 "Tak",     "Nie",  "Czyści sesję użytkownika", ""),
    ("Autoryzacja", "GET",  "/forgot-password",             "forgot_password_page",   "Nie",     "Nie",  "Formularz przypomnienia hasła", ""),
    ("Autoryzacja", "POST", "/forgot-password",             "do_forgot_password",     "Nie",     "Tak",  "Wysyła e-mail resetujący hasło przez Resend API", "db"),
    ("Autoryzacja", "GET",  "/reset-password/<token>",      "reset_password_page",    "Nie",     "Nie",  "Formularz resetowania hasła (token z e-maila)", "db"),
    ("Autoryzacja", "POST", "/reset-password/<token>",      "do_reset_password",      "Nie",     "Tak",  "Weryfikuje token, aktualizuje hash hasła", "db"),
    ("Autoryzacja", "GET",  "/change-password",             "change_password_page",   "użytkownik","Nie","Formularz zmiany hasła", ""),
    ("Autoryzacja", "POST", "/change-password",             "do_change_password",     "użytkownik","Tak","Weryfikuje stare hasło, zapisuje nowy hash", "db"),

    # Panel główny
    ("Panel główny", "GET",  "/",                           "index",                  "użytkownik","Nie","Główny panel – ostatnie porównania, KPI", "db"),
    ("Panel główny", "GET",  "/dashboard",                  "dashboard",              "użytkownik","Nie","Pełny panel KPI (Chart.js)", "db"),
    ("Panel główny", "GET",  "/compare",                    "compare_page",           "użytkownik","Nie","Strona przesyłania plików do porównania dokumentów", ""),
    ("Panel główny", "GET",  "/artwork",                    "artwork_page",           "użytkownik","Nie","Strona przesyłania plików do porównania grafik", ""),
    ("Panel główny", "GET",  "/history",                    "history_page",           "użytkownik","Nie","Lista historii porównań", "db"),
    ("Panel główny", "GET",  "/history/<int:cid>",          "history_detail",         "użytkownik","Nie","Szczegóły pojedynczego wynikuporównania", "db"),

    # API porównywania dokumentów
    ("API porównań", "POST", "/api/compare",                "api_compare",            "użytkownik","Tak","Pełne porównanie ZO/PI/FK/LP: ekstrakcja→normalizacja→dopasowanie→porównanie→walidacja AI", "table_extractor, normalizer, semantic_matcher, enhanced_comparator, ai_validator, db"),
    ("API porównań", "POST", "/api/table_compare",          "api_table_compare",      "użytkownik","Tak","Szybkie porównanie tylko tabel (bez AI)", "table_extractor, normalizer, enhanced_comparator"),
    ("API porównań", "POST", "/api/typo_compare",           "api_typo_compare",       "użytkownik","Tak","Wykrywanie literówek/błędów OCR między dokumentami", "table_extractor, typo_detector"),
    ("API porównań", "GET",  "/api/compare/<int:cid>",      "api_compare_get",        "użytkownik","Nie","Pobierz zapisany wynik porównania (JSON)", "db"),
    ("API porównań", "POST", "/api/compare/<int:cid>/flag", "api_flag_result",        "użytkownik","Tak","Oznacz/odznacz flagą znalezisko", "db"),
    ("API porównań", "POST", "/api/compare/<int:cid>/comment","api_add_comment",      "użytkownik","Tak","Dodaj komentarz do znaleziska", "db"),
    ("API porównań", "GET",  "/compare/result/<int:cid>",   "compare_result_page",    "użytkownik","Nie","Strona HTML z wynikiem porównania", "db"),
    ("API porównań", "GET",  "/compare/result/<int:cid>/summary","compare_summary",   "użytkownik","Nie","Strona podsumowania do druku", "db"),

    # Porównanie grafik
    ("Grafiki (Artwork)", "POST", "/api/artwork/compare",           "api_artwork_compare",    "użytkownik","Tak","Porównanie jednej pary plików PDF grafiki/opakowania", "artwork_comparator, barcode_validator, db"),
    ("Grafiki (Artwork)", "POST", "/api/artwork/compare-multi",     "api_artwork_compare_multi","użytkownik","Tak","Wsadowe porównanie wielu stron grafiki", "artwork_comparator, barcode_validator, db"),
    ("Grafiki (Artwork)", "GET",  "/artwork/report",                "artwork_report_page",    "użytkownik","Nie","Raport HTML z porównania grafiki", "artwork_report_engine, db"),
    ("Grafiki (Artwork)", "GET",  "/artwork/report/<int:aid>",      "artwork_report_detail",  "użytkownik","Nie","Szczegóły zapisanego raportu grafiki", "db"),
    ("Grafiki (Artwork)", "GET",  "/artwork/history",               "artwork_history",        "użytkownik","Nie","Historia porównań grafik", "db"),
    ("Grafiki (Artwork)", "GET",  "/api/artwork/<int:aid>/pdf",     "artwork_export_pdf",     "użytkownik","Nie","Eksport raportu grafiki do PDF", "artwork_report_engine"),
    ("Grafiki (Artwork)", "POST", "/api/artwork/<int:aid>/approve", "artwork_approve",        "kierownik","Tak","Zatwierdź wersję grafiki", "db"),
    ("Grafiki (Artwork)", "POST", "/api/artwork/<int:aid>/reject",  "artwork_reject",         "kierownik","Tak","Odrzuć wersję grafiki", "db"),

    # Kody kreskowe
    ("Kody kreskowe", "POST", "/api/barcode/validate",      "api_barcode_validate",   "użytkownik","Tak","Waliduj kod kreskowy EAN-13 lub GS1-128", "barcode_validator"),
    ("Kody kreskowe", "POST", "/api/barcode/scan",          "api_barcode_scan",       "użytkownik","Tak","Skanuj kody kreskowe z przesłanego obrazu", "barcode_validator"),

    # Eksport
    ("Eksport", "GET",  "/api/export/pdf/<int:cid>",        "export_pdf",             "użytkownik","Nie","Pobierz raport porównania jako PDF (ReportLab)", "export_engine, db"),
    ("Eksport", "GET",  "/api/export/excel/<int:cid>",      "export_excel",           "użytkownik","Nie","Pobierz raport porównania jako Excel (openpyxl)", "export_engine, db"),
    ("Eksport", "GET",  "/api/export/csv/<int:cid>",        "export_csv",             "użytkownik","Nie","Pobierz znaleziska jako CSV", "db"),

    # AI / Uczenie maszynowe
    ("AI / Uczenie", "POST", "/api/ai/learn-document",      "ai_learn_document",      "kierownik","Tak","Wyślij PDF do Claude → pobierz sugestie mapowania kolumn", "ai_learning, db"),
    ("AI / Uczenie", "GET",  "/api/ai/pending-updates",     "ai_pending_updates",     "kierownik","Nie","Lista oczekujących sugestii AI dotyczących profili", "db"),
    ("AI / Uczenie", "POST", "/api/ai/apply-profile",       "ai_apply_profile",       "kierownik","Tak","Zatwierdź i zastosuj sugestię AI", "db, supplier_profiles"),
    ("AI / Uczenie", "DELETE","/api/ai/pending-updates/<int:uid>","ai_dismiss_update", "kierownik","Tak","Odrzuć sugestię AI", "db"),
    ("AI / Uczenie", "POST", "/api/ai/validate",            "api_ai_validate",        "użytkownik","Tak","Oceń ryzyko wyniku porównania przez AI", "ai_validator"),
    ("AI / Uczenie", "GET",  "/ai/learn",                   "ai_learn_page",          "kierownik","Nie","Strona zarządzania uczeniem AI", "db"),
    ("AI / Uczenie", "GET",  "/ai/suggestions",             "ai_suggestions_page",    "kierownik","Nie","Przeglądaj wszystkie sugestie AI", "db"),

    # Dostawcy
    ("Dostawcy", "GET",  "/suppliers",                      "suppliers_page",         "kierownik","Nie","Lista dostawców", "db"),
    ("Dostawcy", "GET",  "/suppliers/wizard",               "suppliers_wizard",       "kierownik","Nie","Interaktywny kreator profilu dostawcy", "supplier_profiles"),
    ("Dostawcy", "GET",  "/api/suppliers",                  "api_suppliers_list",     "użytkownik","Nie","Zwróć wszystkie profile dostawców (JSON)", "db, supplier_profiles"),
    ("Dostawcy", "POST", "/api/suppliers",                  "api_suppliers_create",   "kierownik","Tak","Utwórz nowy profil dostawcy", "db, supplier_profiles"),
    ("Dostawcy", "GET",  "/api/suppliers/<int:sid>",        "api_suppliers_get",      "użytkownik","Nie","Pobierz profil pojedynczego dostawcy (JSON)", "db"),
    ("Dostawcy", "PUT",  "/api/suppliers/<int:sid>",        "api_suppliers_update",   "kierownik","Tak","Zaktualizuj profil dostawcy", "db, supplier_profiles"),
    ("Dostawcy", "DELETE","/api/suppliers/<int:sid>",       "api_suppliers_delete",   "kierownik","Tak","Usuń profil dostawcy", "db"),
    ("Dostawcy", "POST", "/api/suppliers/<int:sid>/merge",  "api_suppliers_merge",    "kierownik","Tak","Scal dwa profile dostawców", "db"),
    ("Dostawcy", "POST", "/api/suppliers/preview_table",    "api_suppliers_preview_table","kierownik","Tak","Podgląd tabeli z PDF do mapowania kolumn", "table_extractor"),
    ("Dostawcy", "GET",  "/api/suppliers/<int:sid>/history","api_suppliers_history",  "użytkownik","Nie","Historia porównań dla dostawcy", "db"),

    # Śledzenie dostaw
    ("Śledzenie dostaw", "GET",  "/delivery",               "delivery_page",          "użytkownik","Nie","Panel śledzenia dostaw", "db"),
    ("Śledzenie dostaw", "GET",  "/delivery/<int:did>",     "delivery_detail",        "użytkownik","Nie","Szczegóły pojedynczej dostawy", "db"),
    ("Śledzenie dostaw", "POST", "/api/delivery",           "api_delivery_create",    "kierownik","Tak","Utwórz nową dostawę/zamówienie", "db"),
    ("Śledzenie dostaw", "PUT",  "/api/delivery/<int:did>", "api_delivery_update",    "kierownik","Tak","Zaktualizuj status/pola dostawy", "db"),
    ("Śledzenie dostaw", "DELETE","/api/delivery/<int:did>","api_delivery_delete",    "kierownik","Tak","Usuń rekord dostawy", "db"),
    ("Śledzenie dostaw", "GET",  "/api/delivery",           "api_delivery_list",      "użytkownik","Nie","Lista wszystkich dostaw (JSON)", "db"),
    ("Śledzenie dostaw", "GET",  "/api/delivery/<int:did>", "api_delivery_get",       "użytkownik","Nie","Pobierz pojedynczą dostawę (JSON)", "db"),
    ("Śledzenie dostaw", "POST", "/api/delivery/<int:did>/attach","api_delivery_attach","użytkownik","Tak","Powiąż wynik porównania z dostawą", "db"),
    ("Śledzenie dostaw", "GET",  "/api/delivery/<int:did>/timeline","api_delivery_timeline","użytkownik","Nie","Oś czasu zdarzeń dostawy", "db"),
    ("Śledzenie dostaw", "POST", "/api/delivery/<int:did>/note","api_delivery_note",  "użytkownik","Tak","Dodaj notatkę do dostawy", "db"),
    ("Śledzenie dostaw", "POST", "/api/delivery/<int:did>/close","api_delivery_close","kierownik","Tak","Oznacz dostawę jako zakończoną", "db"),

    # Administracja – Użytkownicy
    ("Admin – Użytkownicy", "GET",  "/admin",              "admin_page",             "admin","Nie","Panel administratora – przegląd", "db"),
    ("Admin – Użytkownicy", "GET",  "/api/users",          "api_users_list",         "admin","Nie","Lista wszystkich użytkowników (JSON)", "db"),
    ("Admin – Użytkownicy", "POST", "/api/users",          "api_users_create",       "admin","Tak","Utwórz nowe konto użytkownika", "db"),
    ("Admin – Użytkownicy", "PUT",  "/api/users/<int:uid>","api_users_update",       "admin","Tak","Zaktualizuj pola/rolę użytkownika", "db"),
    ("Admin – Użytkownicy", "DELETE","/api/users/<int:uid>","api_users_delete",      "admin","Tak","Usuń konto użytkownika", "db"),
    ("Admin – Użytkownicy", "POST", "/api/users/<int:uid>/reset-password","api_admin_reset_pw","admin","Tak","Wymuś reset hasła przez admina", "db"),
    ("Admin – Użytkownicy", "POST", "/api/users/<int:uid>/toggle-active","api_toggle_active","admin","Tak","Włącz/wyłącz konto użytkownika", "db"),

    # Administracja – System
    ("Admin – System", "GET",  "/admin/stats",             "admin_stats",            "admin","Nie","Statystyki systemu (rozmiar DB, kolejka)", "db"),
    ("Admin – System", "GET",  "/admin/logs",              "admin_logs",             "admin","Nie","Przeglądarka ostatnich logów aplikacji", ""),
    ("Admin – System", "POST", "/admin/flush-cache",       "admin_flush_cache",      "admin","Tak","Wyczyść cache w pamięci", ""),
    ("Admin – System", "POST", "/admin/reindex",           "admin_reindex",          "admin","Tak","Przebuduj indeks wyszukiwania semantycznego", "semantic_matcher"),
    ("Admin – System", "GET",  "/admin/config",            "admin_config",           "admin","Nie","Pokaż konfigurację środowiskową (ukryte sekrety)", ""),
    ("Admin – System", "POST", "/admin/config",            "admin_config_save",      "admin","Tak","Zapisz nadpisania konfiguracji do DB", "db"),
    ("Admin – System", "GET",  "/admin/migrate",           "admin_migrate",          "admin","Nie","Strona statusu migracji DB", "db"),
    ("Admin – System", "POST", "/admin/migrate",           "admin_run_migrate",      "admin","Tak","Uruchom oczekujące migracje DB", "db"),
    ("Admin – System", "GET",  "/admin/backup",            "admin_backup",           "admin","Nie","Interfejs kopii zapasowej/przywracania", "db"),
    ("Admin – System", "POST", "/admin/backup",            "admin_do_backup",        "admin","Tak","Pobierz kopię zapasową DB", "db"),

    # Podgląd / Miniatury
    ("Podgląd plików", "GET",  "/preview/<path:filename>", "preview_file",           "użytkownik","Nie","Serwuje miniaturę wyrenderowanej strony PDF", ""),
    ("Podgląd plików", "GET",  "/api/preview/generate",   "api_preview_generate",   "użytkownik","Nie","Wyrenderuj stronę PDF do PNG na żądanie", "artwork_comparator"),

    # Powiadomienia
    ("Powiadomienia", "GET",  "/api/notifications",        "api_notifications",      "użytkownik","Nie","Lista powiadomień użytkownika", "db"),
    ("Powiadomienia", "POST", "/api/notifications/read",   "api_notifications_read", "użytkownik","Tak","Oznacz powiadomienia jako przeczytane", "db"),
    ("Powiadomienia", "DELETE","/api/notifications/<int:nid>","api_notification_del","użytkownik","Tak","Usuń powiadomienie", "db"),
    ("Powiadomienia", "GET",  "/api/notifications/count",  "api_notif_count",        "użytkownik","Nie","Liczba nieprzeczytanych powiadomień (odznaka)", "db"),

    # Wyszukiwanie
    ("Wyszukiwanie", "GET",  "/api/search",                "api_search",             "użytkownik","Nie","Pełnotekstowe wyszukiwanie w porównaniach", "db"),
    ("Wyszukiwanie", "GET",  "/api/search/suppliers",      "api_search_suppliers",   "użytkownik","Nie","Wyszukaj profile dostawców po nazwie/kodzie", "db"),
    ("Wyszukiwanie", "GET",  "/api/search/products",       "api_search_products",    "użytkownik","Nie","Wyszukaj tabelę synonimów produktów", "db"),

    # Raporty / Analityka
    ("Raporty / Analityka", "GET",  "/reports",            "reports_page",           "kierownik","Nie","Panel raportów i analityki", "db"),
    ("Raporty / Analityka", "GET",  "/api/reports/summary","api_reports_summary",    "kierownik","Nie","Zagregowane KPI (JSON)", "db"),
    ("Raporty / Analityka", "GET",  "/api/reports/error-trends","api_error_trends",  "kierownik","Nie","Dane trendów typów błędów dla Chart.js", "db"),
    ("Raporty / Analityka", "GET",  "/api/reports/supplier-quality","api_supplier_quality","kierownik","Nie","Wskaźniki jakości dla poszczególnych dostawców", "db"),
    ("Raporty / Analityka", "GET",  "/api/reports/monthly","api_monthly_report",     "kierownik","Nie","Statystyki porównań miesiąc po miesiącu", "db"),
    ("Raporty / Analityka", "GET",  "/api/reports/export", "api_reports_export",     "kierownik","Nie","Eksport analityki do Excela", "export_engine, db"),

    # Szablony dokumentów
    ("Szablony dokumentów", "GET",  "/templates",          "templates_page",         "kierownik","Nie","Lista szablonów dokumentów", "db"),
    ("Szablony dokumentów", "POST", "/api/templates",      "api_templates_create",   "kierownik","Tak","Prześlij nowy szablon dokumentu", "db"),
    ("Szablony dokumentów", "PUT",  "/api/templates/<int:tid>","api_templates_update","kierownik","Tak","Zaktualizuj metadane szablonu", "db"),
    ("Szablony dokumentów", "DELETE","/api/templates/<int:tid>","api_templates_delete","kierownik","Tak","Usuń szablon", "db"),
    ("Szablony dokumentów", "POST", "/api/templates/<int:tid>/extract","api_templates_extract","kierownik","Tak","Wyodrębnij strukturę kolumn z szablonu PDF", "table_extractor"),

    # Integracje / Webhooki
    ("Integracje / Webhooki", "POST", "/webhooks/erp",     "webhook_erp",            "Nie","Token","Odbierz push z ERP (powiadomienie o nowym ZO)", "db"),
    ("Integracje / Webhooki", "GET",  "/api/integrations", "api_integrations_list",  "admin","Nie","Lista skonfigurowanych integracji", "db"),
    ("Integracje / Webhooki", "POST", "/api/integrations", "api_integrations_create","admin","Tak","Dodaj konfigurację integracji", "db"),
    ("Integracje / Webhooki", "PUT",  "/api/integrations/<int:iid>","api_integrations_update","admin","Tak","Zaktualizuj konfigurację integracji", "db"),
    ("Integracje / Webhooki", "DELETE","/api/integrations/<int:iid>","api_integrations_del","admin","Tak","Usuń integrację", "db"),
    ("Integracje / Webhooki", "POST", "/api/integrations/<int:iid>/test","api_integrations_test","admin","Tak","Wyślij ping do punktu końcowego integracji", ""),

    # Dziennik audytu
    ("Dziennik audytu", "GET",  "/admin/audit",            "admin_audit",            "admin","Nie","Przeglądarka pełnego dziennika audytu", "db"),
    ("Dziennik audytu", "GET",  "/api/audit",              "api_audit",              "admin","Nie","Dziennik audytu (JSON, stronicowany)", "db"),
    ("Dziennik audytu", "GET",  "/api/audit/export",       "api_audit_export",       "admin","Nie","Eksport dziennika audytu do CSV", "db"),

    # Profil / Konto
    ("Profil użytkownika", "GET",  "/profile",             "profile_page",           "użytkownik","Nie","Strona profilu użytkownika", "db"),
    ("Profil użytkownika", "POST", "/api/profile",         "api_profile_update",     "użytkownik","Tak","Zaktualizuj pola profilu (imię, język, strefa czasowa)", "db"),
    ("Profil użytkownika", "POST", "/api/profile/avatar",  "api_profile_avatar",     "użytkownik","Tak","Prześlij awatar profilu", "db"),
    ("Profil użytkownika", "GET",  "/api/profile/preferences","api_preferences",     "użytkownik","Nie","Pobierz preferencje interfejsu (JSON)", "db"),
    ("Profil użytkownika", "POST", "/api/profile/preferences","api_preferences_save","użytkownik","Tak","Zapisz preferencje interfejsu", "db"),

    # Tagi
    ("Tagi", "GET",  "/api/tags",                          "api_tags_list",          "użytkownik","Nie","Lista wszystkich tagów", "db"),
    ("Tagi", "POST", "/api/tags",                          "api_tags_create",        "kierownik","Tak","Utwórz tag", "db"),
    ("Tagi", "DELETE","/api/tags/<int:tid>",               "api_tags_delete",        "kierownik","Tak","Usuń tag", "db"),
    ("Tagi", "POST", "/api/compare/<int:cid>/tags",        "api_compare_add_tag",    "użytkownik","Tak","Dodaj tag do wyniku porównania", "db"),
    ("Tagi", "DELETE","/api/compare/<int:cid>/tags/<int:tid>","api_compare_rm_tag",  "użytkownik","Tak","Usuń tag z porównania", "db"),

    # Komentarze
    ("Komentarze", "POST", "/api/comments",                "api_comments_create",    "użytkownik","Tak","Dodaj komentarz do dowolnego obiektu", "db"),
    ("Komentarze", "PUT",  "/api/comments/<int:cid>",      "api_comments_update",    "użytkownik","Tak","Edytuj własny komentarz", "db"),
    ("Komentarze", "DELETE","/api/comments/<int:cid>",     "api_comments_delete",    "użytkownik","Tak","Usuń własny komentarz (kierownik – każdy)", "db"),

    # Narzędzia pomocnicze
    ("Narzędzia pomocnicze", "POST", "/api/normalize/number","api_normalize_number", "użytkownik","Tak","Testuj normalizację liczb (debug/dev)", "normalizer"),
    ("Narzędzia pomocnicze", "POST", "/api/normalize/date", "api_normalize_date",    "użytkownik","Tak","Testuj normalizację dat (debug/dev)", "normalizer"),
    ("Narzędzia pomocnicze", "POST", "/api/semantic/match", "api_semantic_match",    "użytkownik","Tak","Testuj semantyczne dopasowanie kodów produktów", "semantic_matcher"),

    # Zarządzanie plikami
    ("Zarządzanie plikami", "GET",  "/api/files",          "api_files_list",         "użytkownik","Nie","Lista przesłanych plików użytkownika", "db"),
    ("Zarządzanie plikami", "DELETE","/api/files/<path:fname>","api_files_delete",   "użytkownik","Tak","Usuń przesłany plik z dysku", ""),
    ("Zarządzanie plikami", "GET",  "/uploads/<path:filename>","serve_upload",       "użytkownik","Nie","Serwuj przesłany plik (z uwierzytelnieniem)", ""),

    # Udostępnione linki
    ("Udostępnione linki", "POST", "/api/share/<int:cid>", "api_share_create",       "kierownik","Tak","Generuj link do udostępnienia wyniku", "db"),
    ("Udostępnione linki", "GET",  "/share/<token>",       "shared_result",          "Nie","Nie","Publiczny podgląd wyniku (bez logowania)", "db"),
    ("Udostępnione linki", "DELETE","/api/share/<token>",  "api_share_delete",       "kierownik","Tak","Unieważnij link udostępniający", "db"),

    # Bezpieczeństwo / Sesja
    ("Bezpieczeństwo", "GET",  "/api/csrf-token",          "api_csrf_token",         "użytkownik","Nie","Pobierz świeży token CSRF (dla SPA)", ""),
    ("Bezpieczeństwo", "POST", "/api/session/ping",        "api_session_ping",       "użytkownik","Nie","Przedłuż TTL sesji (keep-alive)", ""),
    ("Bezpieczeństwo", "GET",  "/health",                  "health_check",           "Nie","Nie","Sonda zdrowia dla load balancera / monitoringu", "db"),

    # Onboarding
    ("Onboarding", "GET",  "/onboarding",                  "onboarding_page",        "użytkownik","Nie","Kreator pierwszego uruchomienia (onboarding)", ""),
    ("Onboarding", "POST", "/api/onboarding/complete",     "api_onboarding_complete","użytkownik","Tak","Oznacz onboarding jako ukończony", "db"),

    # Strony błędów
    ("Strony błędów", "GET",  "/403",                      "error_403",              "Nie","Nie","Strona 403 – Brak dostępu", ""),
    ("Strony błędów", "GET",  "/404",                      "error_404",              "Nie","Nie","Strona 404 – Nie znaleziono", ""),
    ("Strony błędów", "GET",  "/500",                      "error_500",              "Nie","Nie","Strona 500 – Błąd serwera", ""),
]

# ─── Dane modułów ─────────────────────────────────────────────────────────────
MODULES = [
    ("app.py",               "Punkt wejścia, wszystkie trasy Flask (~3 300 linii)",                     "Flask, db, wszystkie moduły potoku",       "Wszystkie moduły"),
    ("db.py",                "Abstrakcja DB: translacja dialektów SQLite↔PostgreSQL, pula połączeń",    "sqlite3, psycopg2",                        "Wszystkie trasy korzystające z DB"),
    ("enhanced_comparator.py","Główny silnik porównań: dopasowanie pól, tolerancje, warunki płatności", "normalizer, semantic_matcher",             "api_compare"),
    ("table_extractor.py",   "Ekstrakcja tabel z PDF: Camelot → pdfplumber → fallback na koordynaty",  "camelot, pdfplumber, PyMuPDF",             "api_compare, podgląd dostawcy"),
    ("normalizer.py",        "Normalizacja liczb/dat (formaty europejski ↔ angielski)",                 "decimal, re",                              "enhanced_comparator, table_extractor"),
    ("semantic_matcher.py",  "Rozmyte dopasowanie kodów produktów: rapidfuzz + TF-IDF",                "rapidfuzz, sklearn",                       "enhanced_comparator"),
    ("typo_detector.py",     "Wykrywanie pomyłek I/1 O/0 oraz brakujących pól",                        "normalizer",                               "api_typo_compare"),
    ("ai_validator.py",      "Ocena ryzyka AI po porównaniu przez Claude API",                          "anthropic",                                "api_compare, api_ai_validate"),
    ("ai_learning.py",       "Pętla uczenia: sugestie Claude → pending_updates",                        "anthropic, db",                            "ai_learn_document"),
    ("export_engine.py",     "Eksport PDF (ReportLab) + Excel (openpyxl)",                             "reportlab, openpyxl",                      "export_pdf, export_excel"),
    ("artwork_comparator.py","Wizualny diff PDF: różnica pikseli, OCR, ekstrakcja pól opakowania",      "PyMuPDF, Pillow, Tesseract",               "api_artwork_compare"),
    ("artwork_report_engine.py","Buduje słownik raportu grafiki dla szablonu Jinja2",                   "artwork_comparator",                       "artwork_report_page"),
    ("barcode_validator.py", "Suma kontrolna EAN-13, parsowanie GS1-128 AI, skanowanie zxingcpp/pyzbar","zxingcpp, pyzbar",                        "api_artwork_compare, api_barcode_*"),
    ("supplier_profiles.py", "DEFAULT_PROFILES, mapa płatności SAP, tablice synonimów",                "—",                                        "enhanced_comparator, kreator dostawców"),
    ("migrate_db.py",        "Migracje schematu DB (narzędzie CLI)",                                   "db",                                       "admin_run_migrate"),
    ("install.py",           "Pomocnik instalacji pierwszego uruchomienia (narzędzie CLI)",             "db",                                       "—"),
]

# ─── Kluczowe procesy ─────────────────────────────────────────────────────────
# Format: (nazwa przepływu, krok, aktor/komponent, działanie, wynik/notatka)
FLOWS = [
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 1,  "Przeglądarka",       "Użytkownik wybiera typ dokumentu i przesyła 2 pliki PDF",  "POST /api/compare"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 2,  "app.py",             "Weryfikuje PDF, zapisuje do uploads/ z unikalną nazwą (prefiks UUID)", "Unikalny plik"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 3,  "table_extractor.py", "Camelot wyodrębnia tabele → pdfplumber (fallback) → koordynaty tekstu (ostatni)", "Surowe wiersze/kolumny"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 4,  "normalizer.py",      "Normalizacja liczb (EU↔US) i dat",                          "Wartości Decimal"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 5,  "semantic_matcher.py","Dopasowanie kodów produktów między wariantami nazw dostawcy","Lista dopasowanych par"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 6,  "enhanced_comparator.py","Porównanie ilości/ceny/wartości netto, warunki płatności (mapa SAP), kontrola arytmetyczna (ilość×cena=netto)", "Lista znalezisk"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 7,  "typo_detector.py",   "Skanowanie pomyłek I/1 O/0 i brakujących pól",              "Znaleziska literówek"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 8,  "ai_validator.py",    "Wyślij znaleziska do Claude API → risk_score 0-100 + overall_risk", "Ocena AI"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 9,  "db.py",              "Zapisz wynik w tabeli compare_results",                     "compare_id"),
    ("Porównanie dokumentów (ZO vs PI / FK vs LP)", 10, "Przeglądarka",       "Wyrenderuj /compare/result/<id> z kolorowo-kodowanymi znaleziskami", "Użytkownik przegląda"),

    ("Porównanie grafiki (Artwork)", 1,  "Przeglądarka",       "Użytkownik przesyła PDF referencyjny i od dostawcy",        "POST /api/artwork/compare"),
    ("Porównanie grafiki (Artwork)", 2,  "artwork_comparator.py","Renderuj strony PDF w 150 DPI (podgląd) i 220 DPI (OCR)", "Obrazy PIL"),
    ("Porównanie grafiki (Artwork)", 3,  "artwork_comparator.py","Różnica pikseli → mapa intensywności regionów (strefy czerwona/żółta/niebieska)", "Heatmapa różnic"),
    ("Porównanie grafiki (Artwork)", 4,  "artwork_comparator.py","Ekstrakcja tekstu PyMuPDF → fallback Tesseract OCR",        "Bloki tekstowe"),
    ("Porównanie grafiki (Artwork)", 5,  "artwork_comparator.py","Wyodrębnij pola opakowania: EAN, REF, Rewizja, Data, Gauge, Kolor, Format", "Pola strukturalne"),
    ("Porównanie grafiki (Artwork)", 6,  "barcode_validator.py","Suma kontrolna EAN-13 + parsowanie GS1-128 AI + skanowanie zxingcpp", "Raport kodów kreskowych"),
    ("Porównanie grafiki (Artwork)", 7,  "db.py",              "Zapisz ArtworkCompareResult + barcode_report",               "artwork_id"),
    ("Porównanie grafiki (Artwork)", 8,  "Przeglądarka",       "Wyrenderuj artwork_report.html z nakładką diff + sekcją kodów kreskowych", "Użytkownik przegląda"),

    ("Pętla uczenia AI", 1,  "Przeglądarka (kierownik)", "Prześlij przykładowy PDF dostawcy",                         "POST /api/ai/learn-document"),
    ("Pętla uczenia AI", 2,  "ai_learning.py",       "Wyślij tekst PDF do Claude API z promptem do wykrywania kolumn", "Odpowiedź Claude"),
    ("Pętla uczenia AI", 3,  "ai_learning.py",       "Parsuj sugestie (role kolumn, nazwa dostawcy)",                 "Wiersz pending_updates"),
    ("Pętla uczenia AI", 4,  "Przeglądarka (kierownik)","Przejrzyj oczekujące sugestie pod /ai/suggestions",          "Zatwierdź/odrzuć"),
    ("Pętla uczenia AI", 5,  "app.py",               "POST /api/ai/apply-profile → scal z profilem dostawcy",        "Profil zaktualizowany"),

    ("Eksport PDF", 1, "Przeglądarka", "Kliknij 'Eksportuj PDF' na stronie wyników",                   "GET /api/export/pdf/<id>"),
    ("Eksport PDF", 2, "export_engine.py", "Pobierz wynik z DB, zbuduj dokument ReportLab",            "PDF w pamięci"),
    ("Eksport PDF", 3, "export_engine.py", "Osadź tabelę znalezisk, sumy, podsumowanie AI, logo",      "Ostylowany PDF"),
    ("Eksport PDF", 4, "Przeglądarka", "Pobierz PDF (streaming download)",                             "Plik zapisany lokalnie"),

    ("Eksport Excel", 1, "Przeglądarka", "Kliknij 'Eksportuj Excel' na stronie wyników",               "GET /api/export/excel/<id>"),
    ("Eksport Excel", 2, "export_engine.py", "Zbuduj skoroszyt openpyxl: arkusze Znaleziska, Podsumowanie, Surowe dane", "XLSX w pamięci"),
    ("Eksport Excel", 3, "export_engine.py", "_xsafe() usuwa prefiksy wstrzyknięcia formuł Excel (=,+,@)", "Bezpieczne wartości komórek"),
    ("Eksport Excel", 4, "Przeglądarka", "Pobierz XLSX (streaming download)",                          "Plik zapisany lokalnie"),

    ("Kreator profilu dostawcy", 1, "Przeglądarka (kierownik)", "Otwórz /suppliers/wizard",            "Strona kreatora"),
    ("Kreator profilu dostawcy", 2, "Przeglądarka (kierownik)", "Prześlij przykładowy PDF → POST /api/suppliers/preview_table", "Podgląd tabeli"),
    ("Kreator profilu dostawcy", 3, "Przeglądarka (kierownik)", "Przypisz role kolumnom (ref/qty/price/net/lot) klikając", "JSON mapy ról"),
    ("Kreator profilu dostawcy", 4, "Przeglądarka (kierownik)", "Skonfiguruj tolerancje, warunki płatności, synonimy",   "JSON profilu"),
    ("Kreator profilu dostawcy", 5, "app.py", "POST /api/suppliers → zapisz do DB",                    "Profil dostawcy zapisany"),
    ("Kreator profilu dostawcy", 6, "enhanced_comparator.py", "Automatyczne wykrywanie dostawcy przy porównaniu → załaduj profil", "Porównanie używa profilu"),
]

# ─── Macierz ról/uprawnień ───────────────────────────────────────────────────
ROLE_MATRIX = [
    # (obszar funkcji, anonimowy, użytkownik, kierownik, superuser, admin)
    ("Logowanie / przypomnienie hasła",            "Tak", "—",    "—",    "—",    "—"),
    ("Widok panelu głównego",                      "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Uruchomienie porównania dokumentów",         "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Uruchomienie porównania grafiki",            "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Eksport PDF / Excel",                        "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Przeglądanie historii porównań",             "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Oznaczanie / komentowanie znalezisk",        "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Wyszukiwanie",                               "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Przeglądanie śledzenia dostaw",              "Nie", "Tak",  "Tak",  "Tak",  "Tak"),
    ("Tworzenie / edycja dostaw",                  "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Zatwierdzanie / odrzucanie grafik",          "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Zarządzanie profilami dostawców",            "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Uczenie AI (learn-document)",                "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Zatwierdzanie sugestii AI",                  "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Przeglądanie raportów / analityki",          "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Zarządzanie szablonami dokumentów",          "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Generowanie udostępnionych linków",          "Nie", "Nie",  "Tak",  "Tak",  "Tak"),
    ("Przeglądanie panelu administratora",         "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Tworzenie / usuwanie użytkowników",          "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Zmiana ról użytkowników",                    "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Przeglądanie dziennika audytu",              "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Uruchamianie migracji DB",                   "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Zarządzanie integracjami",                   "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Czyszczenie cache / reindeksacja",           "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Podgląd konfiguracji systemu (ukryte sekrety)","Nie","Nie", "Nie",  "Nie",  "Tak"),
    ("Pobieranie kopii zapasowej DB",              "Nie", "Nie",  "Nie",  "Nie",  "Tak"),
    ("Podgląd udostępnionego linku (bez logowania)","Tak","Tak",  "Tak",  "Tak",  "Tak"),
    ("Endpoint health check",                      "Tak", "Tak",  "Tak",  "Tak",  "Tak"),
]

# ─── Tabele bazy danych ───────────────────────────────────────────────────────
DB_TABLES = [
    ("users",                  "Konta użytkowników",                           "id, email, password_hash, name, role, active, created_at, last_login, avatar, preferences, timezone, language, onboarding_done"),
    ("compare_results",        "Wyniki porównań dokumentów",                   "id, user_id, supplier_id, doc_type, file_a, file_b, result_json, ai_risk, ai_score, flagged, created_at, delivery_id"),
    ("artwork_compare_results","Wyniki porównań grafik",                        "id, user_id, file_a, file_b, diff_json, barcode_report, approved_by, approved_at, status, created_at"),
    ("suppliers",              "Profile dostawców",                            "id, name, code, profile_json, created_at, updated_at, created_by"),
    ("pending_updates",        "Oczekujące sugestie aktualizacji profili AI",  "id, supplier_id, suggestion_json, source_file, created_at, status, reviewed_by, reviewed_at"),
    ("deliveries",             "Śledzenie dostaw / zamówień",                  "id, po_number, supplier_id, status, created_at, updated_at, notes_json, closed_at, closed_by"),
    ("delivery_events",        "Dziennik zdarzeń dostawy",                     "id, delivery_id, event_type, event_json, created_at, user_id"),
    ("document_templates",     "Szablony dokumentów wielokrotnego użytku",     "id, name, doc_type, supplier_id, column_map_json, created_at, created_by"),
    ("integrations",           "Konfiguracje integracji ERP/zewnętrznych",     "id, name, type, config_json, active, created_at"),
    ("notifications",          "Powiadomienia użytkownika",                    "id, user_id, type, message, read, link, created_at"),
    ("audit_log",              "Pełna ścieżka audytu",                         "id, user_id, action, entity_type, entity_id, detail_json, ip, user_agent, created_at"),
    ("tags",                   "Tagowanie obiektów",                           "id, name, color, created_by"),
    ("compare_tags",           "Złączenie: porównania ↔ tagi",                 "compare_id, tag_id"),
    ("comments",               "Komentarze do dowolnego obiektu",              "id, user_id, entity_type, entity_id, body, created_at, updated_at"),
    ("shared_links",           "Publiczne tokeny udostępniania",               "id, compare_id, token, expires_at, created_by, created_at"),
    ("password_reset_tokens",  "Tokeny resetowania hasła",                     "id, user_id, token_hash, expires_at, used"),
    ("migrations",             "Dziennik zastosowanych migracji DB",           "id, name, applied_at"),
    ("ai_usage_log",           "Dziennik wywołań Claude API (śledzenie kosztów)","id, user_id, route, tokens_in, tokens_out, cost_usd, created_at"),
]

# ─── Budowanie arkuszy ────────────────────────────────────────────────────────

def build_routes_sheet(wb):
    ws = wb.create_sheet("01_Trasy")
    ws.sheet_view.showGridLines = False

    headers    = ["#", "Kategoria", "Metoda", "URL", "Funkcja", "Auth", "CSRF", "Opis", "Używane moduły"]
    col_widths = [5, 22, 8, 45, 30, 12, 7, 55, 42]

    ws.merge_cells("A1:I1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Mapa Tras  (wszystkie punkty końcowe Flask)"
    t.font = _font(bold=True, colour=C_WHITE, sz=14)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 30

    ws.merge_cells("A2:I2")
    s = ws["A2"]
    s.value = f"Wygenerowano: 2026-06-04  |  Łączna liczba tras: {len(ROUTES)}"
    s.font = _font(italic=True, colour="444444", sz=9)
    s.fill = _fill(C_GREY)
    s.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[2].height = 16

    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        _hdr(ws, 3, ci, h, bg=C_BLUE, sz=10)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[3].height = 22

    METHOD_COLOURS = {
        "GET": "D9EAD3", "POST": "FCE5CD", "PUT": "FFF2CC",
        "DELETE": "F4CCCC", "PATCH": "D0E4F7",
    }
    current_cat = None
    row = 4
    for idx, (cat, method, url, fn, auth, csrf, desc, mods) in enumerate(ROUTES, 1):
        if cat != current_cat:
            ws.merge_cells(f"A{row}:I{row}")
            c = ws.cell(row=row, column=1, value=f"  {cat}")
            c.font = _font(bold=True, colour=C_WHITE, sz=10)
            c.fill = _fill(C_BLUE)
            c.alignment = _align(h="left", v="center", wrap=False)
            c.border = _border()
            ws.row_dimensions[row].height = 18
            row += 1
            current_cat = cat

        bg = C_LBLUE if idx % 2 == 0 else C_WHITE
        m_bg = METHOD_COLOURS.get(method, C_WHITE)

        _cell(ws, row, 1, idx,  bg=bg, align="center")
        _cell(ws, row, 2, cat,  bg=bg)
        c_m = ws.cell(row=row, column=3, value=method)
        c_m.font = _font(bold=True)
        c_m.fill = _fill(m_bg)
        c_m.alignment = _align(h="center", v="top")
        c_m.border = _border()
        _cell(ws, row, 4, url,  bg=bg)
        _cell(ws, row, 5, fn,   bg=bg)
        a_bg = C_GREEN if auth == "Nie" else (C_RED if auth in ("admin","kierownik") else C_WHITE)
        _cell(ws, row, 6, auth,  bg=a_bg, align="center")
        _cell(ws, row, 7, csrf,  bg=bg,   align="center")
        _cell(ws, row, 8, desc,  bg=bg)
        _cell(ws, row, 9, mods,  bg=bg)
        ws.row_dimensions[row].height = 30
        row += 1

    ws.freeze_panes = "A4"

    legend_row = row + 1
    ws.merge_cells(f"A{legend_row}:I{legend_row}")
    l = ws.cell(row=legend_row, column=1,
                value="Legenda kolorów  |  Metoda: GET=zielony  POST=pomarańczowy  PUT=żółty  DELETE=czerwony  |  Auth: zielony=publiczny  czerwony=admin/kierownik  biały=każdy zalogowany")
    l.font = _font(italic=True, sz=8, colour="555555")
    l.alignment = Alignment(horizontal="left", vertical="center")


def build_modules_sheet(wb):
    ws = wb.create_sheet("02_Moduły")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:D1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Mapa Modułów / Komponentów"
    t.font = _font(bold=True, colour=C_WHITE, sz=14)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 30

    headers = ["Moduł / Plik", "Przeznaczenie", "Kluczowe zależności", "Wywołujące moduły"]
    widths  = [28, 62, 36, 46]
    for ci, (h, w) in enumerate(zip(headers, widths), 1):
        _hdr(ws, 2, ci, h, bg=C_BLUE)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[2].height = 20

    for ri, (mod, purpose, deps, callers) in enumerate(MODULES, 3):
        bg = C_LBLUE if ri % 2 == 0 else C_WHITE
        _cell(ws, ri, 1, mod,     bg=bg, bold=True)
        _cell(ws, ri, 2, purpose, bg=bg)
        _cell(ws, ri, 3, deps,    bg=bg)
        _cell(ws, ri, 4, callers, bg=bg)
        ws.row_dimensions[ri].height = 36

    ws.freeze_panes = "A3"

    note_row = len(MODULES) + 4
    ws.merge_cells(f"A{note_row}:D{note_row+5}")
    note_text = (
        "Uwagi architektoniczne:\n"
        "• Wszystkie trasy Flask znajdują się w app.py (~3 300 linii). Trasy importują z modułów potoku według potrzeb.\n"
        "• db.py zapewnia ujednoliconą abstrakcję DB: SQLite (dev) ↔ PostgreSQL (prod). Całe SQL przechodzi przez db.get_db().\n"
        "• normalizer.py obsługuje niejednoznaczność europejskich liczb: '1.234' może oznaczać 1 234 lub 1,234 — rozwiązane heurystyką 3-cyfrową.\n"
        "• semantic_matcher.py używa rapidfuzz + sklearn TF-IDF do rozmytego dopasowania kodów produktów między wariantami nazw dostawcy.\n"
        "• enhanced_comparator.py to główny silnik i używa SAP_PAYMENT_MAP do sprawdzania równoważności warunków płatności.\n"
        "• ai_validator.py i ai_learning.py wywołują Anthropic Claude API (wymaga ANTHROPIC_API_KEY w pliku .env)."
    )
    n = ws.cell(row=note_row, column=1, value=note_text)
    n.font = _font(sz=9, italic=True, colour="333333")
    n.fill = _fill(C_TEAL)
    n.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    n.border = _border()
    ws.row_dimensions[note_row].height = 110


def build_flows_sheet(wb):
    ws = wb.create_sheet("03_ProcesyKrokPoCroku")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:E1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Kluczowe Procesy (krok po kroku)"
    t.font = _font(bold=True, colour=C_WHITE, sz=14)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 30

    headers = ["Proces", "Krok", "Aktor / Komponent", "Działanie", "Wynik / Notatki"]
    widths  = [35, 6, 26, 62, 42]
    for ci, (h, w) in enumerate(zip(headers, widths), 1):
        _hdr(ws, 2, ci, h, bg=C_BLUE)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[2].height = 20

    row = 3
    current_flow = None
    for (flow, step, actor, action, output) in FLOWS:
        if flow != current_flow:
            if current_flow is not None:
                row += 1
            ws.merge_cells(f"A{row}:E{row}")
            c = ws.cell(row=row, column=1, value=f"  PROCES: {flow}")
            c.font = _font(bold=True, colour=C_WHITE, sz=11)
            c.fill = _fill(C_NAVY)
            c.alignment = _align(h="left", v="center", wrap=False)
            c.border = _border()
            ws.row_dimensions[row].height = 22
            row += 1
            current_flow = flow

        bg = C_LBLUE if step % 2 == 0 else C_WHITE
        _cell(ws, row, 1, flow,   bg=bg)
        _cell(ws, row, 2, step,   bg=bg, align="center")
        _cell(ws, row, 3, actor,  bg=bg, bold=True)
        _cell(ws, row, 4, action, bg=bg)
        _cell(ws, row, 5, output, bg=bg)
        ws.row_dimensions[row].height = 30
        row += 1

    ws.freeze_panes = "A3"


def build_roles_sheet(wb):
    ws = wb.create_sheet("04_MacierzUprawnien")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:F1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Macierz Ról / Uprawnień"
    t.font = _font(bold=True, colour=C_WHITE, sz=14)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 30

    headers = ["Funkcja / Akcja", "Anonimowy", "Użytkownik", "Kierownik", "Superuser", "Admin"]
    widths  = [52, 12, 13, 13, 13, 10]
    for ci, (h, w) in enumerate(zip(headers, widths), 1):
        _hdr(ws, 2, ci, h, bg=C_BLUE)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[2].height = 20

    for ri, (feature, anon, user, mgr, su, adm) in enumerate(ROLE_MATRIX, 3):
        bg = C_LBLUE if ri % 2 == 0 else C_WHITE
        _cell(ws, ri, 1, feature, bg=bg, bold=True)

        def _perm(ws, r, c, val, bg):
            v = val.strip()
            if v == "Tak":
                pb = "C6EFCE"; pf = "276221"
            elif v == "Nie":
                pb = "FFCCCC"; pf = "9C0006"
            else:
                pb = bg; pf = "666666"
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = _font(bold=(v == "Tak"), colour=pf, sz=10)
            cell.fill = _fill(pb)
            cell.alignment = _align(h="center", v="center", wrap=False)
            cell.border = _border()

        _perm(ws, ri, 2, anon, bg)
        _perm(ws, ri, 3, user, bg)
        _perm(ws, ri, 4, mgr,  bg)
        _perm(ws, ri, 5, su,   bg)
        _perm(ws, ri, 6, adm,  bg)
        ws.row_dimensions[ri].height = 22

    ws.freeze_panes = "A3"

    note_row = len(ROLE_MATRIX) + 4
    ws.merge_cells(f"A{note_row}:F{note_row+2}")
    note = ws.cell(row=note_row, column=1, value=(
        "Hierarchia ról (rosnąco): anonimowy < użytkownik < kierownik < superuser < admin\n"
        "• Superuser ma te same uprawnienia co admin w większości obszarów — dokładne rozdzielenie zdefiniowane w app.py dla każdej trasy.\n"
        "• Ochrona CSRF dotyczy wszystkich tras POST/PUT/DELETE modyfikujących dane (patrz arkusz Trasy)."
    ))
    note.font = _font(sz=9, italic=True)
    note.fill = _fill(C_YELLOW)
    note.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    note.border = _border()
    ws.row_dimensions[note_row].height = 55


def build_db_sheet(wb):
    ws = wb.create_sheet("05_BazaDanych")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:C1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Przegląd Tabel Bazy Danych"
    t.font = _font(bold=True, colour=C_WHITE, sz=14)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 30

    headers = ["Tabela", "Przeznaczenie", "Kluczowe kolumny"]
    widths  = [28, 42, 92]
    for ci, (h, w) in enumerate(zip(headers, widths), 1):
        _hdr(ws, 2, ci, h, bg=C_BLUE)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[2].height = 20

    for ri, (table, purpose, cols) in enumerate(DB_TABLES, 3):
        bg = C_LBLUE if ri % 2 == 0 else C_WHITE
        _cell(ws, ri, 1, table,   bg=bg, bold=True)
        _cell(ws, ri, 2, purpose, bg=bg)
        _cell(ws, ri, 3, cols,    bg=bg)
        ws.row_dimensions[ri].height = 36

    ws.freeze_panes = "A3"

    note_row = len(DB_TABLES) + 4
    ws.merge_cells(f"A{note_row}:C{note_row+4}")
    note = ws.cell(row=note_row, column=1, value=(
        "Uwagi dotyczące bazy danych:\n"
        "• Wszystkie tabele są tworzone przy pierwszym uruchomieniu przez migrate_db.py. Schemat jest wyłącznie przyrostowy (nigdy destrukcyjny).\n"
        "• Klucze główne to auto-incrementujące liczby całkowite. Klucze obce używają referencji id.\n"
        "• Kolumny JSON (result_json, profile_json, diff_json itp.) przechowują słowniki Pythona jako tekst JSON.\n"
        "• db.py tłumaczy dialekty SQL: SQLite używa symboli ?, PostgreSQL używa %s. Abstrakcja jest przezroczysta.\n"
        "• Pula połączeń: PostgreSQL używa psycopg2 ThreadedConnectionPool; SQLite używa połączeń thread-local."
    ))
    note.font = _font(sz=9, italic=True)
    note.fill = _fill(C_TEAL)
    note.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    note.border = _border()
    ws.row_dimensions[note_row].height = 85


def build_index_sheet(wb):
    ws = wb.active
    ws.title = "00_Indeks"
    ws.sheet_view.showGridLines = False

    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 72
    ws.column_dimensions["D"].width = 20

    ws.merge_cells("A1:D1")
    t = ws["A1"]
    t.value = "DocCompare v6 — Mapa Procesów Aplikacji"
    t.font = _font(bold=True, colour=C_WHITE, sz=18)
    t.fill = _fill(C_NAVY)
    t.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[1].height = 42

    ws.merge_cells("A2:D2")
    s = ws["A2"]
    s.value = "ACME  |  DocCompare v6  |  Wygenerowano: 2026-06-04"
    s.font = _font(italic=True, colour="555555", sz=10)
    s.fill = _fill(C_GREY)
    s.alignment = _align(h="center", v="center", wrap=False)
    ws.row_dimensions[2].height = 18

    sheets = [
        ("01_Trasy",                f"Wszystkie punkty końcowe Flask ({len(ROUTES)} tras) — metoda, URL, auth, moduły"),
        ("02_Moduły",               "Moduły/pliki Python — przeznaczenie, zależności, wywołujące"),
        ("03_ProcesyKrokPoCroku",   "Kluczowe procesy krok po kroku (porównanie, grafika, AI, eksport, kreator)"),
        ("04_MacierzUprawnien",     "Macierz ról/uprawnień — anonimowy → admin"),
        ("05_BazaDanych",           f"Przegląd tabel bazy danych ({len(DB_TABLES)} tabel)"),
    ]

    _hdr(ws, 4, 1, "#",       bg=C_BLUE, sz=10)
    _hdr(ws, 4, 2, "Arkusz",  bg=C_BLUE, sz=10)
    _hdr(ws, 4, 3, "Zawartość", bg=C_BLUE, sz=10)
    ws.row_dimensions[4].height = 20

    for ri, (name, desc) in enumerate(sheets, 5):
        bg = C_LBLUE if ri % 2 == 0 else C_WHITE
        _cell(ws, ri, 1, ri - 4,  bg=bg, align="center")
        _cell(ws, ri, 2, name,    bg=bg, bold=True)
        _cell(ws, ri, 3, desc,    bg=bg)
        ws.row_dimensions[ri].height = 22

    sr = 12
    ws.merge_cells(f"A{sr}:D{sr}")
    ws.cell(row=sr, column=1, value="Podsumowanie aplikacji").font = _font(bold=True, sz=12, colour=C_WHITE)
    ws.cell(row=sr, column=1).fill = _fill(C_BLUE)
    ws.cell(row=sr, column=1).alignment = _align(h="left", v="center")
    ws.row_dimensions[sr].height = 22

    summary = [
        ("Stos technologiczny", "Python 3.11 · Flask · SQLite (dev) / PostgreSQL (prod) · Gunicorn"),
        ("Frontend",            "Szablony Jinja2 · Alpine.js · Tailwind CSS · Chart.js · Dropzone.js"),
        ("AI",                  "Anthropic Claude API (ai_validator.py, ai_learning.py)"),
        ("Ekstrakcja PDF",      "Camelot → pdfplumber → fallback na koordynaty tekstu (table_extractor.py)"),
        ("Kody kreskowe",       "zxingcpp (preferowany) → fallback pyzbar · EAN-13 + GS1-128"),
        ("Eksport",             "ReportLab (PDF) · openpyxl (Excel)"),
        ("Wdrożenie",           "Render.com (Frankfurt EU) · render.yaml · Sentry · Resend"),
        ("Trasy",               f"{len(ROUTES)} punktów końcowych Flask w {len(set(r[0] for r in ROUTES))} kategoriach"),
        ("Tabele DB",           f"{len(DB_TABLES)} tabel · wszystkie zapytania przez db.get_db() z parametrami"),
        ("Użytkownicy",         "Role: użytkownik < kierownik < superuser < admin · CSRF na wszystkich mutacjach"),
    ]
    for ri2, (k, v) in enumerate(summary, sr + 1):
        bg = C_LBLUE if ri2 % 2 == 0 else C_WHITE
        _cell(ws, ri2, 1, k, bg=bg, bold=True)
        ws.merge_cells(f"B{ri2}:D{ri2}")
        c = ws.cell(row=ri2, column=2, value=v)
        c.font = _font()
        c.fill = _fill(bg)
        c.alignment = _align(h="left", v="center", wrap=True)
        c.border = _border()
        ws.row_dimensions[ri2].height = 20


def main():
    wb = openpyxl.Workbook()
    build_index_sheet(wb)
    build_routes_sheet(wb)
    build_modules_sheet(wb)
    build_flows_sheet(wb)
    build_roles_sheet(wb)
    build_db_sheet(wb)

    out = "/home/user/compare/doccompare_process_map.xlsx"
    wb.save(out)
    print(f"Zapisano: {out}")
    print(f"Arkusze: {[s.title for s in wb.worksheets]}")
    print(f"Trasy: {len(ROUTES)}  |  Moduły: {len(MODULES)}  |  Przepływy: {len(FLOWS)}  |  Tabele DB: {len(DB_TABLES)}")


if __name__ == "__main__":
    main()
