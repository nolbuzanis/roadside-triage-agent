import logging
import sys

from fastapi import FastAPI

from app.api.twilio import router as twilio_router
from app.api.webhooks import router as webhooks_router
from app.core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Roadside Triage Agent")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(twilio_router, prefix="/api/v1")


@app.on_event("startup")
async def validate_settings() -> None:
    try:
        get_settings()
    except Exception as e:
        logger.critical("Required environment variables are missing: %s", e)
        sys.exit(1)


@app.get("/health")
async def health():
    return {"status": "ok"}
