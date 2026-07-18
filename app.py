"""Adeeb AI Meeting Agent - self-hosted, human-reviewed interview meetings.

Core guarantees:
- The HR dashboard creates a private meeting link without collecting a candidate name.
- The candidate identifies themselves after joining the link and explicitly consents to audio processing.
- English audio is transcribed locally with faster-whisper; Urdu can use Groq multilingual Whisper with a local fallback.
- The agent can answer company questions only from approved local/Google Sheet RAG sources.
- Quality review and consented corrections build a local vocabulary-adaptation loop. This is not a claim of
  model fine-tuning or a guaranteed accuracy percentage; it produces measurable WER metrics instead.
"""
from __future__ import annotations

import asyncio
import csv
import hmac
import hashlib
import io
import importlib.util
import json
import logging
import mimetypes
import math
import os
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from faster_whisper import WhisperModel
from identity import IdentityProtector, format_cnic, mask_cnic

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def resolve_data_dir() -> Path:
    """Keep all candidate records beside the currently running project by default.

    A relative ADEEB_DATA_DIR is resolved from this app.py directory, never from the
    terminal's working directory. This prevents records from silently moving when the
    project is launched from Downloads, another drive, VS Code, or a Git clone.
    """
    configured = os.getenv("ADEEB_DATA_DIR", "").strip()
    if not configured:
        return (BASE_DIR / "data").resolve()
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATA_DIR = resolve_data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"  # legacy/shared upload location
CANDIDATE_DATA_ROOT = DATA_DIR / "candidates"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
DB_PATH = DATA_DIR / "interviews.sqlite3"
QUESTIONS_PATH = BASE_DIR / "questions.json"
KNOWLEDGE_PATH = BASE_DIR / "company_knowledge.md"
RAG_SETTINGS_PATH = DATA_DIR / "rag_settings.json"
RAG_CACHE_PATH = DATA_DIR / "google_sheet_cache.json"
PDF_RAG_CACHE_PATH = DATA_DIR / "pdf_rag_cache.json"
AGENT_TRANSLATION_CACHE_PATH = DATA_DIR / "agent_translation_cache.json"
PDF_KNOWLEDGE_DIR = KNOWLEDGE_DIR / "pdfs"
LEARNED_HINTS_PATH = DATA_DIR / "learned_hints.json"
DOMAIN_HINTS_PATH = KNOWLEDGE_DIR / "urdu_english_domain_hints.txt"
SEED_FAQ_PATH = KNOWLEDGE_DIR / "adeeb_faq_seed.csv"
IDENTITY_SECRETS_PATH = DATA_DIR / ".identity_secrets.json"
UNIVERSAL_JOIN_PATH = "/join"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
PDF_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_local_storage() -> None:
    """Import the two common legacy locations without storing machine-specific paths."""
    legacy_db = BASE_DIR / "interviews.sqlite3"
    if not DB_PATH.exists() and legacy_db.is_file():
        shutil.copy2(legacy_db, DB_PATH)
    legacy_uploads = BASE_DIR / "uploads"
    if legacy_uploads.is_dir():
        for source in legacy_uploads.iterdir():
            if source.is_file():
                target = UPLOAD_DIR / source.name
                if not target.exists():
                    shutil.copy2(source, target)


migrate_legacy_local_storage()
identity_protector = IdentityProtector(DATA_DIR)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Live transcription is optimised for turn-taking. The optional final pass is slower and runs after a meeting.
WHISPER_LIVE_MODEL = os.getenv("WHISPER_LIVE_MODEL", os.getenv("WHISPER_MODEL", "turbo"))
WHISPER_FINAL_MODEL = os.getenv("WHISPER_FINAL_MODEL", "large-v3")
# Optional CTranslate2 model path exported after LoRA fine-tuning. Leave blank to use the live/final model.
# Urdu uses the full multilingual model by default. The previous turbo-only path was
# fast in English but often confused short Urdu answers and code-switched technical terms.
WHISPER_URDU_MODEL = os.getenv("WHISPER_URDU_MODEL", "turbo").strip()
WHISPER_HINDI_MODEL = os.getenv("WHISPER_HINDI_MODEL", "").strip()
URDU_WHISPER_BEAM_SIZE = min(10, max(3, int(os.getenv("URDU_WHISPER_BEAM_SIZE", "8"))))
URDU_SECOND_PASS_ON_LOW_CONFIDENCE = os.getenv("URDU_SECOND_PASS_ON_LOW_CONFIDENCE", "false").lower() == "true"
URDU_SECOND_PASS_MIN_CONFIDENCE = min(0.99, max(0.2, float(os.getenv("URDU_SECOND_PASS_MIN_CONFIDENCE", "0.86"))))
URDU_DIRECT_AUDIO_TRANSLATION = os.getenv("URDU_DIRECT_AUDIO_TRANSLATION", "true").lower() == "true"
URDU_TRANSLATION_MODE = os.getenv("URDU_TRANSLATION_MODE", "ollama_then_whisper").strip().lower()
TRANSLATION_TIMEOUT_SECONDS = min(90, max(8, int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "35"))))
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_THREADS = max(0, int(os.getenv("WHISPER_CPU_THREADS", "0")))
WHISPER_NUM_WORKERS = max(1, int(os.getenv("WHISPER_NUM_WORKERS", "1")))
WHISPER_BEAM_SIZE = min(8, max(1, int(os.getenv("WHISPER_BEAM_SIZE", "3"))))
FINAL_WHISPER_BEAM_SIZE = min(10, max(1, int(os.getenv("FINAL_WHISPER_BEAM_SIZE", "5"))))
ENABLE_FINAL_RETRANSCRIBE = os.getenv("ENABLE_FINAL_RETRANSCRIBE", "false").lower() == "true"
# Candidate audio is retained for HR replay by default after mandatory recording consent.
# Set this false only when your documented retention policy requires audio deletion after transcription.
RETAIN_CANDIDATE_AUDIO = os.getenv("RETAIN_CANDIDATE_AUDIO", "true").lower() == "true"
AUDIO_RETENTION_DAYS = max(0, int(os.getenv("AUDIO_RETENTION_DAYS", "0")))
PLAINTEXT_CANDIDATE_DATA = os.getenv("PLAINTEXT_CANDIDATE_DATA", "true").lower() == "true"
VAD_MIN_SILENCE_MS = min(2500, max(100, int(os.getenv("VAD_MIN_SILENCE_MS", "650"))))
VAD_SPEECH_PAD_MS = min(1200, max(0, int(os.getenv("VAD_SPEECH_PAD_MS", "360"))))
AUTO_LANGUAGE_RETRY = os.getenv("AUTO_LANGUAGE_RETRY", "true").lower() == "true"
AUTO_LANGUAGE_RETRY_MIN_CONFIDENCE = min(0.95, max(0.2, float(os.getenv("AUTO_LANGUAGE_RETRY_MIN_CONFIDENCE", "0.62"))))

OLLAMA_INTERACTION_MODEL = os.getenv("OLLAMA_INTERACTION_MODEL", "qwen2.5:3b-instruct")
OLLAMA_SUMMARY_MODEL = os.getenv("OLLAMA_SUMMARY_MODEL", "qwen2.5:3b-instruct")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
INTERACTION_OLLAMA_TIMEOUT_SECONDS = min(60, max(3, int(os.getenv("INTERACTION_OLLAMA_TIMEOUT_SECONDS", "18"))))
OLLAMA_TIMEOUT_SECONDS = min(120, max(5, int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "70"))))

# Optional API-based brain and voice. These are optional so the project still runs free/local.
# Recommended free API brain: Groq. Fallbacks: Gemini, OpenRouter, then local Ollama.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").strip().lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
# Urdu live ASR can use Groq's multilingual Whisper endpoint when the same Groq key
# is already configured for the LLM brain. This removes the long CPU wait that caused
# the browser to time out and start listening again before Adeeb could answer.
URDU_ASR_PROVIDER = os.getenv("URDU_ASR_PROVIDER", "auto").strip().lower()
GROQ_STT_ENABLED = os.getenv("GROQ_STT_ENABLED", "true").lower() == "true"
# When enabled, the same Groq Whisper endpoint is used for English, Urdu, Hindi,
# and mixed answers. This removes most CPU load from slower Windows computers while
# keeping the complete local faster-whisper path as an offline fallback.
GROQ_STT_ALL_LANGUAGES = os.getenv("GROQ_STT_ALL_LANGUAGES", "true").lower() == "true"
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip()
GROQ_STT_ACCURACY_MODEL = os.getenv("GROQ_STT_ACCURACY_MODEL", "whisper-large-v3").strip()
GROQ_STT_TIMEOUT_SECONDS = min(120, max(10, int(os.getenv("GROQ_STT_TIMEOUT_SECONDS", "45"))))
URDU_LLM_TRANSCRIPT_REPAIR = os.getenv("URDU_LLM_TRANSCRIPT_REPAIR", "true").lower() == "true"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "http://localhost:8000").strip()
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "Adeeb AI Meeting Agent").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GENERIC_AGENT_QUESTIONS = os.getenv("GENERIC_AGENT_QUESTIONS", "true").lower() == "true"
AGENT_COMMAND_TIMEOUT_SECONDS = min(12, max(2, int(os.getenv("AGENT_COMMAND_TIMEOUT_SECONDS", "5"))))
AGENT_TRANSLATION_TIMEOUT_SECONDS = min(12, max(2, int(os.getenv("AGENT_TRANSLATION_TIMEOUT_SECONDS", "5"))))
MAX_FIELD_RELEVANT_QUESTIONS = min(5, max(0, int(os.getenv("MAX_FIELD_RELEVANT_QUESTIONS", "3"))))
MAX_PROJECT_RELEVANT_QUESTIONS = min(5, max(0, int(os.getenv("MAX_PROJECT_RELEVANT_QUESTIONS", "3"))))
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL").strip()
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
ELEVENLABS_OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip()
ELEVENLABS_VOICE_STABILITY = min(1.0, max(0.0, float(os.getenv("ELEVENLABS_VOICE_STABILITY", "0.45"))))
ELEVENLABS_VOICE_SIMILARITY = min(1.0, max(0.0, float(os.getenv("ELEVENLABS_VOICE_SIMILARITY", "0.75"))))
# Free server-side voice fallback. Edge TTS is especially useful on mobile devices that
# do not include a native Urdu voice and block delayed browser autoplay.
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "auto").strip().lower()
EDGE_TTS_ENABLED = os.getenv("EDGE_TTS_ENABLED", "true").lower() == "true"
EDGE_TTS_VOICE_EN = os.getenv("EDGE_TTS_VOICE_EN", "en-US-AriaNeural").strip()
EDGE_TTS_VOICE_UR = os.getenv("EDGE_TTS_VOICE_UR", "ur-PK-UzmaNeural").strip()
EDGE_TTS_VOICE_HI = os.getenv("EDGE_TTS_VOICE_HI", "hi-IN-SwaraNeural").strip()
EDGE_TTS_RATE = os.getenv("EDGE_TTS_RATE", "+0%").strip()
EDGE_TTS_VOLUME = os.getenv("EDGE_TTS_VOLUME", "+0%").strip()
TTS_TIMEOUT_SECONDS = min(45, max(5, int(os.getenv("TTS_TIMEOUT_SECONDS", "25"))))
ADAPT_PLANNED_QUESTIONS = os.getenv("ADAPT_PLANNED_QUESTIONS", "true").lower() == "true"
QUESTION_REPEAT_SIMILARITY = min(0.98, max(0.55, float(os.getenv("QUESTION_REPEAT_SIMILARITY", "0.78"))))
MAX_FOLLOWUPS = min(2, max(0, int(os.getenv("MAX_FOLLOWUPS_PER_QUESTION", "1"))))
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_MB", "25")) * 1024 * 1024
RAG_ALLOW_SENSITIVE = os.getenv("RAG_ALLOW_SENSITIVE", "false").lower() == "true"
MAX_PDF_UPLOAD_MB = min(80, max(1, int(os.getenv("MAX_PDF_UPLOAD_MB", "15"))))
MAX_PDF_PAGES = min(250, max(1, int(os.getenv("MAX_PDF_PAGES", "60"))))
PDF_CHUNK_CHARS = min(3000, max(700, int(os.getenv("PDF_CHUNK_CHARS", "1400"))))
PDF_CHUNK_OVERLAP = min(600, max(0, int(os.getenv("PDF_CHUNK_OVERLAP", "180"))))
# LLM-driven mode requires every answer to be transcribed before Adeeb chooses the next action.
# The legacy background queue remains available only for backwards compatibility and is disabled by default.
LLM_DRIVEN_INTERVIEW = os.getenv("LLM_DRIVEN_INTERVIEW", "true").lower() == "true"
# Staged mode queues the first four fixed answers, then transcribes the final
# project answer immediately so the LLM can ask exactly two evidence-based follow-ups.
STAGED_INTERVIEW_FLOW = os.getenv("STAGED_INTERVIEW_FLOW", "true").lower() == "true"
FAST_ANSWER_QUEUE = os.getenv("FAST_ANSWER_QUEUE", "true").lower() == "true"
QUEUE_WAIT_POLL_SECONDS = min(3.0, max(0.25, float(os.getenv("QUEUE_WAIT_POLL_SECONDS", "0.50"))))
QUEUE_MAX_WAIT_SECONDS = max(60, int(os.getenv("QUEUE_MAX_WAIT_SECONDS", "7200")))
STAGED_TRANSCRIPT_WAIT_SECONDS = min(300, max(20, int(os.getenv("STAGED_TRANSCRIPT_WAIT_SECONDS", "150"))))
PRELOAD_LOCAL_WHISPER = os.getenv("PRELOAD_LOCAL_WHISPER", "false").lower() == "true"

LANGUAGE_NAMES = {"en": "English", "ur": "Urdu", "hi": "Hindi"}
ALLOWED_INPUT_LANGUAGES = {"auto", "en", "ur", "hi"}
COMMON_TRAINING_WORDS = {
    "the", "and", "for", "with", "that", "this", "have", "from", "your", "about", "would", "could", "should",
    "into", "when", "where", "what", "which", "they", "them", "then", "than", "were", "been", "will", "also",
    "can", "are", "was", "you", "our", "their", "its", "not", "but", "very", "just", "more", "some", "have",
}
logger = logging.getLogger("adeeb_meeting_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Adeeb AI Meeting Agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def root_favicon() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/apple-touch-icon.png", include_in_schema=False)
def root_apple_icon() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "apple-touch-icon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/site.webmanifest", include_in_schema=False)
def site_manifest() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "site.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=86400"})


@app.middleware("http")
async def browser_safety_headers(request: Request, call_next):
    """Avoid stale interview pages and add lightweight browser security headers."""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if request.url.path.startswith("/api/") or "text/html" in content_type:
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(self), autoplay=(self)")
    return response
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
security = HTTPBasic(auto_error=False)

_whisper_models: dict[str, WhisperModel] = {}
_model_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_transcription_lock = asyncio.Lock()
# Queue jobs are serialized to avoid multiple Whisper models competing for CPU/RAM
# on a slower laptop. Groq remains fast, and local fallback stays stable.
_background_transcription_semaphore = asyncio.Semaphore(1)
_background_tasks: set[asyncio.Task[Any]] = set()


def start_background_task(coro: Any) -> None:
    """Keep a reference to a background coroutine so it is not garbage collected early."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


DEFAULT_KNOWLEDGE_BASE = """# Approved interview information

This local document is one of Adeeb's approved RAG sources. Add only information that candidates may safely receive during an interview.

## Interview process
- This meeting is recorded and transcribed into English for human review.
- Adeeb AI Meeting Agent does not make employment decisions.
- A recruiter or hiring manager makes the final decision and can clarify information that is not in the approved sources.

## RAG safety
- The bundled FAQ CSV is available as a local seed source.
- Sensitive payment and account records are excluded by default from candidate answers.
- Do not add passwords, national identity numbers, private client information, bank accounts, API keys, or confidential data.
"""


# ---------- Generic helpers ----------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text


def safe_json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def public_candidate_name(value: Any) -> str:
    return clean_text(value) or "Waiting for candidate"


def safe_admin_session_dict(value: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Return session metadata without exposing encrypted tokens or hashes to the browser."""
    item = dict(value)
    for key in (
        "father_name_encrypted", "cnic_encrypted", "cnic_hash",
        "resume_code_hash", "resume_code_encrypted",
    ):
        item.pop(key, None)
    return item


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    known = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in known:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ---------- Database ----------
def init_database() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                candidate_name TEXT NOT NULL DEFAULT '',
                role_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                question_snapshot TEXT NOT NULL,
                final_summary TEXT,
                summary_source TEXT,
                scheduled_for TEXT,
                active_prompt TEXT NOT NULL DEFAULT '',
                follow_up_count INTEGER NOT NULL DEFAULT 0,
                quality_consent INTEGER NOT NULL DEFAULT 0,
                candidate_identified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                question_index INTEGER NOT NULL,
                question_category TEXT,
                question_text TEXT NOT NULL,
                answer_original TEXT NOT NULL,
                answer_english TEXT NOT NULL,
                model_english TEXT,
                reviewed_text TEXT,
                detected_language TEXT,
                created_at TEXT NOT NULL,
                candidate_edited INTEGER NOT NULL DEFAULT 0,
                processing_ms INTEGER,
                transcription_status TEXT NOT NULL DEFAULT 'ready',
                audio_path TEXT,
                spoken_language TEXT,
                processing_error TEXT,
                transcript_ready_at TEXT,
                is_final INTEGER NOT NULL DEFAULT 1,
                model_version TEXT,
                quality_status TEXT NOT NULL DEFAULT 'unreviewed',
                word_error_rate REAL,
                final_pass_status TEXT NOT NULL DEFAULT 'not_requested',
                UNIQUE(session_id, question_id),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                question_id TEXT,
                speaker TEXT NOT NULL,
                kind TEXT NOT NULL,
                text_en TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processing_ms INTEGER,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS quality_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                answer_id INTEGER NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                corrected_text TEXT NOT NULL,
                reviewer_note TEXT,
                word_error_rate REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(answer_id) REFERENCES answers(id),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session_id, question_index);
            """
        )
        # Safe upgrade path from earlier project versions.
        upgrades = {
            "sessions": {
                "quality_consent": "INTEGER NOT NULL DEFAULT 0",
                "candidate_identified_at": "TEXT",
                "active_prompt": "TEXT NOT NULL DEFAULT ''",
                "follow_up_count": "INTEGER NOT NULL DEFAULT 0",
                "father_name_encrypted": "TEXT",
                "cnic_encrypted": "TEXT",
                "cnic_hash": "TEXT",
                "resume_code_hash": "TEXT",
                "resume_code_encrypted": "TEXT",
                "identity_verified_at": "TEXT",
                "candidate_language": "TEXT",
            },
            "answers": {
                "model_english": "TEXT",
                "reviewed_text": "TEXT",
                "model_version": "TEXT",
                "quality_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
                "word_error_rate": "REAL",
                "final_pass_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "transcription_status": "TEXT NOT NULL DEFAULT 'ready'",
                "audio_path": "TEXT",
                "spoken_language": "TEXT",
                "audio_mime_type": "TEXT",
                "audio_bytes": "INTEGER",
                "audio_duration_ms": "INTEGER",
                "processing_error": "TEXT",
                "transcript_ready_at": "TEXT",
                "is_final": "INTEGER NOT NULL DEFAULT 1",
            },
        }
        for table, fields in upgrades.items():
            for name, definition in fields.items():
                ensure_column(connection, table, name, definition)
        # One CNIC is allowed to create only one record. NULL/blank legacy rows remain valid.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_cnic_hash_unique ON sessions(cnic_hash) WHERE cnic_hash IS NOT NULL AND cnic_hash <> ''")
        connection.execute("UPDATE answers SET model_english = answer_english WHERE model_english IS NULL OR model_english = ''")
        connection.execute("UPDATE answers SET quality_status = 'unreviewed' WHERE quality_status IS NULL OR quality_status = ''")
        connection.execute("UPDATE answers SET final_pass_status = 'not_requested' WHERE final_pass_status IS NULL OR final_pass_status = ''")


# ---------- Settings / authentication ----------
def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not ADMIN_PASSWORD:
        return ADMIN_USERNAME
    if credentials is None:
        raise HTTPException(status_code=401, detail="Admin authentication required.", headers={"WWW-Authenticate": 'Basic realm="Adeeb AI Meeting Agent Admin"'})
    valid = hmac.compare_digest(credentials.username, ADMIN_USERNAME) and hmac.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect admin credentials.", headers={"WWW-Authenticate": 'Basic realm="Adeeb AI Meeting Agent Admin"'})
    return credentials.username


def current_request_origin(request: Request) -> str:
    """Return the origin actually used for this request.

    Navigation APIs use relative paths, so an expired TryCloudflare hostname can never
    be retained in the database, .env, or candidate records. This helper is only for
    displaying/copying the currently active universal join link.
    """
    return str(request.base_url).rstrip("/")


def default_rag_settings() -> dict[str, Any]:
    return {
        "source_mode": "local_csv",
        "google_sheet_csv_url": "",
        "auto_sync_minutes": 30,
        "last_synced_at": "",
        "last_sync_status": "Using bundled local FAQ CSV.",
        "last_record_count": 0,
    }


def load_rag_settings() -> dict[str, Any]:
    data = safe_json_loads(RAG_SETTINGS_PATH.read_text(encoding="utf-8") if RAG_SETTINGS_PATH.exists() else "", {})
    settings = default_rag_settings()
    if isinstance(data, dict):
        settings.update({key: value for key, value in data.items() if key in settings})
    return settings


def save_rag_settings(settings: dict[str, Any]) -> dict[str, Any]:
    clean = default_rag_settings()
    clean.update({key: settings.get(key, clean[key]) for key in clean})
    clean["auto_sync_minutes"] = min(1440, max(5, int(clean["auto_sync_minutes"] or 30)))
    clean["source_mode"] = clean["source_mode"] if clean["source_mode"] in {"local_csv", "google_sheet"} else "local_csv"
    temp = RAG_SETTINGS_PATH.with_suffix(".tmp")
    temp.write_text(json_dump(clean) + "\n", encoding="utf-8")
    temp.replace(RAG_SETTINGS_PATH)
    return clean


# ---------- Questions ----------
def load_questions() -> dict[str, Any]:
    if not QUESTIONS_PATH.exists():
        raise RuntimeError("questions.json is missing.")
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    validate_questions(payload)
    return payload


def validate_questions(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Question file must contain one JSON object.")
    for key in ("bot_name", "welcome_message", "closing_message"):
        if not normalize_space(payload.get(key)):
            raise ValueError(f"{key.replace('_', ' ').title()} is required.")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Add at least one interview question.")
    seen: set[str] = set()
    for index, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            raise ValueError(f"Question {index} must be an object.")
        question_id = str(question.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", question_id):
            raise ValueError(f"Question {index} needs an ID containing only letters, numbers, _ or -.")
        if question_id in seen:
            raise ValueError(f"Question ID '{question_id}' is repeated.")
        seen.add(question_id)
        if len(normalize_space(question.get("text"))) < 6:
            raise ValueError(f"Question {index} needs a longer spoken question.")
        seconds = int(question.get("max_seconds", 90))
        if seconds < 15 or seconds > 900:
            raise ValueError(f"Question {index} max seconds must be between 15 and 900.")
        followups = int(question.get("max_followups", payload.get("default_max_followups", MAX_FOLLOWUPS)))
        if followups < 0 or followups > 2:
            raise ValueError(f"Question {index} max follow-ups must be between 0 and 2.")
        transcription_mode = str(question.get("transcription_mode", "immediate")).strip().lower()
        if transcription_mode not in {"background", "immediate"}:
            raise ValueError(f"Question {index} transcription_mode must be background or immediate.")
        if question.get("followup_plan") is not None and not isinstance(question.get("followup_plan"), list):
            raise ValueError(f"Question {index} followup_plan must be a list.")


def to_phrase_list(raw: Any) -> list[str]:
    candidates = raw if isinstance(raw, list) else re.split(r"[,\n;]", str(raw or ""))
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        phrase = normalize_space(candidate)
        if 1 <= len(phrase) <= 100 and phrase.casefold() not in seen:
            seen.add(phrase.casefold())
            result.append(phrase)
    return result[:100]


# ---------- Sessions / answers ----------
def session_or_404(session_id: str) -> sqlite3.Row:
    with db() as connection:
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview meeting was not found.")
    return row


def get_answers(session_id: str) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT * FROM answers WHERE session_id = ? ORDER BY question_index ASC", (session_id,)).fetchall()
    return [dict(row) for row in rows]


def get_turns(session_id: str) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()
    return [dict(row) for row in rows]


def append_turn(session_id: str, question_id: str | None, speaker: str, kind: str, text_en: str, processing_ms: int | None = None) -> None:
    text = clean_text(text_en)
    if not text:
        return
    with db() as connection:
        connection.execute(
            "INSERT INTO turns (session_id, question_id, speaker, kind, text_en, created_at, processing_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, question_id, speaker, kind, text, utc_now(), processing_ms),
        )
    sync_candidate_plaintext(session_id)


def count_agent_turns(session_id: str, kinds: set[str]) -> int:
    if not kinds:
        return 0
    placeholders = ",".join("?" for _ in kinds)
    with db() as connection:
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM turns WHERE session_id = ? AND speaker = 'agent' AND kind IN ({placeholders})",
            (session_id, *sorted(kinds)),
        ).fetchone()
    return int(row["count"] if row else 0)


def field_question_limit_reached(session_id: str) -> bool:
    return count_agent_turns(session_id, {"role_specific_question", "skill_follow_up"}) >= MAX_FIELD_RELEVANT_QUESTIONS


def project_question_limit_reached(session_id: str) -> bool:
    return count_agent_turns(session_id, {"follow_up", "clarify_answer"}) >= MAX_PROJECT_RELEVANT_QUESTIONS


def get_next_question(session: sqlite3.Row) -> tuple[int, dict[str, Any] | None]:
    snapshot = safe_json_loads(session["question_snapshot"], {})
    questions = snapshot.get("questions", [])
    with db() as connection:
        rows = connection.execute(
            "SELECT question_id FROM answers WHERE session_id = ? AND COALESCE(is_final, 1) = 1", (session["id"],)
        ).fetchall()
    completed_ids = {row["question_id"] for row in rows}
    for index, question in enumerate(questions):
        if question.get("id") not in completed_ids:
            return index, question
    return len(questions), None


LLM_QUESTION_PREFIX = "__LLM_QUESTION__:"


def active_prompt(session: sqlite3.Row, base_question: dict[str, Any] | None) -> dict[str, Any] | None:
    if base_question is None:
        return None
    prompt = normalize_space(session["active_prompt"])
    result = dict(base_question)
    result["original_text"] = str(base_question.get("text", ""))
    if prompt:
        if prompt.startswith(LLM_QUESTION_PREFIX):
            result["text"] = normalize_space(prompt[len(LLM_QUESTION_PREFIX):])
            result["prompt_type"] = "question"
            result["llm_generated"] = True
        else:
            result["text"] = prompt
            result["prompt_type"] = "follow_up"
    else:
        if str(base_question.get("id")) == "role_specific":
            role_name = normalize_space(session["role_name"] if "role_name" in session.keys() else "")
            if role_name:
                result["text"] = (
                    f"For the {role_name} role, describe one relevant tool, method, or project, "
                    "and explain how you used it in practice."
                )
        result["prompt_type"] = "question"
    return result


def pending_transcription_count(session_id: str) -> int:
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM answers WHERE session_id = ? AND transcription_status IN ('queued', 'processing')",
            (session_id,),
        ).fetchone()
    return int(row["count"] if row else 0)


def state_payload(session_id: str) -> dict[str, Any]:
    session = session_or_404(session_id)
    snapshot = safe_json_loads(session["question_snapshot"], {})
    index, base_question = get_next_question(session)
    total = len(snapshot.get("questions", []))
    pending = pending_transcription_count(session_id)
    return {
        "session_id": session_id,
        "candidate_name": clean_text(session["candidate_name"]),
        "candidate_identified": bool(normalize_space(session["candidate_name"])),
        "identity_registered": bool(normalize_space(session["cnic_hash"]) if "cnic_hash" in session.keys() else False),
        "quality_consent": bool(session["quality_consent"]),
        "candidate_language": str(session["candidate_language"] or "auto") if "candidate_language" in session.keys() else "auto",
        "status": session["status"],
        "bot_name": snapshot.get("bot_name", "Adeeb AI Meeting Agent"),
        "meeting_title": snapshot.get("meeting_title", snapshot.get("interview_title", "Adeeb AI Meeting")),
        "welcome_message": snapshot.get("welcome_message", ""),
        "closing_message": snapshot.get("closing_message", "Thank you for your time."),
        "total_questions": total,
        "completed_questions": index if base_question is not None else total,
        "current_index": index,
        "question": active_prompt(session, base_question),
        "follow_up_count": int(session["follow_up_count"] or 0),
        "fast_answer_mode": FAST_ANSWER_QUEUE,
        "staged_interview_flow": STAGED_INTERVIEW_FLOW,
        "llm_driven_interview": LLM_DRIVEN_INTERVIEW,
        "pending_transcriptions": pending,
    }


def candidate_audio_should_be_kept(session: sqlite3.Row) -> bool:
    """Keep interview audio for authenticated HR replay after recording consent.

    Quality-consent controls only whether reviewed text contributes to learning hints;
    it does not control the operational HR recording archive.
    """
    return RETAIN_CANDIDATE_AUDIO


def portable_storage_path(path: str | Path) -> str:
    """Store app files as project-relative paths so records survive folder/computer moves."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        try:
            return resolved.relative_to(DATA_DIR.resolve()).as_posix()
        except ValueError:
            return resolved.name


