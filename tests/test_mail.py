"""Outbound mail: the guards, not the wire.

Nothing here opens a socket. What is worth testing is the part that decides
whether a message may be sent at all, because that is what stands between a test
environment and a stranger's inbox.
"""

import pytest

from sca.config import Settings
from sca.mail import MailError, get_mailer, resolve_recipient
from sca.mail.base import ConsoleMailer, NullMailer, SmtpMailer


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's own .env cannot change what these assert.
    return Settings(_env_file=None, **overrides)


def test_mail_is_off_unless_someone_turns_it_on():
    assert isinstance(get_mailer(_settings()), NullMailer)


def test_console_provider_never_reaches_the_network():
    assert isinstance(get_mailer(_settings(mail_provider="console")), ConsoleMailer)


def test_smtp_refuses_to_start_without_credentials():
    with pytest.raises(MailError) as exc:
        get_mailer(_settings(mail_provider="smtp"))
    assert "SCA_MAIL_SMTP_USER" in str(exc.value)


def test_smtp_starts_when_fully_configured():
    mailer = get_mailer(_settings(
        mail_provider="smtp", mail_smtp_user="me@example.com",
        mail_smtp_password="app-password", mail_from="me@example.com",
    ))
    assert isinstance(mailer, SmtpMailer)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("smtp.gmail.com", 587),          # STARTTLS
        ("smtp.office365.com", 587),
        ("smtp.zoho.com", 465),           # implicit TLS
        ("smtp.mail.yahoo.com", 465),
        ("smtp-relay.brevo.com", 587),
    ],
)
def test_any_provider_configures_without_special_casing(host, port):
    """The mailer is plain SMTP. A provider is a host and a port, not a code path."""
    mailer = get_mailer(_settings(
        mail_provider="smtp", mail_smtp_host=host, mail_smtp_port=port,
        mail_smtp_user="me@example.com", mail_smtp_password="app-password",
        mail_from="me@example.com",
    ))
    assert isinstance(mailer, SmtpMailer)
    assert mailer.settings.mail_smtp_host == host


def test_a_redirect_overrides_the_supplier_address():
    """The demo suppliers have invented addresses on domains someone may own."""
    settings = _settings(mail_redirect_to="me@example.com")
    assert resolve_recipient("orders@gzsilkmill.cn", settings) == "me@example.com"


def test_an_address_outside_the_allowlist_is_refused():
    settings = _settings(mail_allowed_domains=("example.com",))
    with pytest.raises(MailError) as exc:
        resolve_recipient("orders@gzsilkmill.cn", settings)
    assert "SCA_MAIL_REDIRECT_TO" in str(exc.value)


def test_an_address_inside_the_allowlist_passes():
    settings = _settings(mail_allowed_domains=("example.com",))
    assert resolve_recipient("buyer@example.com", settings) == "buyer@example.com"


def test_a_redirect_beats_an_allowlist():
    """Both set is the common test configuration, and the redirect has to win or
    the allowlist would reject the message before it could be rerouted."""
    settings = _settings(
        mail_redirect_to="me@example.com", mail_allowed_domains=("example.com",)
    )
    assert resolve_recipient("orders@gzsilkmill.cn", settings) == "me@example.com"


def test_a_supplier_with_no_address_is_an_error_not_a_silent_skip():
    with pytest.raises(MailError):
        resolve_recipient(None, _settings())
