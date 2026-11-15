from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Job, StudyItem, StudyProject, User
from app.services.document_model import build_reviewed_document_model as _build_reviewed_document_model
from app.services.study.ai_provider import AIProviderUnconfiguredError, get_ai_provider_status
from app.services.study.pipeline import run_study_pipeline

router = APIRouter(prefix="/study", tags=["Study Studio"])


class CreateStudyProjectRequest(BaseModel):
    job_id: str
    title: Optional[str] = None
    language: str = "bilingual"  # tam | eng | bilingual
    subject: Optional[str] = None
    exam_board: Optional[str] = None
    selected_pages: Optional[List[int]] = None
    selected_text: Optional[str] = None
    chapter: Optional[str] = None
    topic: Optional[str] = None
    output_mode: str = "combined"  # study_guide | question_bank | practice_paper | combined
    question_count: Optional[int] = None  # None = Maximum meaningful coverage
    difficulty: str = "mixed"  # foundational | intermediate | advanced | mixed
    mark_categories: List[int] = Field(default_factory=lambda: [1, 2, 3, 5, 10])
    strictly_source_based: bool = True


class UpdateStudyItemRequest(BaseModel):
    prompt_text: Optional[str] = None
    answer_text: Optional[str] = None
    explanation_text: Optional[str] = None
    hint_text: Optional[str] = None
    verification_status: Optional[str] = None  # ai_draft | auto_checked | user_approved
    is_locked: Optional[bool] = None
    issue_note: Optional[str] = None


class ExportStudyRequest(BaseModel):
    export_format: str = Field(default="docx", pattern="^(docx|pdf|txt|md|xlsx|json|zip)$")
    layout: str = Field(default="complete_study_guide")
    include_source_appendix: bool = True
    target_total_marks: Optional[int] = None


def _serialize_item(it: StudyItem) -> Dict[str, Any]:
    return {
        "id": it.id,
        "project_id": it.project_id,
        "item_type": it.item_type,
        "chapter": it.chapter,
        "topic": it.topic,
        "marks": it.marks,
        "difficulty": it.difficulty,
        "language": it.language,
        "question_type": it.question_type,
        "prompt_text": it.prompt_text,
        "answer_text": it.answer_text,
        "explanation_text": it.explanation_text,
        "hint_text": it.hint_text,
        "scoring_points": it.scoring_points_json or [],
        "options": it.options_json or [],
        "study_support": it.study_support_json or {},
        "source_page": it.source_page,
        "source_ref": it.source_ref,
        "verification_status": it.verification_status,
        "is_locked": it.is_locked,
        "issue_note": it.issue_note,
        "order_index": it.order_index,
    }


def _serialize_project(proj: StudyProject, items: List[StudyItem]) -> Dict[str, Any]:
    return {
        "project_id": proj.id,
        "job_id": proj.job_id,
        "title": proj.title,
        "settings": proj.settings_json,
        "status": proj.status,
        "current_stage": proj.current_stage,
        "coverage_report": proj.coverage_report_json,
        "grammar_diffs": proj.grammar_diffs_json,
        "items": [_serialize_item(i) for i in items],
        "created_at": proj.created_at.isoformat() if proj.created_at else None,
    }


@router.get("/status")
def get_study_provider_status(_current_user: User = Depends(get_current_user)):
    """Report whether a real AI provider is configured for Study Studio."""
    return get_ai_provider_status()


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_study_project(
    payload: CreateStudyProjectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == payload.job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")
    if job.status != "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source job must be completed first")

    doc_model = _build_reviewed_document_model(job, db)
    settings_dict = payload.model_dump()
    try:
        result = run_study_pipeline(doc_model, settings_dict)
    except AIProviderUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_PROVIDER_UNCONFIGURED", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    proj = StudyProject(
        job_id=job.id,
        user_id=current_user.id,
        title=payload.title or f"Study Studio — {job.original_filename}",
        settings_json=settings_dict,
        status="completed",
        current_stage="ready",
        coverage_report_json=result["coverage_report"],
        grammar_diffs_json=result["grammar_diffs"],
    )
    db.add(proj)
    db.flush()

    db_items: List[StudyItem] = []
    for raw in result["items"]:
        si = StudyItem(
            project_id=proj.id,
            item_type=raw["item_type"],
            chapter=raw["chapter"],
            topic=raw["topic"],
            marks=raw["marks"],
            difficulty=raw["difficulty"],
            language=raw["language"],
            question_type=raw.get("question_type"),
            prompt_text=raw["prompt_text"],
            answer_text=raw["answer_text"],
            explanation_text=raw["explanation_text"],
            hint_text=raw["hint_text"],
            scoring_points_json=raw.get("scoring_points", []),
            options_json=raw.get("options", []),
            study_support_json=raw.get("study_support", {}),
            source_page=raw.get("source_page"),
            source_ref=raw.get("source_ref"),
            verification_status=raw.get("verification_status", "ai_draft"),
            is_locked=False,
            issue_note=raw.get("issue_note"),
            order_index=raw.get("order_index", 0),
        )
        db.add(si)
        db_items.append(si)

    db.commit()
    return _serialize_project(proj, db_items)


