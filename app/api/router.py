from __future__ import annotations

import copy
import os
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import PreviewIteration, Resume, Session
from app.schemas import (
    AcceptRequest,
    AnalyzeJDRequest,
    RefineRequest,
    ResumeDetailOut,
    ResumeOut,
    SessionOut,
    StartPreviewRequest,
)
from ats_poc.gemini_client import GeminiUnavailableError, configure_genai, run_structured_call
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
# Maximum resumes sent to CALL_3 (full evaluation).
# Set high enough that typical uploads are never silently truncated.
# Token budget: ~800 tokens/resume × 50 = 40K candidate tokens, well within context.
FULL_EVAL_BATCH_SIZE = 50
# Number of resumes per CALL_3 sub-batch.
# Gemini silently drops candidates when given too many at once.
# 8 per call keeps the payload small enough that every resume comes back.
CALL_3_BATCH_SIZE = 8


# ── helpers ────────────────────────────────────────────────────────────────

_ALLOWED_FEEDBACK_ACTIONS = {"approve", "reject", "disagree", "strong_yes", "strong_no", "unclear"}
_RESULT_CLASSIFICATIONS = {"P0", "Baseline", "Reject"}

def _resolve_key(request_key: Optional[str]) -> str:
    key = request_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(status_code=400, detail="Gemini API key required.")
    return key


def _raise_gemini_error(exc: GeminiUnavailableError) -> None:
    """Translate a transient Gemini error into a proper HTTP response."""
    http_code = 503 if exc.status_code == 503 else 429
    detail = (
        "The Gemini model is temporarily overloaded. Please wait a few seconds and try again."
        if exc.status_code == 503
        else "Gemini API rate limit reached. Please wait before retrying."
    )
    raise HTTPException(status_code=http_code, detail=detail)


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


def _reset_session_evaluation_state(row: Session) -> None:
    """Clear any derived rubric/scoring state that depends on the current resume set."""
    row.synthesized_config = None
    row.final_config = None
    row.status = "resumes_uploaded"
    row.preview_iteration_count = 0
    row.extra_params_history = []
    row.candidate_feedback_history = []
    row.preview_field_results = None
    row.preview_seen_files = []
    row.full_results = None
    row.token_totals = {}


def _normalize_feedback_action(action: str) -> str:
    normalized = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    return normalized if normalized in _ALLOWED_FEEDBACK_ACTIONS else "disagree"


def _find_candidate_result(results: dict[str, Any] | None, file_name: str) -> dict[str, Any] | None:
    if not isinstance(results, dict):
        return None
    target_file = (file_name or "").strip().lower()
    stem = target_file.replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
    for item in results.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        candidate_name = (item.get("candidate_name") or "").strip().lower()
        if candidate_name in {target_file, stem}:
            return item
    return None


