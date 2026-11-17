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


def test_indesign_and_legacy_tamil_auto_correction():
    samples = [
        ("ப ொதுத்தமிழ்", "பொதுத்தமிழ்"),
        ("குதி – அ இலக்கணம்", "பகுதி – அ இலக்கணம்"),
        ("1.4. ப ொழிப்ப யர்த்தல்", "1.4. மொழிபெயர்த்தல்"),
        ("1.5. வொக்கியம் அம த்தல்", "1.5. வாக்கியம் அமைத்தல்"),
        ("1.6 உவம  அணி", "1.6 உவமை அணி"),
        ("பொருமைத் ததர்வு பசய்தல்", "பொருளைத் தேர்வு செய்தல்"),
        ("வமகயறிதல்", "வகையறிதல்"),
        ("பசொற்கமை ஒழுங்கு டுத்திச்", "சொற்களை ஒழுங்குபடுத்திச்"),
        ("புகழ்ப ற்  நூல்களும்", "புகழ்பெற்ற நூல்களும்"),
        ("பகட் தாய்", "கேட்ட தாய்"),
        ("திரு+பகாயில்", "திரு+கோயில்"),
        ("பகாைி", "கோழி"),
        ("யசாற்கைில்", "சொற்களில்"),
        ("வேறுாடுகலை", "வேறுபாடுகளை"),
        ("பமல் + நாடு = பமனாடு", "மேல் + நாடு = மேனாடு"),
        ("தெரியாதேனால் மேற்குறித்திலைகள் பநர்கின்றன", "தெரியாததனால் மேற்குறித்த பிழைகள் நேர்கின்றன"),
        ("பதிதெண் கீழ்க்கைக்கு நூல்கள்", "பதினெண் கீழ்க்கணக்கு நூல்கள்"),
        ("நால்மணிக்கடிணக", "நான்மணிக்கடிகை"),
        ("சிறுதஞ்சனெனம்", "சிறுபஞ்சமூலம்"),
        ("அகத்திலை நூல்கள்", "அகத்திணை நூல்கள்"),
        ("லணுமொழிக்காஞ்சி", "முதுமொழிக்காஞ்சி"),
        ("தழமொழி நாலூறு", "பழமொழி நானூறு"),
        ("இல்ணா நாற்தணு", "இன்னா நாற்பது"),
        ("இணியணவ நாற்தணு", "இனியவை நாற்பது"),
        ("ஆசாரக்தகாணவ", "ஆசாரக்கோவை"),
        ("சார்ற்தணவ", "சார்ந்தவை"),
        ("ஆறு ணெல்கள் அகத்திணண சார்பாணணவ", "ஆறு நூல்கள் அகத்திணை சார்பானவை"),
        ("என்று லுறத்திணண ணெல்", "ஒன்று புறத்திணை நூல்"),
    ]
    for raw, expected in samples:
        res = normalize_unicode(raw)
        assert expected in res, f"Failed for '{raw}': got '{res}', expected '{expected}'"