def resolve_stored_path(value: str | Path) -> Path:
    """Resolve old absolute paths and new portable paths against the current project."""
    raw = Path(str(value))
    if not raw.is_absolute():
        candidate = (BASE_DIR / raw).resolve()
        if candidate.exists():
            return candidate
        candidate = (DATA_DIR / raw).resolve()
        if candidate.exists():
            return candidate
    elif raw.exists():
        return raw.resolve()
    # Old databases may contain D:\... or C:\... absolute upload paths.
    # On another computer, recover the recording by filename from either supported archive.
    legacy = (UPLOAD_DIR / raw.name).resolve()
    if legacy.exists():
        return legacy
    try:
        match = next(CANDIDATE_DATA_ROOT.rglob(raw.name))
        return match.resolve()
    except (StopIteration, OSError):
        return legacy


def is_safe_upload_path(value: str | Path) -> Path | None:
    """Return an existing recording only from an approved project data directory."""
    try:
        path = resolve_stored_path(value)
        for root in (UPLOAD_DIR.resolve(), CANDIDATE_DATA_ROOT.resolve()):
            try:
                path.relative_to(root)
                return path if path.is_file() else None
            except ValueError:
                continue
        return None
    except OSError:
        return None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def candidate_plain_identity(session: sqlite3.Row) -> dict[str, str]:
    father = identity_protector.decrypt(session["father_name_encrypted"]) if "father_name_encrypted" in session.keys() else ""
    cnic_digits = identity_protector.decrypt(session["cnic_encrypted"]) if "cnic_encrypted" in session.keys() else ""
    resume = identity_protector.decrypt(session["resume_code_encrypted"]) if "resume_code_encrypted" in session.keys() else ""
    try:
        cnic = format_cnic(cnic_digits) if cnic_digits else ""
    except ValueError:
        cnic = clean_text(cnic_digits)
    return {
        "full_name": clean_text(session["candidate_name"]),
        "father_or_guardian": clean_text(father),
        "cnic": cnic,
        "cnic_digits": re.sub(r"\D", "", cnic_digits),
        "resume_code": clean_text(resume),
    }


def candidate_folder_for_session(session_id: str, *, create: bool = True) -> Path | None:
    try:
        session = session_or_404(session_id)
    except HTTPException:
        return None
    identity = candidate_plain_identity(session)
    cnic = identity.get("cnic") or ""
    if not cnic:
        return None
    folder = CANDIDATE_DATA_ROOT / cnic
    if create:
        (folder / "audio").mkdir(parents=True, exist_ok=True)
    return folder


def sync_candidate_plaintext(session_id: str) -> None:
    """Write a human-readable, CNIC-separated mirror requested by the project owner.

    The SQLite database remains the operational source of truth so existing dashboard,
    resume, deletion, and uniqueness features remain stable. The mirror is deliberately
    unencrypted and must be protected like HR records.
    """
    if not PLAINTEXT_CANDIDATE_DATA:
        return
    try:
        session = session_or_404(session_id)
        folder = candidate_folder_for_session(session_id)
        if folder is None:
            return
        identity = candidate_plain_identity(session)
        answers = get_answers(session_id)
        turns = get_turns(session_id)
        record = {
            "warning": "CONFIDENTIAL HR DATA - this folder contains unencrypted identity and interview records.",
            "identity": identity,
            "meeting": safe_admin_session_dict(session),
            "answers": answers,
            "conversation": turns,
            "updated_at_utc": utc_now(),
        }
        atomic_write_text(folder / "candidate_record.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        identity_lines = [
            "CONFIDENTIAL HR DATA - UNENCRYPTED",
            "",
            f"Candidate: {identity['full_name']}",
            f"Father / guardian: {identity['father_or_guardian']}",
            f"CNIC: {identity['cnic']}",
            f"Resume code: {identity['resume_code'] or 'Not available'}",
            f"Status: {session['status']}",
            f"Created: {session['created_at']}",
            f"Started: {session['started_at'] or ''}",
            f"Completed: {session['completed_at'] or ''}",
        ]
        atomic_write_text(folder / "identity_and_status.txt", "\n".join(identity_lines).strip() + "\n")
        if answers:
            atomic_write_text(folder / "english_transcript.txt", build_transcript(session, answers) + "\n")
            native_lines: list[str] = []
            for number, answer in enumerate(answers, 1):
                native_lines.extend([
                    f"QUESTION {number}: {answer.get('question_text', '')}",
                    f"ORIGINAL SPOKEN TRANSCRIPT: {answer.get('answer_original', '')}",
                    f"ENGLISH MEANING: {answer.get('answer_english', '')}",
                    "",
                ])
            atomic_write_text(folder / "original_and_english_answers.txt", "\n".join(native_lines).strip() + "\n")
        else:
            atomic_write_text(folder / "english_transcript.txt", "No finalized answer transcript yet.\n")
            atomic_write_text(folder / "original_and_english_answers.txt", "No finalized answer transcript yet.\n")
        conversation_lines: list[str] = []
        for turn in turns:
            conversation_lines.append(
                f"[{turn.get('created_at', '')}] {str(turn.get('speaker', '')).upper()} · {turn.get('kind', '')}\n{turn.get('text_en', '')}\n"
            )
        atomic_write_text(folder / "conversation_log.txt", "\n".join(conversation_lines).strip() + "\n")
        atomic_write_text(
            folder / "README_CONFIDENTIAL.txt",
            "This folder is grouped by the candidate CNIC and is intentionally unencrypted.\n"
            "It contains personal identity, interview text, and retained answer audio.\n"
            "Keep the data folder on an HR-authorized computer and do not upload it publicly.\n",
        )
    except Exception as exc:
        logger.warning("Could not refresh readable candidate folder for %s: %s", session_id, type(exc).__name__)


def sync_all_candidate_plaintext() -> None:
    if not PLAINTEXT_CANDIDATE_DATA or not DB_PATH.exists():
        return
    try:
        with db() as connection:
            rows = connection.execute("SELECT id FROM sessions WHERE cnic_encrypted IS NOT NULL AND cnic_encrypted <> ''").fetchall()
        for row in rows:
            sync_candidate_plaintext(str(row["id"]))
    except Exception as exc:
        logger.warning("Could not refresh existing candidate folders: %s", type(exc).__name__)


# ---------- Speech accuracy / local vocabulary ----------
def load_learned_hints() -> list[str]:
    value = safe_json_loads(LEARNED_HINTS_PATH.read_text(encoding="utf-8") if LEARNED_HINTS_PATH.exists() else "", [])
    return to_phrase_list(value if isinstance(value, list) else [])[:80]


def load_domain_hints() -> list[str]:
    """Load editable organisation and Urdu-English recognition hints.

    This is vocabulary adaptation, not model fine-tuning. Add only names,
    role titles, software, common commands, and approved company terms.
    """
    if not DOMAIN_HINTS_PATH.exists():
        return []
    lines = []
    for line in DOMAIN_HINTS_PATH.read_text(encoding="utf-8").splitlines():
        value = normalize_space(line)
        if value and not value.startswith("#"):
            lines.append(value)
    return to_phrase_list(lines)[:120]


def build_transcription_context(snapshot: dict[str, Any], question: dict[str, Any], requested_language: str) -> tuple[str, str | None]:
    """Return ASR prompt/hotwords without leaking instructions into transcripts.

    Earlier versions used a natural-language instruction prompt such as
    "Preserve company names". Whisper can repeat prompt text when audio is
    unclear, which caused false HR transcripts. This version sends only compact
    vocabulary hints through hotwords and avoids instructional initial_prompt.
    """
    hints = (
        to_phrase_list(snapshot.get("transcription_hints", ""))
        + load_domain_hints()
        + load_learned_hints()
    )

    # Include current question words as vocabulary hints, not instructions.
    prompt_question = normalize_space(question.get("original_text") or question.get("text"))[:260]
    hints += [part for part in re.split(r"[,;:.!?\n]+", prompt_question) if 2 <= len(part.strip()) <= 80]

    # High-value interview/control words that help command recognition.
    hints += [
        "Adeeb AI Meeting Agent", "Urdu", "Hindi", "English", "talk in Urdu", "speak Urdu",
        "Urdu mein baat karein", "Hindi mein baat karein", "talk in English", "next question",
        "repeat the question", "ask me a field question", "Python", "React", "WordPress",
        "SEO", "AEO", "Google Ads", "Meta Ads", "Canva", "Figma", "machine learning",
        "graphic design", "content writing", "social media marketing", "internship", "portfolio",
        # Native Urdu vocabulary improves recognition without asking Whisper to follow
        # an instruction prompt (which can leak into a weak transcript).
        "ادیب", "اردو", "انگریزی", "انٹرویو", "انٹرن شپ", "درخواست", "تعارف",
        "تعلیم", "یونیورسٹی", "سمسٹر", "تجربہ", "مہارت", "پراجیکٹ", "مسئلہ",
        "حل", "نتیجہ", "مشین لرننگ", "مصنوعی ذہانت", "ویب ڈویلپمنٹ",
        "ورڈپریس", "ایس ای او", "پائتھن", "ری ایکٹ", "اگلا سوال",
        "سوال دوبارہ دہرائیں", "انگریزی میں بات کریں", "اردو میں بات کریں",
    ]

    unique: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        value = normalize_space(hint)
        # Drop instruction-like phrases that have caused leakage before.
        if not value or re.search(r"\b(preserve|transcribe|candidate speech|faithfully|do not|question:)\b", value, re.I):
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)

    # No initial_prompt by default. The hotwords list is safer for vocabulary support.
    return "", ", ".join(unique[:120]) if unique else None


async def get_whisper_model(model_name: str) -> WhisperModel:
    if model_name in _whisper_models:
        return _whisper_models[model_name]
    lock = _model_locks[model_name]
    async with lock:
        if model_name not in _whisper_models:
            logger.info("Loading local speech model: %s", model_name)
            _whisper_models[model_name] = await run_in_threadpool(
                WhisperModel,
                model_name,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                cpu_threads=WHISPER_CPU_THREADS,
                num_workers=WHISPER_NUM_WORKERS,
            )
    return _whisper_models[model_name]


def transcription_kwargs(
    task: str,
    language: str | None,
    prompt: str,
    hotwords: str | None,
    beam_size: int,
    *,
    use_vad: bool = True,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "language": language,
        "task": task,
        "beam_size": beam_size,
        "best_of": 1,
        "patience": 1.2 if beam_size > 1 else 1.0,
        "temperature": 0.0,
        "repetition_penalty": 1.08,
        "no_repeat_ngram_size": 3,
        "suppress_blank": True,
        "without_timestamps": True,
        "vad_filter": use_vad,
        "vad_parameters": {
            "min_silence_duration_ms": VAD_MIN_SILENCE_MS,
            "speech_pad_ms": VAD_SPEECH_PAD_MS,
        },
        "multilingual": True,
        "condition_on_previous_text": False,
    }
    if prompt:
        kwargs["initial_prompt"] = prompt
    if hotwords:
        kwargs["hotwords"] = hotwords
    return kwargs


def run_transcription(
    model: WhisperModel,
    file_path: str,
    task: str,
    language: str | None,
    prompt: str,
    hotwords: str | None,
    beam_size: int,
    *,
    use_vad: bool = True,
) -> tuple[str, str, float]:
    kwargs = transcription_kwargs(task, language, prompt, hotwords, beam_size, use_vad=use_vad)
    try:
        segments, info = model.transcribe(file_path, **kwargs)
    except TypeError:
        # Supports older faster-whisper builds that do not yet expose hotwords.
        kwargs.pop("hotwords", None)
        segments, info = model.transcribe(file_path, **kwargs)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    detected = getattr(info, "language", None) or "unknown"
    probability = float(getattr(info, "language_probability", 0.0) or 0.0)
    return clean_text(text), detected, probability


