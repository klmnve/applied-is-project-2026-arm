"""Run: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000."""

import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.academic import router as academic_router
from app.auth import Principal, require_audit_view
from app.auth_routes import admin_router, auth_router
from app.catalogs import router as catalogs_router
from app.config import Settings
from app.core import router as core_router
from app.database.db import create_db_engine, init_db
from app.database.seed import seed_resources
from app.db import AuditRepository
from app.embeddings import EmbeddingAdapter, LocalEmbedder
from app.errors import AppError
from app.i18n import error_message, request_locale
from app.learning import router as learning_router
from app.llm import LLMAdapter, OllamaAdapter
from app.routers.resources import router as resources_router
from app.schemas import SearchRequest, SearchResponse
from app.service import AuditSearchService
from app.store import Store
from app.students import router as students_router
from app.teachers import router as teachers_router

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    llm: LLMAdapter | None = None,
    embedder: EmbeddingAdapter | None = None,
    repository: AuditRepository | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()
    repo = repository if repository is not None else AuditRepository(config.database_path)
    language_model = llm if llm is not None else OllamaAdapter(config)
    embedding_model = embedder if embedder is not None else LocalEmbedder(config)
    service = AuditSearchService(repo, language_model, embedding_model, config)
    store = Store(config)
    orm_engine = create_db_engine(config.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        store.seed_academics()
        repo.initialize()
        repo.seed_demo()
        init_db(orm_engine)
        seed_resources(orm_engine)
        try:
            yield
        finally:
            orm_engine.dispose()

    application = FastAPI(
        title="Кафедра АРМ · Қол жеткізу және аудит",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    application.state.repository = repo
    application.state.search_service = service
    application.state.settings = config
    application.state.store = store
    application.state.orm_engine = orm_engine
    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(teachers_router)
    application.include_router(catalogs_router)
    application.include_router(academic_router)
    application.include_router(students_router)
    application.include_router(core_router)
    application.include_router(learning_router)
    application.include_router(resources_router)
    application.mount("/static", StaticFiles(directory=STATIC), name="static")

    @application.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = {
                origin.strip() for origin in config.allowed_origins.split(",") if origin.strip()
            }
            origin = request.headers.get("origin")
            if (origin is not None and origin not in allowed) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "csrf_failed",
                            "message": error_message(
                                "csrf_failed", "Сұрау көзіне рұқсат жоқ.", request_locale(request)
                            ),
                        }
                    },
                )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        response = JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": error_message(exc.code, exc.message, request_locale(request)),
                }
            },
        )
        if exc.code == "rate_limited":
            response.headers["Retry-After"] = str(config.login_window_seconds)
        return response

    @application.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": error_message(
                        "invalid_request",
                        "Енгізілген деректер жарамсыз. "
                        "Өрістердің пішімі мен рұқсат етілген мәндерін тексеріңіз.",
                        request_locale(request),
                    ),
                }
            },
        )

    @application.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> JSONResponse:
        # Avoid logging the question, journal reason, model output or sensitive internals.
        logger.error("Audit request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "server_error",
                    "message": error_message(
                        "server_error",
                        "Сервер сұрауды өңдей алмады. Сервер күйін тексеріп, қайталаңыз.",
                        request_locale(request),
                    ),
                }
            },
        )

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @application.get("/docs", include_in_schema=False)
    def swagger_docs() -> FileResponse:
        return FileResponse(STATIC / "docs.html")

    @application.get("/api/health")
    def health() -> dict[str, str | bool]:
        return {"status": "alive", "timezone": "UTC"}

    @application.get("/api/readiness")
    def readiness(principal: Annotated[Principal, Depends(require_audit_view)]) -> JSONResponse:
        try:
            count = repo.count()
            database = {"status": "ready", "message": f"SQLite: {count} аудит жазбасы."}
        except (sqlite3.Error, OSError):
            database = {
                "status": "unavailable",
                "message": "SQLite қолжетімсіз. python -m app.db орындаңыз.",
            }
        components = {
            "database": database,
            "ollama": language_model.readiness(),
            "embedding": embedding_model.readiness(),
        }
        ready = all(component["status"] == "ready" for component in components.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "components": components,
                "timezone": "UTC",
            },
        )

    @application.post("/api/audit/search", response_model=SearchResponse)
    def search(
        payload: SearchRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_audit_view)],
    ) -> SearchResponse:
        if language_model.readiness()["status"] != "ready":
            raise AppError(
                "model_unavailable",
                "AI көмекшісінің локалды моделі қолжетімсіз. Қалған бөлімдерді пайдалануға болады.",
                503,
            )
        return service.search(payload.question, request_locale(request))

    return application


app = create_app()
