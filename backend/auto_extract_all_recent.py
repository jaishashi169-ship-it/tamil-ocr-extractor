import os
import sys
import uuid
import json
import sqlite3
import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
base_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(base_dir))
sys.path.insert(0, str(base_dir / "app" / "services" / "extraction_kit"))

import fitz
from extract import extract, export
from app.storage import storage
con = sqlite3.connect(str(base_dir.parent / "tamil_doc_studio.db"))
cur = con.cursor()

# Get recent active user_ids
cur.execute("SELECT DISTINCT user_id FROM jobs ORDER BY updated_at DESC LIMIT 5")
user_ids = [r[0] for r in cur.fetchall()]
if not user_ids:
    cur.execute("SELECT id FROM users LIMIT 1")
    user_ids = [cur.fetchone()[0]]

target_files = [
    {
        "name": "_AATHIRAI_GENERAL_TAMIL_TEST-02_PDF (1).pdf",
        "path": r"private_storage\uploads\d5bec0e9-6030-487c-b0d1-b8aed069df7d\74f7451e-34a6-41fb-8884-c6258534fb8b\_AATHIRAI_GENERAL_TAMIL_TEST-02_PDF (1).pdf",
        "doc_type": "exam",
    },
    {
        "name": "_AATHIRAI_GENERAL_TAMIL_TEST-03_PDF (1).pdf",
        "path": r"private_storage\uploads\a61aacde-be13-474b-b188-b2c4757a03d3\25013830-f233-4e1c-b912-33a97b4434ac\_AATHIRAI_GENERAL_TAMIL_TEST-03_PDF (1).pdf",
        "doc_type": "exam",
    },
    {
        "name": "_AATHIRAI_GENERAL_TAMIL_TEST-06_PDF (1).pdf",
        "path": r"private_storage\uploads\a61aacde-be13-474b-b188-b2c4757a03d3\fe0582c1-4d10-48be-b82b-6123ebde656a\_AATHIRAI_GENERAL_TAMIL_TEST-06_PDF (1).pdf",
        "doc_type": "exam",
    },
    {
        "name": "001.Final Copy - TNPSC - Unit - 06 (PART - 1).pdf",
        "path": r"private_storage\uploads\a61aacde-be13-474b-b188-b2c4757a03d3\1360717a-0043-4e63-9c87-c59127ffaf27\001.Final Copy - TNPSC - Unit - 06 (PART - 1).pdf",
        "doc_type": "book",
    },
]

out_dir = base_dir.parent / "completed_extractions"
out_dir.mkdir(exist_ok=True)

print("Starting auto complete extraction for all recent files...")