@router.get("/projects")
def list_study_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    projs = (
        db.query(StudyProject)
        .filter(StudyProject.user_id == current_user.id)
        .order_by(StudyProject.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "items": [
            {
                "project_id": p.id,
                "job_id": p.job_id,
                "title": p.title,
                "status": p.status,
                "coverage_report": p.coverage_report_json,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in projs
        ]
    }


@router.get("/projects/{project_id}")
def get_study_project(
    project_id: str,
    chapter: Optional[str] = None,
    marks: Optional[int] = None,
    difficulty: Optional[str] = None,
    verification_status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    proj = (
        db.query(StudyProject)
        .filter(StudyProject.id == project_id, StudyProject.user_id == current_user.id)
        .first()
    )
    if not proj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study project not found")

    q = db.query(StudyItem).filter(StudyItem.project_id == proj.id)
    if chapter:
        q = q.filter(StudyItem.chapter == chapter)
    if marks is not None:
        q = q.filter(StudyItem.marks == marks)
    if difficulty:
        q = q.filter(StudyItem.difficulty == difficulty)
    if verification_status:
        q = q.filter(StudyItem.verification_status == verification_status)

    items = q.order_by(StudyItem.order_index.asc()).all()
    return _serialize_project(proj, items)


@router.patch("/items/{item_id}")
def update_study_item(
    item_id: str,
    payload: UpdateStudyItemRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = (
        db.query(StudyItem)
        .join(StudyProject, StudyItem.project_id == StudyProject.id)
        .filter(StudyItem.id == item_id, StudyProject.user_id == current_user.id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study item not found")

    if payload.prompt_text is not None:
        item.prompt_text = payload.prompt_text
    if payload.answer_text is not None:
        item.answer_text = payload.answer_text
    if payload.explanation_text is not None:
        item.explanation_text = payload.explanation_text
    if payload.hint_text is not None:
        item.hint_text = payload.hint_text
    if payload.verification_status is not None:
        item.verification_status = payload.verification_status
    if payload.is_locked is not None:
        item.is_locked = payload.is_locked
    if payload.issue_note is not None:
        item.issue_note = payload.issue_note

    db.commit()
    db.refresh(item)
    return _serialize_item(item)


@router.post("/projects/{project_id}/regenerate")
def regenerate_unlocked_items(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Regenerate unlocked items while preserving all locked/approved items untouched."""
    proj = (
        db.query(StudyProject)
        .filter(StudyProject.id == project_id, StudyProject.user_id == current_user.id)
        .first()
    )
    if not proj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study project not found")
    job = db.query(Job).filter(Job.id == proj.job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")

    locked_items = (
        db.query(StudyItem)
        .filter(StudyItem.project_id == proj.id, StudyItem.is_locked.is_(True))
        .all()
    )
    locked_prompts = {it.prompt_text for it in locked_items}

    doc_model = _build_reviewed_document_model(job, db)
    try:
        result = run_study_pipeline(doc_model, proj.settings_json or {})
    except AIProviderUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_PROVIDER_UNCONFIGURED", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    db.query(StudyItem).filter(StudyItem.project_id == proj.id, StudyItem.is_locked.is_(False)).delete()
    proj.coverage_report_json = result["coverage_report"]

    next_idx = len(locked_items)
    for raw in result["items"]:
        if raw["prompt_text"] in locked_prompts:
            continue
        db.add(
            StudyItem(
                project_id=proj.id,
                item_type=raw["item_type"],
                chapter=raw["chapter"],
                topic=raw["topic"],
                marks=raw["marks"],
                difficulty=raw["difficulty"],
                language=raw["language"],
                question_type=raw.get("question_type"),
                prompt_text=raw["prompt_text"],
                answer_text=raw["answer_text"],
                explanation_text=raw["explanation_text"],
                hint_text=raw["hint_text"],
                scoring_points_json=raw.get("scoring_points", []),
                options_json=raw.get("options", []),
                study_support_json=raw.get("study_support", {}),
                source_page=raw.get("source_page"),
                source_ref=raw.get("source_ref"),
                verification_status=raw.get("verification_status", "auto_checked"),
                is_locked=False,
                order_index=next_idx,
            )
        )
        next_idx += 1

    db.commit()
    all_items = db.query(StudyItem).filter(StudyItem.project_id == proj.id).order_by(StudyItem.order_index.asc()).all()
    return _serialize_project(proj, all_items)


@router.post("/projects/{project_id}/export")
def export_study_project_endpoint(
    project_id: str,
    payload: ExportStudyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    proj = (
        db.query(StudyProject)
        .filter(StudyProject.id == project_id, StudyProject.user_id == current_user.id)
        .first()
    )
    if not proj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study project not found")

    items = db.query(StudyItem).filter(StudyItem.project_id == proj.id).order_by(StudyItem.order_index.asc()).all()
    item_dicts = [_serialize_item(i) for i in items]

    from app.services.study.exporter import export_study_project

    data_bytes, ext = export_study_project(
        proj.title,
        item_dicts,
        proj.coverage_report_json or {},
        payload.export_format,
        payload.layout,
        payload.include_source_appendix,
        payload.target_total_marks,
    )
    return Response(
        content=data_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="study_export.{ext}"'},
    )
