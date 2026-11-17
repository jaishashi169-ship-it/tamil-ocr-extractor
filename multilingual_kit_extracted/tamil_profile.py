"""Glyph mappings for the verified Latha subsets and SunTommy punctuation."""
import re
import unicodedata


def repair_fonts(document, refs):
    # The original CMaps use contiguous ranges across gaps in Tamil Unicode.
    # Replace the two CID maps in memory. The original PDF stays untouched.
    mapping = dict(zip(range(53, 65), "ஃஅஆஇஈஉஊஎஏஐஒஓ"))
    mapping.update(dict(zip(range(66, 85), "கஙசஜஞடணதநனபமயரறலளழவ")))
    mapping.update(dict(zip(range(88, 91), "ாிீ")))
    mapping.update({93:"\ue000", 94:"\ue001", 95:"\ue002", 142:"ி",143:"ி",
                    145:"ீ",147:"ீ",202:" ",312:"“",313:"”",315:"–",427:"ர்",428:"ரி"})
    mapping.update({i: c+"்" for i,c in enumerate("கஙசஜஞடணதநனபமயரறலளழவ",116)})
    for start, syllables in [(148,"கு கூ"),(152,"சு"),(156,"டி"),(158,"டு"),
                              (160,"ணு ணூ து தூ நு நூ"),(167,"னு னூ பு பூ மு மூ யு"),
                              (175,"ரு"),(178,"று"),(180,"லு லூ ளு"),(184,"ழு"),(186,"வு")]:
        mapping.update(dict(enumerate(syllables.split(),start)))
    for ref in refs:
        original = document.xref_stream(ref)
        entries = [f"<{gid:04X}> <{value.encode('utf-16-be').hex()}>" for gid,value in mapping.items()]
        body = "\n".join(f"{len(entries[i:i+80])} beginbfchar\n" + "\n".join(entries[i:i+80]) + "\nendbfchar" for i in range(0,len(entries),80))
        old = original.split(b"endcodespacerange")[0] + b"endcodespacerange\n"
        document.update_stream(ref,old+body.encode()+b"\nendcmap\n"+original.split(b"endcmap\n")[1])


def clean_tamil_line(text):
    text = text.replace("\uf0b7", "•")
    text = text.replace(">", ",")
    text = text.replace("\uf0b7 \n", "• ")
    text = text.replace("\uf0b7\n", "• ")
    # Kombu + Consonant + AA: \ue000 + C + ா -> C + ொ
    text = re.sub(r"\ue000([க-ஹ])ா", r"\1ொ", text)
    # Kombu2 + Consonant + AA: \ue001 + C + ா -> C + ோ
    text = re.sub(r"\ue001([க-ஹ])ா", r"\1ோ", text)
    # Single prefix vowels:
    text = re.sub(r"([\ue000-\ue002])([க-ஹ])", r"\2\1", text)
    text = text.translate(str.maketrans("\ue000\ue001\ue002", "ெேை"))
    return unicodedata.normalize("NFC", text)


def read_page(page):
    blocks = sorted(page.get_text("blocks"), key=lambda b: (round(b[1], 1), round(b[0], 1)))
    lines = []
    for b in blocks:
        raw_text = b[4].strip()
        if not raw_text:
            continue
        cleaned = clean_tamil_line(raw_text)
        for line in cleaned.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
    return "\n".join(lines)
