import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.config import get_settings
from app.services.document_model import BoundingBox, SourceReference, TextBlock
from app.services.tamil_utils import detect_script_language, normalize_unicode

settings = get_settings()
pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


class OCRUnavailableError(RuntimeError):
    """Raised when Tesseract OCR binary or required language packs are unavailable."""


@dataclass
class OCROptions:
    language: str = "tam+eng"  # tam | eng | tam+eng
    auto_rotate: bool = True
    deskew: bool = True
    denoise: bool = False
    enhance_contrast: bool = True
    multi_column: bool = True
    crop_box: Optional[Tuple[int, int, int, int]] = None  # x, y, w, h in pixels


def get_ocr_status() -> Dict[str, Any]:
    """Check whether Tesseract binary is executable, its version, and installed tam/eng language packs."""
    cmd = settings.TESSERACT_CMD
    try:
        ver_res = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        if ver_res.returncode != 0:
            return {
                "available": False,
                "version": None,
                "executable": cmd,
                "languages": [],
                "tam_available": False,
                "eng_available": False,
                "error": ver_res.stderr.strip() or "Tesseract returned non-zero exit code.",
            }
        first_line = (ver_res.stdout or ver_res.stderr or "").splitlines()[0].strip() if (ver_res.stdout or ver_res.stderr) else "unknown"
        langs = get_installed_tesseract_languages()
        return {
            "available": True,
            "version": first_line,
            "executable": cmd,
            "languages": langs,
            "tam_available": "tam" in langs,
            "eng_available": "eng" in langs,
            "error": None,
        }
    except FileNotFoundError:
        return {
            "available": False,
            "version": None,
            "executable": cmd,
            "languages": [],
            "tam_available": False,
            "eng_available": False,
            "error": f"Tesseract executable '{cmd}' not found. Install Tesseract OCR (with tam and eng traineddata) or run via Docker.",
        }
    except Exception as exc:
        return {
            "available": False,
            "version": None,
            "executable": cmd,
            "languages": [],
            "tam_available": False,
            "eng_available": False,
            "error": f"Tesseract check failed: {exc}",
        }


def get_installed_tesseract_languages() -> List[str]:
    """Query Tesseract for actually installed language packs."""
    try:
        langs = pytesseract.get_languages(config="")
        return sorted([l for l in langs if l != "osd"])
    except Exception:
        try:
            res = subprocess.run(
                [settings.TESSERACT_CMD, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [ln.strip() for ln in res.stdout.splitlines()[1:] if ln.strip()]
            return sorted(lines)
        except Exception:
            return []


def _deskew_image(gray: np.ndarray) -> np.ndarray:
    """Estimate skew angle and deskew if angle is between 0.5 and 15 degrees."""
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.5 or abs(angle) > 15.0:
        return gray
    (h, w) = gray.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def preprocess_for_ocr(image_bgr: np.ndarray, opts: OCROptions) -> np.ndarray:
    """Apply optional crop, rotation, deskew, denoise, and contrast enhancement."""
    img = image_bgr
    if opts.crop_box:
        x, y, w, h = opts.crop_box
        ih, iw = img.shape[:2]
        x1 = max(0, min(x, iw - 1))
        y1 = max(0, min(y, ih - 1))
        x2 = max(x1 + 1, min(x + w, iw))
        y2 = max(y1 + 1, min(y + h, ih))
        img = img[y1:y2, x1:x2]

    if opts.auto_rotate:
        try:
            osd = pytesseract.image_to_osd(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), output_type=pytesseract.Output.DICT)
            rot = int(osd.get("rotate", 0))
            if rot == 90:
                img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            elif rot == 180:
                img = cv2.rotate(img, cv2.ROTATE_180)
            elif rot == 270:
                img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        except Exception:
            pass

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img

    if opts.denoise:
        gray = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)

    if opts.enhance_contrast:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

    if opts.deskew:
        gray = _deskew_image(gray)

    return gray


def _order_blocks_reading_order(blocks: List[TextBlock], page_width: int, multi_column: bool) -> List[TextBlock]:
    if not multi_column or len(blocks) < 2 or page_width <= 0:
        return sorted(blocks, key=lambda b: (b.bbox.y if b.bbox else 0.0, b.bbox.x if b.bbox else 0.0))

    mid = page_width / 2.0
    left_col: List[TextBlock] = []
    right_col: List[TextBlock] = []
    spanning: List[TextBlock] = []

    for b in blocks:
        if not b.bbox:
            spanning.append(b)
            continue
        bx1 = b.bbox.x
        bx2 = b.bbox.x + b.bbox.width
        if bx2 < mid * 1.05 and b.bbox.width < page_width * 0.55:
            left_col.append(b)
        elif bx1 > mid * 0.95 and b.bbox.width < page_width * 0.55:
            right_col.append(b)
        else:
            spanning.append(b)

    # Only use 2-column ordering when both columns have meaningful blocks
    if len(left_col) >= 2 and len(right_col) >= 2:
        left_col.sort(key=lambda b: b.bbox.y if b.bbox else 0.0)
        right_col.sort(key=lambda b: b.bbox.y if b.bbox else 0.0)
        spanning.sort(key=lambda b: b.bbox.y if b.bbox else 0.0)
        return spanning + left_col + right_col

    return sorted(blocks, key=lambda b: (b.bbox.y if b.bbox else 0.0, b.bbox.x if b.bbox else 0.0))


