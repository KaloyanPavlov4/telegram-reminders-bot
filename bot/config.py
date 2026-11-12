from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_token: str
    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"
    allowed_user_id: int
    tz: str = "UTC"
    db_path: str = "data/reminders.db"
    tick_seconds: int = 30
