from conftest import PASSWORD, auth, create_alias, outbox, register, verify_email
from extensions import db
from models import Alias, RetiredAlias, User


def change_email(client, token, email, verification_token, password=PASSWORD):
    return client.post(
        "/api/account/email",
        headers=auth(token),
        json={"email": email, "verification_token": verification_token, "password": password},
    )


def test_change_email_requires_verification(client, app):
    token = register(client, app, email="old@example.com")
    response = change_email(client, token, "new@example.com", verification_token=None)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_verification"


def test_change_email_requires_password(client, app):
    token = register(client, app, email="old@example.com")
    verification = verify_email(client, app, "new@example.com", purpose="change_email")
    response = change_email(client, token, "new@example.com", verification, password="Wrong-pass1")
    assert response.status_code == 403  # not 401: the session itself is still valid
    assert response.get_json()["code"] == "invalid_password"


def test_change_email_success_notifies_old_address(client, app):
    token = register(client, app, email="old@example.com")
    verification = verify_email(client, app, "new@example.com", purpose="change_email")
    response = change_email(client, token, "New@Example.com", verification)
    assert response.status_code == 200
    assert response.get_json()["user"]["email"] == "new@example.com"
    assert any(m["to"] == "old@example.com" and "new@example.com" in m["body"] for m in outbox(app))


def test_change_email_to_an_address_in_use(client, app):
    register(client, app, email="taken@example.com")
    token = register(client, app, email="me@example.com")
    # The code request for a registered address only sends a notice; simulate an owner who
    # verified the address before it got taken by issuing the token directly.
    from security import issue_verification_token

    response = change_email(
        client, token, "taken@example.com", issue_verification_token("taken@example.com", "change_email")
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "email_taken"


def test_change_password_revokes_other_sessions(client, app):
    first_session = register(client, app)
    response = client.post(
        "/api/account/password",
        headers=auth(first_session),
        json={"current_password": PASSWORD, "new_password": "An0ther-pass"},
    )
    assert response.status_code == 200
    assert client.get("/api/account", headers=auth(first_session)).status_code == 401
    assert client.get("/api/account", headers=auth(response.get_json()["token"])).status_code == 200


def test_change_password_checks_current_password(client, app):
    token = register(client, app)
    response = client.post(
        "/api/account/password",
        headers=auth(token),
        json={"current_password": "Wrong-pass1", "new_password": "An0ther-pass"},
    )
    assert response.status_code == 403


def test_logout_everywhere(client, app):
    token = register(client, app)
    assert client.post("/api/account/logout-all", headers=auth(token)).status_code == 200
    assert client.get("/api/account", headers=auth(token)).status_code == 401


def test_delete_account_requires_password(client, app):
    token = register(client, app)
    response = client.delete("/api/account", headers=auth(token), json={"password": "Wrong-pass1"})
    assert response.status_code == 403
    assert db.session.query(User).count() == 1


def test_delete_account_removes_everything(client, app):
    token = register(client, app, email="gone@example.com")
    gone = {create_alias(client, token)["alias"], create_alias(client, token)["alias"]}
    keeper = register(client, app, email="keeper@example.com")
    create_alias(client, keeper)

    response = client.delete("/api/account", headers=auth(token), json={"password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/account", headers=auth(token)).status_code == 401
    assert db.session.query(User).count() == 1
    assert db.session.query(Alias).count() == 1  # only the other user's alias is left
    # the addresses stay reserved, without any link to the deleted account
    assert {r.local_part for r in db.session.query(RetiredAlias)} == gone
    assert any(m["to"] == "gone@example.com" and "удалён" in m["subject"] for m in outbox(app))


def test_wrong_password_reentries_are_limited_per_account_across_ips(app_factory):
    app = app_factory(RATELIMIT_ENABLED=True)
    client = app.test_client()
    token = register(client, app)

    def from_ip(i):
        return {"headers": auth(token), "environ_base": {"REMOTE_ADDR": f"198.51.100.{i}"}}

    wrong_delete = {"password": "Wrong-pass1"}
    wrong_change = {"current_password": "Wrong-pass1", "new_password": "An0ther-pass!"}
    statuses = [
        client.delete("/api/account", json=wrong_delete, **from_ip(i)).status_code
        if i % 2
        else client.post("/api/account/password", json=wrong_change, **from_ip(i)).status_code
        for i in range(20)
    ]
    assert statuses == [403] * 20
    # one counter for every endpoint that asks for the password; it holds even for the right one
    response = client.delete("/api/account", json={"password": PASSWORD}, **from_ip(99))
    assert response.status_code == 429
    assert db.session.query(User).count() == 1

    other = register(client, app, email="other@example.com")
    response = client.post(
        "/api/account/password",
        headers=auth(other),
        json={"current_password": PASSWORD, "new_password": "An0ther-pass!"},
        environ_base={"REMOTE_ADDR": "198.51.100.98"},
    )
    assert response.status_code == 200  # other accounts are unaffected
