import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, String, Integer, Text, DateTime, JSON, Float, LargeBinary
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime, default=datetime.utcnow)
    jd_text = Column(Text, nullable=True)
    jd_analysis = Column(JSON, nullable=True)
    base_criteria = Column(JSON, nullable=True)
    synthesized_config = Column(JSON, nullable=True)
    final_config = Column(JSON, nullable=True)
    status = Column(String(50), default="created")
    preview_iteration_count = Column(Integer, default=0)
    extra_params_history = Column(JSON, default=list)
    candidate_feedback_history = Column(JSON, default=list)
    preview_field_results = Column(JSON, nullable=True)
    preview_seen_files = Column(JSON, default=list)
    full_results = Column(JSON, nullable=True)
    full_eval_progress = Column(JSON, nullable=True)
    token_totals = Column(JSON, default=dict)


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    file_name = Column(String(255))
    resume_json = Column(JSON)
    raw_text = Column(Text)
    quality = Column(JSON)
    keyword_score = Column(Float, default=0.0)
    resume_lens = Column(JSON, nullable=True)
    pdf_bytes = Column(LargeBinary, nullable=True)
    is_exemplar = Column(Boolean, default=False, nullable=False, server_default="false")
    created_at = Column(DateTime, default=datetime.utcnow)


class PreviewIteration(Base):
    __tablename__ = "preview_iterations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    iteration_number = Column(Integer)
    extra_params = Column(JSON)
    field_results = Column(JSON)
    synthesized_config_snapshot = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
