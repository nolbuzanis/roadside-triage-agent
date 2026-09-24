"""Secure demo-session start flow: anonymous visitors create short-lived sessions."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.demo_sessions import create_demo_session, normalize_phone_e164
from app.services.tickets import _get_supabase
from supabase import AuthApiError

logger = structlog.get_logger(__name__)

router = APIRouter()


class DemoSessionStartRequest(BaseModel):
    phone: str = Field(min_length=1)


class DemoSessionStartResponse(BaseModel):
    id: str
    phone_last4: str
    expires_at: str
    demo_phone: str


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        logger.warning("Demo session auth rejected", reason="missing_bearer_token")
        raise HTTPException(status_code=401, detail="Bearer access token required")
    return token.strip()


async def _authenticate_demo_visitor(token: str) -> str:
    """Validate the Supabase access token and return the anonymous auth user id."""

    def _get_user() -> Any:
        return _get_supabase().auth.get_user(jwt=token)

    try:
        user_response = await asyncio.to_thread(_get_user)
    except AuthApiError:
        logger.warning("Demo session auth rejected", reason="invalid_token")
        raise HTTPException(status_code=401, detail="Invalid access token") from None

    user = user_response.user if user_response is not None else None
    if user is None:
        logger.warning("Demo session auth rejected", reason="no_user")
        raise HTTPException(status_code=401, detail="Invalid access token")
    if not user.is_anonymous:
        logger.warning(
            "Demo session auth rejected",
            reason="not_anonymous",
            auth_user_id=str(user.id),
        )
        raise HTTPException(
            status_code=403,
            detail="Demo sessions require an anonymous user",
        )
    return str(user.id)


@router.post(
    "/demo-sessions",
    response_model=DemoSessionStartResponse,
    status_code=201,
)
async def start_demo_session(
    payload: DemoSessionStartRequest,
    request: Request,
) -> DemoSessionStartResponse:
    token = _bearer_token(request)
    auth_user_id = await _authenticate_demo_visitor(token)

    try:
        normalize_phone_e164(phone=payload.phone)
    except ValueError:
        logger.warning("Demo session start rejected", reason="invalid_phone")
        raise HTTPException(status_code=422, detail="Invalid phone number") from None

    row = await asyncio.to_thread(
        create_demo_session,
        auth_user_id=auth_user_id,
        phone=payload.phone,
    )

    settings = get_settings()
    session_id = str(row.get("id") or "")
    if not session_id:
        logger.error(
            "Demo session start failed",
            reason="insert_returned_no_id",
            auth_user_id=auth_user_id,
        )
        raise HTTPException(status_code=500, detail="Failed to create demo session")
    logger.info(
        "Demo session start succeeded",
        demo_session_id=session_id,
        auth_user_id=auth_user_id,
    )
    return DemoSessionStartResponse(
        id=session_id,
        phone_last4=str(row["phone_last4"]),
        expires_at=str(row["expires_at"]),
        demo_phone=settings.TWILIO_PHONE_NUMBER,
    )