async def groq_transcribe_audio(
    file_path: str,
    *,
    language: str = "ur",
    vocabulary: str | None = None,
    model: str | None = None,
) -> tuple[str, str, float] | None:
    """Fast multilingual ASR through Groq when a key is configured.

    The endpoint is OpenAI-compatible. The local faster-whisper path remains the
    complete offline fallback, so a temporary API/network failure does not stop the
    interview. Only the recorded turn is sent, and only when GROQ_STT_ENABLED is true.
    """
    if not (GROQ_STT_ENABLED and GROQ_API_KEY):
        return None
    chosen_model = clean_text(model or GROQ_STT_MODEL)
    if not chosen_model:
        return None
    path = Path(file_path)
    mime = mimetypes.guess_type(path.name)[0] or "audio/webm"
    data: dict[str, str] = {
        "model": chosen_model,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    if language in {"en", "ur", "hi"}:
        data["language"] = language
    # A compact vocabulary list improves names and technical terms without giving the
    # recognizer an instruction it can accidentally copy into the transcript.
    if vocabulary:
        data["prompt"] = clean_text(vocabulary)[:900]
    try:
        async with httpx.AsyncClient(timeout=GROQ_STT_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with path.open("rb") as handle:
                response = await client.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    data=data,
                    files={"file": (path.name, handle, mime)},
                )
        response.raise_for_status()
        payload = response.json()
        text = sanitize_transcript_text(str(payload.get("text", "")))
        if not text:
            return None
        detected = clean_text(str(payload.get("language", ""))).casefold() or language
        # verbose_json does not guarantee one comparable utterance confidence value.
        # Use a high neutral value and let script/content checks decide whether repair is needed.
        return text, detected, 0.95
    except Exception as exc:
        logger.warning("Groq speech recognition unavailable (%s); using local Whisper fallback", type(exc).__name__)
        return None


def should_use_groq_stt(requested_language: str) -> bool:
    if not (GROQ_STT_ENABLED and GROQ_API_KEY):
        return False
    # Urdu keeps the explicit provider override. Other languages use Groq only when
    # the all-language speed option is enabled.
    if requested_language == "ur":
        if URDU_ASR_PROVIDER == "local":
            return False
        return True
    return GROQ_STT_ALL_LANGUAGES and requested_language in {"auto", "en", "hi"}


def transcript_plausibility(text: str, language: str) -> float:
    """Cheap deterministic tie-breaker for an optional auto-language retry.

    This does not claim linguistic certainty; it only avoids choosing empty or obviously
    low-information output when auto-detection has low confidence.
    """
    value = normalize_space(text)
    if not value:
        return -100.0
    score = min(len(value), 280) / 28.0
    if language == "ur" and re.search(r"[\u0600-\u06FF]", value):
        score += 3.0
    if language == "hi" and re.search(r"[\u0900-\u097F]", value):
        score += 3.0
    if language == "en" and re.search(r"[A-Za-z]", value):
        score += 2.0
    if len(tokenize(value)) <= 1:
        score -= 2.0
    return score


PROMPT_LEAK_PATTERNS = [
    r"\bpreserve company names?\b",
    r"\bpreserve company\b",
    r"\bpreserve names?\b",
    r"\bpreserve (?:names|numbers|tools|acronyms|technical terms)\b",
    r"\bperson names? numbers tools and acronyms\b",
    r"\bformal internship interview\b",
    r"\btranscribe only the candidate speech\b",
    r"\bcurrent interviewer prompt\b",
    r"\bquestion[: ]\s*which role or internship\b",
    r"\bdo not (?:translate|invent|improve|summarize)\b",
]


def looks_like_prompt_leak(text: str) -> bool:
    """Reject common Whisper prompt leakage instead of saving it as a candidate answer."""
    value = normalize_space(text).casefold().strip(" .,!?:;-")
    if not value:
        return True
    if any(re.search(pattern, value) for pattern in PROMPT_LEAK_PATTERNS):
        return True
    leak_tokens = {"preserve", "company", "names", "numbers", "tools", "acronyms", "transcribe", "candidate", "speech"}
    tokens = set(tokenize(value))
    if len(tokens) <= 6 and len(tokens & leak_tokens) >= 2:
        return True
    return False


def sanitize_transcript_text(text: str) -> str:
    value = clean_text(text)
    return "" if looks_like_prompt_leak(value) else value


async def translate_native_to_english(native_text: str, source_language: str) -> str | None:
    """Faithfully translate a native-script ASR transcript after transcription.

    This is deliberately separate from ASR so HR can retain the original Urdu/Hindi
    words alongside the English report. The local model is instructed not to infer
    or polish candidate content.
    """
    language_name = LANGUAGE_NAMES.get(source_language, source_language)
    result = await agent_json(
        "You are a precise translator for interview records. Translate only the supplied source text into English. "
        "Do not improve, explain, summarize, add skills, infer intent, or omit names/numbers/technical terms. "
        "Return JSON exactly: {\"translation\":\"...\"}.",
        f"Source language: {language_name}\nSource transcript: {native_text}",
        timeout=TRANSLATION_TIMEOUT_SECONDS,
        max_predict=350,
        model=OLLAMA_INTERACTION_MODEL,
    )
    translated = clean_text(str((result or {}).get("translation", "")))
    return translated or None


def choose_whisper_model(requested_language: str, final_pass: bool) -> str:
    if requested_language == "ur" and WHISPER_URDU_MODEL:
        return WHISPER_URDU_MODEL
    if requested_language == "hi" and WHISPER_HINDI_MODEL:
        return WHISPER_HINDI_MODEL
    return WHISPER_FINAL_MODEL if final_pass else WHISPER_LIVE_MODEL


def script_character_ratio(text: str, language: str) -> float:
    value = normalize_space(text)
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return 0.0
    if language == "ur":
        matches = sum(1 for char in letters if "\u0600" <= char <= "\u06FF")
    elif language == "hi":
        matches = sum(1 for char in letters if "\u0900" <= char <= "\u097F")
    else:
        matches = sum(1 for char in letters if char.isascii())
    return matches / max(1, len(letters))


def repeated_token_penalty(text: str) -> float:
    tokens = tokenize(text)
    if len(tokens) < 4:
        return 0.0
    counts = Counter(tokens)
    repeated = sum(max(0, count - 2) for count in counts.values())
    return float(repeated)


def urdu_candidate_score(text: str, probability: float) -> float:
    value = sanitize_transcript_text(text)
    if not value:
        return -100.0
    score = transcript_plausibility(value, "ur")
    score += script_character_ratio(value, "ur") * 7.0
    score += min(1.0, max(0.0, probability)) * 2.0
    score -= repeated_token_penalty(value) * 1.5
    return score


async def reconcile_urdu_understanding(
    native_text: str,
    native_translation: str | None,
    direct_audio_translation: str | None,
    question_text: str,
) -> str | None:
    """Produce a conservative English meaning for the LLM brain from two ASR paths.

    The native Urdu transcript remains untouched in the HR record. This helper only
    improves semantic understanding by comparing a text translation with Whisper's
    direct audio-to-English translation. It is forbidden from answering the question
    or inventing details that are absent from both signals.
    """
    native_translation = clean_text(native_translation or "")
    direct_audio_translation = clean_text(direct_audio_translation or "")
    if native_translation and not direct_audio_translation:
        return native_translation
    if direct_audio_translation and not native_translation:
        return direct_audio_translation
    if not native_translation and not direct_audio_translation:
        return None
    if normalize_space(native_translation).casefold() == normalize_space(direct_audio_translation).casefold():
        return native_translation

    result = await agent_json(
        "You are an ASR adjudicator for an interview record. Create one faithful English rendering of the candidate's spoken Urdu using the supplied evidence. "
        "Do not answer the interview question. Do not add a name, skill, number, result, reason, or fact that is not supported by at least one ASR signal. "
        "Preserve technical terms and code-switched English words. When the signals disagree, keep only the meaning that is reasonably supported and avoid polishing. "
        "Return JSON exactly: {\"translation\":\"...\"}.",
        f"Interview question (context only): {clean_text(question_text)[:500]}\n"
        f"Native Urdu transcript: {native_text}\n"
        f"Translation of native transcript: {native_translation}\n"
        f"Direct audio-to-English ASR: {direct_audio_translation}",
        timeout=TRANSLATION_TIMEOUT_SECONDS,
        max_predict=380,
        model=OLLAMA_INTERACTION_MODEL,
    )
    reconciled = clean_text(str((result or {}).get("translation", "")))
    return reconciled or direct_audio_translation or native_translation or None


def transcript_overlap_score(left: str, right: str) -> float:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


async def repair_urdu_transcript_with_llm(
    native_text: str,
    preliminary_english: str | None,
    question_text: str,
) -> tuple[str, str | None]:
    """Conservatively repair obvious Urdu ASR mistakes before routing the turn.

    This is an editor, not an answer generator. It may fix a misheard technical term
    such as ANN, machine learning, internship, university, or a repeated filler, but it
    must not add candidate experience or answer the interview question.
    """
    raw = clean_text(native_text)
    english = clean_text(preliminary_english or "")
    if not (URDU_LLM_TRANSCRIPT_REPAIR and raw):
        return raw, english or None
    result = await agent_json(
        "You are a conservative Urdu interview ASR editor. Correct only obvious recognition errors in the supplied candidate transcript. "
        "Never answer the interview question, never add experience, projects, numbers, names, results, or reasons that were not spoken. "
        "Preserve Urdu words in normal Pakistani Urdu script and preserve code-switched English technical terms in English letters. "
        "Do not convert the whole transcript to Roman Urdu. If uncertain, keep the original wording. "
        "Also provide one literal English translation for the interview brain. Return JSON exactly: "
        "{\"corrected_original\":\"...\",\"english\":\"...\"}.",
        f"Current interviewer question (context only): {clean_text(question_text)[:500]}\n"
        f"Raw candidate transcript: {raw}\n"
        f"Existing literal English rendering: {english}",
        timeout=min(TRANSLATION_TIMEOUT_SECONDS, 24),
        max_predict=420,
        model=OLLAMA_INTERACTION_MODEL,
    )
    corrected = sanitize_transcript_text(str((result or {}).get("corrected_original", "")))
    repaired_english = sanitize_transcript_text(str((result or {}).get("english", "")))
    # Reject a rewrite that has almost no lexical connection to either ASR signal.
    # This blocks the failure seen in the supplied meeting where Adeeb invented its own
    # customer-churn project instead of handling the candidate's answer.
    source_for_overlap = clean_text(f"{raw} {english}")
    if corrected and transcript_overlap_score(source_for_overlap, corrected) < 0.18 and len(tokenize(corrected)) > 5:
        corrected = raw
    if repaired_english and transcript_overlap_score(source_for_overlap, repaired_english) < 0.12 and len(tokenize(repaired_english)) > 7:
        repaired_english = english
    return corrected or raw, repaired_english or english or None


async def transcribe_turn(file_path: str, snapshot: dict[str, Any], question: dict[str, Any], requested_language: str, *, final_pass: bool = False) -> dict[str, Any]:
    """Native speech -> native transcript -> faithful English understanding.

    Urdu mode is genuinely language-locked: short answers are not sent through auto
    detection, the full multilingual model is used by default, and a separate direct
    audio translation gives the LLM brain a second semantic signal. English behavior
    remains unchanged.
    """
    selected_language = requested_language if requested_language in {"en", "ur", "hi"} else None
    prompt, hotwords = build_transcription_context(snapshot, question, requested_language)
    model_name = choose_whisper_model(requested_language, final_pass)
    if final_pass:
        beam = FINAL_WHISPER_BEAM_SIZE
    elif requested_language == "ur":
        beam = URDU_WHISPER_BEAM_SIZE
    else:
        beam = WHISPER_BEAM_SIZE
    started = time.perf_counter()
    model: WhisperModel | None = None
    asr_provider = "local"

    cloud_result: tuple[str, str, float] | None = None
    if not final_pass and should_use_groq_stt(requested_language):
        cloud_result = await groq_transcribe_audio(
            file_path,
            language=requested_language if requested_language in {"en", "ur", "hi"} else "auto",
            vocabulary=hotwords,
            model=GROQ_STT_MODEL,
        )
    if cloud_result:
        native_text, detected, probability = cloud_result
        asr_provider = f"groq:{GROQ_STT_MODEL}"
        # Use the accuracy model only for an obviously weak Urdu result. Normal turns
        # stay on the turbo endpoint so the meeting responds before the browser times out.
        weak_cloud_result = (
            len(tokenize(native_text)) <= 1
            or repeated_token_penalty(native_text) > 1
            or looks_like_prompt_leak(native_text)
        )
        if requested_language == "ur" and weak_cloud_result and GROQ_STT_ACCURACY_MODEL and GROQ_STT_ACCURACY_MODEL != GROQ_STT_MODEL:
            accurate = await groq_transcribe_audio(
                file_path, language="ur", vocabulary=hotwords, model=GROQ_STT_ACCURACY_MODEL
            )
            if accurate and urdu_candidate_score(accurate[0], accurate[2]) > urdu_candidate_score(native_text, probability):
                native_text, detected, probability = accurate
                asr_provider = f"groq:{GROQ_STT_ACCURACY_MODEL}"
    else:
        try:
            model = await get_whisper_model(model_name)
        except Exception as exc:
            if requested_language == "ur" and model_name != WHISPER_LIVE_MODEL:
                logger.warning("Urdu model %s could not load (%s); falling back to %s", model_name, type(exc).__name__, WHISPER_LIVE_MODEL)
                model_name = WHISPER_LIVE_MODEL
                model = await get_whisper_model(model_name)
            else:
                raise
        async with _transcription_lock:
            native_text, detected, probability = await run_in_threadpool(
                run_transcription, model, file_path, "transcribe", selected_language, prompt, hotwords, beam
            )
        # On some laptop/phone recordings Silero VAD can classify the whole quiet Urdu
        # utterance as silence. Rescue that exact case once without VAD instead of
        # launching several slow recognition passes or making the browser listen again.
        if requested_language == "ur" and not clean_text(native_text):
            async with _transcription_lock:
                native_text, detected, probability = await run_in_threadpool(
                    lambda: run_transcription(
                        model, file_path, "transcribe", "ur", prompt, hotwords, beam, use_vad=False
                    )
                )

    # For an uncertain Urdu result, compare one unbiased pass without hotwords. This
    # helps when vocabulary hints accidentally pull a nearby but incorrect word.
    if requested_language == "ur" and URDU_SECOND_PASS_ON_LOW_CONFIDENCE and model is not None:
        primary_score = urdu_candidate_score(native_text, probability)
        needs_retry = (
            probability < URDU_SECOND_PASS_MIN_CONFIDENCE
            or script_character_ratio(native_text, "ur") < 0.55
            or repeated_token_penalty(native_text) > 0
            or len(tokenize(native_text)) <= 2
        )
        if needs_retry:
            async with _transcription_lock:
                retry_text, retry_detected, retry_probability = await run_in_threadpool(
                    run_transcription,
                    model,
                    file_path,
                    "transcribe",
                    "ur",
                    "",
                    None,
                    min(10, max(beam, URDU_WHISPER_BEAM_SIZE + 1)),
                )
            retry_score = urdu_candidate_score(retry_text, retry_probability)
            if retry_score > primary_score + 0.15:
                native_text, detected, probability = retry_text, retry_detected or "ur", retry_probability

    # Auto mode remains available for genuinely mixed input. It is no longer used for
    # short turns when the meeting has already been locked to Urdu or Hindi.
    if (
        AUTO_LANGUAGE_RETRY
        and requested_language == "auto"
        and (detected not in {"en", "ur", "hi"} or probability < AUTO_LANGUAGE_RETRY_MIN_CONFIDENCE)
    ):
        candidates: list[tuple[str, str, float]] = [(native_text, detected, probability)]
        async with _transcription_lock:
            for retry_language in ("en", "ur", "hi"):
                retry_text, retry_detected, retry_probability = await run_in_threadpool(
                    run_transcription, model, file_path, "transcribe", retry_language, prompt, hotwords, max(beam, 4)
                )
                candidates.append((retry_text, retry_detected or retry_language, retry_probability))
        native_text, detected, probability = max(
            candidates,
            key=lambda item: transcript_plausibility(item[0], item[1] if item[1] in {"en", "ur", "hi"} else "en") + item[2],
        )

    native_text = sanitize_transcript_text(native_text)
    if not native_text:
        raise HTTPException(status_code=422, detail="No clear candidate speech was detected. HR can replay the saved audio if needed.")

    # Respect the explicit meeting lock. Whisper's reported language can fluctuate on
    # code-switched Urdu sentences, but that must not silently change the pipeline.
    source_language = selected_language or (detected if detected in {"en", "ur", "hi"} else "en")
    english = native_text
    translator = "not_needed"
    if source_language in {"ur", "hi"}:
        native_translation = await translate_native_to_english(native_text, source_language)
        direct_audio_translation: str | None = None
        if source_language == "ur" and URDU_DIRECT_AUDIO_TRANSLATION and URDU_TRANSLATION_MODE != "ollama_only" and model is not None and asr_provider == "local":
            # Use the already-warmed live model as an independent and faster semantic
            # channel when the native Urdu pass uses large-v3. This improves meaning
            # recovery without running the full model twice on every answer.
            translation_model_name = WHISPER_LIVE_MODEL if model_name != WHISPER_LIVE_MODEL else model_name
            translation_model = model if translation_model_name == model_name else await get_whisper_model(translation_model_name)
            async with _transcription_lock:
                direct_audio_translation, _, _ = await run_in_threadpool(
                    run_transcription, translation_model, file_path, "translate", "ur", "", hotwords, max(WHISPER_BEAM_SIZE, 5)
                )
            direct_audio_translation = sanitize_transcript_text(direct_audio_translation)
            english = await reconcile_urdu_understanding(
                native_text,
                native_translation,
                direct_audio_translation,
                str(question.get("original_text") or question.get("text") or ""),
            )
            translator = f"native+{translation_model_name}_audio_adjudicated"
        else:
            english = native_translation
            translator = "llm_native_translation"

        if not english and URDU_TRANSLATION_MODE != "ollama_only":
            if model is None:
                fallback_model_name = WHISPER_LIVE_MODEL
                model = await get_whisper_model(fallback_model_name)
            async with _transcription_lock:
                english, _, _ = await run_in_threadpool(
                    run_transcription, model, file_path, "translate", source_language, "", hotwords, max(beam, 5)
                )
            translator = "whisper_translation"
        if not english:
            english = native_text
            translator = "native_only"

        native_text, repaired_english = await repair_urdu_transcript_with_llm(
            native_text,
            english,
            str(question.get("original_text") or question.get("text") or ""),
        )
        if repaired_english:
            english = repaired_english
            translator = f"{translator}+llm_repair"

    processing_ms = round((time.perf_counter() - started) * 1000)
    return {
        "english": clean_text(english),
        "original": native_text,
        "language": LANGUAGE_NAMES.get(source_language, source_language.title() if source_language else "Auto detected"),
        "processing_ms": processing_ms,
        "language_confidence": round(probability, 3),
        "model_version": f"{asr_provider if asr_provider != 'local' else model_name} ({translator})",
    }


# ---------- Local RAG ----------
def is_sensitive_record(title: str, answer: str) -> bool:
    text = f"{title}\n{answer}".casefold()
    patterns = [
        r"\b(account\s*(?:no|number)|bank\s*name|iban|cnic|password|api\s*key|secret)\b",
        r"\b(jazzcash|easypaisa)\b",
        r"\b\d{10,}\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.findall(r"[\w\u0600-\u06FF\u0900-\u097F]+", normalized, flags=re.UNICODE)


def parse_csv_records(payload: str, source: str) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(payload))
    rows = [row for row in reader if any(normalize_space(cell) for cell in row)]
    if not rows:
        return []
    header = [normalize_space(cell).casefold() for cell in rows[0]]
    has_header = any(word in " ".join(header) for word in ("question", "trigger", "keyword", "answer", "response", "reply"))
    data_rows = rows[1:] if has_header else rows
    title_index, answer_index = 0, 1
    if has_header:
        for index, cell in enumerate(header):
            if any(key in cell for key in ("trigger", "keyword", "question", "query", "title")):
                title_index = index
            if any(key in cell for key in ("answer", "response", "reply", "content")):
                answer_index = index
    records: list[dict[str, Any]] = []
    for index, row in enumerate(data_rows, 1):
        if len(row) < 2:
            continue
        title = normalize_space(row[title_index] if title_index < len(row) else row[0])
        answer = normalize_space(row[answer_index] if answer_index < len(row) else row[1])
        if not title or not answer:
            continue
        records.append({
            "id": f"{source}-{index}",
            "title": title,
            "content": answer,
            "source": source,
            "sensitive": is_sensitive_record(title, answer),
        })
    return records


def markdown_records() -> list[dict[str, Any]]:
    content = KNOWLEDGE_PATH.read_text(encoding="utf-8") if KNOWLEDGE_PATH.exists() else DEFAULT_KNOWLEDGE_BASE
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", content) if chunk.strip()]
    records: list[dict[str, Any]] = []
    active_title = "Approved company information"
    for index, chunk in enumerate(chunks, 1):
        heading_match = re.match(r"^#{1,6}\s+(.+)$", chunk)
        if heading_match:
            active_title = normalize_space(heading_match.group(1))
            continue
        title = active_title
        records.append({
            "id": f"markdown-{index}",
            "title": title,
            "content": chunk,
            "source": "approved_markdown",
            "sensitive": is_sensitive_record(title, chunk),
        })
    return records




def load_pdf_cache() -> dict[str, Any]:
    data = safe_json_loads(PDF_RAG_CACHE_PATH.read_text(encoding="utf-8") if PDF_RAG_CACHE_PATH.exists() else "", {})
    if not isinstance(data, dict):
        data = {}
    documents = data.get("documents", [])
    if not isinstance(documents, list):
        documents = []
    return {"documents": [doc for doc in documents if isinstance(doc, dict)]}


def save_pdf_cache(cache: dict[str, Any]) -> dict[str, Any]:
    clean = {"documents": cache.get("documents", []) if isinstance(cache.get("documents"), list) else []}
    temp = PDF_RAG_CACHE_PATH.with_suffix(".tmp")
    temp.write_text(json_dump(clean) + "\n", encoding="utf-8")
    temp.replace(PDF_RAG_CACHE_PATH)
    return clean


def clean_pdf_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_pdf_text(text: str, chunk_chars: int = PDF_CHUNK_CHARS, overlap: int = PDF_CHUNK_OVERLAP) -> list[str]:
    text = clean_pdf_text(text)
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= chunk_chars:
            current = f"{current}\n\n{para}".strip()
            continue
        if current:
            chunks.append(current)
        if len(para) <= chunk_chars:
            current = para
        else:
            start = 0
            while start < len(para):
                end = min(len(para), start + chunk_chars)
                chunks.append(para[start:end].strip())
                if end == len(para):
                    break
                start = max(0, end - overlap)
            current = ""
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if len(chunk) >= 40]


def extract_pdf_text(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("PDF import requires the pypdf package. Run: pip install pypdf") from exc
    reader = PdfReader(str(path))
    pages = min(len(reader.pages), MAX_PDF_PAGES)
    text_parts: list[str] = []
    for page_index in range(pages):
        try:
            text_parts.append(reader.pages[page_index].extract_text() or "")
        except Exception:
            logger.warning("Could not extract text from PDF page %s in %s", page_index + 1, path.name)
    return clean_pdf_text("\n\n".join(text_parts)), pages


def pdf_records() -> list[dict[str, Any]]:
    cache = load_pdf_cache()
    records: list[dict[str, Any]] = []
    for doc in cache.get("documents", []):
        for record in doc.get("records", []) if isinstance(doc.get("records"), list) else []:
            if isinstance(record, dict) and record.get("title") and record.get("content"):
                records.append(record)
    return records


def pdf_rag_status_payload() -> dict[str, Any]:
    cache = load_pdf_cache()
    documents = []
    total_chunks = 0
    total_chars = 0
    for doc in cache.get("documents", []):
        chunks = int(doc.get("chunks", 0) or 0)
        chars = int(doc.get("characters", 0) or 0)
        total_chunks += chunks
        total_chars += chars
        documents.append({
            "doc_id": doc.get("doc_id", ""),
            "filename": doc.get("filename", "Untitled PDF"),
            "uploaded_at": doc.get("uploaded_at", ""),
            "pages": doc.get("pages", 0),
            "chunks": chunks,
            "characters": chars,
        })
    return {"documents": documents, "document_count": len(documents), "chunk_count": total_chunks, "characters": total_chars}


def pdf_expected_keywords(text: str, limit: int = 6) -> list[str]:
    stop = {
        "this", "that", "with", "from", "your", "about", "which", "their", "there", "have", "will",
        "candidate", "interview", "document", "information", "please", "should", "according", "because",
        "اور", "کے", "کی", "کا", "میں", "ہے", "ہیں", "کو", "سے", "پر", "یہ", "وہ", "کر", "लिए", "और", "है", "का", "की", "के",
    }
    counts = Counter(token for token in tokenize(text) if len(token) >= 4 and token not in stop)
    return [word for word, _ in counts.most_common(limit)]


def generate_pdf_questions(limit: int = 5) -> list[dict[str, Any]]:
    records = pdf_records()
    # Prefer different documents/chunks, then longer chunks with useful terms.
    records = sorted(records, key=lambda r: len(str(r.get("content", ""))), reverse=True)
    questions: list[dict[str, Any]] = []
    used_docs: set[str] = set()
    for record in records:
        if len(questions) >= limit:
            break
        keywords = pdf_expected_keywords(f"{record.get('title')} {record.get('content')}")
        if not keywords:
            continue
        topic = normalize_space(record.get("title") or "the uploaded PDF")
        doc_id = str(record.get("doc_id") or "")
        if doc_id in used_docs and len(questions) < max(1, min(limit, len({str(r.get('doc_id') or '') for r in records}))):
            continue
        used_docs.add(doc_id)
        questions.append({
            "question": f"According to the uploaded document, explain the key point about {topic}.",
            "expected_keywords": "|".join(keywords),
            "source": record.get("source", "pdf"),
            "pdf_filename": record.get("filename", "Uploaded PDF"),
            "chunk_id": record.get("id", ""),
        })
    # If only one PDF exists, fill remaining from other chunks.
    if len(questions) < limit:
        used_chunk_ids = {q["chunk_id"] for q in questions}
        for record in records:
            if len(questions) >= limit:
                break
            if record.get("id") in used_chunk_ids:
                continue
            keywords = pdf_expected_keywords(f"{record.get('title')} {record.get('content')}")
            if not keywords:
                continue
            questions.append({
                "question": f"What does the PDF say about {', '.join(keywords[:2])}?",
                "expected_keywords": "|".join(keywords),
                "source": record.get("source", "pdf"),
                "pdf_filename": record.get("filename", "Uploaded PDF"),
                "chunk_id": record.get("id", ""),
            })
    return questions[:limit]

def load_google_cache() -> list[dict[str, Any]]:
    cached = safe_json_loads(RAG_CACHE_PATH.read_text(encoding="utf-8") if RAG_CACHE_PATH.exists() else "", {})
    records = cached.get("records", []) if isinstance(cached, dict) else []
    return [record for record in records if isinstance(record, dict) and record.get("title") and record.get("content")]


def local_csv_records() -> list[dict[str, Any]]:
    if not SEED_FAQ_PATH.exists():
        return []
    return parse_csv_records(SEED_FAQ_PATH.read_text(encoding="utf-8-sig", errors="replace"), "bundled_faq_csv")


def available_rag_records() -> list[dict[str, Any]]:
    settings = load_rag_settings()
    records = markdown_records()
    if settings["source_mode"] == "google_sheet":
        records.extend(load_google_cache())
    else:
        records.extend(local_csv_records())
    records.extend(pdf_records())
    return records


def score_records(query: str, records: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    eligible = [record for record in records if RAG_ALLOW_SENSITIVE or not record.get("sensitive")]
    query_tokens = tokenize(query)
    if not query_tokens or not eligible:
        return []
    docs = [tokenize(f"{record['title']} {record['title']} {record['content']}") for record in eligible]
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    average = max(1.0, sum(len(doc) for doc in docs) / len(docs))
    n = len(docs)
    query_counts = Counter(query_tokens)
    scored: list[tuple[float, dict[str, Any]]] = []
    for record, doc in zip(eligible, docs):
        term_counts = Counter(doc)
        score = 0.0
        for token, q_count in query_counts.items():
            if token not in term_counts:
                continue
            idf = math.log(1 + (n - df[token] + 0.5) / (df[token] + 0.5))
            denominator = term_counts[token] + 1.2 * (1 - 0.75 + 0.75 * len(doc) / average)
            score += idf * ((term_counts[token] * 2.2) / denominator) * min(2, q_count)
        title_norm = " ".join(tokenize(record["title"]))
        query_norm = " ".join(query_tokens)
        if title_norm and query_norm:
            score += SequenceMatcher(None, query_norm, title_norm).ratio() * 1.1
        if score > 0:
            output = dict(record)
            output["score"] = round(score, 4)
            scored.append((score, output))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[:top_k]]


def retrieve_knowledge(query: str) -> list[dict[str, Any]]:
    return score_records(query, available_rag_records())


def rag_evidence(matches: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    for match in matches:
        pieces.append(f"[Source: {match['source']} | Topic: {match['title']}]\n{match['content']}")
    return "\n\n".join(pieces)[:10000]


async def maybe_auto_sync_google_sheet() -> None:
    """Refresh a published Sheet only when the local cache is stale; a sync failure never blocks the interview."""
    settings = load_rag_settings()
    if settings.get("source_mode") != "google_sheet" or not normalize_space(settings.get("google_sheet_csv_url")):
        return
    last_synced = normalize_space(settings.get("last_synced_at"))
    minutes = max(5, int(settings.get("auto_sync_minutes") or 30))
    stale = True
    if last_synced:
        try:
            last = datetime.fromisoformat(last_synced.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - last).total_seconds() >= minutes * 60
        except ValueError:
            stale = True
    if stale:
        try:
            await sync_google_sheet()
        except HTTPException:
            logger.warning("RAG auto-sync skipped because the Google Sheet was unavailable.")


async def sync_google_sheet(force_url: str | None = None) -> dict[str, Any]:
    settings = load_rag_settings()
    url = normalize_space(force_url or settings.get("google_sheet_csv_url"))
    if not url:
        raise HTTPException(status_code=422, detail="Add a published Google Sheet CSV URL first.")
    if not re.match(r"^https://", url, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="The Google Sheet source must use an HTTPS URL.")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Adeeb-AI-Meeting-Agent/2.0"})
            response.raise_for_status()
        records = parse_csv_records(response.text, "google_sheet")
        if not records:
            raise ValueError("No two-column FAQ records were found. Use a sheet with a trigger/question column and an answer/response column.")
        cache = {"synced_at": utc_now(), "records": records, "source_url": url}
        temp = RAG_CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json_dump(cache) + "\n", encoding="utf-8")
        temp.replace(RAG_CACHE_PATH)
        settings.update({
            "google_sheet_csv_url": url,
            "last_synced_at": cache["synced_at"],
            "last_sync_status": "Google Sheet synchronized.",
            "last_record_count": len(records),
        })
        save_rag_settings(settings)
        return {"ok": True, "records": len(records), "sensitive_records": sum(1 for record in records if record["sensitive"]), "synced_at": cache["synced_at"]}
    except HTTPException:
        raise
    except Exception as exc:
        settings.update({"last_sync_status": f"Sync failed: {type(exc).__name__}"})
        save_rag_settings(settings)
        logger.warning("Google Sheet sync failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the Google Sheet. Confirm that the CSV URL is published and accessible without sign-in.") from exc


# ---------- Local LLM agent logic ----------
def extract_json(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


async def ollama_json(system: str, user: str, *, timeout: int, max_predict: int, model: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "format": "json",
                    "keep_alive": "20m",
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"temperature": 0.08, "num_predict": max_predict},
                },
            )
            response.raise_for_status()
            return extract_json(response.json().get("message", {}).get("content", ""))
    except Exception as exc:
        logger.info("Local Ollama unavailable: %s", type(exc).__name__)
        return None




