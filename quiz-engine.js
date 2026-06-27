/**
 * QuizEngine — embeddable quiz widget
 * Usage: see embed-demo.html
 * Version: 1.0.0
 */
(function (global) {
  "use strict";

  // ─── Parser ───────────────────────────────────────────────────────────────
  function parseQuestions(raw) {
    const blocks = raw.trim().split(/\n\s*\n/).map(b => b.trim()).filter(Boolean);
    const optMap = { A: 0, B: 1, C: 2, D: 3, E: 4 };
    const questions = [];

    blocks.forEach((block, idx) => {
      const lines = block.split("\n").map(l => l.trim()).filter(Boolean);
      const qLine   = lines.find(l => /^Q:/i.test(l));
      const optLines = lines.filter(l => /^[A-E]\)/i.test(l));
      const cLine   = lines.find(l => /^Correct:/i.test(l));

      if (!qLine || optLines.length < 2 || !cLine) {
        throw new Error(
          `Block ${idx + 1}: must have a "Q:" line, at least 2 options (A)…), and a "Correct:" line.`
        );
      }

      const letter = cLine.replace(/^Correct:\s*/i, "").trim().toUpperCase();
      const correctIdx = optMap[letter];
      if (correctIdx === undefined || correctIdx >= optLines.length) {
        throw new Error(`Block ${idx + 1}: "Correct: ${letter}" is invalid or out of range.`);
      }

      questions.push({
        q:       qLine.replace(/^Q:\s*/i, "").trim(),
        opts:    optLines.map(l => l.replace(/^[A-E]\)\s*/i, "").trim()),
        correct: correctIdx,
      });
    });

    if (questions.length === 0) throw new Error("No valid questions found.");
    return questions;
  }

  // ─── Styles (injected into Shadow DOM) ────────────────────────────────────
  const CSS = `
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :host {
      display: block;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #1a1a1a;
      background: transparent;
    }

    .qe-wrap { max-width: 680px; margin: 0 auto; padding: 0 4px; }

    /* ── Header ── */
    .qe-header { margin-bottom: 1.5rem; }
    .qe-title  { font-size: 18px; font-weight: 600; color: #111; }
    .qe-sub    { font-size: 13px; color: #777; margin-top: 3px; }
    .qe-prog-bg   { height: 4px; background: #eee; border-radius: 99px; margin-top: 10px; overflow: hidden; }
    .qe-prog-fill { height: 100%; background: #5b50d6; border-radius: 99px; transition: width .35s ease; }

    /* ── Cards ── */
    .qe-card {
      background: #fff;
      border: 1px solid #e8e8e8;
      border-radius: 10px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1rem;
      box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .qe-q-num  { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #aaa; margin-bottom: 6px; }
    .qe-q-text { font-size: 15px; line-height: 1.6; color: #111; margin-bottom: 1rem; }

    /* ── Options ── */
    .qe-opts { display: flex; flex-direction: column; gap: 8px; }
    .qe-opt  {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px;
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      cursor: pointer;
      font-size: 14px; color: #222;
      background: #fff;
      transition: border-color .15s, background .15s, box-shadow .15s;
      user-select: none;
    }
    .qe-opt:hover { background: #f7f7f7; border-color: #ccc; }
    .qe-opt.selected { border-color: #5b50d6; background: #f0effe; color: #2e2882; box-shadow: 0 0 0 3px rgba(91,80,214,.12); }
    .qe-opt.correct  { border-color: #1a9e74 !important; background: #e4f8f0 !important; color: #0a4f3c !important; box-shadow: 0 0 0 3px rgba(26,158,116,.15) !important; }
    .qe-opt.wrong    { border-color: #d94f30 !important; background: #fdf0ec !important; color: #5a1a0a !important; box-shadow: 0 0 0 3px rgba(217,79,48,.15) !important; }
    .qe-opt.correct-unselected { border-color: #1a9e74 !important; background: #e4f8f0 !important; color: #0a4f3c !important; opacity: .7; }

    .qe-marker {
      width: 22px; height: 22px; border-radius: 50%;
      border: 1px solid #ddd;
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 600;
      flex-shrink: 0;
      transition: background .15s, border-color .15s, color .15s;
    }
    .qe-opt.selected .qe-marker         { background: #5b50d6; border-color: #5b50d6; color: #fff; }
    .qe-opt.correct  .qe-marker         { background: #1a9e74; border-color: #1a9e74; color: #fff; }
    .qe-opt.wrong    .qe-marker         { background: #d94f30; border-color: #d94f30; color: #fff; }
    .qe-opt.correct-unselected .qe-marker { background: #1a9e74; border-color: #1a9e74; color: #fff; }

    /* ── Actions ── */
    .qe-actions { display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 1.5rem; }
    .qe-btn {
      padding: 9px 22px; border-radius: 8px;
      font-size: 14px; font-weight: 500; cursor: pointer;
      border: 1px solid #ddd; background: #fff; color: #333;
      transition: background .15s, transform .1s;
    }
    .qe-btn:hover  { background: #f5f5f5; }
    .qe-btn:active { transform: scale(.98); }
    .qe-btn-primary { background: #5b50d6; border-color: #5b50d6; color: #fff; }
    .qe-btn-primary:hover { background: #4640b8; }
    .qe-btn-primary:disabled { opacity: .4; cursor: not-allowed; transform: none; }

    /* ── Score card ── */
    .qe-score-card {
      background: #fff; border: 1px solid #e8e8e8;
      border-radius: 10px; padding: 2rem;
      text-align: center; margin-bottom: 1.5rem;
      box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .qe-score-big    { font-size: 52px; font-weight: 700; color: #5b50d6; line-height: 1; }
    .qe-score-denom  { font-size: 18px; color: #999; margin-top: 4px; }
    .qe-score-pct    { font-size: 14px; color: #777; margin-top: 6px; }
    .qe-score-msg    { font-size: 15px; font-weight: 600; color: #222; margin-top: 12px; }

    /* ── Review ── */
    .qe-review-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #aaa; margin-bottom: .75rem; }
    .qe-tag { display: inline-block; font-size: 11px; padding: 2px 9px; border-radius: 99px; margin-left: 8px; font-weight: 500; vertical-align: middle; }
    .qe-tag-ok  { background: #e4f8f0; color: #0a4f3c; }
    .qe-tag-bad { background: #fdf0ec; color: #5a1a0a; }

    /* ── Error ── */
    .qe-error { background: #fdf0ec; border: 1px solid #f3c5b5; border-radius: 8px; padding: .75rem 1rem; font-size: 13px; color: #8b2000; margin-bottom: 1rem; }
  `;

  // ─── Renderer ──────────────────────────────────────────────────────────────
  const LETTERS = ["A", "B", "C", "D", "E"];

  function scoreMsg(pct) {
    if (pct === 100) return "Perfect score! 🎉";
    if (pct >= 80)  return "Great job!";
    if (pct >= 60)  return "Good effort.";
    if (pct >= 40)  return "Keep practising.";
    return "Review the material and try again.";
  }

  function createInstance(config) {
    // config: { target (HTMLElement), questions (array), title (string) }
    const { target, questions, title = "Quiz" } = config;
    let answers   = {};
    let submitted = false;

    // Shadow DOM so host styles can't interfere
    const shadow = target.attachShadow({ mode: "open" });
    const style  = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);

    const root = document.createElement("div");
    root.className = "qe-wrap";
    shadow.appendChild(root);

    function render() {
      const answered = Object.keys(answers).length;
      const total    = questions.length;
      const pct      = Math.round((answered / total) * 100);

      if (submitted) {
        const correctCount = questions.filter((q, i) => answers[i] === q.correct).length;
        const scorePct     = Math.round((correctCount / total) * 100);

        root.innerHTML = `
          <div class="qe-score-card">
            <div class="qe-score-big">${correctCount}</div>
            <div class="qe-score-denom">out of ${total}</div>
            <div class="qe-score-pct">${scorePct}% correct</div>
            <div class="qe-score-msg">${scoreMsg(scorePct)}</div>
          </div>
          <div class="qe-review-label">Review</div>
          ${questions.map((q, i) => {
            const chosen    = answers[i];
            const isCorrect = chosen === q.correct;
            return `<div class="qe-card">
              <div class="qe-q-num">Question ${i + 1}
                <span class="qe-tag ${isCorrect ? "qe-tag-ok" : "qe-tag-bad"}">${isCorrect ? "Correct" : "Incorrect"}</span>
              </div>
              <div class="qe-q-text">${q.q}</div>
              <div class="qe-opts">
                ${q.opts.map((o, j) => {
                  let cls = "qe-opt";
                  if (j === q.correct && j === chosen) cls += " correct";
                  else if (j === chosen)    cls += " wrong";
                  else if (j === q.correct) cls += " correct-unselected";
                  return `<div class="${cls}">
                    <div class="qe-marker">${LETTERS[j]}</div>
                    <span>${o}</span>
                  </div>`;
                }).join("")}
              </div>
            </div>`;
          }).join("")}
          <div class="qe-actions">
            <button class="qe-btn qe-btn-primary" id="qe-restart">Try again</button>
          </div>
        `;

        // Fire result event on the host element
        target.dispatchEvent(new CustomEvent("quizComplete", {
          bubbles: true,
          detail: {
            score:   correctCount,
            total,
            percent: scorePct,
            answers: questions.map((q, i) => ({
              question:  q.q,
              chosen:    q.opts[answers[i]],
              correct:   q.opts[q.correct],
              isCorrect: answers[i] === q.correct,
            })),
          },
        }));

        shadow.getElementById("qe-restart").addEventListener("click", () => {
          answers   = {};
          submitted = false;
          render();
        });
        return;
      }

      root.innerHTML = `
        <div class="qe-header">
          <div class="qe-title">${title}</div>
          <div class="qe-sub">${answered} of ${total} answered</div>
          <div class="qe-prog-bg">
            <div class="qe-prog-fill" style="width:${pct}%"></div>
          </div>
        </div>
        ${questions.map((q, i) => `
          <div class="qe-card">
            <div class="qe-q-num">Question ${i + 1}</div>
            <div class="qe-q-text">${q.q}</div>
            <div class="qe-opts">
              ${q.opts.map((o, j) => `
                <div class="qe-opt${answers[i] === j ? " selected" : ""}" data-q="${i}" data-o="${j}">
                  <div class="qe-marker">${LETTERS[j]}</div>
                  <span>${o}</span>
                </div>
              `).join("")}
            </div>
          </div>
        `).join("")}
        <div class="qe-actions">
          <button class="qe-btn qe-btn-primary" id="qe-submit" ${answered < total ? "disabled" : ""}>
            Submit answers
          </button>
        </div>
      `;

      root.querySelectorAll(".qe-opt").forEach(el => {
        el.addEventListener("click", () => {
          if (submitted) return;
          answers[+el.dataset.q] = +el.dataset.o;
          render();
        });
      });

      const submitBtn = shadow.getElementById("qe-submit");
      if (submitBtn) {
        submitBtn.addEventListener("click", () => {
          if (Object.keys(answers).length < total) return;
          submitted = true;
          render();
        });
      }
    }

    render();
  }

  // ─── Public API ────────────────────────────────────────────────────────────
  const QuizEngine = {
    /**
     * Programmatic init.
     * @param {object} config
     * @param {string|HTMLElement} config.target  — CSS selector or DOM element
     * @param {string}            [config.text]   — raw Q&A text to parse
     * @param {Array}             [config.questions] — pre-parsed questions array
     * @param {string}            [config.title]  — quiz heading
     */
    init(config) {
      const target = typeof config.target === "string"
        ? document.querySelector(config.target)
        : config.target;

      if (!target) throw new Error(`QuizEngine: target "${config.target}" not found.`);

      let questions = config.questions;
      if (!questions && config.text) questions = parseQuestions(config.text);
      if (!questions || questions.length === 0) throw new Error("QuizEngine: no questions provided.");

      createInstance({ target, questions, title: config.title || "Quiz" });
    },

    /** Parse raw Q&A text into a questions array (useful for pre-processing). */
    parse: parseQuestions,
  };

  // ─── Auto-init via data attributes ────────────────────────────────────────
  function autoInit() {
    document.querySelectorAll("[data-quiz-engine]").forEach(el => {
      const raw   = el.getAttribute("data-questions") || "";
      const title = el.getAttribute("data-title") || "Quiz";
      if (!raw.trim()) {
        el.innerHTML = `<div class="qe-error">QuizEngine: no questions found in data-questions attribute.</div>`;
        return;
      }
      try {
        const questions = parseQuestions(raw);
        createInstance({ target: el, questions, title });
      } catch (err) {
        el.innerHTML = `<div style="color:#8b2000;font-size:14px;padding:1rem;border:1px solid #f3c5b5;border-radius:8px;background:#fdf0ec">${err.message}</div>`;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoInit);
  } else {
    autoInit();
  }

  global.QuizEngine = QuizEngine;
})(window);
