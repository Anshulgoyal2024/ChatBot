"""API tests: LiteLLM is mocked at the provider seam (`mock_response`), the DB is an isolated temp file.

The mocked provider still runs inside `litellm.acompletion`, so LiteLLM's real cache layer (with the
custom model+messages key) is what decides LLM_CACHE vs LIVE_INFERENCE.
"""
import sqlite3
import json

import litellm
import pytest
from fastapi.testclient import TestClient

from app import llm_gateway
from app.config import Settings
from app.main import create_app

SIMULATED_DELAY = 0.25  # makes a live call measurably slower than a cache hit


class FakeProvider:
    """Replacement for the provider seam. Replies are unique per live call; failing keys raise real LiteLLM errors."""

    def __init__(self):
        self.calls = 0
        self.keys_seen = []
        self.completed_prompts = set()
        self.live_completions = 0
        self.failing = {}  # api_key -> exception factory

    async def __call__(self, **kwargs):
        self.calls += 1
        api_key = kwargs.get("api_key")
        if api_key == "warmup":
            return await litellm.acompletion(**kwargs)
        self.keys_seen.append(api_key)
        if api_key in self.failing:
            raise self.failing[api_key]()
        prompt_key = json.dumps(
            {"model": kwargs.get("model"), "messages": kwargs.get("messages")},
            sort_keys=True,
        )
        is_new_prompt = prompt_key not in self.completed_prompts
        self.completed_prompts.add(prompt_key)
        kwargs["mock_response"] = f"mock reply #{self.calls} to: {kwargs['messages'][-1]['content']}"
        # Simulate provider latency only for a cache miss. LiteLLM applies mock_delay
        # before it checks its cache, which would make cached replies look slower too.
        kwargs["mock_delay"] = SIMULATED_DELAY if is_new_prompt else 0
        response = await litellm.acompletion(**kwargs)
        hidden = getattr(response, "_hidden_params", None) or {}
        if not hidden.get("cache_hit"):
            self.live_completions += 1
        return response


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(llm_gateway, "_provider_acompletion", fake)
    return fake


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "nested" / "dir" / "app.db"


@pytest.fixture
def make_client(db_path, provider):
    clients = []

    def _make(keys=("key-aaa", "key-bbb"), simulation=True):
        settings = Settings(
            llm_api_keys=tuple(keys),
            llm_model="gemini/gemini-3.6-flash",
            database_url=f"sqlite:///{db_path}",
            allowed_origins=("http://localhost:3000",),
            enable_failure_simulation=simulation,
        )
        client = TestClient(create_app(settings), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def message_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


def test_health(make_client, db_path):
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}
    assert db_path.exists()  # directories and file created on startup


def test_chat_contract_and_history(make_client):
    client = make_client()
    first = chat(client, "s-1", "What is the capital of France?")
    assert first.status_code == 200
    body = first.json()
    assert body["session_id"] == "s-1"
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-3.6-flash"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] > 0
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0

    second = chat(client, "s-1", "And Germany?")
    assert second.status_code == 200

    history = client.get("/sessions/s-1/messages")
    assert history.status_code == 200
    data = history.json()
    assert data["session_id"] == "s-1"
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert data["messages"][0]["content"] == "What is the capital of France?"
    assert "metadata" not in data["messages"][0]
    assert data["messages"][1]["content"] == body["reply"]
    assert {"resolution_source", "input_tokens", "output_tokens"} <= set(data["messages"][1]["metadata"])
    assert all(m["role"] != "system" for m in data["messages"])


def test_history_is_sent_to_llm_and_sessions_are_separate(make_client, provider, monkeypatch):
    client = make_client()
    sent = []
    original = provider.__call__

    async def spy(**kwargs):
        sent.append(kwargs["messages"])
        return await original(**kwargs)

    monkeypatch.setattr(llm_gateway, "_provider_acompletion", spy)
    chat(client, "s-a", "My name is Priya")
    chat(client, "s-a", "What is my name?")
    chat(client, "s-b", "What is my name?")
    assert [m["role"] for m in sent[1]] == ["system", "user", "assistant", "user"]
    assert sent[1][1]["content"] == "My name is Priya"
    assert [m["role"] for m in sent[2]] == ["system", "user"]  # other session sees nothing
    assert len(client.get("/sessions/s-b/messages").json()["messages"]) == 2


