import logging
import sys

from fastapi import FastAPI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Roadside Triage Agent")


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