def _build_feedback_context(row: Session, db: DBSession, feedback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not feedback:
        return []

    resumes = {
        r.file_name: r
        for r in db.query(Resume).filter(Resume.session_id == str(row.id)).all()
    }
    enriched_feedback: list[dict[str, Any]] = []
    for item in feedback:
        file_name = item.get("file_name", "")
        resume = resumes.get(file_name)
        preview_result = _find_candidate_result(row.preview_field_results, file_name)
        full_result = _find_candidate_result(row.full_results, file_name)
        enriched_feedback.append({
            "file_name": file_name,
            "action": _normalize_feedback_action(item.get("action", "")),
            "reason": (item.get("reason") or "").strip(),
            "current_preview_result": preview_result,
            "current_full_result": full_result,
            "resume_snapshot": {
                "name": ((resume.resume_json or {}).get("name") if resume else "") or "",
                "quality": (resume.quality if resume else {}) or {},
                "resume_json": (resume.resume_json if resume else {}) or {},
                "resume_lens": (resume.resume_lens if resume else None),
            },
        })
    return enriched_feedback


def _normalize_result_item(
    item: Any,
    *,
    fallback_name: str,
    include_field_matches: bool,
    include_extra_param_matches: bool,
    include_confidence: bool,
) -> dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    candidate_name = (source.get("candidate_name") or fallback_name).strip() or fallback_name
    baseline_pass = bool(source.get("baseline_pass", False))
    p0_score = source.get("p0_score", 0)
    try:
        p0_score = int(round(float(p0_score)))
    except Exception:
        p0_score = 0
    p0_score = max(0, min(100, p0_score))

    overall_score = source.get("overall_score", 0)
    try:
        overall_score = int(round(float(overall_score)))
    except Exception:
        overall_score = 0
    overall_score = max(0, min(100, overall_score))

    classification = str(source.get("classification") or "").strip()
    if classification not in _RESULT_CLASSIFICATIONS:
        if not baseline_pass:
            classification = "Reject"
        elif p0_score >= 75:
            classification = "P0"
        else:
            classification = "Baseline"

    normalized = {
        "candidate_name": candidate_name,
        "baseline_pass": baseline_pass,
        "baseline_failures": [str(x) for x in source.get("baseline_failures", []) if str(x).strip()],
        "p0_score": p0_score,
        "p0_matches": [str(x) for x in source.get("p0_matches", []) if str(x).strip()],
        "overall_score": overall_score,
        "classification": classification,
        "reasoning": (source.get("reasoning") or "No reasoning returned by model.").strip(),
    }

    if include_field_matches:
        field_matches = source.get("field_matches")
        normalized["field_matches"] = field_matches if isinstance(field_matches, list) else []
    if include_extra_param_matches:
        extra_param_matches = source.get("extra_param_matches")
        normalized["extra_param_matches"] = (
            [str(x) for x in extra_param_matches if str(x).strip()]
            if isinstance(extra_param_matches, list)
            else []
        )
    if include_confidence:
        confidence = str(source.get("confidence") or "low").strip().lower()
        normalized["confidence"] = confidence if confidence in {"high", "medium", "low"} else "low"
        red_flags_found = source.get("red_flags_found")
        normalized["red_flags_found"] = (
            [str(x) for x in red_flags_found if str(x).strip()]
            if isinstance(red_flags_found, list)
            else []
        )

    return normalized


def _summarize_results(results: list[dict[str, Any]], include_low_confidence: bool) -> dict[str, int]:
    summary = {
        "total_evaluated": len(results),
        "p0_count": sum(1 for r in results if r.get("classification") == "P0"),
        "baseline_count": sum(1 for r in results if r.get("classification") == "Baseline"),
        "reject_count": sum(1 for r in results if r.get("classification") == "Reject"),
    }
    if include_low_confidence:
        summary["low_confidence_count"] = sum(1 for r in results if r.get("confidence") == "low")
    return summary


def _normalize_scoring_output(
    parsed: Any,
    *,
    expected_names: list[str],
    include_field_matches: bool,
    include_extra_param_matches: bool,
    include_confidence: bool,
) -> dict[str, Any]:
    source = parsed if isinstance(parsed, dict) else {}
    raw_results = source.get("results")
    raw_results = raw_results if isinstance(raw_results, list) else []
    by_name = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        candidate_name = (item.get("candidate_name") or "").strip().lower()
        if candidate_name:
            by_name[candidate_name] = item

    normalized_results: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for expected_name in expected_names:
        match = by_name.get(expected_name.strip().lower())
        if match is None:
            missing.append({
                "candidate_name": expected_name,
                "reason": "Candidate was sent to the model but missing from the structured response.",
            })
            continue
        normalized_results.append(
            _normalize_result_item(
                match,
                fallback_name=expected_name,
                include_field_matches=include_field_matches,
                include_extra_param_matches=include_extra_param_matches,
                include_confidence=include_confidence,
            )
        )

    normalized_results.sort(key=lambda r: r.get("overall_score", 0), reverse=True)
    normalized = {
        "results": normalized_results,
        "summary": _summarize_results(normalized_results, include_low_confidence=include_confidence),
    }
    if missing:
        normalized["_skipped"] = {"model_dropped": missing}
    return normalized


def _normalize_synthesized_config(parsed: Any) -> dict[str, Any]:
    source = parsed if isinstance(parsed, dict) else {}
    rubric = source.get("scoring_rubric")
    rubric = rubric if isinstance(rubric, dict) else {}

    required_fields = source.get("required_resume_fields")
    if not isinstance(required_fields, list):
        required_fields = []
    normalized_fields = []
    for field in required_fields:
        value = str(field).strip()
        if value and value not in normalized_fields:
            normalized_fields.append(value)
    if "name" not in normalized_fields:
        normalized_fields.insert(0, "name")

    baseline_checks = []
    for item in rubric.get("baseline_checks", []) or []:
        if not isinstance(item, dict):
            continue
        check = _coerce_str(item.get("check"))
        resume_field = str(item.get("resume_field") or "").strip() or "work_experience"
        if not check:
            continue
        baseline_checks.append({
            "check": check,
            "resume_field": resume_field,
            "reject_if_missing": bool(item.get("reject_if_missing", True)),
        })
        if resume_field not in normalized_fields:
            normalized_fields.append(resume_field)

    p0_weights = []
    for item in rubric.get("p0_weights", []) or []:
        if not isinstance(item, dict):
            continue
        signal = _coerce_str(item.get("signal"))
        resume_field = str(item.get("resume_field") or "").strip() or "work_experience"
        if not signal:
            continue
        weight = item.get("weight", 0)
        try:
            weight = int(round(float(weight)))
        except Exception:
            weight = 0
        p0_weights.append({
            "signal": signal,
            "weight": max(0, weight),
            "resume_field": resume_field,
        })
        if resume_field not in normalized_fields:
            normalized_fields.append(resume_field)

    total_weight = sum(item["weight"] for item in p0_weights)
    if p0_weights and total_weight != 100:
        delta = 100 - total_weight
        p0_weights[0]["weight"] = max(0, p0_weights[0]["weight"] + delta)

    red_flag_checks = []
    for item in rubric.get("red_flag_checks", []) or []:
        if not isinstance(item, dict):
            continue
        check = _coerce_str(item.get("check"))
        resume_field = str(item.get("resume_field") or "").strip() or "work_experience"
        if not check:
            continue
        red_flag_checks.append({
            "check": check,
            "resume_field": resume_field,
            "deprioritize_if_present": bool(item.get("deprioritize_if_present", True)),
        })
        if resume_field not in normalized_fields:
            normalized_fields.append(resume_field)

    lessons = []
    for item in source.get("lessons_learned", []) or []:
        if not isinstance(item, dict):
            continue
        lessons.append({
            "candidate": str(item.get("candidate") or "").strip(),
            "action": _normalize_feedback_action(str(item.get("action") or "")),
            "reason_given": str(item.get("reason_given") or "").strip(),
            "lesson": str(item.get("lesson") or "").strip(),
            "rubric_change": str(item.get("rubric_change") or "").strip(),
        })

    recruiter_profile = source.get("recruiter_profile")
    if not isinstance(recruiter_profile, dict):
        recruiter_profile = {}

    return {
        "final_evaluation_prompt": str(source.get("final_evaluation_prompt") or "").strip(),
        "required_resume_fields": normalized_fields,
        "scoring_rubric": {
            "baseline_checks": baseline_checks,
            "p0_weights": p0_weights,
            "red_flag_checks": red_flag_checks,
        },
        "screening_summary": str(source.get("screening_summary") or "").strip(),
        "synthesis_notes": str(source.get("synthesis_notes") or "").strip(),
        "lessons_learned": lessons,
        "recruiter_profile": recruiter_profile,
    }


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

    try:
        parsed, _raw, usage, _prompt = run_structured_call(
            model_name=MODEL_NAME,
            system_instruction=CALL_1_SYSTEM,
            template=CALL_1_TEMPLATE,
            replacements={"JD_TEXT": body.jd_text},
        )
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

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
    _reset_session_evaluation_state(row)

    for f in files:
        pdf_bytes = await f.read()
        try:
            parsed = parse_resume_pdf(f.filename, pdf_bytes)
        except Exception as exc:
            parsed = {
                "file_name": f.filename,
                "raw_text": "",
                "resume_json": {},
                "quality": {"text_extractable": False, "readable": False, "score": 0, "reasons": [str(exc)]},
            }

        resume_row = Resume(
            session_id=session_id,
            file_name=f.filename,
            resume_json=parsed.get("resume_json", {}),
            raw_text=parsed.get("raw_text", ""),
            quality=parsed.get("quality", {}),
            pdf_bytes=pdf_bytes,
        )
        db.add(resume_row)

    db.commit()
    db.refresh(row)
    return row


@router.get("/sessions/{session_id}/resumes", response_model=List[ResumeOut])
def list_resumes(session_id: str, db: DBSession = Depends(get_db)):
    return db.query(Resume).filter(Resume.session_id == session_id).all()


@router.get("/sessions/{session_id}/resumes/{file_name:path}/pdf")
def get_resume_pdf(session_id: str, file_name: str, db: DBSession = Depends(get_db)):
    resume = (
        db.query(Resume)
        .filter(Resume.session_id == session_id, Resume.file_name == file_name)
        .first()
    )
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not resume.pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF not stored for this resume")
    return Response(
        content=resume.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{file_name}"'},
    )


@router.get("/sessions/{session_id}/resumes/{file_name:path}", response_model=ResumeDetailOut)
def get_resume_detail(session_id: str, file_name: str, db: DBSession = Depends(get_db)):
    resume = (
        db.query(Resume)
        .filter(Resume.session_id == session_id, Resume.file_name == file_name)
        .first()
    )
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


# ── step 3: preview loop ───────────────────────────────────────────────────

_LENS_REQUIRED_FIELDS = {
    "ownership_arc", "domain_proximity", "craft_signals",
    "experience_profile", "trajectory", "hm_flag", "red_flag_notes",
}
# Fields where the model is explicitly told to write "None" when there is nothing to report.
# The lens prompt instructs: red_flag_notes → "Write 'None' if clean."
# hm_flag → "If no concern, say so." (produces "No obvious flag" or "None")
# Treating these as filler would discard every clean-looking resume.
_LENS_NULLABLE_FIELDS = {"red_flag_notes", "hm_flag"}
_LENS_FILLER_VALUES = {
    "not applicable", "n/a", "", "no information",
    "not available", "not provided",
}
_LENS_MIN_CHARS = 30


def _validate_lens(lens: dict) -> bool:
    """Return True only if every required lens field is populated with non-filler content.

    Fields in _LENS_NULLABLE_FIELDS are allowed to contain "none", "no flag", or similar
    explicit-absence phrases — the lens prompt tells the model to write those when there
    is genuinely nothing to report.
    """
    if not isinstance(lens, dict):
        return False
    for field in _LENS_REQUIRED_FIELDS:
        val = lens.get(field, "")
        if not isinstance(val, str):
            return False
        stripped = val.strip().lower()
        # Universally invalid: empty or generic non-answer
        if stripped in _LENS_FILLER_VALUES:
            return False
        # Nullable fields: "none", "no flag", "no concern", "no obvious flag" are valid
        if field in _LENS_NULLABLE_FIELDS:
            continue
        # Substantive fields: must have real content, not just "none" or a very short string
        if stripped == "none" or len(stripped) < _LENS_MIN_CHARS:
            return False
    return True


def _run_resume_enrichment(row: Session, resumes: list[Resume], db: DBSession) -> None:
    """Read each resume's full raw text through the JD lens and store the result.

    Idempotent — only processes resumes where resume_lens is None.
    Batched in groups of LENS_BATCH_SIZE to keep token payloads manageable.
    """
    un_lensed = [
        r for r in resumes
        if r.resume_lens is None and (r.quality or {}).get("text_extractable", (r.quality or {}).get("readable"))
    ]
    if not un_lensed:
        return

    jd = row.jd_analysis or {}
    # Pass gap_questions so the lens knows what JD ambiguities exist even if unanswered
    gap_context = jd.get("gap_questions", [])

    for i in range(0, len(un_lensed), LENS_BATCH_SIZE):
        batch = un_lensed[i : i + LENS_BATCH_SIZE]
        # Wrap each resume in sentinel delimiters so the model cannot blend candidates.
        resume_blocks = [
            f"=== CANDIDATE START: {r.file_name} ===\n{r.raw_text or ''}\n=== CANDIDATE END: {r.file_name} ==="
            for r in batch
        ]
        resume_inputs_str = "\n\n".join(resume_blocks)

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
                "RESUMES": resume_inputs_str,
            },
        )
        _accumulate_tokens(row, f"Lens {i + 1}", usage)

        lens_list = parsed if isinstance(parsed, list) else []

        # Warn if Gemini returned fewer entries than we sent — those resumes fall back to compress_resume.
        returned_names = {
            item.get("file_name", "") for item in lens_list if isinstance(item, dict)
        }
        dropped = [r.file_name for r in batch if r.file_name not in returned_names]
        if dropped:
            print(
                f"[LENS DROP WARNING] Sent {len(batch)} resume(s), received {len(lens_list)}. "
                f"Dropped (will use compress_resume fallback): {dropped}"
            )

        lens_by_file = {item["file_name"]: item for item in lens_list if isinstance(item, dict)}
        for r in batch:
            lens = lens_by_file.get(r.file_name)
            if lens:
                if _validate_lens(lens):
                    r.resume_lens = lens
                    print(f"[LENS SUCCESS] {r.file_name} — lens validated and stored")
                else:
                    print(
                        f"[LENS VALIDATION FAILED] {r.file_name} — one or more fields are empty "
                        f"or filler. Storing None; scoring will fall back to compress_resume()."
                    )
                    r.resume_lens = None
            else:
                print(f"[LENS DROP] {r.file_name} — not returned by model. Scoring will fall back to compress_resume().")
                r.resume_lens = None
            db.add(r)

    db.flush()


