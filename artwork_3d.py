"""
artwork_3d.py — generowanie modelu 3D opakowania (GLB) z artworku PDF (dieline).

Pipeline:
  1. extract_dieline_info()  — wymiary W×H×D [mm] z tabelki artworku (regex na tekście),
                               rozmiar strony, typ opakowania.
  2. locate_panels()         — heurystyka wymiarowa: linie cięcia/bigowania z
                               page.get_drawings() → klastry współrzędnych → komórki siatki
                               dopasowane do wymiarów paneli (front/back/left/right/top/bottom).
  3. render_panels()         — render wycinków strony (clip) do PIL.Image w zadanym DPI.
  4. build_glb()             — prostopadłościan o rzeczywistych proporcjach, 6 ścian
                               z teksturami UV, eksport do samodzielnego pliku GLB (trimesh).

Konwencje:
  - Wymiary w mm: W (szerokość, oś X), H (wysokość, oś Y), D (głębokość, oś Z).
  - Dieline ACME są w skali 1:1 — współrzędne PDF pt → mm (× 25.4/72).
  - glTF używa metrów — build_glb() dzieli mm przez 1000.
  - Ciężkie importy (pymupdf, PIL, trimesh, numpy) są lazy — moduł importuje się bez nich.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PT_TO_MM = 25.4 / 72.0
MM_TO_PT = 72.0 / 25.4

# Nazwy paneli i ich wymiary docelowe (szer_panelu, wys_panelu) jako funkcja (W, H, D)
PANEL_TARGETS = {
    "front":  lambda w, h, d: (w, h),
    "back":   lambda w, h, d: (w, h),
    "left":   lambda w, h, d: (d, h),
    "right":  lambda w, h, d: (d, h),
    "top":    lambda w, h, d: (w, d),
    "bottom": lambda w, h, d: (w, d),
}

# Rotacja tekstury panelu w stopniach (CCW) — FALLBACK, gdy na panelu nie ma tekstu,
# z którego dałoby się zmierzyć orientację (patrz _panel_text_rotation). W dielinach
# klapowych klapy góra/dół leżą zwykle "do góry nogami" względem frontu.
PANEL_ROTATION = {"front": 0, "back": 0, "left": 0, "right": 0, "top": 180, "bottom": 180}

# Fallback: gdy panelu brak w siatce, użyj tekstury panelu lustrzanego
PANEL_MIRROR = {"back": "front", "right": "left", "bottom": "top",
                "front": "back", "left": "right", "top": "bottom"}

MAX_TEXTURE_PX = 2048   # dłuższy bok tekstury ściany
DEFAULT_DPI = 220


@dataclass
class DielineInfo:
    w_mm: float = 0.0
    h_mm: float = 0.0
    d_mm: float = 0.0
    page_w_mm: float = 0.0
    page_h_mm: float = 0.0
    page_index: int = 0
    package_type: str = ""      # indywidualne / pośrednie / karton transportowy
    finish: str = ""            # np. "matt foil" — wpływa na roughness materiału
    ean: str = ""
    warnings: list = field(default_factory=list)

    @property
    def has_dims(self) -> bool:
        return self.w_mm > 0 and self.h_mm > 0 and self.d_mm > 0


# ───────────────────────────── 1. Parsowanie wymiarów ─────────────────────────────

_DIMS_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d+)?)\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?)\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?)"
    r"\s*(mm|cm)?",
    re.IGNORECASE,
)

# Jednostka zapisu → przelicznik na mm. Bez jednostki zakładamy mm (tak są opisane
# tabelki "Rozmiar [mm]"), ale wymiar kartonu bywa podany w cm ("MEAS: 35,5 x 29,5 x 31 cm")
# i wzięty dosłownie dawał bryłę 10× za małą — w dodatku po cichu, bo mieścił się w zakresie.
_UNIT_TO_MM = {"": 1.0, "mm": 1.0, "cm": 10.0}

_EAN_RE = re.compile(r"\b(\d{13})\b")

_PKG_TYPES = ("karton transportowy", "pośrednie", "posrednie", "indywidualne")


def parse_dimensions_mm(text: str):
    """Znajdź 'A x B x C [mm|cm]' w tekście artworku. Zwraca (a, b, c) w mm lub None.

    Bierze pierwszy trójwymiarowy zapis, w którym wszystkie wartości — PO przeliczeniu
    jednostki — mieszczą się w sensownym zakresie opakowania (5–2000 mm).

    Zwracana trójka jest w KOLEJNOŚCI ZAPISU, nie w (W, H, D): tabelki artworków nie
    trzymają jednej konwencji (np. "145 x 66 x 274" to opakowanie 274 szer. × 145 wys.
    × 66 gł.). Kolejność rozstrzyga dopiero locate_panels(), dopasowując ją do rysunku.
    """
    if not text:
        return None
    for m in _DIMS_RE.finditer(text):
        try:
            vals = [float(v.replace(",", ".")) for v in m.groups()[:3]]
        except ValueError:
            continue
        factor = _UNIT_TO_MM[(m.group(4) or "").lower()]
        vals = [v * factor for v in vals]
        if all(5.0 <= v <= 2000.0 for v in vals):
            return tuple(vals)
    return None


def detect_package_type(text: str) -> str:
    """FALLBACK: typ opakowania zgadnięty z samego tekstu, gdy nie ma dostępu do strony.

    Uwaga: tabelka artworku wymienia WSZYSTKIE trzy opcje jako etykiety checkboxów, więc
    po tekście nie da się poznać, która jest zaznaczona — ta funkcja zwróci pierwszą z
    listy. Zaznaczenie czyta dopiero package_type_from_page(); tekstowy wynik jest tylko
    ostatnią deską ratunku i jest oznaczany ostrzeżeniem w extract_dieline_info().
    """
    low = (text or "").lower()
    for t in _PKG_TYPES:
        if t in low:
            return "pośrednie" if t == "posrednie" else t
    return ""


# Początek etykiety checkboxa → pełna nazwa typu. Etykiety bywają wielosłowowe
# ("karton transportowy"), a get_text('words') tnie je na słowa, więc sklejamy linię
# i porównujemy jej początek.
_PKG_LABEL_HEADS = (
    ("karton transportowy", "karton transportowy"),
    ("pośrednie", "pośrednie"),
    ("posrednie", "pośrednie"),
    ("indywidualne", "indywidualne"),
)


def pick_checked_option(options, marks, row_tol: float = 4.0, max_gap: float = 40.0) -> str:
    """RDZEŃ (czysty): która opcja ma zaznaczony checkbox. '' gdy żadna albo niejednoznacznie.

    options : [(nazwa, x_lewa_krawędź_etykiety, y_środek_etykiety), ...]
    marks   : [(x0, y0, x1, y1, wypełniony: bool), ...] — kwadraciki checkboxów
    row_tol : ile jednostek w pionie może dzielić środek checkboxa od środka etykiety
    max_gap : jak daleko NA LEWO od etykiety może leżeć jej checkbox

    Bez zależności i bez PDF-a — testowalne na gołych liczbach (jak artwork_text_layer).
    """
    hits = []
    for name, lx, ly in options:
        for x0, y0, x1, y1, filled in marks:
            if not filled:
                continue
            if abs((y0 + y1) / 2.0 - ly) > row_tol:
                continue          # inny wiersz tabelki
            if not (0.0 <= lx - x1 <= max_gap):
                continue          # checkbox musi być tuż na lewo od etykiety
            hits.append(name)
            break
    # Zaznaczona ma być dokładnie jedna. Dwie = tabelka wypełniona błędnie — wtedy
    # lepiej nie zwracać nic niż zgadywać, którą autor miał na myśli.
    return hits[0] if len(hits) == 1 else ""


def package_type_from_page(page) -> str:
    """Typ opakowania odczytany z ZAZNACZENIA w tabelce. '' gdy nie da się ustalić.

    Adapter nad pick_checked_option: z PDF-a bierze linie tekstu (etykiety) i małe
    kwadraty z get_drawings (checkboxy). Zaznaczony checkbox jest wypełniony — w
    PyMuPDF ma niepuste 'fill' (type 'f'/'fs'), pusty ma tylko obrys (type 's').
    """
    lines = {}
    for x0, y0, x1, y1, word, block, line, _no in page.get_text("words"):
        lines.setdefault((block, line), []).append((x0, y0, x1, y1, word))

    options = []
    for parts in lines.values():
        parts.sort(key=lambda t: t[0])
        text = " ".join(p[4] for p in parts).strip().lower()
        for head, full in _PKG_LABEL_HEADS:
            if text.startswith(head):
                lx = min(p[0] for p in parts)
                ly = (min(p[1] for p in parts) + max(p[3] for p in parts)) / 2.0
                options.append((full, lx, ly))
                break

    marks = []
    for item in page.get_drawings():
        r = item["rect"]
        if not (1.0 <= r.width <= 16.0 and 1.0 <= r.height <= 16.0):
            continue
        if abs(r.width - r.height) > 0.35 * max(r.width, r.height):
            continue          # checkbox jest kwadratem, nie kreską czy strzałką
        marks.append((r.x0, r.y0, r.x1, r.y1, item.get("fill") is not None))

    return pick_checked_option(options, marks)


def extract_dieline_info(pdf_path: str, page_index=None) -> DielineInfo:
    """Odczytaj wymiary i metadane z artworku. Wybiera stronę o największej powierzchni,
    chyba że page_index podany jawnie."""
    import pymupdf

    info = DielineInfo()
    with pymupdf.open(pdf_path) as doc:
        if page_index is None:
            page_index = max(range(len(doc)),
                             key=lambda i: doc[i].rect.width * doc[i].rect.height)
        page = doc[page_index]
        info.page_index = page_index
        info.page_w_mm = page.rect.width * PT_TO_MM
        info.page_h_mm = page.rect.height * PT_TO_MM
        text = page.get_text() or ""
        checked = package_type_from_page(page)

    dims = parse_dimensions_mm(text)
    if dims:
        info.w_mm, info.h_mm, info.d_mm = dims
    else:
        info.warnings.append("Nie znaleziono wymiarów W×H×D w tekście artworku — podaj ręcznie.")
    info.package_type = checked or detect_package_type(text)
    if not checked and info.package_type:
        info.warnings.append(
            f"Typ opakowania zgadnięty z tekstu ({info.package_type}) — nie odczytano "
            "zaznaczonego checkboxa w tabelce.")
    if "foil" in text.lower():
        info.finish = "matt foil"
    ean = _EAN_RE.search(text)
    if ean:
        info.ean = ean.group(1)
    return info


# ───────────────────────────── 2. Lokalizacja paneli ─────────────────────────────

def cluster_coords(values, tol: float):
    """Klastruj posortowane współrzędne 1D: wartości bliższe niż tol → jeden klaster.
    Zwraca listę środków klastrów (posortowaną)."""
    if not values:
        return []
    vals = sorted(values)
    clusters = [[vals[0]]]
    for v in vals[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def _iter_segments_mm(page):
    """Wszystkie segmenty (linie + krawędzie prostokątów) ze strony jako
    [((x0,y0),(x1,y1))] w mm. Jedno źródło dekodowania get_drawings — konsumowane
    zarówno przez wykrywanie siatki (_axis_line_coords), jak i shapely.polygonize."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception as e:  # uszkodzony content stream nie powinien wywracać całości
        logger.warning("get_drawings failed: %s", e)
        return out
    for d in drawings:
        for item in d.get("items", []):
            op = item[0]
            if op == "l":  # linia
                pairs = [(item[1], item[2])]
            elif op == "re":  # prostokąt → 4 krawędzie
                r = item[1]
                pairs = [(r.top_left, r.top_right), (r.bottom_left, r.bottom_right),
                         (r.top_left, r.bottom_left), (r.top_right, r.bottom_right)]
            else:
                continue
            for p1, p2 in pairs:
                out.append(((p1.x * PT_TO_MM, p1.y * PT_TO_MM),
                            (p2.x * PT_TO_MM, p2.y * PT_TO_MM)))
    return out


