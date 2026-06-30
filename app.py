"""
QuizEngine Backend
──────────────────
Flask server that accepts PDF / TXT file uploads,
extracts Q&A text, parses it into structured questions,
and stores quizzes for retrieval by ID.

Public routes (no auth required)
  GET  /api/health              Health check
  GET  /api/quiz/<id>           Fetch a quiz by ID (used by embedded widget)
  GET  /preview/<id>            Serve preview page

Auth routes
  POST /api/auth/signup         Register a new school + admin account
  POST /api/auth/login          Exchange email+password for JWT (via Supabase)

Protected routes (JWT required)
  POST /api/parse               Upload a file → parsed questions JSON
  POST /api/quiz                Save a quiz   → quiz ID
  GET  /api/quizzes             List quizzes for the authenticated school
"""

import os
import re
import uuid
import random
import logging
import functools
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, g
from flask_cors import CORS
import fitz  # PyMuPDF
import jwt as pyjwt
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import Session
from models import School, User, Quiz, Question, QuizQuestion, Subject, SubjectTopic, UserSubject, Assignment, Result, Answer, ResultTopicScore

# ─── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, supports_credentials=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"pdf", "txt"}
MAX_FILE_BYTES     = 10 * 1024 * 1024  # 10 MB

# ─── Database setup ───────────────────────────────────────────────────────────

_engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=1,
    max_overflow=0,
)

# ─── Auth helpers ─────────────────────────────────────────────────────────────
#
# Supabase signs JWTs with a project-specific secret (the JWT Secret from
# Project Settings → API). We verify the signature locally — no round-trip
# to Supabase on every request.

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SETUP_SECRET      = os.environ.get("SETUP_SECRET", "")

# JWKS cache — fetched once per process, refreshed on key-not-found errors.
_jwks_client: pyjwt.PyJWKClient | None = None


def _get_jwks_client() -> pyjwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        _jwks_client = pyjwt.PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_client


def _verify_jwt(token: str) -> dict:
    """
    Verify a Supabase JWT using their JWKS endpoint (ECC P-256 / ES256).
    Raises jwt.InvalidTokenError on failure.
    """
    client = _get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(token)
    return pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256", "RS256", "HS256"],  # accept any Supabase-issued algorithm
        audience="authenticated",
        leeway=30,  # tolerate up to 30s clock skew between Supabase and server
    )


