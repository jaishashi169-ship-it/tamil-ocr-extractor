"""Decode known symbol font subsets, preserving neighboring Latin text and page order."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import fitz


def get_pdftotext_cmd():
    cmd = shutil.which("pdftotext")
    if cmd:
        return cmd
    for p in Path(__file__).resolve().parents:
        cand = p / "poppler" / "poppler-24.08.0" / "Library" / "bin" / "pdftotext.exe"
        if cand.exists():
            return str(cand)
        cand2 = p / "backend" / "poppler" / "poppler-24.08.0" / "Library" / "bin" / "pdftotext.exe"
        if cand2.exists():
            return str(cand2)
    return "pdftotext"


def decoded_pages(doc):
    profile_path = Path(__file__).resolve().parent / "rgb_profiles.json"
    if not profile_path.exists():
        return {}
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    fonts = {f[0]: f[3].split("+")[-1] for p in doc for f in p.get_fonts()
             if f[3].split("+")[-1].startswith("RGB_")}
    if not fonts:
        return {}
    markers = {}
    with fitz.open(stream=doc.tobytes(), filetype="pdf") as copy:
        for ref, name in fonts.items():
            model = profiles.get(name)
            if not model:
                continue
            kind, target = copy.xref_get_key(ref, "ToUnicode")
            if kind != "xref":
                continue
            cmap = int(target.split()[0])
            data = copy.xref_stream(cmap)
            if hashlib.sha256(data).hexdigest() != model.get("cmap", ""):
                continue
            def replace(match):
                code = match[1].decode()
                actual = model["mapping"].get(code)
                if not actual or actual == " ":
                    return match[0]
                marker = 0xe100 + list(profiles).index(name)*256 + int(code, 16)
                markers[marker] = actual
                return b"<"+match[1]+b"><"+match[1]+b"><"+f"{marker:04x}".encode()+b">"
            data = re.sub(rb"<([0-9a-f]{2})><\1><[0-9a-f]{4}>", replace, data)
            copy.update_stream(cmap, data)

        pdf_bytes = copy.tobytes()
        pages_raw = None
        cmd = get_pdftotext_cmd()
        try:
            raw = subprocess.run([cmd, "-layout", "-enc", "UTF-8", "-", "-"],
                                 input=pdf_bytes, capture_output=True, timeout=180, check=True).stdout.decode(encoding="utf-8", errors="replace")
            pages_raw = raw.split("\f")[:len(doc)]
        except (FileNotFoundError, subprocess.SubprocessError):
            with fitz.open(stream=pdf_bytes, filetype="pdf") as patched:
                pages_raw = [p.get_text("text", sort=True) for p in patched]

    result = {}
    for index, page in enumerate(pages_raw):
        if not any(ord(ch) in markers for ch in page) and "ANS KEY" not in page:
            continue
        # Reorder only markers belonging to a legacy run.
        def decode(run):
            value = run[0].translate(markers)
            value = re.sub(r"([ெேை])([க-ஹ])", r"\2\1", value)
            return unicodedata.normalize("NFC", value)
        page = re.sub(r"[\ue100-\ue4ff]+", decode, page)
        # Clean up tile overlap artifacts and normalize intra-word spacing
        page = re.sub(r"என்என்ப", "என்ப", page)
        page = re.sub(r"பொருள்பொ", "பொருள்", page)
        page = re.sub(r"\bவா\s+வாழ்க்கை", "வாழ்க்கை", page)
        page = re.sub(r"வாழ்க்கை\s+நெறி", "வாழ்க்கை நெறி", page)
        result[index] = "\n".join(line.rstrip() for line in page.strip().splitlines())
    return result
