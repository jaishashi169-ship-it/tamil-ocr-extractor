import collections
import re
import unicodedata


def issues(text):
    counts = collections.Counter()
    for ch in text:
        kind = unicodedata.category(ch)
        if ch == "\ufffd" or kind in {"Co", "Cn"}:
            counts[f"U+{ord(ch):04X}"] += 1
    notes = [f"Unexpected character {code} ({count} occurrences)" for code, count in counts.items()]
    if re.search(r"[\u0b80-\u0bff][A-Za-z][\u0b80-\u0bff]", text):
        notes.append("Latin letter embedded inside Tamil text")
    return notes


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))
