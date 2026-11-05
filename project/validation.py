"""Server-side validation of all user input (the frontend checks are only a convenience)."""

import re
from urllib.parse import urlsplit

from flask import current_app, request

from errors import bad_request

# Letters, digits and the RFC 5322 atext symbols only. Commas, quotes, angle brackets, brackets
# and the like are refused: Postfix reads a comma in the looked-up main address as a list of
# recipients, and smtplib splits the To header the same way, so "root,me@gmail.com" would also
# deliver to root@<the server's domain>.
EMAIL_RE = re.compile(r"[\w!#$%&'*+/=?^`{|}~.-]+@[\w-]+(?:\.[\w-]+)+")  # used with fullmatch
# The part before @MAIL_DOMAIN of a forwarding address (security.generate_alias_local_part).
ALIAS_LOCAL_PART_RE = re.compile(r"[a-z0-9]{1,64}")  # used with fullmatch
MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72  # bcrypt ignores everything after 72 bytes
MAX_NAME_LENGTH = 100
MAX_SERVICE_LENGTH = 100
MAX_URL_LENGTH = 2048


def get_json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise bad_request("invalid_request", "Expected a JSON object")
    return data


def get_str(data, field, *, max_length, required=True):
    value = data.get(field)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise bad_request("invalid_field", f"'{field}' must be a string", field=field)
    value = value.strip()
    if required and not value:
        raise bad_request("invalid_field", f"'{field}' is required", field=field)
    if len(value) > max_length:
        raise bad_request("invalid_field", f"'{field}' is too long (max {max_length})", field=field)
    return value


def normalize_email(value, field="email"):
    if not isinstance(value, str):
        raise bad_request("invalid_email", "A valid email address is required", field=field)
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_RE.fullmatch(email):
        raise bad_request("invalid_email", "A valid email address is required", field=field)
    return email


def require_external_email(email, field="email"):
    """Main addresses must live outside the alias domain, otherwise forwarding would loop."""
    domain = current_app.config["MAIL_DOMAIN"]
    if email.rsplit("@", 1)[1] == domain or email.endswith("." + domain):
        raise bad_request("alias_domain_email", f"Addresses on {domain} cannot be used as a main address", field=field)
    return email


def password_problem(password):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"at least {MIN_PASSWORD_LENGTH} characters"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"at most {MAX_PASSWORD_BYTES} bytes"
    if not re.search(r"[a-z]", password):
        return "a lowercase letter"
    if not re.search(r"[A-Z]", password):
        return "an uppercase letter"
    if not re.search(r"\d", password):
        return "a digit"
    if not re.search(r"[^A-Za-z0-9]", password):
        return "a special character"
    return None


def validate_new_password(value, field="password"):
    if not isinstance(value, str):
        raise bad_request("weak_password", "Password is required", field=field)
    problem = password_problem(value)
    if problem:
        raise bad_request("weak_password", f"Password must contain {problem}", field=field)
    return value


def normalize_service_url(value):
    """Accept http(s) URLs (scheme optional); reject javascript:, data: and other schemes."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise bad_request("invalid_url", "'service_url' must be a string", field="service_url")
    url = value.strip()
    if not url:
        return ""
    if len(url) > MAX_URL_LENGTH:
        raise bad_request("invalid_url", "URL is too long", field="service_url")
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        # raises ValueError for a non-numeric port: "javascript:alert(1)" -> "https://javascript:alert(1)"
        _ = parts.port
        valid = (
            parts.scheme in {"http", "https"}
            and "." in (parts.hostname or "")
            and parts.username is None  # no "https://trusted.com@evil.com" style links
            and not re.search(r"\s", url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise bad_request("invalid_url", "Only http(s) links are allowed", field="service_url")
    return url
