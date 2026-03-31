from __future__ import annotations

import copy
import os
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import PreviewIteration, Resume, Session
from app.schemas import (
    AcceptRequest,
    AnalyzeJDRequest,
    RefineRequest,
    ResumeOut,
    SessionOut,
    StartPreviewRequest,
)
from ats_poc.gemini_client import configure_genai, run_structured_call
from ats_poc.prompts import (
    CALL_1_SYSTEM,
    CALL_1_TEMPLATE,
    CALL_2_SYSTEM,
    CALL_2_TEMPLATE,
    CALL_3_SYSTEM,
    CALL_3_TEMPLATE,
    CALL_PREVIEW_SYSTEM,
    CALL_PREVIEW_TEMPLATE,
    CALL_SYNTHESIZE_SYSTEM,
    CALL_SYNTHESIZE_TEMPLATE,
)
from ats_poc.resume_parser import parse_resume_pdf
from ats_poc.sample_selection import (
    compress_resume,
    extract_keywords,
    pick_representative_sample,
)

router = APIRouter(prefix="/api")

MODEL_NAME = "gemini-2.5-flash-lite"
PREVIEW_BATCH_SIZE = 2


# ── helpers ────────────────────────────────────────────────────────────────

def _resolve_key(request_key: Optional[str]) -> str:
    key = request_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(status_code=400, detail="Gemini API key required.")
    return key


def _accumulate_tokens(session: Session, label: str, usage: dict[str, Any]) -> None:
    totals = dict(session.token_totals or {})
    totals[label] = usage
    totals.setdefault("_total_tokens", 0)
    totals["_total_tokens"] += usage.get("total_tokens", 0)
    session.token_totals = totals


def _get_session_or_404(session_id: str, db: DBSession) -> Session:
    row = db.query(Session).filter(Session.id == session_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    return row


# ── session CRUD ───────────────────────────────────────────────────────────

@router.get("/sessions", response_model=List[SessionOut])
def list_sessions(db: DBSession = Depends(get_db)):
    return db.query(Session).order_by(Session.created_at.desc()).limit(50).all()


@router.post("/sessions", response_model=SessionOut)
def create_session(db: DBSession = Depends(get_db)):
    row = Session()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, db: DBSession = Depends(get_db)):
    return _get_session_or_404(session_id, db)


# ── step 1: JD analysis ────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/jd", response_model=SessionOut)
def analyze_jd(session_id: str, body: AnalyzeJDRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_1_SYSTEM,
        template=CALL_1_TEMPLATE,
        replacements={"JD_TEXT": body.jd_text},
    )
    
    quality_score = parsed.get("jd_quality_score", 0)
    if quality_score < 6:
        raise HTTPException(
            status_code=400,
            detail=f"Job Description quality score is low ({quality_score}/10). "
                   "It may be too vague to extract reliable screening criteria. "
                   "Please provide a more detailed description."
        )

    row.jd_text = body.jd_text
    row.jd_analysis = parsed
    row.base_criteria = copy.deepcopy(parsed)
    row.status = "jd_analyzed"
    _accumulate_tokens(row, "Call 1", usage)

    db.commit()
    db.refresh(row)
    return row


# ── step 2: resume upload + parsing ───────────────────────────────────────

@router.post("/sessions/{session_id}/resumes", response_model=SessionOut)
async def upload_resumes(
    session_id: str,
    files: List[UploadFile] = File(...),
    api_key: Optional[str] = Form(None),
    db: DBSession = Depends(get_db),
):
    row = _get_session_or_404(session_id, db)

    # delete previous resumes for this session
    db.query(Resume).filter(Resume.session_id == session_id).delete()

    for f in files:
        pdf_bytes = await f.read()
        try:
            parsed = parse_resume_pdf(f.filename, pdf_bytes)
        except Exception as exc:
            parsed = {
                "file_name": f.filename,
                "raw_text": "",
                "resume_json": {},
                "quality": {"readable": False, "score": 0, "reasons": [str(exc)]},
            }

        resume_row = Resume(
            session_id=session_id,
            file_name=f.filename,
            resume_json=parsed.get("resume_json", {}),
            raw_text=parsed.get("raw_text", ""),
            quality=parsed.get("quality", {}),
        )
        db.add(resume_row)

    row.status = "resumes_uploaded"
    row.preview_seen_files = []
    db.commit()
    db.refresh(row)
    return row