def run_ocr_on_image(
    image_path: Path,
    filename: str,
    page_number: int,
    opts: OCROptions,
) -> Tuple[List[TextBlock], Optional[float], List[str]]:
    """
    Run Tesseract OCR on an image file and return (TextBlocks, average_confidence, warnings).
    """
    warnings: List[str] = []
    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS

    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Unable to decode image '{filename}' for OCR.")

    ih, iw = img_bgr.shape[:2]
    if ih * iw > settings.MAX_IMAGE_PIXELS:
        raise ValueError(f"Image pixel count ({ih * iw}) exceeds maximum allowed ({settings.MAX_IMAGE_PIXELS}).")

    processed = preprocess_for_ocr(img_bgr, opts)
    lang_arg = opts.language if opts.language in {"tam", "eng", "tam+eng"} else "tam+eng"

    ocr_status = get_ocr_status()
    if not ocr_status["available"]:
        raise OCRUnavailableError(ocr_status["error"] or "Tesseract OCR is not available on this worker.")

    installed = ocr_status["languages"]
    missing_langs = [req_lang for req_lang in lang_arg.split("+") if installed and req_lang not in installed]
    if missing_langs:
        raise OCRUnavailableError(
            f"Required Tesseract language pack(s) missing: {', '.join(missing_langs)}. Installed: {', '.join(installed) or 'none'}."
        )

    try:
        data = pytesseract.image_to_data(
            processed,
            lang=lang_arg,
            config="--psm 3",
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRUnavailableError(f"Tesseract executable '{settings.TESSERACT_CMD}' not found.") from exc
    except pytesseract.TesseractError as exc:
        raise OCRUnavailableError(f"Tesseract OCR failed: {exc}") from exc

    # Group words into paragraphs (block_num, par_num)
    para_map: dict[tuple[int, int], dict] = {}
    n_items = len(data.get("text", []))
    for i in range(n_items):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        conf_raw = data["conf"][i]
        try:
            conf = float(conf_raw)
        except (ValueError, TypeError):
            conf = -1.0

        key = (int(data["block_num"][i]), int(data["par_num"][i]))
        x = float(data["left"][i])
        y = float(data["top"][i])
        w = float(data["width"][i])
        h = float(data["height"][i])

        if key not in para_map:
            para_map[key] = {
                "words": [],
                "confs": [],
                "x1": x,
                "y1": y,
                "x2": x + w,
                "y2": y + h,
            }
        entry = para_map[key]
        entry["words"].append(word)
        if conf >= 0:
            entry["confs"].append(conf)
        entry["x1"] = min(entry["x1"], x)
        entry["y1"] = min(entry["y1"], y)
        entry["x2"] = max(entry["x2"], x + w)
        entry["y2"] = max(entry["y2"], y + h)

    blocks: List[TextBlock] = []
    all_confs: List[float] = []
    for entry in para_map.values():
        text = normalize_unicode(" ".join(entry["words"]))
        if not text.strip():
            continue
        avg_c = round(sum(entry["confs"]) / len(entry["confs"]), 2) if entry["confs"] else None
        if avg_c is not None:
            all_confs.append(avg_c)
        bbox = BoundingBox(
            x=entry["x1"],
            y=entry["y1"],
            width=max(1.0, entry["x2"] - entry["x1"]),
            height=max(1.0, entry["y2"] - entry["y1"]),
        )
        block_id = str(uuid.uuid4())
        blocks.append(
            TextBlock(
                id=block_id,
                block_type="paragraph",
                text=text,
                raw_ocr_text=text,
                language=detect_script_language(text),
                extraction_method="ocr",
                confidence=avg_c,
                bbox=bbox,
                source_ref=SourceReference(
                    filename=filename,
                    page_number=page_number,
                    block_id=block_id,
                    bbox=bbox,
                ),
            )
        )

    ordered = _order_blocks_reading_order(blocks, processed.shape[1], opts.multi_column)
    page_conf = round(sum(all_confs) / len(all_confs), 2) if all_confs else None
    return ordered, page_conf, warnings
