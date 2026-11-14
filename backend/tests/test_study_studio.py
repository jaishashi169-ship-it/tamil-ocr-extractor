from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.services.study.ai_provider import AIStudyItemSchema, AIStudySupportSchema

client = TestClient(app)


def _upload_and_process_md(headers):
    md_content = (
        "# Chapter 1: Photosynthesis\n\n"
        "## Light Reaction\n\n"
        "Photosynthesis is defined as the process by which green plants convert light energy into chemical energy.\n\n"
        "ஒளிச்சேர்க்கை என்பது பசுமைத் தாவரங்கள் சூரிய ஒளியின் உதவியால் உணவு தயாரிக்கும் நிகழ்வாகும்.\n\n"
        "Chlorophyll absorbs sunlight in the thylakoid membrane of chloroplasts.\n"
    ).encode("utf-8")

    init_r = client.post(
        "/api/v1/uploads/init",
        json={"filename": "biology_ta_en.md", "expected_size_bytes": len(md_content)},
        headers=headers,
    )
    sid = init_r.json()["session_id"]
    client.put(f"/api/v1/uploads/{sid}/chunk/0", files={"chunk": ("c0", md_content, "application/octet-stream")}, headers=headers)
    client.post(f"/api/v1/uploads/{sid}/complete", headers=headers)
    job = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers).json()
    return job["job_id"]


