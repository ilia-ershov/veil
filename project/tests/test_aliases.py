from datetime import timedelta
from uuid import UUID

import pytest

from conftest import auth, create_alias, register
from extensions import db
from models import Alias, RetiredAlias
from security import ALIAS, create_token


def test_generate_requires_login(client):
    assert client.post("/api/aliases/generate").status_code == 401


def test_create_and_list_aliases_newest_first(client, app):
    token = register(client, app)
    first = create_alias(client, token, service="GitHub", service_url="github.com")
    second = create_alias(client, token, service="Shop", service_url="")

    assert first["address"] == f"{first['alias']}@{app.config['MAIL_DOMAIN']}"
    assert first["service_url"] == "https://github.com"
    assert len(first["alias"]) == 10 and first["alias"].isalnum()

    listed = client.get("/api/aliases", headers=auth(token)).get_json()["aliases"]
    assert [a["id"] for a in listed] == [second["id"], first["id"]]


def test_alias_token_cannot_be_replayed(client, app):
    token = register(client, app)
    generated = client.post("/api/aliases/generate", headers=auth(token)).get_json()
    body = {"alias_token": generated["alias_token"], "service": "x"}
    assert client.post("/api/aliases", headers=auth(token), json=body).status_code == 201
    replay = client.post("/api/aliases", headers=auth(token), json=body)
    assert replay.status_code == 409
    assert replay.get_json()["code"] == "alias_taken"


def test_alias_token_is_bound_to_its_user(client, app):
    alice = register(client, app, email="alice@example.com")
    bob = register(client, app, email="bob@example.com")
    generated = client.post("/api/aliases/generate", headers=auth(alice)).get_json()
    response = client.post(
        "/api/aliases", headers=auth(bob), json={"alias_token": generated["alias_token"], "service": "x"}
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_alias_token"


def test_client_cannot_choose_or_forge_the_address(client, app):
    token = register(client, app)
    for alias_token in [None, "abc", "admin", create_token("access", "x", timedelta(minutes=1))]:
        response = client.post("/api/aliases", headers=auth(token), json={"alias_token": alias_token, "service": "x"})
        assert response.status_code == 400


def test_expired_alias_token(client, app):
    token = register(client, app)
    user_id = client.get("/api/account", headers=auth(token)).get_json()["user"]["id"]
    expired = create_token(ALIAS, user_id, timedelta(seconds=-1), alias="abcdefghij")
    response = client.post("/api/aliases", headers=auth(token), json={"alias_token": expired, "service": "x"})
    assert response.status_code == 400


def test_deactivate_and_reactivate(client, app):
    token = register(client, app)
    alias = create_alias(client, token)

    response = client.post(f"/api/aliases/{alias['id']}/deactivate", headers=auth(token))
    assert response.status_code == 200
    assert response.get_json()["alias"]["active"] is False
    assert response.get_json()["alias"]["deactivated_at"]
    # idempotent
    assert client.post(f"/api/aliases/{alias['id']}/deactivate", headers=auth(token)).status_code == 200

    active = client.get("/api/aliases?status=active", headers=auth(token)).get_json()["aliases"]
    inactive = client.get("/api/aliases?status=inactive", headers=auth(token)).get_json()["aliases"]
    assert active == [] and [a["id"] for a in inactive] == [alias["id"]]

    response = client.post(f"/api/aliases/{alias['id']}/activate", headers=auth(token))
    assert response.get_json()["alias"]["active"] is True
    assert response.get_json()["alias"]["deactivated_at"] is None


def test_invalid_status_filter(client, app):
    token = register(client, app)
    assert client.get("/api/aliases?status=deleted", headers=auth(token)).status_code == 400


def test_users_cannot_touch_each_others_aliases(client, app):
    alice = register(client, app, email="alice@example.com")
    bob = register(client, app, email="bob@example.com")
    alias = create_alias(client, alice)

    assert client.get("/api/aliases", headers=auth(bob)).get_json()["aliases"] == []
    for action in ("deactivate", "activate"):
        assert client.post(f"/api/aliases/{alias['id']}/{action}", headers=auth(bob)).status_code == 404
    assert client.patch(f"/api/aliases/{alias['id']}", headers=auth(bob), json={"service": "pwned"}).status_code == 404
    assert db.session.get(Alias, UUID(alias["id"])).service == "GitHub"


def test_edit_service(client, app):
    token = register(client, app)
    alias = create_alias(client, token)
    response = client.patch(
        f"/api/aliases/{alias['id']}",
        headers=auth(token),
        json={"service": "GitLab", "service_url": "https://gitlab.com/x"},
    )
    assert response.status_code == 200
    assert response.get_json()["alias"]["service"] == "GitLab"
    assert response.get_json()["alias"]["service_url"] == "https://gitlab.com/x"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com",
        "https://user@evil.example",
        "https://exa mple.com",
        "not a url",
        "https://" + "a" * 2050 + ".com",
    ],
)
def test_dangerous_service_urls_are_rejected(client, app, url):
    token = register(client, app)
    alias = create_alias(client, token)
    response = client.patch(f"/api/aliases/{alias['id']}", headers=auth(token), json={"service_url": url})
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_url"


