"""RiRi pilot server. Run from the project root, SINGLE worker (rate/cap state
is in-process):
    uvicorn riri.app:app --host 127.0.0.1 --port 8000

Auth model: instructor-minted random tokens (scripts/make_tokens.py). The token
is the only identity — no usernames, no emails, no PII. Cookie carries the raw
token over HTTPS only; server stores hashes.

Local testing: set RIRI_DEV=1 so the session cookie drops its Secure flag —
otherwise the browser silently refuses to send it over plain http and every
request after login 401s. Never set RIRI_DEV on the VM.

v2: lifespan boot checks (DB, prompt files, provider env) fail at deploy time;
logging added (provider errors were previously swallowed unrecorded); per-IP
login throttle; daily cap check corrected to >=; gate submissions are overlap-
checked against RiRi's own replies and FLAGGED for instructor review, never
auto-rejected.
"""
import logging, os, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, gates, prompts
from .providers import get_provider, est_tokens, ProviderError

log = logging.getLogger("riri")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEV_MODE        = os.environ.get("RIRI_DEV", "") == "1"
DAILY_TOKEN_CAP = int(os.environ.get("RIRI_DAILY_TOKEN_CAP", "60000"))   # per student token
RATE_WINDOW_S   = 60
RATE_MAX_MSGS   = int(os.environ.get("RIRI_RATE_MAX", "10"))             # msgs/min/token
LOGIN_WINDOW_S  = 300
LOGIN_MAX_TRIES = int(os.environ.get("RIRI_LOGIN_MAX", "15"))            # attempts/5min/IP
MAX_MSG_CHARS   = 8000
COOKIE          = "riri_token"

_rate: dict[str, deque] = defaultdict(deque)        # per token-hash; in-memory, single worker
_login_rate: dict[str, deque] = defaultdict(deque)  # per client IP
provider = None                                     # set in lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    global provider
    db.init()
    prompts.validate()          # missing .md fails here, not as a 500 mid-semester
    provider = get_provider()   # missing API key fails here, by name
    log.info("RiRi up: provider=%s dev_mode=%s prompts=%s",
             provider.name, DEV_MODE, prompts.PROMPT_DIR.resolve())
    yield


app = FastAPI(title="RiRi", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------- shared limiters ----------

def _within_rate(q: deque, window_s: float, max_n: int) -> bool:
    now = time.monotonic()
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= max_n:
        return False
    q.append(now)
    return True


def _client_ip(request: Request) -> str:
    # First hop of X-Forwarded-For — trustworthy here because the pilot runs
    # behind the campus reverse proxy; direct exposure would need this hardened.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------- auth ----------

def require_token(request: Request) -> str:
    raw = request.cookies.get(COOKIE, "")
    h = db.token_valid(raw) if raw else None
    if not h:
        raise HTTPException(401, "Not signed in. Enter your access token.")
    return h


class LoginIn(BaseModel):
    token: str = Field(min_length=8, max_length=64)


@app.post("/api/login")
def login(request: Request, body: LoginIn):
    ip = _client_ip(request)
    if not _within_rate(_login_rate[ip], LOGIN_WINDOW_S, LOGIN_MAX_TRIES):
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes.")
    h = db.token_valid(body.token)
    if not h:
        log.warning("login failure from %s", ip)   # never log the token itself
        raise HTTPException(401, "That token isn't recognized. Check the card your instructor gave you.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, body.token.strip().upper(),
                    httponly=True, secure=not DEV_MODE, samesite="strict",
                    max_age=60 * 60 * 24 * 180)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request):
    h = require_token(request)
    return {"ok": True, "usage_today": db.usage_today(h), "cap": DAILY_TOKEN_CAP,
            "provider": provider.name}


# ---------- voice brief ----------

class VoiceBriefIn(BaseModel):
    style: str = Field(max_length=2000)
    register: str = Field(max_length=2000)
    context: str = Field(max_length=2000)
    goals: str = Field(max_length=2000)


@app.post("/api/voice-brief")
def voice_brief(request: Request, body: VoiceBriefIn):
    require_token(request)
    md = prompts.compose_voice_brief(body.model_dump())
    return {"markdown": md}