def _get_current_batch(row: Session, db: DBSession) -> list[dict]:
    """Return the resumes that are currently displayed in preview_field_results.

    Used after a prompt upgrade so the recruiter sees how the SAME candidates
    are re-scored under the new rubric, rather than seeing a new unseen set.
    """
    prev = row.preview_field_results
    if not prev or not prev.get("results"):
        return []

    # Collect every candidate_name that was in the last preview
    current_names: set[str] = {
        r.get("candidate_name", "").lower()
        for r in prev["results"]
        if isinstance(r, dict)
    }
    if not current_names:
        return []

    all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
    batch = []
    for r in all_resumes:
        resume_name = ((r.resume_json or {}).get("name") or "").lower()
        stem_name = r.file_name.replace(".pdf", "").replace("_", " ").replace("-", " ").strip().lower()
        if resume_name in current_names or stem_name in current_names or r.file_name.lower() in current_names:
            batch.append({
                "file_name": r.file_name,
                "resume_json": r.resume_json,
                "resume_lens": r.resume_lens,
                "quality": r.quality,
            })
    return batch


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
        if (r.quality or {}).get("text_extractable", (r.quality or {}).get("readable"))
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
    normalized = _normalize_synthesized_config(parsed)
    _accumulate_tokens(row, "Preview Call 2", usage)
    _validate_synthesized_config(normalized, label="CALL_2")
    row.synthesized_config = normalized
    return normalized


