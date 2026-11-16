import base64
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import httpx
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
    document_type: str = "printed"  # printed | textbook | handwritten
    engine: str = "auto"  # auto | tesseract | gemini
    force_ocr: bool = False


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


def _suppress_gutter_shadows(gray: np.ndarray) -> np.ndarray:
    """Detect and whiten dark gutter binding shadows along outer vertical margins in textbook scans."""
    h, w = gray.shape[:2]
    out = gray.copy()
    page_med = float(np.median(gray))
    if page_med > 90 and w > 100:
        margin_w = max(10, int(w * 0.06))
        left_band = gray[:, :margin_w]
        if np.mean(left_band) < page_med * 0.45:
            out[:, :margin_w] = 255
        right_band = gray[:, -margin_w:]
        if np.mean(right_band) < page_med * 0.45:
            out[:, -margin_w:] = 255
    return out


def preprocess_for_ocr(image_bgr: np.ndarray, opts: OCROptions) -> np.ndarray:
    """Apply optional crop, rotation, deskew, denoise, handwriting enhancement, and contrast enhancement."""
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

    # Textbook specific preprocessing: suppress spine gutter shadows
    if opts.document_type == "textbook":
        gray = _suppress_gutter_shadows(gray)

    # Handwritten specific preprocessing: bilateral filter removes paper fibers and notebook lines without blurring ink strokes
    if opts.document_type == "handwritten":
        gray = cv2.bilateralFilter(gray, d=7, sigmaColor=50, sigmaSpace=50)

    if opts.denoise and opts.document_type != "handwritten":
        gray = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)

    if opts.enhance_contrast:
        clip = 2.5 if opts.document_type == "handwritten" else 2.0
        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

    if opts.deskew:
        gray = _deskew_image(gray)

    if opts.document_type == "handwritten":
        # Adaptive Gaussian thresholding for varying ink pressure and thin cursive strokes
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 11
        )
        kernel = np.ones((2, 2), np.uint8)
        gray = cv2.morphologyEx(adaptive, cv2.MORPH_CLOSE, kernel)

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


def run_gemini_vision_ocr(
    image_path: Path,
    filename: str,
    page_number: int,
    opts: OCROptions,
) -> Tuple[List[TextBlock], Optional[float], List[str]]:
    """
    Execute high-accuracy multimodal Vision OCR using Gemini 2.0 Flash.
    Specialized for Tamil & English handwriting, textbooks, poetry verses, and complex layouts.
    """
    settings = get_settings()
    api_key = (settings.GEMINI_API_KEY or "").strip()
    if not api_key:
        raise OCRUnavailableError("GEMINI_API_KEY is not configured on the server.")

    warnings: List[str] = []
    with open(image_path, "rb") as img_f:
        img_bytes = img_f.read()
    b64_data = base64.b64encode(img_bytes).decode("utf-8")

    ext = image_path.suffix.lower()
    mime_type = "image/png" if ext == ".png" else "image/jpeg"

    system_prompt = (
        "You are an expert bilingual Tamil and English Optical Character Recognition (OCR) and transcription engine.\n"
        "Your task is to accurately extract all text from the provided document image with 100% language fidelity.\n"
        "CRITICAL RULES:\n"
        "1. Language Fidelity: Extract both Tamil (தமிழ்) and English words exactly as written, preserving original words, technical terms, names, and numbers without translation or omission.\n"
        "2. Handwriting Recognition: If the document is handwritten (Tamil handwriting, cursive English, student notebooks, answer scripts), decipher every character and ligature with high precision.\n"
        "3. Textbooks & Layouts: Preserve multi-column reading order (read column 1 top-to-bottom, then column 2), chapter headings, subheadings, bullet lists, and tables.\n"
        "4. Tamil Poetry & Verses: For couplets (Thirukkural), Sangam literature (செய்யுள்), and poems, strictly preserve the exact verse lines and stanza breaks.\n"
        "5. Output Unicode: Return clean, standard Unicode UTF-8 NFC text.\n"
        "Return ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "blocks": [\n'
        '    {\n'
        '      "text": "exact extracted paragraph, verse or heading text",\n'
        '      "block_type": "paragraph" | "heading" | "verse" | "list_item",\n'
        '      "heading_level": 1 | 2 | 3 | null,\n'
        '      "language": "tam" | "eng" | "tam+eng",\n'
        '      "confidence": 98.0\n'
        '    }\n'
        '  ],\n'
        '  "confidence_avg": 98.0\n'
        "}"
    )

    doc_style = f"Document type: {opts.document_type}." if opts.document_type else "Document type: printed."
    user_prompt = f"Transcribe all Tamil and English text on page {page_number} of '{filename}'. {doc_style} Language preference: {opts.language}."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": b64_data}},
                    {"text": user_prompt},
                ],
            }
        ],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }

    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        res_json = resp.json()

    candidates = res_json.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates.")
    raw_text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))

    parsed = json.loads(raw_text)
    raw_blocks = parsed.get("blocks", [])

    with Image.open(image_path) as im:
        iw, ih = im.size

    blocks: List[TextBlock] = []
    confs: List[float] = []
    n_blocks = max(1, len(raw_blocks))
    block_h = ih / n_blocks

    for idx, b in enumerate(raw_blocks):
        b_text = normalize_unicode(b.get("text", "").strip())
        if not b_text:
            continue
        c = float(b.get("confidence", 98.0))
        confs.append(c)
        b_type = b.get("block_type", "paragraph")
        if b_type not in {"heading", "paragraph", "list_item", "table_cell", "caption", "header_footer", "verse"}:
            b_type = "paragraph"
        b_id = str(uuid.uuid4())
        y_pos = idx * block_h
        bbox = BoundingBox(x=0.0, y=y_pos, width=float(iw), height=block_h)
        blocks.append(
            TextBlock(
                id=b_id,
                block_type=b_type,
                heading_level=b.get("heading_level"),
                text=b_text,
                raw_ocr_text=b_text,
                language=b.get("language") or detect_script_language(b_text),
                extraction_method="gemini_vision",
                confidence=c,
                bbox=bbox,
                source_ref=SourceReference(filename=filename, page_number=page_number, block_id=b_id, bbox=bbox),
            )
        )

    avg_conf = round(sum(confs) / len(confs), 2) if confs else 98.0
    return blocks, avg_conf, warnings


