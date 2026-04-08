"""Local PDF extraction and rule-based resume parsing."""

from __future__ import annotations

import io
import logging
import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

import pdfplumber

# pdfplumber emits noisy FontBBox / font-descriptor warnings for PDFs that use
# non-standard fonts. These are harmless — text extraction still works. Silence
# them so the recruiter-facing terminal doesn't fill with irrelevant noise.
logging.getLogger("pdfplumber").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

SECTION_ALIASES = {
    "education": {"education", "academic background", "academics"},
    "work_experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "career history",
    },
    "skills": {"skills", "technical skills", "core skills", "core competencies"},
    "certifications": {"certifications", "licenses", "certificates"},
    "projects": {"projects", "selected projects", "personal projects"},
    "publications": {"publications", "research", "papers"},
}

GENERIC_CONTACT_WORDS = {
    "resume",
    "curriculum vitae",
    "linkedin",
    "github",
    "email",
    "phone",
    "mobile",
    "address",
}


def extract_text_from_pdf(file_bytes: bytes) -> str:
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                # Corrupt / non-standard page — skip it, don't abort the whole PDF.
                pages.append("")
    return "\n".join(pages).strip()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).replace("\r", "").strip()


def split_lines(text: str) -> list[str]:
    lines = [normalize_whitespace(line) for line in text.splitlines()]
    return [line for line in lines if line]


def canonical_heading(line: str) -> str:
    return re.sub(r"[^a-z ]", "", line.lower()).strip()


