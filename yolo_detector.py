"""
yolo_detector.py — YOLOv8 packaging element detector for medical artwork validation.

Usage modes
-----------
1. Inference (trained model):
       detector = ArtworkDetector("models/artwork_yolov8.pt")
       results  = detector.detect(pil_image)
       # → list of {class_name, confidence, x1_pct, y1_pct, x2_pct, y2_pct}

2. Training:
       python yolo_detector.py train --data dataset.yaml --epochs 100

3. Export to ONNX (for CPU deployment without ultralytics at runtime):
       python yolo_detector.py export --weights runs/detect/train/weights/best.pt

Packaging element classes
-------------------------
The model is trained to detect the following elements on medical packaging labels.
Each class corresponds to a region that is safety-critical for MDR compliance.

    0  ean_barcode        — EAN-13 barcode (linear)
    1  gs1_128            — GS1-128 / UDI barcode
    2  ce_mark            — CE marking + notified body number
    3  ref_number         — REF / catalogue number field
    4  lot_placeholder    — LOT / batch placeholder (may be empty)
    5  exp_placeholder    — EXP / expiry date placeholder
    6  single_use_symbol  — Single-use / do-not-reuse symbol (ISO 7000-1051)
    7  sterility_symbol   — Sterility indicator (STERILE EO / R symbol)
    8  size_table         — Glove / product size table (XS–XL)
    9  manufacturer_block — Manufacturer name + address block
   10  eu_rep_block       — EU authorised representative block
   11  distributor_block  — Distributor / importer block (e.g. ACME)
   12  product_name       — Primary product name text area
   13  gauge_badge        — Gauge / French size badge (yellow circle)
   14  latex_free_symbol  — Latex-free symbol / text
   15  caution_symbol     — CAUTION / WARNING symbol
   16  ifu_symbol         — IFU / consult instructions symbol
   17  revision_block     — Document revision / version block
   18  color_spec         — Colour specification (Pantone / CMYK)
   19  translation_block  — Multi-language translation block

Dataset preparation
-------------------
Expected directory layout (YOLO format):
    dataset/
        images/
            train/  *.jpg / *.png
            val/    *.jpg / *.png
        labels/
            train/  *.txt   (one annotation per line: class cx cy w h, normalised)
            val/    *.txt
        dataset.yaml

Minimum recommended dataset size: ~200 annotated artworks per class.
Use a tool like LabelImg, CVAT, or Roboflow to create YOLO-format annotations.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("yolo_detector")

# ── Class definitions ─────────────────────────────────────────────────────────

CLASS_NAMES = [
    "ean_barcode",        # 0
    "gs1_128",            # 1
    "ce_mark",            # 2
    "ref_number",         # 3
    "lot_placeholder",    # 4
    "exp_placeholder",    # 5
    "single_use_symbol",  # 6
    "sterility_symbol",   # 7
    "size_table",         # 8
    "manufacturer_block", # 9
    "eu_rep_block",       # 10
    "distributor_block",  # 11
    "product_name",       # 12
    "gauge_badge",        # 13
    "latex_free_symbol",  # 14
    "caution_symbol",     # 15
    "ifu_symbol",         # 16
    "revision_block",     # 17
    "color_spec",         # 18
    "translation_block",  # 19
]

# MDR-critical classes — a missing or changed detection in these classes
# triggers a 'critical' finding in the comparison report.
CRITICAL_CLASSES = {
    "ean_barcode", "gs1_128", "ce_mark", "ref_number",
    "lot_placeholder", "exp_placeholder",
    "single_use_symbol", "sterility_symbol",
    "size_table", "manufacturer_block", "eu_rep_block",
}

# Clearly-minor classes — a change here is informational, not a defect.
# (Pozostałe, nie-krytyczne i nie-info klasy pozostają „important".)
INFO_CLASSES = {
    "color_spec", "translation_block", "gauge_badge",
    "ifu_symbol", "caution_symbol",
}


def _class_severity(cls_name: str) -> str:
    """Maps a detected class to a finding severity: critical / info / important."""
    if cls_name in CRITICAL_CLASSES:
        return "critical"
    if cls_name in INFO_CLASSES:
        return "info"
    return "important"

# ── Lazy model singleton ──────────────────────────────────────────────────────

_yolo_model = None
_yolo_model_path: str = ""


def _get_model(weights: str = "models/artwork_yolov8.pt"):
    """Lazy-load YOLOv8 model. Returns model or None if weights file not found."""
    global _yolo_model, _yolo_model_path
    if _yolo_model is not None and _yolo_model_path == weights:
        return _yolo_model
    if not os.path.isfile(weights):
        logger.debug("YOLOv8 weights not found: %s (training required)", weights)
        return None
    try:
        from ultralytics import YOLO
        _yolo_model = YOLO(weights)
        _yolo_model_path = weights
        logger.info("YOLOv8 model loaded: %s", weights)
        return _yolo_model
    except Exception as exc:
        logger.warning("YOLOv8 load failed: %s", exc)
        return None


# ── Inference ─────────────────────────────────────────────────────────────────

class ArtworkDetector:
    """High-level detector — wraps YOLOv8 inference for artwork element detection."""

    def __init__(self, weights: str = "models/artwork_yolov8.pt",
                 confidence: float = 0.40):
        self.weights    = weights
        self.confidence = confidence

    def detect(self, img, page_idx: int = 0) -> list:
        """Detect packaging elements in a PIL image.

        Returns list of dicts:
            {class_name, class_id, confidence,
             x1_pct, y1_pct, x2_pct, y2_pct,   # bbox as % of image size
             severity}                            # 'critical' | 'important' | 'info'
        """
        model = _get_model(self.weights)
        if model is None:
            return []
        try:
            # Przekaż obraz PIL (RGB) wprost — ultralytics traktuje ndarray jako BGR
            # (konwencja OpenCV), więc np.array(RGB) zamieniał kanały R↔B i psuł
            # detekcję klas zależnych od koloru (np. żółty badge gauge, color_spec).
            rgb = img.convert("RGB")
            W, H = rgb.size
            results = model.predict(rgb, conf=self.confidence, verbose=False)
            detections = []
            for r in results:
                for box in r.boxes:
                    cls_id = int(box.cls[0])
                    cls_name = (CLASS_NAMES[cls_id]
                                if cls_id < len(CLASS_NAMES) else f"class_{cls_id}")
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    detections.append({
                        "class_name":  cls_name,
                        "class_id":    cls_id,
                        "confidence":  round(float(box.conf[0]), 3),
                        "x1_pct":      round(x1 / W * 100, 2),
                        "y1_pct":      round(y1 / H * 100, 2),
                        "x2_pct":      round(x2 / W * 100, 2),
                        "y2_pct":      round(y2 / H * 100, 2),
                        "severity":    _class_severity(cls_name),
                        "page_idx":    page_idx,
                    })
            return detections
        except Exception as exc:
            logger.warning("YOLOv8 detection error: %s", exc)
            return []

    def compare(self, img_a, img_b) -> list:
        """Detect elements in both artworks and compare.

        Returns field_report-compatible rows for integration with ArtworkCompareResult.
        """
        # Przy wielu detekcjach tej samej klasy zachowaj NAJPEWNIEJSZĄ (nie ostatnią
        # przypadkową) — deterministyczny wybór najlepszego pudełka.
        def _by_class_best(dets):
            out = {}
            for d in dets:
                c = d["class_name"]
                if c not in out or d["confidence"] > out[c]["confidence"]:
                    out[c] = d
            return out
        dets_a = _by_class_best(self.detect(img_a, page_idx=0))
        dets_b = _by_class_best(self.detect(img_b, page_idx=0))
        all_classes = sorted(set(list(dets_a.keys()) + list(dets_b.keys())))

        rows = []
        for cls in all_classes:
            da = dets_a.get(cls)
            db = dets_b.get(cls)

            if da and db:
                # Both found — compare position (centre)
                ca_x = (da["x1_pct"] + da["x2_pct"]) / 2
                ca_y = (da["y1_pct"] + da["y2_pct"]) / 2
                cb_x = (db["x1_pct"] + db["x2_pct"]) / 2
                cb_y = (db["y1_pct"] + db["y2_pct"]) / 2
                shift = ((ca_x - cb_x) ** 2 + (ca_y - cb_y) ** 2) ** 0.5
                changed = shift > 5.0  # >5% shift = layout change
                val_a = f"({ca_x:.0f}%, {ca_y:.0f}%) conf={da['confidence']:.0%}"
                val_b = f"({cb_x:.0f}%, {cb_y:.0f}%) conf={db['confidence']:.0%}"
                note = f"YOLO: przesunięcie {shift:.1f}%" if changed else "YOLO: pozycja zgodna"
            elif da:
                changed = True
                val_a = f"({(da['x1_pct']+da['x2_pct'])/2:.0f}%, {(da['y1_pct']+da['y2_pct'])/2:.0f}%) conf={da['confidence']:.0%}"
                val_b = "BRAK"
                note = "YOLO: element obecny w matrycy, brak u dostawcy"
            else:
                changed = True
                val_a = "BRAK"
                val_b = f"({(db['x1_pct']+db['x2_pct'])/2:.0f}%, {(db['y1_pct']+db['y2_pct'])/2:.0f}%) conf={db['confidence']:.0%}"
                note = "YOLO: element dodany u dostawcy"

            rows.append({
                "field":    f"[YOLO] {cls}",
                "val_a":    val_a,
                "val_b":    val_b,
                "severity": _class_severity(cls),
                "changed":  changed,
                "note":     note,
            })
        return rows


# ── Training helpers ──────────────────────────────────────────────────────────

DATASET_YAML_TEMPLATE = """\
# YOLOv8 dataset configuration — medical artwork element detection
# Generated by yolo_detector.py

