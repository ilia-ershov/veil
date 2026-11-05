"""Email verification codes, registration, login with optional TOTP and password reset."""

import hmac
import logging
import math
from uuid import UUID

import jwt
from flask import Blueprint, current_app, jsonify, request
from flask_limiter.util import get_remote_address
from sqlalchemy.exc import IntegrityError

from errors import ApiError, bad_request, conflict, unauthorized
from extensions import db, limiter
from mailer import notify, send_email
from models import EmailVerification, User, as_utc, utcnow
from security import (
    MFA,
    check_verification_token,
    create_token,
    decode_token,
    generate_code,
    hash_code,
    hash_password,
    issue_access_token,
    issue_verification_token,
    verify_password,
    verify_second_factor,
)
from validation import (
    MAX_NAME_LENGTH,
    get_json,
    get_str,
    normalize_email,
    require_external_email,
    validate_new_password,
)

bp = Blueprint("auth", __name__, url_prefix="/auth")
log = logging.getLogger(__name__)

PURPOSES = ("register", "change_email", "reset_password")
SUBJECTS = {
    "register": "Код подтверждения регистрации",
    "change_email": "Код подтверждения нового адреса",
    "reset_password": "Код для сброса пароля",
}


def failed_auth(response):
    """Rate limits on credential checks only count failures, so normal use is never throttled."""
    return response.status_code in (400, 401, 403)


def code_sent(response):
    return response.status_code == 200


def _requested_email():
    data = request.get_json(silent=True)
    email = data.get("email") if isinstance(data, dict) else None
    return email.strip().lower()[:254] if isinstance(email, str) else ""


# Per-IP limits alone let a botnet try passwords or 2FA codes against one account without end,
# so failures are also counted per account, whichever addresses they come from.
def login_account_key():
    email = _requested_email()
    return f"login-account:{email}" if email else get_remote_address()


# Likewise, codes are counted per destination address: a botnet must not be able to flood one
# inbox with codes (hurting the sender's reputation too), nor get unlimited fresh codes to guess.
def code_address_key():
    email = _requested_email()
    return f"code-address:{email}" if email else get_remote_address()


def mfa_account_key():
    data = request.get_json(silent=True)
    try:
        claims = decode_token(data.get("mfa_token") if isinstance(data, dict) else None, MFA)
    except jwt.InvalidTokenError:
        return get_remote_address()
    return f"mfa-account:{claims['sub']}"


