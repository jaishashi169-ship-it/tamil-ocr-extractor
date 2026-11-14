from app.services.tamil_utils import (
    convert_legacy_tamil,
    detect_repeated_headers_footers,
    detect_script_language,
    normalize_unicode,
)
from app.services.study.grammar import review_grammar_and_spelling


def test_tamil_unicode_preservation_and_detection():
    mixed = "தமிழ் மொழி ஒரு செம்மொழி. Tamil and English mixed paragraph 2026."
    norm = normalize_unicode(mixed)
    assert "தமிழ்" in norm
    assert detect_script_language(norm) == "tam+eng"
    assert detect_script_language("வணக்கம் நண்பர்களே") == "tam"
    assert detect_script_language("Hello world") == "eng"


def test_explicit_bamini_conversion():
    converted, warns = convert_legacy_tamil("jkpo;", "bamini")
    assert "த" in converted
    assert len(warns) >= 1


def test_repeated_header_footer_detection():
    pages = [
        "My Textbook Header\nContent on page 1\n1",
        "My Textbook Header\nContent on page 2\n2",
        "My Textbook Header\nContent on page 3\n3",
    ]
    res = detect_repeated_headers_footers(pages)
    assert "My Textbook Header" in res["headers"]


def test_grammar_review_modes():
    src = "teh  langauge is தமிழ் ."
    res = review_grammar_and_spelling(src)
    assert res["original_text"] == normalize_unicode(src)
    assert "the language" in res["corrected_text"]
    assert res["has_changes"] is True