path: {dataset_root}
train: images/train
val:   images/val

nc: {nc}
names: {names}
"""


def generate_dataset_yaml(dataset_root: str,
                           output_path: str = "dataset.yaml") -> str:
    """Write a dataset.yaml for the artwork element classes."""
    content = DATASET_YAML_TEMPLATE.format(
        dataset_root=os.path.abspath(dataset_root),
        nc=len(CLASS_NAMES),
        names=CLASS_NAMES,
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Dataset YAML written: %s", output_path)
    return output_path


def train(data_yaml: str = "dataset.yaml",
          epochs: int = 100,
          imgsz: int = 1024,
          batch: int = 8,
          base_model: str = "yolov8m.pt",
          project: str = "runs/detect",
          name: str = "artwork_train",
          device: str = "cpu") -> str:
    """Launch YOLOv8 training.

    Recommended settings for medical artwork detection:
    - imgsz=1024: artworks have fine details (EAN digits, CE numbers)
    - batch=8: fits 16 GB GPU; reduce to 4 for 8 GB
    - base_model='yolov8m.pt': medium — good balance of speed and accuracy
      (use 'yolov8l.pt' if GPU memory allows, for higher mAP)
    - device='cpu' | '0' | '0,1' — GPU index or 'cpu'

    Returns path to best weights.
    """
    try:
        from ultralytics import YOLO
        model = YOLO(base_model)
        model.train(
            data=data_yaml,
            epochs=epochs,
            imgsz=imgsz,
            batch=batch,
            project=project,
            name=name,
            patience=20,           # early stopping
            save=True,
            device=device,
            augment=True,
            mosaic=1.0,
            degrees=5.0,           # small rotations (artworks are mostly upright)
            scale=0.3,             # scale variation (±30% — simulates scan zoom)
            fliplr=0.0,            # no horizontal flip (text would be mirrored)
            flipud=0.0,            # no vertical flip
            hsv_h=0.01,
            hsv_s=0.3,
            hsv_v=0.3,
        )
        weights = Path(project) / name / "weights" / "best.pt"
        logger.info("Training complete. Best weights: %s", weights)
        return str(weights)
    except Exception as exc:
        logger.error("Training failed: %s", exc)
        raise


def export_onnx(weights: str, output: str = "models/artwork_yolov8.onnx") -> str:
    """Export trained weights to ONNX for CPU deployment."""
    try:
        from ultralytics import YOLO
        model = YOLO(weights)
        model.export(format="onnx", dynamic=True, simplify=True)
        src = str(Path(weights).with_suffix(".onnx"))
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        if src != output:
            import shutil
            shutil.move(src, output)
        logger.info("ONNX export: %s", output)
        return output
    except Exception as exc:
        logger.error("ONNX export failed: %s", exc)
        raise


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="YOLOv8 artwork element detector")
    sub = parser.add_subparsers(dest="cmd")

    p_train = sub.add_parser("train", help="Train the model")
    p_train.add_argument("--data",       default="dataset.yaml")
    p_train.add_argument("--epochs",     type=int,   default=100)
    p_train.add_argument("--imgsz",      type=int,   default=1024)
    p_train.add_argument("--batch",      type=int,   default=8)
    p_train.add_argument("--base-model", default="yolov8m.pt")
    p_train.add_argument("--project",    default="runs/detect")
    p_train.add_argument("--name",       default="artwork_train")
    p_train.add_argument("--device",     default="cpu",
                         help="Device: 'cpu', '0' (GPU 0), '0,1' (multi-GPU)")

    p_export = sub.add_parser("export", help="Export to ONNX")
    p_export.add_argument("--weights", required=True)
    p_export.add_argument("--output",  default="models/artwork_yolov8.onnx")

    p_yaml = sub.add_parser("yaml", help="Generate dataset.yaml")
    p_yaml.add_argument("--dataset", required=True, help="Root directory of dataset")
    p_yaml.add_argument("--output",  default="dataset.yaml")

    args = parser.parse_args()

    if args.cmd == "train":
        best = train(data_yaml=args.data, epochs=args.epochs, imgsz=args.imgsz,
                     batch=args.batch, base_model=args.base_model,
                     project=args.project, name=args.name,
                     device=args.device)
        print(f"Best weights: {best}")

    elif args.cmd == "export":
        out = export_onnx(args.weights, args.output)
        print(f"Exported: {out}")

    elif args.cmd == "yaml":
        out = generate_dataset_yaml(args.dataset, args.output)
        print(f"Dataset YAML: {out}")

    else:
        parser.print_help()
