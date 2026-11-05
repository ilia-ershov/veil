"""Migration from the original schema (PostgreSQL only: the legacy tables used JSONB)."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text

from conftest import PASSWORD, TEST_DATABASE_URL
from database import migrate_legacy
from extensions import db
from models import Alias, RetiredAlias, User
from security import hash_password

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"), reason="legacy schema used PostgreSQL JSONB columns"
)


@pytest.fixture
def legacy_db(app):
    """Recreate the original schema: users without the new columns, plus emails/deactivated."""
    with db.engine.begin() as connection:
        connection.execute(
            text("DROP TABLE IF EXISTS aliases, email_verifications, emails, deactivated, users CASCADE")
        )
        connection.execute(
            text(
                "CREATE TABLE users (uuid UUID PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                "email VARCHAR(255) UNIQUE NOT NULL, password_hash VARCHAR(128) NOT NULL)"
            )
        )
        for table in ("emails", "deactivated"):
            connection.execute(
                text(
                    f"CREATE TABLE {table} (uuid UUID PRIMARY KEY, data JSONB, "
                    "user_id UUID NOT NULL REFERENCES users(uuid))"
                )
            )
    alice, bob = uuid4(), uuid4()
    rows = {
        "users": [(alice, "Alice@Example.com"), (bob, "bob@example.com")],
        "emails": [
            (alice, ["abc123def4", "GitHub", "github.com", "sig"]),
            (alice, ["abc123def4", "GitHub", "github.com", "sig"]),  # duplicate the old API allowed
            (bob, ["abc123def4", "Copy", "", "sig"]),  # same alias claimed by another user
            (bob, ["zzz999yyy8", "Shop", "javascript:alert(1)", "sig"]),
            (bob, ["../../etc", "Bad", "", "sig"]),  # invalid local part
        ],
        "deactivated": [(alice, {"email": "old0000000", "service": "Forum", "url": "https://forum.example"})],
    }
    with db.engine.begin() as connection:
        for user_id, email in rows["users"]:
            connection.execute(
                text("INSERT INTO users VALUES (:u, 'x', :e, :p)"),
                {"u": user_id, "e": email, "p": hash_password(PASSWORD)},
            )
        for table in ("emails", "deactivated"):
            for user_id, data in rows[table]:
                connection.execute(
                    text(f"INSERT INTO {table} VALUES (:id, CAST(:d AS JSONB), :u)"),
                    {"id": uuid4(), "d": json.dumps(data), "u": user_id},
                )
    yield alice, bob
    db.session.remove()  # release the test's row locks before dropping tables
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS emails, deactivated CASCADE"))


def test_migrate_legacy_schema(app, legacy_db):
    alice, bob = legacy_db
    report = migrate_legacy(drop=True, force=True)

    assert report["emails_lowercased"] == 1
    assert report["copied"] == 3
    assert report["already_present"] == 2
    assert report["owner_conflicts"] == [{"alias": "abc123def4", "kept_owner": str(alice), "user_id": str(bob)}]
    assert len(report["skipped"]) == 1
    assert {"emails", "deactivated"}.isdisjoint(inspect(db.engine).get_table_names())

    aliases = {a.local_part: a for a in db.session.query(Alias).all()}
    assert aliases["abc123def4"].user_id == alice and aliases["abc123def4"].service_url == "https://github.com"
    assert aliases["zzz999yyy8"].service_url == ""  # unsafe link dropped
    assert aliases["old0000000"].is_active is False
    assert db.session.get(User, alice).email == "alice@example.com"
    assert db.session.get(User, alice).token_version == 0  # new column added with its default


def test_migration_refuses_to_drop_when_rows_were_skipped(app, legacy_db):
    with pytest.raises(RuntimeError, match="could not be migrated"):
        migrate_legacy(drop=True)
    # the whole transaction was rolled back
    assert {"emails", "deactivated"} <= set(inspect(db.engine).get_table_names())
    assert db.session.query(Alias).count() == 0


def test_login_works_after_migration(app, client, legacy_db):
    migrate_legacy()
    response = client.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert response.status_code == 200


def test_a_rerun_does_not_bring_back_deleted_aliases(app, legacy_db):
    migrate_legacy()
    deleted = db.session.query(Alias).filter_by(local_part="old0000000").one()
    db.session.add(RetiredAlias(local_part=deleted.local_part))  # what DELETE /api/aliases/<id> does
    db.session.delete(deleted)
    db.session.commit()

    report = migrate_legacy()
    assert report["copied"] == 0
    assert report["deleted_since"] == 1
    assert db.session.query(Alias).filter_by(local_part="old0000000").count() == 0