def test_unknown_session_returns_404(make_client):
    client = make_client()
    response = client.get("/sessions/does-not-exist/messages")
    assert response.status_code == 404
    assert response.json() == {"detail": "session not found"}


def test_cache_hit_across_sessions(make_client, provider):
    client = make_client()
    live = chat(client, "s-cache-1", "Tell me a fun fact").json()
    live_completion_count = provider.live_completions
    hit = chat(client, "s-cache-2", "Tell me a fun fact").json()

    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert hit["metadata"]["key_id"] is None
    assert hit["metadata"]["fallback_attempts"] == 0
    assert hit["reply"] == live["reply"]
    assert hit["metadata"]["input_tokens"] == live["metadata"]["input_tokens"]
    assert hit["metadata"]["output_tokens"] == live["metadata"]["output_tokens"]
    assert provider.live_completions == live_completion_count
    # the cached answer is still stored in the new session's history
    assert len(client.get("/sessions/s-cache-2/messages").json()["messages"]) == 2


def test_cache_ignores_which_key_answered(make_client, provider):
    client = make_client()
    provider.failing["key-aaa"] = lambda: litellm.RateLimitError("limited", "gemini", "m")
    live = chat(client, "s-k1", "Key independence").json()
    assert live["metadata"]["key_id"] == "key_2"
    provider.failing.clear()
    hit = chat(client, "s-k2", "Key independence").json()  # now key_1 is healthy, but the cache key has no api_key
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert hit["reply"] == live["reply"]


