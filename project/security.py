"""Passwords, signed tokens, one-time codes and the login_required decorator."""

import hashlib
import hmac
import re
import secrets
import string
from functools import wraps
from uuid import UUID

import bcrypt
import jwt
import pyotp
from flask import current_app, g, request

from errors import bad_request, unauthorized
from extensions import db
from models import User, utcnow

ALGORITHM = "HS256"
ALIAS_ALPHABET = string.ascii_lowercase + string.digits
ALIAS_LENGTH = 10
RECOVERY_CODE_COUNT = 10

# Token types. Each token carries its type so that, for example, an email-verification
# token can never be replayed as a login token (all tokens share one signing key).
ACCESS = "access"
EMAIL_VERIFICATION = "email_verification"
ALIAS = "alias"
MFA = "mfa"
TOTP_SETUP = "totp_setup"

_dummy_hash = None


# --- passwords ---------------------------------------------------------------------------


def hash_password(password):
    rounds = current_app.config["BCRYPT_ROUNDS"]
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password, password_hash):
    """Constant-work check: when the user does not exist a dummy hash is still verified,
    so response time does not reveal which emails are registered."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(current_app.config["BCRYPT_ROUNDS"]))
    if not isinstance(password, str):
        password = ""
    # Older accounts were hashed by passlib, which silently used only the first 72 bytes;
    # bcrypt >= 5 raises instead, so truncate the same way to keep those passwords working.
    candidate = password.encode("utf-8")[:72]
    try:
        if password_hash is None:
            bcrypt.checkpw(candidate, _dummy_hash)
            return False
        return bcrypt.checkpw(candidate, password_hash.encode("ascii"))
    except ValueError:
        return False


# --- signed tokens -------------------------------------------------------------------------


def create_token(token_type, subject, ttl, **claims):
    now = utcnow()
    payload = {"type": token_type, "sub": subject, "iat": now, "exp": now + ttl, **claims}
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm=ALGORITHM)


def decode_token(token, token_type):
    """Return the claims of a valid, unexpired token of the given type or raise jwt.InvalidTokenError."""
    if not isinstance(token, str) or not token:
        raise jwt.InvalidTokenError("missing token")
    claims = jwt.decode(
        token,
        current_app.config["SECRET_KEY"],
        algorithms=[ALGORITHM],  # never let the token choose its own algorithm
        options={"require": ["exp", "iat", "sub", "type"]},
    )
    if claims["type"] != token_type:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def issue_access_token(user):
    return create_token(ACCESS, str(user.uuid), current_app.config["ACCESS_TOKEN_TTL"], ver=user.token_version)


def issue_verification_token(email, purpose, **claims):
    ttl = current_app.config["VERIFICATION_TOKEN_TTL"]
    return create_token(EMAIL_VERIFICATION, email, ttl, purpose=purpose, **claims)


def check_verification_token(token, email, purpose):
    """The token proves its holder entered the code sent to `email` for this purpose. Returns its claims."""
    try:
        claims = decode_token(token, EMAIL_VERIFICATION)
    except jwt.InvalidTokenError:
        raise bad_request("invalid_verification", "Email address is not verified or verification expired")
    if claims["sub"] != email or claims.get("purpose") != purpose:
        raise bad_request("invalid_verification", "Email address is not verified or verification expired")
    return claims


def bearer_token():
    """The token from an `Authorization: Bearer <token>` header, or "" when there is none."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = bearer_token()
        if not token:
            raise unauthorized("token_missing", "Authorization token is missing")
        try:
            claims = decode_token(token, ACCESS)
            user_id = UUID(claims["sub"])
        except jwt.ExpiredSignatureError:
            raise unauthorized("token_expired", "Token has expired")
        except (jwt.InvalidTokenError, ValueError):
            raise unauthorized("token_invalid", "Invalid token")
        user = db.session.get(User, user_id)
        if user is None or claims.get("ver") != user.token_version:
            raise unauthorized("token_invalid", "Invalid token")
        g.user = user
        return view(*args, **kwargs)

    return wrapper


# --- one-time codes --------------------------------------------------------------------------


def generate_code():
    return f"{secrets.randbelow(10**6):06d}"


def hash_code(email, code):
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def generate_alias_local_part():
    return "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(ALIAS_LENGTH))


# --- TOTP two-factor authentication --------------------------------------------------------

TOTP_STEP = 30


def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(secret, email):
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=current_app.config["TOTP_ISSUER"])


def match_totp_step(secret, code, last_step=None):
    """Return the time step a 6-digit code belongs to (current step +/- 1), or None.

    Steps at or before `last_step` are rejected, so a code that was already used cannot be replayed.
    """
    if not isinstance(code, str):
        return None
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret, interval=TOTP_STEP)
    current = int(utcnow().timestamp()) // TOTP_STEP
    for step in (current, current - 1, current + 1):  # tolerate +/- 30 s of clock drift
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def generate_recovery_codes():
    """Ten one-time codes like 'a1b2c3d4-e5f6a7b8' (64 random bits each)."""
    codes = ["-".join(secrets.token_hex(4) for _ in range(2)) for _ in range(RECOVERY_CODE_COUNT)]
    return codes, [hash_recovery_code(code) for code in codes]


def hash_recovery_code(code):
    # Keyed with SECRET_KEY, so a leaked database alone is not enough to brute-force the codes.
    normalized = re.sub(r"[\s-]", "", code).lower()
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"recovery:{normalized}".encode(), hashlib.sha256).hexdigest()


def verify_second_factor(user, code):
    """Accept a fresh TOTP code or an unused recovery code; consumes what it accepts.

    The caller commits the session."""
    step = match_totp_step(user.totp_secret, code, user.totp_last_step)
    if step is not None:
        user.totp_last_step = step
        return True
    if isinstance(code, str) and code.strip():
        hashed = hash_recovery_code(code)
        remaining = list(user.recovery_codes or [])
        for stored in remaining:
            if hmac.compare_digest(stored, hashed):
                remaining.remove(stored)
                user.recovery_codes = remaining
                return True
    return False
