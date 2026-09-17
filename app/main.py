import asyncio
import logging
import sys
import time

import structlog
from fastapi import FastAPI, Query

from app.api.twilio import router as twilio_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings

logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

app = FastAPI(title="Roadside Triage Agent")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(twilio_router, prefix="/api/v1")


@app.on_event("startup")
async def validate_settings() -> None:
    try:
        get_settings()
        logger.info("Settings validated successfully")
    except Exception as e:
        logger.critical("Required environment variables are missing", error=str(e))
        sys.exit(1)


@app.get("/health")
async def health(check_db: bool = Query(default=False)):
    """Health check endpoint.

    Returns basic process status by default. Pass ``?check_db=true`` to also
    verify Supabase connectivity (adds latency from a single row read).
    """
    start = time.monotonic()
    status = "ok"
    error_msg: str | None = None

    try:
        get_settings()
    except Exception as exc:
        logger.warning("Health check: configuration invalid", error=str(exc))
        status = "error"
        error_msg = "configuration_invalid"

    if status == "ok" and check_db:

        async def _check_db() -> None:
            from app.services.tickets import _get_supabase

            supabase = _get_supabase()
            supabase.table("breakdown_tickets").select("id").limit(1).execute()

        try:
            await asyncio.wait_for(_check_db(), timeout=5.0)
        except Exception as exc:
            logger.warning("Health check: database unreachable", error=str(exc))
            status = "degraded"

    result: dict[str, object] = {
        "status": status,
        "response_time_ms": round((time.monotonic() - start) * 1000, 1),
    }
    if error_msg:
        result["error"] = error_msg
    if check_db and status != "error":
        result["database"] = "ok" if status == "ok" else "error"
    return result