def _validate_synthesized_config(parsed: dict, label: str) -> None:
    """Log warnings when synthesis output is structurally broken.

    Does NOT raise — a broken rubric is better than a hard failure.
    Warnings surface in backend logs so issues are visible without crashing.
    """
    rubric = parsed.get("scoring_rubric") or {}

    baseline_checks = rubric.get("baseline_checks") or []
    if not baseline_checks:
        print(
            f"[SYNTHESIS WARNING] {label}: scoring_rubric.baseline_checks is empty. "
            "Preview scoring will fall back to original JD baseline_signals."
        )

    p0_weights = rubric.get("p0_weights") or []
    if p0_weights:
        total = sum(
            w.get("weight", 0) for w in p0_weights
            if isinstance(w, dict) and isinstance(w.get("weight"), (int, float))
        )
        if total < 90 or total > 110:
            print(
                f"[SYNTHESIS WARNING] {label}: p0_weights sum to {total} (expected ~100). "
                "Scoring calibration may be off."
            )

    if not parsed.get("final_evaluation_prompt"):
        print(
            f"[SYNTHESIS WARNING] {label}: final_evaluation_prompt is empty. "
            "CALL_3 (full eval) will have no screening instructions."
        )

    lessons = parsed.get("lessons_learned") or []
    print(
        f"[SYNTHESIS OK] {label}: {len(baseline_checks)} baseline_checks, "
        f"{len(p0_weights)} p0_weights, {len(lessons)} lessons_learned"
    )