# ---------- sessions ----------

class SessionIn(BaseModel):
    mode: str
    author_slug: str | None = None
    # Cap matches prompts.MAX_BRIEF_CHARS: reject over-length briefs up front
    # instead of silently truncating (v1 accepted 8000 and truncated at 4000).
    voice_brief_md: str | None = Field(default=None, max_length=prompts.MAX_BRIEF_CHARS)


@app.get("/api/authors")
def authors(request: Request):
    require_token(request)
    return {"authors": prompts.list_authors()}


@app.post("/api/session")
def new_session(request: Request, body: SessionIn):
    h = require_token(request)
    if body.mode not in ("artifact", "assignment"):
        raise HTTPException(422, "mode must be artifact or assignment")
    if body.mode == "artifact":
        if body.author_slug not in {a["slug"] for a in prompts.list_authors()}:
            raise HTTPException(422, "unknown author packet")
        stage = None
    else:
        stage = gates.STAGES[0]
    sid = db.create_session(h, body.mode, body.author_slug, stage, body.voice_brief_md)
    return {"session_id": sid, "mode": body.mode, "stage": stage,
            "author_slug": body.author_slug}


# ---------- chat ----------

class ChatIn(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=MAX_MSG_CHARS)


@app.post("/api/chat")
async def chat(request: Request, body: ChatIn):
    h = require_token(request)
    sess = db.get_session(body.session_id, h)
    if not sess:
        raise HTTPException(404, "Session not found for this token.")
    if not _within_rate(_rate[h], RATE_WINDOW_S, RATE_MAX_MSGS):
        raise HTTPException(429, "You're sending messages faster than the pilot allows. Wait a minute.")
    if db.usage_today(h) >= DAILY_TOKEN_CAP:
        raise HTTPException(429, "Daily usage limit reached for this token. It resets tomorrow.")

    system = prompts.build_system(sess["mode"], sess["stage"],
                                  sess["author_slug"], sess["voice_brief"])
    history = db.get_messages(sess["id"])
    messages = history + [{"role": "user", "content": body.message}]

    try:
        reply = await provider.chat(system, messages)
    except ProviderError as e:
        log.error("provider error session=%s: %s", sess["id"], e)
        raise HTTPException(502, f"The AI provider returned an error. Try again shortly. ({provider.name})")

    db.log_message(sess["id"], "user", body.message)
    db.log_message(sess["id"], "assistant", reply)
    db.add_usage(h, est_tokens(system) + sum(est_tokens(m["content"]) for m in messages)
                 + est_tokens(reply))
    return {"reply": reply, "stage": sess["stage"]}


# ---------- stage gates ----------

class GateIn(BaseModel):
    session_id: str
    submission: str = Field(min_length=1, max_length=12000)


@app.post("/api/gate")
def gate(request: Request, body: GateIn):
    h = require_token(request)
    sess = db.get_session(body.session_id, h)
    if not sess or sess["mode"] != "assignment":
        raise HTTPException(404, "Gate submissions apply to Assignment Mode sessions only.")
    cur = sess["stage"]
    nxt = gates.next_stage(cur)
    if not nxt:
        raise HTTPException(409, "You're already in Polishing — the final stage.")
    ok, why = gates.gate_check(body.submission)
    if not ok:
        raise HTTPException(422, why)
    # Overlap with RiRi's own replies: flag for the instructor's gate-log review,
    # never auto-reject, and don't surface it to the student — substance is a
    # human judgment per the design note in gates.py.
    overlap = gates.assistant_overlap(body.submission, db.assistant_texts(sess["id"]))
    flagged = overlap >= gates.OVERLAP_FLAG_RATIO
    reason = (f"{overlap:.0%} of submission 6-grams match RiRi replies this session"
              if flagged else "")
    db.log_gate(sess["id"], cur, nxt, body.submission, int(flagged), reason)
    if flagged:
        log.info("gate flagged session=%s %s->%s (%s)", sess["id"], cur, nxt, reason)
    return {"advanced": True, "stage": nxt}


# ---------- static ----------

@app.get("/")
def index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")
