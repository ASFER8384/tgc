from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Customer platform settings, still under their own CDP_ prefix.

    What is deliberately not here any more is the database URL and the API key.
    Both halves are one service now, so two ways to configure the same connection
    and the same credential is two ways to configure it wrong: point them at
    different databases and the platform silently becomes two products again.
    Those come from the platform settings; everything below is CDP's own policy.
    """

    model_config = SettingsConfigDict(env_prefix="CDP_", env_file=".env", extra="ignore")

    shopify_webhook_secret: str = ""
    default_country_code: str = "966"
    # Origins allowed to call the API from a browser. "*" while the console is a
    # static demo page on fabricated data; narrow this before real customers land.
    cors_allow_origins: tuple[str, ...] = ("*",)

    # Recency thresholds (days) and monetary thresholds (SAR) behind the RFM
    # score. Static rather than population quintiles on purpose: quintiles move
    # under you as the customer base grows, so last month's "R5" stops meaning
    # what it meant. Revisit these with the client, do not tune them silently.
    rfm_recency_days: tuple[int, ...] = (30, 60, 120, 240)
    rfm_frequency_orders: tuple[int, ...] = (1, 2, 4, 8)
    rfm_monetary_sar: tuple[int, ...] = (200, 500, 1500, 4000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