@router.get("/sessions/{session_id}/resumes", response_model=List[ResumeOut])
def list_resumes(session_id: str, db: DBSession = Depends(get_db)):
    return db.query(Resume).filter(Resume.session_id == session_id).all()


# ── step 3: preview loop ───────────────────────────────────────────────────

def _pick_preview_batch(row: Session, db: DBSession) -> list[dict]:
    """Select PREVIEW_BATCH_SIZE readable resumes not yet shown."""
    all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
    readable = [
        {"file_name": r.file_name, "resume_json": r.resume_json, "quality": r.quality}
        for r in all_resumes
        if (r.quality or {}).get("readable")
    ]
    seen: set[str] = set(row.preview_seen_files or [])
    unseen = [r for r in readable if r["file_name"] not in seen]

    if not unseen:
        seen = set()
        unseen = readable

    config = row.synthesized_config or row.base_criteria or {}
    keywords = extract_keywords(row.jd_text or "", row.base_criteria or {}, config)

    if len(unseen) <= PREVIEW_BATCH_SIZE:
        batch = unseen
    else:
        batch = pick_representative_sample(unseen, keywords, sample_size=PREVIEW_BATCH_SIZE)

    row.preview_seen_files = list(seen | {r["file_name"] for r in batch})
    return batch


def _run_silent_call2(row: Session) -> dict:
    """Run Call 2 with no edits to get initial scoring config."""
    criteria = row.base_criteria or {}
    manual_payload = {"original_requirements": criteria, "edited_requirements": criteria, "notes": ""}
    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_2_SYSTEM,
        template=CALL_2_TEMPLATE,
        replacements={
            "CALL_1_JSON_OUTPUT": criteria,
            "ROHAN_GAP_ANSWERS": {},
            "ROHAN_EDITS": manual_payload,
        },
    )
    _accumulate_tokens(row, "Preview Call 2", usage)
    row.synthesized_config = parsed
    return parsed


def _run_synthesis(row: Session, candidate_feedback: list[dict] = None) -> dict:
    """Synthesize base criteria + all extra params + human feedback into updated config."""
    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_SYNTHESIZE_SYSTEM,
        template=CALL_SYNTHESIZE_TEMPLATE,
        replacements={
            "BASE_CRITERIA_JSON": row.base_criteria or {},
            "EXTRA_PARAMS_HISTORY": row.extra_params_history or [],
            "CANDIDATE_FEEDBACK_JSON": candidate_feedback or [],
            "PREVIEW_RESULTS_JSON": row.preview_field_results or {},
        },
    )
    _accumulate_tokens(row, "Synthesis", usage)
    row.synthesized_config = parsed
    return parsed

def _run_preview_scoring(row: Session, batch: list[dict]) -> dict:
    """Run field-level scoring on the selected batch with JD-aware compression."""
    criteria = row.synthesized_config or row.base_criteria or {}
    required_fields = criteria.get("required_resume_fields", [])
    
    resume_jsons = []
    for r in batch:
        rj = compress_resume(r["resume_json"], required_fields)
        if not rj.get("name"):
            rj["name"] = r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        resume_jsons.append(rj)
    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_PREVIEW_SYSTEM,
        template=CALL_PREVIEW_TEMPLATE,
        replacements={
            "CRITERIA_JSON": criteria,
            "RESUME_JSON_ARRAY": resume_jsons,
        },
    )
    _accumulate_tokens(row, "Preview Score", usage)
    row.preview_field_results = parsed
    return parsed