def _run_synthesis(row: Session, db: DBSession, new_feedback: list[dict] | None = None) -> dict:
    """Synthesize base criteria + ALL accumulated params + ALL accumulated feedback.

    new_feedback is appended to the session's candidate_feedback_history before
    synthesis runs, so every call sees the complete feedback record to date.
    """
    if new_feedback:
        history = list(row.candidate_feedback_history or [])
        enriched_feedback = _build_feedback_context(row, db, new_feedback)
        for fb in enriched_feedback:
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
    normalized = _normalize_synthesized_config(parsed)
    _accumulate_tokens(row, "Synthesis", usage)
    _validate_synthesized_config(normalized, label="CALL_SYNTHESIZE")
    row.synthesized_config = normalized
    row.final_config = None
    row.full_results = None
    return normalized

def _coerce_str(val: Any) -> str:
    """Safely coerce a value to a plain string.

    Gemini occasionally returns structured objects instead of plain strings
    for signal/check fields (e.g. {"signal": "...", "evidence_fields": [...]}).
    This function extracts the string content from any such object, so the
    downstream scoring model always receives a clean text string.
    """
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        # Try common field names in order of preference
        for key in ("signal", "check", "text", "description", "value", "label"):
            candidate = val.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        # Last resort: stringify the whole dict (avoids silent empty strings)
        return str(val)
    if val is None:
        return ""
    return str(val)


