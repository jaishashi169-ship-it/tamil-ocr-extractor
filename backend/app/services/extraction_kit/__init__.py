"""Multilingual and Tamil Extraction Kit."""
from .extract import extract, export, installed_languages
from .rgb_fonts import decoded_pages
from .source_languages import read_source_text, rtl_paragraph, LABELS
from .fonts import page_profile, repaired_copy, known_profiles
from .quality import issues, normalized

__all__ = [
    "extract",
    "export",
    "installed_languages",
    "decoded_pages",
    "read_source_text",
    "rtl_paragraph",
    "LABELS",
    "page_profile",
    "repaired_copy",
    "known_profiles",
    "issues",
    "normalized",
]
