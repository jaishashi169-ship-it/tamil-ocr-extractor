"""Extract editable text, retaining one selected representation per page."""
import argparse
import json
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import fitz
from PIL import Image, ImageOps, ImageSequence
from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn
from html import escape
from functools import lru_cache
from difflib import SequenceMatcher
from fonts import page_profile, repaired_copy, known_profiles
from quality import issues, normalized


def command(args, **kwargs):
    try:
        return subprocess.run(args, capture_output=True, check=True, timeout=180, **kwargs).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(exc.stderr.decode(errors="replace").strip()) from exc
    except FileNotFoundError as exc:
        raise ValueError(f"Install the system dependency {args[0]}") from exc


@lru_cache(maxsize=1)
def installed_languages():
    return set(command(["tesseract", "--list-langs"]).decode().splitlines()[1:])


def recognize(image, lang):
    missing = set(lang.split("+")) - installed_languages()
    if missing:
        raise ValueError("Missing OCR language data: " + ", ".join(sorted(missing)))
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "page.png"
        ImageOps.exif_transpose(image).convert("RGB").save(source)
        return command(["tesseract", str(source), "stdout", "-l", lang, "--psm", "3"]).decode().strip()


def visual_text(page, lang):
    pix = page.get_pixmap(dpi=300, colorspace=fitz.csRGB, alpha=False)
    with Image.frombytes("RGB", (pix.width,pix.height),pix.samples) as img:
        return recognize(img,lang)


def extract(path, lang="tam+eng", mode="auto", encoding="utf-8-sig", verify=False):
    path = Path(path)
    ext = path.suffix.lower()
    sections = []

    def add(text, method, review=False, reasons=(), **extra):
        text = unicodedata.normalize("NFC", text)
        notes = list(reasons) + issues(text)
        if not text.strip():
            notes.append("Empty section")
        sections.append(dict(section=len(sections)+1, method=method,
                             review=bool(review or notes), reasons=notes, text=text, **extra))

    if ext == ".pdf":
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise ValueError("Supply an unlocked PDF")
            known, cache = known_profiles(), {}
            for page in doc:
                profiled = mode != "ocr" and page_profile(page,cache,known)
                native = repaired_copy(page) if profiled else page.get_text(sort=True).strip()
                method = "matched font profile" if profiled else "native"
                broken = bool(issues(native))
                raster = any((fitz.Rect(i["bbox"]) & page.rect).get_area() > .20 * page.rect.get_area()
                             for i in page.get_image_info())
                tamil = any("\u0b80" <= ch <= "\u0bff" for ch in native)
                legacy = not profiled and any(f[3].split("+")[-1].casefold() in
                    {"bamini","suntommy","tam-tamil","tab-tamil"} for f in page.get_fonts())
                compare = verify or (tamil and not profiled and "tam" in lang.split("+"))
                if mode == "native" or (mode == "auto" and native and not (broken or raster or legacy or compare)):
                    add(native, method)
                    continue
                try:
                    ocr = visual_text(page, lang)
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    if not native or mode == "ocr":
                        raise ValueError(f"Page {page.number+1}: {exc}") from exc
                    add(native, method, reasons=[f"OCR unavailable: {exc}"])
                    continue
                if mode == "ocr" or not native:
                    add(ocr,"ocr",reasons=["Check transcription against the page image"])
                    continue
                agreement = SequenceMatcher(None,normalized(native),normalized(ocr),autojunk=False).ratio()
                extra = dict(agreement=round(agreement,4))
                if ocr and (broken or raster or legacy) and not issues(ocr):
                    extra["alternate_text"] = native
                    add(ocr,"ocr",reasons=["Check OCR selection against the page image"],**extra)
                elif normalized(native) != normalized(ocr):
                    extra["alternate_text"] = ocr
                    add(native,method,reasons=["Native and visual readings differ"],**extra)
                else:
                    add(native,method,**extra)
    elif ext in {".jpg",".jpeg",".tif",".tiff",".png",".webp",".bmp"}:
        with Image.open(path) as img:
            for frame in ImageSequence.Iterator(img):
                add(recognize(frame,lang),"ocr",reasons=["Check transcription against the image"])
    elif ext in {".txt",".md",".csv",".tsv"}:
        add(path.read_text(encoding=encoding), "decoded text")
    elif ext in {".docx",".epub",".html",".htm",".odt",".rtf"}:
        reader = "html" if ext==".htm" else ext[1:]
        add(command(["pandoc","-f",reader,"-t","plain","--wrap=none",str(path.resolve())]).decode(), "pandoc text", True)
    elif ext == ".doc":
        add(command(["antiword","-m","UTF-8.txt",str(path.resolve())]).decode(), "antiword text", True)
    else:
        raise ValueError(f"Unsupported input {ext}")
    return sections


def export(sections, path):
    path = Path(path)
    ext = path.suffix.lower()
    texts = [s["text"] for s in sections]
    if ext == ".json":
        path.write_text(json.dumps(sections, ensure_ascii=False, indent=2),encoding="utf-8")
    elif ext == ".txt":
        path.write_text("\n\f\n".join(texts), encoding="utf-8")
    elif ext == ".docx":
        doc = Document()
        normal = doc.styles["Normal"]
        normal.font.name = "Nirmala UI"
        normal.font.size = Pt(11)
        normal._element.rPr.rFonts.set(qn("w:cs"), "Nirmala UI")
        for i, text in enumerate(texts):
            if i:
                doc.add_page_break()
            for line in text.splitlines():
                doc.add_paragraph(line)
        doc.save(path)
    else:
        html = '<!doctype html><meta charset="utf-8"><title>Extracted text</title>' + "".join(
            "<section>"+"".join("<p>"+escape(line)+"</p>" for line in t.splitlines())+"</section>" for t in texts)
        if ext in {".html",".htm"}:
            path.write_text(html,encoding="utf-8")
        elif ext in {".epub",".md",".odt",".rtf"}:
            command(["pandoc","-f","html","-o",str(path.resolve())],input=html.encode())
        else:
            raise ValueError(f"Unsupported output {ext}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("--lang",default="tam+eng")
    parser.add_argument("--mode",choices=["auto","native","ocr"],default="auto")
    parser.add_argument("--verify",action="store_true",help="Compare native PDF pages with OCR in auto mode")
    parser.add_argument("--encoding",default="utf-8-sig")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Choose a separate output path")
    try:
        pages = extract(args.input,args.lang,args.mode,args.encoding,args.verify)
        export(pages,args.output)
        flagged = [p["section"] for p in pages if p["review"]]
        print(f"Extracted {len(pages)} section(s). Review flags: {flagged}")
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1,f"Extraction failed: {exc}\n")
