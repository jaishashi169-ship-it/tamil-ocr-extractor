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



def test_user_requirements_regression():
    from app.services.tamil_utils import ASSIGNED_TAMIL_CODEPOINTS, normalize_unicode

    regression_cases = [
        # 1. நீதி நூல்கள், item 11
        ("ல௅ணுமொழிக்காஞ்சி", "முதுமொழிக்காஞ்சி"),
        ("லணுமொழிக்காஞ்சி", "முதுமொழிக்காஞ்சி"),

        # 2. அகத்திணை நூல்கள், item 1
        ("஍ஐந்திணை ஍ஐம்பது", "ஐந்திணை ஐம்பது"),
        ("ற்திணண ம்தணு", "ஐந்திணை ஐம்பது"),

        # 3. அகத்திணை நூல்கள், item 2
        ("திணணமொழி ஍ஐம்பது", "திணைமொழி ஐம்பது"),
        ("திணணமொழி ஐம்பது", "திணைமொழி ஐம்பது"),

        # 4. அகத்திணை நூல்கள், item 4
        ("திணணமாலை ணெற்ணறஐம்பது", "திணைமாலை நூற்றைம்பது"),

        # 5. Next section heading
        ("ளுறத்திலை நூல்", "புறத்திணை நூல்"),

        # 6. நாலடியார் paragraph: கொண்டணு -> கொண்டது without breaking valid ணு words
        ("இந்நூல் 400 பாடல்களைக் கொண்டணு.", "இந்நூல் 400 பாடல்களைக் கொண்டது."),
        ("அணு ஆராய்ச்சி மற்றும் நுணுக்கம்", "அணு ஆராய்ச்சி மற்றும் நுணுக்கம்"),

        # 7. Symbol font bullet decoding
        ("\uf0b7 திருக்குறள்", "• திருக்குறள்"),

        # 8. Opening verse punctuation: > -> ,
        ('"நாலடி>நால்மணி>நா நாற்தணு>ற்திணண>லப்"', '"நாலடி, நான்மணி, நானாற்பது, ஐந்திணை, முப்பால்"'),
    ]

    for raw, expected in regression_cases:
        res = normalize_unicode(raw)
        assert expected in res or res == expected, f"Failed for {raw}: got {res}, expected {expected}"

    # Test that unassigned Tamil codepoints (U+0B80..U+0BFF) and private-use (U+E000..U+F8FF) are cleaned
    sample_with_unassigned = "\u0ba1\u0ba2\u0ba5\uf0b7\uf0a7\u0b84\u0bc3\u0bce"
    cleaned = normalize_unicode(sample_with_unassigned)
    for ch in cleaned:
        code = ord(ch)
        assert not (0xE000 <= code <= 0xF8FF), f"Private-use char {hex(code)} found in output"
        if 0x0B80 <= code <= 0x0BFF:
            assert code in ASSIGNED_TAMIL_CODEPOINTS, f"Unassigned Tamil codepoint {hex(code)} found in output"