def _axis_line_coords(segments, min_len_mm: float):
    """Z segmentów [mm] wybierz współrzędne długich linii pionowych (x) i poziomych (y).
    Krótkie segmenty (grafika) są odfiltrowane."""
    xs, ys = [], []
    for (x0, y0), (x1, y1) in segments:
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        if dx <= 1.0 and dy >= min_len_mm:      # pionowa
            xs.append((x0 + x1) / 2)
        elif dy <= 1.0 and dx >= min_len_mm:    # pozioma
            ys.append((y0 + y1) / 2)
    return xs, ys


def collect_line_coords(page, min_len_mm: float):
    """Współrzędne [mm] długich linii pionowych/poziomych — cienka nakładka."""
    return _axis_line_coords(_iter_segments_mm(page), min_len_mm)


def _find_cells(xs, ys, tw: float, th: float, tol: float):
    """Wszystkie komórki (x0,y0,x1,y1) o wymiarach ≈ (tw × th) rozpięte na klastrach linii."""
    cells = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            if abs((x1 - x0) - tw) > tol:
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    if abs((y1 - y0) - th) > tol:
                        continue
                    cells.append((x0, y0, x1, y1))
    return cells


def _overlap(a, b) -> float:
    """Pole części wspólnej dwóch rectów (mm²)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


# Pary paneli (primary, secondary) + rozmiar jako klucze wymiarów (szer × wys).
_PANEL_PAIRS = (("front", "back", ("w", "h")),
                ("left", "right", ("d", "h")),
                ("top", "bottom", ("w", "d")))


def _assign_pairs(candidates_for, w: float, h: float, d: float, cx: float, cy: float):
    """Wspólny przydział par paneli (używany przez metodę siatki i shapely).
    candidates_for(tw,th) → rects kandydujące na dany rozmiar. Dla każdej pary: odrzuć
    nachodzące na już przydzielone, sortuj wg odległości od środka (cx,cy), weź do 2
    rozłącznych; bliższy środka = primary (front/left/top). Zwraca dict panel→rect."""
    dims = {"w": w, "h": h, "d": d}

    def cdist(c):
        return abs((c[0] + c[2]) / 2 - cx) + abs((c[1] + c[3]) / 2 - cy)

    found, used = {}, []
    for primary, secondary, (ka, kb) in _PANEL_PAIRS:
        tw, th = dims[ka], dims[kb]
        cand = [c for c in candidates_for(tw, th)
                if all(_overlap(c, u) < 0.25 * tw * th for u in used)]
        cand.sort(key=cdist)
        picked = []
        for c in cand:
            if all(_overlap(c, p) < 0.25 * tw * th for p in picked):
                picked.append(c)
            if len(picked) == 2:
                break
        if picked:
            found[primary] = picked[0]
            used.append(picked[0])
        if len(picked) > 1:
            found[secondary] = picked[1]
            used.append(picked[1])
    return found


def match_panels(xs, ys, w: float, h: float, d: float, tol: float = None):
    """Dopasuj panele do komórek siatki (klastry linii xs/ys). Zwraca dict panel→rect."""
    if tol is None:
        tol = max(3.0, 0.02 * max(w, h, d))
    cx = (min(xs) + max(xs)) / 2 if xs else 0.0
    cy = (min(ys) + max(ys)) / 2 if ys else 0.0
    return _assign_pairs(lambda tw, th: _find_cells(xs, ys, tw, th, tol), w, h, d, cx, cy)


def _polygonize_faces(segments, min_len_mm: float, min_area_mm2: float):
    """Realne ścianki dielinu przez shapely.polygonize na długich segmentach [mm].
    Odporniejsze niż kombinatoryczne `_find_cells` — daje faktyczne wieloboki, nie
    iloczyn kartezjański linii. Zwraca listę (x0,y0,x1,y1) [mm] malejąco po polu.
    [] gdy shapely niedostępny albo brak wieloboków (bezpieczny fallback)."""
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union, polygonize
    except Exception:
        return []
    lines = [LineString([a, b]) for a, b in segments
             if ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 >= min_len_mm]
    if not lines:
        return []
    try:
        faces = [f for f in polygonize(unary_union(lines)) if f.area >= min_area_mm2]
    except Exception:
        return []
    faces.sort(key=lambda f: -f.area)
    return [tuple(f.bounds) for f in faces]


def _match_faces_to_panels(faces, w: float, h: float, d: float, tol: float):
    """Przypisz ścianki (bounds) do paneli po rozmiarze (w×h/d×h/w×d, obie orientacje)
    i pozycji (bliżej środka = front/left/top). Zwraca dict panel→(x0,y0,x1,y1)."""
    if not faces:
        return {}
    cx = (min(f[0] for f in faces) + max(f[2] for f in faces)) / 2
    cy = (min(f[1] for f in faces) + max(f[3] for f in faces)) / 2

    def size_ok(f, tw, th):
        fw, fh = f[2] - f[0], f[3] - f[1]
        return ((abs(fw - tw) <= tol and abs(fh - th) <= tol) or
                (abs(fw - th) <= tol and abs(fh - tw) <= tol))

    return _assign_pairs(lambda tw, th: [f for f in faces if size_ok(f, tw, th)],
                         w, h, d, cx, cy)


def _infer_dims_from_faces(faces):
    """Wywnioskuj (w,h,d) opakowania z realnych ścianek — gdy wymiary z tekstu nie pasują
    do wykrojnika (typowe dla kartonów RSC: parser bierze inny zestaw liczb niż rysunek).

    Ściany boczne dzielą wspólną WYSOKOŚĆ (najczęstsza długość krawędzi wśród dużych
    ścianek); dwie największe pozostałe długości = szerokość i głębokość. Zwraca (w,h,d)
    lub None gdy zbyt mało danych."""
    if len(faces) < 4:
        return None
    edges = []
    for f in faces[:6]:                       # 4–6 największych = ściany boczne
        edges.append(round(f[2] - f[0], 1))
        edges.append(round(f[3] - f[1], 1))
    clusters = []
    for v in sorted(edges):
        if clusters and v - clusters[-1][-1] <= 6:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    centers = [(sum(c) / len(c), len(c)) for c in clusters]
    if len(centers) < 2:
        return None
    h = max(centers, key=lambda c: c[1])[0]   # wysokość = najliczniejszy klaster
    others = sorted((c[0] for c in centers if abs(c[0] - h) > 6), reverse=True)
    if len(others) < 2:
        return None
    w, d = others[0], others[1]
    if not all(5.0 <= v <= 2000.0 for v in (w, h, d)):
        return None
    return (w, h, d)


def locate_panels(page, info: DielineInfo, allow_dim_inference: bool = True):
    """Zlokalizuj panele na stronie dieline. Zwraca (rects_mm, warnings)."""
    warnings = []
    w, h, d = info.w_mm, info.h_mm, info.d_mm
    min_len = 0.5 * min(w, h, d)
    segments = _iter_segments_mm(page)          # jeden odczyt get_drawings dla obu ścieżek
    xs_raw, ys_raw = _axis_line_coords(segments, min_len)
    tol = max(3.0, 0.02 * max(w, h, d))
    xs = cluster_coords(xs_raw, tol=min(tol, 2.5))
    ys = cluster_coords(ys_raw, tol=min(tol, 2.5))

    # sanity: siatka 1:1 musi się mieścić na stronie
    if info.page_w_mm and (w > info.page_w_mm and h > info.page_h_mm):
        warnings.append("Strona mniejsza niż wymiary opakowania — dieline nie jest w skali 1:1.")

    rects = match_panels(xs, ys, w, h, d, tol=tol) if (xs and ys) else {}

    # Kolejność wymiarów z tabelki nie jest konwencją — ten sam zapis "145 x 66 x 274"
    # bywa (H, D, W). Zamiast zgadywać, sprawdzamy wszystkie permutacje NA RYSUNKU i
    # bierzemy tę, która trafia więcej paneli. Tanie (match_panels to arytmetyka na już
    # sklastrowanych współrzędnych) i samo się weryfikuje. Nie ruszamy wymiarów podanych
    # ręcznie (allow_dim_inference=False) — tam kolejność jest deklaracją użytkownika.
    if (allow_dim_inference and xs and ys and len(rects) < len(PANEL_TARGETS)):
        # Bierzemy tylko permutacje trafiające ŚCIŚLE więcej paneli niż zapis z tabelki —
        # pliki działające dziś zostają nietknięte. Remis między nimi rozstrzyga większe
        # pole frontu: na dielinie artworkowym twarzą opakowania jest największa ściana,
        # a nie ta, która wypadła pierwsza w kolejności permutacji.
        best_dims, best = None, rects
        for cand in itertools.permutations((w, h, d)):
            if cand == (w, h, d):
                continue
            alt = match_panels(xs, ys, *cand, tol=tol)
            if len(alt) <= len(rects):
                continue
            if best_dims is None or ((len(alt), cand[0] * cand[1])
                                     > (len(best), best_dims[0] * best_dims[1])):
                best_dims, best = cand, alt
        if best_dims is not None:
            rects = best
            w, h, d = best_dims
            info.w_mm, info.h_mm, info.d_mm = best_dims
            warnings.append(
                f"Kolejność wymiarów poprawiona wg rysunku: {w:.0f}×{h:.0f}×{d:.0f} mm "
                f"(W×H×D) — zapis w tabelce miał inną kolejność.")

    # Fallback geometryczny (shapely): gdy metoda wymiarowa nie dała kompletu 6 paneli
    # (typowe dla kartonów RSC i gdy wymiary nie pasują 1:1 do siatki), spróbuj wykryć
    # realne ścianki przez polygonize. Bierzemy go tylko jeśli znajdzie WIĘCEJ paneli —
    # więc pliki, które działają dziś, nie zmieniają wyniku (brak regresji).
    if len(rects) < len(PANEL_TARGETS):
        min_area = max(300.0, 0.1 * min(w * h, w * d, d * h))
        faces = _polygonize_faces(segments, min_len_mm=min_len, min_area_mm2=min_area)
        alt = _match_faces_to_panels(faces, w, h, d, tol=max(tol, 0.06 * max(w, h, d)))
        if len(alt) > len(rects):
            rects = alt
            warnings.append("Panele wykryte metodą geometryczną (shapely.polygonize).")

        # RSC / złe wymiary z tekstu: gdy nadal niekompletnie, wywnioskuj wymiary z realnych
        # ścianek i dopasuj ponownie (chyba że user podał wymiary ręcznie). Adoptujemy gdy
        # da ≥4 panele i nie gorzej niż obecnie — bo daje POPRAWNE proporcje bryły.
        if allow_dim_inference and len(rects) < len(PANEL_TARGETS) and faces:
            inferred = _infer_dims_from_faces(faces)
            if inferred:
                iw, ih, id_ = inferred
                alt2 = _match_faces_to_panels(faces, iw, ih, id_,
                                              tol=max(3.0, 0.06 * max(iw, ih, id_)))
                if len(alt2) >= 4 and len(alt2) >= len(rects):
                    rects = alt2
                    info.w_mm, info.h_mm, info.d_mm = iw, ih, id_   # popraw też bryłę 3D
                    warnings.append(
                        f"Wymiary wywnioskowane z wykrojnika: {iw:.0f}×{ih:.0f}×{id_:.0f} mm "
                        "(tekst nie pasował do rysunku).")

    if not rects:
        warnings.append("Nie udało się dopasować paneli do siatki — użyto całej strony jako frontu.")
    else:
        missing = [p for p in PANEL_TARGETS if p not in rects]
        if missing:
            warnings.append("Panele uzupełnione lustrzanie/kolorem tła: " + ", ".join(missing))
    return rects, warnings


# ───────────────────────────── 3. Render paneli ─────────────────────────────

def _rotation_for_dir(dx: float, dy: float) -> int:
    """Kąt (CCW, stopnie) obrotu obrazu, po którym tekst o kierunku pisma (dx, dy)
    biegnie poziomo w prawo. Kierunek jak w PyMuPDF: układ strony z osią Y w dół,
    więc (1,0)=normalnie, (-1,0)=do góry nogami, (0,-1)=w górę, (0,1)=w dół."""
    if abs(dx) >= abs(dy):
        return 0 if dx >= 0 else 180
    return 270 if dy < 0 else 90


def _panel_text_rotation(page, clip):
    """Zmierz orientację panelu z kierunku pisma tekstu wewnątrz `clip`.
    Zwraca kąt (CCW) albo None, gdy panel nie ma tekstu (sama grafika / pusty).

    Zastępuje zgadywanie stałą PANEL_ROTATION: w wykrojniku panele bywają obrócone
    o 90/180° względem arkusza i tylko tekst mówi, gdzie naprawdę jest góra."""
    try:
        data = page.get_text("dict", clip=clip)
    except Exception:
        return None
    weights = {}
    for block in data.get("blocks", ()):
        for line in block.get("lines", ()):
            dx, dy = line.get("dir", (1.0, 0.0))
            chars = sum(len(s.get("text", "")) for s in line.get("spans", ()))
            if chars:
                rot = _rotation_for_dir(dx, dy)
                weights[rot] = weights.get(rot, 0) + chars
    if not weights:
        return None
    return max(weights.items(), key=lambda kv: kv[1])[0]


def pick_panel_dpi(clip_w_pt: float, clip_h_pt: float, dpi: int = DEFAULT_DPI) -> int:
    """DPI renderu wycinka: tekstura i tak kończy na MAX_TEXTURE_PX dłuższego boku
    (orient_panel), więc renderowanie ponad ~2× tego rozmiaru (zapas na rotację/
    LANCZOS) to strata czasu i RAM-u. Minimum 40 DPI dla czytelności drobnych paneli."""
    longest_pt = max(clip_w_pt, clip_h_pt) or 1
    return min(dpi, max(40, int(2 * MAX_TEXTURE_PX / (longest_pt / 72))))


def render_panels(pdf_path: str, page_index: int, rects_mm: dict, dpi: int = DEFAULT_DPI):
    """Renderuj wycinki strony (clip) do dict {panel: PIL.Image (RGB)}.

    Panele z czytelnym tekstem są od razu obracane do pionu (orientacja zmierzona, nie
    zgadnięta) i oznaczane `img.info["text_oriented"]`, żeby orient_panel nie dokładał
    już fallbackowej PANEL_ROTATION. Rotacja idzie tu, bo jest cechą ŹRÓDŁA (tak panel
    leży w arkuszu) i musi wędrować razem z teksturą przez swap/mirror."""
    import pymupdf
    from PIL import Image

    panels = {}
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_index]
        for name, (x0, y0, x1, y1) in rects_mm.items():
            clip = pymupdf.Rect(x0 * MM_TO_PT, y0 * MM_TO_PT, x1 * MM_TO_PT, y1 * MM_TO_PT)
            use_dpi = pick_panel_dpi(clip.width, clip.height, dpi)
            pix = page.get_pixmap(clip=clip, dpi=use_dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            measured = _panel_text_rotation(page, clip)
            if measured is not None:
                if measured:
                    img = img.rotate(measured, expand=True)
                img.info["text_oriented"] = True
            panels[name] = img
    return panels


def orient_panel(img, name: str):
    """Ogranicz rozmiar tekstury i — jeśli orientacji NIE zmierzono z tekstu — dołóż
    fallbackową rotację PANEL_ROTATION wg finalnej pozycji panelu."""
    from PIL import Image

    rot = 0 if img.info.get("text_oriented") else PANEL_ROTATION.get(name, 0) % 360
    if rot:
        img = img.rotate(rot, expand=True)
    longest = max(img.size)
    if longest > MAX_TEXTURE_PX:
        scale = MAX_TEXTURE_PX / longest
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
    return img


def _corner_color(img):
    """Kolor tła panelu — próbka z rogu (uśredniona 8×8 px)."""
    patch = img.crop((0, 0, min(8, img.width), min(8, img.height))).resize((1, 1))
    return patch.getpixel((0, 0))


def _blank_panel(size_mm, color=(240, 240, 240)):
    from PIL import Image
    w = max(2, min(MAX_TEXTURE_PX, int(size_mm[0] * 4)))
    h = max(2, min(MAX_TEXTURE_PX, int(size_mm[1] * 4)))
    return Image.new("RGB", (w, h), color)


def _blank_is_expected(name: str, package_type: str) -> bool:
    """Czy pusta (niezadrukowana) ścianka jest normalna dla tego typu opakowania?
    Karton transportowy: klapy góra/dół nie są w wykrojniku jako panele — puste pole
    to poprawny wynik. Opakowanie indywidualne/pośrednie ma wszystkie 6 ścian, więc
    każde puste pole oznacza NIEUDANĄ detekcję i musi trafić do ostrzeżeń."""
    return name in ("top", "bottom") and "karton" in (package_type or "").lower()


def complete_panels(panels: dict, info: DielineInfo) -> dict:
    """Uzupełnij brakujące panele: lustrzany odpowiednik → kolor tła frontu → jasnoszary.
    Puste pole nieoczekiwane dla danego typu opakowania dopisuje ostrzeżenie do info."""
    fallback_color = _corner_color(panels["front"]) if "front" in panels else (240, 240, 240)
    out = dict(panels)
    blanks = set()          # lustro pustego pola to nadal puste pole — nie chowaj go
    for name in PANEL_TARGETS:
        if name in out:
            continue
        mirror = PANEL_MIRROR.get(name)
        if mirror and mirror in out and mirror not in blanks:
            out[name] = out[mirror].copy()
        else:
            tw, th = PANEL_TARGETS[name](info.w_mm, info.h_mm, info.d_mm)
            out[name] = _blank_panel((tw, th), fallback_color)
            blanks.add(name)
            if not _blank_is_expected(name, info.package_type):
                info.warnings.append(
                    f"Nie wykryto ścianki '{name}' — wypełniona kolorem tła. "
                    f"Model nie odzwierciedla nadruku na tej ścianie.")
    return out


def colorfulness(img) -> float:
    """Miara barwności obrazu (metryka Haslera-Süsstrunka, uproszczona).
    Panel brandowany (logo, kolory Pantone) >> panel z blokiem tekstu prawnego."""
    import numpy as np
    a = np.asarray(img.convert("RGB").resize((64, 64)), dtype=np.float32)
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    return float((rg.std() ** 2 + yb.std() ** 2) ** 0.5
                 + 0.3 * ((rg.mean() ** 2 + yb.mean() ** 2) ** 0.5))


def pick_front_by_color(panels: dict) -> dict:
    """Jeśli 'back' jest wyraźnie barwniejszy niż 'front' (heurystyka środka siatki
    wskazała panel z tekstem), zamień je miejscami — front ma być panelem brandowanym."""
    out = dict(panels)
    if "front" in out and "back" in out:
        if colorfulness(out["back"]) > 1.2 * colorfulness(out["front"]):
            out["front"], out["back"] = out["back"], out["front"]
    return out


def enforce_plain_top_bottom(panels: dict, info: DielineInfo) -> dict:
    """Reguła kartonów (domenowa): PUSTA/niezadrukowana ścianka jest zawsze górą lub dołem.
    Jeśli najbardziej pusta PARA przeciwległych ścian nie jest top/bottom — obróć pudełko
    o 90°, by nią była (przemapuj panele + zamień odpowiednie wymiary). Działa tylko gdy
    para jest WYRAŹNIE pusta (colorfulness ≪ pozostałe) — inaczej no-op (np. tuck-box z
    nadrukiem na wszystkich ściankach)."""
    if not all(n in panels for n in PANEL_TARGETS):
        return panels
    c = {n: colorfulness(panels[n]) for n in PANEL_TARGETS}
    fb = (c["front"] + c["back"]) / 2
    lr = (c["left"] + c["right"]) / 2
    tb = (c["top"] + c["bottom"]) / 2
    plain_val, plain_pair = min((fb, "fb"), (lr, "lr"), (tb, "tb"))
    others = [v for v in (fb, lr, tb) if v != plain_val]
    # Reguła fireuje TYLKO gdy para jest realnie PUSTA (prawie-biała, colorfulness < 3)
    # i wyraźnie pustsza niż pozostałe — inaczej no-op (drukowane pudełka jak tuck-box,
    # gdzie żadna ścianka nie jest blank, nie mogą być błędnie obracane).
    if not others or plain_val >= 3.0 or plain_val >= 0.35 * min(others):
        return panels
    out = dict(panels)
    if plain_pair == "tb":
        return out                          # już dobrze (pusta para = góra/dół)
    if plain_pair == "fb":                   # front/back puste → na górę/dół; h↔d
        out["top"], out["bottom"], out["front"], out["back"] = \
            panels["front"], panels["back"], panels["top"], panels["bottom"]
        info.h_mm, info.d_mm = info.d_mm, info.h_mm
    else:                                    # left/right puste → na górę/dół; h↔w
        out["top"], out["bottom"], out["left"], out["right"] = \
            panels["left"], panels["right"], panels["top"], panels["bottom"]
        info.h_mm, info.w_mm = info.w_mm, info.h_mm
    return out


# ───────────────────────────── 4. Budowa GLB ─────────────────────────────

def _face_quads(w: float, h: float, d: float):
    """Definicje 6 ścian: origin + wektory u,v (normalna = u×v skierowana na zewnątrz).
    Wymiary w metrach. UV: (0,0) = lewy-dolny róg tekstury (konwencja trimesh/OBJ)."""
    return {
        "front":  ((-w / 2, -h / 2, +d / 2), (w, 0, 0), (0, h, 0)),
        "back":   ((+w / 2, -h / 2, -d / 2), (-w, 0, 0), (0, h, 0)),
        "left":   ((-w / 2, -h / 2, -d / 2), (0, 0, d), (0, h, 0)),
        "right":  ((+w / 2, -h / 2, +d / 2), (0, 0, -d), (0, h, 0)),
        "top":    ((-w / 2, +h / 2, +d / 2), (w, 0, 0), (0, 0, -d)),
        "bottom": ((-w / 2, -h / 2, -d / 2), (w, 0, 0), (0, 0, d)),
    }


def _to_power_of_two(img, max_side: int = 1024):
    """Przeskaluj teksturę do wymiarów będących potęgą dwójki (≤ max_side).

    trimesh ≥5.0 eksportuje tekstury GLB BEZ samplera → glTF domyślnie ustawia
    zawijanie REPEAT. Tekstury non-power-of-two z REPEAT są nierenderowalne w
    three.js/model-viewer (spada na biały baseColorFactor → biały box). POT działa
    z każdym trybem zawijania na każdym WebGL. UV mapuje pełny obraz na ścianę, więc
    zmiana proporcji pikseli jest niewidoczna. Przy okazji zmniejsza plik GLB."""
    from PIL import Image
    def _pot(n):
        return min(max_side, 1 << max(1, (max(1, n) - 1).bit_length()))
    w, h = _pot(img.width), _pot(img.height)
    return img.resize((w, h), Image.LANCZOS) if (w, h) != img.size else img


def build_glb(panels: dict, info: DielineInfo, out_path: str) -> str:
    """Zbuduj GLB: prostopadłościan W×H×D z teksturami paneli na 6 ścianach."""
    import numpy as np
    import trimesh
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    w = info.w_mm / 1000.0
    h = info.h_mm / 1000.0
    d = info.d_mm / 1000.0
    scene = trimesh.Scene()
    metallic = 0.0
    roughness = 0.55 if "foil" in (info.finish or "").lower() else 0.85

    for name, (origin, u, v) in _face_quads(w, h, d).items():
        o = np.array(origin, dtype=np.float64)
        uvec = np.array(u, dtype=np.float64)
        vvec = np.array(v, dtype=np.float64)
        vertices = np.array([o, o + uvec, o + uvec + vvec, o + vvec])
        faces = np.array([[0, 1, 2], [0, 2, 3]])
        uv = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
        material = PBRMaterial(baseColorTexture=_to_power_of_two(panels[name]),
                               metallicFactor=metallic, roughnessFactor=roughness,
                               name=f"panel_{name}")
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces,
                               visual=TextureVisuals(uv=uv, material=material),
                               process=False)
        scene.add_geometry(mesh, node_name=name, geom_name=name)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    scene.export(out_path)
    return out_path


def orientation_matrix(roll: float, pitch: float, yaw: float):
    """Macierz 4×4 odpowiadająca atrybutowi `orientation="roll pitch yaw"` z model-viewer.

    model-viewer składa to jako three.js Euler(pitch, yaw, roll, "YXZ"), czyli
    R = Ry(yaw) · Rx(pitch) · Rz(roll) w układzie Y-w-górę (ten sam co glTF i nasz GLB).
    Dzięki temu bryła zapisana do pliku wygląda DOKŁADNIE tak, jak user ją ustawił
    w podglądzie — bez tego akceptowałby jeden układ, a pobierał inny."""
    import numpy as np
    from trimesh.transformations import rotation_matrix

    r = np.radians
    return (rotation_matrix(r(yaw), [0, 1, 0])
            @ rotation_matrix(r(pitch), [1, 0, 0])
            @ rotation_matrix(r(roll), [0, 0, 1]))


def bake_orientation(glb_path: str, roll: float, pitch: float, yaw: float) -> bool:
    """Wpisz orientację zaakceptowaną przez użytkownika na trwałe w GLB (in-place).
    Zwraca False, gdy obrót jest zerowy (nie ma czego zapisywać)."""
    import trimesh

    if not (roll % 360 or pitch % 360 or yaw % 360):
        return False
    scene = trimesh.load(glb_path, force="scene")
    scene.apply_transform(orientation_matrix(roll, pitch, yaw))
    scene.export(glb_path)
    return True


def render_glb_preview(glb_path: str, png_path: str, size=(900, 700)) -> bool:
    """Wyrenderuj GLB do statycznego PNG (podgląd/eksport obrazu 3D). Wymaga pyvista+VTK
    z działającym offscreen (OpenGL). Best-effort: przy braku pyvista lub błędzie
    renderowania zwraca False i NIE wywraca generacji GLB."""
    try:
        import pyvista as pv
    except Exception:
        return False
    try:
        pl = pv.Plotter(off_screen=True, window_size=list(size))
        pl.import_gltf(glb_path)
        pl.set_background("white")
        pl.camera_position = "iso"
        pl.screenshot(png_path)
        pl.close()
        return os.path.exists(png_path)
    except Exception as e:
        logger.warning("render_glb_preview failed: %s", e)
        return False


# ───────────────────────────── Orkiestracja ─────────────────────────────

def generate_artwork_3d(pdf_path: str, out_path: str, dims_override=None,
                        dpi: int = DEFAULT_DPI) -> dict:
    """Pełny pipeline: PDF dieline → GLB. Zwraca metadane generacji.

    dims_override: (w_mm, h_mm, d_mm) — wymiary podane ręcznie (nadpisują odczytane).
    """
    import pymupdf

    info = extract_dieline_info(pdf_path)
    if dims_override:
        info.w_mm, info.h_mm, info.d_mm = (float(x) for x in dims_override)
        info.warnings = [wrn for wrn in info.warnings if "wymiar" not in wrn.lower()]
    if not info.has_dims:
        raise ValueError("Brak wymiarów opakowania — nie znaleziono ich w PDF i nie podano ręcznie.")

    with pymupdf.open(pdf_path) as doc:
        page = doc[info.page_index]
        rects, loc_warnings = locate_panels(page, info,
                                            allow_dim_inference=dims_override is None)
    info.warnings.extend(loc_warnings)

    if not rects:
        # fallback: cała strona jako front
        rects = {"front": (0.0, 0.0, info.page_w_mm, info.page_h_mm)}

    raw = render_panels(pdf_path, info.page_index, rects, dpi=dpi)
    # Panele z tekstem są już ustawione do pionu w render_panels (orientacja ZMIERZONA,
    # cecha źródła — wędruje z teksturą przez swap/mirror). Fallbackowa PANEL_ROTATION
    # zależy od nazwy-POZYCJI, więc orient_panel musi być OSTATNI, po swapie/uzupełnieniu.
    raw = pick_front_by_color(raw)
    raw = complete_panels(raw, info)
    # Reguła kartonów też PRZENOSI panele między pozycjami (front→góra itd.), więc jak
    # pick_front_by_color musi zadziałać przed orient_panel — inaczej panel przeniesiony
    # na górę niósłby fallbackową rotację swojej starej pozycji. Wymaga kompletu 6 ścian,
    # stąd po complete_panels.
    raw = enforce_plain_top_bottom(raw, info)
    panels = {name: orient_panel(img, name) for name, img in raw.items()}
    build_glb(panels, info, out_path)
    # Uwaga: render podglądu PNG (render_glb_preview) robi WARSTWA WYŻEJ (trasa), żeby
    # ten czysty generator GLB nie ciągnął pyvista/offscreen dla callerów headless/batch.

    return {
        "glb_path": out_path,
        "dims_mm": [info.w_mm, info.h_mm, info.d_mm],
        "package_type": info.package_type,
        "ean": info.ean,
        "panels_found": sorted(rects.keys()),
        "warnings": info.warnings,
    }
