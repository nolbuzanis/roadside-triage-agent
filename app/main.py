import asyncio
import logging
import sys
import time

import httpx
import structlog
from fastapi import FastAPI, Query

from app.api.demo_sessions import router as demo_sessions_router
from app.api.twilio import router as twilio_router
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
app.include_router(twilio_router, prefix="/api/v1")
app.include_router(demo_sessions_router, prefix="/api/v1")


@app.on_event("startup")
async def validate_settings() -> None:
    try:
        get_settings()
        logger.info("Settings validated successfully")
    except Exception as e:
        logger.critical("Required environment variables are missing", error=str(e))
        sys.exit(1)


@app.get("/health")
async def health(
    check_db: bool = Query(default=False),
    check_twilio: bool = Query(default=False),
    check_openai: bool = Query(default=False),
):
    """Health check endpoint.

    Returns basic process status by default. Pass query params to also verify
    downstream service connectivity (each adds latency from a lightweight API call):

    - ``?check_db=true`` — Supabase row read
    - ``?check_twilio=true`` — Twilio account fetch (non-billable)
    - ``?check_openai=true`` — OpenAI models list (non-billable)
    """
    start = time.monotonic()
    status = "ok"
    error_msg: str | None = None
    settings = None

    try:
        settings = get_settings()
    except Exception as exc:
        logger.warning("Health check: configuration invalid", error=str(exc))
        status = "error"
        error_msg = "configuration_invalid"

    if status == "ok" and check_db:

        def _sync_check_db() -> None:
            from app.services.tickets import _get_supabase

            supabase = _get_supabase()
            supabase.table("assistance_requests").select("id").limit(1).execute()

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_check_db), timeout=5.0)
        except Exception as exc:
            logger.warning("Health check: database unreachable", error=str(exc))
            status = "degraded"

    if status == "ok" and check_twilio:

        def _sync_check_twilio() -> None:
            from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

            client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)  # type: ignore[union-attr]
            client.api.accounts(settings.TWILIO_ACCOUNT_SID).fetch()  # type: ignore[union-attr]

        try:
            await asyncio.wait_for(asyncio.to_thread(_sync_check_twilio), timeout=5.0)
        except Exception as exc:
            logger.warning("Health check: Twilio unreachable", error=str(exc))
            status = "degraded"

    if status == "ok" and check_openai:

        async def _check_openai() -> None:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},  # type: ignore[union-attr]
                    timeout=5.0,
                )
                resp.raise_for_status()

        try:
            await asyncio.wait_for(_check_openai(), timeout=5.0)
        except Exception as exc:
            logger.warning("Health check: OpenAI unreachable", error=str(exc))
            status = "degraded"

    result: dict[str, object] = {
        "status": status,
        "response_time_ms": round((time.monotonic() - start) * 1000, 1),
    }
    if error_msg:
        result["error"] = error_msg
    if check_db and status != "error":
        result["database"] = "ok" if status == "ok" else "error"
    if check_twilio and status != "error":
        result["twilio"] = "ok" if status == "ok" else "error"
    if check_openai and status != "error":
        result["openai"] = "ok" if status == "ok" else "error"
    return result
