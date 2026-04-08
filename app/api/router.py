from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session as DBSession

from app.database import SessionLocal, get_db
from app.models import PreviewIteration, Resume, Session
from app.schemas import (
    AcceptRequest,
    AnalyzeJDRequest,
    MarkExemplarRequest,
    RefineRequest,
    ResumeDetailOut,
    ResumeOut,
    RubricFromExemplarsRequest,
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
    CALL_EXEMPLAR_SYSTEM,
    CALL_EXEMPLAR_TEMPLATE,
    CALL_FIELDS_SYSTEM,
    CALL_FIELDS_TEMPLATE,
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
# Number of resumes per CALL_3 sub-batch sent to Gemini.
# All readable resumes are evaluated — there is no total cap.
# Each sub-batch is small enough that Gemini reliably returns every candidate.
CALL_3_BATCH_SIZE = 1
# Number of CALL_3 Gemini calls to run concurrently in the background worker.
# Each call still sends only 1 resume (CALL_3_BATCH_SIZE=1) to prevent model drops.
# Concurrency multiplies throughput: 5 workers × 1 resume/call × 8s/call ≈ 8s per 5 resumes.
CALL_3_CONCURRENCY = 5


# ── helpers ────────────────────────────────────────────────────────────────

_ALLOWED_FEEDBACK_ACTIONS = {"approve", "reject", "disagree", "strong_yes", "strong_no", "unclear"}
_RESULT_CLASSIFICATIONS = {"P0", "Baseline", "Reject"}
_ALLOWED_RESUME_FIELDS = frozenset({
    "name", "education", "work_experience", "skills", "certifications", "projects",
    "publications", "github_url", "linkedin_url", "location",
    "total_experience_years", "career_gaps_months",
})


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

    # Sort: classification tier first (P0 → Baseline → Reject), then score within tier.
    # This guarantees P0 candidates are always shown at the top regardless of score
    # overlap between tiers (e.g. a Baseline at 69 never buries a P0 at 70).
    _TIER_ORDER = {"P0": 0, "Baseline": 1, "Reject": 2}
    normalized_results.sort(
        key=lambda r: (_TIER_ORDER.get(r.get("classification", "Reject"), 2), -r.get("overall_score", 0))
    )
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

    # Initialise Gemini client if an API key is provided so parse_resume_with_gemini()
    # can make calls. Safe to call even if api_key is None — it's a no-op.
    if api_key:
        configure_genai(api_key=api_key)

    # delete previous resumes for this session
    db.query(Resume).filter(Resume.session_id == session_id).delete()
    _reset_session_evaluation_state(row)

    import hashlib
    seen_names: set[str] = set()
    seen_hashes: set[str] = set()
    skipped: list[dict] = []

    for f in files:
        pdf_bytes = await f.read()

        # Dedup by file_name (case-insensitive) and by content hash.
        name_key = (f.filename or "").strip().lower()
        content_hash = hashlib.sha256(pdf_bytes).hexdigest() if pdf_bytes else ""
        if name_key and name_key in seen_names:
            skipped.append({"file_name": f.filename, "reason": "duplicate filename"})
            continue
        if content_hash and content_hash in seen_hashes:
            skipped.append({"file_name": f.filename, "reason": "duplicate content"})
            continue
        seen_names.add(name_key)
        if content_hash:
            seen_hashes.add(content_hash)

        try:
            # Use Gemini-powered JSON extraction when an API key is available.
            # Falls back to local regex parser on any Gemini error.
            use_gemini = bool(api_key)
            parsed = parse_resume_pdf(f.filename, pdf_bytes, gemini_parse=use_gemini)
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

    if skipped:
        print(f"[UPLOAD] Skipped {len(skipped)} duplicate resume(s): {skipped}")

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


def _current_criteria_signals(row: Session) -> tuple[list, list]:
    """Return (baseline_signals, p0_signals) from synthesized config if available,
    else fall back to the original JD analysis signals.

    After any synthesis (include/exclude/feedback), the synthesized rubric has
    updated checks that reflect the recruiter's current intent. The lens must be
    generated against THOSE signals so the recruiter's reading is aligned with what
    the scoring model will actually evaluate.
    """
    synth = row.synthesized_config or {}
    rubric = synth.get("scoring_rubric") or {}
    baseline_checks = rubric.get("baseline_checks") or []
    p0_weights = rubric.get("p0_weights") or []

    if baseline_checks or p0_weights:
        # Use synthesized signals — these reflect all include/exclude/feedback changes
        baseline_signals = [
            _coerce_str(c.get("check"))
            for c in baseline_checks
            if isinstance(c, dict) and c.get("reject_if_missing", True) and c.get("check")
        ]
        p0_signals = [
            _coerce_str(w.get("signal"))
            for w in p0_weights
            if isinstance(w, dict) and w.get("signal")
        ]
        return baseline_signals, p0_signals

    # Fall back to original JD analysis
    jd = row.jd_analysis or {}
    return jd.get("baseline_signals", []), jd.get("p0_signals", [])


def _run_resume_enrichment(row: Session, resumes: list[Resume], db: DBSession) -> None:
    """Read each resume's full raw text through the JD lens and store the result.

    Uses synthesized criteria signals when available so the lens reflects the
    recruiter's current rubric (after any include/exclude/feedback changes), not
    just the original JD.

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
    gap_context = jd.get("gap_questions", [])
    baseline_signals, p0_signals = _current_criteria_signals(row)

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
                "BASELINE_SIGNALS": baseline_signals,
                "P0_SIGNALS": p0_signals,
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
        lens_by_file = {item["file_name"]: item for item in lens_list if isinstance(item, dict)}

        # Retry each dropped resume individually once before falling back to compress_resume.
        # Per-resume isolation dramatically reduces the drop rate — batches lose candidates
        # far more often than single-resume calls.
        if dropped:
            print(
                f"[LENS DROP WARNING] Sent {len(batch)} resume(s), received {len(lens_list)}. "
                f"Retrying {len(dropped)} individually: {dropped}"
            )
            for r in batch:
                if r.file_name not in dropped:
                    continue
                try:
                    single_block = (
                        f"=== CANDIDATE START: {r.file_name} ===\n"
                        f"{r.raw_text or ''}\n"
                        f"=== CANDIDATE END: {r.file_name} ==="
                    )
                    retry_parsed, _rraw, retry_usage, _rp = run_structured_call(
                        model_name=MODEL_NAME,
                        system_instruction=CALL_LENS_SYSTEM,
                        template=CALL_LENS_TEMPLATE,
                        replacements={
                            "ROLE_TYPE": jd.get("role_type", "unknown"),
                            "ROLE_CONTEXT": jd.get("role_context", {}),
                            "ONE_LINER": jd.get("one_liner", ""),
                            "BASELINE_SIGNALS": baseline_signals,
                            "P0_SIGNALS": p0_signals,
                            "GAP_ANSWERS": gap_context,
                            "RESUMES": single_block,
                        },
                    )
                    _accumulate_tokens(row, f"Lens retry {r.file_name}", retry_usage)
                    retry_list = retry_parsed if isinstance(retry_parsed, list) else []
                    for item in retry_list:
                        if isinstance(item, dict) and item.get("file_name") == r.file_name:
                            lens_by_file[r.file_name] = item
                            print(f"[LENS RETRY SUCCESS] {r.file_name}")
                            break
                except Exception as exc:
                    print(f"[LENS RETRY FAIL] {r.file_name}: {exc}")


        for r in batch:
            lens = lens_by_file.get(r.file_name)
            if lens and _validate_lens(lens):
                r.resume_lens = lens
                print(f"[LENS SUCCESS] {r.file_name} — lens validated and stored")
            else:
                # Mark as degraded so the frontend can flag this candidate —
                # scoring still runs via compress_resume() fallback.
                quality = dict(r.quality or {})
                quality["lens_degraded"] = True
                r.quality = quality
                r.resume_lens = None
                print(
                    f"[LENS DEGRADED] {r.file_name} — no valid lens after retry. "
                    f"Scoring will fall back to compress_resume()."
                )
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
    # Prune seen_files of any filenames that no longer exist in the session
    # (e.g. deleted or replaced on a new upload). Keeps the seen set in sync
    # with the current resume set so new uploads are eligible immediately.
    readable_files = {r["file_name"] for r in readable}
    seen: set[str] = {f for f in (row.preview_seen_files or []) if f in readable_files}
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


def _run_field_selection(row: Session, db: DBSession) -> None:
    """CALL_FIELDS micro-call: identify minimum required resume fields from the rubric.

    Non-fatal — if it fails, the rubric's own required_resume_fields are kept.
    Merges output with _VERIFIABLE_FACT_FIELDS to ensure scoring always has
    the threshold fields it needs.
    """
    from ats_poc.sample_selection import _VERIFIABLE_FACT_FIELDS
    rubric = (row.synthesized_config or {}).get("scoring_rubric")
    if not rubric:
        return
    try:
        parsed, _raw, usage, _prompt = run_structured_call(
            model_name=MODEL_NAME,
            system_instruction=CALL_FIELDS_SYSTEM,
            template=CALL_FIELDS_TEMPLATE,
            replacements={"SCORING_RUBRIC_JSON": rubric},
        )
        _accumulate_tokens(row, "Call Fields", usage)
        raw_fields = parsed.get("required_fields", []) if isinstance(parsed, dict) else []
        filtered = [f for f in raw_fields if isinstance(f, str) and f in _ALLOWED_RESUME_FIELDS]
        # Always include verifiable fact fields
        merged = list(dict.fromkeys(
            ["name"] + filtered + [f for f in _VERIFIABLE_FACT_FIELDS if f in _ALLOWED_RESUME_FIELDS]
        ))
        synth = dict(row.synthesized_config)
        synth["required_resume_fields"] = merged
        row.synthesized_config = synth
        print(f"[CALL_FIELDS] Required fields selected: {merged}")
    except Exception as exc:
        print(f"[CALL_FIELDS] Non-fatal error — keeping existing fields: {exc}")


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
            # Pass the previous rubric so synthesis PRESERVES checks that were
            # not touched by the new iteration, instead of rewriting every check
            # from scratch and producing cosmetic drift.
            "PREVIOUS_RUBRIC_JSON": row.synthesized_config or {},
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

    # Add an explicit structured checklist at the top level so the scoring model
    # can't miss any criterion. The model MUST check every item in this list:
    # - hard_baseline_checks: every item here is a REJECTION if not met
    # - p0_weighted_signals: every item here contributes to the P0 score
    # This is IN ADDITION to the text-based baseline_signals/p0_signals arrays above,
    # giving the model two representations of the same data — belt AND suspenders.
    if baseline_checks:
        base["_hard_baseline_checklist"] = [
            {
                "check": _coerce_str(c.get("check")),
                "check_against_field": c.get("resume_field", "work_experience"),
                "reject_if_missing": c.get("reject_if_missing", True),
                "instruction": (
                    "HARD REJECT if not satisfied — set baseline_pass: false"
                    if c.get("reject_if_missing", True)
                    else "SOFT preference — do NOT reject, only lower p0_score slightly"
                ),
            }
            for c in baseline_checks
            if isinstance(c, dict) and _coerce_str(c.get("check"))
        ]

    if p0_weights:
        base["_p0_weighted_checklist"] = [
            {
                "signal": _coerce_str(w.get("signal")),
                "weight": w.get("weight", 0),
                "check_against_field": w.get("resume_field", "work_experience"),
                "instruction": f"Contributes {w.get('weight', 0)} points to p0_score if clearly evidenced.",
            }
            for w in p0_weights
            if isinstance(w, dict) and _coerce_str(w.get("signal"))
        ]

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
    # Stamp server-known file_name on each result so downstream carryover logic
    # can do O(1) lookups without fuzzy name matching.
    name_to_file = {
        (p.get("name") or "").strip().lower(): b["file_name"]
        for p, b in zip(payloads, batch)
    }
    for item in normalized.get("results", []):
        cname = (item.get("candidate_name") or "").strip().lower()
        fn = name_to_file.get(cname)
        if fn:
            item["file_name"] = fn
    _accumulate_tokens(row, "Preview Score", usage)
    row.preview_field_results = normalized
    return normalized


@router.post("/sessions/{session_id}/preview", response_model=SessionOut)
def start_preview(session_id: str, body: StartPreviewRequest, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    try:
        if row.synthesized_config is None:
            # Lazy lens: do NOT enrich every uploaded resume upfront.
            # _run_silent_call2 only needs base_criteria; lenses are only required
            # for the resumes actually being scored, and are generated below.
            _run_silent_call2(row)
            _run_field_selection(row, db)

        batch = _pick_preview_batch(row, db)
        if not batch:
            raise HTTPException(status_code=400, detail="No readable resumes found in this session.")

        # Lazy enrichment — lens only the 2 resumes about to be scored.
        batch_files = [b["file_name"] for b in batch]
        batch_rows = (
            db.query(Resume)
            .filter(Resume.session_id == str(row.id), Resume.file_name.in_(batch_files))
            .all()
        )
        _run_resume_enrichment(row, batch_rows, db)
        lens_by_file = {r.file_name: r.resume_lens for r in batch_rows}
        for b in batch:
            b["resume_lens"] = lens_by_file.get(b["file_name"])

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

    has_params = any([
        body.instructions.strip(),
        body.include.strip(), body.exclude.strip(),
        body.update_baseline.strip(), body.update_p0.strip(),
    ])
    if has_params:
        history = list(row.extra_params_history or [])
        entry: dict = {"iteration": row.preview_iteration_count}
        if body.instructions.strip():
            entry["instructions"] = body.instructions.strip()
        else:
            entry["include"] = body.include.strip()
            entry["exclude"] = body.exclude.strip()
            entry["update_baseline"] = body.update_baseline.strip()
            entry["update_p0"] = body.update_p0.strip()
        history.append(entry)
        row.extra_params_history = history

    feedback_dicts = [fb.model_dump() for fb in body.candidate_feedback] if body.candidate_feedback else []

    try:
        _run_synthesis(row, db, feedback_dicts)

        # Re-score the SAME candidates currently on screen so the recruiter can see
        # exactly how their include/exclude/rubric changes shifted outcomes.
        batch = _get_current_batch(row, db)
        if not batch:
            # Fallback: no previous results, pick a fresh batch
            batch = _pick_preview_batch(row, db)
        if not batch:
            raise HTTPException(status_code=400, detail="No readable resumes found.")

        batch_files = {r["file_name"] for r in batch}
        # Ensure every file_name that feedback was given on is in the rescoring batch,
        # even if it fell out of the displayed preview. Otherwise disagree feedback has
        # no visible effect because the candidate is never re-scored.
        feedback_files = {fb.get("file_name") for fb in feedback_dicts if fb.get("file_name")}
        batch_files.update(feedback_files)

        batch_resumes = (
            db.query(Resume)
            .filter(Resume.session_id == str(row.id), Resume.file_name.in_(batch_files))
            .all()
        )
        # Only invalidate lenses for resumes the recruiter gave feedback on.
        # Preserving the other lenses eliminates prose-drift variance across
        # re-evaluate clicks — the rest of the batch sees stable context and
        # any score change is purely due to the rubric update.
        for r in batch_resumes:
            if r.file_name in feedback_files:
                r.resume_lens = None
                db.add(r)
        db.flush()

        # Enrich any resume that still has no lens (new-to-batch or just-invalidated).
        _run_resume_enrichment(row, batch_resumes, db)

        # Rebuild the batch dict list from the refreshed DB rows so it includes both
        # the previously-visible preview candidates AND any feedback candidates that
        # were pulled in for re-scoring.
        batch = [
            {
                "file_name": r.file_name,
                "resume_json": r.resume_json,
                "resume_lens": r.resume_lens,
                "quality": r.quality,
            }
            for r in batch_resumes
            if (r.quality or {}).get("text_extractable", (r.quality or {}).get("readable"))
        ]

        results = _run_preview_scoring(row, batch)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1
    row.status = "preview_active"

    db.add(PreviewIteration(
        session_id=session_id,
        iteration_number=row.preview_iteration_count,
        extra_params={
            "include": body.include,
            "exclude": body.exclude,
            "update_baseline": body.update_baseline,
            "update_p0": body.update_p0,
        },
        field_results=results,
        synthesized_config_snapshot=row.synthesized_config,
    ))
    db.commit()
    db.refresh(row)
    return row


@router.post("/sessions/{session_id}/preview/reload", response_model=SessionOut)
def reload_preview(session_id: str, body: StartPreviewRequest, db: DBSession = Depends(get_db)):
    """Load MORE resumes into the existing preview (append, not replace).

    Picks PREVIEW_BATCH_SIZE additional unseen resumes, adds them to the set of
    candidates currently in the preview, and re-scores the combined set using the
    current rubric. The recruiter can keep clicking this to grow the preview to
    2 → 4 → 6 → ... resumes until they are satisfied with the rubric, then accept
    to run the FINAL (frozen) rubric on the remaining not-yet-previewed resumes.
    """
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    # Pick PREVIEW_BATCH_SIZE NEW unseen resumes
    new_batch = _pick_preview_batch(row, db)
    if not new_batch:
        raise HTTPException(status_code=400, detail="No more readable resumes available to load.")

    # Combine with currently-previewed resumes so the grown batch is scored together
    existing_batch = _get_current_batch(row, db)
    combined_by_file: dict[str, dict] = {r["file_name"]: r for r in existing_batch}
    for r in new_batch:
        combined_by_file[r["file_name"]] = r
    combined_batch = list(combined_by_file.values())

    try:
        # Ensure lenses exist for the newly-added resumes against the current rubric
        new_files = {r["file_name"] for r in new_batch}
        new_resume_rows = (
            db.query(Resume)
            .filter(Resume.session_id == str(row.id), Resume.file_name.in_(new_files))
            .all()
        )
        _run_resume_enrichment(row, new_resume_rows, db)

        # Reload combined batch dicts so new rows carry fresh lenses
        combined_by_file = {r["file_name"]: r for r in _get_current_batch(row, db)}
        for rr in new_resume_rows:
            if (rr.quality or {}).get("text_extractable", (rr.quality or {}).get("readable")):
                combined_by_file[rr.file_name] = {
                    "file_name": rr.file_name,
                    "resume_json": rr.resume_json,
                    "resume_lens": rr.resume_lens,
                    "quality": rr.quality,
                }
        combined_batch = list(combined_by_file.values())

        results = _run_preview_scoring(row, combined_batch)
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    row.preview_iteration_count = (row.preview_iteration_count or 0) + 1
    row.status = "preview_active"

    db.add(PreviewIteration(
        session_id=session_id,
        iteration_number=row.preview_iteration_count,
        extra_params={
            "action": "loaded_more_resumes",
            "added_count": len(new_batch),
            "total_in_preview": len(combined_batch),
        },
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

    # Structured checklist so CALL_3 knows exactly which resume_field to look at
    # for each check and whether it is a HARD REJECT or a soft preference.
    annotated["_must_check_every_item"] = [
        {
            "check": _coerce_str(c.get("check")),
            "look_at_field": c.get("resume_field", "work_experience"),
            "verdict_if_missing": "REJECT — set baseline_pass: false" if c.get("reject_if_missing", True) else "SOFT — do NOT reject",
        }
        for c in baseline_checks
        if isinstance(c, dict) and _coerce_str(c.get("check"))
    ]

    # Explicit P0 scoring guide so every signal is evaluated against the right field
    p0_weights = rubric.get("p0_weights") or []
    annotated["_p0_scoring_guide"] = [
        {
            "signal": _coerce_str(w.get("signal")),
            "look_at_field": w.get("resume_field", "work_experience"),
            "max_points": w.get("weight", 0),
        }
        for w in p0_weights
        if isinstance(w, dict) and _coerce_str(w.get("signal"))
    ]

    return annotated


# ── step 4: accept + full evaluation ──────────────────────────────────────

def _build_carryover_map(readable: list[Resume], preview_items: list[Any]) -> dict[str, dict]:
    """Resolve preview results → {file_name: result}, using stamped file_name
    first and falling back to legacy fuzzy name matching."""
    out: dict[str, dict] = {}
    readable_files = {r.file_name for r in readable}
    legacy: dict[str, str] | None = None
    for item in preview_items:
        if not isinstance(item, dict):
            continue
        fname = item.get("file_name")
        if fname and fname in readable_files:
            out[fname] = item
            continue
        if legacy is None:
            legacy = {}
            for r in readable:
                nm = ((r.resume_json or {}).get("name") or "").strip().lower()
                stem = r.file_name.replace(".pdf", "").replace("_", " ").replace("-", " ").strip().lower()
                if nm:
                    legacy[nm] = r.file_name
                legacy[stem] = r.file_name
                legacy[r.file_name.lower()] = r.file_name
        resolved = legacy.get((item.get("candidate_name") or "").strip().lower())
        if resolved:
            out[resolved] = item
    return out


def _is_resume_extractable(r: Resume) -> bool:
    q = r.quality or {}
    if "text_extractable" in q:
        return bool(q["text_extractable"])
    return bool(q.get("readable"))


def _run_full_eval_worker(session_id: str, api_key: str) -> None:
    """Background worker: scores every needs-scoring resume one at a time,
    checkpointing full_results and full_eval_progress after each resume so a
    crash or timeout never loses scored work. Safe to resume — skips any
    file_name already present in full_results.
    """
    db = SessionLocal()
    try:
        configure_genai(api_key=api_key)
        row = db.query(Session).filter(Session.id == session_id).first()
        if not row:
            return

        config = row.final_config or row.synthesized_config or row.base_criteria or {}
        required_fields = config.get("required_resume_fields", [])
        raw_rubric = config.get("scoring_rubric", {})
        prompt_text = config.get("final_evaluation_prompt", "")
        rubric = _annotate_rubric_for_call3(raw_rubric)

        all_resumes = db.query(Resume).filter(Resume.session_id == session_id).all()
        readable = [r for r in all_resumes if _is_resume_extractable(r)]
        unreadable = [r for r in all_resumes if not _is_resume_extractable(r)]

        preview_items = []
        if isinstance(row.preview_field_results, dict):
            preview_items = row.preview_field_results.get("results") or []
        preview_results_by_file = _build_carryover_map(readable, preview_items)

        # Checkpoint: resume from any already-scored results in full_results
        existing_results: list[dict] = []
        if isinstance(row.full_results, dict):
            for item in (row.full_results.get("results") or []):
                if isinstance(item, dict):
                    existing_results.append(item)
        existing_files = {item.get("file_name") for item in existing_results if item.get("file_name")}

        # Attach preview carryover once
        for fname, p_item in preview_results_by_file.items():
            if fname in existing_files:
                continue
            carried = dict(p_item)
            carried["_source"] = "preview_carryover"
            carried["file_name"] = fname
            existing_results.append(carried)
            existing_files.add(fname)

        needs_scoring = [r for r in readable if r.file_name not in existing_files]
        total = len(readable)

        row.full_eval_progress = {
            "status": "running",
            "scored": len(existing_results),
            "total": total,
            "error": None,
        }
        db.commit()

        # Lazy lens for needs_scoring only — invalidate so they align with final rubric
        for r in needs_scoring:
            r.resume_lens = None
            db.add(r)
        db.flush()
        _run_resume_enrichment(row, needs_scoring, db)
        db.commit()
        # Reload to pick up fresh lenses
        refreshed = {
            r.file_name: r
            for r in db.query(Resume).filter(Resume.session_id == session_id).all()
        }
        needs_scoring = [refreshed[r.file_name] for r in needs_scoring if r.file_name in refreshed]

        model_dropped: list[dict] = []
        pending_token_usage: list[tuple[str, dict]] = []

        def _score_one_worker(payload: dict, label: str, file_name: str) -> dict:
            """Thread worker: score one resume, return result bundle.

            Returns a dict with keys: file_name, results (list), usage, label, dropped (bool).
            Does NOT touch the DB or shared state — safe to run concurrently.
            """
            try:
                parsed, _raw, usage, _prompt = run_structured_call(
                    model_name=MODEL_NAME,
                    system_instruction=CALL_3_SYSTEM,
                    template=CALL_3_TEMPLATE,
                    replacements={
                        "FINAL_EVALUATION_PROMPT": prompt_text,
                        "SCORING_RUBRIC_JSON": rubric,
                        "CANDIDATES_JSON": [payload],
                    },
                )
                results = parsed.get("results", []) if isinstance(parsed, dict) else []
            except Exception as exc:
                print(f"[CALL_3 ERROR] {file_name}: {exc}")
                results = []
                usage = {}

            sent_name = (payload.get("name") or "").strip().lower()
            returned = {(x.get("candidate_name") or "").strip().lower() for x in results if isinstance(x, dict)}

            if sent_name not in returned:
                # One in-thread retry for dropped candidate
                try:
                    retry_parsed, _rraw, retry_usage, _rp = run_structured_call(
                        model_name=MODEL_NAME,
                        system_instruction=CALL_3_SYSTEM,
                        template=CALL_3_TEMPLATE,
                        replacements={
                            "FINAL_EVALUATION_PROMPT": prompt_text,
                            "SCORING_RUBRIC_JSON": rubric,
                            "CANDIDATES_JSON": [payload],
                        },
                    )
                    retry_results = retry_parsed.get("results", []) if isinstance(retry_parsed, dict) else []
                    retry_returned = {(x.get("candidate_name") or "").strip().lower() for x in retry_results if isinstance(x, dict)}
                    if sent_name in retry_returned:
                        results = retry_results
                        usage = retry_usage
                        returned = retry_returned
                        print(f"[CALL_3 RETRY SUCCESS] {file_name}")
                    else:
                        print(f"[CALL_3 RETRY FAIL] {file_name}: still missing after retry")
                except Exception as exc:
                    print(f"[CALL_3 RETRY FAIL] {file_name}: {exc}")

            dropped = sent_name not in {(x.get("candidate_name") or "").strip().lower() for x in results if isinstance(x, dict)}
            for item in results:
                if isinstance(item, dict):
                    item["file_name"] = file_name

            return {"file_name": file_name, "results": results, "usage": usage, "label": label, "dropped": dropped}

        # ── Parallel CALL_3 ───────────────────────────────────────────────────
        # Process CALL_3_CONCURRENCY resumes simultaneously. Each thread scores
        # 1 resume (keeps reliability), but N threads run in parallel — cutting
        # wall-clock time from ~80 min to ~80/N min for 600 resumes.
        chunk_start = 1
        for chunk_start_idx in range(0, len(needs_scoring), CALL_3_CONCURRENCY):
            chunk = needs_scoring[chunk_start_idx : chunk_start_idx + CALL_3_CONCURRENCY]

            # Build payloads for this chunk on the main thread (DB access)
            chunk_jobs: list[tuple[dict, str, str]] = []  # (payload, label, file_name)
            for offset, r in enumerate(chunk):
                global_idx = chunk_start_idx + offset + 1
                payload = build_scored_resume_payload(r.resume_json, r.resume_lens, required_fields)
                if not payload.get("name"):
                    payload["name"] = r.file_name.replace(".pdf", "").replace("_", " ").replace("-", " ").strip()
                label = f"Call 3 #{global_idx}"
                chunk_jobs.append((payload, label, r.file_name))

            print(f"[FULL EVAL] Scoring chunk {chunk_start_idx + 1}–{chunk_start_idx + len(chunk)} "
                  f"of {len(needs_scoring)} ({CALL_3_CONCURRENCY} concurrent workers)")

            # Fire all calls in the chunk concurrently
            with ThreadPoolExecutor(max_workers=CALL_3_CONCURRENCY) as executor:
                futures = {
                    executor.submit(_score_one_worker, payload, label, file_name): file_name
                    for payload, label, file_name in chunk_jobs
                }
                chunk_bundle: list[dict] = []
                for future in as_completed(futures):
                    try:
                        bundle = future.result()
                        chunk_bundle.append(bundle)
                    except Exception as exc:
                        fn = futures[future]
                        print(f"[CALL_3 THREAD ERROR] {fn}: {exc}")
                        chunk_bundle.append({"file_name": fn, "results": [], "usage": {}, "label": "?", "dropped": True})

            # Merge chunk results on the main thread — no concurrent DB writes
            for bundle in chunk_bundle:
                if bundle["dropped"]:
                    model_dropped.append({
                        "file_name": bundle["file_name"],
                        "reason": "Sent to the evaluation model and absent from the response even after retry.",
                    })
                else:
                    existing_results.extend(bundle["results"])
                # Accumulate tokens on main thread (no lock needed)
                if bundle.get("usage"):
                    _accumulate_tokens(row, bundle["label"], bundle["usage"])

            # Checkpoint after each chunk — partial results survive crashes
            name_to_file_snapshot = {
                (x.get("candidate_name") or "").strip().lower(): x.get("file_name")
                for x in existing_results if isinstance(x, dict)
            }
            normalized = _normalize_scoring_output(
                {"results": existing_results},
                expected_names=[
                    (x.get("candidate_name") or "")
                    for x in existing_results if isinstance(x, dict)
                ],
                include_field_matches=False,
                include_extra_param_matches=False,
                include_confidence=True,
            )
            for item in normalized.get("results", []):
                fn = name_to_file_snapshot.get((item.get("candidate_name") or "").strip().lower())
                if fn:
                    item["file_name"] = fn
            normalized["_skipped"] = {
                "unreadable": [
                    {
                        "file_name": u.file_name,
                        "reason": "No text could be extracted from this PDF — likely a scanned image or corrupt file.",
                        "quality": u.quality or {},
                    }
                    for u in unreadable
                ],
                "sampled_out": [],
                "model_dropped": model_dropped,
            }
            row.full_results = normalized
            row.full_eval_progress = {
                "status": "running",
                "scored": len(existing_results),
                "total": total,
                "error": None,
            }
            db.commit()

        row.full_eval_progress = {
            "status": "completed",
            "scored": len(existing_results),
            "total": total,
            "error": None,
        }
        row.status = "completed"
        db.commit()
        print(f"[FULL EVAL] Worker complete. {len(existing_results)}/{total} scored.")

    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            row = db.query(Session).filter(Session.id == session_id).first()
            if row:
                prog = dict(row.full_eval_progress or {})
                prog["status"] = "error"
                prog["error"] = str(exc)
                row.full_eval_progress = prog
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.get("/sessions/{session_id}/full-eval-status")
def get_full_eval_status(session_id: str, db: DBSession = Depends(get_db)):
    row = _get_session_or_404(session_id, db)
    results_count = 0
    if isinstance(row.full_results, dict):
        results_count = len(row.full_results.get("results") or [])
    return {
        "progress": row.full_eval_progress or {"status": "idle"},
        "results_count": results_count,
        "status": row.status,
    }


@router.post("/sessions/{session_id}/accept", response_model=SessionOut)
def accept_and_run_full(
    session_id: str,
    body: AcceptRequest,
    background_tasks: BackgroundTasks,
    db: DBSession = Depends(get_db),
):
    row = _get_session_or_404(session_id, db)
    api_key = _resolve_key(body.api_key)
    configure_genai(api_key=api_key)

    try:
        if row.synthesized_config is None:
            # Synthesis only needs base_criteria — safe to run synchronously.
            _run_silent_call2(row)
            _run_field_selection(row, db)

        # Feedback propagation: if the recruiter submitted Disagree on any
        # candidate and is going directly to Accept (without clicking Re-evaluate),
        # re-run synthesis once so their feedback reaches final_config.
        feedback_history = row.candidate_feedback_history or []
        last_synthesis_iter = (row.synthesized_config or {}).get("_last_synthesis_iteration", -1)
        latest_feedback_iter = max(
            (fb.get("iteration", -1) for fb in feedback_history if isinstance(fb, dict)),
            default=-1,
        )
        if latest_feedback_iter >= 0 and latest_feedback_iter != last_synthesis_iter:
            print(
                f"[ACCEPT] Pending feedback detected (latest iter={latest_feedback_iter}, "
                f"last synth iter={last_synthesis_iter}). Running synthesis before full eval."
            )
            _run_synthesis(row, db, [])
            synth = dict(row.synthesized_config or {})
            synth["_last_synthesis_iteration"] = latest_feedback_iter
            row.synthesized_config = synth
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    config = row.synthesized_config or row.base_criteria
    row.final_config = config

    all_resumes = db.query(Resume).filter(Resume.session_id == session_id).all()
    readable = [r for r in all_resumes if _is_resume_extractable(r)]
    if not readable:
        raise HTTPException(status_code=400, detail="No readable resumes to assess.")

    # If a run is already in progress, return current state (idempotent re-click).
    progress = row.full_eval_progress or {}
    if progress.get("status") == "running":
        return row

    # Seed progress so the frontend can show a "queued" state before the worker starts.
    row.full_eval_progress = {
        "status": "queued",
        "scored": 0,
        "total": len(readable),
        "error": None,
    }
    row.status = "running_full_eval"
    db.commit()
    db.refresh(row)

    # Kick off background worker — the rest of full evaluation (lens + CALL_3 + checkpoint)
    # happens asynchronously so even 600 resumes never block an HTTP request.
    background_tasks.add_task(_run_full_eval_worker, session_id, api_key)
    return row


# ── exemplar endpoints ─────────────────────────────────────────────────────

@router.patch("/sessions/{session_id}/resumes/{file_name:path}/mark-exemplar", response_model=ResumeOut)
def mark_exemplar(
    session_id: str,
    file_name: str,
    body: MarkExemplarRequest,
    db: DBSession = Depends(get_db),
):
    resume = (
        db.query(Resume)
        .filter(Resume.session_id == session_id, Resume.file_name == file_name)
        .first()
    )
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")
    resume.is_exemplar = body.is_exemplar
    db.commit()
    db.refresh(resume)
    return resume


@router.post("/sessions/{session_id}/rubric-from-exemplars", response_model=SessionOut)
def rubric_from_exemplars(
    session_id: str,
    body: RubricFromExemplarsRequest,
    db: DBSession = Depends(get_db),
):
    """Generate a scoring rubric by reverse-engineering it from marked exemplar resumes.

    Requires at least 2 resumes marked as is_exemplar=True.
    Falls back to standard CALL_2 path if < 2 exemplars are marked.
    """
    row = _get_session_or_404(session_id, db)
    configure_genai(api_key=_resolve_key(body.api_key))

    exemplars = (
        db.query(Resume)
        .filter(Resume.session_id == session_id, Resume.is_exemplar == True)  # noqa: E712
        .all()
    )

    if len(exemplars) < 2:
        # Not enough exemplars — fall back to standard CALL_2
        print(f"[EXEMPLAR] Only {len(exemplars)} exemplar(s) marked — falling back to CALL_2")
        try:
            _run_silent_call2(row)
            _run_field_selection(row, db)
        except GeminiUnavailableError as exc:
            _raise_gemini_error(exc)
        row.status = "rubric_ready"
        db.commit()
        db.refresh(row)
        return row

    # Build exemplar resume payloads
    exemplar_payloads = [
        {"file_name": r.file_name, "resume_json": r.resume_json or {}}
        for r in exemplars
    ]

    try:
        parsed, _raw, usage, _prompt = run_structured_call(
            model_name=MODEL_NAME,
            system_instruction=CALL_EXEMPLAR_SYSTEM,
            template=CALL_EXEMPLAR_TEMPLATE,
            replacements={
                "JD_TEXT": row.jd_text or "",
                "EXEMPLAR_RESUMES_JSON": exemplar_payloads,
                "EXEMPLAR_COUNT": len(exemplar_payloads),
            },
        )
    except GeminiUnavailableError as exc:
        _raise_gemini_error(exc)

    _accumulate_tokens(row, "Call Exemplar", usage)
    normalized = _normalize_synthesized_config(parsed)
    _validate_synthesized_config(normalized, label="CALL_EXEMPLAR")
    row.synthesized_config = normalized

    _run_field_selection(row, db)
    row.status = "rubric_ready"
    db.commit()
    db.refresh(row)
    return row
