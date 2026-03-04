# unbuckbot: Toolforge async mass rollback for Wikimedia Commons

This project provides a Toolforge API backend plus a Commons userscript portlet for asynchronous rollback batches.

## What you get after cloning

- A Toolforge-ready FastAPI backend (`backend/app.py`)
- A default requester whitelist file (`config/requester_policies.json`) containing:
  - `Alachuckthebuck`
- A Toolforge start script (`toolforge/start.sh`) with optional boot-time self-test
- A GitHub Actions CI workflow that runs unit tests
- A Build Service `Procfile` (`Procfile`)
- A deployable env template (`.env.example`)
- A Commons userscript client (`userscript/mass-rollback.user.js`)

## Architecture

1. **Userscript** sends rollback batches to the Toolforge API and includes the currently logged-in requester username.
2. **Toolforge API** authenticates requester via Wikimedia OAuth2 and validates that `requested_by` matches OAuth identity.
3. **Async worker** executes Commons rollbacks using a dedicated **bot account** via `mwapi`.
4. Rollback edits are marked bot edits and include requester attribution in summaries.
5. Jobs can run in `dry_run` mode to validate request flow without performing real rollback actions.

## Bot account execution model

- Rollbacks run with `BOT_USERNAME` + `BOT_PASSWORD`.
- Backend sends `markbot=1` and `bot=1`.
- Default summary contains `requested-by=<username>` unless custom summary is provided.

## Security and authorization

- OAuth start: `GET /api/v1/auth/start`
- OAuth callback: `GET /api/v1/auth/callback`
- Callback validates Commons `userinfo.rights` and requires `rollback` right.
- Session cookie: `HttpOnly`, `Secure`, `SameSite=None`.
- `POST /api/v1/jobs` requires `requested_by` to match authenticated user.
- `POST /api/v1/jobs` accepts `dry_run: true` to simulate rollback execution without editing Commons.

## Whitelist + custom requester rate limits

- Policies are loaded from `REQUESTER_POLICIES_JSON` or `REQUESTER_POLICIES_FILE`.
- Default policy file is `config/requester_policies.json`.
- `WHITELIST_ONLY=1` (default): only listed users can submit jobs.
- `WHITELIST_ONLY=0`: unlisted users use fallback defaults (`DEFAULT_JOBS_PER_MINUTE`, `DEFAULT_MAX_ITEMS_PER_JOB`).

Example policy format:

```json
{
  "Alachuckthebuck": {"jobs_per_minute": 10, "max_items_per_job": 400},
  "AnotherUser": {"jobs_per_minute": 2, "max_items_per_job": 50}
}
```

## API endpoints

- `GET /api/v1/auth/start`
- `GET /api/v1/auth/callback`
- `POST /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`


## Testing

Run tests locally:

```bash
pytest -q
```

CI executes this same command on every push and pull request via `.github/workflows/ci.yml`.

## Toolforge deployment instructions

1. Clone the repo and enter it.
2. Create and fill `.env` from `.env.example`.
3. Ensure OAuth callback in your Wikimedia OAuth consumer is:
   - `https://YOUR-TOOL.toolforge.org/api/v1/auth/callback`
4. Install deps in your environment:
   ```bash
   pip install -r requirements.txt
   ```
5. Optional: enable startup self-test before webserver boot by setting `SELF_TEST_ON_BOOTUP=1`.
6. Start with Toolforge webservice (Kubernetes), for example:
   ```bash
   webservice --backend kubernetes python3.11 start
   webservice shell
   ./toolforge/start.sh
   ```

> If you use Toolforge build/service configs, point your command to `./toolforge/start.sh`.


## Toolforge Build Service instructions

This repository now includes a Build Service `Procfile`:

```
web: ./toolforge/start.sh
```

### Build + deploy flow (recommended)

1. Create your `.env` from `.env.example` and populate secrets.
2. Build and deploy with Toolforge Build Service (from repo root):
   ```bash
   toolforge build start
   webservice --backend kubernetes buildservice start
   ```
3. Verify runtime env vars are available to the app container.
4. Confirm service health by loading your tool URL and OAuth start endpoint.

If you need to update app code, commit changes and rerun:

```bash
toolforge build start
```

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
cp .env.example .env
# edit .env
set -a && source .env && set +a
pip install -r requirements.txt
./toolforge/start.sh
```

## Userscript setup

- Set `TOOL_ENDPOINT` in `userscript/mass-rollback.user.js`.
- Install script on your Commons account.
- Use **Mass rollback** in page actions.