def _code_email(purpose, code, registered):
    """What to send for a code request.

    The API answer is identical in every case, so it cannot be used to find out
    whether an address has an account; only the mailbox owner sees the difference.
    A message is sent every time, otherwise the response time would give it away.
    """
    service = current_app.config["MAIL_DOMAIN"]
    minutes = int(current_app.config["CODE_TTL"].total_seconds() // 60)
    if purpose == "reset_password" and not registered:
        return (
            "Запрос на сброс пароля",
            f"Кто-то запросил сброс пароля на {service} для этого адреса, но аккаунта с ним нет.\n\n"
            "Если это были вы, возможно, аккаунт зарегистрирован на другой адрес. "
            "Если нет, просто проигнорируйте это письмо.",
        )
    if purpose != "reset_password" and registered:
        return (
            "Попытка использовать ваш адрес",
            f"Кто-то пытался использовать этот адрес для нового аккаунта на {service}, "
            "но он уже привязан к существующему аккаунту.\n\n"
            "Если это были вы, просто войдите или восстановите пароль. "
            "Если нет, ничего делать не нужно: доступа к вашему аккаунту никто не получил.",
        )
    return (
        SUBJECTS[purpose],
        f"Ваш код подтверждения для {service}: {code}\n\n"
        f"Код действует {minutes} минут. Никому его не сообщайте.\n"
        "Если вы не запрашивали код, просто проигнорируйте это письмо.",
    )


def _purpose(data):
    purpose = data.get("purpose", "register")
    if purpose not in PURPOSES:
        raise bad_request("invalid_field", "Unknown purpose", field="purpose")
    return purpose


def _resend_too_soon(seconds):
    return ApiError(429, "resend_too_soon", "Please wait before requesting another code", retry_after=seconds)


@bp.post("/request-code")
@limiter.limit("5 per minute;30 per hour")
@limiter.limit("10 per hour", key_func=code_address_key, deduct_when=code_sent)
def request_code():
    data = get_json()
    email = require_external_email(normalize_email(data.get("email")))
    purpose = _purpose(data)

    config = current_app.config
    now = utcnow()
    EmailVerification.query.filter(EmailVerification.expires_at < now).delete()
    record = db.session.get(EmailVerification, (email, purpose), with_for_update=True)
    if record is not None:
        wait = (as_utc(record.created_at) + config["CODE_RESEND_COOLDOWN"] - now).total_seconds()
        if wait > 0:
            raise _resend_too_soon(math.ceil(wait))
    else:
        record = EmailVerification(email=email, purpose=purpose)
        db.session.add(record)

    code = generate_code()
    record.code_hash = hash_code(email, code)
    record.attempts = 0
    record.created_at = now
    record.expires_at = now + config["CODE_TTL"]
    try:
        db.session.commit()
    except IntegrityError:  # a parallel request for the same address got there first
        db.session.rollback()
        raise _resend_too_soon(int(config["CODE_RESEND_COOLDOWN"].total_seconds()))

    registered = User.query.filter_by(email=email).first() is not None
    try:
        send_email(email, *_code_email(purpose, code, registered))
    except Exception:
        log.exception("Could not send verification email")
        db.session.delete(record)
        db.session.commit()
        raise ApiError(503, "mail_unavailable", "Could not send the email, please try again later")
    return jsonify(message="If this address can receive mail, a code has been sent to it")


@bp.post("/verify-code")
@limiter.limit("20 per minute")
def verify_code():
    data = get_json()
    email = normalize_email(data.get("email"))
    purpose = _purpose(data)
    code = data.get("code")
    code = code.strip() if isinstance(code, str) else ""

    record = db.session.get(EmailVerification, (email, purpose), with_for_update=True)
    if record is None or as_utc(record.expires_at) <= utcnow():
        raise bad_request("invalid_code", "Invalid or expired code")

    if hmac.compare_digest(record.code_hash, hash_code(email, code)):
        db.session.delete(record)  # single use
        db.session.commit()
        claims = {}
        if purpose == "reset_password":
            user = User.query.filter_by(email=email).first()
            if user is not None:
                # The reset bumps token_version, so the token sets a new password only once.
                claims["ver"] = user.token_version
        token = issue_verification_token(email, purpose, **claims)
        return jsonify(verification_token=token, purpose=purpose)

    # Each code allows a few guesses, so brute-forcing a 6-digit code is not feasible.
    record.attempts += 1
    attempts_left = current_app.config["CODE_MAX_ATTEMPTS"] - record.attempts
    if attempts_left <= 0:
        db.session.delete(record)
        db.session.commit()
        raise bad_request("too_many_attempts", "Too many wrong attempts, request a new code")
    db.session.commit()
    raise bad_request("invalid_code", "Invalid or expired code", attempts_left=attempts_left)


@bp.post("/register")
@limiter.limit("20 per hour")
def register():
    data = get_json()
    name = get_str(data, "name", max_length=MAX_NAME_LENGTH)
    email = require_external_email(normalize_email(data.get("email")))
    password = validate_new_password(data.get("password"))
    check_verification_token(data.get("verification_token"), email, "register")

    if User.query.filter_by(email=email).first() is not None:
        raise conflict("email_taken", "An account with this email already exists")
    user = User(name=name, email=email, password_hash=hash_password(password))
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise conflict("email_taken", "An account with this email already exists")
    return jsonify(token=issue_access_token(user), user=user.to_dict()), 201


@bp.post("/login")
@limiter.limit("10 per minute;50 per hour", deduct_when=failed_auth)
@limiter.limit("20 per hour", key_func=login_account_key, deduct_when=failed_auth)
def login():
    data = get_json()
    email = data.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    user = User.query.filter_by(email=email).first() if email else None
    if not verify_password(data.get("password"), user.password_hash if user else None):
        raise unauthorized("invalid_credentials", "Invalid email or password")

    if user.mfa_enabled:
        mfa_token = create_token(MFA, str(user.uuid), current_app.config["MFA_TOKEN_TTL"], ver=user.token_version)
        return jsonify(mfa_required=True, mfa_token=mfa_token)
    return jsonify(token=issue_access_token(user), user=user.to_dict())


@bp.post("/login/2fa")
@limiter.limit("10 per minute;50 per hour", deduct_when=failed_auth)
@limiter.limit("10 per hour", key_func=mfa_account_key, deduct_when=failed_auth)
def login_second_factor():
    data = get_json()
    try:
        claims = decode_token(data.get("mfa_token"), MFA)
        user = db.session.get(User, UUID(claims["sub"]), with_for_update=True)
    except (jwt.InvalidTokenError, ValueError):
        user = None
        claims = {}
    if user is None or claims.get("ver") != user.token_version or not user.mfa_enabled:
        raise unauthorized("mfa_expired", "Sign-in expired, please log in again")

    if not verify_second_factor(user, data.get("code")):
        raise unauthorized("invalid_mfa_code", "Invalid authentication code")
    db.session.commit()  # stores the used time step / consumed recovery code
    return jsonify(token=issue_access_token(user), user=user.to_dict())


@bp.post("/reset-password")
@limiter.limit("10 per hour")
def reset_password():
    data = get_json()
    email = normalize_email(data.get("email"))
    password = validate_new_password(data.get("password"))
    claims = check_verification_token(data.get("verification_token"), email, "reset_password")

    user = User.query.filter_by(email=email).first()
    if user is None or claims.get("ver") != user.token_version:
        raise bad_request("invalid_verification", "Email address is not verified or verification expired")
    user.password_hash = hash_password(password)
    user.token_version += 1  # signs out every existing session
    db.session.commit()
    notify(
        user.email,
        "Пароль сброшен",
        f"Пароль вашего аккаунта на {current_app.config['MAIL_DOMAIN']} сброшен по коду из письма, "
        "все сеансы завершены.\n\nЕсли это сделали не вы, кто-то имеет доступ к этому почтовому ящику: "
        "смените пароль от него, затем снова восстановите пароль аккаунта.",
    )
    return jsonify(message="Password updated")