def _build_scoring_criteria(row: Session) -> dict:
    """Return the criteria dict used for preview scoring.

    CALL_PREVIEW_TEMPLATE iterates over top-level 'baseline_signals' (hard
    filters) and 'p0_signals' (preferences) when evaluating candidates.

    After synthesis the updated checks live in scoring_rubric.baseline_checks /
    p0_weights. This function promotes them back to the top-level arrays that
    the scoring template reads, with a critical rule:

    HARD FILTER PROMOTION:
      Only baseline_checks with reject_if_missing: TRUE are promoted to
      baseline_signals. A check with reject_if_missing: false means the
      recruiter explicitly relaxed it (via an include parameter). Promoting
      it to baseline_signals would still apply it as a hard filter because
      the scoring model reads those as rejection criteria. Instead, relaxed
      checks are promoted to p0_signals — they become preferences, not gates.

    This ensures that after "include candidates without consumer experience",
    the consumer check text (which still contains old FAILS IF language) does
    NOT reach the scoring model as a hard filter, even if synthesis forgot to
    rewrite the check text.

    _coerce_str() is applied to guard against Gemini returning structured
    objects instead of plain strings.
    """
    base = dict(row.synthesized_config or row.base_criteria or {})
    rubric = base.get("scoring_rubric") or {}

    baseline_checks = rubric.get("baseline_checks") or []
    p0_weights = rubric.get("p0_weights") or []

    hard_signals: list[str] = []   # reject_if_missing: true  → hard filters
    soft_signals: list[str] = []   # reject_if_missing: false → preferences

    for c in baseline_checks:
        if not isinstance(c, dict):
            continue
        check_text = _coerce_str(c.get("check"))
        if not check_text:
            continue
        # Treat missing reject_if_missing as True (safer default)
        is_hard = c.get("reject_if_missing", True)
        if is_hard:
            hard_signals.append(check_text)
        else:
            # Relaxed check: demote to soft preference so it doesn't gate candidates
            soft_signals.append(f"[Preferred, not required] {check_text}")

    # P0 weights → p0_signals (signal label only, no numeric weights)
    p0_signal_texts: list[str] = []
    for w in p0_weights:
        if isinstance(w, dict):
            sig = _coerce_str(w.get("signal"))
            if sig:
                p0_signal_texts.append(sig)

    # Overwrite top-level arrays only when the rubric has content
    if baseline_checks:
        base["baseline_signals"] = hard_signals
        # Append relaxed checks to p0_signals so they still influence scoring
        base["p0_signals"] = p0_signal_texts + soft_signals
    elif p0_weights:
        base["p0_signals"] = p0_signal_texts

    # Second pass: coerce any existing top-level signals that came in as objects
    # (guards against CALL_2 path where baseline_signals are raw JD strings)
    if not baseline_checks:
        for key in ("baseline_signals", "p0_signals"):
            existing = base.get(key)
            if isinstance(existing, list):
                base[key] = [_coerce_str(item) for item in existing if item]

    hard_count = len(base.get("baseline_signals", []))
    soft_count = len(soft_signals)
    print(
        f"[SCORING CRITERIA] hard_baseline_signals={hard_count} "
        f"soft_demoted={soft_count} "
        f"p0_signals={len(base.get('p0_signals', []))} "
        f"has_rubric={bool(rubric)}"
    )
    return base


def _run_preview_scoring(row: Session, batch: list[dict]) -> dict:
    """Run field-level scoring on the selected batch using lens + verifiable facts."""
    # Always build a fresh criteria dict that reflects the latest synthesized rubric.
    criteria = _build_scoring_criteria(row)
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
    expected_names = [payload.get("name", "") for payload in payloads]
    normalized = _normalize_scoring_output(
        parsed,
        expected_names=expected_names,
        include_field_matches=True,
        include_extra_param_matches=True,
        include_confidence=False,
    )
    _accumulate_tokens(row, "Preview Score", usage)
    row.preview_field_results = normalized
    return normalized


