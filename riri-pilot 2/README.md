# RiRi pilot — dual-mode rhetorical instrument (ENG110)

FastAPI + SQLite + one static page. Token auth, server-side stage gates,
provider adapter (Anthropic / Gemini), anonymized logging, retention purge.

## Local dev
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...   # or set RIRI_PROVIDER=gemini + GEMINI_API_KEY
    python scripts/make_tokens.py 3 --label dev
    uvicorn riri.app:app --reload
    # open http://127.0.0.1:8000, sign in with a printed token
Note: cookies are set `secure=true`; for plain-HTTP local testing, temporarily
flip that flag in app.py or test through Caddy locally.

## VM deploy (once IT provisions DNS + 80/443)
    sudo useradd -r -m -d /opt/riri riri
    # copy repo to /opt/riri, then as riri user: venv + pip install as above
    cp .env.example /opt/riri/.env && chmod 600 /opt/riri/.env   # add real keys
    sudo cp deploy/riri.service /etc/systemd/system/ && sudo systemctl enable --now riri
    sudo apt install caddy && sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
    # cron (as riri): 15 3 * * * cd /opt/riri && venv/bin/python scripts/purge.py

## The prompt layer (where the pedagogy lives)

Composed in this order by `riri/prompts.py`, joined with `---`:

| Layer | File | Governs |
|---|---|---|
| 1 | `base_system.md` | Identity, hard refusals, AI disclosure, course vocabulary discipline |
| 2 | `editorial_stance.md` | *Manner* — where standards come from, anti-cliché, leap tolerance, register, the diagnosis/prescription line |
| 3 | `assignment_<stage>.md` or `artifact_mode.md` + author packet | *Task* for this stage or mode |
| 4 | Voice Brief, or `NO_BRIEF_NOTE` | The student's own stated standard, last so it stays salient |

Two rules that make this more than a stack of instructions:

- **No house style.** RiRi carries no default idea of good academic prose. The
  Voice Brief supplies the stylistic standard; the ENG110 course lens is the
  fallback and gets *named as the course's lens*, not as a rule of writing. Where
  the two conflict, the Brief wins on style — the course lens holds only on
  argument (a claim needs evidence and a warrant in any register).
- **Diagnosis, never prescription.** A professional editor proposes alternative
  lines. RiRi quotes the student's sentence, names what it does, names where it
  slips, and asks the question that fixes it. This is the pedagogical translation
  of the instructor's own micro-revision prompt: peer-to-peer editing donates
  lines, graded coursework can't.

Polishing carries two distinct outputs — **MACRO AUDIT** (roadmap in
move-language, Bitzer constituent audit, Claim/Measure/Method, ADVANCE vs CIRCLE
BACK, evidence/warrant flags, redundancy diagnosis, one revision brief) and
**MICRO READING** (diagnosis, precision points, line-level interventions,
openings). RiRi routes on what the student submits and asks when ambiguous.

## Operating it
- **Tokens**: `python scripts/make_tokens.py 30 --label fall26-sec01` → print,
  cut into cards, hand out. Lost token → mint a replacement, note attrition.
- **Author packets**: fill `prompts/authors/*.md` from `_TEMPLATE.md`. The
  packet is the product; the code just performs it.
- **All prompt text** is in `prompts/` — edit and restart, no code changes.
- **Gate logs**: `sqlite3 riri.db "SELECT * FROM gate_submissions"` is your
  documented-student-input evidence for the study.

## Deliberately not built (pilot discipline)
- No admin web UI — sqlite3 CLI is the admin UI at this scale.
- No output classifier policing draft leakage — prompt-layer refusal + your
  reading of session logs. Add detection only if logs show it's needed.
- No automated Voice Brief parsing into structured fields. The Brief goes to the
  model as prose. Schematizing it would let you enforce rules mechanically and
  would also flatten exactly what it exists to preserve.
- No cliché blocklist in code. Anti-cliché lives in the prompt as judgment, not
  in a regex. A regex catches "delves into" and misses empty sophistication.
- No streaming responses, no websockets — request/response is fine at 150-word
  reply lengths.
- No Postgres, no Redis, no Docker — one VM, one process, one file DB.

## Threat model honesty
The stage gates stop the *workflow* of draft extraction, not a determined
adversary with the open internet. That is the correct claim for the study:
RiRi structures process for students who engage; it does not DRM cognition.