async def openai_compatible_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: int,
    max_predict: int,
    provider_name: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Call an OpenAI-compatible free/hosted LLM API and force JSON-style output.

    Used for Groq and OpenRouter. The app still works without it by falling back to Gemini/Ollama.
    """
    if not api_key:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system + "\nReturn only valid JSON. Do not wrap it in markdown."},
                {"role": "user", "content": user},
            ],
            "temperature": 0.12,
            "max_tokens": max_predict,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            # Some free routes do not support response_format. Retry once without it.
            if response.status_code in {400, 422}:
                payload.pop("response_format", None)
                payload["messages"][0]["content"] += " The complete response must be parseable JSON."
                response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return extract_json(text)
    except Exception as exc:
        logger.info("%s unavailable: %s", provider_name, type(exc).__name__)
        return None


async def groq_json(system: str, user: str, *, timeout: int, max_predict: int) -> dict[str, Any] | None:
    return await openai_compatible_json(
        base_url=GROQ_BASE_URL,
        api_key=GROQ_API_KEY,
        model=GROQ_MODEL,
        system=system,
        user=user,
        timeout=timeout,
        max_predict=max_predict,
        provider_name="Groq",
    )


async def openrouter_json(system: str, user: str, *, timeout: int, max_predict: int) -> dict[str, Any] | None:
    return await openai_compatible_json(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
        model=OPENROUTER_MODEL,
        system=system,
        user=user,
        timeout=timeout,
        max_predict=max_predict,
        provider_name="OpenRouter",
        extra_headers={"HTTP-Referer": OPENROUTER_SITE_URL, "X-Title": OPENROUTER_APP_NAME},
    )


async def gemini_json(system: str, user: str, *, timeout: int, max_predict: int) -> dict[str, Any] | None:
    """Use Gemini as the optional API brain. Returns parsed JSON or None on failure."""
    if not GEMINI_API_KEY:
        return None
    try:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        prompt = (
            f"SYSTEM INSTRUCTIONS:\n{system}\n\n"
            f"USER REQUEST:\n{user}\n\n"
            "Return only valid JSON. Do not wrap it in markdown."
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": max_predict,
                "responseMimeType": "application/json",
            },
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, params={"key": GEMINI_API_KEY}, json=payload)
            response.raise_for_status()
        data = response.json()
        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text += str(part.get("text", ""))
        return extract_json(text)
    except Exception as exc:
        logger.info("Gemini unavailable: %s", type(exc).__name__)
        return None


async def agent_json(system: str, user: str, *, timeout: int, max_predict: int, model: str | None = None) -> dict[str, Any] | None:
    """Try the configured free API brain first, then fall back safely.

    Recommended order in auto mode:
    1) Groq free/developer tier when GROQ_API_KEY exists.
    2) Gemini free tier when GEMINI_API_KEY exists.
    3) OpenRouter free model routes when OPENROUTER_API_KEY exists.
    4) Local Ollama for fully local/free fallback.
    """
    provider_order = [LLM_PROVIDER] if LLM_PROVIDER not in {"", "auto"} else ["groq", "gemini", "openrouter", "ollama"]
    for provider in provider_order:
        if provider == "groq":
            result = await groq_json(system, user, timeout=timeout, max_predict=max_predict)
        elif provider == "gemini":
            result = await gemini_json(system, user, timeout=timeout, max_predict=max_predict)
        elif provider == "openrouter":
            result = await openrouter_json(system, user, timeout=timeout, max_predict=max_predict)
        elif provider == "ollama":
            result = await ollama_json(system, user, timeout=timeout, max_predict=max_predict, model=model or OLLAMA_INTERACTION_MODEL)
        else:
            result = None
        if result:
            return result
    return None




def load_translation_cache() -> dict[str, str]:
    if not AGENT_TRANSLATION_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(AGENT_TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_translation_cache(cache: dict[str, str]) -> None:
    temp = AGENT_TRANSLATION_CACHE_PATH.with_suffix(".tmp")
    temp.write_text(json_dump(cache) + "\n", encoding="utf-8")
    temp.replace(AGENT_TRANSLATION_CACHE_PATH)


def preferred_agent_language(session: sqlite3.Row | None) -> str:
    if not session:
        return "en"
    value = str(session["candidate_language"] or "en") if "candidate_language" in session.keys() else "en"
    return value if value in {"en", "ur", "hi"} else "en"


LOCAL_AGENT_MESSAGES: dict[str, dict[str, str]] = {
    "language_set_ur": {
        "ur": "جی ضرور، اب میں آپ سے اردو میں بات کروں گا۔ اب اگلا سوال سنیں۔",
        "hi": "ज़रूर, अब मैं आपसे उर्दू मोड में बात करूँगा। अगला सवाल सुनिए।",
        "en": "Sure, I will continue in Urdu mode. Please listen to the next question.",
    },
    "language_set_hi": {
        "ur": "جی ضرور، اب میں آپ سے ہندی موڈ میں بات کروں گا۔ اب اگلا سوال سنیں۔",
        "hi": "ज़रूर, अब मैं आपसे हिंदी में बात करूँगा। अगला सवाल सुनिए।",
        "en": "Sure, I will continue in Hindi mode. Please listen to the next question.",
    },
    "language_set_en": {
        "ur": "Sure, I will continue in English. Please listen to the next question.",
        "hi": "Sure, I will continue in English. Please listen to the next question.",
        "en": "Sure, I will continue in English. Please listen to the next question.",
    },
    "finish_instruction": {
        "ur": "انٹرویو ریکارڈ مکمل رکھنے کے لیے، کسی سوال کو چھوڑنے کے لیے نیکسٹ کوئسچن کہیں یا Next بٹن استعمال کریں۔ آخری سوال کے بعد میٹنگ خود مکمل ہو جائے گی۔",
        "hi": "इंटरव्यू रिकॉर्ड पूरा रखने के लिए, किसी सवाल को छोड़ने के लिए next question कहें या Next बटन इस्तेमाल करें। आखिरी सवाल के बाद मीटिंग अपने आप पूरी हो जाएगी।",
        "en": "To keep your interview record complete, please say next question or use the Next button to skip. The meeting finishes automatically after the final question.",
    },
    "fallback_continue": {
        "ur": "میں اس سوال کا مصدقہ جواب ابھی نہیں دے سکتا۔ ہم موجودہ انٹرویو سوال جاری رکھتے ہیں۔",
        "hi": "मैं इस सवाल का verified जवाब अभी नहीं दे सकता। हम मौजूदा interview question जारी रखते हैं।",
        "en": "I do not have verified information about that yet. We can continue with the current interview question.",
    },
}


def local_agent_message(key: str, language: str) -> str:
    language = language if language in {"en", "ur", "hi"} else "en"
    return LOCAL_AGENT_MESSAGES.get(key, {}).get(language) or LOCAL_AGENT_MESSAGES.get(key, {}).get("en", "")



def bundled_translation(text: str, target_language: str) -> str | None:
    """Return an offline translation for fixed interview prompts and lifecycle messages.

    This keeps Urdu mode functional even when no hosted LLM or local Ollama model is
    available. Dynamic follow-ups still use the configured LLM translator.
    """
    if target_language not in {"ur", "hi"}:
        return None
    source = normalize_space(text)
    if not source:
        return None
    try:
        config = load_questions()
    except Exception:
        return None
    suffix = "_ur" if target_language == "ur" else "_hi"
    pairs: list[tuple[str, str]] = []
    for key in ("welcome_message", "closing_message"):
        original = normalize_space(config.get(key, ""))
        translated = normalize_space(config.get(f"{key}{suffix}", ""))
        if original and translated:
            pairs.append((original, translated))
    for item in config.get("questions", []):
        original = normalize_space(item.get("text", ""))
        translated = normalize_space(item.get(f"text{suffix}", ""))
        if original and translated:
            pairs.append((original, translated))
    for original, translated in pairs:
        if source == original:
            return translated
        # Welcome text contains the candidate name after placeholder substitution.
        if "{candidate_name}" in original:
            prefix, _, tail = original.partition("{candidate_name}")
            if source.startswith(prefix) and tail and tail in source:
                name = source[len(prefix):source.find(tail)]
                return translated.replace("{candidate_name}", name)
    return None


def dynamic_agent_translation_fallback(text: str, target_language: str) -> str:
    """Keep spoken mode in Urdu/Hindi when no translation LLM is reachable.

    Fixed interview objectives use exact bundled translations. This conservative
    fallback covers dynamic follow-ups without inventing candidate-specific facts.
    """
    value = normalize_space(text).casefold()
    if target_language == "ur":
        if "exact role" in value or "internship title" in value:
            return "براہ کرم اس عہدے یا انٹرن شپ کا درست نام واضح طور پر بتائیں جس کے لیے آپ درخواست دے رہے ہیں۔"
        if "already covered enough" in value:
            return "ہم آپ کے شعبے سے متعلق کافی سوالات کر چکے ہیں۔ براہ کرم موجودہ سوال کا جواب دیں یا نیکسٹ کوئسچن کہیں۔"
        if "brief response" in value or "still need" in value:
            return "براہ کرم موجودہ سوال کا مختصر اور واضح جواب دیں، یا اسے چھوڑنے کے لیے نیکسٹ کوئسچن کہیں۔"
        if any(word in value for word in ("difficult", "problem", "challenge", "mistake")):
            return "کسی مشکل مسئلے یا چیلنج کی ایک واضح مثال دیں، بتائیں آپ نے کیا کیا اور نتیجہ کیا نکلا؟"
        if any(word in value for word in ("project", "tool", "skill", "method", "technical", "field", "role")):
            return "اپنے متعلقہ شعبے سے ایک عملی پراجیکٹ، ٹول یا مہارت بیان کریں اور بتائیں کہ آپ نے اسے کیسے استعمال کیا؟"
        if text.rstrip().endswith("?"):
            return "براہ کرم اس سوال کا مختصر اور واضح جواب ایک عملی مثال کے ساتھ دیں؟"
        return "براہ کرم موجودہ سوال کا واضح جواب دیں، یا اگلے سوال کے لیے نیکسٹ کوئسچن کہیں۔"
    if target_language == "hi":
        if "exact role" in value or "internship title" in value:
            return "कृपया उस पद या इंटर्नशिप का सही नाम स्पष्ट रूप से बताइए जिसके लिए आप आवेदन कर रहे हैं।"
        if "already covered enough" in value:
            return "हम आपके क्षेत्र से जुड़े पर्याप्त सवाल पूछ चुके हैं। कृपया मौजूदा सवाल का जवाब दें या next question कहें।"
        if "brief response" in value or "still need" in value:
            return "कृपया मौजूदा सवाल का छोटा और स्पष्ट जवाब दें, या इसे छोड़ने के लिए next question कहें।"
        if any(word in value for word in ("difficult", "problem", "challenge", "mistake")):
            return "किसी कठिन समस्या या चुनौती का एक स्पष्ट उदाहरण दें, आपने क्या किया और परिणाम क्या रहा?"
        if any(word in value for word in ("project", "tool", "skill", "method", "technical", "field", "role")):
            return "अपने संबंधित क्षेत्र का एक व्यावहारिक प्रोजेक्ट, टूल या कौशल बताइए और समझाइए कि आपने उसे कैसे इस्तेमाल किया?"
        if text.rstrip().endswith("?"):
            return "कृपया इस सवाल का छोटा और स्पष्ट जवाब एक व्यावहारिक उदाहरण के साथ दें?"
        return "कृपया मौजूदा सवाल का स्पष्ट जवाब दें, या अगले सवाल के लिए next question कहें।"
    return text


def valid_agent_script(text: str, target_language: str) -> bool:
    """Reject Roman Urdu/Hindi generated by a translation provider or stale cache."""
    value = normalize_space(text)
    if not value:
        return False
    if target_language == "ur":
        return script_character_ratio(value, "ur") >= 0.30 and bool(re.search(r"[\u0600-\u06FF]", value))
    if target_language == "hi":
        return script_character_ratio(value, "hi") >= 0.30 and bool(re.search(r"[\u0900-\u097F]", value))
    return True


async def translate_agent_text(text: str, target_language: str) -> str:
    """Translate Adeeb's own interview prompt into Urdu/Hindi when requested.

    Candidate answers are not rewritten here. This is only for Adeeb's spoken prompt.
    The cache keeps repeated questions fast after the first translation.
    """
    text = clean_text(text)
    target_language = target_language if target_language in {"ur", "hi"} else "en"
    if not text or target_language == "en":
        return text
    offline = bundled_translation(text, target_language)
    if offline:
        return offline
    cache_key = hashlib.sha256(f"native-script-v15.5|{target_language}|{text}".encode("utf-8")).hexdigest()
    cache = load_translation_cache()
    if cache.get(cache_key) and valid_agent_script(cache[cache_key], target_language):
        return cache[cache_key]
    language_name = "Urdu" if target_language == "ur" else "Hindi"
    result = await agent_json(
        "You translate Adeeb AI Meeting Agent prompts for a candidate interview. Translate faithfully. "
        "For Urdu, write normal Pakistani Urdu in Urdu/Arabic script only; never use Roman Urdu or English-letter Urdu. "
        "For Hindi, use Devanagari script. Keep technical terms like Python, React, SEO, CNIC, internship, and portfolio natural. "
        "Do not add new facts. Return JSON exactly: {\"text\":\"...\"}.",
        f"Translate this interview prompt to spoken {language_name}:\n{text}",
        timeout=AGENT_TRANSLATION_TIMEOUT_SECONDS,
        max_predict=260,
        model=OLLAMA_INTERACTION_MODEL,
    )
    translated = clean_text(str((result or {}).get("text", "")))
    if not translated or len(translated) < 2 or not valid_agent_script(translated, target_language):
        return dynamic_agent_translation_fallback(text, target_language)
    cache[cache_key] = translated[:1400]
    try:
        save_translation_cache(cache)
    except Exception as exc:
        logger.info("Could not save translation cache: %s", type(exc).__name__)
    return translated[:1400]


async def agent_spoken_text(session_id: str, text: str) -> str:
    session = session_or_404(session_id)
    language = preferred_agent_language(session)
    return await translate_agent_text(text, language)


async def agent_question_from_text(session_id: str, text: str) -> str:
    return await agent_spoken_text(session_id, text)

async def warm_ollama() -> None:
    result = await ollama_json("Return JSON {\"ok\":true}.", "Warm local model.", timeout=20, max_predict=10, model=OLLAMA_INTERACTION_MODEL)
    logger.info("Ollama interaction warm-up: %s", "ready" if result else "unavailable")


def detect_intent(text: str) -> str:
    value = normalize_space(text).casefold()
    token_count = len(tokenize(value))
    # Fast deterministic controls run before the LLM. They intentionally allow common
    # ASR mistakes such as "Urduk" and "please stop in Urdu" from the supplied test.
    if (
        re.search(r"\b(let'?s|please|kindly)?\s*(talk|speak|continue|carry on|switch|respond|answer|chat)\s*(in|to)?\s*english\b", value)
        or re.search(r"\benglish\s*(please|mode|language|mein|me)\b", value)
        or "انگریزی میں بات" in value
    ):
        return "language_en"
    urdu_word = bool(re.search(r"\burdu[k]?|urdoo|urduu\b", value) or "اردو" in value)
    urdu_command = bool(
        re.search(r"\b(talk|speak|continue|start|switch|respond|answer|chat|stop)\s*(in|to)?\s*urdu[k]?\b", value)
        or re.search(r"\b(urdu[k]?|urdoo|urduu)\s*(please|mode|language|mein|me|main)\b", value)
        or re.search(r"\b(urdu mein baat|urdu me baat|baat urdu|mujh se urdu|mere se urdu)\b", value)
        or "اردو میں بات" in value or "اردو بول" in value or "اردو زبان" in value
        or (urdu_word and token_count <= 5)
    )
    if urdu_command:
        return "language_ur"
    hindi_word = bool(re.search(r"\bhindi\b", value) or "हिंदी" in value or "हिन्दी" in value)
    if (
        re.search(r"\b(talk|speak|continue|start|switch|respond|answer|chat)\s*(in|to)?\s*hindi\b", value)
        or re.search(r"\bhindi\s*(please|mode|language|mein|me|main)\b", value)
        or "हिंदी में बात" in value or "हिन्दी में बात" in value
        or (hindi_word and token_count <= 5)
    ):
        return "language_hi"
    if re.search(r"\b(next question|next one|move on|go ahead|skip (?:this|the)? ?question|agla sawal|agli sawal|agla question|next sawal|aage barhein|aage badhein)\b", value) or "اگلا سوال" in value or "سوال چھوڑ" in value:
        return "next"
    if re.search(r"\b(repeat|say (?:that|it) again|again please|dobara|phir se|repeat karo|dobara batayen)\b", value) or "سوال دوبارہ" in value or "سوال دہر" in value:
        return "repeat"
    if re.search(r"\b(end interview|finish interview|complete interview|interview khatam|finish now)\b", value) or "انٹرویو ختم" in value:
        return "finish"
    if re.search(r"\b(ask me|question about|questions about|field question|role question|technical question|ask about my skill|ask about my field)\b", value) or "فیلڈ کا سوال" in value or "تکنیکی سوال" in value:
        return "field_question"
    starts_question = re.match(r"^(what|why|when|where|who|how|can|could|would|will|do|does|is|are|may|should|tell me|please tell|company|internship|timing|salary|duration)", value)
    concise = token_count <= 24
    urdu_question = bool(re.search(r"(^|\s)(کیا|کیوں|کیسے|کب|کہاں|کون|کتنا|کونسا|بتائیں|سمجھائیں)(\s|$)", value))
    if "?" in value or "؟" in value or urdu_question or (concise and (starts_question or "can you" in value or "could you" in value)):
        return "clarification"
    return "answer"


def looks_like_candidate_question(candidate_english: str, candidate_original: str) -> bool:
    value = normalize_space(f"{candidate_english} {candidate_original}").casefold()
    if "?" in value or "؟" in value:
        return True
    if re.search(r"(^|\s)(کیا|کیوں|کیسے|کب|کہاں|کون|کتنا|کونسا|بتائیں|سمجھائیں)(\s|$)", value):
        return True
    return bool(re.match(r"^(what|why|when|where|who|how|can|could|would|will|do|does|is|are|may|should|tell me|please tell)\b", value))


async def classify_candidate_turn(candidate_english: str, candidate_original: str, active_question: dict[str, Any], duration_ms: int | None = None) -> str:
    """Classify every spoken turn using rules first, then the configured LLM brain.

    The transcript is available before this function runs. Adeeb therefore sees the
    candidate's actual answer, command, language request, or company question before
    choosing the next action.
    """
    combined = normalize_space(f"{candidate_english} {candidate_original}")
    rule = detect_intent(combined)
    if rule != "answer":
        return rule
    # A long response is overwhelmingly likely to be an interview answer. Do not let a
    # router LLM discard it just because it contains role, project, or question words.
    if (duration_ms or 0) >= 18000 or len(tokenize(combined)) >= 42:
        return "answer"
    # Short ambiguous turns are reviewed by the configured brain. Explicit commands
    # above remain deterministic and fast.
    result = await agent_json(
        "You are the fast brain/router for Adeeb AI Meeting Agent. Classify one short candidate voice turn. "
        "Choose exactly one intent from: language_en, language_ur, language_hi, next, repeat, finish, field_question, clarification, answer. "
        "language_ur means the candidate wants Adeeb to start speaking Urdu/Urdu mode. language_hi means Hindi/Hinglish mode. language_en means English mode. "
        "field_question means only that the candidate explicitly asks Adeeb to ask a field, role, skill, or technical question. A normal role or skill answer is always answer. "
        "clarification means the candidate asks about the company, internship, process, timing, role, document, or approved RAG knowledge. "
        "answer means a normal interview answer that should be recorded. Return JSON exactly: {\"intent\":\"...\"}.",
        f"English transcript: {candidate_english}\nOriginal/native transcript: {candidate_original}\nCurrent question: {active_question.get('original_text') or active_question.get('text')}",
        timeout=min(AGENT_COMMAND_TIMEOUT_SECONDS, 6),
        max_predict=40,
        model=OLLAMA_INTERACTION_MODEL,
    )
    intent = str((result or {}).get("intent", "answer")).casefold().strip()
    allowed = {"language_en", "language_ur", "language_hi", "next", "repeat", "finish", "field_question", "clarification", "answer"}
    if intent not in allowed:
        return "answer"
    # The router previously treated a normal Urdu/English project answer as a
    # clarification. Adeeb then answered on the candidate's behalf and invented a
    # customer-churn project. A clarification is now accepted only when the candidate
    # actually asked a question.
    if intent == "clarification" and not looks_like_candidate_question(candidate_english, candidate_original):
        return "answer"
    return intent


SKILL_KEYWORDS = [
    "python", "react", "javascript", "node", "express", "mongodb", "sql", "wordpress", "shopify",
    "seo", "aeo", "content writing", "copywriting", "graphic design", "canva", "figma",
    "video editing", "social media", "meta ads", "google ads", "machine learning", "data science",
    "excel", "power bi", "ui ux", "frontend", "backend", "full stack", "customer support",
]


def mentioned_skill(text: str) -> str | None:
    value = normalize_space(text).casefold()
    for skill in SKILL_KEYWORDS:
        if re.search(r"\b" + re.escape(skill) + r"\b", value):
            return skill
    # Also capture short patterns like "I know X" or "my skill is X".
    match = re.search(r"\b(?:i know|i use|i work on|my skill is|skills are|experience in|expertise in)\s+([a-zA-Z][a-zA-Z0-9 +#.-]{2,40})", value)
    if match:
        candidate = normalize_space(match.group(1)).strip(" .,")
        if candidate and len(candidate) <= 40:
            return candidate
    return None


def weak_answer(text: str, question: dict[str, Any]) -> str | None:
    tokens = [token for token in tokenize(text) if token not in {"thank", "thanks", "you", "okay", "ok", "yes", "no", "sure", "please"}]
    value = normalize_space(text).casefold().strip(".! ")
    if value in {"thank you", "thanks", "okay", "ok", "yes", "no", "sure"} or len(tokens) < 2:
        category = str(question.get("category") or "this question").casefold()
        return f"I still need a brief response about {category}. You can answer the question, or say next question to skip it."
    if str(question.get("id")) == "applying_role" and len(tokens) < 4:
        return "Please state the exact role or internship title you are applying for."
    return None


def recent_agent_questions(session_id: str, limit: int = 18) -> list[str]:
    kinds = (
        "question", "llm_question", "follow_up", "skill_follow_up",
        "role_specific_question", "clarify_answer", "skill_problem_followup",
        "project_question", "project_depth_follow_up", "skill_evidence_follow_up",
    )
    placeholders = ",".join("?" for _ in kinds)
    with db() as connection:
        rows = connection.execute(
            f"SELECT text_en FROM turns WHERE session_id = ? AND speaker = 'agent' AND kind IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (session_id, *kinds, limit),
        ).fetchall()
    return [clean_text(row["text_en"]) for row in rows if clean_text(row["text_en"])]


def question_similarity(left: str, right: str) -> float:
    left_norm = " ".join(tokenize(left))
    right_norm = " ".join(tokenize(right))
    if not left_norm or not right_norm:
        return 0.0
    left_tokens, right_tokens = set(left_norm.split()), set(right_norm.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(jaccard, sequence)


def is_repetitive_question(session_id: str, candidate_question: str, *, ignore: str | None = None) -> bool:
    candidate = clean_text(candidate_question)
    if not candidate:
        return True
    ignored = normalize_space(ignore)
    for previous in recent_agent_questions(session_id):
        if ignored and normalize_space(previous) == ignored:
            continue
        if question_similarity(candidate, previous) >= QUESTION_REPEAT_SIMILARITY:
            return True
    return False


def distinct_role_fallback(session_id: str, role_hint: str = "this role") -> str:
    options = [
        f"What steps do you use to check the quality or correctness of your work in {role_hint}?",
        f"Describe one mistake you learned from while working with a tool or project related to {role_hint}.",
        f"How would you explain one technical decision from your work in {role_hint} to a teammate?",
    ]
    for option in options:
        if not is_repetitive_question(session_id, option):
            return option
    return "What is one new skill you would need to learn first if selected for this role?"


async def generate_role_specific_question(session_id: str, candidate_request: str, active_question: dict[str, Any]) -> str:
    """Create one safe field-related interview prompt when the candidate asks to talk about a field.

    The result is a question, not a hiring decision. It uses previous candidate answers when available,
    especially the role they said they are applying for.
    """
    if field_question_limit_reached(session_id):
        return "We have already covered enough field-specific questions. Please answer the current interview question or say next question."

    answers = get_answers(session_id)
    applying_role = ""
    recent_answer = ""
    for answer in answers:
        if str(answer.get("question_id")) == "applying_role":
            applying_role = clean_text(answer.get("answer_english") or answer.get("model_english") or answer.get("answer_original") or "")
        recent_answer = clean_text(answer.get("answer_english") or answer.get("model_english") or answer.get("answer_original") or recent_answer)
    result = await agent_json(
        "You are Adeeb AI Meeting Agent. Generate exactly one short, fair interview question related to the candidate's stated role or field. "
        "The question must be about skills, tools, projects, learning, or work examples. Never ask about protected traits, family, religion, health, age, nationality, disability, or salary. "
        "Return JSON exactly: {\"question\":\"...\"}.",
        (
            f"Candidate request: {candidate_request}\n"
            f"Stated role answer: {applying_role or 'Not available yet'}\n"
            f"Recent answer: {recent_answer or 'Not available yet'}\n"
            f"Current prompt: {active_question.get('original_text') or active_question.get('text')}"
        ),
        timeout=INTERACTION_OLLAMA_TIMEOUT_SECONDS,
        max_predict=120,
        model=OLLAMA_INTERACTION_MODEL,
    )
    question = clean_text(str((result or {}).get("question", "")))
    role_hint = applying_role or clean_text(candidate_request) or "your selected field"
    if not question or "?" not in question or len(question) > 240 or is_repetitive_question(session_id, question):
        question = distinct_role_fallback(session_id, role_hint)
    return question




async def maybe_generate_skill_followup(session_id: str, answer: str, active_question: dict[str, Any]) -> str | None:
    """Ask one practical question about the actual skill/tool/field the candidate mentioned.

    This is intentionally LLM-backed: deterministic skill extraction gives the LLM
    a strong hint, then the brain writes one fair interview follow-up.
    """
    answer = clean_text(answer)
    if not answer or len(tokenize(answer)) < 2:
        return None
    if field_question_limit_reached(session_id):
        return None
    # Preserve V13's strong structured flow: introduction and problem-solving prompts
    # must not be hijacked into another generic project question. Field follow-ups are
    # reserved for the two objectives where they are genuinely useful.
    question_id = str(active_question.get("id") or "")
    if question_id not in {"skills_and_work", "role_specific"}:
        return None
    skill = mentioned_skill(answer)
    result = await agent_json(
        "You are Adeeb AI Meeting Agent. Read the candidate answer and create exactly one short practical follow-up question about the skill, tool, role, field, or project they mentioned. "
        "Do not ask protected personal questions. Do not make a hiring decision. Keep the question under 24 words. Return JSON exactly: {\"question\":\"...\"}.",
        f"Skill hint: {skill or 'infer from answer'}\nInterview question: {active_question.get('original_text') or active_question.get('text')}\nCandidate answer: {answer}",
        timeout=INTERACTION_OLLAMA_TIMEOUT_SECONDS,
        max_predict=100,
        model=OLLAMA_INTERACTION_MODEL,
    )
    question = clean_text(str((result or {}).get("question", "")))
    if not question or "?" not in question or len(question) > 220:
        focus = skill or "that skill"
        question = f"Can you describe one real example where you used {focus} in a project or task?"
    if is_repetitive_question(session_id, question):
        return None
    return question



async def maybe_generate_role_prompt_after_role_answer(session_id: str, role_answer: str, next_question: dict[str, Any] | None) -> str | None:
    """After the applying-role answer, ask one field-specific LLM question before normal flow.

    This runs after the first role answer has been transcribed, so the LLM can use the
    candidate's actual role before asking the next question.
    """
    if not role_answer or next_question is None:
        return None
    if field_question_limit_reached(session_id):
        return None
    result = await agent_json(
        "You are Adeeb AI Meeting Agent. Generate one short interview question for the candidate's field/role. It must test practical understanding, project experience, or tools. Keep it fair and under 26 words. Never ask about protected traits, family, religion, age, health, nationality, disability, salary, or politics. Return JSON exactly: {\"question\":\"...\"}.",
        f"Candidate says they are applying for or interested in: {role_answer}\nNext planned prompt: {next_question.get('text', '')}",
        timeout=AGENT_COMMAND_TIMEOUT_SECONDS,
        max_predict=110,
        model=OLLAMA_INTERACTION_MODEL,
    )
    prompt = clean_text(str((result or {}).get("question", "")))
    if not prompt or "?" not in prompt or len(prompt) > 220:
        prompt = f"Tell me about one practical project or skill that makes you suitable for {role_answer}."
    if is_repetitive_question(session_id, prompt):
        return None
    return prompt

def looks_like_agent_impersonating_candidate(text: str) -> bool:
    """Block an agent reply that invents personal work experience for itself/candidate."""
    value = normalize_space(text).casefold()
    patterns = [
        r"\bi (?:have|had) (?:made|built|created|worked|used|developed|completed)\b",
        r"\bmy (?:project|experience|skills?|university|semester|role)\b",
        r"\b(?:maine|main\s+ne)\s+(?:ek|ye|is|project|model|kaam|customer|heart|machine)",
        r"\bmain\s+apn[ae]\s+(?:experience|project|skills?)",
        r"میں نے.{0,40}(?:پراجیکٹ|ماڈل|کام|بنایا|استعمال)",
    ]
    return any(re.search(pattern, value, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def safe_generic_agent_answer(answer: str) -> str:
    value = clean_text(answer)
    if not value or looks_like_agent_impersonating_candidate(value):
        return "I can help with the interview process, but I cannot answer an interview question on your behalf. Please continue with your own answer, or ask HR for verified company information."
    return value


async def answer_candidate_question(session_id: str, candidate_question: str, active_question: dict[str, Any]) -> str:
    await maybe_auto_sync_google_sheet()
    matches = retrieve_knowledge(candidate_question)
    if not matches:
        if GENERIC_AGENT_QUESTIONS:
            result = await agent_json(
                "You are Adeeb AI Meeting Agent in a job interview. You may answer only generic interview-process, microphone, internet, browser, resume, portfolio, or general preparation questions. Never answer the active interview question for the candidate, never claim that you built a project or have candidate experience, and never invent a sample as if it were the candidate’s own answer. If the candidate asks for company-specific facts, salaries, selection chances, private policies, personal data, legal/medical advice, or anything not safe for an interview, say you do not have verified information and suggest asking HR. Return JSON exactly: {\"answer\":\"...\"}.",
                f"Candidate question: {candidate_question}\nCurrent interview prompt: {active_question.get('original_text') or active_question.get('text')}",
                timeout=INTERACTION_OLLAMA_TIMEOUT_SECONDS,
                max_predict=180,
                model=OLLAMA_INTERACTION_MODEL,
            )
            answer = safe_generic_agent_answer(str((result or {}).get("answer", "")))
            if answer:
                return await agent_spoken_text(session_id, answer)
        return await agent_spoken_text(session_id, local_agent_message("fallback_continue", preferred_agent_language(session_or_404(session_id))))
    evidence = rag_evidence(matches)
    result = await agent_json(
        "You are Adeeb AI Meeting Agent. Answer in clear English using only the verified source excerpts. "
        "Do not invent details, policies, salary, hiring outcomes, fees, dates, benefits, or promises. "
        "Do not mention bank accounts or sensitive information. Return JSON exactly: {\"answer\":\"...\"}. "
        "Keep the answer under 80 words and end by inviting the candidate to continue the interview.",
        f"Candidate question: {candidate_question}\n\nCurrent interview prompt: {active_question.get('original_text') or active_question.get('text')}\n\nVerified source excerpts:\n{evidence}",
        timeout=INTERACTION_OLLAMA_TIMEOUT_SECONDS,
        max_predict=180,
        model=OLLAMA_INTERACTION_MODEL,
    )
    answer = safe_generic_agent_answer(str((result or {}).get("answer", "")))
    if answer:
        return await agent_spoken_text(session_id, answer)
    # Keep the fallback honest rather than presenting raw WhatsApp-style data as English fact.
    return await agent_spoken_text(session_id, "I found relevant approved information, but the answer model is not available to prepare a verified response. Please contact the recruiter after the interview, and we can continue with the current question.")


async def generate_llm_planned_question(session_id: str, planned_question: dict[str, Any], latest_answer: str) -> str:
    """Personalise only the staged skill/problem and final project objectives.

    questions.json remains the source of truth for coverage. The first three prompts
    stay fixed. Question four may become a skill-based problem-solving follow-up after
    its prerequisite transcript is ready. Question five becomes a role-specific project
    prompt after the first four background transcripts are ready.
    """
    fallback = clean_text(planned_question.get("text", ""))
    question_id = str(planned_question.get("id", ""))
    adaptive_from_previous = bool(planned_question.get("adaptive_from_previous", False))
    if not LLM_DRIVEN_INTERVIEW or not ADAPT_PLANNED_QUESTIONS or not fallback:
        return fallback
    if question_id not in {"problem_solving", "role_specific"}:
        return fallback
    if question_id == "problem_solving" and not adaptive_from_previous:
        return fallback

    answers = get_answers(session_id)[-6:]
    history: list[str] = []
    for item in answers:
        answer_text = clean_text(
            item.get("answer_english")
            or item.get("model_english")
            or item.get("answer_original")
            or ""
        )
        if not answer_text or answer_text.startswith("["):
            continue
        history.append(
            f"Question ID: {item.get('question_id')}\n"
            f"Question: {clean_text(item.get('question_text', ''))}\n"
            f"Candidate answer: {answer_text}"
        )

    role = clean_text(session_or_404(session_id)["role_name"]) or "the stated role"
    if question_id == "problem_solving":
        system_prompt = (
            "You are Adeeb AI Meeting Agent. Create exactly one concise fourth interview question. "
            "It must follow up on a skill, tool, coursework example, or project the candidate already mentioned, "
            "and it must test problem solving by asking for a real challenge, the candidate's action, and the result. "
            "Do not invent a skill, project, or fact. If the earlier answer is vague, preserve the planned fallback objective. "
            "Do not repeat an earlier question and do not ask protected personal questions. "
            "Return JSON exactly: {\"question\":\"...\"}."
        )
    else:
        system_prompt = (
            "You are Adeeb AI Meeting Agent. Create exactly one final project-based interview question for the candidate's stated role. "
            "Ask for one concrete project, its goal, the candidate's personal contribution, tools or methods, and the result. "
            "Use only details supported by the previous transcripts. Do not invent experience, answer for the candidate, repeat an earlier question, "
            "or make a hiring decision. Return JSON exactly: {\"question\":\"...\"}."
        )

    result = await agent_json(
        system_prompt,
        (
            f"Planned objective: {fallback}\n"
            f"Stated role: {role}\n"
            f"Latest answer signal: {clean_text(latest_answer)}\n"
            "Previously asked questions (do not repeat or paraphrase them):\n"
            + "\n".join(f"- {item}" for item in recent_agent_questions(session_id))
            + "\n\nAvailable candidate transcripts:\n"
            + ("\n\n".join(history) if history else "No reliable transcript is available yet.")
        ),
        timeout=max(AGENT_COMMAND_TIMEOUT_SECONDS, 8),
        max_predict=150,
        model=OLLAMA_INTERACTION_MODEL,
    )
    question = clean_text(str((result or {}).get("question", "")))
    if not question or "?" not in question or len(question) > 320 or is_repetitive_question(session_id, question):
        if question_id == "problem_solving":
            return fallback
        return (
            f"For {role}, describe one project that best proves your ability. "
            "What was the goal, what did you personally do, which tools or methods did you use, and what was the result?"
        )
    return question


async def generate_project_followup(
    session_id: str,
    combined_answer: str,
    active_question: dict[str, Any],
    stage: int,
) -> str:
    """Generate the two required project follow-ups without repeating or inventing.

    stage 1 checks concrete project depth and personal contribution.
    stage 2 checks skill evidence, verification, result, or learning.
    """
    stage = 1 if stage <= 1 else 2
    if stage == 1:
        instruction = (
            "Ask exactly one short follow-up about the concrete project details the candidate actually mentioned. "
            "Focus on their personal contribution and one tool, method, or technical decision."
        )
        fallback = "What did you personally build or do in that project, and which tool or method did you use for your part?"
    else:
        instruction = (
            "Ask exactly one short follow-up that tests evidence of skill from the project. "
            "Focus on how the candidate checked the result, handled a challenge, measured success, or what they learned."
        )
        fallback = "Which skill was most important in that project, and how did you verify the result or learn from the outcome?"

    result = await agent_json(
        "You are Adeeb AI Meeting Agent. " + instruction + " "
        "Use only the candidate's supplied answer. Do not invent a project, metric, tool, result, or experience. "
        "Do not ask protected personal questions, do not make a hiring decision, and do not repeat a previous question. "
        "Keep the question under 32 words. Return JSON exactly: {\"question\":\"...\"}.",
        (
            f"Original project question: {active_question.get('original_text') or active_question.get('text')}\n"
            f"Candidate project answer so far: {clean_text(combined_answer)}\n"
            "Already asked questions:\n" + "\n".join(f"- {q}" for q in recent_agent_questions(session_id))
        ),
        timeout=max(AGENT_COMMAND_TIMEOUT_SECONDS, 8),
        max_predict=120,
        model=OLLAMA_INTERACTION_MODEL,
    )
    question = clean_text(str((result or {}).get("question", "")))
    if not question or "?" not in question or len(question) > 260 or is_repetitive_question(session_id, question):
        question = fallback
    if is_repetitive_question(session_id, question):
        question = (
            "What exact part of the project was your responsibility?"
            if stage == 1
            else "What evidence showed that your project work was successful?"
        )
    return question


async def decide_follow_up(session_id: str, snapshot: dict[str, Any], question: dict[str, Any], answer: str, count: int) -> str | None:
    permitted = int(question.get("max_followups", snapshot.get("default_max_followups", MAX_FOLLOWUPS)))
    if not bool(question.get("adaptive_follow_up", True)) or count >= permitted or project_question_limit_reached(session_id):
        return None
    result = await agent_json(
        "You are a meeting agent supporting a human hiring team. Decide whether exactly one short factual follow-up is needed. "
        "Ask a follow-up only when the answer lacks an essential detail needed to understand the candidate's example. "
        "Never ask about protected traits, health, family, religion, nationality, age, disability, marital status, or other sensitive attributes. "
        "Never make or imply a hiring decision. Return JSON exactly: {\"action\":\"follow_up\"|\"advance\",\"message\":\"...\"}. "
        "Use an empty message for advance. If follow_up, ask one English question under 35 words.",
        f"Original interview question: {question.get('original_text') or question.get('text')}\n\nCandidate answer so far: {answer}",
        timeout=INTERACTION_OLLAMA_TIMEOUT_SECONDS,
        max_predict=130,
        model=OLLAMA_INTERACTION_MODEL,
    )
    if not result or str(result.get("action", "")).casefold() != "follow_up":
        return None
    message = clean_text(result.get("message", ""))
    if not message or len(message) > 260 or "?" not in message or is_repetitive_question(session_id, message):
        return None
    return message


# ---------- Quality / evaluation ----------
def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, source_token in enumerate(ref, 1):
        current = [i]
        for j, target_token in enumerate(hyp, 1):
            substitution = previous[j - 1] + (source_token != target_token)
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            current.append(min(substitution, insertion, deletion))
        previous = current
    return round(previous[-1] / len(ref), 4)


def quality_metrics() -> dict[str, Any]:
    with db() as connection:
        rows = connection.execute(
            "SELECT word_error_rate FROM answers WHERE quality_status = 'reviewed' AND word_error_rate IS NOT NULL"
        ).fetchall()
        consented = connection.execute(
            "SELECT COUNT(*) AS count FROM answers a JOIN sessions s ON s.id = a.session_id WHERE s.quality_consent = 1 AND a.quality_status = 'reviewed'"
        ).fetchone()["count"]
    values = [float(row["word_error_rate"]) for row in rows]
    average = sum(values) / len(values) if values else None
    score = None if average is None else round(max(0.0, 100 * (1 - average)), 1)
    return {
        "reviewed_turns": len(values),
        "consented_training_turns": int(consented or 0),
        "average_wer": None if average is None else round(average, 4),
        "estimated_word_accuracy": score,
        "target": 90,
        "evaluation_ready": len(values) >= 30,
        "message": "Review at least 30 consented, representative turns before comparing against the 90% target." if len(values) < 30 else "Measured from human-corrected transcripts; review by language and audio quality before making a deployment claim.",
    }


def refresh_learned_hints() -> list[str]:
    with db() as connection:
        rows = connection.execute(
            """SELECT a.reviewed_text FROM answers a JOIN sessions s ON s.id = a.session_id
               WHERE s.quality_consent = 1 AND a.quality_status = 'reviewed' AND a.reviewed_text IS NOT NULL"""
        ).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        for token in tokenize(row["reviewed_text"]):
            if len(token) >= 3 and token not in COMMON_TRAINING_WORDS and not token.isdigit():
                counts[token] += 1
    hints = [token for token, count in counts.most_common(80) if count >= 1]
    temp = LEARNED_HINTS_PATH.with_suffix(".tmp")
    temp.write_text(json_dump(hints) + "\n", encoding="utf-8")
    temp.replace(LEARNED_HINTS_PATH)
    return hints


# ---------- Answer persistence ----------
def paths_from_field(value: str | None) -> list[str]:
    parsed = safe_json_loads(value, [])
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    if normalize_space(value):
        return [str(value)]
    return []


def save_audio_path(existing: str | None, path: Path | None) -> str | None:
    paths = paths_from_field(existing)
    if path is not None:
        paths.append(portable_storage_path(path))
    # Deduplicate while preserving order.
    unique = list(dict.fromkeys(paths))
    return json.dumps(unique) if unique else None


def upsert_candidate_answer(session: sqlite3.Row, index: int, base_question: dict[str, Any], transcript: dict[str, Any], audio_path: Path | None) -> str:
    session_id = session["id"]
    with db() as connection:
        existing = connection.execute("SELECT * FROM answers WHERE session_id = ? AND question_id = ?", (session_id, base_question["id"])).fetchone()
        if existing:
            combined_model = clean_text(f"{existing['model_english'] or existing['answer_english']} {transcript['english']}")
            combined_answer = clean_text(f"{existing['answer_english']} {transcript['english']}") if not existing["reviewed_text"] else str(existing["answer_english"])
            connection.execute(
                """UPDATE answers SET answer_original = ?, answer_english = ?, model_english = ?, detected_language = ?, processing_ms = ?,
                   transcription_status = 'ready', processing_error = NULL, spoken_language = ?, transcript_ready_at = ?, is_final = 0,
                   audio_path = ?, model_version = ?, final_pass_status = CASE WHEN final_pass_status = 'ready' THEN 'ready' ELSE 'not_requested' END
                   WHERE id = ?""",
                (combined_model, combined_answer, combined_model, transcript["language"], transcript["processing_ms"], transcript["language"], utc_now(), save_audio_path(existing["audio_path"], audio_path), transcript["model_version"], existing["id"]),
            )
            sync_candidate_plaintext(session_id)
            return combined_answer
        connection.execute(
            """INSERT INTO answers (session_id, question_id, question_index, question_category, question_text,
               answer_original, answer_english, model_english, reviewed_text, detected_language, created_at, candidate_edited,
               processing_ms, transcription_status, audio_path, spoken_language, processing_error, transcript_ready_at, is_final,
               model_version, quality_status, final_pass_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, 'ready', ?, ?, NULL, ?, 0, ?, 'unreviewed', 'not_requested')""",
            (
                session_id,
                base_question["id"],
                index,
                str(base_question.get("category", "General")),
                str(base_question.get("text", "")),
                transcript["original"],
                transcript["english"],
                transcript["english"],
                transcript["language"],
                utc_now(),
                transcript["processing_ms"],
                json.dumps([portable_storage_path(audio_path)]) if audio_path else None,
                transcript["language"],
                utc_now(),
                transcript["model_version"],
            ),
        )
    sync_candidate_plaintext(session_id)
    return transcript["english"]


def finalize_current_question(session_id: str, index: int, question: dict[str, Any], *, skipped: bool = False) -> None:
    with db() as connection:
        existing = connection.execute("SELECT id FROM answers WHERE session_id = ? AND question_id = ?", (session_id, question["id"])).fetchone()
        if existing:
            connection.execute("UPDATE answers SET is_final = 1 WHERE id = ?", (existing["id"],))
        else:
            label = "[Candidate requested the next question without answering this one.]" if skipped else "[No candidate answer recorded.]"
            connection.execute(
                """INSERT INTO answers (session_id, question_id, question_index, question_category, question_text,
                   answer_original, answer_english, model_english, detected_language, created_at, candidate_edited,
                   transcription_status, is_final, quality_status, final_pass_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Not applicable', ?, 0, 'skipped', 1, 'not_reviewable', 'not_requested')""",
                (session_id, question["id"], index, str(question.get("category", "General")), str(question.get("text", "")), label, label, label, utc_now()),
            )
        connection.execute("UPDATE sessions SET active_prompt = '', follow_up_count = 0 WHERE id = ?", (session_id,))
    sync_candidate_plaintext(session_id)