@router.post("/sessions/{session_id}/preview", response_model=SessionOut)
def start_preview(session_id: str, body: StartPreviewRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    try:
        if row.synthesized_config is None:
            all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
            _run_resume_enrichment(row, all_resumes, db)
            _run_silent_call2(row)

        batch = _pick_preview_batch(row, db)
        if not batch:
            raise HTTPException(status_code=400, detail="No readable resumes found in this session.")

        results = _run_preview_scoring(row, batch)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

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

    try:
        _run_synthesis(row, db, feedback_dicts)

        # Re-score the SAME candidates currently on screen so the recruiter can see
        # exactly how their include/exclude changed the outcome for those candidates.
        batch = _get_current_batch(row, db)
        if not batch:
            # Fallback: no previous results, pick a fresh batch
            batch = _pick_preview_batch(row, db)
        if not batch:
            raise HTTPException(status_code=400, detail="No readable resumes found.")

        results = _run_preview_scoring(row, batch)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1
    row.status = "preview_active"

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

    try:
        results = _run_preview_scoring(row, batch)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1
    row.status = "preview_active"

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


def _annotate_rubric_for_call3(rubric: dict) -> dict:
    """Return a copy of the rubric with an explicit hard_filters / soft_preferences
    split at the top level.

    CALL_3 receives the full scoring_rubric JSON and must correctly apply
    reject_if_missing: false checks as preferences, not hard filters. Because
    Gemini often ignores the flag and treats all baseline_checks as rejecting,
    we add a human-readable summary key that lists which checks are hard vs soft.
    This makes the distinction impossible to overlook.
    """
    if not rubric:
        return rubric

    annotated = dict(rubric)
    baseline_checks = rubric.get("baseline_checks") or []

    hard = [_coerce_str(c.get("check")) for c in baseline_checks
            if isinstance(c, dict) and c.get("reject_if_missing", True) and c.get("check")]
    soft = [_coerce_str(c.get("check")) for c in baseline_checks
            if isinstance(c, dict) and c.get("reject_if_missing") is False and c.get("check")]

    annotated["_evaluation_note"] = (
        f"HARD FILTERS (reject_if_missing: true) — failing any of these → Reject: {hard}. "
        f"SOFT PREFERENCES (reject_if_missing: false) — these do NOT cause rejection, "
        f"only slightly adjust p0_score: {soft}."
        if (hard or soft)
        else "All baseline_checks are hard filters."
    )
    return annotated


# ── step 4: accept + full evaluation ──────────────────────────────────────

@router.post("/sessions/{session_id}/accept", response_model=SessionOut)
def accept_and_run_full(session_id: str, body: AcceptRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    try:
        if row.synthesized_config is None:
            all_resumes = db.query(Resume).filter(Resume.session_id == str(row.id)).all()
            _run_resume_enrichment(row, all_resumes, db)
            _run_silent_call2(row)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    config = row.synthesized_config or row.base_criteria
    row.final_config = config

    all_resumes = db.query(Resume).filter(Resume.session_id == session_id).all()
    # Gate on text_extractable (was any text pulled from the PDF?) not on readable
    # (did our section parser find structured fields?). readable=False just means our
    # heuristic parser missed the structure — the raw text is still usable for scoring.
    def _is_extractable(r: Resume) -> bool:
        q = r.quality or {}
        # text_extractable is the new field; fall back to readable for old rows
        if "text_extractable" in q:
            return bool(q["text_extractable"])
        return bool(q.get("readable"))

    readable   = [r for r in all_resumes if _is_extractable(r)]
    unreadable = [r for r in all_resumes if not _is_extractable(r)]

    if not readable:
        raise HTTPException(status_code=400, detail="No readable resumes to assess.")

    print(
        f"[FULL EVAL] Total uploaded: {len(all_resumes)}, "
        f"readable: {len(readable)}, "
        f"unreadable (skipped): {len(unreadable)}"
    )
    if unreadable:
        print(f"[FULL EVAL] Skipped files: {[r.file_name for r in unreadable]}")

    try:
        # Enrich any resumes not yet lensed (e.g. never appeared in a preview batch)
        _run_resume_enrichment(row, readable, db)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

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

    # Evaluate every readable resume up to FULL_EVAL_BATCH_SIZE.
    # Only sample down if the upload is unusually large.
    if len(resume_dicts) <= FULL_EVAL_BATCH_SIZE:
        batch = resume_dicts
    else:
        print(
            f"[FULL EVAL] {len(resume_dicts)} readable resumes exceeds cap "
            f"({FULL_EVAL_BATCH_SIZE}). Sampling down — consider raising FULL_EVAL_BATCH_SIZE."
        )
        keywords = extract_keywords(row.jd_text or "", row.base_criteria or {}, config or {})
        batch = pick_representative_sample(resume_dicts, keywords, sample_size=FULL_EVAL_BATCH_SIZE)

    # Track which readable resumes were sampled out (only possible when > FULL_EVAL_BATCH_SIZE)
    batch_files = {r["file_name"] for r in batch}
    sampled_out = [r for r in resume_dicts if r["file_name"] not in batch_files]

    # Build scored payloads — lens + verifiable facts; fall back to name from filename
    scored_payloads = []
    for r in batch:
        payload = build_scored_resume_payload(r["resume_json"], r.get("resume_lens"), required_fields)
        if not payload.get("name"):
            payload["name"] = r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        scored_payloads.append(payload)

    raw_rubric = config.get("scoring_rubric", {}) if config else {}
    prompt_text = config.get("final_evaluation_prompt", "") if config else ""

    # Annotate the rubric so CALL_3 sees which baseline checks are hard vs soft.
    rubric = _annotate_rubric_for_call3(raw_rubric)

    # Build a name → file_name lookup so dropped-candidate detection is accurate.
    name_to_file: dict[str, str] = {}
    expected_names: list[str] = []
    for r in batch:
        candidate_name = (
            (r.get("resume_json") or {}).get("name")
            or r["file_name"].replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
        )
        normalized_name = candidate_name.strip()
        expected_names.append(normalized_name)
        name_to_file[normalized_name.lower()] = r["file_name"]

    # ── Batched CALL_3 ────────────────────────────────────────────────────────
    # Send CALL_3_BATCH_SIZE resumes per sub-call and merge results.
    # Sending all resumes in one call causes Gemini to silently drop candidates
    # when the array is large. Small batches guarantee every resume comes back.
    all_results: list[dict] = []
    model_dropped: list[dict] = []

    try:
        for batch_idx in range(0, len(scored_payloads), CALL_3_BATCH_SIZE):
            chunk = scored_payloads[batch_idx : batch_idx + CALL_3_BATCH_SIZE]
            batch_label = f"Call 3 batch {batch_idx // CALL_3_BATCH_SIZE + 1}"
            print(f"[FULL EVAL] {batch_label}: scoring {len(chunk)} resume(s)")

            chunk_parsed, _raw, usage, _prompt = run_structured_call(
                model_name=MODEL_NAME,
                system_instruction=CALL_3_SYSTEM,
                template=CALL_3_TEMPLATE,
                replacements={
                    "FINAL_EVALUATION_PROMPT": prompt_text,
                    "SCORING_RUBRIC_JSON": rubric,
                    "CANDIDATES_JSON": chunk,
                },
            )
            _accumulate_tokens(row, batch_label, usage)

            chunk_results = (
                chunk_parsed.get("results", [])
                if isinstance(chunk_parsed, dict)
                else []
            )
            all_results.extend(chunk_results)

            # Per-batch drop detection — compare names sent vs names returned
            returned_in_chunk: set[str] = {
                r["candidate_name"].strip().lower()
                for r in chunk_results
                if isinstance(r, dict) and r.get("candidate_name")
            }
            for payload in chunk:
                sent_name = (payload.get("name") or "").strip().lower()
                if sent_name and sent_name not in returned_in_chunk:
                    file_name = name_to_file.get(sent_name, sent_name)
                    model_dropped.append({
                        "file_name": file_name,
                        "reason": (
                            "Resume was sent to the evaluation model but absent from its output "
                            f"(batch {batch_idx // CALL_3_BATCH_SIZE + 1}). "
                            "Re-run the full evaluation to retry this candidate."
                        ),
                    })
                    print(f"[CALL_3 DROP] '{sent_name}' missing from {batch_label} output.")
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    if model_dropped:
        print(
            f"[CALL_3 DROP WARNING] {len(model_dropped)} resume(s) dropped across all batches: "
            f"{[d['file_name'] for d in model_dropped]}"
        )

    # Reconstruct a single parsed dict from all batches with a stable shape.
    parsed = _normalize_scoring_output(
        {"results": all_results},
        expected_names=expected_names,
        include_field_matches=False,
        include_extra_param_matches=False,
        include_confidence=True,
    )
    model_dropped.extend((parsed.get("_skipped") or {}).get("model_dropped", []))
    deduped_model_dropped: list[dict[str, str]] = []
    seen_drop_keys: set[tuple[str, str]] = set()
    for item in model_dropped:
        key = (str(item.get("file_name", "")), str(item.get("reason", "")))
        if key in seen_drop_keys:
            continue
        deduped_model_dropped.append(item)
        seen_drop_keys.add(key)
    model_dropped = deduped_model_dropped
    print(
        f"[FULL EVAL] Complete. {len(parsed.get('results', []))} evaluated across "
        f"{(len(scored_payloads) + CALL_3_BATCH_SIZE - 1) // CALL_3_BATCH_SIZE} batch(es)."
    )

    # Attach skipped-resume metadata so the frontend can surface it clearly.
    # Unreadable: PDF could not be parsed at all.
    # Sampled out: readable but excluded because upload exceeded FULL_EVAL_BATCH_SIZE.
    # Model dropped: sent to Gemini but absent from the structured response.
    if isinstance(parsed, dict):
        parsed["_skipped"] = {
            "unreadable": [
                {
                    "file_name": r.file_name,
                    "reason": "No text could be extracted from this PDF — likely a scanned image or corrupt file.",
                    "quality": r.quality or {},
                }
                for r in unreadable
            ],
            "sampled_out": [
                {
                    "file_name": r["file_name"],
                    "reason": f"Excluded by representative sampling — upload exceeded {FULL_EVAL_BATCH_SIZE} resume cap.",
                }
                for r in sampled_out
            ],
            "model_dropped": model_dropped,
        }
        total_skipped = len(unreadable) + len(sampled_out) + len(model_dropped)
        if total_skipped:
            print(
                f"[FULL EVAL] {total_skipped} resume(s) skipped: "
                f"{len(unreadable)} unreadable, {len(sampled_out)} sampled out, "
                f"{len(model_dropped)} dropped by model."
            )

    row.full_results = parsed
    row.status = "completed"

    db.commit()
    db.refresh(row)
    return row
