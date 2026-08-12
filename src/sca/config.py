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

    # How far back to look when demand is measured from actual customer orders
    # rather than typed in. Long enough to survive a quiet fortnight, short
    # enough that last season stops voting on this season's buying.
    demand_window_weeks: float = 8.0

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

    # The sample data button. On while this is a demonstration, including on the
    # deployed service, because a demo nobody can put data into demonstrates
    # nothing. A single switch rather than a test against the environment name,
    # so turning it off before real supplier data arrives is one variable and not
    # a code change.
    allow_sample_data: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
