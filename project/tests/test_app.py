"""Cross-cutting behaviour: configuration checks, error format, headers, CORS and rate limits."""

import pytest

from config import load_config
from conftest import make_app


@pytest.mark.parametrize("secret", ["", "your_secret_key", "short-secret"])
def test_weak_secret_key_is_refused(secret):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        make_app(SECRET_KEY=secret)


def test_missing_database_url_is_refused():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        make_app(SQLALCHEMY_DATABASE_URI="")


def test_a_malformed_number_names_its_setting(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "two")
    with pytest.raises(RuntimeError, match="TRUST_PROXY"):
        load_config()


def test_unknown_mail_backend_is_refused():
    with pytest.raises(RuntimeError, match="'smpt'"):
        make_app(MAIL_BACKEND="smpt")


def test_health(client):
    assert client.get("/api/health").get_json() == {"status": "ok"}


def test_errors_are_json_without_internals(client):
    for response in (client.get("/api/nope"), client.get("/api/auth/login")):
        assert response.is_json
        assert set(response.get_json()) == {"error", "code"}
    assert client.get("/api/auth/login").status_code == 405


def test_security_headers(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_cors_is_off_by_default(client):
    response = client.options(
        "/api/aliases",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "Access-Control-Allow-Origin" not in response.headers


def test_cors_allows_only_configured_origins(app_factory):
    client = app_factory(CORS_ORIGINS=["https://app.example"]).test_client()
    preflight = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    allowed = client.options("/api/aliases", headers={"Origin": "https://app.example", **preflight})
    denied = client.options("/api/aliases", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.headers.get("Access-Control-Allow-Origin") == "https://app.example"
    assert "Access-Control-Allow-Origin" not in denied.headers
    assert "Access-Control-Allow-Credentials" not in allowed.headers


def test_failed_logins_are_rate_limited(app_factory):
    client = app_factory(RATELIMIT_ENABLED=True).test_client()
    body = {"email": "a@example.com", "password": "Wrong-pass1"}
    statuses = [client.post("/api/auth/login", json=body).status_code for _ in range(11)]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
    response = client.post("/api/auth/login", json=body)
    assert response.get_json()["code"] == "rate_limited"


def test_failed_logins_are_limited_per_account_across_ips(app_factory):
    client = app_factory(RATELIMIT_ENABLED=True).test_client()
    body = {"email": "a@example.com", "password": "Wrong-pass1"}
    statuses = [
        client.post("/api/auth/login", json=body, environ_base={"REMOTE_ADDR": f"198.51.100.{i}"}).status_code
        for i in range(21)
    ]
    assert statuses == [401] * 20 + [429]
    # other accounts are unaffected
    other = {"email": "b@example.com", "password": "Wrong-pass1"}
    assert client.post("/api/auth/login", json=other, environ_base={"REMOTE_ADDR": "198.51.100.99"}).status_code == 401


def test_code_requests_are_rate_limited(app_factory):
    client = app_factory(RATELIMIT_ENABLED=True).test_client()
    statuses = [
        client.post("/api/auth/request-code", json={"email": f"user{i}@example.com"}).status_code for i in range(6)
    ]
    assert statuses == [200] * 5 + [429]


def test_code_requests_are_limited_per_address_across_ips(app_factory):
    client = app_factory(RATELIMIT_ENABLED=True).test_client()

    def request_code(email, ip):
        return client.post(
            "/api/auth/request-code", json={"email": email}, environ_base={"REMOTE_ADDR": ip}
        ).status_code

    statuses = [request_code("victim@example.com", f"198.51.100.{i}") for i in range(11)]
    assert statuses == [200] * 10 + [429]
    assert request_code("other@example.com", "198.51.100.99") == 200  # other addresses are unaffected


def test_rate_limits_keep_working_while_redis_is_down(app_factory):
    # Nothing listens on port 1, so every storage call fails: the limiter has to fall back to memory
    # instead of turning each login into a 500.
    client = app_factory(RATELIMIT_ENABLED=True, RATELIMIT_STORAGE_URI="redis://127.0.0.1:1/0").test_client()
    body = {"email": "a@example.com", "password": "Wrong-pass1"}
    statuses = [client.post("/api/auth/login", json=body).status_code for _ in range(11)]
    assert statuses == [401] * 10 + [429]
