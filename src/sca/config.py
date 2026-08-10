from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_", env_file=".env", extra="ignore")

    env: str = "local"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5434/tgc_sca"
    api_key: str = "dev-key-change-me"

    # Where the business sits. Supplier local time is per supplier; this is the
    # timezone the buying team works in, and the one every "is anyone awake"
    # decision is measured against.
    home_timezone: str = "Asia/Riyadh"

    # Reordering policy. Cover is measured in weeks of forecast demand: raise a
    # suggestion below `reorder_cover_weeks`, and buy up to `target_cover_weeks`.
    # Static numbers rather than per item settings on purpose for the base build;
    # they are the first thing to move into supplier or category level rules.
    reorder_cover_weeks: float = 4.0
    target_cover_weeks: float = 8.0

    # Approval gates. Anything at or above this value, or with a supplier that has
    # never completed an order, needs a human before it leaves the building.
    approval_threshold_sar: float = 25000.0

    # How long a supplier may sit on an order before the system chases, and how
    # far an ETA may move before it becomes an exception rather than an update.
    ack_reminder_hours: int = 24
    eta_slip_days: int = 3

    # Origins allowed to call the API from a browser. Wide open while auth is a
    # single shared key on fabricated data; narrow before real supplier data.
    cors_allow_origins: tuple[str, ...] = ("*",)


@lru_cache
def get_settings() -> Settings:
    return Settings()