def sectionize_resume(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = defaultdict(list)
    current_section = "header"
    heading_map = {
        alias: canonical
        for canonical, aliases in SECTION_ALIASES.items()
        for alias in aliases
    }

    for line in lines:
        normalized = canonical_heading(line)
        if normalized in heading_map:
            current_section = heading_map[normalized]
            continue
        sections[current_section].append(line)
    return dict(sections)


def parse_name(lines: list[str]) -> str:
    for line in lines[:8]:
        lower = line.lower()
        if any(word in lower for word in GENERIC_CONTACT_WORDS):
            continue
        if "@" in line or re.search(r"\+?\d[\d\s\-()]{7,}", line):
            continue
        # Accept Latin, Indian (Devanagari etc.), dots, hyphens, apostrophes
        clean = line.strip()
        if 1 <= len(clean.split()) <= 5 and len(clean) <= 60:
            # Must have at least one letter; reject pure-symbol/number lines
            if re.search(r"[A-Za-z\u0900-\u097F\u0980-\u09FF]", clean):
                if not re.search(r"[\[\]|\\<>{}()=+*&^%$#!\'\"]+", clean):
                    return clean
    return ""


def parse_github_url(text: str) -> str:
    match = re.search(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+", text, flags=re.IGNORECASE)
    return match.group(0) if match else ""


def parse_skills(section_lines: list[str]) -> list[str]:
    if not section_lines:
        return []
    text = " | ".join(section_lines)
    parts = re.split(r"[,|/•;\n]", text)
    cleaned = []
    seen = set()
    for part in parts:
        item = normalize_whitespace(part).strip("- ").strip()
        if not item or len(item) > 40:
            continue
        key = item.lower()
        if key not in seen:
            cleaned.append(item)
            seen.add(key)
    return cleaned[:25]


def parse_simple_list(section_lines: list[str]) -> list[str]:
    items = []
    seen = set()
    for line in section_lines:
        item = normalize_whitespace(re.sub(r"^[•\-\*]\s*", "", line)).strip()
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            items.append(item)
            seen.add(key)
    return items[:12]


def _parse_date_token(token: str) -> tuple[int, int] | None:
    token = token.strip().lower()
    if token in {"present", "current", "now"}:
        today = datetime.today()
        return today.year, today.month

    month_match = re.match(r"([A-Za-z]{3,9})\s+(\d{4})", token)
    if month_match:
        month_name = month_match.group(1)[:4].lower().rstrip(".")
        month_num = MONTH_MAP.get(month_name[:3]) or MONTH_MAP.get(month_name)
        if month_num:
            return int(month_match.group(2)), month_num

    year_match = re.match(r"(\d{4})", token)
    if year_match:
        return int(year_match.group(1)), 1

    return None


def parse_date_range(text: str) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    match = re.search(
        r"((?:[A-Za-z]{3,9}\s+)?\d{4})\s*(?:-|to|–|—)\s*((?:[A-Za-z]{3,9}\s+)?\d{4}|Present|Current|Now)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    start = _parse_date_token(match.group(1))
    end = _parse_date_token(match.group(2))
    if not start or not end:
        return None

    start_months = start[0] * 12 + start[1]
    end_months = end[0] * 12 + end[1]
    duration_months = max(0, end_months - start_months)
    return duration_months, start, end


def infer_company_type(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in {"consulting", "services", "agency", "outsourcing"}):
        return "service"
    if any(word in lower for word in {"freelance", "contractor", "independent"}):
        return "freelance"
    if any(word in lower for word in {"startup", "seed", "series a", "early stage"}):
        return "startup"
    return "product"


def parse_work_experience(section_lines: list[str]) -> tuple[list[dict[str, Any]], list[tuple[tuple[int, int], tuple[int, int]]]]:
    if not section_lines:
        return [], []

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section_lines:
        if current and re.fullmatch(r"[A-Z][A-Z /&-]{2,}", line):
            blocks.append(current)
            current = [line]
            continue
        current.append(line)
        if len(current) >= 3 and parse_date_range(" ".join(current[-2:])):
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    experience = []
    ranges = []
    for block in blocks:
        joined = " | ".join(block)
        date_info = parse_date_range(joined)
        duration_months = date_info[0] if date_info else 0
        if date_info:
            ranges.append((date_info[1], date_info[2]))

        role = ""
        company = ""
        for line in block[:3]:
            if " at " in line.lower():
                left, right = re.split(r"\bat\b", line, maxsplit=1, flags=re.IGNORECASE)
                role = left.strip(" |-")
                company = right.strip(" |-")
                break
            if "|" in line:
                left, right = [part.strip() for part in line.split("|", 1)]
                if not parse_date_range(line):
                    role = left
                    company = right
                    break

        if not role and block:
            role = block[0]
        if not company and len(block) > 1:
            company = block[1] if not parse_date_range(block[1]) else ""

        description_lines = [line for line in block if line not in {role, company}]
        description = " ".join(description_lines)[:500]
        experience.append(
            {
                "company": company,
                "role": role,
                "duration_months": duration_months,
                "type": infer_company_type(joined),
                "description": description,
            }
        )

    cleaned = [item for item in experience if any([item["company"], item["role"], item["description"]])]
    return cleaned[:12], ranges


def parse_education(section_lines: list[str]) -> list[dict[str, str]]:
    degrees = []
    degree_keywords = (
        "b.tech",
        "btech",
        "m.tech",
        "mtech",
        "b.e",
        "be ",
        "m.e",
        "mba",
        "b.sc",
        "m.sc",
        "bachelor",
        "master",
        "phd",
        "doctor",
    )

    for line in section_lines:
        lower = line.lower()
        if any(keyword in lower for keyword in degree_keywords):
            year_match = re.search(r"(19|20)\d{2}", line)
            institution_match = re.search(r"(?:at|from)\s+(.+)$", line, flags=re.IGNORECASE)
            degrees.append(
                {
                    "degree": line[:120],
                    "institution": institution_match.group(1).strip() if institution_match else "",
                    "year": year_match.group(0) if year_match else "",
                    "tier": "",
                }
            )
    return degrees[:6]


# Lines containing these tokens are JD requirement context, not candidate self-description.
_JD_REQUIREMENT_MARKERS = re.compile(
    r"\b(required|requirements|minimum|preferred|ideal\s+candidate|"
    r"we\s+are\s+looking|must\s+have|you\s+should\s+have|responsibilities)\b",
    flags=re.IGNORECASE,
)


def infer_total_experience_years(text: str, work_experience: list[dict[str, Any]]) -> float:
    # Primary: sum of parsed date-range durations — grounded in explicit dates, not self-reported claims.
    total_months = sum(item.get("duration_months", 0) for item in work_experience)
    if total_months > 0:
        return round(total_months / 12, 1)

    # Fallback: scan text line-by-line, skipping lines that look like JD requirement text
    # so we don't fire on "Requirements: 5+ years of experience" embedded in the resume.
    for line in text.splitlines():
        if _JD_REQUIREMENT_MARKERS.search(line):
            continue
        m = re.search(
            r"(\d+(?:\.\d+)?)\+?\s+years?\s+(?:of\s+)?experience",
            line,
            flags=re.IGNORECASE,
        )
        if m:
            return round(float(m.group(1)), 1)

    return 0.0


def infer_career_gaps_months(ranges: list[tuple[tuple[int, int], tuple[int, int]]]) -> list[int]:
    if len(ranges) < 2:
        return []

    normalized = sorted(ranges, key=lambda item: (item[0][0], item[0][1]), reverse=True)
    gaps = []
    for index in range(len(normalized) - 1):
        current_start = normalized[index][0]
        previous_end = normalized[index + 1][1]
        current_start_months = current_start[0] * 12 + current_start[1]
        previous_end_months = previous_end[0] * 12 + previous_end[1]
        gap = max(0, current_start_months - previous_end_months)
        if gap > 2:
            gaps.append(gap)
    return gaps[:5]


def assess_resume_quality(resume_json: dict[str, Any], raw_text: str) -> dict[str, Any]:
    word_count = len(raw_text.split())
    populated_fields = 0
    tracked_fields = [
        resume_json.get("name"),
        resume_json.get("education"),
        resume_json.get("work_experience"),
        resume_json.get("skills"),
    ]
    for field in tracked_fields:
        if field:
            populated_fields += 1

    # text_extractable: did we actually get text out of the PDF?
    # Very low bar (30 words) — only fails for scanned image PDFs or truly corrupt files.
    # This is the gate used for evaluation inclusion.
    text_extractable = word_count >= 30

    # readable: did our section parser find meaningful structure?
    # This is a quality signal only — a PDF can be text_extractable but have low structure
    # if the section headers use non-standard formatting our parser didn't recognise.
    # Do NOT use this as an evaluation gate.
    readable = populated_fields >= 2 and word_count >= 80

    score = min(100, populated_fields * 20 + min(40, word_count // 20))
    reasons = []
    if not text_extractable:
        reasons.append("No extractable text — likely a scanned image PDF or corrupt file.")
    elif not readable:
        reasons.append(
            f"Text extracted ({word_count} words) but section parser found only "
            f"{populated_fields}/4 structured fields. Resume will still be evaluated "
            f"using raw text."
        )

    return {
        "text_extractable": text_extractable,
        "readable": readable,
        "score": score,
        "reasons": reasons,
    }


def parse_resume_text(text: str) -> dict[str, Any]:
    lines = split_lines(text)
    sections = sectionize_resume(lines)
    work_experience, date_ranges = parse_work_experience(sections.get("work_experience", []))
    resume_json = {
        "name": parse_name(lines),
        "education": parse_education(sections.get("education", [])),
        "work_experience": work_experience,
        "skills": parse_skills(sections.get("skills", [])),
        "certifications": parse_simple_list(sections.get("certifications", [])),
        "projects": parse_simple_list(sections.get("projects", [])),
        "publications": parse_simple_list(sections.get("publications", [])),
        "github_url": parse_github_url(text),
        "total_experience_years": infer_total_experience_years(text, work_experience),
        "career_gaps_months": infer_career_gaps_months(date_ranges),
    }
    return resume_json


def parse_resume_with_gemini(raw_text: str, file_name: str) -> dict[str, Any]:
    """Gemini-powered structured field extraction from raw resume text.

    Uses CALL_PARSE prompt to extract a richer JSON than the regex parser —
    including email, phone, location, LinkedIn, leadership descriptions, etc.

    Falls back to parse_resume_text() on any Gemini error so the upload
    always succeeds regardless of API availability.

    Returns the same schema as parse_resume_text() plus the extra fields.
    """
    from ats_poc.prompts import CALL_PARSE_SYSTEM, CALL_PARSE_TEMPLATE
    from ats_poc.gemini_client import run_structured_call

    try:
        parsed, _raw, _usage, _prompt = run_structured_call(
            model_name="gemini-2.5-flash-lite",
            system_instruction=CALL_PARSE_SYSTEM,
            template=CALL_PARSE_TEMPLATE,
            replacements={"RAW_TEXT": raw_text[:12000]},  # cap at ~3K tokens of text
            temperature=0.0,
        )
        if not isinstance(parsed, dict) or not parsed.get("work_experience") and not parsed.get("name"):
            # Gemini returned empty or malformed — fall back to regex
            print(f"[PARSE GEMINI] {file_name}: empty/malformed response, falling back to regex")
            return parse_resume_text(raw_text)

        # Normalise types — Gemini may return numbers as strings or None values
        resume = dict(parsed)
        try:
            resume["total_experience_years"] = float(resume.get("total_experience_years") or 0)
        except (TypeError, ValueError):
            resume["total_experience_years"] = 0.0

        gaps = resume.get("career_gaps_months") or []
        resume["career_gaps_months"] = [int(g) for g in gaps if isinstance(g, (int, float))]

        for field in ("skills", "certifications", "projects", "publications"):
            val = resume.get(field)
            resume[field] = [str(v) for v in val] if isinstance(val, list) else []

        work_exp = resume.get("work_experience") or []
        normalised_work = []
        for job in work_exp:
            if not isinstance(job, dict):
                continue
            normalised_work.append({
                "company": str(job.get("company") or "").strip(),
                "role": str(job.get("role") or "").strip(),
                "duration_months": int(job.get("duration_months") or 0),
                "type": str(job.get("type") or "other").strip().lower(),
                "description": str(job.get("description") or "").strip()[:500],
            })
        resume["work_experience"] = normalised_work

        edu = resume.get("education") or []
        normalised_edu = []
        for e in edu:
            if not isinstance(e, dict):
                continue
            normalised_edu.append({
                "degree": str(e.get("degree") or "").strip(),
                "institution": str(e.get("institution") or "").strip(),
                "year": str(e.get("year") or "").strip(),
                "tier": "",
            })
        resume["education"] = normalised_edu

        print(f"[PARSE GEMINI] {file_name}: extracted name={resume.get('name')!r}, "
              f"{len(normalised_work)} jobs, {len(normalised_edu)} edu entries")
        return resume

    except Exception as exc:
        print(f"[PARSE GEMINI] {file_name}: Gemini call failed ({exc}), falling back to regex")
        return parse_resume_text(raw_text)


def parse_resume_pdf(file_name: str, file_bytes: bytes, gemini_parse: bool = False) -> dict[str, Any]:
    """Parse a resume PDF.

    Args:
        file_name: Original filename (used for logging).
        file_bytes: Raw PDF bytes.
        gemini_parse: If True, use Gemini for structured field extraction
                      instead of the local regex parser. Requires configure_genai()
                      to have been called before invoking this function.
                      Falls back to regex on any Gemini error.
    """
    raw_text = extract_text_from_pdf(file_bytes)
    if gemini_parse and raw_text.strip():
        resume_json = parse_resume_with_gemini(raw_text, file_name)
    else:
        resume_json = parse_resume_text(raw_text)
    quality = assess_resume_quality(resume_json, raw_text)
    return {
        "file_name": file_name,
        "raw_text": raw_text,
        "resume_json": resume_json,
        "quality": quality,
    }