@router.post("/sessions/{session_id}/preview", response_model=SessionOut)
def start_preview(session_id: str, body: StartPreviewRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    if row.synthesized_config is None:
        _run_silent_call2(row)

    batch = _pick_preview_batch(row, db)
    if not batch:
        raise HTTPException(status_code=400, detail="No readable resumes found in this session.")

    results = _run_preview_scoring(row, batch)
    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1
    row.status = "preview_active"

    db.add(PreviewIteration(
        session_id=session_id,
        iteration_number=row.preview_iteration_count,
        extra_params={},
        field_results=results,
        synthesized_config_snapshot=row.synthesized_config,
    ))
    db.commit()
    db.refresh(row)
    return row


@router.post("/sessions/{session_id}/preview/refine", response_model=SessionOut)
def refine_preview(session_id: str, body: RefineRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    if body.include.strip() or body.exclude.strip():
        history = list(row.extra_params_history or [])
        history.append({
            "iteration": row.preview_iteration_count,
            "include": body.include.strip(),
            "exclude": body.exclude.strip(),
        })
        row.extra_params_history = history

    feedback_dicts = [fb.model_dump() for fb in body.candidate_feedback] if body.candidate_feedback else []
    _run_synthesis(row, feedback_dicts)
    batch = _pick_preview_batch(row, db)
    if not batch:
        raise HTTPException(status_code=400, detail="No readable resumes found.")

    results = _run_preview_scoring(row, batch)
    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1

    db.add(PreviewIteration(
        session_id=session_id,
        iteration_number=row.preview_iteration_count,
        extra_params={"include": body.include, "exclude": body.exclude},
        field_results=results,
        synthesized_config_snapshot=row.synthesized_config,
    ))
    db.commit()
    db.refresh(row)
    return row


@router.post("/sessions/{session_id}/preview/reload", response_model=SessionOut)
def reload_preview(session_id: str, body: StartPreviewRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    batch = _pick_preview_batch(row, db)
    if not batch:
        raise HTTPException(status_code=400, detail="No more readable resumes available to load.")

    results = _run_preview_scoring(row, batch)
    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1

    db.add(PreviewIteration(
        session_id=session_id,
        iteration_number=row.preview_iteration_count,
        extra_params={"action": "reloaded_resumes"},
        field_results=results,
        synthesized_config_snapshot=row.synthesized_config or row.base_criteria,
    ))
    db.commit()
    db.refresh(row)
    return row


# ── step 4: accept + full evaluation ──────────────────────────────────────

@router.post("/sessions/{session_id}/accept", response_model=SessionOut)
def accept_and_run_full(session_id: str, body: AcceptRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    config = row.synthesized_config or row.base_criteria
    row.final_config = config

    all_resumes = db.query(Resume).filter(Resume.session_id == session_id).all()
    readable = [r for r in all_resumes if (r.quality or {}).get("readable")]

    if not readable:
        raise HTTPException(status_code=400, detail="No readable resumes to assess.")

    required_fields = config.get("required_resume_fields", []) if config else []
    resume_dicts = [{"file_name": r.file_name, "resume_json": r.resume_json, "quality": r.quality} for r in readable]

    if len(resume_dicts) <= 15:
        batch = resume_dicts
    else:
        keywords = extract_keywords(row.jd_text or "", row.base_criteria or {}, config or {})
        batch = pick_representative_sample(resume_dicts, keywords, sample_size=15)

    # Always include name so Gemini can identify candidates; fall back to filename
    compressed = []
    for r in batch:
        c = compress_resume(r["resume_json"], required_fields)
        parsed_name = r["resume_json"].get("name", "")
        if not parsed_name:
            parsed_name = r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        c["name"] = parsed_name
        compressed.append(c)
    rubric = config.get("scoring_rubric", {}) if config else {}
    prompt_text = config.get("final_evaluation_prompt", "") if config else ""

    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_3_SYSTEM,
        template=CALL_3_TEMPLATE,
        replacements={
            "FINAL_EVALUATION_PROMPT": prompt_text,
            "SCORING_RUBRIC_JSON": rubric,
            "ARRAY_OF_15_COMPRESSED_RESUMES": compressed,
        },
    )
    _accumulate_tokens(row, "Call 3", usage)
    print(f"Call 3 success. Result keys: {parsed.keys() if isinstance(parsed, dict) else 'not a dict'}")
    row.full_results = parsed
    row.status = "completed"

    db.commit()
    db.refresh(row)
    return row
