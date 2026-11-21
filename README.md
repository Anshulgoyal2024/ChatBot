# LLM Chatbot with Key Failover and Response Caching

React + FastAPI + SQLite chatbot. LiteLLM provides the key pool with automatic failover and the response cache. Every reply shows its source, model, latency and per-request token usage.

## 1. Live URLs

- Frontend: _add the deployed frontend URL before submitting_
- Backend: _add the deployed backend URL before submitting_ (`GET /health` should return `{"status":"ok",...}`)
- Repository: _add the public GitHub URL_
- Demo video: _add a public 2–3 minute demo video link_

Set `ENABLE_FAILURE_SIMULATION=true` and `ALLOWED_ORIGINS=<frontend URL>` on the deployed backend, and build the frontend with `VITE_API_BASE_URL=<backend URL>`.

## 2. Run locally: with Docker and without

### With Docker

```bash
cp .env.example .env     # put at least 2 real keys in LLM_API_KEYS
make up                  # docker compose up --build -d
```

- Backend: http://localhost:8000 (`/health`, `/chat`, `/sessions/{id}/messages`)
- Frontend: http://localhost:3000 by default (`FRONTEND_PORT=3001` if that port is occupied)
- SQLite lives on the named volume `sqlite_data`, mounted at `/data` (`DATABASE_URL=sqlite:////data/app.db`), so history survives restarts.
- `FRONTEND_PORT` defaults to `3000` for the assignment contract; set it to `3001` if another local service already uses port 3000.
- `make down` stops the stack. `make test` runs the backend tests.

For a Render Docker deployment, set `DATABASE_URL=sqlite:////data/app.db` and mount a persistent disk at `/data` if session history must survive deploys. The image also makes `/srv/data` writable for deployments that currently use the relative `sqlite:///./data/app.db` URL.
The backend image binds Uvicorn to Render's `PORT` environment variable (default `10000` there, `8000` locally) on `0.0.0.0`.

### Without Docker

```bash
# backend (Python 3.11+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_API_KEYS="key-one,key-two" LLM_MODEL="gemini/gemini-3.6-flash" \
       DATABASE_URL="sqlite:///./data/app.db" ALLOWED_ORIGINS="http://localhost:3001" \
       ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --port 8000

# frontend (Node 20+), in a second terminal. Vite proxies API calls to port 8000.
cd frontend
npm install
npm run dev                                              # http://localhost:3001
```

### Tests

```bash
make test        # or: cd backend && pytest -v
```

The tests mock the provider (LiteLLM `mock_response`) and use a temporary SQLite file. The real LiteLLM cache layer stays active, so cache hits and misses are genuinely exercised.

### Environment variables

| Variable | Meaning |
| --- | --- |
| `LLM_API_KEYS` | Comma-separated key pool. Ids are `key_1`, `key_2`, ... in this order. |
| `LLM_MODEL` | LiteLLM model name (default `gemini/gemini-3.6-flash`). |
| `DATABASE_URL` | SQLite location, e.g. `sqlite:////data/app.db`. Parent folders and tables are created on startup. |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins. |
| `ENABLE_FAILURE_SIMULATION` | `true` enables the `X-Simulate-Failure` hook. |
| `VITE_API_BASE_URL` | Optional API URL override. Leave blank to use the same-origin frontend proxy. |
| `LLM_REQUEST_TIMEOUT` | Optional. Per-key provider timeout in seconds (default 30). |
| `DEFAULT_SYSTEM_PROMPT` | Optional. Used when a session has no system prompt. |
| `FRONTEND_PORT` | Host port for the Docker frontend (default `3000`). |

## 3. Design decisions: key fallback, caching, SQLite session storage

**Key fallback.** `LLM_API_KEYS` becomes an ordered pool. For each request the gateway tries `key_1`, then `key_2`, and so on, calling `litellm.acompletion` with one explicit `api_key` and `num_retries=0`, so a failing key is abandoned immediately instead of being retried. Any provider failure (429, quota, 5xx, timeout, connection error, empty completion) moves to the next key and increments `fallback_attempts`. When every key fails the API returns `503 {"detail":"all providers unavailable"}`. Only exception class names are logged, never keys or stack traces, and nothing is written to SQLite for a failed request.

