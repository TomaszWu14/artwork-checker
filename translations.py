TRANSLATIONS = {
    # Navigation / sidebar
    "nav.dashboard":        {"pl": "Panel główny",       "en": "Dashboard"},
    "nav.zakupy":           {"pl": "Zakupy",              "en": "Purchasing"},
    "nav.compare":          {"pl": "Porównaj dokumenty",  "en": "Compare documents"},
    "nav.history":          {"pl": "Historia",            "en": "History"},
    "nav.sad":              {"pl": "Zatwierdź SAD",       "en": "Approve SAD"},
    "nav.bundle":           {"pl": "Raport kontenerowy",  "en": "Container report"},
    "nav.agent_proforma":   {"pl": "Agent pro-formy",     "en": "Proforma agent"},
    "nav.transport":        {"pl": "Transport",           "en": "Transport"},
    "nav.transport_queue":  {"pl": "Kolejka kontenerów",  "en": "Container queue"},
    "nav.tracking":         {"pl": "Śledzenie",           "en": "Tracking"},
    "nav.spedycja":         {"pl": "Spedycja",            "en": "Freight"},
    "nav.dispatch":         {"pl": "Spedytorzy / kierowcy","en": "Forwarders / drivers"},
    "nav.magazyn":          {"pl": "Magazyn",             "en": "Warehouse"},
    "nav.warehouse":        {"pl": "Rejestr przyjęć",     "en": "Receipt register"},
    "nav.artwork":          {"pl": "Artwork",             "en": "Artwork"},
    "nav.artwork_compare":  {"pl": "Porównaj artwork",    "en": "Compare artwork"},
    "nav.artwork_history":  {"pl": "Historia artwork",    "en": "Artwork history"},
    "nav.dane_ref":         {"pl": "Dane ref.",           "en": "Ref. data"},
    "nav.currencies":       {"pl": "Kursy walut",         "en": "Exchange rates"},
    "nav.products":         {"pl": "Baza produktów",      "en": "Product database"},
    "nav.suppliers":        {"pl": "Dostawcy",            "en": "Suppliers"},
    "nav.library":          {"pl": "Biblioteka PO",       "en": "PO Library"},
    "nav.data_hub":         {"pl": "Hub danych",          "en": "Data hub"},
    "nav.admin":            {"pl": "Administracja",       "en": "Administration"},
    # Dashboard
    "db.greeting":          {"pl": "Dzień dobry",         "en": "Good morning"},
    "db.subtitle":          {"pl": "Wybierz moduł aby rozpocząć pracę.", "en": "Select a module to get started."},
    "db.open":              {"pl": "Otwórz",              "en": "Open"},
    # Common actions
    "btn.save":             {"pl": "Zapisz",              "en": "Save"},
    "btn.cancel":           {"pl": "Anuluj",              "en": "Cancel"},
    "btn.delete":           {"pl": "Usuń",                "en": "Delete"},
    "btn.add":              {"pl": "Dodaj",               "en": "Add"},
    "btn.edit":             {"pl": "Edytuj",              "en": "Edit"},
    "btn.close":            {"pl": "Zamknij",             "en": "Close"},
    "btn.search":           {"pl": "Szukaj",              "en": "Search"},
    "btn.refresh":          {"pl": "Odśwież",             "en": "Refresh"},
    # Language switcher
    "lang.switch_to_en":    {"pl": "English",             "en": "English"},
    "lang.switch_to_pl":    {"pl": "Polski",              "en": "Polski"},
    "lang.current":         {"pl": "PL",                  "en": "EN"},
}

def t(key: str, lang: str = "pl") -> str:
    entry = TRANSLATIONS.get(key)
    if not entry:
        return key
    return entry.get(lang, entry.get("pl", key))
