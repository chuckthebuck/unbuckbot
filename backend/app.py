from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import mwapi
from aiolimiter import AsyncLimiter
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

WIKIMEDIA_AUTH_BASE = "https://meta.wikimedia.org/w/rest.php/oauth2"
COMMONS_HOST = "https://commons.wikimedia.org"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = os.environ.get(
    "TOOL_USER_AGENT",
    "unbuckbot-massrollback/1.0 (https://toolforge.org; bot-assisted rollback tool)",
)


@dataclass
class RequesterPolicy:
    jobs_per_minute: int = 3
    max_items_per_job: int = 200


def _load_requester_policies() -> dict[str, RequesterPolicy]:
    raw = os.environ.get("REQUESTER_POLICIES_JSON", "").strip()
    if not raw:
        policies_file = os.environ.get("REQUESTER_POLICIES_FILE", "config/requester_policies.json")
        if os.path.exists(policies_file):
            with open(policies_file, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()

    if not raw:
        return {}

    parsed = json.loads(raw)
    policies: dict[str, RequesterPolicy] = {}
    for username, value in parsed.items():
        jobs = int(value.get("jobs_per_minute", 3))
        max_items = int(value.get("max_items_per_job", 200))
        policies[username] = RequesterPolicy(jobs_per_minute=max(jobs, 1), max_items_per_job=max(max_items, 1))
    return policies


REQUESTER_POLICIES = _load_requester_policies()
DEFAULT_POLICY = RequesterPolicy(
    jobs_per_minute=max(int(os.environ.get("DEFAULT_JOBS_PER_MINUTE", "2")), 1),
    max_items_per_job=max(int(os.environ.get("DEFAULT_MAX_ITEMS_PER_JOB", "100")), 1),
)
WHITELIST_ONLY = os.environ.get("WHITELIST_ONLY", "1") == "1"


@dataclass
class Session:
    session_id: str
    access_token: str
    username: str
    rights: set[str]
    expires_at: float


@dataclass
class RollbackTask:
    title: str
    user: str
    summary: str | None = None


@dataclass
class RollbackJob:
    id: str
    owner: str
    requested_by: str
    tasks: list[RollbackTask]
    dry_run: bool = False
    status: str = "queued"
    wiki: str = "commonswiki"
    created_at: float = field(default_factory=time.time)
    completed: int = 0
    failed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)


class AppState:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.oauth_states: dict[str, float] = {}
        self.jobs: dict[str, RollbackJob] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.global_limiter = AsyncLimiter(max_rate=6, time_period=1)
        self.per_user_timestamps: dict[str, deque[float]] = defaultdict(deque)
        self.worker_started = False


state = AppState()
app = FastAPI(title="Toolforge Commons Async Mass Rollback")


