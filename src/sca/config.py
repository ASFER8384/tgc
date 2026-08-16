from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # populate_by_name so a field carrying a validation_alias can still be set by
    # its own name. Without it the aliased fields — the WhatsApp credentials —
    # silently ignore a value passed in code and fall back to None, which reads
    # as "not configured" rather than as a mistake.
    model_config = SettingsConfigDict(
        env_prefix="SCA_", env_file=".env", extra="ignore", populate_by_name=True,
    )

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

    # How far back to look when demand is measured from actual customer orders
    # rather than typed in. Long enough to survive a quiet fortnight, short
    # enough that last season stops voting on this season's buying.
    demand_window_weeks: float = 8.0

    # A floor in units, under which an item is bought back up whatever the
    # forecast says. Cover in weeks is the better trigger and remains the
    # primary one — it is the number a buyer thinks in, and it accounts for how
    # long the mill takes. But it can only speak where there is a demand figure
    # to divide by, and an item with no forecast and no sales history produces
    # no opinion at all however empty the shelf gets. That silence is correct
    # for a system that refuses to invent demand, and wrong for the buyer who
    # simply never wants fewer than fifty abayas in the building.
    #
    # Zero is off, which is the default: a global floor applied to a catalogue
    # of fabric, finished garments and cartons would be the same number for a
    # bolt of silk and a box of labels. The useful setting is per item, on
    # Item.min_stock; this exists so a category with consistent packaging can be
    # covered in one place, and so the console has something to show.
    min_stock_default: int = 0

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

    # Outbound mail. Off by default: this is the only part of the system that can
    # reach someone outside the building, and switching it on should be a
    # deliberate act. "console" prints the message, "smtp" actually sends it.
    mail_provider: str = "none"
    mail_smtp_host: str = "smtp.gmail.com"
    mail_smtp_port: int = 587
    mail_smtp_user: str | None = None
    # An app password, not the account password: Gmail and Microsoft 365 both
    # refuse the latter outright.
    mail_smtp_password: str | None = None
    mail_smtp_starttls: bool = True

    mail_from: str | None = None
    mail_from_name: str = "Procurement"
    # Where supplier replies land, which need not be where we send from. This has
    # to be a mailbox someone or something actually watches, or the
    # acknowledgement half of the loop quietly stops working.
    mail_reply_to: str | None = None

    # Safety rails for anywhere that is not production. A redirect sends every
    # message to one inbox whatever the order says; an allowlist refuses domains
    # outside it. The demo suppliers have invented addresses on domains that may
    # belong to real people, so one of these should always be set outside prod.
    mail_redirect_to: str | None = None
    mail_allowed_domains: tuple[str, ...] = ()

    # Outbound WhatsApp, on the same terms as mail: off unless somebody turns it
    # on, and with the same two safety rails, because it can reach a stranger's
    # phone rather than a mailbox they chose to give us.
    #
    # "cloud" is Meta's Cloud API. There is no SMTP equivalent here — one
    # provider, one shape — so the switch exists to keep the off state explicit
    # rather than to choose between vendors.
    wa_provider: str = "none"
    wa_api_version: str = "v21.0"
    # These three read the Meta_* names as well as the SCA_WA_* ones, because
    # the credentials were already in the environment under Meta's own naming
    # and a second copy of a token is a second thing to rotate — and the one
    # that gets forgotten.
    #
    # A system user token, not a developer one: developer tokens expire in 24
    # hours and would take supplier messaging down without warning.
    wa_access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SCA_WA_ACCESS_TOKEN", "Meta_Access_Token"),
    )
    # The sending number's id, which is not the number. Meta returns both and
    # they are easy to confuse — a WABA id, a phone number id and a phone number
    # are three different strings, and two of them look alike.
    wa_phone_number_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SCA_WA_PHONE_NUMBER_ID", "Meta_Phone_Id"),
    )
    # The account that owns the number. Needed to read and submit templates.
    wa_business_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SCA_WA_BUSINESS_ACCOUNT_ID", "Meta_Waba_Id"),
    )
    # The approved template that opens a conversation. A business-initiated
    # WhatsApp message must use one, whatever it says: there is no exception for
    # operational mail, so an order cannot be sent as free text to a supplier who
    # has not written first.
    wa_template_order: str = "purchase_order"
    wa_template_language: str = "en"
    # The same two rails as mail, for the same reason. A redirect sends every
    # message to one handset whatever the supplier record says; an allowlist
    # refuses any number outside it. One of these should always be set outside
    # production, because the cost of a mistake here is a message to a stranger.
    wa_redirect_to: str | None = None
    wa_allowed_numbers: tuple[str, ...] = ()

    # Inbound. Meta pushes replies to a public HTTPS endpoint rather than
    # offering a mailbox to poll, so both of these are needed before a supplier's
    # answer can reach this system at all.
    #
    # A string this side invents, echoed back during Meta's one-time handshake.
    # It proves the endpoint is ours; it does not authenticate anything after.
    wa_verify_token: str | None = None
    # What authenticates every delivery afterwards. Meta signs each body with the
    # app secret, and without checking that signature the webhook is a public URL
    # anybody can post supplier confirmations to.
    wa_app_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SCA_WA_APP_SECRET", "Meta_App_Secret"),
    )

    # Inbound. The other half of the loop: replies are read out of a real mailbox
    # rather than pasted into the console. IMAP because it works against whatever
    # mailbox the business already has, with no domain, no public URL and no
    # webhook to register. Credentials fall back to the SMTP ones, since sending
    # and receiving are normally the same account.
    mail_imap_host: str = "imap.gmail.com"
    mail_imap_port: int = 993
    mail_imap_user: str | None = None
    mail_imap_password: str | None = None
    mail_imap_folder: str = "INBOX"
    # UNSEEN rather than ALL: the read flag is the cursor, so a restart does not
    # reprocess the entire mailbox.
    mail_imap_search: str = "UNSEEN"
    # Attachments are kept whole in the database, so there has to be a ceiling.
    # Ten megabytes clears every invoice and packing list seen in practice and
    # still refuses the scanned catalogue somebody attaches by mistake. Over the
    # limit the file is skipped and said so, never truncated.
    mail_max_attachment_bytes: int = 10_000_000

    # Serve the API key inside the console page so it connects without anybody
    # typing one. This is not a convenience setting, it is an authentication
    # setting: the consoles are served to anyone who has the URL, so with this on
    # the key is in view-source and the API is effectively open — customer
    # records, supplier pricing, and placing or cancelling orders.
    #
    # It exists because a demonstration where the client is handed a key first
    # demonstrates the key.
    #
    # On by default while this is a demonstration, deliberately: defaulting it
    # off meant remembering to set a variable on every environment, and a
    # security control that has to be switched on to work is one that is off in
    # practice. The honest position is that this service currently has no
    # authentication, said plainly here and warned about on every start, rather
    # than a default that implies protection nobody enabled.
    #
    # Set SCA_CONSOLE_AUTO_CONNECT=false before this holds data somebody would
    # mind losing, and rotate SCA_API_KEY at the same time — everyone who opened
    # the page while it was on has the old one.
    console_auto_connect: bool = True

    # Arrival estimates. Clearing is a flat allowance until enough receipts
    # exist to measure one per lane, and is stated as an assumption wherever it
    # is shown rather than folded into a single confident date.
    customs_clearance_days: int = 3
    # A real forecast for the origin, fetched without a key. It produces a
    # warning beside the estimate and never adds days to it. Off makes the
    # estimate a pure function of stored data, which is what the tests want.
    weather_advisory: bool = True

    # The storefront, read-only. Without these three the Inventory page still
    # works and says plainly that the online shelf has never been counted, which
    # is better than a zero that looks like an answer.
    #
    # The token is an Admin API access token (shpat_…) from a custom app, and it
    # needs read_products and read_inventory. Not write: this platform does not
    # push stock to Shopify, because Shopify moves its own count on a checkout, a
    # refund and a fulfilment that nothing here is told about, and a figure sent
    # from here that tried to lead theirs would oversell the website.
    # Both spellings accepted. The unprefixed ones are what a Shopify app's own
    # setup writes into a .env, and refusing to read a variable that is already
    # sitting there correctly would be pedantry with a support cost.
    shopify_shop_domain: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SCA_SHOPIFY_SHOP_DOMAIN", "SHOPIFY_SHOP_DOMAIN", "SHOPIFY_DOMAIN"
        ),
    )
    # An access token, not the app's API key or secret. The key and secret are
    # OAuth credentials — they identify the app to Shopify and are what a webhook
    # signature is checked against; neither will authenticate an Admin API call.
    # The token is what an install produces, and it starts shpat_.
    shopify_admin_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SCA_SHOPIFY_ADMIN_TOKEN", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_ACCESS_TOKEN"
        ),
    )
    # Pinned rather than "latest": Shopify retires a version a year after it
    # ships, and a query that silently changes shape under a running service is
    # a stock figure that goes wrong without anybody deploying anything.
    shopify_api_version: str = "2025-10"

    # The sample data button. On while this is a demonstration, including on the
    # deployed service, because a demo nobody can put data into demonstrates
    # nothing. A single switch rather than a test against the environment name,
    # so turning it off before real supplier data arrives is one variable and not
    # a code change.
    allow_sample_data: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
