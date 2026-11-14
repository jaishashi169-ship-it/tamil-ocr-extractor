import difflib
import re
from typing import Any, Dict, List, Tuple

from app.services.tamil_utils import normalize_unicode


# Common safe punctuation & spacing normalizations that preserve meaning
SAFE_RULES: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"[ \t]{2,}"), " ", "Collapsed extra horizontal spaces"),
    (re.compile(r"\s+([,.;:!?])"), r"\1", "Removed space before punctuation"),
    (re.compile(r"([,.;:!?])([^\s\d\"')\]])"), r"\1 \2", "Added space after punctuation"),
]

COMMON_ENGLISH_TYPOS: Dict[str, str] = {
    "teh": "the",
    "recieve": "receive",
    "seperate": "separate",
    "occured": "occurred",
    "definately": "definitely",
    "enviroment": "environment",
    "goverment": "government",
    "langauge": "language",
    "experiance": "experience",
    "equasion": "equation",
}


def review_grammar_and_spelling(text: str) -> Dict[str, Any]:
    """
    Produce Original, Corrected, and Compare-changes diffs for Tamil and English mixed text.
    Protects quoted strings, formulas, numbers, and capitalized proper nouns.
    """
    original = normalize_unicode(text)
    corrected = original
    notes: List[Dict[str, str]] = []

    for pattern, repl, desc in SAFE_RULES:
        new_txt = pattern.sub(repl, corrected)
        if new_txt != corrected:
            notes.append({"type": "punctuation_spacing", "description": desc})
            corrected = new_txt

    # Protect quoted text and formulas before word-level spelling suggestions
    tokens = re.findall(r"\S+|\s+", corrected)
    out_tokens: List[str] = []
    in_quotes = False
    for tok in tokens:
        if '"' in tok or "“" in tok or "”" in tok:
            in_quotes = not in_quotes
        lower = tok.lower().strip(".,;:!?()")
        if not in_quotes and lower in COMMON_ENGLISH_TYPOS and tok.islower():
            fixed = tok.lower().replace(lower, COMMON_ENGLISH_TYPOS[lower])
            notes.append({
                "type": "spelling_suggestion",
                "original": tok,
                "suggested": fixed,
                "description": f"Spelling suggestion: '{tok}' -> '{fixed}'",
            })
            out_tokens.append(fixed)
        else:
            out_tokens.append(tok)

    corrected = normalize_unicode("".join(out_tokens))

    # Build line-by-line diff for Compare Changes mode
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(),
            corrected.splitlines(),
            fromfile="Original",
            tofile="Corrected",
            lineterm="",
        )
    )

    return {
        "original_text": original,
        "corrected_text": corrected,
        "has_changes": original != corrected,
        "notes": notes,
        "diff": diff_lines,
    }