class RollbackItem(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    user: str = Field(min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=500)


class CreateJobRequest(BaseModel):
    requested_by: str = Field(min_length=1, max_length=255)
    items: list[RollbackItem] = Field(min_length=1, max_length=500)
    dry_run: bool = False


def _requester_policy(username: str) -> RequesterPolicy:
    if username in REQUESTER_POLICIES:
        return REQUESTER_POLICIES[username]
    if WHITELIST_ONLY:
        raise HTTPException(status_code=403, detail="Requester is not whitelisted for this tool")
    return DEFAULT_POLICY


def _bot_credentials() -> tuple[str, str]:
    bot_username = os.environ.get("BOT_USERNAME", "").strip()
    bot_password = os.environ.get("BOT_PASSWORD", "").strip()
    if not bot_username or not bot_password:
        raise RuntimeError("BOT_USERNAME and BOT_PASSWORD are required for bot-account rollback")
    return bot_username, bot_password


async def require_session(request: Request) -> Session:
    sid = request.cookies.get("unbuckbot_session")
    if not sid or sid not in state.sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = state.sessions[sid]
    if session.expires_at <= time.time():
        state.sessions.pop(sid, None)
        raise HTTPException(status_code=401, detail="Session expired")

    if "rollback" not in session.rights:
        raise HTTPException(status_code=403, detail="Missing rollback right on Commons")

    return session


def _oauth_client() -> tuple[str, str, str]:
    client_id = os.environ.get("OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
    callback = os.environ.get("OAUTH_CALLBACK_URL", "")
    if not client_id or not client_secret or not callback:
        raise RuntimeError("Missing OAUTH_CLIENT_ID/OAUTH_CLIENT_SECRET/OAUTH_CALLBACK_URL")
    return client_id, client_secret, callback


async def _fetch_userinfo(access_token: str) -> tuple[str, set[str]]:
    async with httpx.AsyncClient(timeout=30) as client:
        profile_resp = await client.get(
            f"{WIKIMEDIA_AUTH_BASE}/resource/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile_resp.raise_for_status()
        username = profile_resp.json().get("username")
        if not username:
            raise RuntimeError("OAuth profile did not include username")

        rights_resp = await client.get(
            COMMONS_API,
            params={
                "action": "query",
                "meta": "userinfo",
                "uiprop": "rights",
                "format": "json",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        rights_resp.raise_for_status()
        rights = set(rights_resp.json().get("query", {}).get("userinfo", {}).get("rights", []))

    return username, rights


@app.get("/api/v1/auth/start")
async def auth_start() -> RedirectResponse:
    client_id, _, callback = _oauth_client()
    csrf_state = secrets.token_urlsafe(24)
    state.oauth_states[csrf_state] = time.time() + 600
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "scope": "basic",
        "state": csrf_state,
    }
    return RedirectResponse(f"{WIKIMEDIA_AUTH_BASE}/authorize?{urlencode(params)}")


@app.get("/api/v1/auth/callback")
async def auth_callback(code: str, state_token: str | None = None, state: str | None = None) -> JSONResponse:
    incoming_state = state_token or state
    if not incoming_state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")

    expires = state.oauth_states.pop(incoming_state, 0)
    if expires < time.time():
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    client_id, client_secret, callback = _oauth_client()
    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(
            f"{WIKIMEDIA_AUTH_BASE}/access_token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    username, rights = await _fetch_userinfo(access_token)
    if "rollback" not in rights:
        raise HTTPException(status_code=403, detail="Account does not have rollback right on Commons")

    sid = secrets.token_urlsafe(32)
    state.sessions[sid] = Session(
        session_id=sid,
        access_token=access_token,
        username=username,
        rights=rights,
        expires_at=time.time() + min(expires_in, 3600),
    )

    response = JSONResponse({"ok": True, "username": username, "wiki": "commonswiki"})
    response.set_cookie(
        "unbuckbot_session",
        sid,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=min(expires_in, 3600),
    )
    return response


@app.post("/api/v1/jobs")
async def create_job(payload: CreateJobRequest, session: Session = Depends(require_session)) -> dict[str, str]:
    if payload.requested_by != session.username:
        raise HTTPException(status_code=403, detail="requested_by must match authenticated user")

    policy = _requester_policy(session.username)
    if len(payload.items) > policy.max_items_per_job:
        raise HTTPException(status_code=400, detail=f"Too many items for requester policy ({policy.max_items_per_job})")

    now = time.time()
    recent = state.per_user_timestamps[session.username]
    while recent and recent[0] < now - 60:
        recent.popleft()
    if len(recent) >= policy.jobs_per_minute:
        raise HTTPException(status_code=429, detail=f"Submission throttled: max {policy.jobs_per_minute} jobs per minute")
    recent.append(now)

    job_id = str(uuid.uuid4())
    job = RollbackJob(
        id=job_id,
        owner=session.username,
        requested_by=payload.requested_by,
        tasks=[RollbackTask(title=i.title, user=i.user, summary=i.summary) for i in payload.items],
        dry_run=payload.dry_run,
    )
    state.jobs[job_id] = job
    await state.queue.put(job_id)
    return {"job_id": job_id}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str, session: Session = Depends(require_session)) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.owner != session.username:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "id": job.id,
        "wiki": job.wiki,
        "owner": job.owner,
        "requested_by": job.requested_by,
        "dry_run": job.dry_run,
        "status": job.status,
        "total": len(job.tasks),
        "completed": job.completed,
        "failed": job.failed,
        "results": job.results[-100:],
    }


def _bot_session() -> mwapi.Session:
    bot_username, bot_password = _bot_credentials()
    wiki = mwapi.Session(COMMONS_HOST, user_agent=USER_AGENT)
    wiki.login(bot_username, bot_password)
    return wiki


def _commons_rollback_with_bot(requested_by: str, task: RollbackTask) -> dict[str, Any]:
    wiki = _bot_session()
    token_data = wiki.get(action="query", meta="tokens", type="rollback")
    rollback_token = token_data["query"]["tokens"]["rollbacktoken"]

    result = wiki.post(
        action="rollback",
        title=task.title,
        user=task.user,
        token=rollback_token,
        summary=(
            task.summary
            or f"Mass rollback via Toolforge bot; requested-by={requested_by}"
        ),
        markbot=1,
        bot=1,
    )

    if "error" in result:
        return {"ok": False, "title": task.title, "requested_by": requested_by, "error": result["error"]}
    return {
        "ok": True,
        "title": task.title,
        "requested_by": requested_by,
        "result": result.get("rollback", {}),
    }


async def _rollback_one(requested_by: str, task: RollbackTask) -> dict[str, Any]:
    return await asyncio.to_thread(_commons_rollback_with_bot, requested_by, task)


def _dry_run_result(requested_by: str, task: RollbackTask) -> dict[str, Any]:
    return {
        "ok": True,
        "title": task.title,
        "requested_by": requested_by,
        "dry_run": True,
        "result": {
            "simulated": True,
            "user": task.user,
            "summary": task.summary or f"Mass rollback via Toolforge bot; requested-by={requested_by}",
        },
    }


async def job_worker() -> None:
    while True:
        job_id = await state.queue.get()
        job = state.jobs.get(job_id)
        if not job:
            continue

        owner_session = next((s for s in state.sessions.values() if s.username == job.owner), None)
        if not owner_session:
            job.status = "failed"
            job.failed = len(job.tasks)
            job.results.append({"ok": False, "error": "Owner session expired before execution"})
            continue

        if "rollback" not in owner_session.rights:
            job.status = "failed"
            job.failed = len(job.tasks)
            job.results.append({"ok": False, "error": "Owner no longer has Commons rollback rights"})
            continue

        job.status = "running"
        for task in job.tasks:
            async with state.global_limiter:
                try:
                    item_result = _dry_run_result(job.requested_by, task) if job.dry_run else await _rollback_one(job.requested_by, task)
                except Exception as exc:  # noqa: BLE001
                    item_result = {
                        "ok": False,
                        "title": task.title,
                        "requested_by": job.requested_by,
                        "error": str(exc),
                    }

            job.results.append(item_result)
            if item_result.get("ok"):
                job.completed += 1
            else:
                job.failed += 1

        job.status = "completed"


@app.on_event("startup")
async def startup_event() -> None:
    if not state.worker_started:
        asyncio.create_task(job_worker())
        state.worker_started = True
