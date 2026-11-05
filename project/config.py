"""Application configuration, read from environment variables (and an optional .env file)."""

import os
from datetime import timedelta

# Values that must never be used to sign tokens in a real deployment.
WEAK_SECRETS = {"your_secret_key", "secret", "changeme", "change-me", "dev", "devsecret"}
MIN_SECRET_LENGTH = 32


def _bool(name, default=False):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _str(name, default=""):
    """Like os.getenv, but an empty value (e.g. `VAR=` in docker compose) also means "use the default"."""
    value = os.getenv(name, "").strip()
    return value or default


def _int(name, default):
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {value!r}") from None


def _list(name):
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def normalize_database_url(url):
    """Pin plain PostgreSQL URLs to the psycopg 3 driver.

    SQLAlchemy 2.0 maps ``postgresql://`` to psycopg2 while 2.1 maps it to psycopg 3;
    being explicit makes the URL work the same with either version.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def load_config():
    mail_domain = _str("MAIL_DOMAIN", "veil.ershov.me").lower()
    return {
        "SECRET_KEY": os.getenv("SECRET_KEY", ""),
        "SQLALCHEMY_DATABASE_URI": normalize_database_url(os.getenv("DATABASE_URL", "")),
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
        "AUTO_INIT_DB": _bool("AUTO_INIT_DB", True),
        # Reject oversized request bodies early (the API only ever receives small JSON documents).
        "MAX_CONTENT_LENGTH": 16 * 1024,
        "MAIL_DOMAIN": mail_domain,
        "CORS_ORIGINS": _list("CORS_ORIGINS"),
        "TRUST_PROXY": _int("TRUST_PROXY", 0),
        "BCRYPT_ROUNDS": _int("BCRYPT_ROUNDS", 12),
        "ACCESS_TOKEN_TTL": timedelta(minutes=_int("ACCESS_TOKEN_TTL_MINUTES", 240)),
        "VERIFICATION_TOKEN_TTL": timedelta(minutes=15),
        "ALIAS_TOKEN_TTL": timedelta(minutes=30),
        "MFA_TOKEN_TTL": timedelta(minutes=5),
        "TOTP_SETUP_TOKEN_TTL": timedelta(minutes=15),
        "CODE_TTL": timedelta(minutes=10),
        "CODE_MAX_ATTEMPTS": 5,
        "CODE_RESEND_COOLDOWN": timedelta(seconds=60),
        "MAX_ALIASES_PER_USER": _int("MAX_ALIASES_PER_USER", 100),
        "TOTP_ISSUER": _str("TOTP_ISSUER", mail_domain),
        # console = log messages (development), smtp = deliver through an SMTP server.
        "MAIL_BACKEND": _str("MAIL_BACKEND", "console").lower(),
        "MAIL_FROM": _str("MAIL_FROM", f"no-reply@{mail_domain}"),
        "SMTP_HOST": _str("SMTP_HOST", "localhost"),
        "SMTP_PORT": _int("SMTP_PORT", 587),
        "SMTP_USERNAME": os.getenv("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
        "SMTP_STARTTLS": _bool("SMTP_STARTTLS", True),
        "SMTP_SSL": _bool("SMTP_SSL", False),
        "SMTP_TIMEOUT": _int("SMTP_TIMEOUT", 10),
        "RATELIMIT_ENABLED": _bool("RATELIMIT_ENABLED", True),
        "RATELIMIT_STORAGE_URI": _str("RATELIMIT_STORAGE_URI", "memory://"),
        # While Redis is unreachable each worker keeps counting in memory instead of failing requests.
        "RATELIMIT_IN_MEMORY_FALLBACK_ENABLED": True,
        "RATELIMIT_HEADERS_ENABLED": True,
    }


def validate_config(config):
    """Fail fast instead of silently running with an insecure or broken configuration."""
    secret = config.get("SECRET_KEY") or ""
    if len(secret) < MIN_SECRET_LENGTH or secret in WEAK_SECRETS:
        raise RuntimeError(
            f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_LENGTH} characters, e.g. "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if not config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL must be set, e.g. postgresql://user:password@localhost/mail_organizer")
    if config.get("MAIL_BACKEND") not in {"console", "smtp", "memory"}:
        raise RuntimeError(
            f"MAIL_BACKEND must be 'console' or 'smtp' ('memory' is for the tests), got {config.get('MAIL_BACKEND')!r}"
        )
