from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    OPENAI_API_KEY: str
    OPENAI_REALTIME_MODEL: str
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_PHONE_NUMBER: str
    DISPATCHER_ALERT_PHONE: str
    EMERGENCY_TRANSFER_PHONE: str
    # Demo mode: hang up after the emergency message finishes playing instead
    # of redirecting the call to EMERGENCY_TRANSFER_PHONE. Defaults to True
    # (hang up).
    EMERGENCY_HANGUP_INSTEAD_OF_TRANSFER: bool = True
    DEMO_PHONE_HMAC_SECRET: str = Field(min_length=1)
    DEMO_SESSION_TTL_SECONDS: int = 900
    DEMO_CLAIMED_SESSION_TTL_SECONDS: int = Field(default=1800, gt=0)
    FRONTEND_ORIGINS: str = ""

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
