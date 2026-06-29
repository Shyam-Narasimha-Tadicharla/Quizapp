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
from models import School, User, Quiz, Question, QuizQuestion

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
    """List all quizzes for the authenticated school."""
    return jsonify({"quizzes": list_quizzes(g.school_id)})


@app.route("/api/questions", methods=["GET"])
@require_auth
def get_questions():
    """
    List all questions in the school's question bank.
    Optional query param: ?topic=Algebra  — filters by topic.
    """
    topic_filter = request.args.get("topic", "").strip() or None

    with Session(_engine) as session:
        stmt = select(Question).where(Question.school_id == g.school_id)
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
    """List all distinct topics used by this school's questions, sorted."""
    with Session(_engine) as session:
        rows = session.execute(
            select(Question.topic)
            .where(Question.school_id == g.school_id)
            .where(Question.topic.isnot(None))
            .distinct()
            .order_by(Question.topic)
        ).scalars().all()

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

        session.delete(q)
        session.commit()

    log.info("Deleted question %s school=%s", question_id, g.school_id)
    return jsonify({"message": "Question deleted."})


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


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("QuizEngine backend starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True)