**Failure simulation.** With `ENABLE_FAILURE_SIMULATION=true`, `X-Simulate-Failure` (`key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503) marks keys as failed for that single request. The provider is not called for a simulated key. A specific key id wins over `*`. A simulated request also skips the cache read so the failover path is always demonstrable, even for a message that was answered before.

**Caching.** LiteLLM's built-in in-memory cache (`Cache(type="local")`) is enabled globally. By default LiteLLM includes `api_key` and other call parameters in the cache key, which would make the same prompt miss whenever a different key answers. `ModelMessagesCache` overrides `get_cache_key` so the key is a SHA-256 of **model + the full message list** (system prompt + history + new message). The same first message with the same system prompt in a new session is therefore a hit, while a different message, history or system prompt is always a miss. On a hit the reply and token counts come from the stored response, `key_id` is `null` and `fallback_attempts` is `0`. The cache layer is warmed at startup so the first hit is not slowed by lazy imports.

**SQLite session storage.** Python's built-in `sqlite3` only. `messages` follows the specified schema (plus an index on `session_id, id`); a small `sessions` table stores the per-session system prompt. The client sends only the new message; the backend rebuilds `system + stored history + new message` for every call. The user and assistant rows are written in one transaction after a successful answer, so a 422 or 503 leaves no trace. The system prompt is never returned by the history endpoint.

**Errors.** Handlers for validation errors, HTTP exceptions and unexpected exceptions guarantee that every error is JSON with a `detail` key, never HTML or a stack trace.

## 4. Verification: how to see failover, cache hits and per-message token usage

Replace `$API` with the backend URL (e.g. `http://localhost:8000`).

**Failover**

```bash
# key_1 fails, key_2 answers -> 200, key_id "key_2", fallback_attempts 1
curl -s $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo-1","message":"Name three rivers in Europe."}'

# both keys fail -> 503 {"detail":"all providers unavailable"}, nothing stored
curl -s -i $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"demo-2","message":"Hello"}'
curl -s -i $API/sessions/demo-2/messages    # 404 session not found
```

In the UI, type `key_1=429` into **Failover test** and send a message. The reply's key shows `key_2` and fallbacks shows `1`.

**Cache hit**

```bash
curl -s $API/chat -H 'Content-Type: application/json' -d '{"session_id":"a-1","message":"Tell me a fun fact about octopuses."}'
curl -s $API/chat -H 'Content-Type: application/json' -d '{"session_id":"a-2","message":"Tell me a fun fact about octopuses."}'
```

The second call returns `"resolution_source":"LLM_CACHE"`, `key_id: null`, `fallback_attempts: 0`, the identical `reply`, the same token counts and a lower `latency_ms`. In the UI, click **New Session** and send the same first message again.

**Token usage.** Each reply carries `input_tokens` (system + history + new message) and `output_tokens` for that request only, never a running total. The UI shows them next to every reply (`meta-input-tokens`, `meta-output-tokens`), and `GET /sessions/{id}/messages` returns them in each assistant message's `metadata`.

**Multi-turn.** Send "My name is Priya", then "What is my name?" in the same session. Open a new session and ask again to see that sessions stay separate.

## 5. Setup Used: your editor and AI tools

- AI assistant: OpenAI Codex assisted with implementation and verification.
- Editor: add the editor you used before submission.
- Tooling: Python 3.11, FastAPI, LiteLLM, pytest, Node 20, Vite, React, Docker Compose.

## 6. Bonus features (if any): what you added beyond the requirements and why

- **Failover test field in the UI.** Sets the `X-Simulate-Failure` header so failover can be shown on camera without curl.
- **Key and fallback cells.** Each reply also shows which key answered and how many keys failed first, which makes failover and cache behaviour visible.
- **Cache-key override.** The cache ignores `api_key`, so cached answers are shared across the whole pool.
- **Non-root, health-checked backend image.** Multi-stage build, `HEALTHCHECK` on `/health`.
- **Broad test suite.** Covers key-independent caching, real LiteLLM errors (429, 503, timeout, connection), CORS, restart persistence and zero-write guarantees.
