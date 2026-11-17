"""OCR model discovery and source-preserving text decoding."""
import codecs
import unicodedata
from pathlib import Path

LABELS = dict(tam="தமிழ் · Tamil", eng="English", hin="हिन्दी · Hindi",
              tel="తెలుగు · Telugu", kan="ಕನ್ನಡ · Kannada", mal="മലയാളം · Malayalam",
              ben="বাংলা · Bengali", mar="मराठी · Marathi", guj="ગુજરાતી · Gujarati",
              pan="ਪੰਜਾਬੀ · Punjabi", ori="ଓଡ଼ିଆ · Odia", urd="اردو · Urdu",
              ara="العربية · Arabic", fra="Français", deu="Deutsch", spa="Español",
              jpn="日本語", kor="한국어", chi_sim="简体中文", chi_tra="繁體中文")


def read_source_text(path, encoding):
    data = Path(path).read_bytes()
    if encoding == "auto":
        for mark, codec in [(codecs.BOM_UTF32_LE,"utf-32"),(codecs.BOM_UTF32_BE,"utf-32"),
                            (codecs.BOM_UTF16_LE,"utf-16"),(codecs.BOM_UTF16_BE,"utf-16")]:
            if data.startswith(mark):
                return data.decode(codec)
        encoding = "utf-8-sig"
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise ValueError(f"Cannot decode text using {encoding}. Specify its original character encoding.") from exc


def rtl_paragraph(text):
    for ch in text:
        direction = unicodedata.bidirectional(ch)
        if direction in {"R","AL","L"}:
            return direction != "L"
    return False
