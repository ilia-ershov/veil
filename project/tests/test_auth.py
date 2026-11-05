import re
from datetime import timedelta

import bcrypt
import jwt
import pytest
from sqlalchemy import inspect, text

from conftest import PASSWORD, auth, last_code, outbox, register, verify_email
from database import init_db
from extensions import db
from models import EmailVerification, User, utcnow
from security import create_token, issue_verification_token, verify_password


def test_full_signup_and_login_flow(client, app):
    token = register(client, app, email="New.User@Example.com")

    me = client.get("/api/account", headers=auth(token)).get_json()["user"]
    assert me["email"] == "new.user@example.com"  # stored normalised
    assert me["mfa_enabled"] is False

    response = client.post("/api/auth/login", json={"email": "NEW.USER@example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/account", headers=auth(response.get_json()["token"])).status_code == 200


def test_register_without_verification_is_rejected(client):
    response = client.post(
        "/api/auth/register", json={"name": "x", "email": "victim@example.com", "password": PASSWORD}
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_verification"


def test_confirmed_suffix_cache_collision_is_gone(client, app):
    """The original code accepted a code request for '<victim>_confirmed' as proof of '<victim>'."""
    client.post("/api/auth/request-code", json={"email": "victim@example.com_confirmed"})
    response = client.post(
        "/api/auth/register",
        json={"name": "x", "email": "victim@example.com", "password": PASSWORD, "verification_token": "123456"},
    )
    assert response.status_code == 400
    assert db.session.query(User).count() == 0


def test_verification_token_is_bound_to_email_and_purpose(client, app):
    token = verify_email(client, app, "a@example.com")
    other_email = client.post(
        "/api/auth/register",
        json={"name": "x", "email": "b@example.com", "password": PASSWORD, "verification_token": token},
    )
    assert other_email.status_code == 400

    register(client, app, email="owner@example.com")
    register_token = verify_email(client, app, "fresh@example.com")
    wrong_purpose = client.post(
        "/api/auth/reset-password",
        json={"email": "fresh@example.com", "password": PASSWORD, "verification_token": register_token},
    )
    assert wrong_purpose.status_code == 400


def test_other_token_types_are_not_access_tokens(client, app):
    verification = verify_email(client, app, "a@example.com")
    assert client.get("/api/account", headers=auth(verification)).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer",
        "Bearer not-a-jwt",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_bad_authorization_headers(client, header):
    headers = {"Authorization": header} if header else {}
    response = client.get("/api/account", headers=headers)
    assert response.status_code == 401
    assert response.get_json()["code"] in {"token_missing", "token_invalid"}


def test_forged_expired_and_unsigned_tokens_are_rejected(client, app):
    token = register(client, app)
    claims = jwt.decode(token, options={"verify_signature": False})

    forged = jwt.encode(claims, "some-other-key-that-is-long-enough-123456", algorithm="HS256")
    assert client.get("/api/account", headers=auth(forged)).status_code == 401

    unsigned = jwt.encode(claims, None, algorithm="none")
    assert client.get("/api/account", headers=auth(unsigned)).status_code == 401

    expired = create_token("access", claims["sub"], timedelta(seconds=-1), ver=0)
    response = client.get("/api/account", headers=auth(expired))
    assert response.status_code == 401
    assert response.get_json()["code"] == "token_expired"


def test_code_is_single_use_and_stored_hashed(client, app):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    code = last_code(app, "a@example.com")
    record = db.session.get(EmailVerification, ("a@example.com", "register"))
    assert code not in record.code_hash

    assert client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": code}).status_code == 200
    again = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": code})
    assert again.status_code == 400


def test_code_guessing_is_limited(client, app):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    code = last_code(app, "a@example.com")
    wrong = "000000" if code != "000000" else "111111"
    for attempt in range(4):
        response = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": wrong})
        assert response.get_json()["code"] == "invalid_code"
        assert response.get_json()["attempts_left"] == 4 - attempt
    response = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": wrong})
    assert response.get_json()["code"] == "too_many_attempts"
    # the code is burnt: even the right one no longer works
    response = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": code})
    assert response.status_code == 400


def test_expired_code_is_rejected(client, app):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    code = last_code(app, "a@example.com")
    db.session.get(EmailVerification, ("a@example.com", "register")).expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    response = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": code})
    assert response.status_code == 400


def test_a_request_for_another_purpose_keeps_the_pending_code(client, app):
    register(client, app, email="a@example.com")
    client.post("/api/auth/request-code", json={"email": "a@example.com", "purpose": "reset_password"})
    code = last_code(app, "a@example.com")
    # anyone can ask for a registration code for the same address; the owner's reset code survives
    client.post("/api/auth/request-code", json={"email": "a@example.com", "purpose": "register"})
    response = client.post(
        "/api/auth/verify-code", json={"email": "a@example.com", "code": code, "purpose": "reset_password"}
    )
    assert response.status_code == 200
    assert response.get_json()["purpose"] == "reset_password"


def test_code_only_verifies_for_its_purpose(client, app):
    client.post("/api/auth/request-code", json={"email": "a@example.com", "purpose": "change_email"})
    code = last_code(app, "a@example.com")
    response = client.post("/api/auth/verify-code", json={"email": "a@example.com", "code": code})
    assert response.status_code == 400
    response = client.post(
        "/api/auth/verify-code", json={"email": "a@example.com", "code": code, "purpose": "change_email"}
    )
    assert response.status_code == 200


def test_resend_cooldown(app_factory):
    app = app_factory(CODE_RESEND_COOLDOWN=timedelta(seconds=60))
    client = app.test_client()
    assert client.post("/api/auth/request-code", json={"email": "a@example.com"}).status_code == 200
    response = client.post("/api/auth/request-code", json={"email": "a@example.com"})
    assert response.status_code == 429
    assert response.get_json()["code"] == "resend_too_soon"
    assert 0 < response.get_json()["retry_after"] <= 60


def test_code_request_does_not_reveal_registered_addresses(client, app):
    register(client, app, email="taken@example.com")
    outbox(app).clear()

    taken = client.post("/api/auth/request-code", json={"email": "taken@example.com"})
    free = client.post("/api/auth/request-code", json={"email": "free@example.com"})
    assert taken.status_code == free.status_code == 200
    assert taken.get_json() == free.get_json()

    to_taken = [m for m in outbox(app) if m["to"] == "taken@example.com"]
    assert len(to_taken) == 1 and not re.search(r"\b\d{6}\b", to_taken[0]["body"])  # a notice, no code

    # password reset for an unknown address answers the same and sends a notice without a code;
    # sending nothing would make the answer measurably faster than for a registered address
    outbox(app).clear()
    response = client.post("/api/auth/request-code", json={"email": "nobody@example.com", "purpose": "reset_password"})
    assert response.status_code == 200
    assert response.get_json() == taken.get_json()
    assert [m["to"] for m in outbox(app)] == ["nobody@example.com"]
    assert not re.search(r"\b\d{6}\b", outbox(app)[0]["body"])


@pytest.mark.parametrize(
    "password",
    ["Sh0rt!", "alllowercase1!", "ALLUPPERCASE1!", "NoDigitsHere!", "NoSpecial123", "Aa1!" + "x" * 70],
)
def test_weak_passwords_are_rejected_by_the_server(client, app, password):
    token = verify_email(client, app, "a@example.com")
    response = client.post(
        "/api/auth/register",
        json={"name": "x", "email": "a@example.com", "password": password, "verification_token": token},
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "weak_password"


def test_duplicate_registration(client, app):
    register(client, app, email="a@example.com")
    # A code request for a registered address only sends a notice, so a second verification
    # can only come from a race; issue the token directly to exercise the server-side check.
    token = issue_verification_token("a@example.com", "register")
    response = client.post(
        "/api/auth/register",
        json={"name": "x", "email": "a@example.com", "password": PASSWORD, "verification_token": token},
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "email_taken"


@pytest.mark.parametrize(
    "email",
    ["root,me@gmail.com", "a;b@example.com", '"a"<x>@example.com', "a b@example.com", "a(c)@example.com", "a@x"],
)
def test_addresses_mail_software_would_split_are_rejected(client, email):
    # Postfix reads a comma in the main address as a second recipient (root@<server domain>)
    response = client.post("/api/auth/request-code", json={"email": email})
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_email"


@pytest.mark.parametrize("email", ["first.last+tag@example.co.uk", "o'brien@example.io", "иван@почта.рф"])
def test_ordinary_addresses_are_accepted(client, email):
    assert client.post("/api/auth/request-code", json={"email": email}).status_code == 200


@pytest.mark.parametrize("address", ["abc@{domain}", "abc@sub.{domain}", "ABC@{domain_upper}"])
def test_alias_domain_cannot_be_main_address(client, app, address):
    domain = app.config["MAIL_DOMAIN"]
    email = address.format(domain=domain, domain_upper=domain.upper())
    response = client.post("/api/auth/request-code", json={"email": email})
    assert response.status_code == 400
    assert response.get_json()["code"] == "alias_domain_email"


def test_login_failures_look_the_same(client, app):
    register(client, app, email="a@example.com")
    wrong_password = client.post("/api/auth/login", json={"email": "a@example.com", "password": "Wrong1!pass"})
    unknown_user = client.post("/api/auth/login", json={"email": "b@example.com", "password": "Wrong1!pass"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.get_json() == unknown_user.get_json()


def test_password_reset_signs_out_existing_sessions(client, app):
    old_session = register(client, app, email="a@example.com")
    token = verify_email(client, app, "a@example.com", purpose="reset_password")
    response = client.post(
        "/api/auth/reset-password",
        json={"email": "a@example.com", "password": "N3w-password", "verification_token": token},
    )
    assert response.status_code == 200
    assert client.get("/api/account", headers=auth(old_session)).status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD}).status_code == 401
    assert (
        client.post("/api/auth/login", json={"email": "a@example.com", "password": "N3w-password"}).status_code == 200
    )


def test_password_reset_token_works_once(client, app):
    register(client, app, email="a@example.com")
    token = verify_email(client, app, "a@example.com", purpose="reset_password")
    payload = {"email": "a@example.com", "password": "N3w-password", "verification_token": token}
    assert client.post("/api/auth/reset-password", json=payload).status_code == 200

    payload["password"] = "An0ther-password"
    response = client.post("/api/auth/reset-password", json=payload)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_verification"
    assert (
        client.post("/api/auth/login", json={"email": "a@example.com", "password": "N3w-password"}).status_code == 200
    )


def test_hashes_from_the_original_passlib_version_still_verify(app):
    legacy_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    assert verify_password(PASSWORD, legacy_hash)
    assert not verify_password("wrong", legacy_hash)
    # passlib truncated passwords to 72 bytes; such accounts must keep working with bcrypt 5
    long_password = "Aa1!" + "x" * 80
    truncated_hash = bcrypt.hashpw(long_password.encode()[:72], bcrypt.gensalt(4)).decode()
    assert verify_password(long_password, truncated_hash)


@pytest.mark.parametrize("body", [None, "[]", '"text"', "{not json"])
def test_non_object_bodies_are_rejected(client, body):
    response = client.post("/api/auth/login", data=body, content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_request"


def test_oversized_body_is_rejected(client):
    response = client.post("/api/auth/login", json={"email": "a@example.com", "password": "x" * 20000})
    assert response.status_code == 413
    assert response.get_json()["code"] == "payload_too_large"


def test_old_code_table_keyed_by_address_only_is_upgraded(app):
    EmailVerification.__table__.drop(db.engine)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE email_verifications (email VARCHAR(255) PRIMARY KEY, purpose VARCHAR(32) NOT NULL, "
                "code_hash VARCHAR(64) NOT NULL, attempts INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, "
                "expires_at TIMESTAMP NOT NULL)"
            )
        )
    init_db()
    pk = inspect(db.engine).get_pk_constraint("email_verifications")["constrained_columns"]
    assert sorted(pk) == ["email", "purpose"]
    init_db()  # idempotent
