from pydantic import Field
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

    connect_page_url: str = ""
    connect_apps: str = "happ,v2raytun,hiddify"

    referral_enabled: bool = True
    referral_invitee_bonus_gb: int = Field(default=15, ge=0)
    referral_reward_percent: int = Field(default=30, ge=0, le=100)
    referral_max_active_links: int = Field(default=3, ge=1)
    referral_monthly_cap: int = Field(default=10, ge=0)
    bot_username: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
