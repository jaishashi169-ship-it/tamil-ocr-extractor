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


def read_page(page):
    rows = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                value = span["text"]
                if span["font"] == "SunTommy":
                    value = value.translate(str.maketrans({">":",", ",":"இ", ";":"்", '"':"”",
                                                          "‘":"“","’":"“","‚":"”","‛":"”","“":"–"}))
                if span["font"] == "Symbol":
                    value = value.replace("\uf0b7","•")
                y = round(span["origin"][1])
                rows.setdefault(y,[]).append((span["bbox"][0],value))
    lines = []
    for _,parts in sorted(rows.items()):
        value = "".join(v for _,v in sorted(parts,key=lambda item:item[0])).strip()
        value = re.sub(r"([\ue000-\ue002])([க-ஹ])",r"\2\1",value)
        value = value.translate(str.maketrans("\ue000\ue001\ue002","ெேை"))
        value = re.sub(r"([க-ஹ]) +([்ா-்])", r"\1\2", value)
        value = re.sub(r"நீதி நூல்கள ்", "நீதி நூல்கள்", value)
        value = re.sub(r"அகத்திணை நூல்கள ்", "அகத்திணை நூல்கள்", value)
        if value:
            lines.append(unicodedata.normalize("NFC",value))

    title_repairs = {
        r"^1\.\s+திர.*": "1. திருக்குறள்",
        r"^2\.\s+நாலடியா.*": "2. நாலடியார்",
        r"^3\.\s+நானமண.*": "3. நான்மணிக்கடிகை",
        r"^4\.\s+இன.*நாற.*": "4. இன்னா நாற்பது",
        r"^5\.\s+இனியவை.*": "5. இனியவை நாற்பது",
        r"^6\.\s+திரிகடி.*|6\.\s+திரகடி.*": "6. திரிகடுகம்",
        r"^7\.\s+ஆசாரக்.*": "7. ஆசாரக்கோவை",
        r"^8\.\s+பழமொழி.*": "8. பழமொழி நானூறு",
        r"^9\.\s+.*": "9. சிறுபஞ்சமூலம்",
        r"^10\.\s+ஏலாதி.*": "10. ஏலாதி",
        r"^11\.\s+.*": "11. முதுமொழிக்காஞ்சி",
        r"^1\.\s+ஐந்திணை.*": "1. ஐந்திணை ஐம்பது",
        r"^2\.\s+திணைமொழி.*": "2. திணைமொழி ஐம்பது",
        r"^4\.\s+திணைமாலை.*": "4. திணைமாலை நூற்றைம்பது",
        r"^[புள]றத்திலை நூல்": "புறத்திணை நூல்",
    }
    cleaned_lines = []
    for line in lines:
        for pat, rep in title_repairs.items():
            if re.search(pat, line):
                line = rep
                break
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)
