"""Schema creation/upgrade and the one-off migration from the original (legacy) schema."""

from uuid import uuid4

from sqlalchemy import insert, inspect, select, text

from errors import ApiError
from extensions import db
from models import Alias, EmailVerification, RetiredAlias, utcnow
from validation import ALIAS_LOCAL_PART_RE, MAX_SERVICE_LENGTH, normalize_service_url

# Columns added to the pre-existing `users` table; create_all() never alters existing tables.
USER_COLUMNS = {
    "token_version": "token_version INTEGER NOT NULL DEFAULT 0",
    "totp_secret": "totp_secret VARCHAR(64)",
    "totp_last_step": "totp_last_step INTEGER",
    "recovery_codes": "recovery_codes JSON",
}
LEGACY_TABLES = (("emails", True), ("deactivated", False))


def init_db():
    """Create missing tables and columns. Safe to run on every start."""
    _upgrade_email_verifications()
    db.create_all()
    existing = {column["name"] for column in inspect(db.engine).get_columns("users")}
    missing = [ddl for name, ddl in USER_COLUMNS.items() if name not in existing]
    if missing:
        with db.engine.begin() as connection:
            for ddl in missing:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {ddl}"))


def _upgrade_email_verifications():
    """Pending codes used to be keyed by address alone; now by (address, purpose).

    The table only holds codes that expire within minutes, so it is simply recreated:
    anyone with a code in flight requests a new one.
    """
    inspector = inspect(db.engine)
    if "email_verifications" not in inspector.get_table_names():
        return
    if inspector.get_pk_constraint("email_verifications")["constrained_columns"] == ["email"]:
        EmailVerification.__table__.drop(db.engine)


def _parse_legacy(data):
    """Legacy rows stored [local_part, service, url, signature] (or occasionally a dict) as JSON."""
    if isinstance(data, list) and data:
        local, service, url = (list(data) + ["", "", ""])[:3]
    elif isinstance(data, dict):
        local = data.get("email") or data.get("local")
        service = data.get("service") or data.get("name")
        url = data.get("serviceUrl") or data.get("url")
    else:
        return None
    local = str(local or "").strip().lower()
    if not ALIAS_LOCAL_PART_RE.fullmatch(local):
        return None
    service = (str(service or "").strip() or "Без названия")[:MAX_SERVICE_LENGTH]
    try:
        url = normalize_service_url(str(url or ""))
    except ApiError:
        url = ""
    return local, service, url


def migrate_legacy(drop=False, force=False):
    """Copy aliases from the legacy `emails`/`deactivated` tables into `aliases`.

    Runs in one transaction: on any error nothing changes. Re-running is safe: addresses that
    were already migrated are skipped, and so are those deleted since (they stay retired).
    With drop=True the legacy tables are removed afterwards.
    """
    init_db()
    tables = set(inspect(db.engine).get_table_names())
    report = {
        "emails_lowercased": 0,
        "email_conflicts": [],
        "copied": 0,
        "already_present": 0,
        "deleted_since": 0,
        "owner_conflicts": [],
        "skipped": [],
    }

    with db.engine.begin() as connection:
        # 1. Normalise main addresses to lower case (lookups are now exact-match on the unique index).
        users = connection.execute(text("SELECT uuid, email FROM users")).all()
        taken = {email for _, email in users}
        for user_id, email in users:
            normalized = email.strip().lower()
            if normalized == email:
                continue
            if normalized in taken:
                report["email_conflicts"].append(email)
                continue
            connection.execute(text("UPDATE users SET email = :e WHERE uuid = :u"), {"e": normalized, "u": user_id})
            taken.discard(email)
            taken.add(normalized)
            report["emails_lowercased"] += 1

        # 2. Copy aliases.
        owners = dict(connection.execute(select(Alias.local_part, Alias.user_id)).all())
        retired = set(connection.execute(select(RetiredAlias.local_part)).scalars())
        now = utcnow()
        for table, active in LEGACY_TABLES:
            if table not in tables:
                continue
            for user_id, data in connection.execute(text(f"SELECT user_id, data FROM {table}")):
                parsed = _parse_legacy(data)
                if parsed is None:
                    report["skipped"].append({"table": table, "user_id": str(user_id), "data": data})
                    continue
                local, service, url = parsed
                if local in retired:  # deleted by its owner after an earlier run: never bring it back
                    report["deleted_since"] += 1
                    continue
                if local in owners:
                    # The old API let the same signed address be added repeatedly, even by different users.
                    if owners[local] != user_id:
                        report["owner_conflicts"].append(
                            {"alias": local, "kept_owner": str(owners[local]), "user_id": str(user_id)}
                        )
                    report["already_present"] += 1
                    continue
                connection.execute(
                    insert(Alias.__table__).values(
                        uuid=uuid4(),
                        user_id=user_id,
                        local_part=local,
                        service=service,
                        service_url=url,
                        is_active=active,
                        created_at=now,
                        deactivated_at=None if active else now,
                    )
                )
                owners[local] = user_id
                report["copied"] += 1

        if drop:
            if report["skipped"] and not force:
                raise RuntimeError(
                    f"{len(report['skipped'])} legacy rows could not be migrated; "
                    "inspect them and re-run with --force to drop the legacy tables anyway"
                )
            for table, _ in LEGACY_TABLES:
                if table in tables:
                    connection.execute(text(f"DROP TABLE {table}"))
            report["dropped"] = [table for table, _ in LEGACY_TABLES if table in tables]
    return report
