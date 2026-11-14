"""
Generate real permitted Tamil-English sample files (PDF, DOCX, PNG) for verification.
Run inside the backend/worker container or Python environment:
  python samples/generate_samples.py
"""
from pathlib import Path
from docx import Document
from PIL import Image, ImageDraw
import fitz  # PyMuPDF

OUT_DIR = Path(__file__).parent


def main() -> None:
    # 1. Sample DOCX with Tamil + English
    doc = Document()
    doc.add_heading("Tamil English Document Studio — Sample", level=1)
    doc.add_paragraph("தமிழ் என்பது உலகின் மிகப் பழமையான செம்மொழிகளில் ஒன்றாகும்.")
    doc.add_paragraph("Tamil is one of the oldest classical languages in the world.")
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "Language / மொழி"
    tbl.cell(0, 1).text = "Script / எழுத்து"
    tbl.cell(1, 0).text = "Tamil / தமிழ்"
    tbl.cell(1, 1).text = "Unicode (UTF-8)"
    doc.save(OUT_DIR / "sample_tamil_english.docx")

    # 2. Sample text-based PDF
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Tamil English Document Studio - Sample PDF", fontsize=14)
    page.insert_text((72, 110), "Photosynthesis converts light energy into chemical energy.", fontsize=11)
    pdf.save(OUT_DIR / "sample_english_text.pdf")
    pdf.close()

    # 3. Sample scanned-style PNG
    img = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 60), "Tamil English Document Studio OCR Test", fill="black")
    draw.text((40, 110), "Sample scanned image for English and Tamil OCR verification.", fill="black")
    img.save(OUT_DIR / "sample_scan.png")
    print("Generated sample DOCX, PDF, and PNG in", OUT_DIR)


if __name__ == "__main__":
    main()
