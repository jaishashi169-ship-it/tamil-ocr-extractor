import io
import json
import zipfile
from typing import Any, Dict, List, Tuple

import openpyxl
from docx import Document as DocxDocument
from docx.shared import Pt
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from app.services.exporters.office_and_pdf import _find_unicode_font
from app.services.tamil_utils import normalize_unicode


def _filter_items_for_layout(items: List[Dict[str, Any]], layout: str, target_total_marks: int | None = None) -> List[Dict[str, Any]]:
    if layout == "complete_study_guide":
        return items
    questions = [it for it in items if it.get("item_type") == "question"]
    if layout == "chapter_wise":
        return sorted(questions, key=lambda q: (q.get("chapter") or "", q.get("order_index") or 0))
    if layout == "by_marks":
        return sorted(questions, key=lambda q: (int(q.get("marks") or 0), q.get("order_index") or 0))
    if layout == "mock_exam" and target_total_marks:
        selected: List[Dict[str, Any]] = []
        running = 0
        for q in questions:
            m = int(q.get("marks") or 1)
            if running + m <= target_total_marks:
                selected.append(q)
                running += m
        return selected
    return questions


def export_study_project(
    title: str,
    items: List[Dict[str, Any]],
    coverage_report: Dict[str, Any],
    export_format: str,
    layout: str = "complete_study_guide",
    include_source_appendix: bool = True,
    target_total_marks: int | None = None,
) -> Tuple[bytes, str]:
    filtered = _filter_items_for_layout(items, layout, target_total_marks)
    fmt = export_format.lower().strip()

    if fmt == "json":
        payload = {
            "title": title,
            "layout": layout,
            "coverage_report": coverage_report,
            "items": filtered,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "json"

    if fmt == "xlsx":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Question Bank"
        ws.append(["#", "Chapter", "Topic", "Marks", "Difficulty", "Type", "Status", "Question / Topic", "Answer", "Source Ref"])
        for idx, it in enumerate(filtered, 1):
            ws.append([
                idx,
                it.get("chapter", ""),
                it.get("topic", ""),
                it.get("marks") or "-",
                it.get("difficulty", ""),
                it.get("question_type", ""),
                it.get("verification_status", "ai_draft"),
                it.get("prompt_text", ""),
                "" if layout == "questions_only" else it.get("answer_text", ""),
                it.get("source_ref", ""),
            ])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "xlsx"

    if fmt == "txt":
        total_marks = sum(int(it.get("marks") or 0) for it in filtered if it.get("item_type") == "question")
        lines = [f"{title} ({layout.replace('_', ' ').title()})", f"Total Marks: {total_marks}", "=" * 60, ""]
        for idx, it in enumerate(filtered, 1):
            marks_str = f" [{it['marks']} Marks]" if it.get("marks") else ""
            if layout == "answer_key":
                lines.append(f"{idx}.{marks_str} Answer: {it.get('answer_text', '')}")
                if it.get("explanation_text"):
                    lines.append(f"   Explanation: {it['explanation_text']}")
            else:
                lines.append(f"{idx}. {it['prompt_text']}{marks_str} ({it.get('verification_status', 'ai_draft')})")
                if it.get("options"):
                    for oi, opt in enumerate(it["options"], 1):
                        lines.append(f"   {chr(64 + oi)}) {opt}")
                if layout != "questions_only":
                    lines.append(f"   Answer: {it.get('answer_text', '')}")
            if include_source_appendix and it.get("source_ref"):
                lines.append(f"   Source: {it['source_ref']}")
            lines.append("")
        return normalize_unicode("\n".join(lines)).encode("utf-8"), "txt"

    if fmt == "md":
        lines = [f"# {title}", f"_Layout: {layout}_", "", "## Table of Contents"]
        chapters = sorted({it.get("chapter", "General") for it in filtered})
        for ch in chapters:
            lines.append(f"- {ch}")
        lines.append("")
        for idx, it in enumerate(filtered, 1):
            marks_str = f" **[{it['marks']} Marks]**" if it.get("marks") else ""
            lines.append(f"### {idx}. {it['prompt_text']}{marks_str}")
            lines.append(f"*Status: `{it.get('verification_status', 'ai_draft')}` | Topic: {it.get('topic', '')}*")
            if it.get("options"):
                for oi, opt in enumerate(it["options"], 1):
                    lines.append(f"- {chr(64 + oi)}) {opt}")
            if layout != "questions_only":
                lines.append(f"\n**Answer:**\n{it.get('answer_text', '')}")
            if include_source_appendix and it.get("source_ref"):
                lines.append(f"\n> Source: {it['source_ref']}")
            lines.append("")
        return normalize_unicode("\n".join(lines)).encode("utf-8"), "md"

    if fmt == "docx":
        word = DocxDocument()
        word.styles["Normal"].font.name = "Noto Sans Tamil"
        word.styles["Normal"].font.size = Pt(11)
        word.add_heading(title, level=1)
        word.add_paragraph(f"Format: {layout.replace('_', ' ').title()} (Configurable practice format)")

        for idx, it in enumerate(filtered, 1):
            marks_str = f" [{it['marks']} Marks]" if it.get("marks") else ""
            word.add_heading(f"{idx}. {it['prompt_text']}{marks_str}", level=3)
            if it.get("options"):
                for oi, opt in enumerate(it["options"], 1):
                    word.add_paragraph(f"{chr(64 + oi)}) {opt}", style="List Bullet")
            if layout != "questions_only":
                p = word.add_paragraph()
                r = p.add_run(f"Answer: {it.get('answer_text', '')}")
                r.font.name = "Noto Sans Tamil"
            if include_source_appendix and it.get("source_ref"):
                word.add_paragraph(f"Source reference: {it['source_ref']}")
        buf = io.BytesIO()
        word.save(buf)
        return buf.getvalue(), "docx"

    if fmt == "pdf":
        font_path = _find_unicode_font()
        font_name = "Helvetica"
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont("StudioUnicode", font_path))
                font_name = "StudioUnicode"
            except Exception:
                pass
        buf = io.BytesIO()
        pdf_doc = SimpleDocTemplate(buf, pagesize=A4, title=title)
        styles = getSampleStyleSheet()
        body_st = ParagraphStyle("StudyBody", parent=styles["Normal"], fontName=font_name, fontSize=10.5, leading=15)
        head_st = ParagraphStyle("StudyHead", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=17)
        story: List[Any] = [Paragraph(title, head_st), Spacer(1, 12)]
        for idx, it in enumerate(filtered, 1):
            marks_str = f" [{it['marks']} Marks]" if it.get("marks") else ""
            q_safe = normalize_unicode(f"{idx}. {it['prompt_text']}{marks_str}").replace("&", "&amp;").replace("<", "&lt;")
            story.append(Paragraph(q_safe, head_st))
            if layout != "questions_only":
                a_safe = normalize_unicode(it.get("answer_text", "")).replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>")
                story.append(Paragraph(f"Answer: {a_safe}", body_st))
            if include_source_appendix and it.get("source_ref"):
                story.append(Paragraph(f"Source: {it['source_ref']}", body_st))
            story.append(Spacer(1, 8))
        pdf_doc.build(story)
        return buf.getvalue(), "pdf"

    if fmt == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for sub_fmt in ["docx", "pdf", "txt", "md", "xlsx", "json"]:
                sub_bytes, sub_ext = export_study_project(title, items, coverage_report, sub_fmt, layout, include_source_appendix, target_total_marks)
                zf.writestr(f"study_pack.{sub_ext}", sub_bytes)
        return buf.getvalue(), "zip"

    raise ValueError(f"Unsupported study export format '{export_format}'.")
