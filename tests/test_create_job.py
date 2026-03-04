import asyncio
import time

from fastapi.testclient import TestClient

import backend.app as backend
from backend.app import RequesterPolicy, RollbackJob, RollbackTask, Session, app

client = TestClient(app)


def _login_cookie(username: str = "CommonsRollbacker", rights: set[str] | None = None) -> str:
    sid = f"sess-{username}-{int(time.time() * 1000)}"
    backend.state.sessions[sid] = Session(
        session_id=sid,
        access_token="oauth-token",
        username=username,
        rights=rights or {"rollback"},
        expires_at=time.time() + 60,
    )
    return sid


def test_create_job_success_whitelisted_requester():
    backend.REQUESTER_POLICIES = {"CommonsRollbacker": RequesterPolicy(jobs_per_minute=3, max_items_per_job=10)}

    sid = _login_cookie()
    response = client.post(
        "/api/v1/jobs",
        cookies={"unbuckbot_session": sid},
        json={
            "requested_by": "CommonsRollbacker",
            "items": [{"title": "File:Sandbox.jpg", "user": "Vandal"}],
        },
    )
    assert response.status_code == 200
    assert "job_id" in response.json()


def test_create_job_rejects_mismatched_requester():
    backend.REQUESTER_POLICIES = {"CommonsRollbacker": RequesterPolicy(jobs_per_minute=3, max_items_per_job=10)}

    sid = _login_cookie()
    response = client.post(
        "/api/v1/jobs",
        cookies={"unbuckbot_session": sid},
        json={
            "requested_by": "Imposter",
            "items": [{"title": "File:Sandbox.jpg", "user": "Vandal"}],
        },
    )
    assert response.status_code == 403


def test_create_job_rate_limited_by_custom_policy():
    backend.REQUESTER_POLICIES = {"BurstUser": RequesterPolicy(jobs_per_minute=2, max_items_per_job=10)}

    sid = _login_cookie("BurstUser")
    payload = {
        "requested_by": "BurstUser",
        "items": [{"title": "File:Sandbox.jpg", "user": "Vandal"}],
    }

    for _ in range(2):
        response = client.post("/api/v1/jobs", cookies={"unbuckbot_session": sid}, json=payload)
        assert response.status_code == 200

    blocked = client.post("/api/v1/jobs", cookies={"unbuckbot_session": sid}, json=payload)
    assert blocked.status_code == 429


def test_create_job_rejects_non_whitelisted_when_whitelist_only():
    backend.REQUESTER_POLICIES = {"SomebodyElse": RequesterPolicy(jobs_per_minute=3, max_items_per_job=10)}

    sid = _login_cookie("NotListed")
    payload = {
        "requested_by": "NotListed",
        "items": [{"title": "File:Sandbox.jpg", "user": "Vandal"}],
    }
    response = client.post("/api/v1/jobs", cookies={"unbuckbot_session": sid}, json=payload)
    assert response.status_code == 403


def test_load_requester_policies_from_file(tmp_path, monkeypatch):
    policy_file = tmp_path / "policies.json"
    policy_file.write_text('{"Alachuckthebuck": {"jobs_per_minute": 7, "max_items_per_job": 77}}', encoding="utf-8")

    monkeypatch.setenv("REQUESTER_POLICIES_JSON", "")
    monkeypatch.setenv("REQUESTER_POLICIES_FILE", str(policy_file))

    loaded = backend._load_requester_policies()
    assert "Alachuckthebuck" in loaded
    assert loaded["Alachuckthebuck"].jobs_per_minute == 7
    assert loaded["Alachuckthebuck"].max_items_per_job == 77


def test_create_job_with_dry_run_exposes_flag_on_get_job():
    backend.REQUESTER_POLICIES = {"CommonsRollbacker": RequesterPolicy(jobs_per_minute=3, max_items_per_job=10)}

    sid = _login_cookie()
    response = client.post(
        "/api/v1/jobs",
        cookies={"unbuckbot_session": sid},
        json={
            "requested_by": "CommonsRollbacker",
            "dry_run": True,
            "items": [{"title": "File:Sandbox.jpg", "user": "Vandal"}],
        },
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    fetched = client.get(f"/api/v1/jobs/{job_id}", cookies={"unbuckbot_session": sid})
    assert fetched.status_code == 200
    assert fetched.json()["dry_run"] is True


def test_job_worker_dry_run_completes_without_bot_calls():
    backend.state.sessions["session-1"] = Session(
        session_id="session-1",
        access_token="oauth-token",
        username="CommonsRollbacker",
        rights={"rollback"},
        expires_at=time.time() + 60,
    )
    job = RollbackJob(
        id="job-1",
        owner="CommonsRollbacker",
        requested_by="CommonsRollbacker",
        tasks=[RollbackTask(title="File:Sandbox.jpg", user="Vandal")],
        dry_run=True,
    )
    backend.state.jobs[job.id] = job

    async def _run_worker_once():
        worker_task = asyncio.create_task(backend.job_worker())
        await backend.state.queue.put(job.id)
        for _ in range(30):
            if backend.state.jobs[job.id].status == "completed":
                break
            await asyncio.sleep(0.01)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_worker_once())
    assert backend.state.jobs[job.id].completed == 1
    assert backend.state.jobs[job.id].failed == 0
    assert backend.state.jobs[job.id].results[0]["dry_run"] is True
