import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.services.document_model import DocumentModel
from app.services.study.ai_provider import (
    AIProviderUnconfiguredError,
    generate_study_items_via_ai,
    get_ai_provider_status,
)
from app.services.study.grammar import review_grammar_and_spelling
from app.services.tamil_utils import detect_script_language, normalize_unicode

CHAPTER_RE = re.compile(r"^(?:chapter|unit|lesson|அத்தியாயம்|இயல்|பாடம்)\s*\d*[:.\-\s]*.*", re.IGNORECASE)
DEFINITION_RE = re.compile(r"^([^:\-]{3,60})\s*(?:is defined as|means|refers to|என்பது|:=|:)\s*(.+)$", re.IGNORECASE)


def _extract_learning_points(doc: DocumentModel, selected_pages: Optional[List[int]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Stage 1 & 2: Extraction + Structure Agent.
    Identifies chapters, topics, meaningful definitions/facts, and flags unreadable/low-confidence passages.
    """
    learning_points: List[Dict[str, Any]] = []
    passages_needing_review: List[Dict[str, Any]] = []
    current_chapter = "Chapter 1: Core Concepts"
    current_topic = "Overview"

    for page in doc.pages:
        if selected_pages and page.page_number not in selected_pages:
            continue
        if page.confidence_avg is not None and page.confidence_avg < 65.0:
            passages_needing_review.append({
                "page_number": page.page_number,
                "reason": f"Low OCR confidence ({page.confidence_avg:.1f}%). Please verify text before relying on generated questions.",
            })

        for blk in page.blocks:
            txt = normalize_unicode(blk.text.strip())
            if not txt or len(txt) < 8:
                continue
            if blk.block_type == "header_footer":
                continue

            if blk.block_type == "heading" or CHAPTER_RE.match(txt):
                if CHAPTER_RE.match(txt) or (blk.heading_level == 1):
                    current_chapter = txt[:120]
                    current_topic = txt[:120]
                else:
                    current_topic = txt[:120]
                continue

            # Split long paragraphs into distinct sentences/points
            sentences = [s.strip() for s in re.split(r"(?<=[.!?।])\s+|\n+", txt) if len(s.strip()) >= 15]
            if not sentences:
                sentences = [txt]

            for sent in sentences:
                learning_points.append({
                    "id": str(uuid.uuid4()),
                    "chapter": current_chapter,
                    "topic": current_topic,
                    "text": sent,
                    "language": detect_script_language(sent),
                    "source_page": page.page_number,
                    "source_ref": f"{doc.source_filename} · Page {page.page_number}",
                    "confidence": blk.confidence,
                })

    return learning_points, passages_needing_review


def _author_questions_and_answers(
    points: List[Dict[str, Any]],
    mark_categories: List[int],
    difficulty: str,
    target_language: str,
    max_questions: Optional[int],
) -> List[Dict[str, Any]]:
    """
    Stage 3 & 4: Question Author + Answer Author.
    Generates 1, 2, 3, 5, and 10 mark questions grounded strictly in extracted learning points.
    """
    items: List[Dict[str, Any]] = []
    if not points:
        return items

    # Group points by topic for Study Guide cards + higher-mark questions
    topics_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for pt in points:
        key = (pt["chapter"], pt["topic"])
        topics_map.setdefault(key, []).append(pt)

    order_idx = 0
    for (chapter, topic), t_points in topics_map.items():
        combined_notes = " ".join(p["text"] for p in t_points[:6])
        first_page = t_points[0]["source_page"]
        first_ref = t_points[0]["source_ref"]
        lang = target_language if target_language != "bilingual" else detect_script_language(combined_notes)

        # Study Guide Topic Card
        key_terms = []
        for p in t_points:
            m = DEFINITION_RE.match(p["text"])
            if m:
                key_terms.append({"term": m.group(1).strip(), "definition": m.group(2).strip()})

        items.append({
            "item_type": "guide_topic",
            "chapter": chapter,
            "topic": topic,
            "marks": None,
            "difficulty": difficulty,
            "language": lang,
            "question_type": "study_guide",
            "prompt_text": f"Study Guide: {topic}",
            "answer_text": combined_notes,
            "explanation_text": f"Summarized directly from {len(t_points)} extracted source point(s) on page {first_page}.",
            "hint_text": f"Review key terms under {topic}.",
            "scoring_points": [p["text"] for p in t_points[:4]],
            "options": [],
            "study_support": {
                "key_terms": key_terms[:5],
                "revision_notes": [p["text"] for p in t_points[:3]],
                "common_mistakes": [
                    "Avoid omitting key Tamil/English technical terms or mixing up cause-and-effect steps from the source passage."
                ],
                "memory_aid": f"Connect {topic} with its {min(3, len(t_points))} core points from page {first_page}.",
                "exam_advice": "Practice format advice: organize answers with clear headings and source-backed examples.",
            },
            "source_page": first_page,
            "source_ref": first_ref,
            "verification_status": "auto_checked",
            "order_index": order_idx,
        })
        order_idx += 1

        # Generate questions across requested mark categories
        for idx_p, pt in enumerate(t_points):
            if max_questions and sum(1 for it in items if it["item_type"] == "question") >= max_questions:
                break

            txt = pt["text"]
            m_def = DEFINITION_RE.match(txt)

            # 1-Mark Question (MCQ or Fill-in-the-blank)
            if 1 in mark_categories:
                if m_def and idx_p % 2 == 0:
                    term = m_def.group(1).strip()
                    defn = m_def.group(2).strip()
                    items.append({
                        "item_type": "question",
                        "chapter": chapter,
                        "topic": topic,
                        "marks": 1,
                        "difficulty": "foundational" if difficulty == "mixed" else difficulty,
                        "language": pt["language"],
                        "question_type": "mcq",
                        "prompt_text": f"Which term corresponds to: {defn}?",
                        "answer_text": term,
                        "explanation_text": f"Source states: '{txt}' ({pt['source_ref']}).",
                        "hint_text": f"Look at definitions introduced under {topic}.",
                        "scoring_points": [f"Correctly identifies '{term}' (1 mark)."],
                        "options": [term, f"Non-{term}", "Unrelated concept", "None of the above"],
                        "study_support": {},
                        "source_page": pt["source_page"],
                        "source_ref": pt["source_ref"],
                        "verification_status": "auto_checked",
                        "order_index": order_idx,
                    })
                    order_idx += 1
                else:
                    words = txt.split()
                    keyword = max(words, key=len) if words else "concept"
                    blanked = txt.replace(keyword, "______", 1)
                    items.append({
                        "item_type": "question",
                        "chapter": chapter,
                        "topic": topic,
                        "marks": 1,
                        "difficulty": "foundational" if difficulty == "mixed" else difficulty,
                        "language": pt["language"],
                        "question_type": "fill_in_blank",
                        "prompt_text": f"Fill in the blank: {blanked}",
                        "answer_text": keyword.strip(".,;:!?"),
                        "explanation_text": f"From {pt['source_ref']}: {txt}",
                        "hint_text": f"Starts with '{keyword[0]}' ({len(keyword)} characters).",
                        "scoring_points": [f"Exact term '{keyword.strip('.,;:!?')}' (1 mark)."],
                        "options": [],
                        "study_support": {},
                        "source_page": pt["source_page"],
                        "source_ref": pt["source_ref"],
                        "verification_status": "auto_checked",
                        "order_index": order_idx,
                    })
                    order_idx += 1

            # 2-Mark Question
            if 2 in mark_categories and idx_p == 0:
                prompt = f"Define or state two key points about '{m_def.group(1).strip()}'." if m_def else f"State two key points regarding {topic}."
                pts_2 = [p["text"] for p in t_points[:2]]
                items.append({
                    "item_type": "question",
                    "chapter": chapter,
                    "topic": topic,
                    "marks": 2,
                    "difficulty": "foundational" if difficulty == "mixed" else difficulty,
                    "language": pt["language"],
                    "question_type": "short_answer",
                    "prompt_text": prompt,
                    "answer_text": "\n".join(f"• {p}" for p in pts_2),
                    "explanation_text": f"Covers two distinct points from {pt['source_ref']}.",
                    "hint_text": "Include two concise, source-supported statements.",
                    "scoring_points": [f"Point {i+1}: {p[:80]}" for i, p in enumerate(pts_2)],
                    "options": [],
                    "study_support": {},
                    "source_page": pt["source_page"],
                    "source_ref": pt["source_ref"],
                    "verification_status": "auto_checked",
                    "order_index": order_idx,
                })
                order_idx += 1

            # 3-Mark Question
            if 3 in mark_categories and idx_p == 0 and len(t_points) >= 2:
                pts_3 = [p["text"] for p in t_points[:3]]
                items.append({
                    "item_type": "question",
                    "chapter": chapter,
                    "topic": topic,
                    "marks": 3,
                    "difficulty": "intermediate" if difficulty == "mixed" else difficulty,
                    "language": pt["language"],
                    "question_type": "structured_short",
                    "prompt_text": f"Explain the main steps or features of {topic}.",
                    "answer_text": "\n".join(f"{i+1}. {p}" for i, p in enumerate(pts_3)),
                    "explanation_text": f"Structured 3-point explanation grounded in {pt['source_ref']}.",
                    "hint_text": "Break your answer into 3 clear numbered points.",
                    "scoring_points": [f"Step/Point {i+1}: {p[:80]}" for i, p in enumerate(pts_3)],
                    "options": [],
                    "study_support": {},
                    "source_page": pt["source_page"],
                    "source_ref": pt["source_ref"],
                    "verification_status": "auto_checked",
                    "order_index": order_idx,
                })
                order_idx += 1

            # 5-Mark Question
            if 5 in mark_categories and idx_p == 0 and len(t_points) >= 2:
                pts_5 = [p["text"] for p in t_points[:5]]
                items.append({
                    "item_type": "question",
                    "chapter": chapter,
                    "topic": topic,
                    "marks": 5,
                    "difficulty": "intermediate" if difficulty == "mixed" else difficulty,
                    "language": pt["language"],
                    "question_type": "detailed_answer",
                    "prompt_text": f"Discuss {topic} in detail with supporting points from the text.",
                    "answer_text": f"Introduction:\n{pts_5[0]}\n\nKey Details:\n" + "\n".join(f"• {p}" for p in pts_5[1:]),
                    "explanation_text": f"Detailed 5-mark model answer derived from {first_ref}.",
                    "hint_text": "Begin with a brief introduction, then list supporting details.",
                    "scoring_points": [f"Detail {i+1}: {p[:80]}" for i, p in enumerate(pts_5)],
                    "options": [],
                    "study_support": {},
                    "source_page": first_page,
                    "source_ref": first_ref,
                    "verification_status": "auto_checked",
                    "order_index": order_idx,
                })
                order_idx += 1

            # 10-Mark Question
            if 10 in mark_categories and idx_p == 0:
                pts_10 = [p["text"] for p in t_points[:8]]
                items.append({
                    "item_type": "question",
                    "chapter": chapter,
                    "topic": topic,
                    "marks": 10,
                    "difficulty": "advanced" if difficulty == "mixed" else difficulty,
                    "language": pt["language"],
                    "question_type": "extended_essay",
                    "prompt_text": f"Write a comprehensive analysis of {topic} ({chapter}), covering core concepts, details, and implications.",
                    "answer_text": (
                        f"1. Overview ({topic}):\n{pts_10[0]}\n\n"
                        f"2. Detailed Analysis:\n" + "\n".join(f"- {p}" for p in pts_10[1:]) +
                        f"\n\n3. Summary:\nThese points synthesize the material presented on page {first_page}."
                    ),
                    "explanation_text": f"Extended 10-mark model answer organized with headings from {first_ref}.",
                    "hint_text": "Use clear headings: Overview, Detailed Analysis, and Summary.",
                    "scoring_points": [
                        "Overview & definition (2 marks)",
                        "Core supporting details & examples (6 marks)",
                        "Structured conclusion (2 marks)",
                    ],
                    "options": [],
                    "study_support": {},
                    "source_page": first_page,
                    "source_ref": first_ref,
                    "verification_status": "auto_checked",
                    "order_index": order_idx,
                })
                order_idx += 1

    return items


def _verify_source_and_deduplicate(
    items: List[Dict[str, Any]],
    doc: DocumentModel,
) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    """
    Stage 6 & 7: Source-Support Verification, Ambiguity Checks & Duplicate Reviewer.
    Verifies source_page & source_quote against document pages, validates MCQ options,
    and deduplicates by similar prompt wording and learning point.
    """
    page_texts = {p.page_number: normalize_unicode(p.to_plain_text()).lower() for p in doc.pages}
    seen_prompts: set[str] = set()
    kept: List[Dict[str, Any]] = []
    duplicates_removed = 0
    verification_warnings: List[str] = []

    for item in items:
        if item["item_type"] == "question":
            norm_prompt = re.sub(r"\s+", " ", item["prompt_text"].lower()).strip()
            if norm_prompt in seen_prompts:
                duplicates_removed += 1
                continue
            seen_prompts.add(norm_prompt)

            # Verify source reference exists in document
            sp = item.get("source_page")
            quote = normalize_unicode(item.pop("source_quote", "") or "").lower().strip()
            if sp not in page_texts:
                item["verification_status"] = "ai_draft"
                item["issue_note"] = f"Source page {sp} does not exist in document."
                verification_warnings.append(f"Question '{item['prompt_text'][:45]}' references missing page {sp}.")
            elif quote and len(quote) >= 12 and quote[:40] not in page_texts[sp]:
                # Check token overlap fallback
                q_words = {w for w in re.findall(r"\w+", quote) if len(w) > 3}
                p_words = set(re.findall(r"\w+", page_texts[sp]))
                overlap = len(q_words & p_words) / max(1, len(q_words))
                if overlap < 0.4:
                    item["verification_status"] = "ai_draft"
                    item["issue_note"] = "Supporting passage quote could not be verified on cited page."
                    verification_warnings.append(f"Unsupported citation on page {sp} for '{item['prompt_text'][:45]}'.")

            # Verify MCQ has unambiguous unique options
            if item.get("question_type") == "mcq":
                opts = item.get("options", [])
                if len(set(opts)) < 2 or item["answer_text"] not in opts:
                    item["verification_status"] = "ai_draft"
                    item["issue_note"] = "MCQ options need manual review for uniqueness."
                    verification_warnings.append(f"MCQ '{item['prompt_text'][:50]}' flagged for option review.")
        kept.append(item)

    return kept, duplicates_removed, verification_warnings


def run_study_pipeline(
    doc: DocumentModel,
    settings_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Execute the coordinated Study Studio pipeline on an extracted DocumentModel.
    Requires a real configured AI provider (GEMINI_API_KEY or OPENAI_API_KEY);
    raises AIProviderUnconfiguredError if credentials are missing.
    """
    provider_status = get_ai_provider_status()
    if not provider_status["configured"]:
        raise AIProviderUnconfiguredError(provider_status["instructions"])

    selected_pages = settings_dict.get("selected_pages")
    selected_text = (settings_dict.get("selected_text") or "").strip()
    mark_categories = settings_dict.get("mark_categories") or [1, 2, 3, 5, 10]
    difficulty = settings_dict.get("difficulty", "mixed")
    target_language = settings_dict.get("language", "bilingual")
    max_questions = settings_dict.get("question_count")  # None means Maximum meaningful coverage

    # Stage 1 & 2: Extraction & Structure
    points, passages_needing_review = _extract_learning_points(doc, selected_pages)
    if selected_text:
        points = [
            {
                "id": str(uuid.uuid4()),
                "chapter": settings_dict.get("chapter") or "Selected Passage",
                "topic": settings_dict.get("topic") or "Selected Text",
                "text": normalize_unicode(selected_text),
                "language": detect_script_language(selected_text),
                "source_page": (selected_pages[0] if selected_pages else (doc.pages[0].page_number if doc.pages else 1)),
                "source_ref": f"{doc.source_filename} · Selected Text",
                "confidence": 100.0,
            }
        ]

    # Stage 5: Grammar & Spelling Review on each page
    grammar_diffs: List[Dict[str, Any]] = []
    for page in doc.pages:
        if selected_pages and page.page_number not in selected_pages:
            continue
        diff_res = review_grammar_and_spelling(page.to_plain_text())
        grammar_diffs.append({"page_number": page.page_number, **diff_res})

    # Stage 3 & 4: AI-Powered Question & Answer Authoring in bounded batches
    raw_items: List[Dict[str, Any]] = []
    batch_size = 12
    order_idx = 0
    for i in range(0, max(1, len(points)), batch_size):
        batch = points[i : i + batch_size]
        if not batch:
            continue
        ai_items = generate_study_items_via_ai(
            batch_passages=batch,
            mark_categories=mark_categories,
            difficulty=difficulty,
            target_language=target_language,
            max_questions=max_questions,
        )
        for ai_it in ai_items:
            raw_items.append({
                "item_type": ai_it.item_type,
                "chapter": ai_it.chapter,
                "topic": ai_it.topic,
                "marks": ai_it.marks,
                "difficulty": ai_it.difficulty,
                "language": ai_it.language,
                "question_type": ai_it.question_type,
                "prompt_text": normalize_unicode(ai_it.prompt_text),
                "answer_text": normalize_unicode(ai_it.answer_text),
                "explanation_text": normalize_unicode(ai_it.explanation_text),
                "hint_text": normalize_unicode(ai_it.hint_text),
                "scoring_points": [normalize_unicode(s) for s in ai_it.scoring_points],
                "options": [normalize_unicode(o) for o in ai_it.options],
                "study_support": ai_it.study_support.model_dump(),
                "source_page": ai_it.source_page,
                "source_quote": ai_it.source_quote,
                "source_ref": f"{doc.source_filename} · Page {ai_it.source_page}",
                "verification_status": "auto_checked",
                "order_index": order_idx,
            })
            order_idx += 1

    # Stage 6 & 7: Source Verification & Deduplication
    verified_items, duplicates_removed, ver_warnings = _verify_source_and_deduplicate(raw_items, doc)

    processed_pages = [
        p.page_number for p in doc.pages if not selected_pages or p.page_number in selected_pages
    ]
    topics_covered = sorted({it["topic"] for it in verified_items})
    questions_only = [it for it in verified_items if it["item_type"] == "question"]

    by_marks: Dict[str, int] = {}
    for q in questions_only:
        mk = str(q.get("marks") or 0)
        by_marks[mk] = by_marks.get(mk, 0) + 1

    coverage_report = {
        "ai_provider": provider_status["provider"],
        "processed_pages": len(processed_pages),
        "total_document_pages": len(doc.pages),
        "covered_topics_count": len(topics_covered),
        "covered_topics": topics_covered,
        "generated_questions_count": len(questions_only),
        "by_marks": by_marks,
        "duplicates_removed": duplicates_removed,
        "passages_needing_review": passages_needing_review,
        "verification_warnings": ver_warnings,
    }

    return {
        "items": verified_items,
        "coverage_report": coverage_report,
        "grammar_diffs": grammar_diffs,
    }