def test_service_is_required_and_bounded(client, app):
    token = register(client, app)
    generated = client.post("/api/aliases/generate", headers=auth(token)).get_json()
    for service in ["", "   ", "x" * 101, 123]:
        response = client.post(
            "/api/aliases", headers=auth(token), json={"alias_token": generated["alias_token"], "service": service}
        )
        assert response.status_code == 400


def test_alias_limit(app_factory):
    app = app_factory(MAX_ALIASES_PER_USER=2)
    client = app.test_client()
    token = register(client, app)
    create_alias(client, token)
    create_alias(client, token)
    generated = client.post("/api/aliases/generate", headers=auth(token)).get_json()
    response = client.post(
        "/api/aliases", headers=auth(token), json={"alias_token": generated["alias_token"], "service": "x"}
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "alias_limit"


def test_unknown_alias_id(client, app):
    token = register(client, app)
    response = client.post("/api/aliases/00000000-0000-0000-0000-000000000000/activate", headers=auth(token))
    assert response.status_code == 404
    assert client.post("/api/aliases/not-a-uuid/activate", headers=auth(token)).status_code == 404


def test_delete_deactivated_alias_frees_the_limit(app_factory):
    app = app_factory(MAX_ALIASES_PER_USER=1)
    client = app.test_client()
    token = register(client, app)
    alias = create_alias(client, token)

    response = client.delete(f"/api/aliases/{alias['id']}", headers=auth(token))
    assert response.status_code == 409
    assert response.get_json()["code"] == "alias_active"

    client.post(f"/api/aliases/{alias['id']}/deactivate", headers=auth(token))
    assert client.delete(f"/api/aliases/{alias['id']}", headers=auth(token)).status_code == 200
    assert client.get("/api/aliases", headers=auth(token)).get_json()["aliases"] == []
    create_alias(client, token)  # the limit counts the remaining aliases only


def test_deleted_address_is_never_handed_out_again(client, app, monkeypatch):
    token = register(client, app)
    alias = create_alias(client, token)
    client.post(f"/api/aliases/{alias['id']}/deactivate", headers=auth(token))
    monkeypatch.setattr("api.aliases.generate_alias_local_part", lambda: alias["alias"])
    stale = create_token(ALIAS, alias_owner(client, token), timedelta(minutes=5), alias=alias["alias"])
    client.delete(f"/api/aliases/{alias['id']}", headers=auth(token))

    assert db.session.get(RetiredAlias, alias["alias"]) is not None
    # the generator skips retired addresses (here it can only produce that one)
    assert client.post("/api/aliases/generate", headers=auth(token)).status_code == 503
    # and a token issued for it before the deletion no longer creates it
    response = client.post("/api/aliases", headers=auth(token), json={"alias_token": stale, "service": "x"})
    assert response.status_code == 409
    assert response.get_json()["code"] == "alias_taken"


def test_users_cannot_delete_each_others_aliases(client, app):
    alice = register(client, app, email="alice@example.com")
    bob = register(client, app, email="bob@example.com")
    alias = create_alias(client, alice)
    client.post(f"/api/aliases/{alias['id']}/deactivate", headers=auth(alice))
    assert client.delete(f"/api/aliases/{alias['id']}", headers=auth(bob)).status_code == 404
    assert db.session.get(Alias, UUID(alias["id"])) is not None


def alias_owner(client, token):
    return client.get("/api/account", headers=auth(token)).get_json()["user"]["id"]
