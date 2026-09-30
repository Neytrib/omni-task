from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from conftest import ROOT
from sqlalchemy import func, select


def test_migration_matches_models(db_engine, database_url):
    configuration = Config(str(ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    with db_engine.connect() as connection:
        configuration.attributes["connection"] = connection
        command.check(configuration)


def test_business_transaction_rolls_back_task_receipt_revision_and_event(
    app, users, db_engine, db_tables
):
    from app.services import create_task

    with pytest.raises(RuntimeError, match="synthetic failure"):
        with app.state.session_factory.begin() as session:
            create_task(
                session,
                UUID(users[0]["id"]),
                "Rollback-only content",
                "telegram_text",
                bot_identity="foundation-test-bot",
                message_id=50,
            )
            raise RuntimeError("synthetic failure after task transaction changes")
    with db_engine.connect() as connection:
        for table in ("tasks", "source_receipts", "outbox"):
            assert connection.scalar(select(func.count()).select_from(db_tables[table])) == 0
        assert (
            connection.scalar(
                select(db_tables["users"].c.task_revision).where(
                    db_tables["users"].c.id == UUID(users[0]["id"])
                )
            )
            == 0
        )