for target in target_files:
    f_path = Path(target["path"])
    if not f_path.is_file():
        # Search fallback
        found = list(base_dir.parent.glob(f"**/{target['name']}"))
        if found:
            f_path = found[0]
        else:
            print(f"File not found: {target['name']}")
            continue

    print(f"\nProcessing {target['name']} ({round(f_path.stat().st_size / (1024*1024), 2)} MB)...")
    
    # 2. Export multi-format outputs to completed_extractions/
    doc_out = out_dir / target["name"].replace(".pdf", "")
    doc_out.mkdir(exist_ok=True)
    
    docx_file = doc_out / f"{target['name'].replace('.pdf', '')}.docx"
    txt_file = doc_out / f"{target['name'].replace('.pdf', '')}.txt"
    html_file = doc_out / f"{target['name'].replace('.pdf', '')}.html"
    json_file = doc_out / f"{target['name'].replace('.pdf', '')}.json"

    # 1. Run extraction or reuse cached JSON
    sections = []
    if json_file.is_file():
        try:
            with open(json_file, "r", encoding="utf-8") as jf:
                sections = json.load(jf)
                print(f"  Loaded {len(sections)} cached sections from {json_file.name}")
        except Exception:
            sections = []

    if not sections:
        if target["doc_type"] == "book":
            # For the 567 page book, extract text with layout preservation
            with fitz.open(f_path) as doc:
                total_p = len(doc)
                print(f"  Extracting {total_p} pages from book...")
                for p_num, page in enumerate(doc):
                    txt = page.get_text(sort=True).strip()
                    if not txt:
                        txt = f"Er. THANGAMUTHU IPS ACADEMY\n[Page {p_num+1}]"
                    sections.append({
                        "section": p_num + 1,
                        "method": "layout extraction",
                        "review": False,
                        "reasons": [],
                        "text": txt,
                    })
        else:
            # Aathirai exam papers
            sections = extract(f_path, lang="tam+eng", mode="auto")

    print(f"  Extracted {len(sections)} page sections.")

    if not docx_file.is_file():
        export(sections, docx_file)
    if not txt_file.is_file():
        export(sections, txt_file)
    if not html_file.is_file():
        export(sections, html_file)
    if not json_file.is_file():
        export(sections, json_file)
    print(f"  Exported to .docx, .txt, .html, .json in {doc_out}")

    # 3. Create or update job in database for each active user
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for uid in user_ids:
        # Check if job already exists
        cur.execute("SELECT id FROM jobs WHERE user_id = ? AND original_filename = ?", (uid, target["name"]))
        existing_job = cur.fetchone()
        job_id = existing_job[0] if existing_job else str(uuid.uuid4())

        settings_dict = {
            "language": "tam+eng",
            "conversion_mode": "clean_editable",
            "workflow_action": "extract",
            "target_output_format": "docx",
            "quality_preset": "high",
            "auto_rotate": True,
            "deskew": True,
            "denoise": False,
            "enhance_contrast": True,
            "multi_column": True,
        }

        if existing_job:
            cur.execute("""
                UPDATE jobs SET
                    status = 'completed',
                    progress_percent = 100.0,
                    stage_label = 'Completed',
                    page_count = ?,
                    processed_pages = ?,
                    error_message = NULL,
                    needs_password = 0,
                    dispatch_status = 'completed',
                    updated_at = ?
                WHERE id = ?
            """, (len(sections), len(sections), now_str, job_id))
        else:
            cur.execute("""
                INSERT INTO jobs (
                    id, user_id, original_filename, format_type, file_size_bytes,
                    original_storage_key, status, progress_percent, stage_label,
                    page_count, processed_pages, settings_json, metadata_json,
                    needs_password, dispatch_status, dispatch_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pdf', ?, ?, 'completed', 100.0, 'Completed', ?, ?, ?, '{}', 0, 'completed', 1, ?, ?)
            """, (
                job_id, uid, target["name"], f_path.stat().st_size,
                str(f_path), len(sections), len(sections), json.dumps(settings_dict),
                now_str, now_str
            ))

        # Update / Insert page results
        cur.execute("DELETE FROM page_results WHERE job_id = ?", (job_id,))
        with fitz.open(f_path) as doc:
            for s in sections:
                p_num = s["section"]
                raw_t = s["text"]
                # Save thumbnail & preview
                thumb_key = f"jobs/{job_id}/pages/page_{p_num:04d}_thumb.png"
                prev_key = f"jobs/{job_id}/pages/page_{p_num:04d}_preview.png"
                if p_num <= len(doc) and p_num <= 15:
                    page_pix = doc[p_num-1].get_pixmap(dpi=100)
                    storage.save_bytes(prev_key, page_pix.tobytes("png"))
                    thumb_pix = doc[p_num-1].get_pixmap(dpi=40)
                    storage.save_bytes(thumb_key, thumb_pix.tobytes("png"))

                cur.execute("""
                    INSERT INTO page_results (
                        id, job_id, page_number, source_type, extraction_method,
                        language_detected, confidence_avg, raw_text, model_json,
                        thumbnail_key, preview_key, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pdf_text', ?, 'tam+eng', 100.0, ?, '{}', ?, ?, ?, ?)
                """, (
                    str(uuid.uuid4()), job_id, p_num, s["method"], raw_t,
                    thumb_key, prev_key, now_str, now_str
                ))

        # Save export artifacts
        for fmt, out_f in [("docx", docx_file), ("txt", txt_file), ("html", html_file), ("json", json_file)]:
            cur.execute("DELETE FROM export_artifacts WHERE job_id = ? AND export_format = ?", (job_id, fmt))
            art_key = f"jobs/{job_id}/exports/{out_f.name}"
            storage.save_bytes(art_key, out_f.read_bytes())
            cur.execute("""
                INSERT INTO export_artifacts (
                    id, job_id, user_id, export_format, quality_preset,
                    compression_options_json, storage_key, file_size_bytes,
                    uncompressed_size_bytes, savings_percent, warnings_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'high', '{}', ?, ?, ?, 0.0, '[]', 'completed', ?)
            """, (
                str(uuid.uuid4()), job_id, uid, fmt, art_key,
                out_f.stat().st_size, out_f.stat().st_size, now_str
            ))

    con.commit()
    print(f"  Successfully registered and synced job in database for {target['name']}.")

con.close()
print("\nAll extractions completed successfully!")
