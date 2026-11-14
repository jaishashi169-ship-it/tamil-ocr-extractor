import pytest
from app.security import detect_format_by_signature, sanitize_filename, sanitize_html, safe_join


def test_sanitize_filename_prevents_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\secret.txt") == "secret.txt"
    assert sanitize_filename("தமிழ்_ஆவணம்.pdf") == "தமிழ்_ஆவணம்.pdf"


def test_safe_join_blocks_escape(tmp_path):
    with pytest.raises(ValueError):
        safe_join(tmp_path, "..", "outside.txt")


def test_magic_byte_signature_validation():
    fmt, err = detect_format_by_signature(b"%PDF-1.7\n...", "sample.pdf")
    assert fmt == "pdf" and err is None

    # Spoofed extension must fail
    fmt, err = detect_format_by_signature(b"%PDF-1.7\n...", "fake.png")
    assert fmt is None and err is not None

    fmt, err = detect_format_by_signature(b"8BPS\x00\x01", "design.psd")
    assert fmt == "psd" and err is None

    fmt, err = detect_format_by_signature(b"\x89PNG\r\n\x1a\n", "scan.png")
    assert fmt == "png" and err is None


def test_html_sanitization_strips_scripts():
    dirty = '<p>வணக்கம்</p><script>alert("xss")</script><a href="javascript:evil()" onclick="steal()">link</a>'
    clean = sanitize_html(dirty)
    assert "<script>" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert "வணக்கம்" in clean
