"""Utilities for sample selection and token-efficient resume compression."""

from __future__ import annotations

import math
import re
from typing import Any


STOPWORDS = {
    "and",
    "the",
    "with",
    "for",
    "from",
    "that",
    "this",
    "have",
    "your",
    "will",
    "into",
    "role",
    "team",
    "years",
    "year",
    "candidate",
    "candidates",
    "experience",
    "required",
}


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}", text.lower())
        if token not in STOPWORDS
    ]


def extract_keywords(
    jd_text: str,
    jd_analysis: dict[str, Any] | None = None,
    final_config: dict[str, Any] | None = None,
) -> list[str]:
    keywords = set(tokenize(jd_text))
    if jd_analysis:
        # Signal-based format: baseline_signals and p0_signals are plain strings
        for signal in jd_analysis.get("baseline_signals", []):
            keywords.update(tokenize(signal))
        for signal in jd_analysis.get("p0_signals", []):
            keywords.update(tokenize(signal))
        # red_flags is object[] with .flag field
        for flag in jd_analysis.get("red_flags", []):
            flag_text = flag.get("flag", "") if isinstance(flag, dict) else str(flag)
            keywords.update(tokenize(flag_text))
        # domain context from role_context
        role_context = jd_analysis.get("role_context", {})
        keywords.update(tokenize(role_context.get("domain_constraints", "")))
    if final_config:
        rubric = final_config.get("scoring_rubric", {})
        for check in rubric.get("baseline_checks", []):
            keywords.update(tokenize(check.get("check", "")))
        for weight in rubric.get("p0_weights", []):
            keywords.update(tokenize(weight.get("signal", "")))
    return sorted(keyword for keyword in keywords if len(keyword) > 2)[:120]


def flatten_resume(resume_json: dict[str, Any]) -> str:
    segments = [resume_json.get("name", "")]
    for education in resume_json.get("education", []):
        segments.extend([education.get("degree", ""), education.get("institution", "")])
    for job in resume_json.get("work_experience", []):
        segments.extend([job.get("company", ""), job.get("role", ""), job.get("description", "")])
    for field in ["skills", "certifications", "projects", "publications"]:
        segments.extend(resume_json.get(field, []))
    segments.append(resume_json.get("github_url", ""))
    return " ".join(segment for segment in segments if segment)


def score_resume_against_keywords(resume_json: dict[str, Any], keywords: list[str]) -> int:
    haystack = flatten_resume(resume_json).lower()
    matches = {keyword for keyword in keywords if keyword in haystack}
    return len(matches)


def compress_resume(resume_json: dict[str, Any], required_fields: list[str]) -> dict[str, Any]:
    """Compress resume while preserving qualitative signals for fallback scoring."""
    compressed = {}
    
    # Always include basic identification
    compressed["name"] = resume_json.get("name", "")
    
    # Include verifiable facts for threshold checking — full lists, no truncation,
    # so college-tier and skills-based baseline checks work on the complete data.
    for field in _VERIFIABLE_FACT_FIELDS:
        if field in resume_json:
            compressed[field] = resume_json[field]
    
    # Process required fields with enhanced qualitative preservation
    for field in required_fields:
        if field == "work_experience" and isinstance(resume_json.get(field), list):
            work_items = []
            for item in resume_json[field][:5]:
                work_item = {
                    "company": item.get("company", ""),
                    "role": item.get("role", ""),
                    "duration_months": item.get("duration_months", 0),
                    "type": item.get("type", ""),
                    # Preserve more description for qualitative analysis
                    "description": item.get("description", "")[:400],
                }
                # Extract ownership signals from description
                desc = item.get("description", "").lower()
                ownership_signals = []
                if any(phrase in desc for phrase in ["owned", "led", "built", "created", "developed"]):
                    ownership_signals.append("ownership")
                if any(phrase in desc for phrase in ["shipped", "launched", "released", "deployed"]):
                    ownership_signals.append("delivery")
                if any(phrase in desc for phrase in ["team", "collaborated", "worked with"]):
                    ownership_signals.append("collaboration")
                work_item["ownership_signals"] = ownership_signals
                work_items.append(work_item)
            compressed[field] = work_items
        elif field == "education" and isinstance(resume_json.get(field), list):
            compressed[field] = resume_json[field][:3]
        elif field in {"skills", "certifications", "projects", "publications"} and isinstance(resume_json.get(field), list):
            compressed[field] = resume_json[field][:12]
        elif field not in compressed:  # Don't overwrite verifiable facts
            compressed[field] = resume_json.get(field)
    
    # Add synthetic qualitative summary for fallback scoring
    work_exp = resume_json.get("work_experience", [])
    if work_exp:
        total_months = sum(item.get("duration_months", 0) for item in work_exp)
        compressed["synthetic_summary"] = {
            "total_experience_months": total_months,
            "role_count": len(set(item.get("role", "") for item in work_exp)),
            "company_count": len(set(item.get("company", "") for item in work_exp)),
            "has_ownership_language": any(
                any(phrase in item.get("description", "").lower() 
                    for phrase in ["owned", "led", "built", "created", "developed"])
                for item in work_exp
            ),
            "has_delivery_language": any(
                any(phrase in item.get("description", "").lower() 
                    for phrase in ["shipped", "launched", "released", "deployed"])
                for item in work_exp
            )
        }
    
    return compressed


