# Tamil and multilingual text extractor

Run this Python 3.11+ component in the backend. The worker can run independently of a JavaScript frontend. Text output is UTF-8 and has a form-feed between source pages. It preserves printed page numbers 2 through 14. The original sometimes prints unusual punctuation or spacing. This is a transcription, not a revised edition.

## Install (Debian/Ubuntu)
```sh
sudo apt-get update
sudo apt-get install -y python3-venv tesseract-ocr tesseract-ocr-tam tesseract-ocr-eng pandoc antiword
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
tesseract --list-langs
```

## Run
```sh
python extract.py book.pdf book.txt
python extract.py book.pdf book.json
python extract.py scan.png scan.docx --lang tam+eng
python extract.py other.pdf other.txt --mode ocr --lang tam
python extract.py lessons.epub lessons.docx
python extract.py notes.docx notes.epub
python check_sample.py "/path/to/original.pdf"
```
Select installed Tesseract language codes explicitly. To OCR Hindi and English, install tesseract-ocr-hin and use --lang hin+eng. Native-text paths do not require an OCR language model. For non-UTF-8 text files specify --encoding, e.g. utf-16. A wrong but decodable legacy text encoding may need a font-specific conversion and a reference image.

## Integrate with an existing app
```python
from extract import extract, export

sections = extract(upload_path, lang="tam+eng")
export(sections, download_path)  # extension selects the output format
```
Pass the original upload bytes to the worker via a file retaining its extension. Return the generated file or the sections (JSON) to your frontend. Render each section once and use its text for export. A PDF section corresponds to a physical page. Show OCR sections beside their page images for proofreading. A review flag is a heuristic, not a probability of correctness.

## Routing and limits
- PDF routing is per page. Six verified embedded font resource signatures cover the supplied Latha and SunTommy subsets. Signatures include the embedded font program, encoding and original ToUnicode map. The code checks the identity CID-to-glyph mapping, locates object references at runtime and repairs a temporary page copy. This handles reordered pages and files reusing those resources. Different subsets take the generic path. The verified profile reconstructs horizontal lines and is intended for the source’s single-column layout.
- On other PDFs, auto mode reads native text and checks for invalid Unicode, private-use characters and Latin letters inside Tamil words. Unknown Tamil text receives a visual cross-check when tam is selected. Large raster regions, known legacy fonts, or a suspect text layer trigger OCR. If both outputs look plausible but disagree, JSON returns native text plus the OCR alternative and a review reason. Agreement measures similarity after removing whitespace, not correctness. If the OCR model is missing, usable native text is returned with a warning. Scan-only pages raise an explicit error.
- Use --verify to cross-check every PDF page in auto mode, including English pages; use --mode ocr when a legacy font produces plausible but wrong text. Always display review reasons to the operator before finalizing an export. Multi-column order and tables need visual review.
- JPG, PNG, TIFF (including multiple frames), BMP, WebP: OCR. Use legible source images. OCR language packs are system dependencies.
- DOCX, EPUB, ODT, HTML and RTF: Pandoc reads document text. DOC uses antiword. These paths do not OCR text inside embedded pictures or preserve page layout, drawings, footnotes and tables with full fidelity. For image-based office files, export to PDF and use the OCR path. EPUB support covers unencrypted books. TXT, Markdown, CSV and TSV inputs preserve literal text.
- Output: TXT, JSON, DOCX, HTML, Markdown, EPUB, ODT, RTF. JSON includes section number, source method and review flag. All are text exports. DOCX uses editable paragraphs and page breaks. Unsupported extensions raise errors.
- Verification: the included check uses hand-transcribed first-page book titles and the thirteen page footers. To measure broader OCR accuracy, transcribe additional representative pages independently and compare them with the selected output. Count substitutions, insertions and deletions after consistent NFC normalization. Divide their sum by reference length for character error rate. Report the language, sample size and whether spaces count.

## Container and regression checks
Build the included image to install the Tamil and English models. Mount a directory containing the upload and use an output path in the mount.
```sh
docker build -t tamil-extractor .
docker run --rm -v "$PWD:/data" tamil-extractor /data/input.pdf /data/result.json
python test_routes.py "/path/to/original.pdf"
```
Use the JSON text field as the editable source for the preview and exports. If the reviewer edits a section, pass the updated sections directly to export(). This avoids recomputing OCR when switching download formats.
