from functools import lru_cache

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
    WEBHOOK_SECRET: str

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
