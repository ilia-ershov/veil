"""Forwarding addresses (aliases): generate, create, list, edit, deactivate, reactivate and delete."""

import jwt
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import IntegrityError

from errors import ApiError, bad_request, conflict, not_found
from extensions import db, limiter
from models import Alias, RetiredAlias, utcnow
from security import ALIAS, create_token, decode_token, generate_alias_local_part, login_required
from validation import ALIAS_LOCAL_PART_RE, MAX_SERVICE_LENGTH, get_json, get_str, normalize_service_url

bp = Blueprint("aliases", __name__, url_prefix="/aliases")

STATUS_FILTERS = {"all": None, "active": True, "inactive": False}


def _payload(alias):
    return alias.to_dict(current_app.config["MAIL_DOMAIN"])


def _owned_alias(alias_id):
    # Filtering by owner as well as id means one user can never touch another user's alias.
    alias = Alias.query.filter_by(uuid=alias_id, user_id=g.user.uuid).first()
    if alias is None:
        raise not_found("Alias not found")
    return alias


def _address_taken(local_part):
    return (
        db.session.get(RetiredAlias, local_part) is not None
        or Alias.query.filter_by(local_part=local_part).first() is not None
    )


def retire(aliases):
    """Delete aliases for good; their addresses stay reserved (see RetiredAlias)."""
    for alias in aliases:
        db.session.add(RetiredAlias(local_part=alias.local_part))
        db.session.delete(alias)


@bp.post("/generate")
@limiter.limit("30 per minute")
@login_required
def generate():
    for _ in range(5):
        local_part = generate_alias_local_part()
        if not _address_taken(local_part):
            break
    else:  # 36^10 possibilities: practically unreachable
        raise ApiError(503, "alias_unavailable", "Could not generate an address, please try again")

    # The address is handed out as a signed, short-lived token bound to this user, so the
    # client cannot pick an arbitrary address or reuse one generated for someone else.
    token = create_token(ALIAS, str(g.user.uuid), current_app.config["ALIAS_TOKEN_TTL"], alias=local_part)
    return jsonify(
        alias=local_part,
        address=f"{local_part}@{current_app.config['MAIL_DOMAIN']}",
        alias_token=token,
    )


@bp.get("")
@login_required
def list_aliases():
    status = request.args.get("status", "all")
    if status not in STATUS_FILTERS:
        raise bad_request("invalid_field", "status must be one of: all, active, inactive", field="status")
    query = Alias.query.filter_by(user_id=g.user.uuid)
    if STATUS_FILTERS[status] is not None:
        query = query.filter_by(is_active=STATUS_FILTERS[status])
    aliases = query.order_by(Alias.created_at.desc()).all()
    return jsonify(aliases=[_payload(alias) for alias in aliases])


@bp.post("")
@login_required
def create_alias():
    data = get_json()
    try:
        claims = decode_token(data.get("alias_token"), ALIAS)
    except jwt.InvalidTokenError:
        claims = None
    local_part = claims.get("alias") if claims else None
    if (
        not claims
        or claims["sub"] != str(g.user.uuid)
        or not isinstance(local_part, str)
        or not ALIAS_LOCAL_PART_RE.fullmatch(local_part)
    ):
        raise bad_request("invalid_alias_token", "The generated address has expired, please generate a new one")

    service = get_str(data, "service", max_length=MAX_SERVICE_LENGTH)
    service_url = normalize_service_url(data.get("service_url"))

    if db.session.get(RetiredAlias, local_part) is not None:  # deleted after the token was issued
        raise conflict("alias_taken", "This address has already been used, please generate a new one")

    limit = current_app.config["MAX_ALIASES_PER_USER"]
    if Alias.query.filter_by(user_id=g.user.uuid).count() >= limit:
        raise conflict("alias_limit", f"You can have at most {limit} addresses")

    alias = Alias(user_id=g.user.uuid, local_part=local_part, service=service, service_url=service_url)
    db.session.add(alias)
    try:
        db.session.commit()
    except IntegrityError:  # unique local_part: the token was already used
        db.session.rollback()
        raise conflict("alias_taken", "This address has already been used, please generate a new one")
    return jsonify(alias=_payload(alias)), 201


@bp.patch("/<uuid:alias_id>")
@login_required
def update_alias(alias_id):
    data = get_json()
    alias = _owned_alias(alias_id)
    if "service" in data:
        alias.service = get_str(data, "service", max_length=MAX_SERVICE_LENGTH)
    if "service_url" in data:
        alias.service_url = normalize_service_url(data.get("service_url"))
    db.session.commit()
    return jsonify(alias=_payload(alias))


@bp.post("/<uuid:alias_id>/deactivate")
@login_required
def deactivate_alias(alias_id):
    alias = _owned_alias(alias_id)
    if alias.is_active:
        alias.is_active = False
        alias.deactivated_at = utcnow()
        db.session.commit()
    return jsonify(alias=_payload(alias))


@bp.delete("/<uuid:alias_id>")
@login_required
def delete_alias(alias_id):
    alias = _owned_alias(alias_id)
    # Only deactivated aliases: an active one may still be in use, and a mistake here is final.
    if alias.is_active:
        raise conflict("alias_active", "Deactivate the address before deleting it")
    retire([alias])
    db.session.commit()
    return jsonify(message="Alias deleted")


@bp.post("/<uuid:alias_id>/activate")
@login_required
def activate_alias(alias_id):
    alias = _owned_alias(alias_id)
    if not alias.is_active:
        alias.is_active = True
        alias.deactivated_at = None
        db.session.commit()
    return jsonify(alias=_payload(alias))