def test_study_studio_unconfigured_returns_503(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()

    auth = client.post("/api/v1/auth/guest").json()
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    status_res = client.get("/api/v1/study/status", headers=headers)
    assert status_res.status_code == 200
    assert status_res.json()["configured"] is False

    job_id = _upload_and_process_md(headers)
    proj_res = client.post(
        "/api/v1/study/projects",
        json={"job_id": job_id, "language": "bilingual", "mark_categories": [1, 2, 3, 5, 10]},
        headers=headers,
    )
    assert proj_res.status_code == 503
    assert proj_res.json()["detail"]["code"] == "AI_PROVIDER_UNCONFIGURED"


def test_study_studio_all_marks_lock_verification_and_exports():
    auth = client.post("/api/v1/auth/guest").json()
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    job_id = _upload_and_process_md(headers)

    mock_items = [
        AIStudyItemSchema(
            item_type="guide_topic",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            language="bilingual",
            question_type="study_guide",
            prompt_text="Study Guide: Light Reaction",
            answer_text="ஒளிச்சேர்க்கை (Photosynthesis) converts light energy into chemical energy.",
            explanation_text="Grounded in page 1.",
            study_support=AIStudySupportSchema(
                key_terms=[{"term": "Photosynthesis", "definition": "Process converting light energy into chemical energy"}],
                revision_notes=["Chlorophyll absorbs sunlight in the thylakoid membrane."],
                common_mistakes=["Confusing light reaction with dark reaction."],
                memory_aid="Photo = light, synthesis = making food.",
                exam_advice="Include both Tamil and English key terms.",
            ),
            source_page=1,
            source_quote="Photosynthesis is defined as the process by which green plants convert light energy into chemical energy.",
        ),
        AIStudyItemSchema(
            item_type="question",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            marks=1,
            difficulty="foundational",
            language="bilingual",
            question_type="mcq",
            prompt_text="Which pigment absorbs sunlight in chloroplasts?",
            answer_text="Chlorophyll",
            options=["Chlorophyll", "Hemoglobin", "Melanin", "Keratin"],
            explanation_text="Chlorophyll absorbs sunlight in the thylakoid membrane.",
            source_page=1,
            source_quote="Chlorophyll absorbs sunlight in the thylakoid membrane of chloroplasts.",
        ),
        AIStudyItemSchema(
            item_type="question",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            marks=2,
            difficulty="foundational",
            language="tam",
            question_type="definition",
            prompt_text="ஒளிச்சேர்க்கை வரையறுக்கவும்.",
            answer_text="ஒளிச்சேர்க்கை என்பது பசுமைத் தாவரங்கள் சூரிய ஒளியின் உதவியால் உணவு தயாரிக்கும் நிகழ்வாகும்.",
            source_page=1,
            source_quote="ஒளிச்சேர்க்கை என்பது பசுமைத் தாவரங்கள் சூரிய ஒளியின் உதவியால் உணவு தயாரிக்கும் நிகழ்வாகும்.",
        ),
        AIStudyItemSchema(
            item_type="question",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            marks=3,
            difficulty="intermediate",
            language="eng",
            question_type="short_answer",
            prompt_text="Explain the role of chlorophyll and light energy.",
            answer_text="Chlorophyll absorbs sunlight and plants convert light energy into chemical energy.",
            source_page=1,
            source_quote="Chlorophyll absorbs sunlight in the thylakoid membrane of chloroplasts.",
        ),
        AIStudyItemSchema(
            item_type="question",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            marks=5,
            difficulty="intermediate",
            language="bilingual",
            question_type="detailed_answer",
            prompt_text="Describe photosynthesis in detail.",
            answer_text="Photosynthesis converts light energy into chemical energy in green plants.",
            source_page=1,
            source_quote="Photosynthesis is defined as the process by which green plants convert light energy into chemical energy.",
        ),
        AIStudyItemSchema(
            item_type="question",
            chapter="Chapter 1: Photosynthesis",
            topic="Light Reaction",
            marks=10,
            difficulty="advanced",
            language="bilingual",
            question_type="extended_essay",
            prompt_text="Write a comprehensive essay on Photosynthesis (unsupported page test).",
            answer_text="Full essay answer.",
            source_page=99,
            source_quote="Nonexistent page quote",
        ),
    ]

    with (
        patch("app.services.study.pipeline.get_ai_provider_status", return_value={"configured": True, "provider": "gemini", "instructions": "mocked"}),
        patch("app.services.study.pipeline.generate_study_items_via_ai", return_value=mock_items),
    ):
        proj_res = client.post(
            "/api/v1/study/projects",
            json={
                "job_id": job_id,
                "language": "bilingual",
                "mark_categories": [1, 2, 3, 5, 10],
                "difficulty": "mixed",
            },
            headers=headers,
        )
        assert proj_res.status_code == 201
        proj = proj_res.json()
        project_id = proj["project_id"]

        marks_present = {it["marks"] for it in proj["items"] if it["item_type"] == "question"}
        assert {1, 2, 3, 5, 10}.issubset(marks_present)

        # Unsupported page 99 question must be flagged as ai_draft with issue_note
        q10 = next(it for it in proj["items"] if it.get("marks") == 10)
        assert q10["verification_status"] == "ai_draft"
        assert q10["issue_note"] is not None

        # Approve & lock first question
        first_q = next(it for it in proj["items"] if it["item_type"] == "question")
        patch_res = client.patch(
            f"/api/v1/study/items/{first_q['id']}",
            json={"verification_status": "user_approved", "is_locked": True, "prompt_text": "LOCKED CUSTOM QUESTION"},
            headers=headers,
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["is_locked"] is True

        # Regenerate unlocked items -> locked question must survive
        regen_res = client.post(f"/api/v1/study/projects/{project_id}/regenerate", headers=headers)
        assert regen_res.status_code == 200
        prompts = [it["prompt_text"] for it in regen_res.json()["items"]]
        assert "LOCKED CUSTOM QUESTION" in prompts

        # Export DOCX, PDF, TXT, MD, XLSX, JSON, ZIP across layouts
        for fmt in ["docx", "pdf", "txt", "md", "xlsx", "json", "zip"]:
            exp_res = client.post(
                f"/api/v1/study/projects/{project_id}/export",
                json={"export_format": fmt, "layout": "complete_study_guide"},
                headers=headers,
            )
            assert exp_res.status_code == 200
            assert len(exp_res.content) > 0

        # Separate questions_only & answer_key TXT check
        q_only = client.post(
            f"/api/v1/study/projects/{project_id}/export",
            json={"export_format": "txt", "layout": "questions_only"},
            headers=headers,
        )
        assert q_only.status_code == 200
        assert "Answer:" not in q_only.text

        ans_key = client.post(
            f"/api/v1/study/projects/{project_id}/export",
            json={"export_format": "txt", "layout": "answer_key"},
            headers=headers,
        )
        assert ans_key.status_code == 200
        assert "Answer:" in ans_key.text
