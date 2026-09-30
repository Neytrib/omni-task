import asyncio
import hmac
import logging
import re
from contextlib import asynccontextmanager, suppress
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import auth, schemas, services, voice
from app.config import Settings
from app.errors import AppError
from app.models import ProcessingRequest
from app.realtime import LiveHub, subscriber_loop, task_socket
from omni_logging import configure_logging, correlation_context, log_event

logger = logging.getLogger("omni_task")


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db  # Closing rolls back reads or any failed operation; writes commit explicitly.


DB = Annotated[Session, Depends(get_db)]


def current_session(request: Request, db: DB) -> auth.AuthenticatedSession:
    result = auth.authenticate_session(
        db, request.cookies.get(request.app.state.settings.cookie_name)
    )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied.encode(), result.csrf_token.encode()):
            raise AppError(403, "csrf_failed", "A valid CSRF token is required.")
    return result


Browser = Annotated[auth.AuthenticatedSession, Depends(current_session)]


def expected_version(if_match: Annotated[str | None, Header()] = None) -> int:
    if if_match is None:
        raise AppError(428, "version_required", "Supply the task version in If-Match.")
    if not re.fullmatch(r'(?:[1-9][0-9]{0,9}|"[1-9][0-9]{0,9}")', if_match):
        raise AppError(422, "invalid_version", "If-Match must contain a positive task version.")
    return int(if_match.strip('"'))