def build_transcript(session: sqlite3.Row, answers: list[dict[str, Any]]) -> str:
    snapshot = safe_json_loads(session["question_snapshot"], {})
    lines = [
        f"Meeting: {snapshot.get('meeting_title', snapshot.get('interview_title', 'Interview'))}",
        f"Candidate: {public_candidate_name(session['candidate_name'])}",
        f"Created (UTC): {session['created_at']}",
        "",
    ]
    for number, answer in enumerate(answers, 1):
        lines.append(f"{number}. [{answer.get('question_category') or 'Question'}] Interviewer: {answer['question_text']}")
        original = clean_text(answer.get("answer_original", ""))
        english = clean_text(answer.get("answer_english", ""))
        language = str(answer.get("detected_language") or "")
        if original and original != english and not original.startswith("["):
            lines.append(f"Original {language or 'spoken-language'} transcript: {original}")
        lines.extend([f"Candidate answer in English: {english}", ""])
    return "\n".join(lines).strip()


def summary_fallback(session: sqlite3.Row, answers: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    return {
        "overall_summary": f"{public_candidate_name(session['candidate_name'])} completed {len(answers)} interview question(s). Review the evidence in the transcript using human judgement.",
        "strengths_observed": [],
        "areas_to_clarify": [],
        "recommended_follow_up_questions": [],
        "evidence_by_question": [{"question": answer["question_text"], "answer": answer["answer_english"]} for answer in answers],
        "human_review_note": reason,
    }


async def generate_summary(session: sqlite3.Row, answers: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    transcript = build_transcript(session, answers)
    result = await ollama_json(
        "You are an evidence-based interview note-taker assisting a human hiring team. Use only the supplied transcript. "
        "Do not infer protected traits. Do not make a hire/no-hire decision. Return JSON exactly with keys: overall_summary, strengths_observed, areas_to_clarify, recommended_follow_up_questions, evidence_by_question, human_review_note.",
        f"Transcript:\n{transcript}",
        timeout=OLLAMA_TIMEOUT_SECONDS,
        max_predict=760,
        model=OLLAMA_SUMMARY_MODEL,
    )
    if result is None:
        return summary_fallback(session, answers, "Local summary model was unavailable. Review the English transcript."), "fallback"
    return result, "ollama"


async def high_accuracy_retranscribe(session_id: str) -> None:
    """Optional after-meeting pass. It never blocks the candidate meeting."""
    session = session_or_404(session_id)
    snapshot = safe_json_loads(session["question_snapshot"], {})
    answers = get_answers(session_id)
    for answer in answers:
        paths = paths_from_field(answer.get("audio_path"))
        if not paths:
            continue
        question = {
            "id": answer["question_id"],
            "text": answer["question_text"],
            "original_text": answer["question_text"],
            "category": answer.get("question_category", "General"),
        }
        pieces: list[str] = []
        language = str(answer.get("spoken_language") or "auto").casefold()
        try:
            for path in paths:
                if Path(path).exists():
                    result = await transcribe_turn(path, snapshot, question, language, final_pass=True)
                    pieces.append(result["english"])
            if pieces:
                revised = clean_text(" ".join(pieces))
                with db() as connection:
                    if answer.get("quality_status") == "reviewed":
                        connection.execute(
                            "UPDATE answers SET model_english = ?, final_pass_status = 'ready', model_version = ? WHERE id = ?",
                            (revised, WHISPER_FINAL_MODEL, answer["id"]),
                        )
                    else:
                        connection.execute(
                            "UPDATE answers SET answer_original = ?, answer_english = ?, model_english = ?, final_pass_status = 'ready', model_version = ? WHERE id = ?",
                            (result["original"], revised, revised, result["model_version"], answer["id"]),
                        )
        except Exception:
            logger.exception("Final transcription failed for answer %s", answer.get("id"))
            with db() as connection:
                connection.execute("UPDATE answers SET final_pass_status = 'failed' WHERE id = ?", (answer["id"],))


async def postprocess_session(session_id: str) -> None:
    try:
        await wait_for_pending_transcriptions(session_id)
        if ENABLE_FINAL_RETRANSCRIBE:
            await high_accuracy_retranscribe(session_id)
        session = session_or_404(session_id)
        answers = get_answers(session_id)
        summary, source = await generate_summary(session, answers)
        with db() as connection:
            connection.execute("UPDATE sessions SET final_summary = ?, summary_source = ? WHERE id = ?", (json.dumps(summary, ensure_ascii=False), source, session_id))
        sync_candidate_plaintext(session_id)
    except Exception:
        logger.exception("Post-processing failed for meeting %s", session_id)
        with db() as connection:
            connection.execute("UPDATE sessions SET summary_source = 'failed' WHERE id = ?", (session_id,))


def completion_response(session_id: str, snapshot: dict[str, Any]) -> tuple[bool, str]:
    with db() as connection:
        connection.execute(
            "UPDATE sessions SET status = 'completed', completed_at = COALESCE(completed_at, ?), final_summary = NULL, summary_source = 'generating', active_prompt = '', follow_up_count = 0 WHERE id = ?",
            (utc_now(), session_id),
        )
    start_background_task(postprocess_session(session_id))
    return True, str(snapshot.get("closing_message") or "Thank you. Your interview has been submitted for human review.")


def purge_expired_audio() -> int:
    """Optional retention cleanup. AUDIO_RETENTION_DAYS=0 keeps recordings until HR deletes them manually."""
    if AUDIO_RETENTION_DAYS <= 0:
        return 0
    cutoff = time.time() - AUDIO_RETENTION_DAYS * 86400
    removed = 0
    for path in UPLOAD_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Removed %s expired audio recording(s).", removed)
    return removed


# ---------- Startup ----------
async def warm_live_model() -> None:
    try:
        await get_whisper_model(WHISPER_LIVE_MODEL)
        logger.info("Live speech model ready: %s", WHISPER_LIVE_MODEL)
    except Exception:
        logger.exception("Speech model warm-up failed. The app will retry when a candidate speaks.")


@app.on_event("startup")
async def startup() -> None:
    init_database()
    purge_expired_audio()
    sync_all_candidate_plaintext()
    if not KNOWLEDGE_PATH.exists():
        KNOWLEDGE_PATH.write_text(DEFAULT_KNOWLEDGE_BASE, encoding="utf-8")
    if not RAG_SETTINGS_PATH.exists():
        save_rag_settings(default_rag_settings())
    # A Groq-backed installation should not download/load a large local model at every
    # startup. Local Whisper still loads automatically if Groq is unavailable.
    if PRELOAD_LOCAL_WHISPER or not (GROQ_STT_ENABLED and GROQ_API_KEY and GROQ_STT_ALL_LANGUAGES):
        start_background_task(warm_live_model())
    start_background_task(warm_ollama())
    start_background_task(resume_queued_transcriptions())


# ---------- Views ----------
def interview_context(request: Request, session: sqlite3.Row) -> dict[str, Any]:
    return {"request": request, "session": dict(session), "config": safe_json_loads(session["question_snapshot"], {})}


@app.get("/healthz")
def public_health_check() -> dict[str, Any]:
    """Small unauthenticated readiness check used by Windows and Cloudflare launchers."""
    return {"ok": True, "status": "ready", "service": "Adeeb AI Meeting Agent", "version": "15.5.2"}


@app.get("/", response_class=HTMLResponse)
def admin_home(request: Request, _: str = Depends(require_admin)) -> Response:
    return templates.TemplateResponse(request=request, name="admin.html", context={"admin_unprotected": not bool(ADMIN_PASSWORD)})


@app.get("/join", response_class=HTMLResponse)
def universal_join_page(request: Request) -> Response:
    config = load_questions()
    return templates.TemplateResponse(request=request, name="join.html", context={"config": config})


@app.get("/interview/{session_id}", response_class=HTMLResponse)
def interview_page(session_id: str, request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="interview.html", context=interview_context(request, session_or_404(session_id)))


@app.get("/results/{session_id}", response_class=HTMLResponse)
def results_page(session_id: str, request: Request, _: str = Depends(require_admin)) -> Response:
    return templates.TemplateResponse(request=request, name="results.html", context=interview_context(request, session_or_404(session_id)))


# ---------- Admin APIs ----------
@app.get("/api/health")
async def health(_: str = Depends(require_admin)) -> dict[str, Any]:
    ollama_ok = False
    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if response.is_success:
                ollama_ok = True
                models = [model.get("name", "") for model in response.json().get("models", [])]
    except httpx.HTTPError:
        pass
    settings = load_rag_settings()
    return {
        "status": "ok",
        "live_speech_model": WHISPER_LIVE_MODEL,
        "speech_model_loaded": WHISPER_LIVE_MODEL in _whisper_models,
        "urdu_speech_model": WHISPER_URDU_MODEL,
        "urdu_speech_model_loaded": bool(WHISPER_URDU_MODEL and WHISPER_URDU_MODEL in _whisper_models),
        "final_speech_model": WHISPER_FINAL_MODEL,
        "final_pass_enabled": ENABLE_FINAL_RETRANSCRIBE,
        "ollama_reachable": ollama_ok,
        "ollama_models": models,
        "interaction_model": OLLAMA_INTERACTION_MODEL,
        "summary_model": OLLAMA_SUMMARY_MODEL,
        "rag_source": settings["source_mode"],
        "quality": quality_metrics(),
        "admin_password_configured": bool(ADMIN_PASSWORD),
        "identity_encryption_source": "env" if identity_protector.using_env_secrets else "local_generated",
        "plaintext_candidate_data_enabled": PLAINTEXT_CANDIDATE_DATA,
        "candidate_data_root": str(CANDIDATE_DATA_ROOT),
        "urdu_asr_provider": "groq" if should_use_groq_stt("ur") else "local",
        "groq_stt_model": GROQ_STT_MODEL if should_use_groq_stt("ur") else "",
        "audio_retention_enabled": RETAIN_CANDIDATE_AUDIO,
        "audio_retention_days": AUDIO_RETENTION_DAYS,
        "llm_brain_configured": bool(GROQ_API_KEY or GEMINI_API_KEY or OPENROUTER_API_KEY or ollama_ok),
        "llm_provider_order": [LLM_PROVIDER] if LLM_PROVIDER not in {"", "auto"} else ["groq", "gemini", "openrouter", "ollama"],
        "voice_api_configured": bool(ELEVENLABS_API_KEY),
        "tts_provider": TTS_PROVIDER,
        "edge_tts_enabled": EDGE_TTS_ENABLED,
        "edge_tts_package_available": importlib.util.find_spec("edge_tts") is not None,
        "universal_join_path": UNIVERSAL_JOIN_PATH,
        "data_directory": str(DATA_DIR),
        "database_path": str(DB_PATH),
    }


@app.get("/api/admin/questions")
def read_questions(_: str = Depends(require_admin)) -> dict[str, Any]:
    return load_questions()


@app.put("/api/admin/questions")
async def save_questions(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        payload = await request.json()
        validate_questions(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    temp = QUESTIONS_PATH.with_suffix(".tmp")
    temp.write_text(json_dump(payload) + "\n", encoding="utf-8")
    temp.replace(QUESTIONS_PATH)
    return {"ok": True, "question_count": len(payload["questions"])}


@app.get("/api/admin/knowledge")
def read_knowledge(_: str = Depends(require_admin)) -> dict[str, str]:
    content = KNOWLEDGE_PATH.read_text(encoding="utf-8") if KNOWLEDGE_PATH.exists() else DEFAULT_KNOWLEDGE_BASE
    return {"content": content}


@app.put("/api/admin/knowledge")
async def save_knowledge(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    payload = await request.json()
    content = str(payload.get("content", "")).strip()
    if len(content) < 20:
        raise HTTPException(status_code=422, detail="Add at least a short approved company or role note.")
    if len(content) > 80000:
        raise HTTPException(status_code=422, detail="Knowledge base is too long. Keep it below 80,000 characters.")
    temp = KNOWLEDGE_PATH.with_suffix(".tmp")
    temp.write_text(content + "\n", encoding="utf-8")
    temp.replace(KNOWLEDGE_PATH)
    return {"ok": True, "characters": len(content)}


@app.get("/api/admin/rag")
def rag_status(_: str = Depends(require_admin)) -> dict[str, Any]:
    settings = load_rag_settings()
    records = available_rag_records()
    return {
        "settings": settings,
        "available_records": len(records),
        "blocked_sensitive_records": sum(1 for record in records if record.get("sensitive")),
        "source_note": "Sensitive payment/account-style FAQ rows are blocked from candidate answers unless RAG_ALLOW_SENSITIVE=true is explicitly set in .env.",
    }


@app.put("/api/admin/rag")
async def update_rag_settings(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    payload = await request.json()
    current = load_rag_settings()
    mode = str(payload.get("source_mode", current["source_mode"]))
    url = normalize_space(payload.get("google_sheet_csv_url", current["google_sheet_csv_url"]))
    minutes = payload.get("auto_sync_minutes", current["auto_sync_minutes"])
    if mode not in {"local_csv", "google_sheet"}:
        raise HTTPException(status_code=422, detail="RAG source must be local_csv or google_sheet.")
    if mode == "google_sheet" and not re.match(r"^https://", url, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="Add an HTTPS published Google Sheet CSV URL.")
    current.update({"source_mode": mode, "google_sheet_csv_url": url, "auto_sync_minutes": minutes})
    return {"ok": True, "settings": save_rag_settings(current)}


@app.post("/api/admin/rag/sync")
async def sync_rag(_: str = Depends(require_admin)) -> dict[str, Any]:
    return await sync_google_sheet()




@app.get("/api/admin/rag/pdf")
def pdf_rag_status(_: str = Depends(require_admin)) -> dict[str, Any]:
    return pdf_rag_status_payload()


@app.post("/api/admin/rag/pdf/upload")
async def upload_pdf_rag(file: UploadFile = File(...), _: str = Depends(require_admin)) -> dict[str, Any]:
    filename = Path(file.filename or "knowledge.pdf").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Upload a PDF file only.")
    doc_id = secrets.token_urlsafe(12)
    safe_name = re.sub(r"[^A-Za-z0-9_. -]+", "_", filename).strip() or "knowledge.pdf"
    final_path = PDF_KNOWLEDGE_DIR / f"{doc_id}_{safe_name}"
    temp_path = final_path.with_suffix(".tmp")
    bytes_written = 0
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_PDF_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"PDF is too large. Maximum allowed size is {MAX_PDF_UPLOAD_MB} MB.")
                handle.write(chunk)
        temp_path.replace(final_path)
        text, pages = await run_in_threadpool(extract_pdf_text, final_path)
        chunks = chunk_pdf_text(text)
        if not chunks:
            final_path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="No readable text was found in this PDF. If it is a scanned image PDF, convert it with OCR first.")
        records = []
        title = safe_name.rsplit(".", 1)[0]
        for index, chunk in enumerate(chunks, 1):
            chunk_title = f"{title} · chunk {index}"
            records.append({
                "id": f"pdf-{doc_id}-{index}",
                "title": chunk_title,
                "content": chunk,
                "source": f"pdf:{safe_name}",
                "sensitive": is_sensitive_record(chunk_title, chunk),
                "doc_id": doc_id,
                "filename": safe_name,
            })
        cache = load_pdf_cache()
        cache["documents"] = [doc for doc in cache.get("documents", []) if doc.get("doc_id") != doc_id]
        cache["documents"].append({
            "doc_id": doc_id,
            "filename": safe_name,
            "stored_path": str(final_path.relative_to(BASE_DIR)),
            "uploaded_at": utc_now(),
            "pages": pages,
            "characters": len(text),
            "chunks": len(records),
            "records": records,
        })
        save_pdf_cache(cache)
        return {"ok": True, "doc_id": doc_id, "filename": safe_name, "pages": pages, "chunks": len(records), "characters": len(text)}
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        logger.exception("PDF import failed")
        raise HTTPException(status_code=500, detail=f"Could not import PDF: {type(exc).__name__}") from exc
    finally:
        await file.close()


@app.delete("/api/admin/rag/pdf/{doc_id}")
def delete_pdf_rag(doc_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    cache = load_pdf_cache()
    kept = []
    removed = None
    for doc in cache.get("documents", []):
        if doc.get("doc_id") == doc_id:
            removed = doc
        else:
            kept.append(doc)
    if not removed:
        raise HTTPException(status_code=404, detail="PDF document was not found.")
    path_text = str(removed.get("stored_path") or "")
    path = (BASE_DIR / path_text).resolve() if path_text else None
    if path and str(path).startswith(str(PDF_KNOWLEDGE_DIR.resolve())):
        path.unlink(missing_ok=True)
    save_pdf_cache({"documents": kept})
    return {"ok": True, "removed": removed.get("filename", doc_id)}


@app.get("/api/admin/rag/pdf/questions")
def pdf_rag_questions(_: str = Depends(require_admin)) -> dict[str, Any]:
    questions = generate_pdf_questions(5)
    return {"questions": questions, "question_count": len(questions)}

@app.get("/api/admin/quality")
def get_quality(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {"metrics": quality_metrics(), "learned_hint_count": len(load_learned_hints())}


@app.get("/api/admin/quality/export")
def export_quality(_: str = Depends(require_admin)) -> Response:
    with db() as connection:
        rows = connection.execute(
            """SELECT s.id AS meeting_id, s.candidate_name, s.quality_consent, a.question_text, a.detected_language,
                      a.model_english, a.reviewed_text, a.word_error_rate, a.audio_path, a.created_at
               FROM answers a JOIN sessions s ON s.id = a.session_id
               WHERE a.quality_status = 'reviewed' AND s.quality_consent = 1
               ORDER BY a.created_at DESC"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["meeting_id", "candidate_name", "quality_consent", "question_text", "detected_language", "model_transcript", "human_corrected_transcript", "word_error_rate", "audio_retained", "created_at"])
    for row in rows:
        writer.writerow([
            row["meeting_id"], row["candidate_name"], row["quality_consent"], row["question_text"], row["detected_language"], row["model_english"], row["reviewed_text"], row["word_error_rate"], bool(paths_from_field(row["audio_path"])), row["created_at"],
        ])
    return PlainTextResponse(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="consented-transcript-quality-data.csv"'})


@app.get("/api/admin/universal-link")
def universal_join_link(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    return {
        "join_path": UNIVERSAL_JOIN_PATH,
        "join_url": f"{current_request_origin(request)}{UNIVERSAL_JOIN_PATH}",
        "note": "This link always uses the host currently open in the browser; no old Cloudflare hostname is stored.",
    }


@app.post("/api/admin/sessions")
async def create_session(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    payload = await request.json()
    scheduled_for = normalize_space(payload.get("scheduled_for"))
    snapshot = load_questions()
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions (id, candidate_name, role_name, status, created_at, question_snapshot, scheduled_for) VALUES (?, '', '', 'created', ?, ?, ?)",
            (session_id, utc_now(), json.dumps(snapshot, ensure_ascii=False), scheduled_for),
        )
    return {"id": session_id, "interview_url": f"/interview/{session_id}", "results_url": f"/results/{session_id}"}


@app.get("/api/admin/sessions")
def list_sessions(_: Request, __: str = Depends(require_admin)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            """SELECT s.*, COUNT(a.id) AS answer_count,
               COALESCE(SUM(CASE WHEN COALESCE(a.is_final, 1) = 1 THEN 1 ELSE 0 END), 0) AS final_answer_count
               FROM sessions s LEFT JOIN answers a ON a.session_id = s.id
               GROUP BY s.id ORDER BY s.created_at DESC LIMIT 150"""
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        total = len(safe_json_loads(item["question_snapshot"], {}).get("questions", []))
        item["candidate_display_name"] = public_candidate_name(item["candidate_name"])
        item["father_name"] = identity_protector.decrypt(item.get("father_name_encrypted")) if item.get("father_name_encrypted") else "—"
        raw_cnic = identity_protector.decrypt(item.get("cnic_encrypted")) if item.get("cnic_encrypted") else ""
        item["cnic_full"] = format_cnic(raw_cnic) if raw_cnic else "—"
        # The universal registration link is shared; do not expose candidate-specific private URLs in the tracker.
        item["universal_join_url"] = UNIVERSAL_JOIN_PATH
        item["results_url"] = f"/results/{item['id']}"
        encrypted_resume = item.get("resume_code_encrypted")
        item["resume_code"] = identity_protector.decrypt(encrypted_resume) if encrypted_resume else ""
        item["progress"] = f"{item['final_answer_count']}/{total}"
        output.append({**safe_admin_session_dict(item), "father_name": item["father_name"], "cnic_full": item["cnic_full"], "resume_code": item["resume_code"], "universal_join_url": item["universal_join_url"], "results_url": item["results_url"], "progress": item["progress"]})
    return output


@app.get("/api/admin/sessions/{session_id}")
def get_admin_session(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    session = session_or_404(session_id)
    answers = get_answers(session_id)
    for answer in answers:
        paths = paths_from_field(answer.get("audio_path"))
        answer["audio_available"] = bool(paths and is_safe_upload_path(paths[0]))
        answer.pop("audio_path", None)
    return {
        "session": safe_admin_session_dict(session),
        "identity": {
            "full_name": public_candidate_name(session["candidate_name"]),
            "father_name": identity_protector.decrypt(session["father_name_encrypted"]) if "father_name_encrypted" in session.keys() else "",
            "cnic": format_cnic(identity_protector.decrypt(session["cnic_encrypted"])) if "cnic_encrypted" in session.keys() and session["cnic_encrypted"] else "",
            "resume_code": identity_protector.decrypt(session["resume_code_encrypted"]) if "resume_code_encrypted" in session.keys() and session["resume_code_encrypted"] else "",
            "identity_registered": bool(session["cnic_hash"]) if "cnic_hash" in session.keys() else False,
        },
        "answers": answers,
        "turns": get_turns(session_id),
        "transcript": build_transcript(session, answers),
        "summary": safe_json_loads(session["final_summary"], None),
        "quality": quality_metrics(),
    }


@app.delete("/api/admin/sessions/{session_id}")
def delete_candidate_session(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Delete one candidate record and its retained audio, freeing the CNIC for reuse."""
    session = session_or_404(session_id)
    readable_folder = candidate_folder_for_session(session_id, create=False)
    with db() as connection:
        audio_rows = connection.execute(
            "SELECT audio_path FROM answers WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        answer_rows = connection.execute(
            "SELECT id FROM answers WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        answer_ids = [int(row["id"]) for row in answer_rows]
        if answer_ids:
            placeholders = ",".join("?" for _ in answer_ids)
            connection.execute(
                f"DELETE FROM quality_reviews WHERE answer_id IN ({placeholders})",
                answer_ids,
            )
        connection.execute("DELETE FROM quality_reviews WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM answers WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    deleted_audio = 0
    for row in audio_rows:
        for stored in paths_from_field(row["audio_path"]):
            path = is_safe_upload_path(stored)
            if path:
                path.unlink(missing_ok=True)
                deleted_audio += 1
    if readable_folder and readable_folder.exists():
        shutil.rmtree(readable_folder, ignore_errors=True)
    return {
        "ok": True,
        "deleted_session_id": session_id,
        "candidate_name": public_candidate_name(session["candidate_name"]),
        "audio_files_deleted": deleted_audio,
        "cnic_reusable": True,
    }


@app.post("/api/admin/sessions/{session_id}/resume-code/reset")
def reset_candidate_resume_code(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Create a new recoverable resume code for an incomplete legacy/current record."""
    session = session_or_404(session_id)
    if str(session["status"]) == "completed":
        raise HTTPException(status_code=409, detail="A completed interview does not need a resume code. Delete the record only if a new attempt is allowed.")
    code = identity_protector.make_resume_code()
    with db() as connection:
        connection.execute(
            "UPDATE sessions SET resume_code_hash = ?, resume_code_encrypted = ? WHERE id = ?",
            (identity_protector.resume_code_hash(code), identity_protector.encrypt(code), session_id),
        )
    sync_candidate_plaintext(session_id)
    return {"ok": True, "resume_code": code}


@app.get("/api/admin/sessions/{session_id}/answers/{answer_id}/audio")
def play_saved_answer_audio(session_id: str, answer_id: int, _: str = Depends(require_admin)) -> Response:
    """Stream a retained candidate response only to the authenticated HR dashboard."""
    with db() as connection:
        answer = connection.execute(
            "SELECT audio_path, audio_mime_type FROM answers WHERE id = ? AND session_id = ?",
            (answer_id, session_id),
        ).fetchone()
    if not answer:
        raise HTTPException(status_code=404, detail="Answer recording was not found.")
    paths = paths_from_field(answer["audio_path"])
    path = is_safe_upload_path(paths[0]) if paths else None
    if not path:
        raise HTTPException(status_code=404, detail="This answer has no retained audio recording.")
    media_type = normalize_space(answer["audio_mime_type"]) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Content-Disposition": f'inline; filename="{path.name}"', "Cache-Control": "private, no-store"})


@app.post("/api/admin/sessions/{session_id}/review")
async def review_transcript(session_id: str, request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    session = session_or_404(session_id)
    payload = await request.json()
    reviews = payload.get("reviews", [])
    if not isinstance(reviews, list):
        raise HTTPException(status_code=422, detail="Reviews must be a list.")
    saved = 0
    with db() as connection:
        for item in reviews:
            answer_id = int(item.get("answer_id", 0) or 0)
            corrected = clean_text(item.get("corrected_text", ""))
            note = normalize_space(item.get("reviewer_note", ""))[:1000]
            if not answer_id or not corrected:
                continue
            answer = connection.execute("SELECT * FROM answers WHERE id = ? AND session_id = ?", (answer_id, session_id)).fetchone()
            if not answer:
                continue
            model_text = clean_text(answer["model_english"] or answer["answer_english"])
            wer = word_error_rate(corrected, model_text)
            connection.execute(
                """UPDATE answers SET answer_english = ?, reviewed_text = ?, candidate_edited = 1, quality_status = 'reviewed', word_error_rate = ? WHERE id = ?""",
                (corrected, corrected, wer, answer_id),
            )
            connection.execute(
                """INSERT INTO quality_reviews (answer_id, session_id, corrected_text, reviewer_note, word_error_rate, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(answer_id) DO UPDATE SET corrected_text = excluded.corrected_text, reviewer_note = excluded.reviewer_note,
                   word_error_rate = excluded.word_error_rate, created_at = excluded.created_at""",
                (answer_id, session_id, corrected, note, wer, utc_now()),
            )
            saved += 1
    learned = refresh_learned_hints() if session["quality_consent"] else load_learned_hints()
    sync_candidate_plaintext(session_id)
    return {"ok": True, "saved": saved, "training_eligible": bool(session["quality_consent"]), "learned_hint_count": len(learned), "metrics": quality_metrics()}


@app.get("/api/admin/sessions/{session_id}/export")
def export_session(session_id: str, format: str = "md", _: str = Depends(require_admin)) -> Response:
    session = session_or_404(session_id)
    answers = get_answers(session_id)
    turns = get_turns(session_id)
    summary = safe_json_loads(session["final_summary"], None)
    identity = {
        "full_name": public_candidate_name(session["candidate_name"]),
        "father_name": identity_protector.decrypt(session["father_name_encrypted"]) if "father_name_encrypted" in session.keys() else "",
        "cnic": format_cnic(identity_protector.decrypt(session["cnic_encrypted"])) if "cnic_encrypted" in session.keys() and session["cnic_encrypted"] else "",
        "resume_code": identity_protector.decrypt(session["resume_code_encrypted"]) if "resume_code_encrypted" in session.keys() and session["resume_code_encrypted"] else "",
    }
    if format == "json":
        return JSONResponse({"session": safe_admin_session_dict(session), "identity": identity, "answers": answers, "turns": turns, "english_transcript": build_transcript(session, answers), "summary": summary}, headers={"Content-Disposition": f'attachment; filename="meeting-{session_id}.json"'})
    if format != "md":
        raise HTTPException(status_code=422, detail="Export format must be md or json.")
    conversation = "\n".join(f"- {turn['speaker'].title()} ({turn['kind']}): {turn['text_en']}" for turn in turns)
    output = "# Adeeb AI Meeting Record\n\n"
    output += f"Candidate: {identity['full_name']}\nFather / guardian: {identity['father_name']}\nCNIC: {identity['cnic']}\nResume code: {identity['resume_code'] or 'Unavailable for legacy record'}\n\n"
    output += build_transcript(session, answers)
    output += "\n\n# Meeting Conversation\n\n" + (conversation or "No additional meeting turns recorded.")
    output += "\n\n# English Summary\n\n" + json.dumps(summary or {"note": "Summary is still generating or unavailable."}, ensure_ascii=False, indent=2)
    return PlainTextResponse(output, headers={"Content-Disposition": f'attachment; filename="meeting-{session_id}.md"'})


# ---------- Universal candidate registration ----------
@app.post("/api/join/register")
async def register_universal_candidate(request: Request) -> dict[str, Any]:
    """Create or securely resume a candidate meeting from the shared /join URL."""
    payload = await request.json()
    try:
        record = identity_protector.build_record(
            str(payload.get("candidate_name", "")),
            str(payload.get("father_name", "")),
            str(payload.get("cnic", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    quality_consent = bool(payload.get("quality_consent", False))
    language = str(payload.get("preferred_language", "auto")).casefold().strip()
    if language not in ALLOWED_INPUT_LANGUAGES:
        language = "auto"
    resume_code = normalize_space(payload.get("resume_code", "")).upper()

    with db() as connection:
        existing = connection.execute("SELECT * FROM sessions WHERE cnic_hash = ?", (record.cnic_hash,)).fetchone()
        if existing:
            if str(existing["status"]) == "completed":
                raise HTTPException(status_code=409, detail="An interview record already exists for these details. Please contact HR for assistance.")
            if not resume_code:
                raise HTTPException(status_code=409, detail="A saved interview record exists. Enter the resume code you received when you first joined.")
            matches_code = hmac.compare_digest(str(existing["resume_code_hash"] or ""), identity_protector.resume_code_hash(resume_code))
            matches_name = hmac.compare_digest(public_candidate_name(existing["candidate_name"]).casefold(), record.full_name.casefold())
            matches_father = identity_protector.verify_father_name(existing["father_name_encrypted"], record.father_name)
            if not (matches_code and matches_name and matches_father):
                raise HTTPException(status_code=403, detail="The identity details or resume code could not be verified. Please contact HR for assistance.")
            connection.execute(
                "UPDATE sessions SET quality_consent = CASE WHEN quality_consent = 1 THEN 1 ELSE ? END, candidate_language = COALESCE(NULLIF(?, ''), candidate_language) WHERE id = ?",
                (1 if quality_consent else 0, language, existing["id"]),
            )
            connection.commit()
            sync_candidate_plaintext(str(existing["id"]))
            return {
                "ok": True,
                "resumed": True,
                "session_url": f"/interview/{existing['id']}",
                "resume_code": None,
            }

        snapshot = load_questions()
        session_id = secrets.token_urlsafe(24)
        new_resume_code = identity_protector.make_resume_code()
        try:
            connection.execute(
                """INSERT INTO sessions (
                    id, candidate_name, role_name, status, created_at, question_snapshot,
                    quality_consent, candidate_identified_at, father_name_encrypted,
                    cnic_encrypted, cnic_hash, resume_code_hash, resume_code_encrypted, identity_verified_at, candidate_language
                ) VALUES (?, ?, '', 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, record.full_name, utc_now(), json.dumps(snapshot, ensure_ascii=False),
                    1 if quality_consent else 0, utc_now(), record.father_name_encrypted,
                    record.cnic_encrypted, record.cnic_hash, identity_protector.resume_code_hash(new_resume_code),
                    identity_protector.encrypt(new_resume_code), utc_now(), language,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="An interview record already exists for these details. Please contact HR for assistance.") from exc
    sync_candidate_plaintext(session_id)
    return {
        "ok": True,
        "resumed": False,
        "session_url": f"/interview/{session_id}",
        "resume_code": new_resume_code,
    }


# ---------- Candidate APIs ----------
@app.get("/api/interview/{session_id}")
def get_interview_state(session_id: str) -> dict[str, Any]:
    return state_payload(session_id)


@app.post("/api/interview/{session_id}/identify")
async def identify_candidate(session_id: str, request: Request) -> dict[str, Any]:
    session = session_or_404(session_id)
    payload = await request.json()
    name = clean_text(payload.get("candidate_name", ""))
    quality_consent = bool(payload.get("quality_consent", False))
    if len(name) < 2 or len(name) > 120:
        raise HTTPException(status_code=422, detail="Please enter your full name before joining the meeting.")
    existing = clean_text(session["candidate_name"])
    identity_registered = bool(session["cnic_hash"]) if "cnic_hash" in session.keys() else False
    if identity_registered and existing and existing.casefold() != name.casefold():
        raise HTTPException(status_code=409, detail="Your identity was already registered through the secure candidate form and cannot be changed here.")
    if existing and existing.casefold() != name.casefold() and session["status"] in {"in_progress", "completed"}:
        raise HTTPException(status_code=409, detail="This private link is already assigned to a candidate. Ask HR for help.")
    with db() as connection:
        connection.execute(
            "UPDATE sessions SET candidate_name = ?, quality_consent = CASE WHEN quality_consent = 1 THEN 1 ELSE ? END, candidate_identified_at = COALESCE(candidate_identified_at, ?) WHERE id = ?",
            (existing if identity_registered else name, 1 if quality_consent else 0, utc_now(), session_id),
        )
    sync_candidate_plaintext(session_id)
    return state_payload(session_id)


@app.post("/api/interview/{session_id}/start")
def start_interview(session_id: str) -> dict[str, Any]:
    session = session_or_404(session_id)
    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="This interview has already finished.")
    if not normalize_space(session["candidate_name"]):
        raise HTTPException(status_code=422, detail="Please enter your name before starting the meeting.")
    if session["status"] == "created":
        with db() as connection:
            connection.execute("UPDATE sessions SET status = 'in_progress', started_at = ? WHERE id = ?", (utc_now(), session_id))
    sync_candidate_plaintext(session_id)
    return {"ok": True}


async def write_upload(upload: UploadFile, session_id: str) -> tuple[Path, int, str]:
    """Store candidate audio immediately and return path, byte count, and MIME type.

    The browser provides the extension based on the MediaRecorder MIME type. The server
    still validates the extension and size before committing the upload.
    """
    suffix = Path(upload.filename or "turn.webm").suffix.lower() or ".webm"
    allowed_suffixes = {".webm", ".m4a", ".mp4", ".ogg", ".wav"}
    suffix = suffix if suffix in allowed_suffixes else ".webm"
    content_type = normalize_space(upload.content_type)
    candidate_folder = candidate_folder_for_session(session_id)
    audio_root = (candidate_folder / "audio") if candidate_folder is not None else UPLOAD_DIR
    audio_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = audio_root / f"answer-{timestamp}-{secrets.token_hex(6)}{suffix}"
    size = 0
    try:
        with target.open("wb") as file:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="Audio turn is larger than the configured limit.")
                file.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if size < 650:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="The audio turn was too short. Please speak again.")
    mime = content_type or mimetypes.guess_type(target.name)[0] or "audio/webm"
    return target, size, mime



def create_queued_answer(
    session: sqlite3.Row,
    index: int,
    base_question: dict[str, Any],
    file_path: Path,
    spoken_language: str,
    audio_bytes: int,
    audio_mime_type: str,
    audio_duration_ms: int | None,
) -> int:
    """Persist a placeholder immediately so the next question can begin before Whisper runs."""
    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM answers WHERE session_id = ? AND question_id = ?",
            (session["id"], base_question["id"]),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="This response is already being saved. Please wait for the next question.")
        cursor = connection.execute(
            """INSERT INTO answers (
                session_id, question_id, question_index, question_category, question_text,
                answer_original, answer_english, model_english, detected_language, created_at,
                candidate_edited, processing_ms, transcription_status, audio_path, spoken_language, audio_mime_type, audio_bytes, audio_duration_ms,
                processing_error, transcript_ready_at, is_final, model_version, quality_status, final_pass_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 'queued', ?, ?, ?, ?, ?, NULL, NULL, 1, ?, 'unreviewed', 'not_requested')""",
            (
                session["id"],
                base_question["id"],
                index,
                str(base_question.get("category", "General")),
                str(base_question.get("text", "")),
                "[English transcript is processing in the background.]",
                "[English transcript is processing in the background.]",
                "",
                "Pending",
                utc_now(),
                json.dumps([portable_storage_path(file_path)]),
                spoken_language,
                audio_mime_type,
                audio_bytes,
                audio_duration_ms,
                WHISPER_LIVE_MODEL,
            ),
        )
        connection.execute("UPDATE sessions SET active_prompt = '', follow_up_count = 0 WHERE id = ?", (session["id"],))
        return int(cursor.lastrowid)


async def process_queued_answer(
    session_id: str,
    answer_id: int,
    file_path_value: str,
    snapshot: dict[str, Any],
    question: dict[str, Any],
    spoken_language: str,
) -> None:
    """Transcribe one already-saved answer without blocking the meeting screen."""
    file_path = resolve_stored_path(file_path_value)
    keep_audio = True
    try:
        with db() as connection:
            connection.execute(
                "UPDATE answers SET transcription_status = 'processing', processing_error = NULL WHERE id = ? AND session_id = ?",
                (answer_id, session_id),
            )
        session = session_or_404(session_id)
        keep_audio = candidate_audio_should_be_kept(session)
        async with _background_transcription_semaphore:
            transcript = await transcribe_turn(str(file_path), snapshot, question, spoken_language)
        saved_audio = json.dumps([portable_storage_path(file_path)]) if keep_audio else None
        with db() as connection:
            row = connection.execute("SELECT reviewed_text FROM answers WHERE id = ? AND session_id = ?", (answer_id, session_id)).fetchone()
            if row and row["reviewed_text"]:
                connection.execute(
                    """UPDATE answers SET answer_original = ?, model_english = ?, detected_language = ?, processing_ms = ?,
                       transcription_status = 'ready', audio_path = ?, spoken_language = ?, processing_error = NULL,
                       transcript_ready_at = ?, model_version = ? WHERE id = ? AND session_id = ?""",
                    (
                        transcript["original"], transcript["english"], transcript["language"], transcript["processing_ms"],
                        saved_audio, spoken_language, utc_now(), transcript["model_version"], answer_id, session_id,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE answers SET answer_original = ?, answer_english = ?, model_english = ?, detected_language = ?,
                       processing_ms = ?, transcription_status = 'ready', audio_path = ?, spoken_language = ?, processing_error = NULL,
                       transcript_ready_at = ?, model_version = ? WHERE id = ? AND session_id = ?""",
                    (
                        transcript["original"], transcript["english"], transcript["english"], transcript["language"],
                        transcript["processing_ms"], saved_audio, spoken_language, utc_now(), transcript["model_version"],
                        answer_id, session_id,
                    ),
                )
        if str(question.get("id")) == "applying_role":
            with db() as connection:
                connection.execute(
                    "UPDATE sessions SET role_name = ? WHERE id = ?",
                    (clean_text(transcript["english"])[:180], session_id),
                )
        append_turn(session_id, question["id"], "candidate", "answer_ready", transcript["english"], transcript["processing_ms"])
    except Exception as error:
        logger.exception("Background transcription failed for queued answer %s", answer_id)
        message = clean_text(str(getattr(error, "detail", error))) or "Transcript could not be prepared."
        with db() as connection:
            connection.execute(
                """UPDATE answers SET answer_original = ?, answer_english = ?, model_english = ?, transcription_status = 'failed',
                   processing_error = ?, transcript_ready_at = ? WHERE id = ? AND session_id = ?""",
                (
                    "[Background transcription failed.]",
                    "[Background transcription failed. HR should review the retained audio when available.]",
                    "[Background transcription failed.]",
                    message[:1000], utc_now(), answer_id, session_id,
                ),
            )
        append_turn(session_id, question["id"], "system", "transcription_failed", "Background transcript failed; review is required.")
    finally:
        if not keep_audio:
            file_path.unlink(missing_ok=True)


async def wait_for_pending_transcriptions(session_id: str) -> None:
    """Do not generate the HR summary while queued answers still contain placeholders."""
    started = time.monotonic()
    while pending_transcription_count(session_id) > 0:
        if time.monotonic() - started >= QUEUE_MAX_WAIT_SECONDS:
            logger.warning("Timed out waiting for queued transcripts for session %s", session_id)
            return
        await asyncio.sleep(QUEUE_WAIT_POLL_SECONDS)


async def wait_for_answer_transcription(answer_id: int, timeout_seconds: int | None = None) -> dict[str, Any] | None:
    """Wait only at the two staged decision points, never after every answer."""
    timeout = float(timeout_seconds or STAGED_TRANSCRIPT_WAIT_SECONDS)
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        with db() as connection:
            row = connection.execute("SELECT * FROM answers WHERE id = ?", (answer_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("transcription_status") in {"ready", "failed", "skipped"}:
            return item
        await asyncio.sleep(QUEUE_WAIT_POLL_SECONDS)
    logger.warning("Timed out waiting for staged transcript answer_id=%s", answer_id)
    return None


async def resume_queued_transcriptions() -> None:
    """Resume interrupted queued work after a server restart whenever the temporary audio still exists."""
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM answers WHERE transcription_status IN ('queued', 'processing') ORDER BY created_at ASC"
        ).fetchall()
    for row in rows:
        paths = paths_from_field(row["audio_path"])
        path = resolve_stored_path(paths[0]) if paths else None
        if not path or not path.exists():
            with db() as connection:
                connection.execute(
                    "UPDATE answers SET transcription_status = 'failed', processing_error = 'Queued audio was unavailable after restart.', transcript_ready_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
            continue
        session = session_or_404(row["session_id"])
        snapshot = safe_json_loads(session["question_snapshot"], {})
        question = {
            "id": row["question_id"],
            "text": row["question_text"],
            "category": row["question_category"] or "General",
        }
        start_background_task(
            process_queued_answer(
                row["session_id"], int(row["id"]), str(path), snapshot, question,
                str(row["spoken_language"] or "auto"),
            )
        )


async def advance_or_complete(session_id: str, session: sqlite3.Row, snapshot: dict[str, Any], base_question: dict[str, Any], index: int, candidate_text: str, transcript: dict[str, Any]) -> dict[str, Any]:
    latest = session_or_404(session_id)
    _, next_question = get_next_question(latest)
    if next_question is None:
        completed, bot_message_en = completion_response(session_id, snapshot)
        append_turn(session_id, base_question["id"], "agent", "closing", bot_message_en)
        bot_message = await agent_spoken_text(session_id, bot_message_en)
        return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "completed", "completed": completed, "state": state_payload(session_id)}

    bot_message_en = await generate_llm_planned_question(session_id, dict(next_question), candidate_text)
    with db() as connection:
        connection.execute(
            "UPDATE sessions SET active_prompt = ?, follow_up_count = 0 WHERE id = ?",
            (f"{LLM_QUESTION_PREFIX}{bot_message_en}", session_id),
        )
    append_turn(session_id, next_question["id"], "agent", "llm_question", bot_message_en)
    bot_message = await agent_spoken_text(session_id, bot_message_en)
    return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "next", "completed": False, "state": state_payload(session_id)}


@app.post("/api/interview/{session_id}/queue-answer")
async def queue_answer_in_background(
    session_id: str,
    spoken_language: str = Form("en"),
    audio_duration_ms: int | None = Form(None),
    audio: UploadFile = File(...),
) -> dict[str, Any]:
    """Save the first four answers immediately and process them in a controlled queue.

    Q1 and Q2 move on immediately. After Q3, Adeeb waits for the queued transcript and
    asks a skill/problem-solving follow-up as Q4. After Q4, it waits for the staged
    transcripts and asks the final tailored project question. The project answer and its
    two follow-ups use the synchronous /turn endpoint.
    """
    if not (FAST_ANSWER_QUEUE and STAGED_INTERVIEW_FLOW):
        raise HTTPException(status_code=409, detail="Staged background transcription is disabled in this installation.")
    session = session_or_404(session_id)
    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="This interview has already finished.")
    if not normalize_space(session["candidate_name"]):
        raise HTTPException(status_code=422, detail="Please enter your name before speaking.")
    index, base_question = get_next_question(session)
    snapshot = safe_json_loads(session["question_snapshot"], {})
    if base_question is None:
        completed, closing = completion_response(session_id, snapshot)
        return {"accepted": True, "bot_message": closing, "action": "completed", "completed": completed, "state": state_payload(session_id)}
    if str(base_question.get("transcription_mode", "immediate")).lower() != "background":
        raise HTTPException(status_code=409, detail="This project question must be transcribed immediately.")

    language = str(spoken_language or "auto").casefold().strip()
    if language not in ALLOWED_INPUT_LANGUAGES:
        language = "auto"
    prompt_question = active_prompt(session, base_question) or dict(base_question)
    file_path, audio_bytes, audio_mime_type = await write_upload(audio, session_id)
    try:
        answer_id = create_queued_answer(
            session, index, prompt_question, file_path, language, audio_bytes, audio_mime_type, audio_duration_ms
        )
    except Exception:
        file_path.unlink(missing_ok=True)
        raise

    start_background_task(
        process_queued_answer(
            session_id, answer_id, str(file_path), snapshot, dict(prompt_question), language,
        )
    )

    latest = session_or_404(session_id)
    _, next_question = get_next_question(latest)
    if next_question is None:
        completed, bot_message_en = completion_response(session_id, snapshot)
        append_turn(session_id, base_question["id"], "agent", "closing", bot_message_en)
        bot_message = await agent_spoken_text(session_id, bot_message_en)
        return {
            "accepted": True,
            "transcription_status": "queued",
            "bot_message": bot_message,
            "action": "completed",
            "completed": completed,
            "state": state_payload(session_id),
        }

    current_id = str(base_question.get("id", ""))
    staged_row: dict[str, Any] | None = None
    bot_message_en = str(next_question.get("text", ""))
    turn_kind = "question"

    # The fourth prompt depends on what the candidate actually said about skills. Waiting
    # here still saves time because questions one and two were processed while the meeting continued.
    if current_id == "skills_and_work":
        staged_row = await wait_for_answer_transcription(answer_id)
        latest_answer = clean_text((staged_row or {}).get("answer_english", ""))
        bot_message_en = await generate_llm_planned_question(session_id, dict(next_question), latest_answer)
        turn_kind = "skill_problem_followup"
    # The final project prompt is generated only after the fourth answer is available.
    elif current_id == "problem_solving":
        staged_row = await wait_for_answer_transcription(answer_id)
        latest_answer = clean_text((staged_row or {}).get("answer_english", ""))
        bot_message_en = await generate_llm_planned_question(session_id, dict(next_question), latest_answer)
        turn_kind = "project_question"

    with db() as connection:
        connection.execute(
            "UPDATE sessions SET active_prompt = ?, follow_up_count = 0 WHERE id = ?",
            (f"{LLM_QUESTION_PREFIX}{bot_message_en}" if turn_kind != "question" else "", session_id),
        )
    append_turn(session_id, next_question["id"], "agent", turn_kind, bot_message_en)
    bot_message = await agent_spoken_text(session_id, bot_message_en)
    return {
        "accepted": True,
        "candidate_text": "",
        "transcription_status": (staged_row or {}).get("transcription_status", "queued"),
        "bot_message": bot_message,
        "action": "next",
        "completed": False,
        "state": state_payload(session_id),
    }




@app.post("/api/interview/{session_id}/language")
async def set_interview_language(session_id: str, request: Request) -> dict[str, Any]:
    """Lock Adeeb's spoken language until the candidate explicitly changes it again."""
    session = session_or_404(session_id)
    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="This interview has already finished.")
    payload = await request.json()
    language = str(payload.get("language", "")).casefold().strip()
    if language not in {"en", "ur", "hi"}:
        raise HTTPException(status_code=422, detail="Choose English, Urdu, or Hindi.")
    with db() as connection:
        connection.execute("UPDATE sessions SET candidate_language = ? WHERE id = ?", (language, session_id))
    if language == "ur" and WHISPER_URDU_MODEL and not should_use_groq_stt("ur"):
        # Warm the local fallback only when Urdu is configured to stay offline. When
        # Groq speech recognition is active, loading a large local model in parallel can
        # compete for CPU/RAM and delay the live LLM response.
        start_background_task(get_whisper_model(WHISPER_URDU_MODEL))
    updated = session_or_404(session_id)
    _, base_question = get_next_question(updated)
    prompt = active_prompt(updated, base_question) if base_question else None
    key = {"en": "language_set_en", "ur": "language_set_ur", "hi": "language_set_hi"}[language]
    acknowledgement = local_agent_message(key, language)
    question_en = str((prompt or {}).get("text", ""))
    question = await translate_agent_text(question_en, language)
    bot_message = clean_text(f"{acknowledgement} {question}")
    append_turn(session_id, str((base_question or {}).get("id") or ""), "agent", "language_preference", bot_message)
    return {
        "ok": True,
        "language": language,
        "bot_message": bot_message,
        "question": question,
        "state": state_payload(session_id),
    }


@app.get("/api/interview/{session_id}/spoken-current")
async def spoken_current_prompt(session_id: str) -> dict[str, Any]:
    session = session_or_404(session_id)
    snapshot = safe_json_loads(session["question_snapshot"], {})
    _, base_question = get_next_question(session)
    prompt = active_prompt(session, base_question)
    language = preferred_agent_language(session)
    welcome = str(snapshot.get("welcome_message", "")).replace("{candidate_name}", clean_text(session["candidate_name"]) or "Candidate")
    question_text = str((prompt or {}).get("text", ""))
    return {
        "language": language,
        "welcome": await translate_agent_text(welcome, language),
        "question": await translate_agent_text(question_text, language),
    }

@app.post("/api/voice/fallback-text")
async def voice_fallback_text(request: Request) -> dict[str, str]:
    """Keep browser fallback speech in the selected native script.

    Earlier builds converted Urdu to Roman Urdu when a device lacked a native voice.
    That made the closing prompt sound unnatural and changed visible wording. v15.5
    never converts Adeeb's response to Roman text; Edge neural TTS remains the primary
    mobile voice and the browser receives the original Urdu script as the final fallback.
    """
    payload = await request.json()
    text = clean_text(payload.get("text", ""))[:900]
    language = str(payload.get("language", "en")).casefold().strip()
    if language not in {"en", "ur", "hi"}:
        language = "en"
    return {"text": text, "language": language}


async def edge_tts_audio(text: str, language: str) -> bytes:
    """Generate MP3 audio through the free Edge online voice service.

    It is intentionally imported lazily so the core app can still start and show a
    clear diagnostic if the optional package is missing.
    """
    try:
        import edge_tts  # type: ignore
    except ImportError as exc:
        raise RuntimeError("edge-tts is not installed") from exc
    voice = {
        "ur": EDGE_TTS_VOICE_UR,
        "hi": EDGE_TTS_VOICE_HI,
        "en": EDGE_TTS_VOICE_EN,
    }.get(language, EDGE_TTS_VOICE_EN)
    communicate = edge_tts.Communicate(text, voice, rate=EDGE_TTS_RATE, volume=EDGE_TTS_VOLUME)
    audio_parts: list[bytes] = []

    async def collect_audio() -> None:
        async for message in communicate.stream():
            if message.get("type") == "audio" and message.get("data"):
                audio_parts.append(message["data"])

    # asyncio.wait_for keeps this compatible with Python 3.10 as well as 3.11/3.12.
    await asyncio.wait_for(collect_audio(), timeout=TTS_TIMEOUT_SECONDS)
    data = b"".join(audio_parts)
    if len(data) < 256:
        raise RuntimeError("Edge TTS returned no usable audio")
    return data


async def elevenlabs_tts_audio(text: str) -> tuple[bytes, str]:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ElevenLabs is not configured")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": ELEVENLABS_VOICE_STABILITY,
            "similarity_boost": ELEVENLABS_VOICE_SIMILARITY,
        },
    }
    async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
            headers={"xi-api-key": ELEVENLABS_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"},
            json=body,
        )
        response.raise_for_status()
    return response.content, response.headers.get("content-type", "audio/mpeg")


@app.post("/api/voice/tts")
async def voice_tts(request: Request) -> Response:
    """Return real audio for desktop and mobile, preferring configured cloud voice.

    In auto mode Urdu/Hindi use their dedicated Edge neural voices first. English may
    use configured ElevenLabs first. Browser speech remains the final client fallback.
    """
    payload = await request.json()
    text = clean_text(payload.get("text", ""))
    language = str(payload.get("language", "en")).casefold().strip()
    if language not in {"en", "ur", "hi"}:
        language = "en"
    if not text or len(text) > 1200:
        raise HTTPException(status_code=422, detail="TTS text must be between 1 and 1200 characters.")

    providers: list[str]
    if TTS_PROVIDER in {"elevenlabs", "edge", "browser"}:
        providers = [TTS_PROVIDER]
    else:
        if language in {"ur", "hi"}:
            providers = (["edge"] if EDGE_TTS_ENABLED else []) + (["elevenlabs"] if ELEVENLABS_API_KEY else [])
        else:
            providers = (["elevenlabs"] if ELEVENLABS_API_KEY else []) + (["edge"] if EDGE_TTS_ENABLED else [])
    errors: list[str] = []
    for provider in providers:
        try:
            if provider == "elevenlabs":
                audio, media_type = await elevenlabs_tts_audio(text)
            elif provider == "edge":
                audio, media_type = await edge_tts_audio(text, language), "audio/mpeg"
            else:
                break
            return Response(
                audio,
                media_type=media_type,
                headers={
                    "Cache-Control": "private, max-age=300",
                    "X-Adeeb-TTS-Provider": provider,
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception as exc:
            logger.info("%s TTS unavailable: %s", provider, type(exc).__name__)
            errors.append(f"{provider}:{type(exc).__name__}")
    raise HTTPException(
        status_code=503,
        detail="Server voice is unavailable; the browser voice fallback will be used. " + ", ".join(errors[:3]),
    )


@app.post("/api/interview/{session_id}/ask")
async def ask_adeeb(session_id: str, request: Request) -> dict[str, Any]:
    """Explicit agent-question path. It is deliberately separate from normal answer queueing."""
    session = session_or_404(session_id)
    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="This interview has already finished.")
    payload = await request.json()
    candidate_question = clean_text(payload.get("question", ""))
    if len(candidate_question) < 3:
        raise HTTPException(status_code=422, detail="Please type a complete question for Adeeb.")
    _, base_question = get_next_question(session)
    if base_question is None:
        raise HTTPException(status_code=409, detail="There is no active interview question.")
    prompt = active_prompt(session, base_question) or dict(base_question)
    append_turn(session_id, base_question["id"], "candidate", "typed_question", candidate_question)
    answer = await answer_candidate_question(session_id, candidate_question, prompt)
    append_turn(session_id, base_question["id"], "agent", "clarification", answer)
    return {"answer": answer, "state": state_payload(session_id)}


@app.post("/api/interview/{session_id}/turn")
async def submit_voice_turn(
    session_id: str,
    spoken_language: str = Form("en"),
    audio_duration_ms: int | None = Form(None),
    audio: UploadFile = File(...),
) -> dict[str, Any]:
    """Save audio, transcribe it immediately, let the LLM understand it, then respond.

    This is the primary professional interview path. No interview answer is advanced
    before the live transcript and agent decision are available.
    """
    session = session_or_404(session_id)
    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="This interview has already finished.")
    if not normalize_space(session["candidate_name"]):
        raise HTTPException(status_code=422, detail="Please enter your name before speaking.")
    index, base_question = get_next_question(session)
    snapshot = safe_json_loads(session["question_snapshot"], {})
    if base_question is None:
        completed, closing = completion_response(session_id, snapshot)
        return {"candidate_text": "", "bot_message": closing, "action": "completed", "completed": completed, "state": state_payload(session_id)}
    question = active_prompt(session, base_question) or dict(base_question)
    language = str(spoken_language or "auto").casefold().strip()
    if language not in ALLOWED_INPUT_LANGUAGES:
        language = "auto"
    # The browser language selector is also a hard meeting-language lock. Keeping it
    # in the session prevents a stale auto/English value from letting Roman Urdu pass
    # through at the end of an otherwise Urdu interview.
    if language in {"en", "ur", "hi"} and str(session["candidate_language"] or "auto") != language:
        with db() as connection:
            connection.execute("UPDATE sessions SET candidate_language = ? WHERE id = ?", (language, session_id))
        session = session_or_404(session_id)
    file_path, audio_bytes, audio_mime_type = await write_upload(audio, session_id)
    keep_audio = candidate_audio_should_be_kept(session)
    audio_persisted = False
    try:
        # Short turns may be language-switch commands, so auto-detect those. Longer
        # answers use the candidate's locked language for much stronger Urdu/Hindi ASR.
        explicit_language = language if language in {"en", "ur", "hi"} else str(session["candidate_language"] or "auto")
        # Once Urdu/Hindi is selected, even a two-second answer must stay on that
        # recognizer. The previous short-turn auto-detection was the main reason simple
        # Urdu replies were misheard as Hindi or English. English/auto mode keeps the
        # lightweight command-detection behavior.
        if explicit_language in {"ur", "hi"}:
            turn_language = explicit_language
        else:
            turn_language = "auto" if (audio_duration_ms or 0) <= 12000 else explicit_language
        if turn_language not in ALLOWED_INPUT_LANGUAGES:
            turn_language = "auto"
        transcript = await transcribe_turn(str(file_path), snapshot, question, turn_language)
        candidate_text = transcript["english"]
        candidate_original = transcript.get("original", candidate_text)
        append_turn(session_id, base_question["id"], "candidate", "speech", candidate_text, transcript["processing_ms"])
        intent = await classify_candidate_turn(candidate_text, candidate_original, question, audio_duration_ms)
        if intent in {"language_en", "language_ur", "language_hi"}:
            language_code = {"language_en": "en", "language_ur": "ur", "language_hi": "hi"}[intent]
            with db() as connection:
                connection.execute("UPDATE sessions SET candidate_language = ? WHERE id = ?", (language_code, session_id))
            if language_code == "ur" and WHISPER_URDU_MODEL and not should_use_groq_stt("ur"):
                start_background_task(get_whisper_model(WHISPER_URDU_MODEL))
            base_key = {"en": "language_set_en", "ur": "language_set_ur", "hi": "language_set_hi"}[language_code]
            question_text = question.get('text') or base_question['text']
            bot_message = f"{local_agent_message(base_key, language_code)} {await translate_agent_text(str(question_text), language_code)}"
            append_turn(session_id, base_question["id"], "agent", "language_preference", bot_message)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "question": await translate_agent_text(str(question_text), language_code), "action": "language", "completed": False, "state": state_payload(session_id)}
        if intent == "repeat":
            bot_message_en = str(question.get("text") or base_question["text"])
            bot_message = await agent_spoken_text(session_id, bot_message_en)
            append_turn(session_id, base_question["id"], "agent", "repeat", bot_message_en)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "repeat", "completed": False, "state": state_payload(session_id)}
        if intent == "field_question":
            bot_message_en = await generate_role_specific_question(session_id, candidate_original or candidate_text, question)
            append_turn(session_id, base_question["id"], "agent", "role_specific_question", bot_message_en)
            bot_message = await agent_spoken_text(session_id, bot_message_en)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "role_specific_question", "completed": False, "state": state_payload(session_id)}
        if intent == "clarification":
            bot_message = await answer_candidate_question(session_id, candidate_text, question)
            append_turn(session_id, base_question["id"], "agent", "clarification", bot_message)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "clarification", "completed": False, "state": state_payload(session_id)}
        if intent == "finish":
            bot_message = local_agent_message("finish_instruction", preferred_agent_language(session_or_404(session_id)))
            append_turn(session_id, base_question["id"], "agent", "instruction", bot_message)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": bot_message, "action": "instruction", "completed": False, "state": state_payload(session_id)}
        if intent == "next":
            finalize_current_question(session_id, index, base_question, skipped=True)
            return await advance_or_complete(session_id, session, snapshot, base_question, index, candidate_text, transcript)
        weak = weak_answer(candidate_text, base_question)
        # The final project section has its own two-stage LLM follow-up plan. A short but
        # understandable project answer is accepted and explored there instead of creating
        # an extra uncontrolled clarification loop.
        if weak and str(base_question.get("id")) != "role_specific":
            append_turn(session_id, base_question["id"], "agent", "clarify_answer", weak)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": await agent_spoken_text(session_id, weak), "action": "clarify_answer", "completed": False, "state": state_payload(session_id)}
        persisted_path = file_path if keep_audio else None
        answer_question = dict(base_question)
        if question.get("prompt_type") == "follow_up":
            answer_question["text"] = str(question.get("text") or base_question.get("text") or "")
        combined = upsert_candidate_answer(session, index, answer_question, transcript, persisted_path)
        audio_persisted = bool(persisted_path)
        current = session_or_404(session_id)

        # The final project question is transcribed immediately and followed by exactly
        # two LLM questions: project depth, then skill evidence/result. The combined answer
        # remains under the single project question in the HR transcript.
        if str(base_question.get("id")) == "role_specific":
            asked_count = int(current["follow_up_count"] or 0)
            permitted = min(2, max(0, int(base_question.get("max_followups", 2))))
            if question.get("prompt_type") == "follow_up" and asked_count >= permitted:
                finalize_current_question(session_id, index, base_question)
                return await advance_or_complete(session_id, current, snapshot, base_question, index, candidate_text, transcript)
            next_stage = asked_count + 1
            if next_stage <= permitted:
                project_followup = await generate_project_followup(session_id, combined, question, next_stage)
                with db() as connection:
                    connection.execute(
                        "UPDATE sessions SET active_prompt = ?, follow_up_count = ? WHERE id = ?",
                        (project_followup, next_stage, session_id),
                    )
                kind = "project_depth_follow_up" if next_stage == 1 else "skill_evidence_follow_up"
                append_turn(session_id, base_question["id"], "agent", kind, project_followup)
                return {
                    "candidate_text": candidate_text,
                    "detected_language": transcript["language"],
                    "processing_ms": transcript["processing_ms"],
                    "bot_message": await agent_spoken_text(session_id, project_followup),
                    "action": kind,
                    "completed": False,
                    "state": state_payload(session_id),
                }
            finalize_current_question(session_id, index, base_question)
            return await advance_or_complete(session_id, current, snapshot, base_question, index, candidate_text, transcript)

        # Non-project follow-up prompts remain single-turn safeguards. In the staged JSON
        # the first four questions have max_followups=0, so this path is used only by a
        # custom configuration.
        if question.get("prompt_type") == "follow_up":
            finalize_current_question(session_id, index, base_question)
            return await advance_or_complete(session_id, current, snapshot, base_question, index, candidate_text, transcript)

        skill_followup = None if STAGED_INTERVIEW_FLOW else await maybe_generate_skill_followup(session_id, candidate_text, base_question)
        if skill_followup:
            with db() as connection:
                connection.execute("UPDATE sessions SET active_prompt = ?, follow_up_count = follow_up_count + 1 WHERE id = ?", (skill_followup, session_id))
            append_turn(session_id, base_question["id"], "agent", "skill_follow_up", skill_followup)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": await agent_spoken_text(session_id, skill_followup), "action": "skill_follow_up", "completed": False, "state": state_payload(session_id)}
        if str(base_question.get("id")) == "applying_role":
            # Preserve the reliable V13 order. Store the role, then ask the planned
            # introduction question. Earlier LLM-driven builds injected a project
            # question here and accidentally displaced the candidate introduction.
            with db() as connection:
                connection.execute(
                    "UPDATE sessions SET role_name = ? WHERE id = ?",
                    (clean_text(candidate_text)[:180], session_id),
                )
            finalize_current_question(session_id, index, base_question)
            return await advance_or_complete(session_id, session_or_404(session_id), snapshot, base_question, index, candidate_text, transcript)
        follow_up = await decide_follow_up(session_id, snapshot, question, combined, int(current["follow_up_count"] or 0))
        if follow_up:
            with db() as connection:
                connection.execute("UPDATE sessions SET active_prompt = ?, follow_up_count = follow_up_count + 1 WHERE id = ?", (follow_up, session_id))
            append_turn(session_id, base_question["id"], "agent", "follow_up", follow_up)
            return {"candidate_text": candidate_text, "detected_language": transcript["language"], "processing_ms": transcript["processing_ms"], "bot_message": await agent_spoken_text(session_id, follow_up), "action": "follow_up", "completed": False, "state": state_payload(session_id)}
        finalize_current_question(session_id, index, base_question)
        return await advance_or_complete(session_id, session, snapshot, base_question, index, candidate_text, transcript)
    finally:
        # Control commands and rejected/weak turns are not answer recordings. Remove
        # their temporary files even when retained answer audio is enabled.
        if not audio_persisted:
            file_path.unlink(missing_ok=True)


@app.post("/api/interview/{session_id}/skip")
async def skip_current_question(session_id: str) -> dict[str, Any]:
    session = session_or_404(session_id)
    index, question = get_next_question(session)
    if question is None:
        raise HTTPException(status_code=409, detail="There are no remaining questions.")
    snapshot = safe_json_loads(session["question_snapshot"], {})
    finalize_current_question(session_id, index, question, skipped=True)
    append_turn(session_id, question["id"], "candidate", "skip", "[Candidate skipped this question.]")
    return await advance_or_complete(
        session_id,
        session,
        snapshot,
        question,
        index,
        "",
        {"language": "Not applicable", "processing_ms": 0},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
