import os
import threading
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.limiter import limiter
from app.routers import notes_router
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

# Force WEB_CONCURRENCY to 1 if set higher on free-tier deployments to avoid duplicate worker processes
if int(os.environ.get("WEB_CONCURRENCY", "1")) > 1:
    os.environ["WEB_CONCURRENCY"] = "1"

_celery_worker_thread = None
_celery_worker_lock = threading.Lock()
_celery_worker_started = False


def _start_celery_worker():
    try:
        logger.info("Starting in-process Celery worker thread...")
        celery_app.worker_main(
            argv=[
                "worker",
                "--loglevel=info",
                "--pool=solo",
                "--without-heartbeat",
                "--without-gossip",
                "--without-mingle",
            ]
        )
    except Exception as e:
        logger.error(f"In-process Celery worker encountered an error: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _celery_worker_thread, _celery_worker_started
    with _celery_worker_lock:
        if not _celery_worker_started:
            _celery_worker_thread = threading.Thread(
                target=_start_celery_worker,
                name="celery-inprocess-worker",
                daemon=True,
            )
            _celery_worker_thread.start()
            _celery_worker_started = True
            logger.info("Celery in-process background worker thread initialized.")
    yield


app = FastAPI(title="Audio Notes Platform API", lifespan=lifespan)
app.state.limiter = limiter

MAX_BODY_SIZE = 110 * 1024 * 1024  # 110 MB limit (allows 100MB audio file + multipart metadata)


class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST":
            content_length_header = request.headers.get("content-length")
            if content_length_header:
                try:
                    content_length = int(content_length_header)
                    if content_length > MAX_BODY_SIZE:
                        return JSONResponse(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            content={
                                "error_code": "REQUEST_TOO_LARGE",
                                "message": "Request payload exceeds maximum allowed size limit of 105MB.",
                            },
                        )
                except ValueError:
                    pass
        return await call_next(request)


@app.exception_handler(RateLimitExceeded)
def custom_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error_code": "RATE_LIMIT_EXCEEDED",
            "message": "Rate limit exceeded. Please try again later.",
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled server exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error_code": "INTERNAL_SERVER_ERROR", "message": str(exc)},
    )


def _get_cors_origins() -> list[str]:
    origins = {
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    }
    if settings.FRONTEND_URL:
        for u in settings.FRONTEND_URL.split(","):
            u_clean = u.strip().rstrip("/")
            if u_clean:
                origins.add(u_clean)
    return sorted(list(origins))


cors_origins = _get_cors_origins()
logger.info(f"Configured CORS origins: {cors_origins}")

# Configure Middleware
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(LimitUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"^https?://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(notes_router, prefix="/api")


@app.get("/api/health")
def health_check():
    db_status = "ok"
    try:
        from sqlalchemy import text
        from app.db.session import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "ok",
        "database": db_status,
        "worker_thread_alive": _celery_worker_thread.is_alive() if _celery_worker_thread else False,
        "frontend_url": settings.FRONTEND_URL,
        "cors_origins": cors_origins,
    }


