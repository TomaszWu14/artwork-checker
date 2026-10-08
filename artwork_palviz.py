"""artwork_palviz — PalViz: panele + eksport 6 PNG/ścianka + podgląd GLB (z dieline PDF).

Wejście: artwork PDF z siatką pudełka (dieline). Wyjście: plik GLB (glTF binary)
z prostopadłościanem o proporcjach z wymiarów artworku, oteksturowany wycinkami
paneli ścian (front/tył/boki/góra/dół).

Ciężkie importy (trimesh, fitz) są function-local — moduł ładuje się bez nich,
żeby compileall / testy bez ML pozostały zielone.

Reuse z artwork_comparator: _load_pages, _extract_text_from_pdf, _get_dims_mm.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

# 6 ścian bryły. Rect = (x0, y0, x1, y1) w mm w układzie strony (y w dół).
FACES = ("front", "back", "left", "right", "top", "bottom")

# Rotacja (stopnie, CCW) nakładana na obraz panelu PRZED teksturowaniem.
# Klapy góra/dół w typowym dieline są obrócone względem frontu — knob do
# wizualnej korekty (Krok 4 planu). Zmiana tu = jedyne miejsce do strojenia.
ORIENT = {"front": 0, "back": 0, "left": 0, "right": 0, "top": 180, "bottom": 0}

# regex 3-osiowych wymiarów: "210 x 120 x 55 mm", "290×250×222", spacje/×/x, opc. mm
_DIM_RE = re.compile(r"(\d{1,4})\s*[x×]\s*(\d{1,4})\s*[x×]\s*(\d{1,4})\s*(?:mm)?", re.I)

MAX_TEX_PX = 2048          # dłuższy bok tekstury
MAX_PIXMAP_MPIX = 100      # limit renderu strony

# Proporcje ścianki wg wymiarów opakowania (l=w_mm długość, w=d_mm szerokość, h=h_mm wys.).
# (num_attr, den_attr) → szerokość:wysokość obrazka. front/back=l×h, left/right=w×h, top/bottom=l×w.
FACE_DIMS = {
    "front": ("w_mm", "h_mm"), "back":  ("w_mm", "h_mm"),
    "left":  ("d_mm", "h_mm"), "right": ("d_mm", "h_mm"),
    "top":   ("w_mm", "d_mm"), "bottom": ("w_mm", "d_mm"),
}
# Poziom = typ artworku (spójne z katalogiem/docs). box/carton/carton_print = netty 3D;
# sztuka/opz/karton zachowane dla kompatybilności starszego UI.
PALVIZ_LEVELS = ("box", "carton", "carton_print", "sztuka", "opz", "karton")

# Które ściany mają grafikę per poziom. Karton/carton: tylko boki (góra/dół to
# czysta tektura). Reszta (box/sztuka/opz/carton_print): pełne 6.
FACES_BY_LEVEL = {
    "carton": ("front", "back", "left", "right"),
    "karton": ("front", "back", "left", "right"),
}


def faces_for_level(level: str) -> tuple:
    """carton/karton → 4 ściany (bez góry/dołu); pozostałe poziomy → pełne 6."""
    return FACES_BY_LEVEL.get(level, FACES)


@dataclass
class DielineInfo:
    w_mm: float = 0.0
    h_mm: float = 0.0
    d_mm: float = 0.0
    page_w_mm: float = 0.0
    page_h_mm: float = 0.0
    page_idx: int = 0
    pkg_type: str = "indywidualne"   # indywidualne | pośrednie | karton transportowy
    source: str = "parsed"           # parsed | manual
    warnings: list = field(default_factory=list)

    @property
    def has_dims(self) -> bool:
        return self.w_mm > 0 and self.h_mm > 0 and self.d_mm > 0


def _classify_pkg(text: str, longest_mm: float) -> str:
    t = (text or "").lower()
    if "transport" in t or "karton zbior" in t or longest_mm >= 250:
        return "karton transportowy"
    if "pośredni" in t or "posredni" in t:
        return "pośrednie"
    return "indywidualne"


def extract_dieline_info(pdf_path: str, manual_dims: tuple | None = None) -> DielineInfo:
    """Odczytuje wymiary W×H×D (mm), rozmiar strony i typ opakowania.

    manual_dims=(w,h,d) z UI nadpisuje parsowanie. Gdy brak wymiarów w PDF i brak
    manual_dims → source='manual', has_dims=False, warning (endpoint poprosi o ręczne).
    Wybiera stronę o największym polu (PDF wielostronicowy).
    """
    from artwork_comparator import _extract_text_from_pdf, _get_dims_mm  # lazy

    info = DielineInfo()

    # największa strona (pole page.rect) — lazy fitz
    page_idx, pw, ph = 0, 0.0, 0.0
    try:
        import fitz
        doc = fitz.open(pdf_path)
        best = -1.0
        for i, page in enumerate(doc):
            w = page.rect.width / 72 * 25.4
            h = page.rect.height / 72 * 25.4
            if w * h > best:
                best, page_idx, pw, ph = w * h, i, round(w), round(h)
        doc.close()
    except Exception:
        pw, ph = _get_dims_mm(pdf_path)
    info.page_idx, info.page_w_mm, info.page_h_mm = page_idx, pw, ph

    texts = _extract_text_from_pdf(pdf_path)
    full = "\n".join(texts)

    if manual_dims and all(manual_dims):
        info.w_mm, info.h_mm, info.d_mm = (float(x) for x in manual_dims)
        info.source = "manual"
    else:
        m = _DIM_RE.search(full)
        if m:
            info.w_mm, info.h_mm, info.d_mm = (float(g) for g in m.groups())
            info.source = "parsed"
        else:
            info.source = "manual"
            info.warnings.append("Brak wymiarów w PDF — podaj W×H×D ręcznie.")

    info.pkg_type = _classify_pkg(full, max(info.w_mm, info.h_mm, info.d_mm))
    return info


def locate_panels(info: DielineInfo, content: tuple | None = None) -> dict:
    """Heurystyka wymiarowa → rect-y (mm) 6 paneli.

    Główny pas [D|W|D|W]×H (front, prawy bok, tył, lewy bok), klapy góra/dół (W×D)
    nad/pod frontem. content=(x0,y0,x1,y1) obszaru siatki w mm; domyślnie cała strona.
    Skala 1:1 weryfikowana szerokością pasa (±5%); inaczej skaluje + warning.
    Zwraca dict {face: (x0,y0,x1,y1)} tylko dla 6 paneli głównych.
    """
    if not info.has_dims:
        return {}
    W, H, D = info.w_mm, info.h_mm, info.d_mm
    if content is None:
        content = (0.0, 0.0, info.page_w_mm or (2 * D + 2 * W), info.page_h_mm or (H + 2 * D))
    cx0, cy0, cx1, cy1 = content
    Cw, Ch = cx1 - cx0, cy1 - cy0

    strip_w = 2 * D + 2 * W
    scale = 1.0
    if Cw > 0 and abs(strip_w - Cw) / Cw > 0.05:
        scale = Cw / strip_w
        info.warnings.append("Układ siatki wykryty heurystycznie (skala ≠ 1:1).")

    sW, sH, sD = W * scale, H * scale, D * scale
    total_w, total_h = 2 * sD + 2 * sW, sH + 2 * sD
    ox = cx0 + (Cw - total_w) / 2
    oy = cy0 + (Ch - total_h) / 2
    band_top = oy + sD

    # segmenty poziome: front | prawy | tył | lewy
    fx0 = ox + sD
    fx1 = fx0 + sW
    rx0, rx1 = fx1, fx1 + sD
    bx0, bx1 = rx1, rx1 + sW
    lx0, lx1 = ox, ox + sD

    band_bot = band_top + sH
    return {
        "front":  (fx0, band_top, fx1, band_bot),
        "right":  (rx0, band_top, rx1, band_bot),
        "back":   (bx0, band_top, bx1, band_bot),
        "left":   (lx0, band_top, lx1, band_bot),
        "top":    (fx0, oy, fx1, band_top),
        "bottom": (fx0, band_bot, fx1, band_bot + sD),
    }


def render_panels(pdf_path: str, rects: dict, info: DielineInfo, dpi: int = 220) -> dict:
    """Jeden render strony (info.page_idx) → crop per rect (mm→px). Zwraca {face: PIL}."""
    from artwork_comparator import _load_pages  # lazy
    from PIL import Image

    # dobierz dpi tak, by pixmapa < MAX_PIXMAP_MPIX
    pw, ph = info.page_w_mm or 300, info.page_h_mm or 300
    use_dpi = dpi
    while (pw / 25.4 * use_dpi) * (ph / 25.4 * use_dpi) > MAX_PIXMAP_MPIX * 1e6 and use_dpi > 40:
        use_dpi = int(use_dpi * 0.8)

    pages = _load_pages(pdf_path, use_dpi)
    page_img = pages[min(info.page_idx, len(pages) - 1)]
    px_per_mm = use_dpi / 25.4

    out = {}
    for face, (x0, y0, x1, y1) in rects.items():
        box = (max(0, int(x0 * px_per_mm)), max(0, int(y0 * px_per_mm)),
               min(page_img.width, int(x1 * px_per_mm)),
               min(page_img.height, int(y1 * px_per_mm)))
        if box[2] <= box[0] or box[3] <= box[1]:
            out[face] = Image.new("RGB", (16, 16), (210, 205, 195))  # brak grafiki → tło
        else:
            out[face] = page_img.crop(box)
    return out


def _rot_for(face: str, orient: dict | None) -> int:
    """Rotacja ścianki: override z szablonu (orient), inaczej globalna tabela ORIENT."""
    if orient and face in orient:
        return int(orient[face]) % 360
    return ORIENT.get(face, 0)


def _prep_texture(img, face: str, orient: dict | None = None):
    """Rotacja (override szablonu lub ORIENT), RGB, ograniczenie dłuższego boku."""
    from PIL import Image
    img = img.convert("RGB")
    rot = _rot_for(face, orient)
    if rot:
        img = img.rotate(rot, expand=True)
    if max(img.size) > MAX_TEX_PX:
        s = MAX_TEX_PX / max(img.size)
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    return img


# rogi ścian (bottom-left, bottom-right, top-right, top-left) — CCW patrząc z zewnątrz.
# hx=W/2, hy=H/2, hz=D/2 (metry). Front = +Z. UV: bl(0,1) br(1,1) tr(1,0) tl(0,0).
def _face_corners(hx, hy, hz):
    return {
        "front":  [(-hx, -hy,  hz), ( hx, -hy,  hz), ( hx,  hy,  hz), (-hx,  hy,  hz)],
        "back":   [( hx, -hy, -hz), (-hx, -hy, -hz), (-hx,  hy, -hz), ( hx,  hy, -hz)],
        "right":  [( hx, -hy,  hz), ( hx, -hy, -hz), ( hx,  hy, -hz), ( hx,  hy,  hz)],
        "left":   [(-hx, -hy, -hz), (-hx, -hy,  hz), (-hx,  hy,  hz), (-hx,  hy, -hz)],
        "top":    [(-hx,  hy,  hz), ( hx,  hy,  hz), ( hx,  hy, -hz), (-hx,  hy, -hz)],
        "bottom": [(-hx, -hy, -hz), ( hx, -hy, -hz), ( hx, -hy,  hz), (-hx, -hy,  hz)],
    }


def build_glb(panels: dict, info: DielineInfo, out_path: str, orient: dict | None = None) -> str:
    """Buduje GLB: 6 quadów, per-ściana tekstura+materiał PBR (matt). glTF w metrach."""
    import numpy as np
    import trimesh
    from trimesh.visual import TextureVisuals
    from trimesh.visual.material import PBRMaterial

    hx, hy, hz = info.w_mm / 2000.0, info.h_mm / 2000.0, info.d_mm / 2000.0  # mm→m, /2
    corners = _face_corners(hx, hy, hz)
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], dtype=float)
    tris = np.array([[0, 1, 2], [0, 2, 3]])

    scene = trimesh.Scene()
    for face in FACES:
        img = panels.get(face)
        if img is None:
            from PIL import Image
            img = Image.new("RGB", (16, 16), (210, 205, 195))
        from artwork_3d import _to_power_of_two  # POT: inaczej trimesh≥5 → NPOT+REPEAT → biały box
        tex = _to_power_of_two(_prep_texture(img, face, orient))
        verts = np.array(corners[face], dtype=float)
        mat = PBRMaterial(baseColorTexture=tex, metallicFactor=0.0, roughnessFactor=0.85)
        mesh = trimesh.Trimesh(vertices=verts, faces=tris, process=False)
        mesh.visual = TextureVisuals(uv=uv, material=mat, image=tex)
        scene.add_geometry(mesh, geom_name=face)

    scene.export(out_path, file_type="glb")
    return out_path


def _fit_face(img, aspect_w: float, aspect_h: float, min_px: int = 1024):
    """Przeskaluj obraz do DOKŁADNEJ proporcji ścianki, dłuższy bok ≥ min_px."""
    from PIL import Image
    ratio = aspect_w / aspect_h
    if ratio >= 1:
        w = max(min_px, img.width)
        h = max(1, round(w / ratio))
    else:
        h = max(min_px, img.height)
        w = max(1, round(h * ratio))
    return img.resize((w, h), Image.LANCZOS)


def _export_faces_from_panels(panels: dict, info: DielineInfo, out_dir: str,
                              level: str, sku: str, min_px: int = 1024,
                              blank_std: float = 6.0, orient: dict | None = None) -> dict:
    """PalViz: zapis gotowych paneli jako 6 PNG + manifest.json (bez ekstrakcji)."""
    from PIL import ImageStat

    if level not in PALVIZ_LEVELS:
        raise ValueError(f"level musi być jednym z {PALVIZ_LEVELS}")
    os.makedirs(out_dir, exist_ok=True)

    faces = {}
    for face in FACES:
        img = panels.get(face)
        if img is None:
            continue
        rot = _rot_for(face, orient)
        if rot:
            img = img.rotate(rot, expand=True)
        img = img.convert("RGB")
        if ImageStat.Stat(img.convert("L")).stddev[0] < blank_std:
            continue  # czysta ścianka (np. spód kartonu) — pomiń
        aw = getattr(info, FACE_DIMS[face][0])
        ah = getattr(info, FACE_DIMS[face][1])
        img = _fit_face(img, aw, ah, min_px)
        fn = f"{face}.png"
        img.save(os.path.join(out_dir, fn))
        faces[face] = fn

    manifest = {
        "level": level,
        "sku": sku,
        "dims_cm": {"l": round(info.w_mm / 10, 1),
                    "w": round(info.d_mm / 10, 1),
                    "h": round(info.h_mm / 10, 1)},
        "faces": faces,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    return {"needs_manual_dims": False, "manifest": manifest,
            "out_dir": out_dir, "faces_written": len(faces),
            "warnings": info.warnings}


def export_faces(pdf_path: str, out_dir: str, level: str, sku: str,
                 manual_dims: tuple | None = None, min_px: int = 1024,
                 blank_std: float = 6.0) -> dict:
    """Eksport dla PalViz (standalone): ekstrakcja → 6 PNG + manifest.json.

    Ścianki „na wprost", przycięte do krawędzi, proporcje = wymiary opakowania.
    Ścianki bez grafiki (std < blank_std) są pomijane. NIE generuje plików 3D.
    """
    if level not in PALVIZ_LEVELS:
        raise ValueError(f"level musi być jednym z {PALVIZ_LEVELS}")
    info = extract_dieline_info(pdf_path, manual_dims=manual_dims)
    if not info.has_dims:
        return {"needs_manual_dims": True, "warnings": info.warnings}
    allowed = faces_for_level(level)
    rects = {f: r for f, r in locate_panels(info).items() if f in allowed}
    panels = render_panels(pdf_path, rects, info)
    return _export_faces_from_panels(panels, info, out_dir, level, sku, min_px, blank_std)


def generate_glb(pdf_path: str, out_path: str, manual_dims: tuple | None = None) -> dict:
    """Orkiestracja end-to-end dla endpointu. Zwraca dict z warnings/dims/panels.

    Gdy brak wymiarów → {'needs_manual_dims': True, 'warnings': [...]} bez generacji.
    """
    info = extract_dieline_info(pdf_path, manual_dims=manual_dims)
    if not info.has_dims:
        return {"needs_manual_dims": True, "warnings": info.warnings,
                "dims_mm": None, "panels_found": 0}
    rects = locate_panels(info)
    panels = render_panels(pdf_path, rects, info)
    build_glb(panels, info, out_path)
    return {
        "needs_manual_dims": False,
        "glb_path": out_path,
        "dims_mm": {"w": info.w_mm, "h": info.h_mm, "d": info.d_mm},
        "pkg_type": info.pkg_type,
        "source": info.source,
        "panels_found": len(rects),
        "warnings": info.warnings,
    }


def build_bundle(pdf_path: str, glb_out: str, faces_out_dir: str, level: str,
                 sku: str, manual_dims: tuple | None = None, min_px: int = 1024,
                 blank_std: float = 6.0, rects: dict | None = None,
                 orient: dict | None = None) -> dict:
    """Jeden przebieg: podgląd (GLB) + eksport PalViz (PNG+manifest) z jednego renderu.

    Panele liczone raz i podane do obu builderów. `rects` (regiony w mm, np. z szablonu
    dieline_templates) nadpisują heurystykę `locate_panels`. `orient` (per-ściana stopnie
    z szablonu) nadpisuje globalną tabelę ORIENT. Gdy brak wymiarów →
    {'needs_manual_dims': True, 'warnings': [...]} bez generacji.
    """
    if level not in PALVIZ_LEVELS:
        raise ValueError(f"level musi być jednym z {PALVIZ_LEVELS}")
    info = extract_dieline_info(pdf_path, manual_dims=manual_dims)
    if not info.has_dims:
        return {"needs_manual_dims": True, "warnings": info.warnings}

    allowed = faces_for_level(level)                       # carton=4, box=6
    src_rects = rects if rects is not None else locate_panels(info)
    if rects is not None:
        info.warnings.append("Panele wg zapisanego szablonu wykrojnika.")
    rects = {f: r for f, r in src_rects.items() if f in allowed}
    panels = render_panels(pdf_path, rects, info)          # jeden render
    build_glb(panels, info, glb_out, orient=orient)        # brakujące ściany → domyślne
    exp = _export_faces_from_panels(panels, info, faces_out_dir, level, sku,
                                    min_px, blank_std, orient=orient)
    return {
        "needs_manual_dims": False,
        "glb_path": glb_out,
        "manifest": exp["manifest"],
        "faces_dir": faces_out_dir,
        "faces_written": exp["faces_written"],
        "dims_mm": {"w": info.w_mm, "h": info.h_mm, "d": info.d_mm},
        "pkg_type": info.pkg_type,
        "panels_found": len(rects),
        "warnings": info.warnings,
    }


def save_contact_sheet(panels: dict, out_path: str, cell: int = 300) -> str:
    """Arkusz kontaktowy: 6 ścianek (upright wg ORIENT) w siatce 3×2 z podpisami.

    Do wizualnej weryfikacji orientacji na realnym pliku (logo nie do góry nogami).
    """
    from PIL import Image, ImageDraw

    pad, label_h, cols, rows = 10, 22, 3, 2
    W = cols * cell + (cols + 1) * pad
    H = rows * (cell + label_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (245, 243, 238))
    draw = ImageDraw.Draw(sheet)
    for i, face in enumerate(FACES):
        cx = pad + (i % cols) * (cell + pad)
        cy = pad + (i // cols) * (cell + label_h + pad)
        draw.text((cx, cy), face, fill=(60, 55, 45))
        img = panels.get(face)
        box = (cx, cy + label_h, cx + cell, cy + label_h + cell)
        if img is None:
            draw.rectangle(box, outline=(200, 195, 185))
            draw.text((cx + 8, cy + label_h + cell // 2), "— brak —", fill=(160, 155, 145))
            continue
        rot = ORIENT.get(face, 0)
        thumb = (img.rotate(rot, expand=True) if rot else img).convert("RGB")
        thumb.thumbnail((cell, cell), Image.LANCZOS)
        sheet.paste(thumb, (cx + (cell - thumb.width) // 2,
                            cy + label_h + (cell - thumb.height) // 2))
    sheet.save(out_path)
    return out_path


def _cli(argv=None):
    """CLI weryfikacyjny: python -m artwork_3d <plik.pdf> [outdir].

    Renderuje wszystkie 6 ścian (do inspekcji, niezależnie od poziomu) →
    front.png … bottom.png + _contact.png. Podaje odczytane wymiary.
    """
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("użycie: python -m artwork_3d <plik.pdf> [outdir]")
        return 2
    pdf = argv[0]
    out_dir = argv[1] if len(argv) > 1 else "artwork3d_check"
    os.makedirs(out_dir, exist_ok=True)

    info = extract_dieline_info(pdf)
    if not info.has_dims:
        print("Brak wymiarów w PDF — podaj ręcznie (moduł oczekuje W×H×D).")
        print("Ostrzeżenia:", info.warnings)
        return 1
    print(f"Wymiary (mm): {info.w_mm}×{info.h_mm}×{info.d_mm}  typ={info.pkg_type}")
    rects = locate_panels(info)                    # wszystkie 6 do inspekcji
    panels = render_panels(pdf, rects, info)
    for face in FACES:
        img = panels.get(face)
        if img is None:
            continue
        rot = ORIENT.get(face, 0)
        (img.rotate(rot, expand=True) if rot else img).convert("RGB").save(
            os.path.join(out_dir, f"{face}.png"))
    sheet = save_contact_sheet(panels, os.path.join(out_dir, "_contact.png"))
    if info.warnings:
        print("Ostrzeżenia:", info.warnings)
    print(f"Zapisano 6 ścian + arkusz: {sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