def test_cache_misses_for_different_message_history_or_system_prompt(make_client):
    client = make_client()
    base = chat(client, "s-m1", "Capital of Spain?").json()
    other_message = chat(client, "s-m2", "Capital of Italy?").json()
    other_prompt = chat(client, "s-m3", "Capital of Spain?", system_prompt="Answer in French.").json()
    assert other_message["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert other_prompt["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert other_message["reply"] != base["reply"]

    # same text as the first message but with prior history in the session -> different message list -> miss
    chat(client, "s-m4", "Hello there")
    with_history = chat(client, "s-m4", "Capital of Spain?").json()
    assert with_history["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_same_system_prompt_hits_cache_across_sessions(make_client):
    client = make_client()
    chat(client, "s-p1", "Hi", system_prompt="Be terse.")
    hit = chat(client, "s-p2", "Hi", system_prompt="Be terse.").json()
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"


def test_simulated_single_key_failure_fails_over(make_client, db_path):
    client = make_client()
    response = chat(client, "s-f1", "Failover please", headers={"X-Simulate-Failure": "key_1=429"})
    assert response.status_code == 200
    meta = response.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert response.json()["reply"].startswith("mock reply")
    assert message_count(db_path) == 2


@pytest.mark.parametrize("header", ["key_1=500", "key_1=502", "key_1=503", "key_1=429, key_2=500", "KEY"])
def test_simulation_header_variants(make_client, header):
    client = make_client(keys=("key-aaa", "key-bbb", "key-ccc"))
    response = chat(client, f"s-{header}", "Variant", headers={"X-Simulate-Failure": header})
    assert response.status_code == 200
    if header == "KEY":  # malformed header is ignored
        assert response.json()["metadata"]["key_id"] == "key_1"
    elif "key_2" in header:
        assert response.json()["metadata"]["key_id"] == "key_3"
        assert response.json()["metadata"]["fallback_attempts"] == 2
    else:
        assert response.json()["metadata"]["key_id"] == "key_2"


def test_simulation_hook_skips_provider_for_simulated_keys(make_client, provider):
    client = make_client()
    chat(client, "s-skip", "No provider call", headers={"X-Simulate-Failure": "key_1=500"})
    assert provider.keys_seen == ["key-bbb"]


def test_all_keys_simulated_failure_returns_503_and_writes_nothing(make_client, db_path):
    client = make_client()
    response = chat(client, "s-503", "Everything fails", headers={"X-Simulate-Failure": "*=429"})
    assert response.status_code == 503
    assert response.json() == {"detail": "all providers unavailable"}
    assert message_count(db_path) == 0
    assert client.get("/sessions/s-503/messages").status_code == 404


def test_simulation_requires_flag(make_client):
    client = make_client(simulation=False)
    response = chat(client, "s-off", "Flag off", headers={"X-Simulate-Failure": "*=429"})
    assert response.status_code == 200
    assert response.json()["metadata"]["key_id"] == "key_1"


def test_simulation_applies_to_one_request_only(make_client):
    client = make_client()
    assert chat(client, "s-once", "One", headers={"X-Simulate-Failure": "key_1=429"}).json()["metadata"]["key_id"] == "key_2"
    assert chat(client, "s-once", "Two").json()["metadata"]["key_id"] == "key_1"


def test_real_provider_errors_fail_over_and_exhaust(make_client, provider, db_path):
    client = make_client()
    provider.failing["key-aaa"] = lambda: litellm.RateLimitError("quota exceeded", "gemini", "m")
    ok = chat(client, "s-real", "Real 429")
    assert ok.status_code == 200 and ok.json()["metadata"]["key_id"] == "key_2"
    assert ok.json()["metadata"]["fallback_attempts"] == 1

    provider.failing["key-bbb"] = lambda: litellm.ServiceUnavailableError("down", "gemini", "m")
    down = chat(client, "s-real-2", "Everything down")
    assert down.status_code == 503
    assert down.json() == {"detail": "all providers unavailable"}
    assert "key-aaa" not in down.text and "key-bbb" not in down.text
    assert message_count(db_path) == 2  # only the first successful exchange


def test_timeout_and_connection_errors_fail_over(make_client, provider):
    client = make_client()
    provider.failing["key-aaa"] = lambda: litellm.Timeout("timed out", "gemini", "m")
    assert chat(client, "s-t", "Timeout").json()["metadata"]["key_id"] == "key_2"
    provider.failing["key-aaa"] = lambda: litellm.APIConnectionError("conn", "gemini", "m")
    assert chat(client, "s-c", "Connection").json()["metadata"]["key_id"] == "key_2"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "s", "message": ""},
        {"session_id": "s", "message": "   \n\t "},
        {"session_id": "s", "message": "x" * 4001},
        {"session_id": "", "message": "hi"},
        {"session_id": "s" * 129, "message": "hi"},
        {"message": "hi"},
        {"session_id": "s"},
        {"session_id": "s", "message": 123},
    ],
)
def test_validation_errors_return_422_and_write_nothing(make_client, db_path, payload):
    client = make_client()
    response = client.post("/chat", json=payload)
    assert response.status_code == 422
    assert "detail" in response.json()
    assert message_count(db_path) == 0


def test_malformed_json_returns_422(make_client):
    client = make_client()
    response = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "detail" in response.json()


def test_message_is_trimmed_and_boundary_lengths_accepted(make_client):
    client = make_client()
    assert chat(client, "s-trim", "  hello  ").status_code == 200
    assert client.get("/sessions/s-trim/messages").json()["messages"][0]["content"] == "hello"
    assert chat(client, "s-max", "x" * 4000).status_code == 200
    assert chat(client, "s" * 128, "hi").status_code == 200


def test_errors_are_json_never_html(make_client):
    client = make_client()
    not_found = client.get("/nope")
    assert not_found.status_code == 404 and "detail" in not_found.json()
    wrong_method = client.get("/chat")
    assert wrong_method.status_code == 405 and "detail" in wrong_method.json()


def test_unhandled_exception_returns_json_500(make_client, monkeypatch):
    client = make_client()

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client.app.state.db, "save_exchange", boom)
    response = client.post("/chat", json={"session_id": "s-500", "message": "hi"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert "secret" not in response.text


def test_cors_allows_configured_origin_and_failure_header(make_client):
    client = make_client()
    response = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-simulate-failure" in response.headers["access-control-allow-headers"].lower()


def test_history_survives_app_restart(make_client):
    first = make_client()
    chat(first, "s-persist", "Remember me")
    second = make_client()  # new app instance, same SQLite file
    messages = second.get("/sessions/s-persist/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
