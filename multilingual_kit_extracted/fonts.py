"""Recognize tested font resources independently of document object numbers."""
import hashlib
import json
import re
from pathlib import Path


def fingerprint(doc, font):
    ref, _, kind, name, _, encoding = font[:6]
    family = name.split("+")[-1]
    if family not in {"Latha", "Latha,Bold", "SunTommy"}:
        return None
    cmap_kind, cmap = doc.xref_get_key(ref, "ToUnicode")
    if encoding not in {"WinAnsiEncoding","Identity-H"}:
        return None
    if kind == "Type0":
        array_kind, array = doc.xref_get_key(ref, "DescendantFonts")
        if array_kind == "xref":
            array = doc.xref_object(int(array.split()[0]))
        child = re.search(r"\[\s*(\d+) \d+ R\s*\]", array)
        if not child or doc.xref_get_key(int(child[1]), "CIDToGIDMap")[1] != "/Identity":
            return None
    fontbytes = doc.extract_font(ref)[3]
    if not fontbytes or cmap_kind not in {"null", "xref"}:
        return None
    cmap_ref = int(cmap.split()[0]) if cmap_kind == "xref" else None
    digest = hashlib.sha256(fontbytes).hexdigest()
    cmap_hash = hashlib.sha256(doc.xref_stream(cmap_ref)).hexdigest() if cmap_ref else "-"
    return ("/".join((family,kind,encoding,digest,cmap_hash)), cmap_ref)


def page_profile(page, cache, known):
    targets = []
    for font in page.get_fonts():
        family = font[3].split("+")[-1]
        if family not in {"Latha","Latha,Bold","SunTommy"}:
            continue
        if font[0] not in cache:
            cache[font[0]] = fingerprint(page.parent, font)
        record = cache[font[0]]
        if not record or record[0] not in known:
            return False
        if family.startswith("Latha") and font[2] == "Type0":
            targets.append(record[1])
    return bool(targets)


def repaired_copy(page):
    import fitz
    from tamil_profile import repair_fonts, read_page
    with fitz.open() as single:
        single.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        refs = {fingerprint(single,font)[1] for font in single[0].get_fonts()
                if font[3].split("+")[-1].startswith("Latha") and font[2]=="Type0"}
        repair_fonts(single,refs)
        return read_page(single[0])


def known_profiles():
    return set(json.loads(Path(__file__).with_name("profiles.json").read_text()))
