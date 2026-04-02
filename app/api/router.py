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
    CALL_LENS_SYSTEM,
    CALL_LENS_TEMPLATE,
    CALL_PREVIEW_SYSTEM,
    CALL_PREVIEW_TEMPLATE,
    CALL_SYNTHESIZE_SYSTEM,
    CALL_SYNTHESIZE_TEMPLATE,
)
from ats_poc.resume_parser import parse_resume_pdf
from ats_poc.sample_selection import (
    build_scored_resume_payload,
    compress_resume,
    extract_keywords,
    pick_representative_sample,
)

router = APIRouter(prefix="/api")

MODEL_NAME = "gemini-2.5-flash-lite"
PREVIEW_BATCH_SIZE = 2
LENS_BATCH_SIZE = 6


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

def _run_resume_enrichment(row: Session, resumes: list[Resume], db: DBSession) -> None:
    """Read each resume's full raw text through the JD lens and store the result.

    Idempotent — only processes resumes where resume_lens is None.
    Batched in groups of LENS_BATCH_SIZE to keep token payloads manageable.
    """
    un_lensed = [
        r for r in resumes
        if r.resume_lens is None and (r.quality or {}).get("readable")
    ]
    if not un_lensed:
        return

    jd = row.jd_analysis or {}
    # Pass gap_questions so the lens knows what JD ambiguities exist even if unanswered
    gap_context = jd.get("gap_questions", [])

    for i in range(0, len(un_lensed), LENS_BATCH_SIZE):
        batch = un_lensed[i : i + LENS_BATCH_SIZE]
        resume_inputs = [
            {"file_name": r.file_name, "raw_text": r.raw_text or ""}
            for r in batch
        ]
        parsed, _raw, usage, _prompt = run_structured_call(
            model_name=MODEL_NAME,
            system_instruction=CALL_LENS_SYSTEM,
            template=CALL_LENS_TEMPLATE,
            replacements={
                "ROLE_TYPE": jd.get("role_type", "unknown"),
                "ROLE_CONTEXT": jd.get("role_context", {}),
                "ONE_LINER": jd.get("one_liner", ""),
                "BASELINE_SIGNALS": jd.get("baseline_signals", []),
                "P0_SIGNALS": jd.get("p0_signals", []),
                "GAP_ANSWERS": gap_context,
                "RESUMES": resume_inputs,
            },
        )
        _accumulate_tokens(row, f"Lens batch {i // LENS_BATCH_SIZE + 1}", usage)

        lens_list = parsed if isinstance(parsed, list) else []
        lens_by_file = {item["file_name"]: item for item in lens_list if isinstance(item, dict)}
        for r in batch:
            lens = lens_by_file.get(r.file_name)
            if lens:
                r.resume_lens = lens
                db.add(r)

    db.flush()


def _pick_preview_batch(row: Session, db: DBSession) -> list[dict]:
    """Select PREVIEW_BATCH_SIZE readable resumes not yet shown."""
    all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
    readable = [
        {
            "file_name": r.file_name,
            "resume_json": r.resume_json,
            "resume_lens": r.resume_lens,
            "quality": r.quality,
        }
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


def _run_synthesis(row: Session, new_feedback: list[dict] | None = None) -> dict:
    """Synthesize base criteria + ALL accumulated params + ALL accumulated feedback.

    new_feedback is appended to the session's candidate_feedback_history before
    synthesis runs, so every call sees the complete feedback record to date.
    """
    if new_feedback:
        history = list(row.candidate_feedback_history or [])
        for fb in new_feedback:
            fb_with_iter = dict(fb)
            fb_with_iter["iteration"] = row.preview_iteration_count
            history.append(fb_with_iter)
        row.candidate_feedback_history = history

    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_SYNTHESIZE_SYSTEM,
        template=CALL_SYNTHESIZE_TEMPLATE,
        replacements={
            "BASE_CRITERIA_JSON": row.base_criteria or {},
            "EXTRA_PARAMS_HISTORY": row.extra_params_history or [],
            "CANDIDATE_FEEDBACK_JSON": row.candidate_feedback_history or [],
            "PREVIEW_RESULTS_JSON": row.preview_field_results or {},
        },
    )
    _accumulate_tokens(row, "Synthesis", usage)
    row.synthesized_config = parsed
    return parsed

def _run_preview_scoring(row: Session, batch: list[dict]) -> dict:
    """Run field-level scoring on the selected batch using lens + verifiable facts."""
    criteria = row.synthesized_config or row.base_criteria or {}
    required_fields = criteria.get("required_resume_fields", [])

    payloads = []
    for r in batch:
        payload = build_scored_resume_payload(
            r["resume_json"],
            r.get("resume_lens"),
            required_fields,
        )
        if not payload.get("name"):
            payload["name"] = r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        payloads.append(payload)

    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_PREVIEW_SYSTEM,
        template=CALL_PREVIEW_TEMPLATE,
        replacements={
            "CRITERIA_JSON": criteria,
            "RESUME_JSON_ARRAY": payloads,
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
        all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
        _run_resume_enrichment(row, all_resumes, db)
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

    # Enrich any resumes that haven't been lensed yet (e.g. never appeared in a preview batch)
    _run_resume_enrichment(row, readable, db)

    required_fields = config.get("required_resume_fields", []) if config else []
    resume_dicts = [
        {
            "file_name": r.file_name,
            "resume_json": r.resume_json,
            "resume_lens": r.resume_lens,
            "quality": r.quality,
        }
        for r in readable
    ]

    if len(resume_dicts) <= 15:
        batch = resume_dicts
    else:
        keywords = extract_keywords(row.jd_text or "", row.base_criteria or {}, config or {})
        batch = pick_representative_sample(resume_dicts, keywords, sample_size=15)

    # Build scored payloads — lens + verifiable facts; fall back to name from filename
    scored_payloads = []
    for r in batch:
        payload = build_scored_resume_payload(r["resume_json"], r.get("resume_lens"), required_fields)
        if not payload.get("name"):
            payload["name"] = r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        scored_payloads.append(payload)

    rubric = config.get("scoring_rubric", {}) if config else {}
    prompt_text = config.get("final_evaluation_prompt", "") if config else ""

    parsed, _raw, usage, _prompt = run_structured_call(
        model_name=MODEL_NAME,
        system_instruction=CALL_3_SYSTEM,
        template=CALL_3_TEMPLATE,
        replacements={
            "FINAL_EVALUATION_PROMPT": prompt_text,
            "SCORING_RUBRIC_JSON": rubric,
            "ARRAY_OF_15_COMPRESSED_RESUMES": scored_payloads,
        },
    )
    _accumulate_tokens(row, "Call 3", usage)
    print(f"Call 3 success. Result keys: {parsed.keys() if isinstance(parsed, dict) else 'not a dict'}")
    row.full_results = parsed
    row.status = "completed"

    db.commit()
    db.refresh(row)
    return row
