"""Integration regression suite. Pass the original 13-page PDF as the argument."""
import io
import shutil
import sys
import tempfile
from pathlib import Path
import fitz
from PIL import Image, ImageDraw, ImageFont
from extract import extract, export
from quality import issues

source = Path(sys.argv[1])
original = extract(source)
assert len(original)==13 and all(p["method"]=="matched font profile" for p in original)
assert not any(issues(p["text"]) for p in original)
assert "திணைமாலை நூற்றைம்பது" in original[1]["text"]
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    mixed = root/"different-book.pdf"
    with fitz.open(source) as sample, fitz.open() as new:
        new.insert_pdf(sample,from_page=1,to_page=1)
        new.new_page().insert_text((60,80),"A separate English chapter.")
        new.save(mixed, garbage=4, deflate=True)
    parts = extract(mixed,lang="eng")
    assert [p["method"] for p in parts] == ["matched font profile","native"]
    assert parts[0]["text"] == original[1]["text"]
    assert parts[1]["text"] == "A separate English chapter."
    img = Image.new("RGB",(1250,290),"white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf",54)
    except OSError:
        font = ImageFont.truetype("arial.ttf",54)
    draw.text((40,70),"Independent scanned page",font=font,fill="black")
    imagepath = root/"page.png"
    img.save(imagepath)
    assert "Independent scanned page" in extract(imagepath,"eng")[0]["text"]
    scan = root/"scan.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=625,height=145)
        page.insert_image(page.rect, filename=str(imagepath))
        doc.save(scan)
    result = extract(scan,"eng")[0]
    assert result["method"]=="ocr" and result["review"] and "Independent scanned page" in result["text"]
    corrupt = root/"legacy.txt"
    corrupt.write_text("஍ஐந்திணை",encoding="utf-8")
    assert extract(corrupt)[0]["review"]
    has_pandoc = shutil.which("pandoc") is not None
    supported_exts = ["txt","json","docx","html"]
    if has_pandoc:
        supported_exts += ["epub","md","odt","rtf"]
    for ext in supported_exts:
        output = root/f"export.{ext}"
        export(parts,output)
        assert output.stat().st_size
    verify_exts = ["docx","html"]
    if has_pandoc:
        verify_exts += ["epub","odt"]
    for ext in verify_exts:
        assert "திணைமாலை நூற்றைம்பது" in extract(root/f"export.{ext}")[0]["text"]
print("Passed: original, renumbered/recombined page, native text, scanned PDF, image, quality flags and exports.")
