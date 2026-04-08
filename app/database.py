from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ats_db")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401 — registers all models
    Base.metadata.create_all(bind=engine)
    # Additive migrations — safe to run on every startup
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS resume_lens JSON"
        ))
        conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS candidate_feedback_history JSON"
        ))
        conn.execute(text(
            "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS pdf_bytes BYTEA"
        ))
        conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS full_eval_progress JSON"
        ))
        conn.execute(text(
            "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS is_exemplar BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.commit()