Version = Annotated[int, Depends(expected_version)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    factory = sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging("api")
        stop = asyncio.Event()
        background_tasks = []
        if settings.voice_dispatch_enabled:
            from app.jobs.dispatcher import dispatcher_loop

            background_tasks.append(asyncio.create_task(dispatcher_loop(factory, settings, stop)))
        if settings.live_enabled:
            from app.live_outbox import outbox_loop

            background_tasks.extend(
                [
                    asyncio.create_task(outbox_loop(factory, settings, stop)),
                    asyncio.create_task(
                        subscriber_loop(settings, application.state.live_hub, stop)
                    ),
                ]
            )
        try:
            yield
        finally:
            stop.set()
            for task in background_tasks:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            redis.close()
            engine.dispose()

    application = FastAPI(
        title="Omni Task API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = factory
    application.state.live_hub = LiveHub()
    application.add_api_websocket_route("/api/ws/tasks", task_socket)

    def error_response(request: Request, status: int, code: str, message: str):
        return JSONResponse(
            status_code=status,
            content={
                "error": {"code": code, "message": message},
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @application.exception_handler(AppError)
    async def known_error(request: Request, error: AppError):
        return error_response(request, error.status, error.code, error.message)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _error: RequestValidationError):
        # FastAPI's default validation details may echo raw content or credentials.
        return error_response(
            request, 422, "invalid_input", "Request fields are invalid or unsupported."
        )

    @application.middleware("http")
    async def security_boundary(request: Request, call_next):
        started = monotonic()
        with correlation_context(request.headers.get("X-Correlation-ID")) as correlation_id:
            request.state.request_id = str(uuid4())
            path = request.url.path
            response = None
            if path.startswith("/internal"):
                value = request.headers.get("authorization", "")
                expected = "Bearer " + settings.bot_api_key.get_secret_value()
                if not hmac.compare_digest(value.encode(), expected.encode()):
                    response = error_response(
                        request, 401, "bot_unauthenticated", "Service authentication is required."
                    )
                elif request.headers.get("origin") is not None or request.cookies:
                    response = error_response(
                        request, 403, "bot_only", "This endpoint is reserved for the bot service."
                    )
            elif path.startswith("/api") and request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != settings.dashboard_origin:
                    response = error_response(
                        request, 403, "origin_failed", "The request origin is not allowed."
                    )
            if response is None:
                try:
                    if request.method in {"POST", "PATCH", "PUT"}:
                        # Stream a bounded body before JSON parsing, even without Content-Length.
                        body = bytearray()
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > 1_048_576:
                                raise AppError(
                                    413, "request_too_large", "Request body is too large."
                                )
                        request._body = bytes(body)
                    response = await call_next(request)
                except AppError as error:
                    response = error_response(request, error.status, error.code, error.message)
                except Exception:
                    # Do not log exception objects, requests, URLs, SQL parameters, or headers.
                    log_event(
                        logger,
                        "http_request_failed",
                        level=logging.ERROR,
                        request_id=request.state.request_id,
                        error_code="service_unavailable",
                    )
                    response = error_response(
                        request,
                        503,
                        "service_unavailable",
                        "The service is temporarily unavailable.",
                    )
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Correlation-ID"] = correlation_id
            if path not in {"/api/health/live", "/api/health/ready", "/internal/health"}:
                log_event(
                    logger,
                    "http_request_completed",
                    request_id=request.state.request_id,
                    http_status=response.status_code,
                    duration_ms=int((monotonic() - started) * 1000),
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

    @application.get("/api/health/live")
    def live():
        return {"status": "ok"}

    @application.get("/api/health/ready")
    def ready(db: DB):
        db.execute(text("SELECT 1"))
        # Readiness requires migration, not merely a listening database socket.
        db.execute(text("SELECT version_num FROM alembic_version"))
        if not redis.ping():
            raise AppError(503, "not_ready", "Dependencies are not ready.")
        return {"status": "ready"}

    @application.get("/internal/health")
    def internal_health():
        return {"status": "ok", "service": "api"}

    bot = APIRouter(prefix="/internal/bot")

    @bot.post("/users", response_model=schemas.UserView)
    def register_user(body: schemas.UserCreate, db: DB):
        user = services.ensure_user(db, body.telegram_user_id, body.private_chat_id)
        db.commit()
        return user

    @bot.post("/tasks", response_model=schemas.TaskView)
    def bot_create(body: schemas.BotTaskCreate, db: DB, response: Response):
        user = services.telegram_user(db, body.telegram_user_id)
        task, created = services.create_task(
            db,
            user.id,
            body.content,
            "telegram_text",
            bot_identity=settings.bot_identity,
            message_id=body.message_id,
        )
        db.commit()
        response.status_code = 201 if created else 200
        return task

    @bot.get("/tasks", response_model=schemas.TaskPage)
    def bot_list(query: Annotated[schemas.BotPageQuery, Query()], db: DB):
        user = services.telegram_user(db, query.telegram_user_id)
        result = services.list_tasks(
            db, user.id, query.limit, query.cursor, settings.bot_api_key.get_secret_value()
        )
        return schemas.TaskPage.model_validate(result)

    @bot.get("/tasks/{task_id}", response_model=schemas.TaskView)
    def bot_detail(task_id: UUID, query: Annotated[schemas.BotActorQuery, Query()], db: DB):
        user = services.telegram_user(db, query.telegram_user_id)
        return services.owned_task(db, user.id, task_id)

    @bot.patch("/tasks/{task_id}", response_model=schemas.TaskView)
    def bot_status(
        task_id: UUID,
        query: Annotated[schemas.BotActorQuery, Query()],
        body: schemas.StatusChange,
        version: Version,
        db: DB,
    ):
        user = services.telegram_user(db, query.telegram_user_id)
        task = services.change_status(db, user.id, task_id, body.status, version)
        db.commit()
        return task

    @bot.delete("/tasks/{task_id}", status_code=204)
    def bot_delete(
        task_id: UUID, query: Annotated[schemas.BotActorQuery, Query()], version: Version, db: DB
    ):
        user = services.telegram_user(db, query.telegram_user_id)
        services.delete_task(db, user.id, task_id, version)
        db.commit()
        return Response(status_code=204)

    @bot.post("/processing-requests", response_model=schemas.ProcessingView)
    def accept_processing(body: schemas.ProcessingCreate, db: DB, response: Response):
        if body.duration_seconds > settings.voice_max_duration_seconds or (
            body.file_size is not None and body.file_size > settings.voice_max_bytes
        ):
            raise AppError(422, "voice_limit_exceeded", "The recording exceeds configured limits.")
        user = services.telegram_user(db, body.telegram_user_id)
        result, created = services.create_processing_request(
            db,
            user.id,
            body.file_id,
            settings.bot_identity,
            body.message_id,
            ack_message_id=body.acknowledgement_message_id,
            duration_seconds=body.duration_seconds,
            file_size=body.file_size,
            provider_name=settings.transcription_provider,
        )
        db.commit()
        response.status_code = 201 if created else 200
        log_event(
            logger,
            "voice_request_accepted",
            job_id=result.id,
            outcome="created" if created else "duplicate",
        )
        return voice.processing_view(db, result)

    @bot.get("/processing-requests/{request_id}", response_model=schemas.ProcessingView)
    def processing_detail(
        request_id: UUID, query: Annotated[schemas.BotActorQuery, Query()], db: DB
    ):
        user = services.telegram_user(db, query.telegram_user_id)
        result = db.scalar(
            select(ProcessingRequest).where(
                ProcessingRequest.id == request_id, ProcessingRequest.owner_id == user.id
            )
        )
        if result is None:
            raise AppError(404, "processing_not_found", "Processing request not found.")
        return voice.processing_view(db, result)

    @application.post("/internal/voice/{request_id}/download-context")
    def voice_download_context(request_id: UUID, body: schemas.VoiceLease, db: DB):
        return voice.download_context(db, request_id, body.lease_token, settings)

    @application.post("/internal/voice/{request_id}/notification-context")
    def voice_notification_context(request_id: UUID, body: schemas.VoiceLease, db: DB):
        return voice.notification_context(db, request_id, body.lease_token, settings)

    @application.post("/internal/voice/{request_id}/notification-checkpoint", status_code=204)
    def voice_notification_checkpoint(
        request_id: UUID, body: schemas.NotificationCheckpoint, db: DB
    ):
        if not voice.checkpoint_notification(
            db, request_id, body.lease_token, body.message_id, settings
        ):
            raise AppError(409, "lease_expired", "This notification lease is no longer active.")
        db.commit()
        return Response(status_code=204)

    @bot.post("/login-links")
    def login_link(body: schemas.TelegramActor, db: DB):
        user = services.telegram_user(db, body.telegram_user_id)
        result = auth.issue_login_link(db, user, settings)
        db.commit()
        return result

    browser = APIRouter(prefix="/api")

    @browser.post("/auth/exchange")
    def exchange(body: schemas.Exchange, db: DB, response: Response, request: Request):
        token, session = auth.exchange_link(db, body.token, settings)
        # Logging in rotates/revokes any previous browser session in this cookie jar.
        previous = request.cookies.get(settings.cookie_name)
        if previous:
            from app.models import BrowserSession

            old = db.scalar(
                select(BrowserSession).where(
                    BrowserSession.token_hash == auth.credential_hash(previous)
                )
            )
            if old is not None:
                old.revoked_at = services.utcnow()
        db.commit()
        response.set_cookie(
            key=settings.cookie_name,
            value=token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return {"expires_at": session.expires_at}

    @browser.get("/session")
    def session_info(current: Browser):
        return {
            "user": schemas.UserView.model_validate(current.user),
            "csrf_token": current.csrf_token,
            "expires_at": current.session.expires_at,
        }

    @browser.post("/auth/logout", status_code=204)
    def logout(current: Browser, db: DB):
        current.session.revoked_at = services.utcnow()
        db.commit()
        response = Response(status_code=204)
        response.delete_cookie(
            settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @browser.get("/tasks", response_model=schemas.TaskPage)
    def browser_list(query: Annotated[schemas.PageQuery, Query()], current: Browser, db: DB):
        result = services.list_tasks(
            db, current.user.id, query.limit, query.cursor, settings.bot_api_key.get_secret_value()
        )
        return schemas.TaskPage.model_validate(result)

    @browser.post("/tasks", response_model=schemas.TaskView)
    def browser_create(
        body: schemas.TaskCreate,
        current: Browser,
        db: DB,
        response: Response,
        idempotency_key: Annotated[UUID, Header()],
    ):
        task, created = services.create_task(
            db, current.user.id, body.content, "dashboard", client_request_id=idempotency_key
        )
        db.commit()
        response.status_code = 201 if created else 200
        return task

    @browser.get("/tasks/{task_id}", response_model=schemas.TaskView)
    def browser_detail(task_id: UUID, current: Browser, db: DB):
        return services.owned_task(db, current.user.id, task_id)

    @browser.patch("/tasks/{task_id}", response_model=schemas.TaskView)
    def browser_status(
        task_id: UUID, body: schemas.StatusChange, version: Version, current: Browser, db: DB
    ):
        task = services.change_status(db, current.user.id, task_id, body.status, version)
        db.commit()
        return task

    @browser.delete("/tasks/{task_id}", status_code=204)
    def browser_delete(task_id: UUID, version: Version, current: Browser, db: DB):
        services.delete_task(db, current.user.id, task_id, version)
        db.commit()
        return Response(status_code=204)

    application.include_router(bot)
    application.include_router(browser)
    return application
