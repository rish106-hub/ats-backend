# ATS Backend

FastAPI backend for the ATS Prompt Lab. Handles JD analysis, resume parsing, iterative preview scoring, and full candidate evaluation using the Gemini API.

## Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI + Uvicorn |
| Database | PostgreSQL (SQLAlchemy ORM) |
| AI | Google Gemini 2.0 Flash (`google-generativeai`) |
| PDF parsing | pdfplumber |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sessions` | List all sessions |
| POST | `/api/sessions` | Create a new session |
| GET | `/api/sessions/{id}` | Get session state |
| POST | `/api/sessions/{id}/jd` | Analyze JD (Call 1) |
| POST | `/api/sessions/{id}/resumes` | Upload + parse resume PDFs |
| GET | `/api/sessions/{id}/resumes` | List parsed resumes |
| POST | `/api/sessions/{id}/preview` | Run initial preview scoring |
| POST | `/api/sessions/{id}/preview/refine` | Add params + re-synthesize + re-score |
| POST | `/api/sessions/{id}/accept` | Accept criteria, run full evaluation (Call 3) |
| GET | `/health` | Health check |

## Local Setup

### Prerequisites

- Python 3.10+
- PostgreSQL running locally
- A [Gemini API key](https://aistudio.google.com/app/apikey)

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env:
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ats_db
# GOOGLE_API_KEY=your_key_here
```

### Create the database

```bash
psql -U postgres -c "CREATE DATABASE ats_db;"
```

Tables are created automatically on startup.

### Run

```bash
uvicorn app.main:app --reload --port 8000
```

API docs available at `http://localhost:8000/docs`

## Railway Deployment

### 1. Create a new Railway project

In the [Railway dashboard](https://railway.app), create a new project and add:
- A **PostgreSQL** plugin (Railway auto-provisions `DATABASE_URL`)
- A service from this GitHub repo

### 2. Set environment variables

In the Railway service settings → Variables, add:

| Variable | Value |
|----------|-------|
| `GOOGLE_API_KEY` | Your Gemini API key |

`DATABASE_URL` is automatically injected by the Railway PostgreSQL plugin.

### 3. Deploy

Railway detects the `Procfile` and runs:
```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Tables are created automatically on first startup.

### 4. Wire the frontend

Once deployed, copy the Railway public URL and set `NEXT_PUBLIC_API_URL` in the frontend deployment to point to it.
