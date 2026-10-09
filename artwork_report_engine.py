"""
artwork_report_engine.py — generator strukturalnego raportu porownania artworkow.

Produkuje obiekt ArtworkReport gotowy do renderowania przez Jinja2
(templates/artwork_report.html) lub do eksportu HTML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ─── AI MODEL CONFIGURATION ──────────────────────────────────────────────────

_DEFAULT_REPORT_MODEL = os.environ.get("ARTWORK_AI_MODEL", "claude-haiku-4-5-20251001")

# ─── API KEY HELPER ──────────────────────────────────────────────────────────

try:
    from ai_validator import _get_api_key
except ImportError:
    def _get_api_key():  # type: ignore[misc]
        return os.environ.get("ANTHROPIC_API_KEY", "")


# ─── STATUS HELPERS ───────────────────────────────────────────────────────────

CHECKLIST_ITEMS = [
    # (klucz, etykieta, poziom krytyczności)
    ("ref_present",       "REF / Nr katalogowy",                        "critical"),
    ("ean_present",       "EAN-13 / GTIN",                              "critical"),
    ("ce_mark",           "Oznaczenie CE + nr jednostki notyfikowanej", "critical"),
    ("single_use_symbol", "Symbol jednorazowego użycia",                "critical"),
    ("sterility_mark",    "Oznaczenie sterylności (STERILE/EO)",        "critical"),
    ("lot_placeholder",   "Placeholder LOT/Nr serii",                   "critical"),
    ("exp_placeholder",   "Placeholder EXP/Data ważności",              "critical"),
    ("warnings_present",  "Ostrzeżenia (CAUTION/WARNING)",              "important"),
    ("manufacturer_info", "Dane producenta / adres",                    "important"),
    ("revision_present",  "Numer rewizji / wersji dokumentu",           "important"),
    ("product_name",      "Nazwa produktu",                             "important"),
    ("translations_ok",   "Tłumaczenia (PL/EN/DE i inne)",             "important"),
    ("colors_spec",       "Specyfikacja kolorów (CMYK/Pantone)",        "info"),
    ("dimensions_ok",     "Format / rozmiar arkusza",                   "info"),
    ("barcode_checksum",  "Suma kontrolna kodów kreskowych (GS1)",      "critical"),
    ("barcode_consistency","Spójność: kod kreskowy ↔ tekst",            "critical"),
]

STATUS_CLASSES = {
    "ok":        "status-ok",
    "zgodny":    "status-ok",
    "wzorzec":   "status-wzorzec",
    "sprawdz":   "status-sprawdz",
    "uwagi":     "status-uwagi",
    "uwaga":     "status-uwagi",
    "szablon":   "status-szablon",
    "niezgodnosc": "status-error",
    "error":     "status-error",
    "krytyczny": "status-error",
    "brak":      "status-brak",
    "rozny":     "status-rozny",
    "format":    "status-format",
    "info":      "status-info",
    "literowka": "status-literowka",
    "tylko master": "status-format",
}

MSG_LEVELS = {
    "ok":       "msg-ok",
    "uwaga":    "msg-uwaga",
    "krytyczny":"msg-krytyczny",
    "info":     "msg-info",
}


def _status_class(status: str) -> str:
    if not status:
        return "status-info"
    return STATUS_CLASSES.get(status.lower().strip(), "status-info")


def _msg_class(level: str) -> str:
    if not level:
        return "msg-info"
    return MSG_LEVELS.get(level.lower().strip(), "msg-info")


# ─── DATA STRUCTURES ──────────────────────────────────────────────────────────

@dataclass
class ReportFile:
    filename: str
    role: str
    size: str
    ean: str
    revision: str
    status: str

    @property
    def status_class(self) -> str:
        return _status_class(self.status)


@dataclass
class ReportRow:
    element: str
    master: str
    carton: str
    pouch: str
    status: str

    @property
    def status_class(self) -> str:
        return _status_class(self.status)


@dataclass
class ReportMessage:
    level: str   # "OK" | "UWAGA" | "KRYTYCZNY" | "INFO"
    text: str

    @property
    def msg_class(self) -> str:
        return _msg_class(self.level)

    @property
    def label(self) -> str:
        return f"[{self.level.upper()}]"


@dataclass
class ArtworkReport:
    # ── nagłówek ──────────────────────────────────────────────────────────────
    product_code: str = ""
    product_name: str = ""
    analysis_date: str = ""
    master_version: str = ""
    factory_version: str = ""

    # ── statystyki ────────────────────────────────────────────────────────────
    critical_count: int = 0
    warning_count:  int = 0
    ok_count:       int = 0

    # ── sekcje ────────────────────────────────────────────────────────────────
    analyzed_files: list[ReportFile]    = field(default_factory=list)
    variable_data:  list[ReportRow]     = field(default_factory=list)
    gs1_messages:   list[ReportMessage] = field(default_factory=list)
    color_spec:     list[ReportRow]     = field(default_factory=list)
    color_note:     str                 = ""
    texts:          list[ReportRow]     = field(default_factory=list)
    graphics:       list[ReportRow]     = field(default_factory=list)
    dimensions:     list[ReportRow]     = field(default_factory=list)
    manufacturer:   list[ReportRow]     = field(default_factory=list)
    section_comparison: list            = field(default_factory=list)  # AI sekcja-po-sekcji
    icon_comparison:    list            = field(default_factory=list)  # pary ikon A vs B

    # ── lista kontrolna MDR ───────────────────────────────────────────────────
    checklist:        list = field(default_factory=list)   # [{key, label, severity, status, note}]

    # ── weryfikacja kodów kreskowych ──────────────────────────────────────────
    barcode_messages:    list[ReportMessage] = field(default_factory=list)
    barcode_summary:     dict               = field(default_factory=dict)
    # {eans_a: [...], eans_b: [...], lot_a, lot_b, gs1_a, gs1_b}

    # ── weryfikacja wydruku dostawcy (dane zmienne B) ─────────────────────────
    supplier_validation: list = field(default_factory=list)
    # [{field, value, status(ok/warning/error/info), note}]

    # ── rekomendacja zatwierdzenia (z AI lub heurystyki) ──────────────────────
    approval_recommendation: str = ""   # APPROVED|APPROVED_WITH_NOTES|NEEDS_REVISION|REJECTED
    approval_reason:         str = ""
    approval_confidence:     int = 0    # 0-100 confidence in the approval recommendation
    ai_summary:              str = ""
    ai_comment:              str = ""   # AI-generated narrative comment (Word/HTML report)

    # ── tabelaryczne sekcje porównania (nowy format raportu) ─────────────────
    comparison_sections: list = field(default_factory=list)  # [{title, rows}]
    matched_items:       list = field(default_factory=list)  # list of str (zgodne)
    diff_items:          list = field(default_factory=list)  # list of str (różnice)

    # ── wizualne porównanie stron (split-screen + overlay) ────────────────────
    page_diffs: list = field(default_factory=list)
    # each item: {page_num, pixel_diff_pct, diff_regions, summary,
    #             img_a_b64, img_b_b64, img_a_annot_b64, img_b_annot_b64}

    # ── podsumowanie ──────────────────────────────────────────────────────────
    summary_errors:   list[ReportMessage] = field(default_factory=list)
    summary_warnings: list[ReportMessage] = field(default_factory=list)

    # ── tryb szablonowy (profile mode) ──────────────────────────────────────
    profile_active: bool = False
    profile_name: str = ""
    profile_fields: list = field(default_factory=list)
    # raw field_report entries with crop_a_b64/crop_b_b64 when profile_active

    # ── szczegółowy rozkład wydajności (gdy profilowanie włączone) ──────────
    performance: dict = field(default_factory=dict)

    # ── globalne porównanie kolorów (cała strona master vs dostawca) ────────
    global_color: dict = field(default_factory=dict)

    @property
    def files_count(self) -> int:
        return len(self.analyzed_files)

    @property
    def has_summary(self) -> bool:
        return bool(self.summary_errors or self.summary_warnings)


# ─── DEMO REPORT (zgodny ze zrzutami ekranu) ──────────────────────────────────

def build_demo_report() -> ArtworkReport:
    """Zwraca przykładowy raport identyczny z raportem pokazanym na zrzutach."""

    r = ArtworkReport(
        product_code    = "DM-DEMO5-G",
        product_name    = "Skirt for patients in the operating room",
        analysis_date   = date.today().strftime("%d.%m.%Y"),
        master_version  = "1Rev.01",
        factory_version = "1Rev.00",
        critical_count  = 5,
        warning_count   = 4,
        ok_count        = 12,
    )

    # ── PLIKI ANALIZOWANE ──────────────────────────────────────────────────────
    r.analyzed_files = [
        ReportFile(
            filename="DM-DEMO5-G.pdf",
            role="Etykieta master (wzorzec)",
            size="300x150 mm",
            ean="5900002608103",
            revision="1Rev.01",
            status="Wzorzec",
        ),
        ReportFile(
            filename="DM-DEMO5-G_carton_sticker.pdf",
            role="Etykieta zbiorcza (fabryczna)",
            size="300x150 mm",
            ean="5900002608103",
            revision="1Rev.00",
            status="Sprawdz",
        ),
        ReportFile(
            filename="DM-DEMO5-G_pouch.pdf",
            role="Etykieta saszetki (fabryczna)",
            size="100x130 mm",
            ean="5900002608097",
            revision="1Rev.00",
            status="Uwagi",
        ),
    ]

    # ── 1. DANE ZMIENNE ────────────────────────────────────────────────────────
    r.variable_data = [
        ReportRow("LOT", "123456789N", "— (pole puste, szablon)", "— (pole puste, szablon)", "Szablon"),
        ReportRow("Data produkcji (mfg)", "2026-05-11", "—", "—", "Szablon"),
        ReportRow("Data waznosci (exp)", "2031-05-10", "—", "—", "Szablon"),
        ReportRow("EAN — karton (13 cyfr)", "5900002608103", "5900002608103", "—", "Zgodny"),
        ReportRow("EAN — saszetka (13 cyfr)", "5900002608097", "—", "5900002608097", "Zgodny"),
        ReportRow("Pantone kolor 2", "PANTONE 299", "PANTONE 294", "PANTONE 294", "Niezgodnosc"),
    ]

    # ── 2. GS1 / UDI ──────────────────────────────────────────────────────────
    r.gs1_messages = [
        ReportMessage("OK",
            "EAN-13 (5900002608103): Cyfra kontrolna: 3. Obliczona: 3. Poprawna."),
        ReportMessage("OK",
            "EAN-13 (5900002608097): Cyfra kontrolna: 7. Obliczona: 7. Poprawna."),
        ReportMessage("OK",
            "Kod GS1-128 (liniowy, master): (01)05900002608103 — GTIN-14 prawidlowy. "
            "(17)310510 — data waznosci maj 2031 zgodna z tekstem. (10)123456789N — LOT zgodny z nadrukiem."),
        ReportMessage("UWAGA",
            "DataMatrix (pouch): Brak AI 11 (data produkcji). Sprawdzic czy wymagane przez "
            "regulacje MDR/UDI dla tej klasy wyrobu."),
        ReportMessage("KRYTYCZNY",
            "Dwa rozne GTIN na etykiecie glownej: Liniowy kod 1D = EAN kartonu (608103). "
            "Kod QR w prawym panelu = EAN saszetki (608097). Ryzyko bledu skanowania "
            "w lancuchu logistycznym."),
    ]

    # ── 3. SPECYFIKACJA KOLOROW ───────────────────────────────────────────────
    r.color_spec = [
        ReportRow("Kolor 1 — ciemny",   "PANTONE 447", "PANTONE 447", "PANTONE 447", "Zgodny"),
        ReportRow("Kolor 2 — niebieski", "PANTONE 299", "PANTONE 294", "PANTONE 294", "Rozny"),
        ReportRow("Kolor 3 — jasnoniebieski", "PANTONE 292", "BRAK", "PANTONE 292", "Brak na carton"),
        ReportRow("Dot circle (wizual)", "Cyan-niebieski", "Granatowy", "Trzy kolory", "Wizualna roznica"),
    ]
    r.color_note = (
        "Uwaga: PANTONE 292 = jasnoniebieski | PANTONE 294 = ciemnoniebieski | "
        "PANTONE 299 = intensywny cyan-niebieski. Roznica wizualnie zauważalna."
    )

    # ── 4. TEKSTY I TLUMACZENIA ────────────────────────────────────────────────
    r.texts = [
        ReportRow("EN (glowny)",       "Identyczny we wszystkich", "Identyczny", "Identyczny", "OK"),
        ReportRow("PL",                "Zgodny", "Zgodny", "Zgodny", "OK"),
        ReportRow("ET (estonski)",      "mitte-steriilne", "LITEROWKA w srodku slowa", "mitte-steriilne", "Literowka"),
        ReportRow("CS (czeski)",        "Sukne (z hacek)", "Sukne — OK", "Sukne (brak hacek)", "Pouch blad"),
        ReportRow("Pozostale 23 jezyki","Wzorzec", "Wizualnie zgodny", "Wizualnie zgodny", "OK"),
    ]

    # ── 5. ELEMENTY GRAFICZNE I SYMBOLE ────────────────────────────────────────
    r.graphics = [
        ReportRow("Logo ACME demoMedical", "OK", "OK", "OK", "OK"),
        ReportRow("REF DM-DEMO5-G",          "OK", "OK", "OK", "OK"),
        ReportRow("Symbol CE MD",             "OK", "OK", "OK", "OK"),
        ReportRow("Symbol NON STERILE",       "OK", "OK", "OK", "OK"),
        ReportRow("Ilosc — piktogram",        "10x10", "10x10", "10 (inny format)", "Format"),
        ReportRow("Rewizja etykiety",         "1Rev.01", "1Rev.00", "1Rev.00", "Niezgodnosc"),
        ReportRow("UDI placeholder",          "OK", "OK", "OK", "OK"),
        ReportRow("Ikony dat (mfg/exp)",      "OK", "OK", "OK", "OK"),
    ]

    # ── 6. ROZMIARY I SPECYFIKACJA TECHNICZNA ──────────────────────────────────
    r.dimensions = [
        ReportRow("Rozmiar etykiety",    "300x150 mm",        "300x150 mm — OK",  "100x130 mm — OK", "OK"),
        ReportRow("Wymiary kartonu",     "40x40x27 cm",       "40x40x27 cm — OK", "—",               "OK"),
        ReportRow("Wysokosc kodu 1D",    "31.75 mm",          "Nie podano",        "Nie podano",       "Info"),
        ReportRow("Dlugosc kodu 1D",     "136.10 mm",         "Nie podano",        "Nie podano",       "Info"),
        ReportRow("Adnotacje CN (chinski)", "Tak (master only)", "Brak",           "Brak",             "Tylko master"),
    ]

    # ── 7. DANE PRODUCENTA I DYSTRYBUTORA ──────────────────────────────────────
    r.manufacturer = [
        ReportRow("Producent",
                  "ACME sp. z o.o. ul. Magazynowa 20, 26-603 Radom",
                  "Identyczny", "Identyczny", "OK"),
        ReportRow("Przedstawiciel UA",   "TOW Demo Medservice",  "OK", "OK", "OK"),
        ReportRow("CH REP",             "Medlink Swiss GmbH, 8000 Zürich, Beispielstrasse 1", "OK", "OK", "OK"),
        ReportRow("Opracowal",          "Anna Przykładowa",        "OK", "OK", "OK"),
        ReportRow("Zatwierdzil",        "Ewa Testowa",          "OK", "OK", "OK"),
        ReportRow("Data zatwierdzenia", "07.01.2026",               "OK", "OK", "OK"),
    ]

    # ── PODSUMOWANIE ───────────────────────────────────────────────────────────
    r.summary_errors = [
        ReportMessage("KRYTYCZNY",
            "Niezgodnosc numeru Pantone: Specyfikacja mastera zawiera PANTONE 299, natomiast "
            "carton sticker i pouch podaja PANTONE 294. To rozne odcienie niebieskiego. "
            "Wymagane potwierdzenie ktory numer jest wlasciwy i korekta dokumentu."),
        ReportMessage("KRYTYCZNY",
            "Niezgodnosc rewizji etykiety: Etykieta glowna nosi wersje 1Rev.01, carton sticker "
            "i pouch — 1Rev.00. Fabryka musiala drukowac ze starszej wersji. Nalezy zweryfikowac "
            "czy 1Rev.01 byl przekazany do fabryki."),
        ReportMessage("KRYTYCZNY",
            "Literowka w estonskim (ET) na carton sticker: Blad w slowie \"mitte-steriilne\". "
            "Tlumaczenie medyczne wymaga 100% poprawnosci ortograficznej."),
        ReportMessage("KRYTYCZNY",
            "Dwa rozne kody GTIN na etykiecie glownej: Liniowy kod 1D = EAN kartonu "
            "(5900002608103), prawy panel QR = EAN saszetki (5900002608097). "
            "Ryzyko bledu skanowania w lancuchu logistycznym."),
        ReportMessage("KRYTYCZNY",
            "Brak Pantone 292 na carton sticker: Master i pouch zawieraja PANTONE 292. "
            "Carton sticker pomija ten kolor. Niekompletna specyfikacja kolorystyczna."),
    ]
    r.summary_warnings = [
        ReportMessage("UWAGA",
            "Czeski (CS) na pouch: Brak znaku diakrytycznego hacek w slowie \"Sukne\" "
            "(powinno byc \"Sukne\"). Mozliwa literowka lub problem z kodowaniem znakow."),
        ReportMessage("UWAGA",
            "Format ilosci na pouch: Piktogram pokazuje \"10\" zamiast \"10x10\" jak na "
            "pozostalych etykietach. Zweryfikowac poprawnosc formatu."),
        ReportMessage("UWAGA",
            "Brak AI 11 (data produkcji) w DataMatrix pouch: Sprawdzic wymagania MDR/UDI "
            "dla tej klasy wyrobu medycznego."),
        ReportMessage("UWAGA",
            "Adnotacje chinskie na masterze: Pola 230ZR i \"spodnica 40 gramow\" widoczne "
            "tylko na etykiecie zbiorczej. Potwierdzic ze nie powinny pojawiac sie na "
            "pozostalych etykietach."),
    ]

    # Wypełnij sekcje tabelaryczne z istniejących danych demo
    all_rows = r.variable_data + r.color_spec + r.dimensions + r.manufacturer
    r.comparison_sections, r.matched_items, r.diff_items = _build_comparison_sections(all_rows)
    return r


def _build_supplier_validation(field_report: list, barcode_rep: dict) -> list:
    """
    Weryfikuje dane zmienne na etykiecie dostawcy (B):
    EAN-13, LOT, EXP, kody kreskowe GS1-128.
    Zwraca listę dict: {field, value, status, note}.
    """
    import re as _re

    def _val_b(keyword: str) -> str:
        kw = keyword.lower()
        for f in field_report:
            if kw in f.get("field", "").lower():
                return f.get("val_b") or "—"
        return "—"

    def _is_real(val: str) -> bool:
        if not val or val == "—":
            return False
        cleaned = _re.sub(r"[□X\s\-_]", "", val)
        return len(cleaned) >= 2

    checks = []

    # ── EAN-13 ──
    eans_b = barcode_rep.get("eans_b", [])
    ean_ocr = _val_b("ean")
    if eans_b:
        try:
            from barcode_validator import validate_barcode_number
            # eans_b zawiera też 12-cyfrowe UPC-A i 8-cyfrowe EAN-8 (z regexu OCR),
            # więc dyspozytujemy walidację po długości zamiast wymuszać EAN-13.
            invalid = [e for e in eans_b if not validate_barcode_number(e)["valid"]]
        except Exception:
            invalid = []
        if invalid:
            checks.append({"field": "EAN-13", "value": ", ".join(eans_b),
                            "status": "error",
                            "note": "Błędna suma kontrolna: " + ", ".join(invalid)})
        else:
            checks.append({"field": "EAN-13", "value": ", ".join(eans_b),
                            "status": "ok", "note": "Suma kontrolna GS1 poprawna"})
    elif ean_ocr != "—":
        checks.append({"field": "EAN-13 (OCR)", "value": ean_ocr,
                        "status": "warning",
                        "note": "Wykryto z OCR — nie zweryfikowano z kodu kreskowego"})
    else:
        checks.append({"field": "EAN-13", "value": "nie wykryto",
                        "status": "warning",
                        "note": "Brak EAN-13 w tekście OCR etykiety dostawcy"})

    # ── LOT ──
    lot_b = _val_b("lot")
    if _is_real(lot_b):
        checks.append({"field": "LOT / Nr serii", "value": lot_b,
                        "status": "ok", "note": "Pole wypełnione prawidłowo"})
    elif lot_b == "—":
        checks.append({"field": "LOT / Nr serii", "value": "—",
                        "status": "error", "note": "LOT nie wykryto na etykiecie dostawcy"})
    else:
        checks.append({"field": "LOT / Nr serii", "value": lot_b,
                        "status": "warning", "note": "Wygląda jak niezapełniony placeholder"})

    # ── EXP / Data ważności ──
    exp_b = _val_b("exp")
    if _is_real(exp_b):
        checks.append({"field": "Data ważności (EXP)", "value": exp_b,
                        "status": "ok", "note": "Pole wypełnione prawidłowo"})
    elif exp_b == "—":
        checks.append({"field": "Data ważności (EXP)", "value": "—",
                        "status": "error", "note": "Data ważności nie wykryta"})
    else:
        checks.append({"field": "Data ważności (EXP)", "value": exp_b,
                        "status": "warning", "note": "Wygląda jak niezapełniony placeholder"})

    # ── REF ──
    ref_b = _val_b("ref")
    if _is_real(ref_b):
        checks.append({"field": "REF / Nr katalogowy", "value": ref_b,
                        "status": "ok", "note": ""})
    else:
        checks.append({"field": "REF / Nr katalogowy", "value": ref_b if ref_b != "—" else "nie wykryto",
                        "status": "warning", "note": "Sprawdź ręcznie"})

    # ── Kody kreskowe B (z barcode_report) ──
    for msg in barcode_rep.get("messages", []):
        if msg.get("source") not in ("B",):
            continue
        lvl = msg.get("level", "INFO")
        checks.append({
            "field": f"Kod ({msg.get('code_type', '?')})",
            "value": msg.get("text", ""),
            "status": "ok" if lvl == "OK" else ("error" if lvl == "KRYTYCZNY" else ("warning" if lvl == "UWAGA" else "info")),
            "note": "",
        })

    return checks


# ─── BUILDER FROM ACTUAL COMPARISON DATA ─────────────────────────────────────

def _dims_close(dims_a: str, dims_b: str, tol: float = 0.03) -> bool:
    """True gdy wszystkie liczby w dwóch napisach wymiarów (np. '362×467 mm') zgadzają
    się w granicach tolerancji względnej. Zapobiega flagowaniu różnicy formatu ARKUSZA
    PDF (margines/spad) jako różnicy wymiaru PRODUKTU — realny wymiar produktu pochodzi
    z pola specyfikacji, nie z rozmiaru strony PDF (np. 362×467 vs 367×467 mm = 1,4%)."""
    import re
    def _nums(s):
        return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", s or "")]
    na, nb = _nums(dims_a), _nums(dims_b)
    if not na or not nb or len(na) != len(nb):
        return False
    for a, b in zip(na, nb):
        denom = max(abs(a), abs(b), 1e-6)
        if abs(a - b) / denom > tol:
            return False
    return True


def build_report_from_comparison(result) -> ArtworkReport:
    """
    Buduje ArtworkReport z obiektu ArtworkCompareResult (lub jego dict).
    Wypełnia sekcje na podstawie field_report i text_diffs z porównania.
    """
    from datetime import date as _date

    # Obsługa zarówno obiektu jak i słownika
    if hasattr(result, "to_dict"):
        d = result.to_dict(include_images=False)
        file_a = result.file_a
        file_b = result.file_b
        dims_a = getattr(result, "dims_a", "") or ""
        dims_b = getattr(result, "dims_b", "") or ""
        field_report = result.field_report or []
        _profile_active = getattr(result, "profile_active", False)
        _profile_name = getattr(result, "profile_name", "")
        text_diffs = []
        section_diffs = []
        visual_page_diffs = []
        page_diffs = result.page_diffs or []
        for pd_obj in page_diffs:
            text_diffs.extend(pd_obj.text_diffs or [])
            section_diffs.extend(pd_obj.section_diffs or [])
            visual_page_diffs.append({
                "page_num":        pd_obj.page_num,
                "pixel_diff_pct":  round(pd_obj.pixel_diff_pct, 2),
                "diff_regions":    pd_obj.diff_regions or [],
                "summary":         pd_obj.summary or "",
                "img_a_b64":       pd_obj.img_a_b64 or "",
                "img_b_b64":       pd_obj.img_b_b64 or "",
                "img_a_annot_b64": pd_obj.img_a_annot_b64 or "",
                "img_b_annot_b64": pd_obj.img_b_annot_b64 or "",
            })
        critical_count = int(result.critical_count or 0)
        important_count = int(result.important_count or 0)
        ok_count = int(result.ok_count or 0)
        icon_comparison = list(result.icon_comparison or [])
    else:
        d = result if isinstance(result, dict) else {}
        file_a = d.get("file_a", "Plik A")
        file_b = d.get("file_b", "Plik B")
        dims_a = d.get("dims_a", "")
        dims_b = d.get("dims_b", "")
        field_report = d.get("field_report", [])
        _profile_active = d.get("profile_active", False)
        _profile_name = d.get("profile_name", "")
        text_diffs = []
        section_diffs = []
        visual_page_diffs = []
        page_diffs = d.get("page_diffs") or []
        for pd in page_diffs:
            text_diffs.extend(pd.get("text_diffs", []))
            section_diffs.extend(pd.get("section_diffs", []))
            visual_page_diffs.append({
                "page_num":        pd.get("page_num", 1),
                "pixel_diff_pct":  pd.get("pixel_diff_pct", 0),
                "diff_regions":    pd.get("diff_regions", []),
                "summary":         pd.get("summary", ""),
                "img_a_b64":       pd.get("img_a_b64") or "",
                "img_b_b64":       pd.get("img_b_b64") or "",
                "img_a_annot_b64": pd.get("img_a_annot_b64") or "",
                "img_b_annot_b64": pd.get("img_b_annot_b64") or "",
            })
        critical_count = d.get("critical_count", 0)
        important_count = d.get("important_count", 0)
        ok_count = d.get("ok_count", 0)
        icon_comparison = d.get("icon_comparison", [])

    import os as _os
    fa = _os.path.basename(file_a)
    fb = _os.path.basename(file_b)

    def _role_guess_b(name: str) -> str:
        n = name.lower()
        if "pouch" in n:   return "Etykieta saszetki (fabryczna)"
        if "carton" in n:  return "Etykieta zbiorcza (fabryczna)"
        return "Etykieta (fabryczna)"

    def _status_from_fields(name: str) -> str:
        n = name.lower()
        if "master" in n: return "Wzorzec"
        return "Sprawdz"

    # ── pliki ──────────────────────────────────────────────────────────────
    def _nz(v):
        return "—" if v is None else str(v)

    def _ean_from_fields() -> tuple:
        ean_a = ean_b = "—"
        for row in field_report:
            if "EAN" in row.get("field", "") or "GTIN" in row.get("field", ""):
                ean_a = _nz(row.get("val_a"))
                ean_b = _nz(row.get("val_b"))
                break
        return ean_a, ean_b

    def _rev_from_fields() -> tuple:
        for row in field_report:
            if "Rewizja" in row.get("field", "") or "Wersja" in row.get("field", ""):
                return _nz(row.get("val_a")), _nz(row.get("val_b"))
        return "—", "—"

    ean_a, ean_b = _ean_from_fields()
    rev_a, rev_b = _rev_from_fields()

    analyzed_files = [
        ReportFile(fa, "Etykieta master (wzorzec)",
                   dims_a or "—", ean_a, rev_a, "Wzorzec"),
        ReportFile(fb, _role_guess_b(fb),
                   dims_b or "—", ean_b, rev_b, "Sprawdz"),
    ]

    # ── Sekcja 1: Dane zmienne ────────────────────────────────────────────
    variable_data: list[ReportRow] = []
    color_rows: list[ReportRow]    = []
    dim_rows: list[ReportRow]      = []
    mfr_rows: list[ReportRow]      = []

    for row in field_report:
        field = row.get("field", "")
        _va   = row.get("val_a")
        _vb   = row.get("val_b")
        va    = "—" if _va is None else (str(_va) or "—")
        vb    = "—" if _vb is None else (str(_vb) or "—")
        changed = row.get("changed", False)
        sev   = row.get("severity", "info")

        if changed:
            if sev == "critical":  status = "Niezgodnosc"
            elif sev == "important": status = "Rozny"
            else:                  status = "Zmiana"
        else:
            if va == "—" and vb == "—": status = "Brak"
            else:                       status = "Zgodny"

        # Routing do odpowiednich sekcji
        fl = field.lower()
        if any(k in fl for k in ("lot", "exp", "ean", "gtin", "ref", "rewizja", "wersja")):
            # Pole zmiennych danych puste po obu stronach = szablon/placeholder
            # (np. master bez wydrukowanego LOT/EXP). Wcześniej warunek opierał się
            # na literalnym „✓", który nigdy nie był emitowany — gałąź była martwa.
            if va == vb and va in ("—", ""):
                vb_display = "— (pole puste, szablon)"
                va_display = "— (pole puste, szablon)"
                status = "Szablon"
            else:
                va_display, vb_display = va, vb
            variable_data.append(ReportRow(field, va_display, vb_display, "—", status))

        elif "kolor" in fl or "pantone" in fl or "cmyk" in fl or "specyfikacja koloru" in fl:
            color_rows.append(ReportRow(field, va, vb, "—", status))

        elif "format" in fl or "rozmiar" in fl or "gauge" in fl or "wymiar" in fl:
            dim_rows.append(ReportRow(field, va, vb, "—", status))

        elif "producent" in fl or "manufacturer" in fl or "g.w" in fl:
            mfr_rows.append(ReportRow(field, va, vb, "—", status))

        else:
            variable_data.append(ReportRow(field, va, vb, "—", status))

    # ── Sekcja 2: GS1/UDI messages ────────────────────────────────────────
    gs1_messages: list[ReportMessage] = []
    for row in field_report:
        changed = row.get("changed", False)
        sev     = row.get("severity", "info")
        field   = row.get("field", "")
        note    = row.get("note", "")
        # Normalizuj None → „—" (rekordy z DB/result_json mogą mieć jawne null,
        # inaczej f-string wstawiłby literał „None" do komunikatu i nagłówka).
        _va, _vb = row.get("val_a"), row.get("val_b")
        va = "—" if _va is None else str(_va)
        vb = "—" if _vb is None else str(_vb)
        if not changed:
            continue
        if sev == "critical":
            level = "KRYTYCZNY"
        elif sev == "important":
            level = "UWAGA"
        else:
            level = "INFO"
        msg = f"{field}: {va} → {vb}"
        if note:
            msg = f"{note} ({va} → {vb})"
        gs1_messages.append(ReportMessage(level, msg))

    # Wyciągnij krytyczne diff tekstowe jako KRYTYCZNY
    for td in text_diffs:
        if td.get("severity") in ("error",) or td.get("priority") == "critical":
            txt = td.get("a") or td.get("b") or ""
            typ = "Usunieto" if td.get("a") else "Dodano"
            gs1_messages.append(ReportMessage("KRYTYCZNY", f"Roznica tekstu ({typ}): {txt}"))

    if not gs1_messages:
        if critical_count == 0 and important_count == 0:
            gs1_messages.append(ReportMessage("OK", "Nie wykryto krytycznych roznic w kodach GS1/UDI."))

    # ── Sekcja 4: Teksty — pełne porównanie sekcja-po-sekcji ──────────────
    # Priorytet: section_diffs z AI; fallback: text_diffs z regexów (z poprawką kluczy)
    text_rows: list[ReportRow] = []

    STATUS_LABEL = {
        "equal":        ("OK",          "status-ok"),
        "changed":      ("Roznica",     "status-niezgodnosc"),
        "only_master":  ("Brak w B",    "status-uwaga"),
        "only_factory": ("Brak w A",    "status-uwaga"),
    }

    # Buduj section_comparison z section_diffs (AI lub regex)
    built_section_comparison = []
    for sec in (section_diffs or []):
        rows_out = []
        has_diff = False
        for r in (sec.get("rows") or []):
            st = r.get("status", "equal")
            label, css = STATUS_LABEL.get(st, ("Info", "status-info"))
            _ca = r.get("content_a")
            _cb = r.get("content_b")
            ca = "—" if _ca is None else (str(_ca) or "—")
            cb = "—" if _cb is None else (str(_cb) or "—")
            if st != "equal":
                has_diff = True
            rows_out.append({
                "content_a":    ca,
                "content_b":    cb,
                "status":       st,
                "status_label": label,
                "status_class": css,
                "is_diff":      st != "equal",
            })
        if rows_out:
            built_section_comparison.append({
                "label":      sec.get("section_label", sec.get("section_key", "")),
                "severity":   sec.get("severity", "info"),
                "diff_count": sec.get("diff_count", 0),
                "has_diff":   has_diff,
                "rows":       rows_out,
                "ai":         sec.get("ai_decomposed", False),
            })

    # Fallback: jeśli brak section_diffs, użyj text_diffs (poprawka kluczy: content_a/b)
    # Deduplication: keep first occurrence of each (va, vb) pair; count duplicates
    if not built_section_comparison:
        seen: dict = {}
        for td in text_diffs:
            _va = td.get("content_a") if "content_a" in td else td.get("a")
            _vb = td.get("content_b") if "content_b" in td else td.get("b")
            va = "—" if _va is None else (str(_va) or "—")
            vb = "—" if _vb is None else (str(_vb) or "—")
            key = (va, vb)
            if va == "—" and vb == "—":
                continue
            if key not in seen:
                seen[key] = td
        for (va, vb), td in seen.items():
            st = "changed" if va != "—" and vb != "—" else ("only_master" if va != "—" else "only_factory")
            label, css = STATUS_LABEL.get(st, ("Roznica", "status-niezgodnosc"))
            # Użyj wyliczonego statusu — tekst obecny tylko po jednej stronie to
            # „Brak w A/B", nie pełna „Niezgodność" (wcześniej zawsze hardkodowane).
            text_rows.append(ReportRow(va[:70], va, vb, "—", label))

    # ── Sekcja 5: Grafika/symbole — z dim_rows ─────────────────────────────
    graphics: list[ReportRow] = []
    for dr in dim_rows:
        if "format" in dr.element.lower():
            graphics.append(dr)

    # ── Pozostałe defaults ─────────────────────────────────────────────────
    if not mfr_rows:
        mfr_rows.append(ReportRow("Producent / Manufacturer", "—", "—", "—", "Brak"))

    # ── Statystyki ─────────────────────────────────────────────────────────
    # ── Lista kontrolna MDR ────────────────────────────────────────────────────
    # Zbierz wartości pól z field_report do słownika pomocniczego
    # Use a list-of-rows map to avoid silently dropping duplicate field names
    _fmap_list = [(row.get("field", "").lower(), row) for row in field_report]
    _fmap = dict(_fmap_list)  # kept for single-match lookups; duplicates resolved via _fmap_list

    def _field_present(keys: list) -> bool:
        for k in keys:
            for fk, row in _fmap_list:
                if k in fk:
                    _va = row.get("val_a")
                    _vb = row.get("val_b")
                    va = "—" if _va is None else (str(_va) or "—")
                    vb = "—" if _vb is None else (str(_vb) or "—")
                    if va != "—" or vb != "—":
                        return True
        return False

    def _field_ok(keys: list) -> bool:
        for k in keys:
            for fk, row in _fmap_list:
                if k in fk:
                    return not row.get("changed", False)
        return False

    # Wyciągnij regulatory_checklist z AI jeśli dostępny
    ai_reg = {}
    ai_report = getattr(result, "ai_report", None) if hasattr(result, "ai_report") else (
        d.get("ai_report") if isinstance(d, dict) else None
    )
    if isinstance(ai_report, dict):
        ai_reg = ai_report.get("regulatory_checklist", {})

    # Dane z barcode_report
    barcode_rep = getattr(result, "barcode_report", None) if hasattr(result, "barcode_report") else (
        d.get("barcode_report") if isinstance(d, dict) else None
    )
    if not isinstance(barcode_rep, dict):
        barcode_rep = {}
    bc_msgs = barcode_rep.get("messages", [])
    bc_critical = any(m.get("level") == "KRYTYCZNY" for m in bc_msgs)
    bc_ok = not bc_critical and bool(bc_msgs)

    checklist = []
    for key, label, severity in CHECKLIST_ITEMS:
        # Ustal status z AI, pól lub heurystyki
        if key == "barcode_checksum":
            eans_a_bc = barcode_rep.get("eans_a", [])
            # Tylko komunikaty o sumie kontrolnej — zgodność A↔B i kod↔tekst
            # ocenia osobno pozycja „barcode_consistency”.
            sum_msgs = [m for m in bc_msgs if m.get("check") == "checksum"]
            bad = [m for m in sum_msgs if m.get("level") != "OK"]
            if not bc_msgs:
                status, note = "info", "Brak danych — biblioteka odczytu nie zainstalowana"
            elif not sum_msgs:
                status, note = "info", "Brak kodów EAN — nie przeprowadzono walidacji sumy"
            elif not bad:
                status, note = "ok", f"EAN-13: {', '.join(eans_a_bc[:2]) or '—'}"
            else:
                status, note = "error", "Błąd sumy kontrolnej: " + "; ".join(m.get("text", "") for m in bad)

        elif key == "barcode_consistency":
            if not bc_msgs:
                status, note = "info", "Brak danych — nie przeprowadzono odczytu z obrazu"
            elif bc_ok:
                status, note = "ok", "Kody kreskowe zgodne z tekstem"
            elif bc_critical:
                status, note = "error", "Niezgodność kodu kreskowego z tekstem"
            else:
                status, note = "warning", "Sprawdź ręcznie"

        elif key in ai_reg:
            ai_val = ai_reg[key]
            status = "ok" if ai_val else "error"
            note = "wg analizy AI"

        elif key == "ref_present":
            status = "ok" if _field_present(["ref"]) and _field_ok(["ref"]) else (
                "warning" if _field_present(["ref"]) else "error")
            note = ""
        elif key == "ean_present":
            status = "ok" if _field_present(["ean", "gtin"]) and _field_ok(["ean", "gtin"]) else (
                "warning" if _field_present(["ean", "gtin"]) else "error")
            note = ""
        elif key == "ce_mark":
            status = "ok" if _field_present(["ce", "mdr"]) else "warning"
            note = ""
        elif key == "single_use_symbol":
            has_it = _field_present(["steryl", "jednorazowy", "single"])
            status = "ok" if has_it else "warning"
            note = "sprawdź symbol graficzny"
        elif key == "sterility_mark":
            status = "ok" if _field_present(["steryl", "jałowy", "sterile"]) else "warning"
            note = ""
        elif key == "lot_placeholder":
            status = "ok" if _field_present(["lot", "seria"]) else "warning"
            note = "placeholder □□□□□ jest normalny w artworkach"
        elif key == "exp_placeholder":
            status = "ok" if _field_present(["exp", "ważności"]) else "warning"
            note = ""
        elif key == "warnings_present":
            status = "ok"
            note = "sprawdź ostrzeżenia CAUTION/WARNING wizualnie"
        elif key == "manufacturer_info":
            status = "ok" if _field_present(["producent", "manufacturer"]) else "warning"
            note = ""
        elif key == "revision_present":
            status = "ok" if _field_present(["rewizja", "wersja"]) else "warning"
            note = ""
        elif key == "product_name":
            status = "ok" if _field_ok(["nazwa produktu"]) else "warning"
            note = ""
        elif key == "translations_ok":
            status = "ok"
            note = "weryfikuj ręcznie wersje językowe"
        elif key == "colors_spec":
            status = "ok" if _field_present(["kolor", "pantone", "cmyk"]) else "info"
            note = ""
        elif key == "dimensions_ok":
            status = "ok" if _field_ok(["format", "rozmiar"]) else "warning"
            note = ""
        else:
            status, note = "info", ""

        checklist.append({
            "key": key, "label": label, "severity": severity,
            "status": status, "note": note,
        })

    # ── Barcode messages → ReportMessage ──────────────────────────────────────
    level_map = {"OK": "OK", "KRYTYCZNY": "KRYTYCZNY", "UWAGA": "UWAGA", "INFO": "INFO"}
    barcode_msgs_report = [
        ReportMessage(
            level=level_map.get(m.get("level", "INFO"), "INFO"),
            text=f"[{m.get('source','?')}] {m.get('code_type','')}: {m.get('text','')}"
        )
        for m in bc_msgs
    ]
    if not barcode_msgs_report:
        barcode_msgs_report.append(
            ReportMessage("INFO", "Brak wyników walidacji kodów — zainstaluj zxingcpp lub pyzbar.")
        )

    # ── Rekomendacja zatwierdzenia ─────────────────────────────────────────────
    approval_recommendation = ""
    approval_reason = ""
    ai_summary_text = ""
    approval_confidence = 0
    if isinstance(ai_report, dict) and not ai_report.get("error"):
        approval_recommendation = ai_report.get("approval_recommendation", "")
        approval_reason         = ai_report.get("approval_reason", "")
        ai_summary_text         = (ai_report.get("summary_pl")
                                   or ai_report.get("summary") or "")
        # AI-provided confidence or derive from diff counts
        try:
            approval_confidence = int(float(ai_report.get("confidence", 0) or 0))
        except (ValueError, TypeError):
            approval_confidence = 0
        if not approval_confidence:
            total = critical_count + important_count + ok_count
            approval_confidence = max(30, min(95, int((ok_count / max(total, 1)) * 100))) if total > 0 else 50
    elif critical_count == 0 and important_count == 0:
        approval_recommendation = "APPROVED"
        approval_reason         = "Brak wykrytych krytycznych i ważnych różnic."
        approval_confidence = 90
    elif critical_count > 0:
        approval_recommendation = "NEEDS_REVISION"
        approval_reason         = f"Wykryto {critical_count} krytycznych różnic wymagających korekty."
        approval_confidence = max(30, 80 - critical_count * 10)
    else:
        approval_recommendation = "APPROVED_WITH_NOTES"
        approval_reason         = f"Wykryto {important_count} uwag — przejrzyj przed zatwierdzeniem."
        approval_confidence = max(40, 75 - important_count * 5)

    all_rows = variable_data + color_rows + dim_rows + mfr_rows
    comp_sections, matched, diffs = _build_comparison_sections(all_rows)

    # Status wymiarów arkusza: gdy brak danych po OBU stronach → „Brak" (nie „Zgodny",
    # bo puste==puste nie jest zgodnością, tylko brakiem pomiaru).
    def _dims_status(da, db) -> str:
        if not da and not db:
            return "Brak"
        if da == db or _dims_close(da, db):
            return "Zgodny"
        return "Niezgodnosc"
    _dims_st = _dims_status(dims_a, dims_b)

    r = ArtworkReport(
        product_code    = fa,
        product_name    = fb,
        analysis_date   = _date.today().strftime("%d.%m.%Y"),
        master_version  = rev_a if rev_a != "—" else "—",
        factory_version = rev_b if rev_b != "—" else "—",
        critical_count  = critical_count,
        warning_count   = important_count,
        ok_count        = ok_count,
        analyzed_files  = analyzed_files,
        variable_data   = variable_data or [ReportRow("Brak danych", "—", "—", "—", "Info")],
        gs1_messages    = gs1_messages,
        color_spec      = color_rows or [ReportRow("Specyfikacja koloru", "—", "—", "—", "Info")],
        texts               = text_rows  or [],
        section_comparison  = built_section_comparison,
        icon_comparison     = icon_comparison,
        graphics        = graphics   or [ReportRow("Format arkusza (PDF)", dims_a or "—", dims_b or "—", "—",
                                                    _dims_st)],
        dimensions      = dim_rows   or [ReportRow("Rozmiar arkusza (PDF)", dims_a or "—", dims_b or "—", "—",
                                                    _dims_st)],
        manufacturer    = mfr_rows,
        checklist       = checklist,
        barcode_messages    = barcode_msgs_report,
        barcode_summary     = {
            "eans_a":  barcode_rep.get("eans_a", []),
            "eans_b":  barcode_rep.get("eans_b", []),
            "lot_a":   barcode_rep.get("lot_a"),
            "lot_b":   barcode_rep.get("lot_b"),
            "gs1_a":   barcode_rep.get("barcode_gs1_a"),
            "gs1_b":   barcode_rep.get("barcode_gs1_b"),
        },
        supplier_validation = _build_supplier_validation(field_report, barcode_rep),
        approval_recommendation = approval_recommendation,
        approval_reason         = approval_reason,
        approval_confidence     = approval_confidence,
        ai_summary              = ai_summary_text,
        summary_errors  = [m for m in gs1_messages if m.level == "KRYTYCZNY"],
        summary_warnings= [m for m in gs1_messages if m.level == "UWAGA"],
        comparison_sections = comp_sections,
        matched_items       = matched,
        diff_items          = diffs,
        page_diffs          = visual_page_diffs,
    )
    r.profile_active = _profile_active
    r.profile_name = _profile_name
    r.profile_fields = field_report if _profile_active else []
    # Performance breakdown — present only when profiling was requested
    _perf = d.get("performance") if isinstance(d, dict) else None
    r.performance = _perf if (isinstance(_perf, dict) and _perf.get("enabled")) else {}
    # Globalne porównanie kolorów (cała strona) — liczone przy kolorymetrii.
    _gc = (d.get("colorimetry") or {}).get("global") if isinstance(d, dict) else None
    r.global_color = _gc if isinstance(_gc, dict) else {}
    try:
        r.ai_comment = _generate_ai_comment(r)
    except Exception:
        r.ai_comment = ""
    return r


# ─── COMPARISON SECTIONS BUILDER ──────────────────────────────────────────────

def _build_comparison_sections(all_rows: list) -> tuple[list, list, list]:
    """
    Grupuje wiersze field_report w 3 sekcje tabelaryczne (format Word).
    Zwraca (comparison_sections, matched_items, diff_items).
    """
    SEC_ID   = ["ref", "ean", "gtin", "nazwa", "product", "steryl", "ce ", "jednorazow", "rewizja", "wersja", "kolor", "pantone", "cmyk", "specyfikacja"]
    SEC_MFR  = ["producent", "manufacturer", "eu rep", "dystrybutor", "importer", "g.w", "n.w"]
    SEC_CODE = ["lot", "exp", "data wa", "data prod", "gs1", "udi", "barcode", "kod kreskowy"]

    secs = [
        {"title": "DANE IDENTYFIKACYJNE PRODUKTU", "rows": []},
        {"title": "DANE PRODUCENTA I EU REP",       "rows": []},
        {"title": "KODY, DATY I BARKODY",            "rows": []},
    ]

    matched, diffs = [], []

    for row in all_rows:
        if isinstance(row, dict):
            fl = row.get("field", "").lower()
            va = row.get("val_a", "—")
            vb = row.get("val_b", "—")
            st = row.get("status", "")
            fn = row.get("field", fl)
            sev = row.get("severity", "info")
        else:
            fn = getattr(row, "element", "") or ""
            fl = fn.lower()
            _va = getattr(row, "master", None)
            _vb = getattr(row, "carton", None)
            va = "—" if _va is None else (str(_va) or "—")
            vb = "—" if _vb is None else (str(_vb) or "—")
            st = getattr(row, "status", "") or ""
            # Infer severity from status when not a dict
            st_l = st.lower()
            if st_l in ("niezgodnosc", "error", "krytyczny"):
                sev = "critical"
            elif st_l in ("rozny", "zmiana", "warning", "uwaga"):
                sev = "important"
            else:
                sev = "info"

        entry = {"field": fn, "val_a": va, "val_b": vb, "status": st, "severity": sev}

        if any(k in fl for k in SEC_MFR):
            secs[1]["rows"].append(entry)
        elif any(k in fl for k in SEC_CODE):
            secs[2]["rows"].append(entry)
        else:
            secs[0]["rows"].append(entry)

        changed = st.lower() in ("niezgodnosc", "rozny", "zmiana", "różnica")
        if changed:
            diffs.append(f"{fn}: {va} → {vb}")
        elif st.lower() not in ("brak", "info", "szablon") and va != "—":
            matched.append(fn)

    return secs, matched, diffs


# ─── AI COMMENT GENERATOR ─────────────────────────────────────────────────────

def _generate_ai_comment(report: "ArtworkReport") -> str:
    """
    Generuje narracyjny komentarz do raportu przy użyciu Claude API.
    Fallback: pusty string gdy brak klucza lub błąd.
    """
    import json
    import urllib.request as _ur

    api_key = _get_api_key().strip()
    if not api_key:
        return ""

    matched_str = "\n".join(f"• {m}" for m in report.matched_items) or "—"
    diffs_str   = "\n".join(f"• {d}" for d in report.diff_items)     or "—"
    mfr_rows    = report.manufacturer or []
    mfr_info    = "; ".join(f"{r.element}: {r.master} → {r.carton}" for r in mfr_rows if r.master != r.carton and r.carton != "—") or "brak różnic"

    prompt = (
        f"Jesteś ekspertem ds. regulacyjnych wyrobów medycznych (MDR).\n"
        f"Napisz ZWIĘZŁY komentarz końcowy do raportu porównania artworków (2-4 zdania po polsku).\n"
        f"Plik A (wzorzec): {report.product_code}\n"
        f"Plik B (dostawca): {report.product_name}\n"
        f"Błędy krytyczne: {report.critical_count}, Ostrzeżenia: {report.warning_count}\n"
        f"Elementy zgodne: {matched_str}\n"
        f"Różnice: {diffs_str}\n"
        f"Producent/adres: {mfr_info}\n\n"
        f"Komentarz powinien: (1) ocenić ogólny stan artworku, "
        f"(2) wymienić najważniejsze różnice wymagające weryfikacji, "
        f"(3) podać zalecenie (zatwierdź / wymaga korekty). "
        f"Odpowiedź: TYLKO tekst komentarza, bez nagłówków."
    )

    payload = json.dumps({
        "model": _DEFAULT_REPORT_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = _ur.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        # Bandit B310: URL stały w kodzie (https://api.anthropic.com), schemat nie pochodzi z wejścia.
        with _ur.urlopen(req, timeout=30) as resp:  # nosec B310
            data = json.loads(resp.read())
        content = data.get("content") or []
        return (content[0].get("text") or "").strip() if content else ""
    except Exception:
        return ""


# ─── WORD EXPORT ──────────────────────────────────────────────────────────────

def export_to_docx(report: "ArtworkReport") -> bytes:
    """Eksportuje raport do formatu Word (.docx) zgodnego z wzorcem Word."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_ALIGN_VERTICAL
    from io import BytesIO

    STATUS_COLORS = {
        "niezgodnosc": RGBColor(0xC0, 0x39, 0x2B),
        "rozny":       RGBColor(0xD3, 0x54, 0x00),
        "zmiana":      RGBColor(0xD3, 0x54, 0x00),
        "zgodny":      RGBColor(0x27, 0xAE, 0x60),
        "brak":        RGBColor(0x99, 0x99, 0x99),
    }
    STATUS_SYMBOLS = {
        "niezgodnosc": "⚠ RÓŻNICA",
        "rozny":       "⚠ RÓŻNICA",
        "zmiana":      "⚠ ZMIANA",
        "zgodny":      "✓ ZGODNE",
        "szablon":     "— SZABLON",
        "brak":        "— BRAK",
    }

    doc = Document()

    # Marginesy
    for sec in doc.sections:
        sec.top_margin    = Cm(2)
        sec.bottom_margin = Cm(2)
        sec.left_margin   = Cm(2.5)
        sec.right_margin  = Cm(2.5)

    # ── Nagłówek ──────────────────────────────────────────────────────────────
    title_parts = [report.product_code, report.product_name]
    title = " | ".join(p for p in title_parts if p and p != "—")
    h = doc.add_heading(f"RAPORT PORÓWNAWCZY ARTWORKU — {title}", level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if h.runs:
        h.runs[0].font.color.rgb = RGBColor(0x1A, 0x52, 0x76)

    p = doc.add_paragraph(f"Data porównania: {report.analysis_date or '—'}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if p.runs:
        p.runs[0].font.size = Pt(10)
        p.runs[0].font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    doc.add_paragraph()

    # ── Sekcja 2: Tabela porównawcza ──────────────────────────────────────────
    doc.add_heading("Tabela porównawcza elementów", level=2)

    fa = report.product_code or "Plik A (wzorzec)"
    fb = report.product_name  or "Plik B (dostawca)"

    for section in report.comparison_sections:
        if not section.get("rows"):
            continue

        # Nagłówek sekcji
        sp = doc.add_paragraph(section.get("title", ""))
        if sp.runs:
            sp.runs[0].bold = True
            sp.runs[0].font.size = Pt(9)
            sp.runs[0].font.color.rgb = RGBColor(0x1A, 0x52, 0x76)

        tbl = doc.add_table(rows=1, cols=4)
        tbl.style = "Table Grid"
        tbl.columns[0].width = Cm(5)
        tbl.columns[1].width = Cm(5.5)
        tbl.columns[2].width = Cm(5.5)
        tbl.columns[3].width = Cm(3)

        hdr = tbl.rows[0].cells
        for i, txt in enumerate(["Pole / Element", fa, fb, "Status"]):
            hdr[i].text = txt
            if hdr[i].paragraphs[0].runs:
                hdr[i].paragraphs[0].runs[0].bold = True
                hdr[i].paragraphs[0].runs[0].font.size = Pt(9)

        for row in section["rows"]:
            r = tbl.add_row().cells
            r[0].text = row.get("field", "")
            r[1].text = row.get("val_a", "—") or "—"
            r[2].text = row.get("val_b", "—") or "—"
            st = (row.get("status") or "").lower()
            sym = STATUS_SYMBOLS.get(st, row.get("status", ""))
            r[3].text = sym
            color = STATUS_COLORS.get(st)
            if color and r[3].paragraphs[0].runs:
                r[3].paragraphs[0].runs[0].font.color.rgb = color
            for cell in r:
                if cell.paragraphs[0].runs:
                    cell.paragraphs[0].runs[0].font.size = Pt(9)
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER

        doc.add_paragraph()

    # ── Sekcja 3: Podsumowanie ────────────────────────────────────────────────
    doc.add_heading("Podsumowanie i wnioski", level=2)

    if report.matched_items:
        p = doc.add_paragraph()
        r = p.add_run(f"✓ ELEMENTY ZGODNE ({len(report.matched_items)})")
        r.bold = True
        r.font.color.rgb = RGBColor(0x27, 0xAE, 0x60)
        for item in report.matched_items:
            doc.add_paragraph(f"• {item}", style="List Bullet")

    if report.diff_items:
        p = doc.add_paragraph()
        r = p.add_run(f"⚠ RÓŻNICE WYMAGAJĄCE WERYFIKACJI ({len(report.diff_items)})")
        r.bold = True
        r.font.color.rgb = RGBColor(0xD3, 0x54, 0x00)
        for item in report.diff_items:
            doc.add_paragraph(f"• {item}", style="List Bullet")

    # ── AI Komentarz ──────────────────────────────────────────────────────────
    if report.ai_comment:
        doc.add_paragraph()
        p = doc.add_paragraph()
        r = p.add_run("Komentarz:")
        r.bold = True
        doc.add_paragraph(report.ai_comment)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_to_pdf(report: "ArtworkReport") -> bytes:
    """Eksportuje raport porównania artworku do PDF (ReportLab) — nagłówek,
    podsumowanie, tabele porównawcze, wycinki master vs dostawca (gdy dostępne),
    weryfikacja kodów kreskowych i wnioski. Polskie znaki przez fonty DocFont
    (DejaVu/Liberation/FreeSans) zarejestrowane w export_engine."""
    import base64
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image as RLImage, KeepTogether)
    try:
        from export_engine import PDF_FONT, PDF_FONT_BOLD
    except Exception:
        PDF_FONT, PDF_FONT_BOLD = "Helvetica", "Helvetica-Bold"

    styles = getSampleStyleSheet()

    def mk(name, **kw):
        kw.setdefault("fontName", PDF_FONT)
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    st_normal = mk("n", fontSize=9, leading=12)
    st_small = mk("s", fontSize=7.5, leading=10, textColor=colors.HexColor("#444444"))
    st_h1 = mk("h1", fontName=PDF_FONT_BOLD, fontSize=16, leading=20,
               textColor=colors.HexColor("#1a5276"), spaceAfter=2)
    st_h2 = mk("h2", fontName=PDF_FONT_BOLD, fontSize=12, leading=16,
               textColor=colors.HexColor("#1a5276"), spaceBefore=10, spaceAfter=5)
    st_center = mk("c", fontSize=9, alignment=1, textColor=colors.HexColor("#555555"))
    st_sec = mk("st", fontName=PDF_FONT_BOLD, fontSize=9,
                textColor=colors.HexColor("#1a5276"), spaceBefore=6, spaceAfter=2)

    def esc(s):
        s = "" if s is None else str(s)
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def b64_image(b64, max_w, max_h):
        if not b64:
            return None
        try:
            raw = base64.b64decode(b64)
            iw, ih = ImageReader(io.BytesIO(raw)).getSize()
            if iw <= 0 or ih <= 0:
                return None
            scale = min(max_w / iw, max_h / ih)
            return RLImage(io.BytesIO(raw), width=iw * scale, height=ih * scale)
        except Exception:
            return None

    SYM = {"niezgodnosc": "ROZNICA", "rozny": "ROZNICA", "zmiana": "ZMIANA",
           "zgodny": "ZGODNE", "szablon": "SZABLON", "brak": "BRAK"}
    COL = {"niezgodnosc": "#c0392b", "rozny": "#d35400", "zmiana": "#d35400",
           "zgodny": "#27ae60", "brak": "#999999"}

    flow = []
    title_parts = [p for p in (report.product_code, report.product_name) if p and p != "—"]
    flow.append(Paragraph("RAPORT PORÓWNAWCZY ARTWORKU", st_h1))
    if title_parts:
        flow.append(Paragraph(esc(" | ".join(title_parts)), st_center))
    flow.append(Paragraph(f"Data porównania: {esc(report.analysis_date or '—')}", st_center))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        f'<font color="#c0392b"><b>Krytyczne:</b> {report.critical_count}</font>'
        f'&nbsp;&nbsp;&nbsp;<font color="#d35400"><b>Ostrzeżenia:</b> {report.warning_count}</font>'
        f'&nbsp;&nbsp;&nbsp;<font color="#27ae60"><b>Zgodne:</b> {report.ok_count}</font>',
        st_normal))
    if report.approval_recommendation:
        flow.append(Spacer(1, 4))
        flow.append(Paragraph(
            f"<b>Rekomendacja:</b> {esc(report.approval_recommendation)}"
            + (f" — {esc(report.approval_reason)}" if report.approval_reason else ""),
            st_normal))

    # ── Tabele porównawcze ──
    fa = report.product_code or "Wzorzec A"
    fb = report.product_name or "Dostawca B"
    if report.comparison_sections:
        flow.append(Paragraph("Tabela porównawcza elementów", st_h2))
        for section in report.comparison_sections:
            rows = section.get("rows") or []
            if not rows:
                continue
            flow.append(Paragraph(esc(section.get("title", "")), st_sec))
            data = [[Paragraph("<b>Pole</b>", st_small),
                     Paragraph(f"<b>{esc(fa)}</b>", st_small),
                     Paragraph(f"<b>{esc(fb)}</b>", st_small),
                     Paragraph("<b>Status</b>", st_small)]]
            for row in rows:
                stt = (row.get("status") or "").lower()
                data.append([
                    Paragraph(esc(row.get("field", "")), st_small),
                    Paragraph(esc(row.get("val_a", "—") or "—"), st_small),
                    Paragraph(esc(row.get("val_b", "—") or "—"), st_small),
                    Paragraph(f'<font color="{COL.get(stt, "#333333")}">'
                              f'{esc(SYM.get(stt, row.get("status", "")))}</font>', st_small),
                ])
            t = Table(data, colWidths=[38 * mm, 52 * mm, 52 * mm, 24 * mm], repeatRows=1)
            t.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d0d0")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f5f8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            flow.append(t)
            flow.append(Spacer(1, 4))

    # ── Wycinki pól: master vs dostawca (gdy w raporcie są base64) ──
    crop_fields = [f for f in (report.profile_fields or [])
                   if isinstance(f, dict) and (f.get("crop_a_b64") or f.get("crop_b_b64"))]
    if crop_fields:
        flow.append(Paragraph("Wycinki pól — wzorzec vs dostawca", st_h2))
        col_w = 83 * mm
        for f in crop_fields[:40]:
            name = f.get("display_name") or f.get("field") or f.get("field_name") or ""
            sev = (f.get("severity") or "").lower()
            badge = {"critical": "#c0392b", "error": "#c0392b",
                     "important": "#d35400", "warning": "#d35400"}.get(sev, "#666666")
            hdr = Paragraph(
                f'<b>{esc(name)}</b>&nbsp;&nbsp;<font color="{badge}" size="7">{esc(sev.upper())}</font>',
                st_normal)
            img_a = b64_image(f.get("crop_a_b64"), col_w - 4, 58 * mm) or Paragraph("—", st_small)
            img_b = b64_image(f.get("crop_b_b64"), col_w - 4, 58 * mm) or Paragraph("—", st_small)
            sub = Table([[Paragraph("MASTER (WZORZEC A)", st_small),
                          Paragraph("DOSTAWCA (PLIK B)", st_small)],
                         [img_a, img_b]], colWidths=[col_w, col_w])
            sub.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#1e3a8a")),
                ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#7f1d1d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BOX", (0, 1), (0, 1), 0.5, colors.HexColor("#1e3a8a")),
                ("BOX", (1, 1), (1, 1), 0.5, colors.HexColor("#7f1d1d")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 1), (-1, 1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
            ]))
            flow.append(KeepTogether([hdr, Spacer(1, 2), sub, Spacer(1, 8)]))

    # ── Kody kreskowe ──
    bs = report.barcode_summary or {}
    if bs.get("eans_a") or bs.get("eans_b") or report.barcode_messages:
        flow.append(Paragraph("Weryfikacja kodów kreskowych (EAN-13 / GS1)", st_h2))
        ea = ", ".join(bs.get("eans_a") or []) or "—"
        eb = ", ".join(bs.get("eans_b") or []) or "—"
        flow.append(Paragraph(f"<b>Wzorzec A — EAN:</b> {esc(ea)}"
                              + (f"&nbsp;&nbsp;LOT: {esc(bs.get('lot_a'))}" if bs.get("lot_a") else ""), st_normal))
        flow.append(Paragraph(f"<b>Dostawca B — EAN:</b> {esc(eb)}"
                              + (f"&nbsp;&nbsp;LOT: {esc(bs.get('lot_b'))}" if bs.get("lot_b") else ""), st_normal))
        for m in (report.barcode_messages or []):
            flow.append(Paragraph(f"- <b>{esc(getattr(m, 'label', ''))}</b> {esc(getattr(m, 'text', ''))}", st_small))

    # ── Wnioski ──
    if report.matched_items or report.diff_items:
        flow.append(Paragraph("Podsumowanie i wnioski", st_h2))
        if report.matched_items:
            flow.append(Paragraph(
                f'<font color="#27ae60"><b>ELEMENTY ZGODNE ({len(report.matched_items)})</b></font>', st_normal))
            for it in report.matched_items[:80]:
                flow.append(Paragraph("- " + esc(it), st_small))
        if report.diff_items:
            flow.append(Spacer(1, 4))
            flow.append(Paragraph(
                f'<font color="#d35400"><b>RÓŻNICE WYMAGAJĄCE WERYFIKACJI ({len(report.diff_items)})</b></font>', st_normal))
            for it in report.diff_items[:80]:
                flow.append(Paragraph("- " + esc(it), st_small))

    if report.ai_comment:
        flow.append(Spacer(1, 6))
        flow.append(Paragraph("<b>Komentarz:</b>", st_normal))
        flow.append(Paragraph(esc(report.ai_comment), st_normal))

    if not flow:
        flow.append(Paragraph("Brak danych raportu.", st_normal))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="Raport porównawczy artworku")
    doc.build(flow)
    return buf.getvalue()


# ─── HTML RENDERER ─────────────────────────────────────────────────────────────

def render_report_html(report: ArtworkReport, template_env=None) -> str:
    """Renderuje raport jako HTML przy użyciu Jinja2 lub (fallback) string format."""
    if template_env is not None:
        tpl = template_env.get_template("artwork_report.html")
        return tpl.render(report=report)

    # Fallback: prosty render bez Jinja2 (nie używany w produkcji)
    from jinja2 import Environment, FileSystemLoader
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    tpl = env.get_template("artwork_report.html")
    return tpl.render(report=report)