def require_auth(f):
    """
    Decorator: extract and verify the Bearer JWT, then load the User row.
    Populates g.user (User ORM object) and g.school_id (str).
    Returns 401 if missing/invalid, 403 if user not found in our users table.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header."}), 401

        token = auth_header[len("Bearer "):]
        try:
            payload = _verify_jwt(token)
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired. Please log in again."}), 401
        except pyjwt.InvalidTokenError as exc:
            return jsonify({"error": f"Invalid token: {exc}"}), 401

        auth_id = payload.get("sub")
        if not auth_id:
            return jsonify({"error": "Token missing subject claim."}), 401

        with Session(_engine) as session:
            user = session.execute(
                select(User).where(User.auth_id == auth_id)
            ).scalar_one_or_none()

        if user is None:
            return jsonify({"error": "Account not found. Contact your administrator."}), 403

        g.user      = user
        g.school_id = user.school_id
        g.user_id   = user.id
        return f(*args, **kwargs)
    return wrapper


# ─── Helpers ──────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(file_bytes: bytes) -> str:
    doc   = fitz.open(stream=file_bytes, filetype="pdf")
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n\n".join(pages)


def extract_text_from_txt(file_bytes: bytes) -> str:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode text file — please save it as UTF-8.")


def parse_questions(raw: str) -> list[dict]:
    """
    Parse a Q&A formatted string into a list of question dicts.

    Expected format (one blank line between questions):

        Topic: Algebra          ← optional; applies to all questions until next Topic: line
        Q: What does CSS stand for?
        A) Computer Style Syntax
        B) Cascading Style Sheets
        C) Creative Styling Service
        D) Colorful Sheet System
        Correct: B

    Returns:
        [{ "q": str, "opts": [str, ...], "correct": int, "topic": str|None }, ...]
    """
    OPT_MAP = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw.strip()) if b.strip()]
    if not blocks:
        raise ValueError("No question blocks found in the file.")

    questions   = []
    current_topic = None  # sticky — carries forward until a new Topic: line appears

    for idx, block in enumerate(blocks, start=1):
        lines = [l.strip() for l in block.splitlines() if l.strip()]

        # Topic: line is optional and sets the sticky topic for following questions
        t_line = next((l for l in lines if re.match(r"^Topic:", l, re.I)), None)
        if t_line:
            current_topic = re.sub(r"^Topic:\s*", "", t_line, flags=re.I).strip() or None

        q_line    = next((l for l in lines if re.match(r"^Q:", l, re.I)), None)
        opt_lines = [l for l in lines if re.match(r"^[A-Ea-e]\)", l)]
        c_line    = next((l for l in lines if re.match(r"^Correct:", l, re.I)), None)

        # A block with only a Topic: line is valid — it just sets the topic
        if not q_line and t_line:
            continue

        if not q_line:
            raise ValueError(f"Block {idx}: missing 'Q:' line.")
        if len(opt_lines) < 2:
            raise ValueError(f"Block {idx}: need at least 2 options (A), B) …).")
        if not c_line:
            raise ValueError(f"Block {idx}: missing 'Correct:' line.")

        letter      = re.sub(r"^Correct:\s*", "", c_line, flags=re.I).strip().upper()
        correct_idx = OPT_MAP.get(letter)

        if correct_idx is None:
            raise ValueError(f"Block {idx}: 'Correct: {letter}' is not a valid option letter.")
        if correct_idx >= len(opt_lines):
            raise ValueError(f"Block {idx}: 'Correct: {letter}' points to a non-existent option.")

        questions.append({
            "q":       re.sub(r"^Q:\s*", "", q_line, flags=re.I).strip(),
            "opts":    [re.sub(r"^[A-Ea-e]\)\s*", "", l).strip() for l in opt_lines],
            "correct": correct_idx,
            "topic":   current_topic,
        })

    return questions


def save_quiz(title: str, questions: list[dict], source_filename: str,
              school_id: str, user_id: str) -> dict:
    """
    Persist a quiz and its questions scoped to a school.
    Incoming questions use legacy API names {q, opts, correct};
    these are translated to DB names {text, options, correct_index}.
    """
    quiz_id = str(uuid.uuid4())
    now     = datetime.now(timezone.utc)

    with Session(_engine) as session:
        quiz = Quiz(
            id              = quiz_id,
            school_id       = school_id,
            created_by      = user_id,
            title           = title or "Untitled Quiz",
            source_filename = source_filename,
            created_at      = now,
        )
        session.add(quiz)

        for position, q in enumerate(questions):
            question = Question(
                id            = str(uuid.uuid4()),
                school_id     = school_id,
                text          = q["q"],
                options       = q["opts"],
                correct_index = q["correct"],
                topic         = q.get("topic"),
            )
            session.add(question)
            session.add(QuizQuestion(
                quiz_id     = quiz_id,
                question_id = question.id,
                position    = position,
            ))

        session.commit()

    log.info("Saved quiz %s (%d questions) for school %s", quiz_id, len(questions), school_id)

    return {
        "id":              quiz_id,
        "title":           title or "Untitled Quiz",
        "source_filename": source_filename,
        "created_at":      now.isoformat(),
        "question_count":  len(questions),
        "questions":       questions,
    }


def load_quiz(quiz_id: str) -> dict | None:
    """
    Load a quiz by ID. No school scoping here — the embed widget needs
    to fetch quizzes by ID without being logged in.
    """
    with Session(_engine) as session:
        quiz = session.get(Quiz, quiz_id)
        if quiz is None:
            return None

        stmt = (
            select(Question)
            .join(QuizQuestion, QuizQuestion.question_id == Question.id)
            .where(QuizQuestion.quiz_id == quiz_id)
            .where(Question.deleted_at.is_(None))
            .order_by(QuizQuestion.position)
        )
        rows = session.execute(stmt).scalars().all()

        questions = [
            {"q": row.text, "opts": row.options, "correct": row.correct_index, "topic": row.topic}
            for row in rows
        ]

        return {
            "id":              quiz.id,
            "title":           quiz.title,
            "source_filename": quiz.source_filename,
            "created_at":      quiz.created_at.isoformat(),
            "question_count":  len(questions),
            "questions":       questions,
        }


def list_quizzes(school_id: str) -> list[dict]:
    """
    Return metadata for all quizzes belonging to a school, newest first.
    """
    with Session(_engine) as session:
        count_subq = (
            select(
                QuizQuestion.quiz_id,
                func.count(QuizQuestion.question_id).label("question_count"),
            )
            .group_by(QuizQuestion.quiz_id)
            .subquery()
        )
        stmt = (
            select(Quiz, count_subq.c.question_count)
            .outerjoin(count_subq, count_subq.c.quiz_id == Quiz.id)
            .where(Quiz.school_id == school_id)
            .order_by(Quiz.created_at.desc())
        )
        rows = session.execute(stmt).all()

        return [
            {
                "id":              quiz.id,
                "title":           quiz.title,
                "source_filename": quiz.source_filename,
                "created_at":      quiz.created_at.isoformat(),
                "question_count":  count or 0,
            }
            for quiz, count in rows
        ]


# ─── Routes — Public ──────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    admin_path = Path(__file__).parent / "admin.html"
    return admin_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html"}


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "QuizEngine Backend"})


@app.route("/api/quiz/<quiz_id>", methods=["GET"])
def get_quiz(quiz_id: str):
    """Public — used by the embedded widget."""
    if not re.match(r"^[0-9a-f\-]{36}$", quiz_id):
        return jsonify({"error": "Invalid quiz ID."}), 400
    record = load_quiz(quiz_id)
    if record is None:
        return jsonify({"error": f"Quiz '{quiz_id}' not found."}), 404
    return jsonify(record)


@app.route("/preview/<quiz_id>", methods=["GET"])
def preview_quiz(quiz_id: str):
    if not re.match(r"^[0-9a-f\-]{36}$", quiz_id):
        return "Invalid quiz ID.", 400
    record = load_quiz(quiz_id)
    if record is None:
        return f"Quiz '{quiz_id}' not found.", 404

    title = record["title"]
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #f6f6f9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      padding: 2rem 1rem;
    }}
    .wrap {{ max-width: 680px; margin: 0 auto; }}
    .top-bar {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 1.75rem;
    }}
    .top-bar h1 {{ font-size: 20px; font-weight: 700; color: #111; }}
    .badge {{
      font-size: 11px; background: #e8e8f0; color: #666;
      padding: 3px 12px; border-radius: 99px; font-weight: 500;
    }}
    #status {{ font-size: 14px; color: #888; text-align: center; margin-top: 3rem; }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top-bar">
    <h1>{title}</h1>
    <span class="badge">Preview</span>
  </div>
  <div id="quiz-mount"></div>
  <div id="status">Loading quiz…</div>
</div>
<script src="/static/quiz-engine.js"></script>
<script>
  fetch("/api/quiz/{quiz_id}")
    .then(r => {{ if (!r.ok) throw new Error("Quiz not found"); return r.json(); }})
    .then(data => {{
      document.getElementById("status").textContent = "";
      QuizEngine.init({{
        target:    "#quiz-mount",
        title:     data.title,
        questions: data.questions
      }});
    }})
    .catch(err => {{
      document.getElementById("status").textContent = "Could not load quiz: " + err.message;
    }});
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}


@app.route("/static/quiz-engine.js", methods=["GET"])
def serve_engine():
    engine_path = Path(__file__).parent / "quiz-engine.js"
    if not engine_path.exists():
        return "quiz-engine.js not found.", 404
    return engine_path.read_text(encoding="utf-8"), 200, {
        "Content-Type": "application/javascript",
        "Access-Control-Allow-Origin": "*",
    }


# ─── Routes — Auth ────────────────────────────────────────────────────────────

@app.route("/setup", methods=["GET"])
def setup_page():
    """
    Private school-provisioning page — only accessible by the platform owner.
    Serves an HTML form; submission goes to POST /setup.
    The SETUP_SECRET env var must be set or this page returns 404.
    """
    if not SETUP_SECRET:
        return "", 404

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>QuizEngine — Provision School</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0f0f13; color: #e2e2e8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      padding: 2rem 1rem;
    }
    .box {
      width: 100%; max-width: 420px;
      background: #17171f; border: 1px solid #2a2a3a;
      border-radius: 16px; padding: 2.5rem 2rem;
      display: flex; flex-direction: column; gap: 1.25rem;
    }
    h1 { font-size: 18px; font-weight: 700; }
    p  { font-size: 13px; color: #888899; line-height: 1.6; }
    label { font-size: 12px; font-weight: 500; color: #888899; display: block; margin-bottom: 5px; }
    input {
      width: 100%; background: #1e1e2a; border: 1px solid #2a2a3a;
      border-radius: 7px; padding: 9px 12px;
      font-size: 14px; color: #e2e2e8; outline: none;
    }
    input:focus { border-color: #7b6ef6; }
    button {
      width: 100%; padding: 10px; background: #7b6ef6; border: none;
      border-radius: 7px; color: #fff; font-size: 14px; font-weight: 600;
      cursor: pointer;
    }
    button:hover { background: #9d92ff; }
    .msg { font-size: 13px; padding: 9px 12px; border-radius: 7px; display: none; }
    .msg.ok  { background: rgba(52,211,153,.1); border: 1px solid #34d399; color: #34d399; }
    .msg.err { background: rgba(248,113,113,.1); border: 1px solid #f87171; color: #f87171; }
  </style>
</head>
<body>
<div class="box">
  <div>
    <h1>Provision a School</h1>
    <p style="margin-top:6px">Platform owner only. Creates a new school and its first admin account.</p>
  </div>
  <div><label>Setup secret</label><input type="password" id="secret" placeholder="Your SETUP_SECRET"/></div>
  <div><label>School name</label><input type="text" id="school" placeholder="e.g. Greenwood High School"/></div>
  <div><label>Admin email</label><input type="email" id="email" placeholder="admin@school.edu"/></div>
  <div><label>Admin password</label><input type="password" id="password" placeholder="Min 8 characters"/></div>
  <div class="msg" id="msg"></div>
  <button onclick="provision()">Create school &amp; admin</button>
</div>
<script>
async function provision() {
  const secret   = document.getElementById('secret').value.trim();
  const school   = document.getElementById('school').value.trim();
  const email    = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  const msg      = document.getElementById('msg');

  msg.style.display = 'none';
  if (!secret || !school || !email || !password) {
    showMsg('All fields are required.', 'err'); return;
  }

  const res  = await fetch('/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret, school_name: school, email, password }),
  });
  const data = await res.json();
  if (res.ok) {
    showMsg('School created! Admin can now log in at the main page.', 'ok');
    document.getElementById('school').value   = '';
    document.getElementById('email').value    = '';
    document.getElementById('password').value = '';
  } else {
    showMsg(data.error || 'Something went wrong.', 'err');
  }
}
function showMsg(text, type) {
  const el = document.getElementById('msg');
  el.textContent = text; el.className = 'msg ' + type;
  el.style.display = 'block';
}
document.addEventListener('keydown', e => { if (e.key === 'Enter') provision(); });
</script>
</body>
</html>""", 200, {"Content-Type": "text/html"}


