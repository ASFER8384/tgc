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

    # Which Shopify vendor is which TGC brand, as "vendor=brand" pairs separated
    # by commas. Merged over the three built in names rather than replacing them,
    # so adding a fourth store does not mean restating the first three.
    #
    # Configuration rather than code because the vendor field is maintained by
    # whoever set the store up, and a single brand store will have called itself
    # whatever it called itself. Getting that wrong should be an environment
    # variable, not a deployment.
    brand_by_vendor: str = ""
    # What an unrecognised vendor becomes. Empty means "unassigned", which is
    # deliberately visible in the console rather than silently dropped — on a
    # multi brand store an unmapped vendor is a mistake worth seeing. On a single
    # brand store it is noise, and naming the brand here is the honest fix.
    default_brand: str = ""
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
