"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.v1 import api_router
from app.container import Container
from app.services.inactivity import InactivitySweeper
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, correlation_id_var, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container on startup, release resources on shutdown."""
    settings: Settings = app.state.settings
    container = Container.build(settings)
    app.state.container = container

    logger.info(
        "Application started",
        extra={
            "environment": settings.environment,
            "whatsapp_enabled": settings.whatsapp_enabled,
            "sheets_enabled": settings.google_sheets_enabled,
            "courses": len(container.knowledge_base.courses),
        },
    )
    # Started after the container so it shares the same database and messaging
    # client, and stopped first on shutdown so a sweep cannot outlive the
    # connection pool it is using.
    sweeper = InactivitySweeper(
        database=container.database,
        messaging=container.messaging,
        settings=settings,
    )
    sweeper.start()
    app.state.sweeper = sweeper

    try:
        yield
    finally:
        await sweeper.stop()
        await container.shutdown()
        logger.info("Application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="AI receptionist for iScale on WhatsApp.",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    _register_middleware(app)
    _register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_prefix)

    # HEAD as well as GET: uptime monitors default to HEAD, and a 405 there
    # reads as an outage.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": __version__,
            "docs": "disabled" if settings.is_production else "/docs",
        }

    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next: Any) -> Any:
        """Tag every log line in a request with one traceable id."""
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        token = correlation_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "Application error",
            extra={"error_code": exc.error_code, "detail": exc.message},
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "details": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        # Never leak internals to the caller.
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "An unexpected error occurred."},
        )


app = create_app()