# Fields that carry hard verifiable facts — always included alongside the lens
# so the scoring model can check thresholds without re-interpreting raw descriptions.
# education, skills, and certifications are included so college-tier / certification
# baseline checks can be verified against concrete values, not just the lens narrative.
_VERIFIABLE_FACT_FIELDS = {
    "total_experience_years",
    "career_gaps_months",
    "education",
    "work_experience",
    "skills",
    "certifications",
    "github_url",
    "linkedin_url",
    "location",
}


def build_scored_resume_payload(
    resume_json: dict[str, Any],
    resume_lens: dict[str, Any] | None,
    required_fields: list[str],
) -> dict[str, Any]:
    """Build the payload sent to scoring calls (CALL_PREVIEW / CALL_3).

    Layer 1 — lens (interpretive): recruiter's narrative notes per dimension.
    Layer 2 — verifiable facts: raw threshold fields the rubric references.

    Falls back to compress_resume() if no lens is available.
    """
    name = resume_json.get("name", "")
    payload: dict[str, Any] = {"name": name}

    if resume_lens is None:
        # No lens yet — fall back to old compressed format so scoring still works
        payload.update(compress_resume(resume_json, required_fields))
        return payload

    # Drop internal file_name key from lens before sending to scoring model
    payload["lens"] = {k: v for k, v in resume_lens.items() if k != "file_name"}

    # ALWAYS attach verifiable facts — the scoring model must be able to check
    # years-of-experience, career gaps, and education without depending on whether
    # the synthesized rubric happened to list them in required_resume_fields.
    # These are cheap, objective, and every rubric needs them.
    for field in _VERIFIABLE_FACT_FIELDS:
        if field in resume_json and resume_json[field] not in (None, "", []):
            value = resume_json[field]
            if field == "education" and isinstance(value, list):
                # Send ALL education entries — college tier checks need the full list.
                payload[field] = value
            elif field == "work_experience" and isinstance(value, list):
                # Trim heavy prose but keep company/role/duration/type so the
                # scoring model can audit YoE, tenure, and career arc.
                trimmed = []
                for job in value[:8]:
                    trimmed.append({
                        "company": job.get("company", ""),
                        "role": job.get("role", ""),
                        "duration_months": job.get("duration_months", 0),
                        "type": job.get("type", ""),
                        "description": (job.get("description", "") or "")[:300],
                    })
                payload[field] = trimmed
            else:
                payload[field] = value

    # Also attach any additional required fields the rubric references
    for field in required_fields:
        if field in payload or field in _VERIFIABLE_FACT_FIELDS:
            continue
        if field in resume_json and resume_json[field] not in (None, "", []):
            payload[field] = resume_json[field]

    # Derived convenience fields — make YoE impossible to miss.
    work_exp = resume_json.get("work_experience", []) or []
    if work_exp and "total_experience_years" not in payload:
        total_months = sum((j.get("duration_months") or 0) for j in work_exp)
        if total_months:
            payload["total_experience_years"] = round(total_months / 12, 1)

    return payload


def pick_representative_sample(
    parsed_resumes: list[dict[str, Any]],
    keywords: list[str],
    sample_size: int = 15,
) -> list[dict[str, Any]]:
    scored = []
    for item in parsed_resumes:
        quality = item.get("quality", {}) or {}
        if not quality.get("text_extractable", quality.get("readable", False)):
            continue
        score = score_resume_against_keywords(item["resume_json"], keywords)
        enriched = dict(item)
        enriched["keyword_score"] = score
        scored.append(enriched)

    scored.sort(key=lambda item: item["keyword_score"], reverse=True)
    if len(scored) <= sample_size:
        return scored

    top = scored[:5]

    middle_pool = scored[5:-5]
    middle = []
    if middle_pool:
        step = max(1, len(middle_pool) // 5)
        middle = [middle_pool[index] for index in range(0, len(middle_pool), step)[:5]]

    bottom = scored[-5:]

    selected = []
    seen = set()
    for item in top + middle + bottom:
        key = item.get("file_name")
        if key not in seen:
            selected.append(item)
            seen.add(key)

    if len(selected) < sample_size:
        for item in scored:
            key = item.get("file_name")
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            if len(selected) >= sample_size:
                break

    return selected[:sample_size]
