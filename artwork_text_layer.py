"""artwork_text_layer.py — wykrywanie napisów wklejonych jako GRAFIKA, nie tekst.

Kluczowy błąd QA artworków: napis widoczny na stronie, ale w warstwie tekstowej PDF
go NIE MA (jest zrasteryzowany / zamieniony na krzywe). Taki napis nie da się
poprawić, przetłumaczyć ani sprawdzić literówek — i zwykle jest błędem do poprawy.

Wykrycie = zestawienie dwóch źródeł tej samej strony:
  - boxy OCR (co widać na obrazie),        [(text, (x1,y1,x2,y2)), ...] w 0..1
  - boxy warstwy tekstowej PDF (co JEST tekstem), [(x1,y1,x2,y2), ...] w 0..1
Napis, który OCR widzi, a pod którym nie ma boxu tekstowego → "tekst jako grafika".

Rdzeń (`find_text_as_graphic`) jest CZYSTY i testowalny bez żadnych ciężkich zależności.
Ekstrakcja z PDF (`extract_pdf_text_boxes`) lazy-importuje PyMuPDF — moduł ładuje się bez niego.
"""

# ponytail: overlap = przecięcie/pole boxu OCR; bez IoU, bo box tekstowy bywa
# większy niż glify OCR i IoU sztucznie zaniżałby pokrycie prawdziwego tekstu.


def _overlap_ratio(a, b) -> float:
    """Ułamek pola boxu `a` (OCR) pokryty boxem `b` (tekst PDF). 0..1."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    if area_a <= 0:
        return 0.0
    return (iw * ih) / area_a


def find_text_as_graphic(ocr_boxes, text_boxes, min_cover=0.30, min_area=0.0002):
    """Napisy widoczne na obrazie, których NIE ma w warstwie tekstowej PDF.

    ocr_boxes  : iterowalne [(text, (x1,y1,x2,y2)), ...], współrzędne 0..1.
    text_boxes : iterowalne [(x1,y1,x2,y2), ...] boxów realnego tekstu PDF, 0..1.
    min_cover  : jaki ułamek boxu OCR musi pokryć tekst PDF, by uznać go za "prawdziwy".
    min_area   : ignoruj boxy mniejsze niż to (szum OCR), jako ułamek strony.

    Zwraca listę {text, bbox, cover} — kandydatów na napis-grafikę, cover rosnąco
    (najpewniejsze, cover≈0, na początku).
    """
    tboxes = list(text_boxes)
    flagged = []
    for text, bbox in ocr_boxes:
        if not (text or "").strip():
            continue  # OCR złapał puste/białe — nie napis
        x1, y1, x2, y2 = bbox
        if (x2 - x1) * (y2 - y1) < min_area:
            continue  # za mały, prawdopodobnie szum
        cover = max((_overlap_ratio(bbox, tb) for tb in tboxes), default=0.0)
        if cover < min_cover:
            flagged.append({"text": text.strip(), "bbox": bbox, "cover": round(cover, 3)})
    flagged.sort(key=lambda f: f["cover"])
    return flagged


def extract_pdf_text_boxes(pdf_path: str, page_index: int = 0):
    """Boxy realnej warstwy tekstowej strony PDF, znormalizowane do 0..1.

    Lazy-import PyMuPDF. Zwraca [] gdy strona nie ma tekstu (cały artwork rastrowy).
    """
    import fitz  # PyMuPDF — lazy

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        pw, ph = page.rect.width, page.rect.height
        if pw <= 0 or ph <= 0:
            return []
        boxes = []
        for b in page.get_text("blocks"):
            x1, y1, x2, y2 = b[:4]
            txt = b[4] if len(b) > 4 else ""
            if not (txt or "").strip():
                continue
            boxes.append((x1 / pw, y1 / ph, x2 / pw, y2 / ph))
        return boxes
    finally:
        doc.close()


def _demo():
    # Strona ma jeden prawdziwy napis (lewa-góra) i jeden wklejony jako grafika (prawa-dół).
    pdf_text = [(0.05, 0.05, 0.35, 0.12)]
    ocr = [
        ("elastoDERM F-IV", (0.06, 0.06, 0.34, 0.11)),   # pokrywa się z tekstem PDF → OK
        ("STERILE EO", (0.60, 0.80, 0.88, 0.87)),        # brak w warstwie tekstu → grafika
        ("  ", (0.10, 0.50, 0.20, 0.55)),                # puste → pomijane
        ("x", (0.50, 0.50, 0.505, 0.505)),               # mikro-szum → pomijane (min_area)
    ]
    out = find_text_as_graphic(ocr, pdf_text)
    assert len(out) == 1, out
    assert out[0]["text"] == "STERILE EO", out
    assert out[0]["cover"] == 0.0, out

    # Cała strona rastrowa (0 boxów tekstu) → oba realne napisy zgłoszone.
    out2 = find_text_as_graphic(ocr, [])
    assert {o["text"] for o in out2} == {"elastoDERM F-IV", "STERILE EO"}, out2

    # Częściowe pokrycie poniżej progu też łapie (tekst PDF minimalnie zachodzi).
    out3 = find_text_as_graphic([("LOT 123", (0.0, 0.0, 0.4, 0.1))],
                                [(0.0, 0.0, 0.05, 0.1)])  # pokrycie ~12% < 30%
    assert len(out3) == 1, out3
    print("artwork_text_layer: OK")


if __name__ == "__main__":
    _demo()
