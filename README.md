# Local Lyftr chatbot

React + FastAPI + SQLite, launched from VS Code. The frontend runs at `http://localhost:3001`, the backend at `http://127.0.0.1:8000`, and SQLite data stays in `backend/data/app.db`.

The application and database run on your machine. Chat completions are sent to the configured OpenAI API, which requires an internet connection and a valid API key.

## 1. Set up in VS Code (Windows)

Open this repository folder in VS Code and use its integrated PowerShell terminal.

One-time backend setup:

```powershell
cd backend
if (-not (Test-Path .\Scripts\python.exe)) { py -3 -m venv . }
.\Scripts\python.exe -m pip install -r requirements.txt
cd ..\frontend
npm ci
cd ..
```

Keep `LLM_API_KEYS` in the repository-root `.env` file. Put the two provider keys there as a comma-separated value if you need the two-key failover demo. Do not commit or share that file. The frontend has no remote API URL setting; it always uses the local backend proxy.

Start both local services from VS Code: press `Ctrl+Shift+P`, choose **Tasks: Run Task**, then choose **Run local app**. VS Code opens one integrated terminal for FastAPI and one for Vite. Open `http://localhost:3001`; `/health` is available at `http://127.0.0.1:8000/health`.

You can start them manually in two VS Code terminals instead:

```powershell
# Terminal 1, from the repository root
cd backend
.\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
# Terminal 2, from the repository root
cd frontend
npm run dev
```

## 2. Local configuration

| Variable | Purpose |
| --- | --- |
| `LLM_API_KEYS` | Comma-separated OpenAI API keys; IDs are `key_1`, `key_2`, and so on. |
| `LLM_MODEL` | LiteLLM model name; default is `openai/gpt-4o-mini`. |
| `DATABASE_URL` | Local SQLite path; use `sqlite:///./data/app.db` for development. |
| `ALLOWED_ORIGINS` | Local browser origin, normally `http://localhost:3001`. |
| `ENABLE_FAILURE_SIMULATION` | Set to `true` to use the failover demo field. |
| `LLM_REQUEST_TIMEOUT` | Provider timeout in seconds. |

The browser only calls its own local origin. Vite forwards `/chat`, `/health`, and `/sessions` to `127.0.0.1:8000`. The Vite and Docker port bindings use loopback addresses, so the app is not exposed to your network.

Run the backend suite with `cd backend; .\Scripts\python.exe -m pytest -v` when you want to check it.
## 3. Design decisions: key fallback, caching, SQLite session storage

**Key fallback.** `LLM_API_KEYS` becomes an ordered pool. For each request the gateway tries `key_1`, then `key_2`, and so on, calling `litellm.acompletion` with one explicit `api_key` and `num_retries=0`, so a failing key is abandoned immediately instead of being retried. Any provider failure (429, quota, 5xx, timeout, connection error, empty completion) moves to the next key and increments `fallback_attempts`. If every configured key is rate-limited, the API returns `429`; other exhausted provider failures return `503`. Only exception class names are logged, never keys or stack traces, and nothing is written to SQLite for a failed request.
Provider keys in the same organization or project may share rate and spend limits; adding key IDs does not increase those limits.

**Failure simulation.** With `ENABLE_FAILURE_SIMULATION=true`, `X-Simulate-Failure` (`key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503) marks keys as failed for that single request. The provider is not called for a simulated key. A specific key id wins over `*`. A simulated request also skips the cache read so the failover path is always demonstrable, even for a message that was answered before.

**Caching.** LiteLLM's built-in in-memory cache (`Cache(type="local")`) is enabled globally. By default LiteLLM includes `api_key` and other call parameters in the cache key, which would make the same prompt miss whenever a different key answers. `ModelMessagesCache` overrides `get_cache_key` so the key is a SHA-256 of **model + the full message list** (system prompt + history + new message). The same first message with the same system prompt in a new session is therefore a hit, while a different message, history or system prompt is always a miss. On a hit the reply and token counts come from the stored response, `key_id` is `null` and `fallback_attempts` is `0`. The cache layer is warmed at startup so the first hit is not slowed by lazy imports.

**SQLite session storage.** Python's built-in `sqlite3` only. `messages` follows the specified schema (plus an index on `session_id, id`); a small `sessions` table stores the per-session system prompt. The client sends only the new message; the backend rebuilds `system + stored history + new message` for every call. The user and assistant rows are written in one transaction after a successful answer, so a validation or provider failure leaves no trace. The system prompt is never returned by the history endpoint.

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
