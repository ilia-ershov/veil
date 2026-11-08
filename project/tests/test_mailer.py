"""The SMTP branch of the mailer, exercised against a fake smtplib server."""

import pytest

import mailer
from mailer import send_email


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append("send")
        self.messages.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_smtp_with_starttls_and_login(app_factory, fake_smtp):
    app_factory(
        MAIL_BACKEND="smtp",
        SMTP_HOST="mail.example.com",
        SMTP_PORT=587,
        SMTP_USERNAME="bot",
        SMTP_PASSWORD="pw",
        MAIL_FROM="no-reply@veil.ershov.me",
    )
    send_email("user@example.com", "Код подтверждения", "Ваш код: 123456")

    server = fake_smtp.instances[0]
    assert (server.host, server.port) == ("mail.example.com", 587)
    assert server.calls == ["starttls", ("login", "bot", "pw"), "send", "quit"]
    message = server.messages[0]
    assert message["From"] == "no-reply@veil.ershov.me"
    assert message["To"] == "user@example.com"
    assert message["Subject"] == "Код подтверждения"
    assert "123456" in message.get_content()
    # Gmail rejects messages without these headers
    assert message["Message-ID"].startswith("<") and message["Message-ID"].endswith("@veil.ershov.me>")
    assert message["Date"].endswith("+0000")


def test_smtp_over_ssl_without_auth(app_factory, fake_smtp):
    app_factory(MAIL_BACKEND="smtp", SMTP_SSL=True, SMTP_PORT=465, SMTP_USERNAME="")
    send_email("user@example.com", "s", "b")
    assert fake_smtp.instances[0].calls == ["send", "quit"]  # implicit TLS: no STARTTLS, no login


def test_failed_delivery_is_reported_and_code_discarded(app_factory, monkeypatch):
    app = app_factory(MAIL_BACKEND="smtp")

    def refuse(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(mailer.smtplib, "SMTP", refuse)
    response = app.test_client().post("/api/auth/request-code", json={"email": "a@example.com"})
    assert response.status_code == 503
    assert response.get_json()["code"] == "mail_unavailable"
    # no stale code left behind: an immediate retry is not blocked by the resend cooldown
    from extensions import db
    from models import EmailVerification

    assert db.session.get(EmailVerification, ("a@example.com", "register")) is None
