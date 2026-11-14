import re
import unicodedata
from collections import Counter
from typing import Dict, List, Tuple

TAMIL_RANGE_RE = re.compile(r"[\u0B80-\u0BFF]")
LATIN_RANGE_RE = re.compile(r"[A-Za-z]")

# Explicit Bamini-to-Unicode mapping (longest tokens matched first)
BAMINI_MAP: List[Tuple[str, str]] = [
    # Two-part / special vowel combinations
    ("n~", "ஷை"),
    ("n[", "ஜை"),
    ("n\\", "ஸை"),
    ("n`", "ஹை"),
    ("nf", "கை"),
    ("n~", "ஷை"),
    # Grantha & special characters
    ("~", "ஷ"),
    ("[", "ஜ"),
    ("\\", "ஸ"),
    ("`", "ஹ"),
    ("=", "ஸ்ரீ"),
    # Vowels
    ("m", "அ"),
    ("M", "ஆ"),
    ("c", "இ"),
    ("C", "ஈ"),
    ("v", "உ"),
    ("V", "ஊ"),
    ("v", "எ"),
    ("V", "ஏ"),
    ("I", "ஐ"),
    ("x", "ஒ"),
    ("X", "ஓ"),
    ("xs", "ஔ"),
    (";", "்"),
    # Consonants
    ("f", "க"),
    ("q", "ங"),
    ("r", "ச"),
    ("Q", "ஞ"),
    ("l", "ட"),
    ("z", "ண"),
    ("j", "த"),
    ("e", "ந"),
    ("d", "ன"),
    ("g", "ப"),
    ("k", "ம"),
    ("a", "ய"),
    ("u", "ர"),
    ("w", "ற"),
    ("y", "ல"),
    ("s", "ள"),
    ("o", "ழ"),
    ("t", "வ"),
    ("d", "ன"),
    # Vowel signs
    ("h", "ா"),
    ("p", "ி"),
    ("P", "ீ"),
    ("]", "ு"),
    ("}", "ூ"),
    ("n", "ே"),
    ("i", "ை"),
]

# Explicit TAB (Tamil Bilingual) high-byte / standard mapping subset
TAB_MAP: Dict[int, str] = {
    0xA1: "ா",
    0xA2: "ி",
    0xA3: "ீ",
    0xA4: "ு",
    0xA5: "ூ",
    0xA6: "ெ",
    0xA7: "ே",
    0xA8: "ை",
    0xAA: "ொ",
    0xAB: "ோ",
    0xAC: "ௌ",
    0xAD: "்",
    0xDC: "அ",
    0xDD: "ஆ",
    0xDE: "இ",
    0xDF: "ஈ",
    0xE0: "உ",
    0xE1: "ஊ",
    0xE2: "எ",
    0xE3: "ஏ",
    0xE4: "ஐ",
    0xE5: "ஒ",
    0xE6: "ஓ",
    0xE7: "ஔ",
    0xE8: "க",
    0xE9: "ங",
    0xEA: "ச",
    0xEB: "ஞ",
    0xEC: "ட",
    0xED: "ண",
    0xEE: "த",
    0xEF: "ந",
    0xF0: "ப",
    0xF1: "ம",
    0xF2: "ய",
    0xF3: "ர",
    0xF4: "ல",
    0xF5: "வ",
    0xF6: "ழ",
    0xF7: "ள",
    0xF8: "ற",
    0xF9: "ன",
    0xFA: "ஜ",
    0xFB: "ஷ",
    0xFC: "ஸ",
    0xFD: "ஹ",
}

SUPPORTED_LEGACY_ENCODINGS = {
    "bamini": "Bamini (Legacy Tamil Font Encoding)",
    "tab": "TAB (Tamil Bilingual Standard)",
}


def normalize_unicode(text: str) -> str:
    """Normalize text to Unicode NFC, preserving Tamil combining vowel signs."""
    if not text:
        return ""
    return unicodedata.normalize("NFC", text)


def detect_script_language(text: str) -> str:
    """Detect whether text contains Tamil, English, or mixed Tamil+English."""
    if not text or not text.strip():
        return "unknown"
    tamil_count = len(TAMIL_RANGE_RE.findall(text))
    latin_count = len(LATIN_RANGE_RE.findall(text))
    if tamil_count > 0 and latin_count > 0:
        # Require at least minimal presence of both
        if tamil_count >= 2 and latin_count >= 2:
            return "tam+eng"
        return "tam" if tamil_count >= latin_count else "eng"
    if tamil_count > 0:
        return "tam"
    if latin_count > 0:
        return "eng"
    return "unknown"


def convert_legacy_tamil(text: str, source_encoding: str) -> Tuple[str, List[str]]:
    """
    Convert explicitly selected legacy Tamil encoding (bamini or tab) to Unicode.
    Never guesses or overwrites silently; returns (converted_text, warnings).
    """
    enc = source_encoding.lower().strip()
    if enc not in SUPPORTED_LEGACY_ENCODINGS:
        raise ValueError(f"Unsupported legacy encoding '{source_encoding}'. Supported: {list(SUPPORTED_LEGACY_ENCODINGS.keys())}")

    warnings: List[str] = [
        f"Converted from explicit '{enc}' encoding. Please review Tamil vowel signs and punctuation before exporting."
    ]

    if enc == "tab":
        out_chars: List[str] = []
        for ch in text:
            code = ord(ch)
            out_chars.append(TAB_MAP.get(code, ch))
        return normalize_unicode("".join(out_chars)), warnings

    # Bamini conversion: reorder pre-base vowel signs (n/N/i) where applicable
    converted = text
    # Sort by key length descending for greedy token match
    for src, dst in sorted(BAMINI_MAP, key=lambda kv: len(kv[0]), reverse=True):
        converted = converted.replace(src, dst)
    return normalize_unicode(converted), warnings


def detect_repeated_headers_footers(pages_text: List[str], min_pages: int = 3) -> Dict[str, List[str]]:
    """
    Identify repeated first/last non-empty lines across pages (candidate headers, footers, page numbers).
    """
    if len(pages_text) < min_pages:
        return {"headers": [], "footers": []}

    top_lines: Counter[str] = Counter()
    bottom_lines: Counter[str] = Counter()

    for page_text in pages_text:
        lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
        if not lines:
            continue
        top_lines[lines[0]] += 1
        if len(lines) > 1:
            bottom_lines[lines[-1]] += 1

    threshold = max(min_pages, len(pages_text) // 2)
    headers = [line for line, cnt in top_lines.items() if cnt >= threshold]
    footers = [line for line, cnt in bottom_lines.items() if cnt >= threshold]
    return {"headers": headers, "footers": footers}


def remove_headers_footers_from_text(text: str, headers: List[str], footers: List[str], remove_page_numbers: bool = True) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    cleaned: List[str] = []
    for idx, ln in enumerate(lines):
        stripped = ln.strip()
        if idx == 0 and stripped in headers:
            continue
        if idx == len(lines) - 1 and stripped in footers:
            continue
        if remove_page_numbers and re.fullmatch(r"(?:Page\s*)?\d{1,4}(?:\s*/\s*\d{1,4})?", stripped, re.IGNORECASE):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned)
