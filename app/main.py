import logging
import sys

import structlog
from fastapi import FastAPI

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
async def health():
    return {"status": "ok"}