def _run_tesseract_ocr(
    image_path: Path,
    filename: str,
    page_number: int,
    opts: OCROptions,
) -> Tuple[List[TextBlock], Optional[float], List[str]]:
    """Run Tesseract OCR on an image file with document-type-optimized PSM and character models."""
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

    # Document-type-optimized Page Segmentation Mode (PSM)
    if opts.document_type == "handwritten":
        psm_config = "--psm 6"  # Uniform block of text prevents fragmentation of handwriting
    elif opts.document_type == "textbook":
        psm_config = "--psm 3" if opts.multi_column else "--psm 4"
    else:
        psm_config = "--psm 3"

    try:
        data = pytesseract.image_to_data(
            processed,
            lang=lang_arg,
            config=psm_config,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRUnavailableError(f"Tesseract executable '{settings.TESSERACT_CMD}' not found.") from exc
    except pytesseract.TesseractError as exc:
        raise OCRUnavailableError(f"Tesseract OCR failed: {exc}") from exc

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

        # Detect Tamil poetry/verse layout (e.g. Thirukkural couplets or short poem lines)
        block_type = "paragraph"
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) >= 2:
            avg_words = sum(len(ln.split()) for ln in lines) / len(lines)
            if avg_words <= 6.5 and all(len(ln) < 55 for ln in lines):
                block_type = "verse"

        blocks.append(
            TextBlock(
                id=block_id,
                block_type=block_type,
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


def run_ocr_on_image(
    image_path: Path,
    filename: str,
    page_number: int,
    opts: OCROptions,
) -> Tuple[List[TextBlock], Optional[float], List[str]]:
    """
    Unified OCR runner supporting intelligent routing between Multimodal Vision AI (Gemini)
    and enhanced local Tesseract with handwriting/textbook preprocessing.
    """
    warnings: List[str] = []
    settings = get_settings()
    has_gemini = bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip())

    should_use_gemini = False
    if opts.engine == "gemini":
        should_use_gemini = True
    elif opts.engine == "auto":
        # In auto mode, use Gemini Vision for handwritten documents when API key is configured
        if has_gemini and opts.document_type == "handwritten":
            should_use_gemini = True

    if should_use_gemini and has_gemini:
        try:
            return run_gemini_vision_ocr(image_path, filename, page_number, opts)
        except Exception as exc:
            warnings.append(f"Gemini Vision OCR attempt failed ({exc}); falling back to local Tesseract.")

    # Execute enhanced local Tesseract OCR
    blocks, conf, t_warns = _run_tesseract_ocr(image_path, filename, page_number, opts)
    warnings.extend(t_warns)

    # Intelligent fallback: If Tesseract yielded very low confidence (< 45%) or 0 blocks on handwritten/textbook documents, upgrade via Gemini Vision
    if has_gemini and (conf is None or conf < 45.0 or len(blocks) == 0):
        try:
            g_blocks, g_conf, g_warns = run_gemini_vision_ocr(image_path, filename, page_number, opts)
            warnings.append("Low-confidence Tesseract result automatically upgraded via Gemini Vision OCR.")
            warnings.extend(g_warns)
            return g_blocks, g_conf, warnings
        except Exception:
            pass

    return blocks, conf, warnings
