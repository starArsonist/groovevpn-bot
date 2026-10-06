from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_admin_id: int
    marzban_api_url: str
    marzban_username: str
    marzban_password: str
    
    database_url: str = "sqlite+aiosqlite:///data/bot.sqlite3"

    trial_enabled: bool = True
    trial_data_gb: int = 10
    trial_days: int = 7
    trial_daily_cap: int = 50

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
