"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

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

    # Order matters. `add_middleware` prepends, so the LAST call is outermost:
    # this lands the shield inside CORS, which is the whole point of it.
    _register_error_shield(app)
    _register_cors(app, settings)
    _register_middleware(app)
    _register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_prefix)

    _register_icons(app)

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


#: Icon files served from `app/web`, with the content type each must carry.
#: An explicit map rather than a static mount: that directory also holds the
#: console's HTML, and mounting it would serve those pages outside the auth
#: checks that currently gate them.
_ICON_FILES: dict[str, str] = {
    "favicon.ico": "image/x-icon",
    "favicon-16.png": "image/png",
    "favicon-32.png": "image/png",
    "favicon-192.png": "image/png",
    "apple-touch-icon.png": "image/png",
    # The brand mark shown beside "The iScale" in both headers.
    "logo.png": "image/png",
}


def _register_icons(app: FastAPI) -> None:
    """Serve the iScale favicon at the paths browsers actually request.

    `/favicon.ico` is fetched automatically whether or not a page links to it,
    so it lives at the root rather than under the console prefix.
    """
    web_dir = Path(__file__).resolve().parent / "web"

    @app.get("/{filename}", include_in_schema=False)
    async def icon(filename: str) -> Response:
        media_type = _ICON_FILES.get(filename)
        if media_type is None:
            raise HTTPException(status_code=404)
        path = web_dir / filename
        if not path.is_file():  # pragma: no cover - packaging error
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type=media_type,
            # Long-lived: the logo changes about never, and a favicon refetched
            # on every page load is pure noise in the access log.
            headers={"Cache-Control": "public, max-age=604800"},
        )


def _register_error_shield(app: FastAPI) -> None:
    """Turn an unhandled exception into a 500 *inside* the CORS layer.

    Starlette handles `Exception` in `ServerErrorMiddleware`, which sits outside
    everything - so a crashing endpoint produces a 500 with no CORS headers, and
    the browser reports it as a CORS failure. The real error is invisible: the
    console showed "blocked by CORS policy" for what was actually a KeyError in
    a log line, which is a long way to walk in the wrong direction.

    Catching here, inside the CORS middleware, means the 500 carries the usual
    `Access-Control-Allow-Origin` header and the frontend reads the real status.
    The handler in `_register_exception_handlers` stays as the backstop for
    anything raised further out than this.
    """

    @app.middleware("http")
    async def error_shield(request: Request, call_next: Any) -> Any:
        try:
            return await call_next(request)
        except Exception:
            logger.exception("Unhandled exception")
            # Still nothing internal in the body - only the status is fixed.
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "An unexpected error occurred.",
                },
            )


def _register_cors(app: FastAPI, settings: Settings) -> None:
    """Allow a separately hosted console to call the API with credentials.

    An explicit allow-list, never "*". These endpoints return every customer
    transcript and phone number we hold; a wildcard with credentials is both
    refused by browsers and wrong on the merits. Nothing is registered at all
    when no external origin is configured, so a same-origin deployment gains no
    new surface.
    """
    origins = settings.console_origins
    pattern = settings.console_origin_regex
    if not origins and not pattern:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=pattern,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    # Logged as parsed, not as configured. A missing scheme or a stray trailing
    # slash blocks every request before it reaches the server, so the logs would
    # otherwise be silent about the one thing that is wrong.
    logger.info(
        "CORS enabled for the console",
        extra={"origins": origins, "origin_regex": pattern},
    )


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
