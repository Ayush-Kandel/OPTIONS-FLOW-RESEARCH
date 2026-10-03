"""Incident cases: gather the evidence for a problem, let a local LLM (Qwen on this PC - no cloud
credits) investigate it, then record which of its conclusions code/data actually confirmed.
Cases live in the `cases` table of flow.db and as readable markdown in cases/."""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pipeline as pl

CASE_MODEL = "qwen3:14b"
CASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id           INTEGER PRIMARY KEY,
    opened_utc   TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,      -- 'open', 'investigated', 'closed'
    evidence     TEXT NOT NULL,
    llm_model    TEXT,
    llm_findings TEXT,               -- the LLM's JSON answer, unedited
    verified     TEXT,               -- what was checked against the data afterwards
    closed_utc   TEXT
)"""

# What the TWS / socket codes seen in our logs mean (from the IBKR API documentation)
CODE_GLOSSARY = """\
IBKR API message codes:
- 162: historical data service error; 'HMDS query returned no data' = no bars exist for that request
- 200: no security definition found for the requested contract
- 438: TWS message 'The application is now locked' (TWS itself refused the API session)
- 1100: connectivity between TWS and IBKR's servers lost; 1101/1102: restored
- 10091: part of the market data needs an extra subscription (the option quotes still arrive)
- 'clientId N already in use?': ib_async's guess whenever TWS closes the socket during the handshake
Windows socket codes: WinError 10054 = connection forcibly closed by the remote side;
WinError 1225 / ConnectionRefusedError = nothing is listening on the API port (TWS closed or restarting)."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db):
    db.execute(SCHEMA)
    db.commit()


def open_case(db, title, evidence):
    ensure_schema(db)
    cur = db.execute("INSERT INTO cases (opened_utc, title, status, evidence) VALUES (?, ?, 'open', ?)",
                     (_now(), title, evidence))
    db.commit()
    return cur.lastrowid


def log_lines(path, pattern, max_lines=40, after_line=0):
    """Lines of a log file matching a regex, with their line numbers (evidence for a case)."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if n > after_line and re.search(pattern, line):
                out.append(f"{os.path.basename(path)}:{n}: {line.rstrip()[:220]}")
    return "\n".join(out[-max_lines:])


def investigate(db, case_id, model=CASE_MODEL):
    """Ask the local LLM for the root cause of each problem in the evidence. Returns its JSON."""
    title, evidence = db.execute("SELECT title, evidence FROM cases WHERE id = ?", (case_id,)).fetchone()
    prompt = (
        "You are the incident investigator for a local options-flow research system. It pulls flow "
        "alerts from Trade Echo (credit-limited API), prices option contracts with Interactive Brokers "
        "TWS (read-only market data API on this PC), and runs a nightly job that prices picks, then "
        "retrains a model.\n\n"
        f"CASE: {title}\n\n{CODE_GLOSSARY}\n\nEVIDENCE:\n{evidence}\n\n"
        "Investigate. Use ONLY the evidence above; where it does not show the cause, say the cause is "
        "unknown instead of guessing. Group the evidence into distinct problems. For each give: when it "
        "happened, what happened, the root cause, the impact on the data or the model, the fix, your "
        "confidence (high/medium/low), and the exact evidence lines that support it. Also list anything "
        "in the evidence that is normal and NOT a problem.\n"
        'Reply with JSON only: {"problems": [{"title": "", "when": "", "what_happened": "", '
        '"root_cause": "", "impact": "", "fix": "", "confidence": "", "evidence": [""]}], '
        '"not_problems": [""]}')
    body = json.dumps({"model": model, "stream": False, "format": "json", "think": True, "keep_alive": -1,
                       "options": {"temperature": 0, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(pl.OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as resp:
        answer = json.loads(json.loads(resp.read().decode("utf-8"))["message"]["content"])
    db.execute("UPDATE cases SET status = 'investigated', llm_model = ?, llm_findings = ? WHERE id = ?",
               (model, json.dumps(answer, indent=1), case_id))
    db.commit()
    return answer


def record_verification(db, case_id, text, close=True):
    db.execute("UPDATE cases SET verified = ?, status = ?, closed_utc = ? WHERE id = ?",
               (text, "closed" if close else "investigated", _now() if close else None, case_id))
    db.commit()


def write_markdown(db, case_id):
    row = db.execute("SELECT opened_utc, title, status, evidence, llm_model, llm_findings, verified, closed_utc "
                     "FROM cases WHERE id = ?", (case_id,)).fetchone()
    opened, title, status, evidence, model, findings, verified, closed = row
    lines = [f"# Case {case_id:04d}: {title}", "",
             f"Opened {opened} - status **{status}**" + (f", closed {closed}" if closed else ""), ""]
    if findings:
        f = json.loads(findings)
        lines += [f"## Local LLM investigation ({model})", ""]
        for i, p in enumerate(f.get("problems") or [], 1):
            lines += [f"### {i}. {p.get('title', '')}",
                      f"- **When:** {p.get('when', '')}",
                      f"- **What happened:** {p.get('what_happened', '')}",
                      f"- **Root cause:** {p.get('root_cause', '')}",
                      f"- **Impact:** {p.get('impact', '')}",
                      f"- **Fix:** {p.get('fix', '')}",
                      f"- **Confidence:** {p.get('confidence', '')}", ""]
        if f.get("not_problems"):
            lines += ["**Normal, not problems:**"] + [f"- {x}" for x in f["not_problems"]] + [""]
    if verified:
        lines += ["## Verified against the data", "", verified, ""]
    lines += ["## Evidence", "", "```", evidence, "```", ""]
    os.makedirs(CASE_DIR, exist_ok=True)
    path = os.path.join(CASE_DIR, f"case-{case_id:04d}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def post_summary(db, case_id, summary_lines):
    title, status = db.execute("SELECT title, status FROM cases WHERE id = ?", (case_id,)).fetchone()
    pl.discord_send(f"🗂️ **Case {case_id:04d}: {title}** ({status})\n" + "\n".join(summary_lines)
                    + f"\nFull write-up: cases/case-{case_id:04d}.md", bot="auditor")
