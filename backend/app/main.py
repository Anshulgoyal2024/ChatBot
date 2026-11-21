"""FastAPI application: /health, /chat, /sessions/{session_id}/messages."""
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

from .config import Settings
from .database import Database
from .llm_gateway import AllProvidersRateLimited, AllProvidersUnavailable, LLMGateway, parse_failure_header
from .schemas import ChatRequest, ChatResponse, HealthResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")


def _clean_validation_errors(errors) -> list:
    return [
        {"loc": [str(part) if not isinstance(part, int) else part for part in err.get("loc", ())],
         "msg": str(err.get("msg", "invalid value")),
         "type": str(err.get("type", "value_error"))}
        for err in errors
    ]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.database_url)
    gateway = LLMGateway(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()
        await gateway.warmup()
        yield

    app = FastAPI(title="LLM Chatbot with Key Failover and Response Caching", lifespan=lifespan)
    app.state.db = db
    app.state.gateway = gateway
    app.state.settings = settings

    origins = list(settings.allowed_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in origins else origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Provider-Failures"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _clean_validation_errors(exc.errors())})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.error("unhandled error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health", response_model=HealthResponse)
    async def health():
        if not db.ping():
            raise HTTPException(status_code=503, detail="database unavailable")
        return HealthResponse(
            status="ok",
            keys_configured=len(gateway.keys),
            cache_enabled=gateway.cache_enabled,
        )

    @app.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request):
        started = time.perf_counter()
        history = db.get_history(payload.session_id)
        system_prompt = payload.system_prompt or db.get_system_prompt(payload.session_id) or settings.default_system_prompt
        messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": payload.message}]
        )
        simulate = parse_failure_header(request.headers.get("x-simulate-failure"))

        try:
            result = await gateway.complete(messages, simulate)
        except AllProvidersRateLimited as exc:
            headers = {"X-Provider-Failures": "; ".join(exc.failures)} if exc.failures else None
            raise HTTPException(status_code=429, detail="provider rate or usage limit reached", headers=headers)
        except AllProvidersUnavailable as exc:
            headers = {"X-Provider-Failures": "; ".join(exc.failures)} if exc.failures else None
            raise HTTPException(status_code=503, detail="all providers unavailable", headers=headers)

        latency_ms = max(1, int(round((time.perf_counter() - started) * 1000)))
        metadata = {
            "resolution_source": result.resolution_source,
            "model": result.model,
            "latency_ms": latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "key_id": result.key_id,
            "fallback_attempts": result.fallback_attempts,
        }
        db.save_exchange(
            session_id=payload.session_id,
            system_prompt=payload.system_prompt,
            user_content=payload.message,
            assistant_content=result.reply,
            metadata=metadata,
        )
        return ChatResponse(session_id=payload.session_id, reply=result.reply, metadata=metadata)

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        if not db.session_exists(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": db.list_messages(session_id)}

    return app


app = create_app()
