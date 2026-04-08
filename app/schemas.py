from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class SessionOut(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    jd_text: Optional[str]
    jd_analysis: Optional[Any]
    base_criteria: Optional[Any]
    synthesized_config: Optional[Any]
    final_config: Optional[Any]
    preview_iteration_count: int
    extra_params_history: Any
    candidate_feedback_history: Any
    preview_field_results: Optional[Any]
    full_results: Optional[Any]
    full_eval_progress: Optional[Any] = None
    token_totals: Any

    model_config = {"from_attributes": True}


class ResumeOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    file_name: str
    resume_json: Any
    quality: Any
    keyword_score: float
    resume_lens: Optional[Any] = None
    is_exemplar: bool = False

    model_config = {"from_attributes": True}


class MarkExemplarRequest(BaseModel):
    is_exemplar: bool


class RubricFromExemplarsRequest(BaseModel):
    api_key: Optional[str] = None


class AnalyzeJDRequest(BaseModel):
    jd_text: str = Field(..., min_length=10)
    api_key: Optional[str] = None


class StartPreviewRequest(BaseModel):
    api_key: Optional[str] = None


class CandidateFeedback(BaseModel):
    file_name: str
    action: str
    reason: str

class RefineRequest(BaseModel):
    instructions: str = ""
    include: str = ""
    exclude: str = ""
    update_baseline: str = ""
    update_p0: str = ""
    api_key: Optional[str] = None
    candidate_feedback: List[CandidateFeedback] = Field(default_factory=list)


class AcceptRequest(BaseModel):
    api_key: Optional[str] = None


class ResumeDetailOut(BaseModel):
    file_name: str
    raw_text: Optional[str]
    resume_json: Any
    resume_lens: Any
    quality: Any

    model_config = {"from_attributes": True}
