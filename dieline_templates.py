"""dieline_templates — szablony wykrojników (Faza 2b).

Heurystyka wymiarowa (`artwork_3d.locate_panels`) zawodzi na realnych dielinach ACME
(tabelka metadanych na arkuszu, niesymetryczne nety). Rozwiązanie: **szablon** =
regiony 6 ścian zdefiniowane RAZ na dany układ arkusza, dopasowywane po **sygnaturze**
(rozmiar strony + wzór pionowych linii cięcia). Pliki tej samej rodziny (np. rękawice
S–XL drukowane na identycznym arkuszu) trafiają w ten sam szablon.

Regiony są znormalizowane 0..1 wg rozmiaru strony → są stałe na arkuszu (niezależne od
wymiarów bryły; wymiary służą tylko proporcjom 3D w build_glb).

Ciężkie importy (fitz) lazy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from artwork_palviz import FACES

# kwantyzacja pozycji cięć do sygnatury (0.02 = 2% szerokości strony)
_SIG_BUCKET = 0.02
# linia „długa" = obejmuje >= tej części wymiaru strony (odsiewa krótkie kreski/tabelkę)
_LONG_FRAC = 0.22


@dataclass
class DielineTemplate:
    name: str
    sig_key: str
    page_w_mm: float
    page_h_mm: float
    panels: dict           # {face: (nx0, ny0, nx1, ny1)} znormalizowane 0..1
    orient: dict = field(default_factory=dict)  # {face: stopnie} nadpisania rotacji

    def to_row(self) -> tuple:
        return (self.name, self.sig_key, self.page_w_mm, self.page_h_mm,
                json.dumps(self.panels), json.dumps(self.orient))

    @staticmethod
    def from_row(row) -> "DielineTemplate":
        g = (lambda k, i: row[k] if isinstance(row, dict) else row[i])
        return DielineTemplate(
            name=g("name", 1), sig_key=g("sig_key", 2),
            page_w_mm=g("page_w_mm", 3), page_h_mm=g("page_h_mm", 4),
            panels=json.loads(g("panels_json", 5) or "{}"),
            orient=json.loads(g("orient_json", 6) or "{}"))


def _crease_lines(page):
    """Zwraca (vcuts_norm, hcuts_norm): znormalizowane pozycje długich linii cięcia."""
    PT = 25.4 / 72
    W = page.rect.width * PT
    H = page.rect.height * PT
    vs, hs = set(), set()
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                (x0, y0), (x1, y1) = it[1], it[2]
                if abs(x1 - x0) < 1 and abs(y1 - y0) * PT > _LONG_FRAC * H:
                    vs.add(round((x0 * PT / W) / _SIG_BUCKET) * _SIG_BUCKET)
                elif abs(y1 - y0) < 1 and abs(x1 - x0) * PT > _LONG_FRAC * W:
                    hs.add(round((y0 * PT / H) / _SIG_BUCKET) * _SIG_BUCKET)
            elif it[0] == "re":
                r = it[1]
                if (r.x1 - r.x0) * PT > _LONG_FRAC * W:
                    for y in (r.y0, r.y1):
                        hs.add(round((y * PT / H) / _SIG_BUCKET) * _SIG_BUCKET)
                if (r.y1 - r.y0) * PT > _LONG_FRAC * H:
                    for x in (r.x0, r.x1):
                        vs.add(round((x * PT / W) / _SIG_BUCKET) * _SIG_BUCKET)
    return sorted(vs), sorted(hs)


def signature(pdf_path: str) -> dict:
    """Sygnatura układu arkusza: rozmiar strony (mm, zaokr. do 10) + wzór pionowych cięć.
    Pliki tej samej rodziny (ten sam arkusz) mają ten sam `key`."""
    import fitz
    doc = fitz.open(pdf_path)
    page = max(doc, key=lambda p: p.rect.width * p.rect.height)
    PT = 25.4 / 72
    pw = round(page.rect.width * PT / 10) * 10
    ph = round(page.rect.height * PT / 10) * 10
    vs, hs = _crease_lines(page)
    doc.close()
    # klucz = rozmiar strony + pionowe ORAZ poziome cięcia (mocniej rozróżnia układy
    # o tym samym rozmiarze arkusza; box-nety mają mało vcuts → hcuts pomagają)
    key = (f"{pw}x{ph}|v:" + ",".join(f"{v:.2f}" for v in vs)
           + "|h:" + ",".join(f"{h:.2f}" for h in hs))
    return {"key": key, "page_w_mm": pw, "page_h_mm": ph, "vcuts": vs, "hcuts": hs}


def locate_panels_by_template(tpl: DielineTemplate, page_w_mm: float,
                              page_h_mm: float) -> dict:
    """Regiony szablonu (0..1) → rect-y w mm dla danej strony. Cięcie niezależne
    od wymiarów bryły (regiony są stałe na arkuszu)."""
    out = {}
    for face, (nx0, ny0, nx1, ny1) in tpl.panels.items():
        out[face] = (nx0 * page_w_mm, ny0 * page_h_mm,
                     nx1 * page_w_mm, ny1 * page_h_mm)
    return out


def make_template_from_regions(name: str, sig: dict, regions_mm: dict) -> DielineTemplate:
    """Buduje szablon z regionów podanych w mm (np. z kreatora UI) — normalizuje 0..1."""
    pw, ph = sig["page_w_mm"], sig["page_h_mm"]
    panels = {f: (x0 / pw, y0 / ph, x1 / pw, y1 / ph)
              for f, (x0, y0, x1, y1) in regions_mm.items() if f in FACES}
    return DielineTemplate(name, sig["key"], pw, ph, panels)


# ─── przechowywanie w bazie ───────────────────────────────────────────────────

def save_template(db, tpl: DielineTemplate, created_by=None) -> None:
    db.execute(
        "INSERT INTO dieline_templates(name,sig_key,page_w_mm,page_h_mm,panels_json,"
        "orient_json,created_by) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(sig_key) DO UPDATE SET name=excluded.name,panels_json=excluded.panels_json,"
        "orient_json=excluded.orient_json", tpl.to_row() + (created_by,))
    db.commit()


def match_template(db, sig_key: str):
    """Zwraca DielineTemplate dla sygnatury (dokładne dopasowanie) albo None."""
    row = db.execute(
        "SELECT id,name,sig_key,page_w_mm,page_h_mm,panels_json,orient_json "
        "FROM dieline_templates WHERE sig_key=?", (sig_key,)).fetchone()
    return DielineTemplate.from_row(row) if row else None