@app.route("/setup", methods=["POST"])
def setup_provision():
    """
    Create a new school + admin account. Requires the correct SETUP_SECRET.
    Called by the /setup page form — not part of the public API.
    """
    import urllib.request
    import urllib.error
    import json as _json

    if not SETUP_SECRET:
        return jsonify({"error": "Setup is not enabled on this server."}), 404

    body        = request.get_json(silent=True) or {}
    secret      = body.get("secret", "")
    email       = (body.get("email") or "").strip().lower()
    password    = body.get("password") or ""
    school_name = (body.get("school_name") or "").strip()

    if secret != SETUP_SECRET:
        return jsonify({"error": "Invalid setup secret."}), 403
    if not email or not password or not school_name:
        return jsonify({"error": "email, password, and school_name are required."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    # Create Supabase auth user
    req_data = _json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/signup",
        data    = req_data,
        method  = "POST",
        headers = {"Content-Type": "application/json", "apikey": SUPABASE_ANON_KEY},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            auth_data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = _json.loads(exc.read())
        return jsonify({"error": err_body.get("msg") or "Supabase signup failed."}), 400

    auth_id = auth_data.get("user", {}).get("id")
    if not auth_id:
        return jsonify({"error": "Supabase did not return a user ID."}), 500

    school_id = str(uuid.uuid4())
    user_id   = str(uuid.uuid4())
    now       = datetime.now(timezone.utc)

    with Session(_engine) as session:
        session.add(School(id=school_id, name=school_name, created_at=now))
        session.add(User(
            id         = user_id,
            auth_id    = auth_id,
            email      = email,
            school_id  = school_id,
            role       = "admin",
            created_at = now,
        ))
        session.commit()

    log.info("School provisioned: %s (%s) admin=%s", school_name, school_id, email)
    return jsonify({"message": f"School '{school_name}' created with admin {email}."}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    Exchange email + password for a Supabase JWT.

    JSON body: { email, password }
    Returns:   { access_token, user: { id, email, school_id, role } }
    """
    import urllib.request
    import urllib.error
    import json as _json

    body     = request.get_json(silent=True) or {}
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not password:
        return jsonify({"error": "email and password are required."}), 400

    supabase_login_url = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"
    req_data = _json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        supabase_login_url,
        data    = req_data,
        method  = "POST",
        headers = {
            "Content-Type": "application/json",
            "apikey":       SUPABASE_ANON_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            auth_data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = _json.loads(exc.read())
        return jsonify({"error": err_body.get("error_description") or "Invalid email or password."}), 401

    access_token = auth_data.get("access_token")
    auth_id      = auth_data.get("user", {}).get("id")

    if not access_token or not auth_id:
        return jsonify({"error": "Unexpected response from auth service."}), 500

    with Session(_engine) as session:
        user = session.execute(
            select(User).where(User.auth_id == auth_id)
        ).scalar_one_or_none()

    if user is None:
        return jsonify({"error": "Account not found. Contact your administrator."}), 403

    return jsonify({
        "access_token": access_token,
        "user": {
            "id":        user.id,
            "email":     email,
            "school_id": user.school_id,
            "role":      user.role,
        },
    })


# ─── Routes — Protected ───────────────────────────────────────────────────────

@app.route("/api/parse", methods=["POST"])
@require_auth
def parse_file():
    """Upload a PDF or TXT file → receive parsed questions. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Only admins can upload files."}), 403
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Filename is empty."}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 415

    file_bytes = f.read()
    if len(file_bytes) > MAX_FILE_BYTES:
        return jsonify({"error": "File exceeds 10 MB limit."}), 413

    ext = f.filename.rsplit(".", 1)[1].lower()

    try:
        raw_text = extract_text_from_pdf(file_bytes) if ext == "pdf" else extract_text_from_txt(file_bytes)
    except Exception as exc:
        log.exception("Text extraction failed")
        return jsonify({"error": f"Could not extract text: {exc}"}), 422

    if not raw_text.strip():
        return jsonify({"error": "The file appears to be empty."}), 422

    try:
        questions = parse_questions(raw_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422

    title = request.form.get("title", "").strip() or f.filename.rsplit(".", 1)[0]

    return jsonify({
        "title":          title,
        "questions":      questions,
        "question_count": len(questions),
    })


@app.route("/api/quiz", methods=["POST"])
@require_auth
def create_quiz():
    """Save a quiz from uploaded file questions. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Only admins can create quizzes from uploads."}), 403
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON."}), 400

    questions = body.get("questions")
    if not isinstance(questions, list) or len(questions) == 0:
        return jsonify({"error": "'questions' must be a non-empty array."}), 400

    for i, q in enumerate(questions):
        if not isinstance(q.get("q"), str) or not q["q"].strip():
            return jsonify({"error": f"Question {i+1}: 'q' field is missing or empty."}), 400
        if not isinstance(q.get("opts"), list) or len(q["opts"]) < 2:
            return jsonify({"error": f"Question {i+1}: 'opts' must have at least 2 items."}), 400
        if not isinstance(q.get("correct"), int) or q["correct"] >= len(q["opts"]):
            return jsonify({"error": f"Question {i+1}: 'correct' index is out of range."}), 400

    record = save_quiz(
        title           = body.get("title", ""),
        questions       = questions,
        source_filename = body.get("source_filename", ""),
        school_id       = g.school_id,
        user_id         = g.user_id,
    )

    return jsonify({
        "id":             record["id"],
        "title":          record["title"],
        "created_at":     record["created_at"],
        "question_count": record["question_count"],
    }), 201


@app.route("/api/quizzes", methods=["GET"])
@require_auth
def get_quizzes():
    """
    List quizzes for the authenticated school.
    Teachers with subject assignments only see quizzes that contain
    at least one question whose topic is in their allowed set.
    """
    with Session(_engine) as session:
        allowed = _allowed_topics_for_user(session, g.user)

    if allowed is None:
        return jsonify({"quizzes": list_quizzes(g.school_id)})

    # Restrict to quizzes that have at least one question in allowed topics
    with Session(_engine) as session:
        count_subq = (
            select(
                QuizQuestion.quiz_id,
                func.count(QuizQuestion.question_id).label("question_count"),
            )
            .group_by(QuizQuestion.quiz_id)
            .subquery()
        )
        allowed_quiz_ids_subq = (
            select(QuizQuestion.quiz_id)
            .join(Question, Question.id == QuizQuestion.question_id)
            .where(Question.topic.in_(allowed))
            .distinct()
            .subquery()
        )
        stmt = (
            select(Quiz, count_subq.c.question_count)
            .outerjoin(count_subq, count_subq.c.quiz_id == Quiz.id)
            .where(Quiz.school_id == g.school_id)
            .where(Quiz.id.in_(select(allowed_quiz_ids_subq.c.quiz_id)))
            .order_by(Quiz.created_at.desc())
        )
        rows = session.execute(stmt).all()

    return jsonify({"quizzes": [
        {
            "id":              quiz.id,
            "title":           quiz.title,
            "source_filename": quiz.source_filename,
            "created_at":      quiz.created_at.isoformat(),
            "question_count":  count or 0,
        }
        for quiz, count in rows
    ]})


def _allowed_topics_for_user(session, user) -> list[str] | None:
    """
    Return the list of topics the user may see, or None if unrestricted.
    Teachers with assigned subjects see only those subjects' topics.
    Admins and teachers with no subject assignments see everything.
    """
    links = session.execute(
        select(UserSubject).where(UserSubject.user_id == user.id)
    ).scalars().all()

    if not links:
        return None  # unrestricted

    subject_ids = [l.subject_id for l in links]
    topic_rows = session.execute(
        select(SubjectTopic.topic).where(SubjectTopic.subject_id.in_(subject_ids))
    ).scalars().all()
    return list(topic_rows)


def _pick_questions_by_rules(session, school_id: str, topic_rules: list) -> list:
    """
    Pick questions from the bank according to topic_rules [{topic, count}].
    Returns a randomly-ordered flat list of Question ORM objects.
    Raises ValueError if any topic has fewer available questions than requested.
    """
    picked = []
    for rule in topic_rules:
        topic = rule.get("topic", "").strip()
        count = int(rule.get("count", 0))
        if count <= 0:
            continue
        rows = session.execute(
            select(Question)
            .where(Question.school_id == school_id)
            .where(Question.topic == topic)
            .where(Question.deleted_at.is_(None))
        ).scalars().all()
        if len(rows) < count:
            raise ValueError(
                f"Topic '{topic}' only has {len(rows)} question(s) but {count} were requested."
            )
        picked.extend(random.sample(rows, count))
    random.shuffle(picked)
    return picked


def _apply_shuffles(questions: list, randomize_questions: bool, randomize_options: bool):
    """
    Given a list of Question ORM objects, apply shuffles and return
    (ordered_questions, question_order, option_orders).

    question_order: [question_id, ...]  — IDs in the display order
    option_orders:  {question_id: [original_idx, ...]}  — maps display slot → original index
    """
    if randomize_questions:
        random.shuffle(questions)

    question_order = [q.id for q in questions]
    option_orders  = {}

    if randomize_options:
        for q in questions:
            indices = list(range(len(q.options)))
            random.shuffle(indices)
            option_orders[q.id] = indices
    else:
        for q in questions:
            option_orders[q.id] = list(range(len(q.options)))

    return questions, question_order, option_orders


def _serialize_questions_for_student(questions: list, option_orders: dict) -> list:
    """
    Serialize Question ORM objects for the student-facing take API.
    Applies option shuffle according to option_orders.
    correct_index is intentionally excluded.
    """
    out = []
    for q in questions:
        order = option_orders.get(q.id, list(range(len(q.options))))
        out.append({
            "id":      q.id,
            "text":    q.text,
            "options": [q.options[i] for i in order],
            "topic":   q.topic,
        })
    return out


@app.route("/api/quiz/generate-from-rules", methods=["POST"])
@require_auth
def generate_quiz_from_rules():
    """
    Mode 2 helper — generate a preview paper from topic_rules without saving it.
    Teachers call this repeatedly (reroll) and then confirm via POST /api/quiz/from-bank.

    Body: { topic_rules: [{topic, count}, ...] }
    Returns: { questions: [{id, text, options, topic}, ...] }  — correct_index included (admin preview)
    """
    body        = request.get_json(silent=True) or {}
    topic_rules = body.get("topic_rules")
    if not isinstance(topic_rules, list) or len(topic_rules) == 0:
        return jsonify({"error": "'topic_rules' must be a non-empty array."}), 400

    with Session(_engine) as session:
        try:
            questions = _pick_questions_by_rules(session, g.school_id, topic_rules)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422

    return jsonify({
        "questions": [
            {
                "id":            q.id,
                "text":          q.text,
                "options":       q.options,
                "correct_index": q.correct_index,
                "topic":         q.topic,
            }
            for q in questions
        ]
    })


@app.route("/api/questions", methods=["GET"])
@require_auth
def get_questions():
    """
    List questions in the school's question bank.
    Teachers with subject assignments only see their subjects' topics.
    Optional query param: ?topic=Algebra  — additional filter by topic.
    """
    topic_filter = request.args.get("topic", "").strip() or None

    with Session(_engine) as session:
        allowed = _allowed_topics_for_user(session, g.user)

        stmt = select(Question).where(Question.school_id == g.school_id).where(Question.deleted_at.is_(None))
        if allowed is not None:
            stmt = stmt.where(Question.topic.in_(allowed))
        if topic_filter:
            stmt = stmt.where(Question.topic == topic_filter)
        stmt = stmt.order_by(Question.topic.nulls_last(), Question.text)
        rows = session.execute(stmt).scalars().all()

    return jsonify({
        "questions": [
            {
                "id":            row.id,
                "text":          row.text,
                "options":       row.options,
                "correct_index": row.correct_index,
                "topic":         row.topic,
            }
            for row in rows
        ]
    })


@app.route("/api/topics", methods=["GET"])
@require_auth
def get_topics():
    """
    List distinct topics for this school's questions.
    Teachers with subject assignments only see their subjects' topics.
    """
    with Session(_engine) as session:
        allowed = _allowed_topics_for_user(session, g.user)

        stmt = (
            select(Question.topic)
            .where(Question.school_id == g.school_id)
            .where(Question.topic.isnot(None))
            .where(Question.deleted_at.is_(None))
        )
        if allowed is not None:
            stmt = stmt.where(Question.topic.in_(allowed))
        stmt = stmt.distinct().order_by(Question.topic)
        rows = session.execute(stmt).scalars().all()

    return jsonify({"topics": list(rows)})


@app.route("/api/quiz/from-bank", methods=["POST"])
@require_auth
def create_quiz_from_bank():
    """
    Create a quiz by selecting existing question IDs from the bank.

    JSON body:
      { title: str, question_ids: [uuid, ...] }

    Questions must belong to the authenticated school.
    Order in the resulting quiz matches the order of question_ids in the request.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON."}), 400

    title        = (body.get("title") or "").strip() or "Untitled Quiz"
    question_ids = body.get("question_ids")

    if not isinstance(question_ids, list) or len(question_ids) == 0:
        return jsonify({"error": "'question_ids' must be a non-empty array."}), 400

    with Session(_engine) as session:
        # Fetch all requested questions, verifying they belong to this school
        rows = session.execute(
            select(Question)
            .where(Question.id.in_(question_ids))
            .where(Question.school_id == g.school_id)
            .where(Question.deleted_at.is_(None))
        ).scalars().all()

        found_ids = {row.id for row in rows}
        missing   = [qid for qid in question_ids if qid not in found_ids]
        if missing:
            return jsonify({"error": f"Question IDs not found: {missing}"}), 404

        # Preserve the caller's requested order
        questions_by_id = {row.id: row for row in rows}
        ordered         = [questions_by_id[qid] for qid in question_ids]

        quiz_id = str(uuid.uuid4())
        now     = datetime.now(timezone.utc)

        quiz = Quiz(
            id              = quiz_id,
            school_id       = g.school_id,
            created_by      = g.user_id,
            title           = title,
            source_filename = "",
            created_at      = now,
        )
        session.add(quiz)

        for position, q in enumerate(ordered):
            session.add(QuizQuestion(
                quiz_id     = quiz_id,
                question_id = q.id,
                position    = position,
            ))

        session.commit()

    log.info("Quiz from bank: %s (%d questions) school=%s", quiz_id, len(ordered), g.school_id)
    return jsonify({
        "id":             quiz_id,
        "title":          title,
        "created_at":     now.isoformat(),
        "question_count": len(ordered),
    }), 201


@app.route("/api/question/<question_id>", methods=["PATCH"])
@require_auth
def update_question(question_id: str):
    """Update a question's topic. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    body  = request.get_json(silent=True) or {}
    topic = body.get("topic", "").strip() or None

    with Session(_engine) as session:
        q = session.execute(
            select(Question)
            .where(Question.id == question_id)
            .where(Question.school_id == g.school_id)
            .where(Question.deleted_at.is_(None))
        ).scalar_one_or_none()

        if q is None:
            return jsonify({"error": "Question not found."}), 404

        q.topic = topic
        session.commit()

    return jsonify({"id": question_id, "topic": topic})


@app.route("/api/question/<question_id>/quizzes", methods=["GET"])
@require_auth
def get_question_quizzes(question_id: str):
    """Return all quizzes that contain a specific question (school-scoped)."""
    with Session(_engine) as session:
        q = session.execute(
            select(Question)
            .where(Question.id == question_id)
            .where(Question.school_id == g.school_id)
            .where(Question.deleted_at.is_(None))
        ).scalar_one_or_none()

        if q is None:
            return jsonify({"error": "Question not found."}), 404

        rows = session.execute(
            select(Quiz)
            .join(QuizQuestion, QuizQuestion.quiz_id == Quiz.id)
            .where(QuizQuestion.question_id == question_id)
            .where(Quiz.school_id == g.school_id)
            .order_by(Quiz.title)
        ).scalars().all()

    return jsonify({
        "quizzes": [{"id": r.id, "title": r.title} for r in rows]
    })


@app.route("/api/question/<question_id>/quiz/<quiz_id>", methods=["DELETE"])
@require_auth
def remove_question_from_quiz(question_id: str, quiz_id: str):
    """Remove a question from a specific quiz. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    with Session(_engine) as session:
        # Verify quiz belongs to this school
        quiz = session.execute(
            select(Quiz)
            .where(Quiz.id == quiz_id)
            .where(Quiz.school_id == g.school_id)
        ).scalar_one_or_none()

        if quiz is None:
            return jsonify({"error": "Quiz not found."}), 404

        qq = session.execute(
            select(QuizQuestion)
            .where(QuizQuestion.quiz_id == quiz_id)
            .where(QuizQuestion.question_id == question_id)
        ).scalar_one_or_none()

        if qq is None:
            return jsonify({"error": "Question is not in this quiz."}), 404

        session.delete(qq)
        session.commit()

    return jsonify({"message": "Question removed from quiz."})


@app.route("/api/question/<question_id>", methods=["DELETE"])
@require_auth
def delete_question(question_id: str):
    """
    Delete a question permanently. Admin only.
    Blocked if the question is still linked to any quiz — caller must
    remove it from all quizzes first via DELETE /api/question/<id>/quiz/<quiz_id>.
    """
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    with Session(_engine) as session:
        q = session.execute(
            select(Question)
            .where(Question.id == question_id)
            .where(Question.school_id == g.school_id)
            .where(Question.deleted_at.is_(None))
        ).scalar_one_or_none()

        if q is None:
            return jsonify({"error": "Question not found."}), 404

        quiz_count = session.execute(
            select(func.count()).where(QuizQuestion.question_id == question_id)
        ).scalar()

        if quiz_count > 0:
            return jsonify({
                "error": f"Question is still in {quiz_count} quiz(es). Remove it from all quizzes first.",
                "quiz_count": quiz_count,
            }), 409

        q.deleted_at = datetime.now(timezone.utc)
        session.commit()

    log.info("Soft-deleted question %s school=%s", question_id, g.school_id)
    return jsonify({"message": "Question deleted."})


@app.route("/api/quiz/<quiz_id>", methods=["PATCH"])
@require_auth
def update_quiz(quiz_id: str):
    """Rename a quiz title. Teacher or admin."""
    body  = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required."}), 400

    with Session(_engine) as session:
        quiz = session.execute(
            select(Quiz)
            .where(Quiz.id == quiz_id)
            .where(Quiz.school_id == g.school_id)
        ).scalar_one_or_none()
        if quiz is None:
            return jsonify({"error": "Quiz not found."}), 404
        quiz.title = title
        session.commit()

    return jsonify({"id": quiz_id, "title": title})


@app.route("/api/quiz/<quiz_id>", methods=["DELETE"])
@require_auth
def delete_quiz(quiz_id: str):
    """
    Delete a quiz. Admin only.
    Questions in the bank are NOT deleted — only the quiz and its
    quiz_questions join rows (CASCADE handles those automatically).
    """
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    with Session(_engine) as session:
        quiz = session.execute(
            select(Quiz)
            .where(Quiz.id == quiz_id)
            .where(Quiz.school_id == g.school_id)
        ).scalar_one_or_none()

        if quiz is None:
            return jsonify({"error": "Quiz not found."}), 404

        session.delete(quiz)
        session.commit()

    log.info("Deleted quiz %s school=%s", quiz_id, g.school_id)
    return jsonify({"message": "Quiz deleted."})


# ─── Routes — Phase 4: Subjects & Teachers ────────────────────────────────────

@app.route("/api/subjects", methods=["GET"])
@require_auth
def list_subjects():
    """List all subjects for this school, including their topics and assigned teachers."""
    with Session(_engine) as session:
        subjects = session.execute(
            select(Subject).where(Subject.school_id == g.school_id).order_by(Subject.name)
        ).scalars().all()

        result = []
        for subj in subjects:
            topics = [st.topic for st in subj.topic_links]
            teacher_ids = [ul.user_id for ul in subj.user_links]
            teachers = []
            if teacher_ids:
                t_rows = session.execute(
                    select(User).where(User.id.in_(teacher_ids))
                ).scalars().all()
                teachers = [{"id": t.id, "email": t.email, "role": t.role} for t in t_rows]
            result.append({
                "id":       subj.id,
                "name":     subj.name,
                "topics":   sorted(topics),
                "teachers": teachers,
            })

    return jsonify({"subjects": result})


@app.route("/api/subjects", methods=["POST"])
@require_auth
def create_subject():
    """Create a new subject. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required."}), 400

    with Session(_engine) as session:
        existing = session.execute(
            select(Subject)
            .where(Subject.school_id == g.school_id)
            .where(Subject.name == name)
        ).scalar_one_or_none()
        if existing:
            return jsonify({"error": f"Subject '{name}' already exists."}), 409

        subj = Subject(
            id        = str(uuid.uuid4()),
            school_id = g.school_id,
            name      = name,
        )
        session.add(subj)
        session.commit()
        subject_id = subj.id

    log.info("Subject created: %s school=%s", name, g.school_id)
    return jsonify({"id": subject_id, "name": name, "topics": [], "teachers": []}), 201


@app.route("/api/subjects/<subject_id>", methods=["DELETE"])
@require_auth
def delete_subject(subject_id: str):
    """Delete a subject and all its topic/user assignments. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    with Session(_engine) as session:
        subj = session.execute(
            select(Subject)
            .where(Subject.id == subject_id)
            .where(Subject.school_id == g.school_id)
        ).scalar_one_or_none()

        if subj is None:
            return jsonify({"error": "Subject not found."}), 404

        session.delete(subj)
        session.commit()

    return jsonify({"message": "Subject deleted."})


@app.route("/api/subjects/<subject_id>/topics", methods=["PUT"])
@require_auth
def set_subject_topics(subject_id: str):
    """
    Replace all topic assignments for a subject.
    Body: { topics: [str, ...] }
    Admin only.
    """
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    body   = request.get_json(silent=True) or {}
    topics = body.get("topics")
    if not isinstance(topics, list):
        return jsonify({"error": "'topics' must be an array."}), 400

    topics = [t.strip() for t in topics if isinstance(t, str) and t.strip()]

    with Session(_engine) as session:
        subj = session.execute(
            select(Subject)
            .where(Subject.id == subject_id)
            .where(Subject.school_id == g.school_id)
        ).scalar_one_or_none()

        if subj is None:
            return jsonify({"error": "Subject not found."}), 404

        # Replace: delete all existing, add new ones
        session.execute(
            SubjectTopic.__table__.delete().where(SubjectTopic.subject_id == subject_id)
        )
        for t in topics:
            session.add(SubjectTopic(subject_id=subject_id, topic=t))

        session.commit()

    return jsonify({"id": subject_id, "topics": sorted(topics)})


@app.route("/api/subjects/<subject_id>/teachers", methods=["PUT"])
@require_auth
def set_subject_teachers(subject_id: str):
    """
    Replace all teacher assignments for a subject.
    Body: { user_ids: [uuid, ...] }
    Admin only.
    """
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    body     = request.get_json(silent=True) or {}
    user_ids = body.get("user_ids")
    if not isinstance(user_ids, list):
        return jsonify({"error": "'user_ids' must be an array."}), 400

    with Session(_engine) as session:
        subj = session.execute(
            select(Subject)
            .where(Subject.id == subject_id)
            .where(Subject.school_id == g.school_id)
        ).scalar_one_or_none()

        if subj is None:
            return jsonify({"error": "Subject not found."}), 404

        # Verify all users belong to this school
        if user_ids:
            valid = session.execute(
                select(User.id)
                .where(User.id.in_(user_ids))
                .where(User.school_id == g.school_id)
            ).scalars().all()
            invalid = set(user_ids) - set(valid)
            if invalid:
                return jsonify({"error": f"Users not found in this school: {list(invalid)}"}), 404

        session.execute(
            UserSubject.__table__.delete().where(UserSubject.subject_id == subject_id)
        )
        for uid in user_ids:
            session.add(UserSubject(user_id=uid, subject_id=subject_id))

        session.commit()

    return jsonify({"id": subject_id, "user_ids": user_ids})


@app.route("/api/teachers", methods=["GET"])
@require_auth
def list_teachers():
    """List all teachers (and admins) in this school. Admin only."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    with Session(_engine) as session:
        users = session.execute(
            select(User)
            .where(User.school_id == g.school_id)
            .order_by(User.created_at)
        ).scalars().all()

        result = []
        for u in users:
            subj_ids = [us.subject_id for us in u.subject_links]
            result.append({
                "id":          u.id,
                "email":       u.email,
                "role":        u.role,
                "subject_ids": subj_ids,
                "created_at":  u.created_at.isoformat(),
            })

    return jsonify({"teachers": result})


@app.route("/api/teachers", methods=["POST"])
@require_auth
def invite_teacher():
    """
    Create a Supabase auth user and add them as a teacher in this school.
    Body: { email, password, role: 'teacher'|'admin' }
    Admin only.
    """
    import urllib.request
    import urllib.error
    import json as _json

    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    body     = request.get_json(silent=True) or {}
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    role     = (body.get("role") or "teacher").strip().lower()

    if not email or not password:
        return jsonify({"error": "email and password are required."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if role not in ("teacher", "admin"):
        return jsonify({"error": "role must be 'teacher' or 'admin'."}), 400

    # Create Supabase auth user
    req_data = _json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/signup",
        data    = req_data,
        method  = "POST",
        headers = {"Content-Type": "application/json", "apikey": SUPABASE_ANON_KEY},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            auth_data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = _json.loads(exc.read())
        return jsonify({"error": err_body.get("msg") or "Supabase signup failed."}), 400

    auth_id = auth_data.get("user", {}).get("id")
    if not auth_id:
        return jsonify({"error": "Supabase did not return a user ID."}), 500

    user_id = str(uuid.uuid4())
    with Session(_engine) as session:
        session.add(User(
            id        = user_id,
            auth_id   = auth_id,
            email     = email,
            school_id = g.school_id,
            role      = role,
        ))
        session.commit()

    log.info("Teacher invited: %s role=%s school=%s", email, role, g.school_id)
    return jsonify({"id": user_id, "email": email, "role": role}), 201


@app.route("/api/teachers/<user_id>", methods=["DELETE"])
@require_auth
def delete_teacher(user_id: str):
    """Remove a teacher from this school. Admin only. Cannot delete yourself."""
    if g.user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403
    if user_id == g.user_id:
        return jsonify({"error": "You cannot remove your own account."}), 400

    with Session(_engine) as session:
        user = session.execute(
            select(User)
            .where(User.id == user_id)
            .where(User.school_id == g.school_id)
        ).scalar_one_or_none()

        if user is None:
            return jsonify({"error": "Teacher not found."}), 404

        session.delete(user)
        session.commit()

    return jsonify({"message": "Teacher removed."})


# ─── Routes — Assignments ─────────────────────────────────────────────────────

@app.route("/api/assignments", methods=["GET"])
@require_auth
def list_assignments():
    """List all assignments for this school, newest first."""
    with Session(_engine) as session:
        rows = session.execute(
            select(Assignment, Quiz.title)
            .outerjoin(Quiz, Quiz.id == Assignment.quiz_id)
            .where(Assignment.school_id == g.school_id)
            .order_by(Assignment.created_at.desc())
        ).all()

    return jsonify({"assignments": [
        {
            "id":                   a.id,
            "class_name":           a.class_name,
            "quiz_id":              a.quiz_id,
            "quiz_title":           title,
            "mode":                 a.mode,
            "randomize_questions":  a.randomize_questions,
            "randomize_options":    a.randomize_options,
            "duration_minutes":     a.duration_minutes,
            "opens_at":             a.opens_at.isoformat()  if a.opens_at  else None,
            "closes_at":            a.closes_at.isoformat() if a.closes_at else None,
            "created_at":           a.created_at.isoformat(),
        }
        for a, title in rows
    ]})


@app.route("/api/assignments", methods=["POST"])
@require_auth
def create_assignment():
    """
    Create an assignment. Supports three modes:
      manual       — quiz_id required; uses a pre-built quiz as-is
      randomized   — quiz_id required; quiz was pre-built via generate-from-rules
      total_random — topic_rules required; no quiz_id; paper generated live per student
    """
    body                 = request.get_json(silent=True) or {}
    quiz_id              = (body.get("quiz_id") or "").strip() or None
    class_name           = (body.get("class_name") or "").strip()
    opens_at             = body.get("opens_at")
    closes_at            = body.get("closes_at")
    mode                 = (body.get("mode") or "manual").strip().lower()
    randomize_questions  = bool(body.get("randomize_questions", False))
    randomize_options    = bool(body.get("randomize_options",   False))
    topic_rules          = body.get("topic_rules")  # [{topic, count}]
    duration_minutes     = body.get("duration_minutes")  # int or None

    if mode not in ("manual", "randomized", "total_random"):
        return jsonify({"error": "mode must be 'manual', 'randomized', or 'total_random'."}), 400
    if not class_name:
        return jsonify({"error": "class_name is required."}), 400
    if mode in ("manual", "randomized") and not quiz_id:
        return jsonify({"error": "quiz_id is required for manual and randomized modes."}), 400
    if mode == "total_random":
        if not isinstance(topic_rules, list) or len(topic_rules) == 0:
            return jsonify({"error": "topic_rules is required for total_random mode."}), 400
        quiz_id = None  # explicitly no quiz for live-generated papers

    def _parse_dt(val, end_of_day=False):
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except Exception:
            return None

    with Session(_engine) as session:
        if quiz_id:
            quiz = session.execute(
                select(Quiz)
                .where(Quiz.id == quiz_id)
                .where(Quiz.school_id == g.school_id)
            ).scalar_one_or_none()
            if quiz is None:
                return jsonify({"error": "Quiz not found."}), 404

        if mode == "total_random":
            # Validate topic_rules against the bank now so the teacher knows immediately
            try:
                _pick_questions_by_rules(session, g.school_id, topic_rules)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 422

        dur = None
        if duration_minutes is not None:
            try:
                dur = int(duration_minutes)
                if dur <= 0:
                    dur = None
            except (TypeError, ValueError):
                dur = None

        a = Assignment(
            id                  = str(uuid.uuid4()),
            school_id           = g.school_id,
            quiz_id             = quiz_id,
            created_by          = g.user_id,
            class_name          = class_name,
            opens_at            = _parse_dt(opens_at,  end_of_day=False),
            closes_at           = _parse_dt(closes_at, end_of_day=True),
            mode                = mode,
            randomize_questions = randomize_questions,
            randomize_options   = randomize_options,
            topic_rules         = topic_rules,
            duration_minutes    = dur,
        )
        session.add(a)
        session.commit()
        aid = a.id

    log.info("Assignment created: class=%s mode=%s quiz=%s school=%s", class_name, mode, quiz_id, g.school_id)
    return jsonify({"id": aid, "class_name": class_name, "quiz_id": quiz_id, "mode": mode}), 201


@app.route("/api/assignments/<assignment_id>", methods=["PATCH"])
@require_auth
def update_assignment(assignment_id: str):
    """
    Update assignment fields: class_name, duration_minutes, opens_at, closes_at.
    Also accepts is_closed=true to immediately close (sets closes_at=now)
    or is_closed=false to reopen (clears closes_at).
    """
    body       = request.get_json(silent=True) or {}

    # Quick close/reopen toggle — doesn't require class_name
    if "is_closed" in body:
        now = datetime.now(timezone.utc)
        with Session(_engine) as session:
            a = session.execute(
                select(Assignment)
                .where(Assignment.id == assignment_id)
                .where(Assignment.school_id == g.school_id)
            ).scalar_one_or_none()
            if a is None:
                return jsonify({"error": "Assignment not found."}), 404
            a.closes_at = now if body["is_closed"] else None
            session.commit()
            is_closed = body["is_closed"]
        return jsonify({"id": assignment_id, "is_closed": is_closed})

    class_name = (body.get("class_name") or "").strip()
    if not class_name:
        return jsonify({"error": "class_name is required."}), 400

    def _parse_dt(val, end_of_day=False):
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except Exception:
            return None

    duration_minutes = body.get("duration_minutes")
    dur = None
    if duration_minutes is not None:
        try:
            dur = int(duration_minutes)
            if dur <= 0:
                dur = None
        except (TypeError, ValueError):
            dur = None

    opens_at  = _parse_dt(body.get("opens_at"),  end_of_day=False)
    closes_at = _parse_dt(body.get("closes_at"), end_of_day=True)
    clear_opens  = body.get("opens_at")  == ""
    clear_closes = body.get("closes_at") == ""

    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(Assignment.school_id == g.school_id)
        ).scalar_one_or_none()
        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        a.class_name       = class_name
        a.duration_minutes = dur
        if opens_at or clear_opens:
            a.opens_at = opens_at
        if closes_at or clear_closes:
            a.closes_at = closes_at
        session.commit()

    return jsonify({"id": assignment_id, "class_name": class_name})


@app.route("/api/assignments/<assignment_id>/results/<result_id>", methods=["PATCH"])
@require_auth
def reset_result(assignment_id: str, result_id: str):
    """
    Reset a student's result so they can retake the quiz.
    Sets is_complete=False — the row stays in DB but is hidden from stats
    and the student can start fresh (the /start endpoint cleans up incomplete rows).
    """
    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(Assignment.school_id == g.school_id)
        ).scalar_one_or_none()
        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        result = session.execute(
            select(Result)
            .where(Result.id == result_id)
            .where(Result.assignment_id == assignment_id)
        ).scalar_one_or_none()
        if result is None:
            return jsonify({"error": "Result not found."}), 404

        result.is_complete = False
        session.commit()

    log.info("Result reset: result=%s assignment=%s by user=%s", result_id, assignment_id, g.user_id)
    return jsonify({"id": result_id, "is_complete": False})


@app.route("/api/assignments/<assignment_id>", methods=["DELETE"])
@require_auth
def delete_assignment(assignment_id: str):
    """Delete an assignment (and its results). Teacher or admin."""
    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(Assignment.school_id == g.school_id)
        ).scalar_one_or_none()
        if a is None:
            return jsonify({"error": "Assignment not found."}), 404
        session.delete(a)
        session.commit()
    return jsonify({"message": "Assignment deleted."})


@app.route("/api/assignments/<assignment_id>/results", methods=["GET"])
@require_auth
def get_assignment_results(assignment_id: str):
    """
    Get all student results for an assignment.
    Includes per-topic scores and per-answer breakdown with question text.
    """
    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(Assignment.school_id == g.school_id)
        ).scalar_one_or_none()
        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        results = session.execute(
            select(Result)
            .where(Result.assignment_id == assignment_id)
            .where(Result.is_complete == True)  # noqa: E712
            .order_by(Result.submitted_at)
        ).scalars().all()

        # Pre-load all questions for this school so we can attach text to answers
        all_questions = session.execute(
            select(Question).where(Question.school_id == g.school_id)
        ).scalars().all()
        question_lookup = {q.id: q for q in all_questions}

        out = []
        for r in results:
            ans_rows = session.execute(
                select(Answer).where(Answer.result_id == r.id)
            ).scalars().all()

            topic_rows = session.execute(
                select(ResultTopicScore).where(ResultTopicScore.result_id == r.id)
            ).scalars().all()

            answers_out = []
            for ans in ans_rows:
                q = question_lookup.get(ans.question_id)
                answers_out.append({
                    "question_id":    ans.question_id,
                    "question_text":  q.text          if q else None,
                    "options":        q.options        if q else [],
                    "correct_index":  q.correct_index  if q else None,
                    "topic":          q.topic          if q else None,
                    "chosen_index":   ans.chosen_index,
                    "is_correct":     ans.is_correct,
                })

            # If no stored topic scores (pre-Phase-4 submissions), derive from answers
            if topic_rows:
                topic_scores_out = [
                    {
                        "topic":   ts.topic,
                        "correct": ts.correct,
                        "total":   ts.total,
                        "percent": round(ts.correct / ts.total * 100) if ts.total else 0,
                    }
                    for ts in sorted(topic_rows, key=lambda x: x.topic)
                ]
            else:
                acc: dict = {}
                for ans in answers_out:
                    topic = ans["topic"] or "Untagged"
                    if topic not in acc:
                        acc[topic] = {"correct": 0, "total": 0}
                    acc[topic]["total"] += 1
                    if ans["is_correct"]:
                        acc[topic]["correct"] += 1
                topic_scores_out = [
                    {
                        "topic":   t,
                        "correct": v["correct"],
                        "total":   v["total"],
                        "percent": round(v["correct"] / v["total"] * 100) if v["total"] else 0,
                    }
                    for t, v in sorted(acc.items())
                ]

            time_taken_seconds = None
            if r.started_at and r.submitted_at:
                time_taken_seconds = max(0, int((r.submitted_at - r.started_at).total_seconds()))

            out.append({
                "id":                 r.id,
                "student_name":       r.student_name,
                "roll_number":        r.roll_number,
                "class_name":         r.class_name,
                "score":              r.score,
                "total":              r.total,
                "percent":            round(r.score / r.total * 100) if r.total else 0,
                "submitted_at":       r.submitted_at.isoformat(),
                "started_at":         r.started_at.isoformat() if r.started_at else None,
                "time_taken_seconds": time_taken_seconds,
                "topic_scores":       topic_scores_out,
                "answers":            answers_out,
            })

    return jsonify({"results": out})


@app.route("/api/assignments/<assignment_id>/analytics", methods=["GET"])
@require_auth
def get_assignment_analytics(assignment_id: str):
    """
    Aggregate analytics for an assignment:
    - Summary stats (count, avg %, avg time, avg score)
    - Score distribution (5 buckets: 0-20, 21-40, 41-60, 61-80, 81-100)
    - Per-topic performance (avg %, total correct/total, student count)
    - Per-question difficulty (% correct, wrong count, option breakdown)
    """
    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(Assignment.school_id == g.school_id)
        ).scalar_one_or_none()
        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        results = session.execute(
            select(Result)
            .where(Result.assignment_id == assignment_id)
            .where(Result.is_complete == True)  # noqa: E712
        ).scalars().all()

        if not results:
            return jsonify({
                "summary": None,
                "distribution": [],
                "topics": [],
                "questions": [],
            })

        # ── Summary ──────────────────────────────────────────────────────────
        n        = len(results)
        avg_pct  = round(sum(r.score / r.total * 100 if r.total else 0 for r in results) / n)
        avg_score_num = sum(r.score for r in results)
        avg_score_den = sum(r.total for r in results) // n if n else 0
        times    = [int((r.submitted_at - r.started_at).total_seconds())
                    for r in results if r.started_at and r.submitted_at]
        avg_time = round(sum(times) / len(times)) if times else None

        summary = {
            "count":        n,
            "avg_percent":  avg_pct,
            "avg_time_seconds": avg_time,
            "avg_score":    round(sum(r.score for r in results) / n, 1),
            "avg_total":    round(sum(r.total for r in results) / n, 1),
        }

        # ── Score distribution ────────────────────────────────────────────────
        buckets = [0, 0, 0, 0, 0]  # 0-20, 21-40, 41-60, 61-80, 81-100
        for r in results:
            pct = round(r.score / r.total * 100) if r.total else 0
            idx = min(4, pct // 21) if pct < 100 else 4
            idx = min(4, pct // 20) if pct > 0 else 0
            buckets[idx] += 1
        distribution = [
            {"label": "0–20%",   "count": buckets[0]},
            {"label": "21–40%",  "count": buckets[1]},
            {"label": "41–60%",  "count": buckets[2]},
            {"label": "61–80%",  "count": buckets[3]},
            {"label": "81–100%", "count": buckets[4]},
        ]

        # ── Topic performance (from stored ResultTopicScore rows) ─────────────
        topic_acc: dict = {}
        for r in results:
            ts_rows = session.execute(
                select(ResultTopicScore).where(ResultTopicScore.result_id == r.id)
            ).scalars().all()
            for ts in ts_rows:
                if ts.topic not in topic_acc:
                    topic_acc[ts.topic] = {"correct": 0, "total": 0, "students": 0}
                topic_acc[ts.topic]["correct"]  += ts.correct
                topic_acc[ts.topic]["total"]    += ts.total
                topic_acc[ts.topic]["students"] += 1

        topics_out = sorted([
            {
                "topic":    t,
                "correct":  v["correct"],
                "total":    v["total"],
                "students": v["students"],
                "percent":  round(v["correct"] / v["total"] * 100) if v["total"] else 0,
            }
            for t, v in topic_acc.items()
        ], key=lambda x: x["percent"])  # weakest first

        # ── Per-question difficulty ───────────────────────────────────────────
        # Collect all answer rows for this assignment
        result_ids = [r.id for r in results]
        all_answers = session.execute(
            select(Answer).where(Answer.result_id.in_(result_ids))
        ).scalars().all()

        # Load question metadata
        if a.mode == "total_random":
            q_ids = list({ans.question_id for ans in all_answers if ans.question_id})
            q_rows = session.execute(
                select(Question)
                .where(Question.id.in_(q_ids))
                .where(Question.school_id == g.school_id)
            ).scalars().all()
        else:
            q_rows = session.execute(
                select(Question)
                .join(QuizQuestion, QuizQuestion.question_id == Question.id)
                .where(QuizQuestion.quiz_id == a.quiz_id)
                .where(Question.deleted_at.is_(None))
                .order_by(QuizQuestion.position)
            ).scalars().all()

        q_lookup = {q.id: q for q in q_rows}

        # Per-question accumulators: {q_id: {correct:n, total:n, option_counts:{idx:n}}}
        q_acc: dict = {}
        for ans in all_answers:
            qid = ans.question_id
            if not qid or qid not in q_lookup:
                continue
            if qid not in q_acc:
                q_acc[qid] = {"correct": 0, "total": 0, "option_counts": {}}
            q_acc[qid]["total"] += 1
            if ans.is_correct:
                q_acc[qid]["correct"] += 1
            ci = ans.chosen_index
            q_acc[qid]["option_counts"][ci] = q_acc[qid]["option_counts"].get(ci, 0) + 1

        questions_out = []
        for position, q in enumerate(q_rows):
            acc = q_acc.get(q.id, {"correct": 0, "total": 0, "option_counts": {}})
            pct = round(acc["correct"] / acc["total"] * 100) if acc["total"] else 0
            option_breakdown = [
                {
                    "index":      i,
                    "text":       q.options[i] if i < len(q.options) else "?",
                    "count":      acc["option_counts"].get(i, 0),
                    "is_correct": i == q.correct_index,
                }
                for i in range(len(q.options))
            ]
            questions_out.append({
                "position":        position + 1,
                "question_id":     q.id,
                "text":            q.text,
                "topic":           q.topic,
                "percent_correct": pct,
                "correct_count":   acc["correct"],
                "wrong_count":     acc["total"] - acc["correct"],
                "total_attempts":  acc["total"],
                "option_breakdown": option_breakdown,
            })

        # Sort weakest first
        questions_out.sort(key=lambda x: x["percent_correct"])

    return jsonify({
        "summary":      summary,
        "distribution": distribution,
        "topics":       topics_out,
        "questions":    questions_out,
    })


# ─── Routes — Student take (public) ───────────────────────────────────────────

@app.route("/take", methods=["GET"])
def take_page():
    take_path = Path(__file__).parent / "take.html"
    if not take_path.exists():
        return "take.html not found.", 404
    return take_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html"}


@app.route("/api/take/<class_name>", methods=["GET"])
def get_take_quiz(class_name: str):
    """
    Public — student enters a class name, gets back active assignment(s).

    If only one active assignment exists for the class code, returns it directly
    in the same shape as before (for backwards compatibility with take.html).

    If multiple exist, returns { multiple: true, assignments: [{id, quiz_title, duration_minutes}, ...] }
    so take.html can show a picker. The student then calls this endpoint with
    ?assignment_id=<id> to load the specific assignment.

    Does NOT include correct answers.
    """
    assignment_id_filter = request.args.get("assignment_id", "").strip() or None
    now = datetime.now(timezone.utc)

    with Session(_engine) as session:
        base_query = (
            select(Assignment)
            .where(func.lower(Assignment.class_name) == class_name.strip().lower())
            .where(
                (Assignment.opens_at.is_(None)) | (Assignment.opens_at <= now)
            )
            .where(
                (Assignment.closes_at.is_(None)) |
                (Assignment.closes_at >= now)
            )
            .order_by(Assignment.created_at.desc())
        )

        if assignment_id_filter:
            # Student selected a specific assignment from the picker
            assignments = session.execute(
                base_query.where(Assignment.id == assignment_id_filter)
            ).scalars().all()
        else:
            assignments = session.execute(base_query).scalars().all()

        if not assignments:
            return jsonify({"error": "No active assignment found for this class."}), 404

        # Multiple assignments and no specific one selected — return the picker list
        if len(assignments) > 1 and not assignment_id_filter:
            quiz_ids = [a.quiz_id for a in assignments if a.quiz_id]
            quiz_titles = {}
            if quiz_ids:
                quiz_rows = session.execute(
                    select(Quiz.id, Quiz.title).where(Quiz.id.in_(quiz_ids))
                ).all()
                quiz_titles = {r.id: r.title for r in quiz_rows}

            return jsonify({
                "multiple": True,
                "class_name": assignments[0].class_name,
                "assignments": [
                    {
                        "id":               a.id,
                        "quiz_title":       quiz_titles.get(a.quiz_id, "Quiz") if a.quiz_id else "Quiz",
                        "duration_minutes": a.duration_minutes,
                    }
                    for a in assignments
                ],
            })

        # Single assignment — load and return the full quiz data
        a = assignments[0]

        if a.mode == "total_random":
            # Live paper: pick questions fresh for this student from topic_rules
            try:
                questions = _pick_questions_by_rules(session, a.school_id, a.topic_rules or [])
            except ValueError as exc:
                return jsonify({"error": f"Assignment configuration error: {exc}"}), 500
            quiz_title = "Quiz"  # no saved quiz for total_random
        else:
            # manual or randomized: load from the saved quiz
            if not a.quiz_id:
                return jsonify({"error": "Assignment has no quiz attached."}), 500
            quiz = session.get(Quiz, a.quiz_id)
            if quiz is None:
                return jsonify({"error": "Quiz not found."}), 404
            quiz_title = quiz.title
            stmt = (
                select(Question)
                .join(QuizQuestion, QuizQuestion.question_id == Question.id)
                .where(QuizQuestion.quiz_id == a.quiz_id)
                .where(Question.deleted_at.is_(None))
                .order_by(QuizQuestion.position)
            )
            questions = list(session.execute(stmt).scalars().all())

        questions, question_order, option_orders = _apply_shuffles(
            questions,
            randomize_questions = a.randomize_questions,
            randomize_options   = a.randomize_options,
        )

    return jsonify({
        "assignment_id":    a.id,
        "quiz_title":       quiz_title,
        "class_name":       a.class_name,
        "duration_minutes": a.duration_minutes,
        "question_order":   question_order,
        "option_orders":    option_orders,
        "questions":        _serialize_questions_for_student(questions, option_orders),
    })


@app.route("/api/take/<class_name>/start", methods=["POST"])
def start_quiz(class_name: str):
    """
    Public — called when a student clicks Start quiz.
    Records started_at on a new Result row and returns remaining_seconds.

    Body: { assignment_id, student_name, roll_number }
    Returns: { result_id, remaining_seconds }  — remaining_seconds is None when no timer
    """
    body          = request.get_json(silent=True) or {}
    assignment_id = (body.get("assignment_id") or "").strip()
    student_name  = (body.get("student_name")  or "").strip()
    roll_number   = (body.get("roll_number")   or "").strip()

    if not assignment_id or not student_name or not roll_number:
        return jsonify({"error": "assignment_id, student_name, and roll_number are required."}), 400

    now = datetime.now(timezone.utc)

    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(func.lower(Assignment.class_name) == class_name.strip().lower())
        ).scalar_one_or_none()

        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        if a.closes_at and a.closes_at <= now:
            return jsonify({"error": "This assignment has closed."}), 403

        # Block re-attempts: if a completed result exists for this roll number, reject
        existing_complete = session.execute(
            select(Result)
            .where(Result.assignment_id == assignment_id)
            .where(Result.roll_number == roll_number)
            .where(Result.is_complete == True)  # noqa: E712
        ).scalar_one_or_none()
        if existing_complete:
            return jsonify({"error": "You have already submitted this test. Re-attempts are not allowed."}), 403

        # Clean up any prior abandoned (incomplete) attempts by this roll number
        session.execute(
            Result.__table__.delete()
            .where(Result.__table__.c.assignment_id == assignment_id)
            .where(Result.__table__.c.roll_number == roll_number)
            .where(Result.__table__.c.is_complete == False)  # noqa: E712
        )

        result_id = str(uuid.uuid4())
        result = Result(
            id            = result_id,
            assignment_id = assignment_id,
            student_name  = student_name,
            roll_number   = roll_number,
            class_name    = a.class_name,
            score         = 0,
            total         = 0,
            submitted_at  = now,
            started_at    = now,
            is_complete   = False,
        )
        session.add(result)
        session.commit()

        remaining_seconds = None
        if a.duration_minutes:
            remaining_seconds = a.duration_minutes * 60

    return jsonify({"result_id": result_id, "remaining_seconds": remaining_seconds})


@app.route("/api/take/<class_name>/submit", methods=["POST"])
def submit_quiz(class_name: str):
    """
    Public — student submits their answers.
    Body: {
      assignment_id, student_name, roll_number,
      question_order: [id, ...],
      option_orders:  {id: [original_idx, ...]},
      answers: [{question_id, chosen_index}]   ← chosen_index is in display (shuffled) space
    }
    """
    body           = request.get_json(silent=True) or {}
    assignment_id  = (body.get("assignment_id") or "").strip()
    student_name   = (body.get("student_name")  or "").strip()
    roll_number    = (body.get("roll_number")   or "").strip()
    raw_answers    = body.get("answers", [])
    question_order = body.get("question_order") or []
    option_orders  = body.get("option_orders")  or {}
    result_id_in   = (body.get("result_id") or "").strip() or None  # from /start

    if not assignment_id or not student_name or not roll_number:
        return jsonify({"error": "assignment_id, student_name, and roll_number are required."}), 400
    if not isinstance(raw_answers, list):
        raw_answers = []

    now = datetime.now(timezone.utc)

    with Session(_engine) as session:
        a = session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .where(func.lower(Assignment.class_name) == class_name.strip().lower())
        ).scalar_one_or_none()

        if a is None:
            return jsonify({"error": "Assignment not found."}), 404

        if a.closes_at and a.closes_at <= now:
            return jsonify({"error": "This assignment has closed."}), 403

        # Timed delivery: check if deadline has passed
        if result_id_in and a.duration_minutes:
            existing = session.get(Result, result_id_in)
            if existing and existing.started_at:
                elapsed = (now - existing.started_at).total_seconds()
                if elapsed > a.duration_minutes * 60 + 30:  # 30s grace period
                    return jsonify({"error": "Time's up. This submission arrived after the deadline."}), 403

        # Load all questions for this assignment (across all modes)
        if a.mode == "total_random":
            # For total_random we don't have a saved quiz; question IDs come from the
            # submitted question_order (what the server sent the student).
            q_ids = [qid for qid in question_order if qid]
            q_rows = session.execute(
                select(Question)
                .where(Question.id.in_(q_ids))
                .where(Question.school_id == a.school_id)
                .where(Question.deleted_at.is_(None))
            ).scalars().all()
        else:
            q_rows = session.execute(
                select(Question)
                .join(QuizQuestion, QuizQuestion.question_id == Question.id)
                .where(QuizQuestion.quiz_id == a.quiz_id)
                .where(Question.deleted_at.is_(None))
            ).scalars().all()

        correct_map  = {q.id: q.correct_index for q in q_rows}
        topic_map    = {q.id: q.topic          for q in q_rows}

        score        = 0
        answer_objs  = []
        # Per-topic accumulators: {topic: {"correct": n, "total": n}}
        topic_acc    = {}

        for ans in raw_answers:
            qid            = ans.get("question_id", "")
            display_chosen = ans.get("chosen_index")
            if display_chosen is None or qid not in correct_map:
                continue

            # Map display index back to original index using option_orders
            display_chosen = int(display_chosen)
            order = option_orders.get(qid)
            if order and len(order) > display_chosen:
                original_chosen = order[display_chosen]
            else:
                original_chosen = display_chosen

            correct    = correct_map[qid]
            is_correct = (original_chosen == correct)
            if is_correct:
                score += 1

            topic = topic_map.get(qid) or "Untagged"
            if topic not in topic_acc:
                topic_acc[topic] = {"correct": 0, "total": 0}
            topic_acc[topic]["total"]   += 1
            if is_correct:
                topic_acc[topic]["correct"] += 1

            answer_objs.append({
                "question_id":  qid,
                "chosen_index": original_chosen,   # store in original index space
                "is_correct":   is_correct,
            })

        total = len(correct_map)

        if result_id_in:
            # Re-use the Result row created by /start; update it with final scores
            result = session.get(Result, result_id_in)
            if result is None:
                result_id_in = None  # fall through to create new
        if not result_id_in:
            result = None

        if result:
            result.score          = score
            result.total          = total
            result.submitted_at   = now
            result.question_order = question_order or None
            result.option_orders  = option_orders  or None
            result.is_complete    = True
            result_id = result.id
        else:
            result_id = str(uuid.uuid4())
            result = Result(
                id             = result_id,
                assignment_id  = assignment_id,
                student_name   = student_name,
                roll_number    = roll_number,
                class_name     = a.class_name,
                score          = score,
                total          = total,
                submitted_at   = now,
                question_order = question_order or None,
                option_orders  = option_orders  or None,
                is_complete    = True,
            )
            session.add(result)

        for ans in answer_objs:
            session.add(Answer(
                id           = str(uuid.uuid4()),
                result_id    = result_id,
                question_id  = ans["question_id"],
                chosen_index = ans["chosen_index"],
                is_correct   = ans["is_correct"],
            ))

        for topic, counts in topic_acc.items():
            session.add(ResultTopicScore(
                id        = str(uuid.uuid4()),
                result_id = result_id,
                topic     = topic,
                correct   = counts["correct"],
                total     = counts["total"],
            ))

        session.commit()

    log.info("Quiz submitted: student=%s roll=%s class=%s score=%d/%d",
             student_name, roll_number, class_name, score, total)

    # Return correct answers in original index space so take.html can display correctly
    return jsonify({
        "score":           score,
        "total":           total,
        "percent":         round(score / total * 100) if total else 0,
        "correct_answers": {qid: idx for qid, idx in correct_map.items()},
        "option_orders":   option_orders,
    }), 201


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("QuizEngine backend starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True)
