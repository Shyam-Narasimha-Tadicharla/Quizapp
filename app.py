"""
QuizEngine Backend
──────────────────
Flask server that accepts PDF / TXT file uploads,
extracts Q&A text, parses it into structured questions,
and stores quizzes for retrieval by ID.

Routes
  POST /api/parse          Upload a file → parsed questions JSON
  POST /api/quiz           Save a quiz   → quiz ID
  GET  /api/quiz/<id>      Fetch a quiz by ID
  GET  /api/quizzes        List all saved quizzes
  GET  /api/health         Health check
"""

import os
import re
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
from upstash_redis import Redis

# ─── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)  # allow requests from any origin (restrict in production)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"pdf", "txt"}
MAX_FILE_BYTES     = 10 * 1024 * 1024  # 10 MB


# ─── Helpers ──────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract plain text from a PDF using PyMuPDF."""
    doc  = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    doc.close()
    return "\n\n".join(pages)


def extract_text_from_txt(file_bytes: bytes) -> str:
    """Decode a plain-text upload."""
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

        Q: What does CSS stand for?
        A) Computer Style Syntax
        B) Cascading Style Sheets
        C) Creative Styling Service
        D) Colorful Sheet System
        Correct: B

    Returns:
        [{ "q": str, "opts": [str, ...], "correct": int }, ...]
    """
    OPT_MAP = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

    # Split into blocks separated by one or more blank lines
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw.strip()) if b.strip()]

    if not blocks:
        raise ValueError("No question blocks found in the file.")

    questions = []

    for idx, block in enumerate(blocks, start=1):
        lines = [l.strip() for l in block.splitlines() if l.strip()]

        # ── Extract each part ──
        q_line   = next((l for l in lines if re.match(r"^Q:", l, re.I)), None)
        opt_lines = [l for l in lines if re.match(r"^[A-Ea-e]\)", l)]
        c_line   = next((l for l in lines if re.match(r"^Correct:", l, re.I)), None)

        # ── Validate ──
        if not q_line:
            raise ValueError(f"Block {idx}: missing 'Q:' line.")
        if len(opt_lines) < 2:
            raise ValueError(f"Block {idx}: need at least 2 options (A), B) …).")
        if not c_line:
            raise ValueError(f"Block {idx}: missing 'Correct:' line.")

        letter = re.sub(r"^Correct:\s*", "", c_line, flags=re.I).strip().upper()
        correct_idx = OPT_MAP.get(letter)

        if correct_idx is None:
            raise ValueError(f"Block {idx}: 'Correct: {letter}' is not a valid option letter.")
        if correct_idx >= len(opt_lines):
            raise ValueError(f"Block {idx}: 'Correct: {letter}' points to a non-existent option.")

        questions.append({
            "q":       re.sub(r"^Q:\s*", "", q_line, flags=re.I).strip(),
            "opts":    [re.sub(r"^[A-Ea-e]\)\s*", "", l).strip() for l in opt_lines],
            "correct": correct_idx,
        })

    return questions


def _get_redis() -> Redis:
    return Redis(
        url=os.environ["KV_REST_API_URL"],
        token=os.environ["KV_REST_API_TOKEN"],
    )


def save_quiz(title: str, questions: list[dict], source_filename: str = "") -> dict:
    """Persist a quiz to Vercel KV and return its metadata."""
    quiz_id = str(uuid.uuid4())
    now     = datetime.now(timezone.utc).isoformat()
    record  = {
        "id":              quiz_id,
        "title":           title or "Untitled Quiz",
        "source_filename": source_filename,
        "created_at":      now,
        "question_count":  len(questions),
        "questions":       questions,
    }
    r = _get_redis()
    r.set(f"quiz:{quiz_id}", json.dumps(record, ensure_ascii=False))
    r.rpush("quiz:index", quiz_id)
    log.info("Saved quiz %s (%d questions)", quiz_id, len(questions))
    return record


def load_quiz(quiz_id: str) -> dict | None:
    """Load a quiz by ID from Vercel KV, or None if not found."""
    data = _get_redis().get(f"quiz:{quiz_id}")
    if data is None:
        return None
    return json.loads(data)


def list_quizzes() -> list[dict]:
    """Return metadata (no questions) for all saved quizzes, newest first."""
    r = _get_redis()
    ids = r.lrange("quiz:index", 0, -1)
    records = []
    for quiz_id in ids:
        data = r.get(f"quiz:{quiz_id}")
        if data is None:
            continue
        try:
            q = json.loads(data)
            records.append({
                "id":              q["id"],
                "title":           q["title"],
                "source_filename": q.get("source_filename", ""),
                "created_at":      q["created_at"],
                "question_count":  q["question_count"],
            })
        except Exception:
            pass
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    admin_path = Path(__file__).parent / "admin.html"
    return admin_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html"}


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "QuizEngine Backend"})


@app.route("/api/parse", methods=["POST"])
def parse_file():
    """
    Upload a PDF or TXT file → receive parsed questions.

    Form fields:
      file   (required) — the uploaded file
      title  (optional) — quiz title

    Returns 200:
      { questions: [...], question_count: N, title: str }
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Send the file in a 'file' form field."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Filename is empty."}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 415

    file_bytes = f.read()
    if len(file_bytes) > MAX_FILE_BYTES:
        return jsonify({"error": "File exceeds 10 MB limit."}), 413

    ext = f.filename.rsplit(".", 1)[1].lower()

    # ── Extract text ──
    try:
        if ext == "pdf":
            raw_text = extract_text_from_pdf(file_bytes)
        else:
            raw_text = extract_text_from_txt(file_bytes)
    except Exception as exc:
        log.exception("Text extraction failed")
        return jsonify({"error": f"Could not extract text from file: {exc}"}), 422

    if not raw_text.strip():
        return jsonify({"error": "The file appears to be empty or contains no extractable text."}), 422

    # ── Parse questions ──
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
def create_quiz():
    """
    Save a quiz for later retrieval.

    JSON body:
      { title: str, questions: [...], source_filename?: str }

    Returns 201:
      { id, title, created_at, question_count }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON."}), 400

    questions = body.get("questions")
    if not isinstance(questions, list) or len(questions) == 0:
        return jsonify({"error": "'questions' must be a non-empty array."}), 400

    # Light validation of question shape
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
    )

    return jsonify({
        "id":             record["id"],
        "title":          record["title"],
        "created_at":     record["created_at"],
        "question_count": record["question_count"],
    }), 201


@app.route("/api/quiz/<quiz_id>", methods=["GET"])
def get_quiz(quiz_id: str):
    """
    Fetch a saved quiz (including full questions) by ID.
    """
    # Basic sanitisation — UUIDs are hex + hyphens only
    if not re.match(r"^[0-9a-f\-]{36}$", quiz_id):
        return jsonify({"error": "Invalid quiz ID."}), 400

    record = load_quiz(quiz_id)
    if record is None:
        return jsonify({"error": f"Quiz '{quiz_id}' not found."}), 404

    return jsonify(record)


@app.route("/api/quizzes", methods=["GET"])
def get_quizzes():
    """
    List all saved quizzes (metadata only, no questions).
    """
    return jsonify({"quizzes": list_quizzes()})


@app.route("/static/quiz-engine.js", methods=["GET"])
def serve_engine():
    """
    Serve quiz-engine.js so the preview page can load it.
    Place quiz-engine.js in the same folder as app.py.
    """
    engine_path = Path(__file__).parent / "quiz-engine.js"
    if not engine_path.exists():
        return "quiz-engine.js not found. Place it in the same folder as app.py.", 404
    return engine_path.read_text(encoding="utf-8"), 200, {
        "Content-Type": "application/javascript",
        "Access-Control-Allow-Origin": "*",
    }


@app.route("/preview/<quiz_id>", methods=["GET"])
def preview_quiz(quiz_id: str):
    """
    Serve a fully working quiz page for a given quiz ID.
    Opens in the browser — no embed needed.
    """
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


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("QuizEngine backend starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True)
