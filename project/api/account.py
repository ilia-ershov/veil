"""The signed-in user's account: profile, main address, password, 2FA and deletion."""

import jwt
from flask import Blueprint, current_app, g, jsonify
from flask_limiter.util import get_remote_address
from sqlalchemy.exc import IntegrityError

from errors import ApiError, bad_request, conflict
from extensions import db, limiter
from mailer import notify
from models import User
from security import (
    ACCESS,
    TOTP_SETUP,
    bearer_token,
    check_verification_token,
    create_token,
    decode_token,
    generate_recovery_codes,
    hash_password,
    issue_access_token,
    login_required,
    match_totp_step,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_second_factor,
)
from validation import get_json, normalize_email, require_external_email, validate_new_password

from .aliases import retire
from .auth import failed_auth

bp = Blueprint("account", __name__, url_prefix="/account")


def wrong_secret(response):
    return response.status_code == 403  # wrong password or 2FA code (see _require_password)


# A stolen session must not turn into a password-guessing oracle: knowing the password unlocks
# moving the main address, i.e. all forwarded mail. Wrong re-entries are counted per account,
# whichever addresses they come from (the per-IP limit alone does not stop a botnet).
def session_account_key():
    try:
        claims = decode_token(bearer_token(), ACCESS)
    except jwt.InvalidTokenError:
        return get_remote_address()
    return f"session-account:{claims['sub']}"


per_account_reentry = limiter.shared_limit(
    "20 per hour", scope="password-reentry", key_func=session_account_key, deduct_when=wrong_secret
)


def _require_password(data, field="password"):
    # 403, not 401: a wrong password here must not look like an expired session to the client.
    if not verify_password(data.get(field), g.user.password_hash):
        raise ApiError(403, "invalid_password", "Wrong password", field=field)


def _require_second_factor(data):
    """With 2FA on, a fresh TOTP or recovery code is needed too: a password alone (phished, or typed
    on someone else's computer) must not be enough to move all forwarded mail. The code is used up
    only if the change is committed."""
    user = g.user
    db.session.refresh(user, with_for_update=True)  # parallel requests cannot reuse one code
    if not verify_second_factor(user, data.get("code")):
        raise ApiError(403, "invalid_mfa_code", "Invalid authentication code", field="code")


@bp.get("")
@login_required
def get_account():
    return jsonify(user=g.user.to_dict())


@bp.post("/email")
@limiter.limit("10 per minute", deduct_when=failed_auth)
@per_account_reentry
@login_required
def change_email():
    data = get_json()
    _require_password(data)
    if g.user.mfa_enabled:
        _require_second_factor(data)
    email = require_external_email(normalize_email(data.get("email")))
    check_verification_token(data.get("verification_token"), email, "change_email")

    user = g.user
    if email == user.email:
        return jsonify(user=user.to_dict())
    if User.query.filter_by(email=email).first() is not None:
        raise conflict("email_taken", "An account with this email already exists")
    old_email = user.email
    user.email = email
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise conflict("email_taken", "An account with this email already exists")

    notify(
        old_email,
        "Основной адрес изменён",
        f"Основной адрес вашего аккаунта на {current_app.config['MAIL_DOMAIN']} изменён на {email}. "
        "Письма с ваших адресов теперь пересылаются туда.\n\n"
        "Если это сделали не вы, срочно восстановите пароль и проверьте настройки аккаунта.",
    )
    return jsonify(user=user.to_dict())


@bp.post("/password")
@limiter.limit("10 per minute", deduct_when=failed_auth)
@per_account_reentry
@login_required
def change_password():
    data = get_json()
    _require_password(data, "current_password")
    password = validate_new_password(data.get("new_password"), field="new_password")
    user = g.user
    user.password_hash = hash_password(password)
    user.token_version += 1  # every other session is signed out
    db.session.commit()
    notify(
        user.email, "Пароль изменён", "Пароль вашего аккаунта был изменён. Если это были не вы, восстановите пароль."
    )
    return jsonify(token=issue_access_token(user), user=user.to_dict())


@bp.post("/logout-all")
@login_required
def logout_everywhere():
    g.user.token_version += 1
    db.session.commit()
    return jsonify(message="All sessions have been signed out")


@bp.delete("")
@limiter.limit("10 per minute", deduct_when=failed_auth)
@per_account_reentry
@login_required
def delete_account():
    data = get_json()
    _require_password(data)
    email = g.user.email
    retire(list(g.user.aliases))
    db.session.delete(g.user)
    db.session.commit()
    notify(
        email,
        "Аккаунт удалён",
        f"Ваш аккаунт на {current_app.config['MAIL_DOMAIN']} удалён, все его адреса отключены "
        "и персональные данные стёрты.",
    )
    return jsonify(message="Account deleted")


# --- two-factor authentication (TOTP, RFC 6238) ----------------------------------------------


@bp.post("/2fa/setup")
@login_required
def setup_2fa():
    user = g.user
    if user.mfa_enabled:
        raise conflict("mfa_already_enabled", "Two-factor authentication is already enabled")
    secret = new_totp_secret()
    # The secret only reaches the database once the user proves their app generates valid codes.
    setup_token = create_token(TOTP_SETUP, str(user.uuid), current_app.config["TOTP_SETUP_TOKEN_TTL"], secret=secret)
    return jsonify(secret=secret, otpauth_uri=totp_uri(secret, user.email), setup_token=setup_token)


@bp.post("/2fa/enable")
@limiter.limit("10 per minute", deduct_when=failed_auth)
@login_required
def enable_2fa():
    data = get_json()
    user = g.user
    try:
        claims = decode_token(data.get("setup_token"), TOTP_SETUP)
    except jwt.InvalidTokenError:
        claims = None
    if not claims or claims["sub"] != str(user.uuid) or not isinstance(claims.get("secret"), str):
        raise bad_request("invalid_setup_token", "Setup expired, please start again")
    if user.mfa_enabled:
        raise conflict("mfa_already_enabled", "Two-factor authentication is already enabled")

    step = match_totp_step(claims["secret"], data.get("code"))
    if step is None:
        raise bad_request("invalid_mfa_code", "Invalid authentication code")
    codes, hashes = generate_recovery_codes()
    user.totp_secret = claims["secret"]
    user.totp_last_step = step
    user.recovery_codes = hashes
    db.session.commit()
    notify(user.email, "Двухфакторная аутентификация включена", "Для входа в аккаунт теперь нужен код из приложения.")
    return jsonify(user=user.to_dict(), recovery_codes=codes)


@bp.post("/2fa/disable")
@limiter.limit("10 per minute", deduct_when=failed_auth)
@per_account_reentry
@login_required
def disable_2fa():
    data = get_json()
    _require_password(data)
    user = g.user
    if not user.mfa_enabled:
        return jsonify(user=user.to_dict())
    _require_second_factor(data)
    user.totp_secret = None
    user.totp_last_step = None
    user.recovery_codes = None
    db.session.commit()
    notify(user.email, "Двухфакторная аутентификация отключена", "Если это сделали не вы, срочно смените пароль.")
    return jsonify(user=user.to_dict())
